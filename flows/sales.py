# flows/sales.py

from config import USER_STATE, BASE_URL
from datetime import datetime, date, timedelta
import base64
import os
import time
import threading
import json
from db import get_full_car_details, get_latest_manufacturing_year, get_all_cars_paginated
from utils import is_valid_appointment_slot, get_available_booking_dates, resolve_date_from_text

# Initialize Groq client
from llm_config import groq_client as client, MODEL_NAME, smart_llm_call


# ============================
# AI INTENT DETECTION FUNCTION
# ============================
def detect_sales_intent_with_ai(phone, user_text, user_state):
    """
    Use Groq AI to analyze user intent and decide which flow to route to.
    Returns: (intent_type, confidence_score, extracted_data)
    """
    try:
        # Get car context if user has selected a car
        car_context = ""
        if user_state.get("selected_car_model"):
            car_context = f"""
Selected Car: {user_state.get('selected_car_make', '')} {user_state.get('selected_car_model', '')}
Variant: {user_state.get('selected_car_variant', '')}
Price: {user_state.get('selected_car_onroad_price_formatted', '')}
Year: {user_state.get('selected_car_manufacturing_year', '')}
Mileage: {user_state.get('selected_car_mileage', '')} km/l
"""
        
        # System prompt for intent detection
        system_prompt = """You are an AI intent detector for an automobile sales chatbot called AutoSherpa.

 Your task: Analyze the user's message and determine their INTENT based on the available options in the sales flow.

AVAILABLE SALES FLOW OPTIONS:
1. NEW_CARS - User wants to browse new cars, see models, check prices.
2. LOCATIONS - User wants to know about showroom locations.
3. FINANCE_OPTIONS - User EXPLICITLY wants to apply for a loan or start a finance application.
4. BOOK_TEST_DRIVE - User EXPLICITLY wants to book or schedule a test drive.
5. BACK_TO_HYUNDAI_LIST - User wants to go back to car list.
6. PREVIOUS_MENU - User wants to go back to previous menu.
7. MAIN_MENU - User wants to go to main menu.
8. END_CHAT - User wants to end conversation.
10. TALK_TO_ADVISOR - User wants to speak with human advisor, agent, customer care.
11. GENERAL_QUERY - User is asking a question about a car, price, mileage, features, or finance policies (e.g., "what is the down payment?", "how much mileage?").

CRITICAL RULES:
- If the user asks a QUESTION (what, how, when, is it, etc.), ALWAYS return GENERAL_QUERY.
- Only return FINANCE_OPTIONS if they say "I want to apply" or "start finance".
- Only return BOOK_TEST_DRIVE if they say "I want to book" or "schedule a drive".

Return your response in this JSON format ONLY:
{
    "intent": "one of the above intent names",
    "confidence": 0.0-1.0,
    "explanation": "brief reason for this classification",
    "extracted_info": {
        "car_model": "if mentioned",
        "budget": "if mentioned",
        "preferred_date": "if mentioned"
    }
}

Examples:
- User: "show me Verna" → NEW_CARS
- User: "what is the down payment" → GENERAL_QUERY
- User: "how is the mileage" → GENERAL_QUERY
- User: "can i get a loan" → GENERAL_QUERY
- User: "book test drive for creta" → BOOK_TEST_DRIVE
- User: "apply for finance" → FINANCE_OPTIONS
"""

        prompt = f"""
{system_prompt}

CAR CONTEXT (if user has selected a car):
{car_context}

CURRENT USER MESSAGE: "{user_text}"

Analyze this message and return ONLY the JSON response with intent classification.
"""

        # Call smart_llm_call
        result = smart_llm_call(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
            as_json=True
        )
        
        print(f"🎯 AI Intent Detection Result: {result}")
        
        return result
        
    except Exception as e:
        print(f"❌ AI Intent Detection Error: {e}")
        # Fallback to GENERAL_QUERY if AI fails
        return {
            "intent": "GENERAL_QUERY",
            "confidence": 0.5,
            "explanation": "AI detection failed, using fallback",
            "extracted_info": {}
        }


# ============================================================
# SALES — KEYWORD FALLBACK (used when Groq is unavailable)
# ============================================================
def _sales_keyword_intent(text: str) -> str:
    t = text.lower().strip()
    end_kw        = ["bye", "exit", "stop", "end", "close", "done", "finish", "thank you", "thanks"]
    main_menu_kw  = ["main menu", "home", "start", "beginning", "restart", "go back"]
    test_drive_kw = ["test drive", "testdrive", "drive", "trial", "book appointment", "book test"]
    finance_kw    = ["finance", "loan", "emi", "interest", "bank", "installment", "funding"]
    location_kw   = ["location", "showroom", "address", "where", "directions", "reach"]
    back_list_kw  = ["back to list", "car list", "show list", "all cars", "other car"]
    new_cars_kw   = ["new car", "new cars", "show car", "show me", "models", "hyundai",
                     "creta", "verna", "i20", "tucson", "exter", "venue", "alcazar", "ioniq", "aura"]
    agent_kw      = ["advisor", "agent", "human", "talk to someone", "customer care", "person"]
    # For keywords, we are VERY conservative. Only return actions if they are explicit.
    # Questions should default to GENERAL_QUERY.
    if "?" in t or any(t.startswith(s) for s in ["what", "how", "when", "can i", "is it"]):
        return "GENERAL_QUERY"

    if any(k in t for k in agent_kw):      return "TALK_TO_ADVISOR"
    if any(k in t for k in end_kw):        return "END_CHAT"
    if any(k in t for k in main_menu_kw):  return "MAIN_MENU"
    
    # Only return action intents if they look like commands, not questions
    if any(k in t for k in test_drive_kw) and ("book" in t or "schedule" in t): 
        return "BOOK_TEST_DRIVE"
    if any(k in t for k in finance_kw) and ("apply" in t or "start" in t):
        return "FINANCE_OPTIONS"
    
    if any(k in t for k in location_kw):   return "LOCATIONS"
    if any(k in t for k in back_list_kw):  return "BACK_TO_HYUNDAI_LIST"
    if any(k in t for k in new_cars_kw):   return "NEW_CARS"
    return "GENERAL_QUERY"


# ============================================================
# SALES — CENTRAL AI ROUTER  (called from every state)
# Groq AI → keyword fallback → action.  User NEVER gets stuck.
# ============================================================
def _sales_route_via_ai(phone, text, state):
    from webhook import send_whatsapp_message, send_list_message, send_button_message
    from utils import is_genuine_query

    # ── Gate: check keyword intent first (no AI cost) ─────────────────
    kw_check = _sales_keyword_intent(text)
    # If it's clearly GENERAL_QUERY from keywords and also not a real question,
    # skip AI entirely and re-prompt.
    if kw_check == "GENERAL_QUERY" and not is_genuine_query(text, state):
        print(f"[sales] _sales_route_via_ai: blocked non-query input | state={state} | text='{text}'")
        handle_sales(phone)
        return

    # Step 1 ─ Try Groq AI
    intent = "GENERAL_QUERY"
    extracted_car_model = None
    try:
        result     = detect_sales_intent_with_ai(phone, text, USER_STATE.get(phone, {}))
        intent     = result.get("intent", "GENERAL_QUERY")
        confidence = result.get("confidence", 0.5)
        extracted_car_model = result.get("extracted_info", {}).get("car_model", "")
        print(f"🎯 Sales AI → {intent} ({confidence}) | car_model={extracted_car_model} | state={state}")
        if confidence < 0.6:
            kw = _sales_keyword_intent(text)
            if kw != "GENERAL_QUERY":
                print(f"🔑 Low-conf → keyword: {kw}")
                intent = kw
    except Exception as ai_err:
        print(f"❌ Sales Groq failed → keyword fallback: {ai_err}")
        intent = _sales_keyword_intent(text)

    # ── Keyword-based car model extraction (typo-safe fallback) ──────────
    if not extracted_car_model:
        t = text.lower()
        car_keywords = ["venue n line", "creta n line", "i20 n line", "venue n-line", "creta n-line", "i20 n-line", 
                        "i20", "creta", "verna", "tucson", "exter", "venue",
                        "alcazar", "ioniq", "aura", "i10", "grand i10", "nios"]
        for kw in car_keywords:
            if kw in t:
                extracted_car_model = kw.replace("-", " ") # normalize n-line to n line
                break

    print(f"✅ Final sales intent: {intent} | car_model: {extracted_car_model}")

    # Step 2 ─ Execute
    
    # ── PRIORITY: If it's a genuine question, let the Expert answer first ──
    # This ensures that questions about finance, test drives, or models get
    # a natural language response (using RAG/Inventory) instead of a canned menu.
    if is_genuine_query(text, state) and intent not in ["MAIN_MENU", "END_CHAT", "BACK_TO_HYUNDAI_LIST", "PREVIOUS_MENU"]:
        try:
            from ai import handle_sales_ai_query
            ai_reply, follow_on = handle_sales_ai_query(phone, text, USER_STATE.get(phone, {}))
            send_whatsapp_message(phone, ai_reply)
            
            if follow_on and follow_on != "NONE":
                intent = follow_on
                
            # If the user is just asking a general question and no action intent is detected, stop here.
            # Otherwise, allow the action intent block below to trigger the structured flow menus.
            if intent == "GENERAL_QUERY":
                return
        except Exception as _e:
            print(f"[sales] AI Expert error, falling back to intent logic: {_e}")

    # ── ACTION INTENTS: Only fire if it's NOT a general question ───────────
    if intent == "NEW_CARS":
        # ── If user mentioned a specific car model → find & select it directly ──
        if extracted_car_model:
            import mysql.connector
            from config import DB_CONFIG
            try:
                conn   = mysql.connector.connect(**DB_CONFIG)
                cursor = conn.cursor(dictionary=True)
                cursor.execute("""
                    SELECT id, make, model FROM sales_car_details
                    WHERE LOWER(model) LIKE %s
                    ORDER BY `Ex-Showroom Price Base Model` DESC LIMIT 1
                """, (f"%{extracted_car_model.lower()}%",))
                matched_car = cursor.fetchone()
                cursor.close()
                conn.close()
                if matched_car:
                    print(f"🚗 Direct car match: {matched_car['make']} {matched_car['model']} (id={matched_car['id']})")
                    USER_STATE[phone]["selected_car_id"]     = str(matched_car["id"])
                    USER_STATE[phone]["selected_model_name"] = matched_car["model"]
                    show_model_details_with_great_choice(phone)
                    return
            except Exception as db_err:
                print(f"❌ Car search error: {db_err}")

        # ── No specific car → show full car list ─────────────────────────
        USER_STATE[phone]["state"]    = "SALES_NEW_CARS"
        USER_STATE[phone]["car_page"] = 1
        result = get_all_cars_paginated(page=1, per_page=8)
        if not result["cars"]:
            send_whatsapp_message(phone, "No cars available at the moment 😕"); return
        rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                for c in result["cars"]]
        if result["has_next"]: rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
        if result["has_prev"]: rows.append({"id": "PREV_PAGE", "title": "⬅️ Previous Page"})
        send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                          [{"title": f"Our Cars (Page {result['page']} of {result['total_pages']})", "rows": rows}])

    elif intent == "LOCATIONS":
        USER_STATE[phone]["state"] = "SALES_LOCATIONS"
        send_whatsapp_message(phone,
            "📍 *Our Locations*\n\nWe'd love to welcome you! Here are our locations:\n\n"
            "📍 *SHERPA HYUNDAI LOCATIONS:*\n\n"
            "🏢 *Main Showroom - Bangalore:*\n"
            "📍 Address: 123 MG Road, Bangalore - 560001\n"
            "📞 Phone: +91-9876543210\n"
            "🕒 Mon-Sat: 9:00 AM - 8:00 PM, Sun: 10:00 AM - 6:00 PM\n"
            "🅿️ Facilities: Free parking, Test drive facility, Customer lounge\n\n"
            "🏢 *Branch - Electronic City:*\n"
            "📍 Address: 456 Hosur Road, Electronic City - 560100\n"
            "📞 Phone: +91-9876543211\n"
            "🕒 Timings: Mon-Sat: 9:00 AM - 8:00 PM")
        send_button_message(phone, "What would you like to do next?", [
            {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅️ Previous Menu"}},
            {"type": "reply", "reply": {"id": "END_CHAT",      "title": "❌ Exit Chat"}}])

    elif intent == "FINANCE_OPTIONS":
        USER_STATE[phone]["state"] = "SALES_FINANCE"
        msg = ("Thank you for your interest! 😊\n\n"
               "Our Relationship Manager will contact you shortly.\n\n"
               "🌐 Visit: www.Autosherpas.com\n📞 Call: +91-7757832783")
        send_whatsapp_message(phone, msg)
        send_button_message(phone, "What would you like to do next?", [
            {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅️ Previous Menu"}},
            {"type": "reply", "reply": {"id": "END_CHAT",      "title": "❌ Exit Chat"}}])

    elif intent == "BOOK_TEST_DRIVE":
        if USER_STATE[phone].get("selected_car_id"):
            USER_STATE[phone]["state"] = "SALES_SELECT_APPOINTMENT_TYPE"
            send_list_message(phone, "What kind of Appointment you'd like to book?", "Select Appointment",
                [{"title": "Select Appointment Type", "rows": [
                    {"id": "APPT_HOME_VISIT", "title": "🏠 Book a Home Visit"},
                    {"id": "APPT_SHOWROOM",   "title": "🏢 Showroom Visit"}]}])
        else:
            USER_STATE[phone]["state"]    = "SALES_NEW_CARS"
            USER_STATE[phone]["car_page"] = 1
            result = get_all_cars_paginated(page=1, per_page=8)
            rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                    for c in result.get("cars", [])]
            if result.get("has_next"): rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
            send_list_message(phone, "Please select a car first to book a test drive:", "Select Car",
                              [{"title": "Our Cars", "rows": rows}] if rows else
                              [{"title": "Menu", "rows": [{"id": "MAIN_MENU", "title": "🏠 Main Menu"}]}])

    elif intent == "BACK_TO_HYUNDAI_LIST":
        show_car_type_selection(phone)

    elif intent == "MAIN_MENU":
        send_global_main_menu(phone)

    elif intent == "END_CHAT":
        USER_STATE.pop(phone, None)
        send_whatsapp_message(phone,
            "✅ Thank you for choosing AutoSherpa! 🚗\n\nIf you need anything else, just type *Hi* anytime.")

    elif intent == "TALK_TO_ADVISOR":
        from flows.service import bot
        bot.trigger_human_agent_confirmation(phone)
        USER_STATE[phone]["state"] = "SALES_AGENT_CONFIRMATION"

    else:
        # Fallback for anything else (should be rare now)
        handle_sales(phone)


def _sales_route_after_ai_follow_on(phone, text, state):
    """Route sales follow-on actions after the AI has already replied.

    This preserves the original user message for model extraction and
    avoids running the full AI expert flow a second time.
    """
    from webhook import send_whatsapp_message, send_list_message

    extracted_car_model = None
    t = text.lower()
    car_keywords = [
        "venue n line", "creta n line", "i20 n line", "venue n-line", "creta n-line", "i20 n-line",
        "i20", "creta", "verna", "tucson", "exter", "venue",
        "alcazar", "ioniq", "aura", "i10", "grand i10", "nios"
    ]
    for kw in car_keywords:
        if kw in t:
            extracted_car_model = kw.replace("-", " ")
            break

    resolved_cartype = _resolve_car_type_keyword(phone, text)
    if resolved_cartype:
        print(f"🚗 Sales follow-on detected car type keyword: '{text}' → {resolved_cartype}")
        sales_flow_handler(phone, resolved_cartype)
        return

    if extracted_car_model:
        import mysql.connector
        from config import DB_CONFIG
        try:
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, make, model FROM sales_car_details
                WHERE LOWER(model) LIKE %s
                ORDER BY `Ex-Showroom Price Base Model` DESC LIMIT 1
            """, (f"%{extracted_car_model.lower()}%",))
            matched_car = cursor.fetchone()
            cursor.close()
            conn.close()

            if matched_car:
                print(f"🚗 Direct car match after AI follow-on: {matched_car['make']} {matched_car['model']} (id={matched_car['id']})")
                USER_STATE[phone]["selected_car_id"] = str(matched_car["id"])
                USER_STATE[phone]["selected_model_name"] = matched_car["model"]
                show_model_details_with_great_choice(phone)
                return
        except Exception as db_err:
            print(f"❌ Car search error in follow-on routing: {db_err}")

    selected_type_id = USER_STATE.get(phone, {}).get("selected_car_type_id")
    if selected_type_id:
        try:
            from db import get_cars_by_type_id
            cars = get_cars_by_type_id(selected_type_id)
            if cars:
                USER_STATE[phone]["state"] = "SALES_NEW_CARS"
                USER_STATE[phone]["car_page"] = 1
                rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                        for c in cars]
                send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                                  [{"title": "Available Cars", "rows": rows}])
                return
        except Exception as e:
            print(f"❌ Error fetching filtered cars by selected type: {e}")

    # Fallback: show full car list if no specific model or type was matched
    USER_STATE[phone]["state"] = "SALES_NEW_CARS"
    USER_STATE[phone]["car_page"] = 1
    result = get_all_cars_paginated(page=1, per_page=8)
    if not result["cars"]:
        send_whatsapp_message(phone, "No cars available at the moment 😕")
        return

    rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
            for c in result["cars"]]
    if result["has_next"]:
        rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
    if result["has_prev"]:
        rows.append({"id": "PREV_PAGE", "title": "⬅️ Previous Page"})
    send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                      [{"title": f"Our Cars (Page {result['page']} of {result['total_pages']})", "rows": rows}])


def save_base64_to_image_and_get_url(model_name, base64_str):
    """Save base64 image to file system and return public URL"""
    if not base64_str:
        print("❌ No base64 string provided")
        return None

    try:
        # Create directory if it doesn't exist
        os.makedirs("static/cars", exist_ok=True)

        # Clean filename
        filename = f"{model_name.lower().replace(' ', '_')}.jpg"
        filepath = os.path.join("static", "cars", filename)

        # Decode and save
        image_data = base64.b64decode(base64_str)
        with open(filepath, "wb") as f:
            f.write(image_data)

        # Create public URL
        public_url = f"{BASE_URL}/static/cars/{filename}"
        print(f"✅ Image saved: {filepath}")
        print(f"✅ Public URL: {public_url}")
        
        return public_url
    except Exception as e:
        print(f"❌ Error saving image: {e}")
        return None


def save_base64_to_pdf_and_get_url(model_name, base64_str):
    """Save base64 PDF to file system and return public URL"""
    if not base64_str:
        print("❌ No base64 string provided for PDF")
        return None

    try:
        # Create directory if it doesn't exist
        os.makedirs("static/brochures", exist_ok=True)

        # Clean filename
        filename = f"{model_name.lower().replace(' ', '_')}_brochure.pdf"
        filepath = os.path.join("static", "brochures", filename)

        # Decode and save
        pdf_data = base64.b64decode(base64_str)
        with open(filepath, "wb") as f:
            f.write(pdf_data)

        # Create public URL
        public_url = f"{BASE_URL}/static/brochures/{filename}"
        print(f"✅ PDF saved: {filepath}")
        print(f"✅ Public URL: {public_url}")
        
        return public_url
    except Exception as e:
        print(f"❌ Error saving PDF: {e}")
        return None


def get_remaining_week_dates():
    today = datetime.now().date()
    dates = []

    start_day = today + timedelta(days=2)

    days_until_sunday = 6 - start_day.weekday()
    if days_until_sunday < 0:
        days_until_sunday = 0

    end_day = start_day + timedelta(days=days_until_sunday)

    current = start_day
    while current <= end_day:
        dates.append(current)
        current = current + timedelta(days=1)

    return dates


def get_next_week_dates():
    today = datetime.now().date()

    days_until_next_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_until_next_monday)

    dates = []
    for i in range(7):
        dates.append(next_monday + timedelta(days=i))

    return dates


def send_global_main_menu(phone):
    """Send the full global main menu when customer clicks Main Menu from within sales flow."""
    from webhook import send_list_message

    USER_STATE[phone]["state"] = "MENU"

    sections = [{
        "title": "Main Menu",
        "rows": [
            {"id": "menu_new_cars",    "title": "🔹 New Cars – Sales",    "description": "Browse new car models & prices"},
            {"id": "menu_used_cars",   "title": "🚗 Used Cars",           "description": "Browse, value or test drive pre-owned cars"},
            {"id": "menu_service",     "title": "🔧 Vehicle Service",     "description": "Book a service appointment"},
            {"id": "menu_insurance",   "title": "🔰 Vehicle Insurance",   "description": "Renew or get insurance for your vehicle"},
            {"id": "menu_refinancing", "title": "💳 Refinancing",         "description": "Reduce EMI or get a loan against your car"},
            {"id": "menu_about",       "title": "ℹ️ About Us",            "description": "Learn more about AutoSherpa"},
            {"id": "menu_contact",     "title": "📞 Contact & Support",   "description": "Get in touch with our team"},
        ]
    }]

    send_list_message(
        phone,
        "👋 Welcome to *AutoSherpa* — your one-stop automotive assistant 🚗✨\n\nHow can I help you today?",
        "View Options",
        sections
    )


def handle_sales(phone):
    """Entry point for sales flow - shows main menu"""
    from webhook import send_list_message
    
    USER_STATE[phone]["state"] = "SALES_MENU"
    
    sections = [
        {
            "title": "Main Menu",
            "rows": [
                {"id": "SALES_NEW", "title": "🚗 Latest Arrivals"},
                {"id": "SALES_LOCATIONS", "title": "📍 Our Sales Locations"}
            ]
        }
    ]

    send_list_message(
        phone,
        "Hi there! 👋 Welcome to *Autosherpa*.\n\n"
        "Searching for a new car in Bengaluru?\n"
        "I can help you with prices, brochure and book a test drive instantly.\n\n"
        "*Main Menu*\n"
        "Please select an option:",
        "Main Menu",
        sections
    )

def send_messages_in_strict_order(phone):

    from webhook import send_whatsapp_message, send_whatsapp_image, send_whatsapp_document, send_list_message

    try:
        car_id = USER_STATE[phone].get("selected_car_id")

        if not car_id:
            print("❌ No selected_car_id found")
            return

        car_data = get_full_car_details(car_id)

        if not car_data:
            print("❌ Car data not found")
            return

        car = car_data["car"]
        colors = car_data["colors"]
        fuel_types = car_data["fuel_types"]
        transmissions = car_data["transmissions"]

        model_name = car["model"]

        # Get latest manufacturing year for this car
        latest_year = get_latest_manufacturing_year(car_id)

        # Format on-road price with ₹ symbol and commas
        try:
            on_road_val = car.get('on_road_price') or 0
            formatted_price = f"₹{int(on_road_val):,}"
        except (ValueError, TypeError):
            formatted_price = "Price on request"

        # Store car details in user state for later use in appointments
        USER_STATE[phone]["selected_car_make"] = car["make"]
        USER_STATE[phone]["selected_car_model"] = car["model"]
        USER_STATE[phone]["selected_car_onroad_price_formatted"] = formatted_price
        USER_STATE[phone]["selected_car_onroad_price_raw"] = car.get("on_road_price", 0)
        USER_STATE[phone]["selected_car_variant"] = car.get("variant", "")
        USER_STATE[phone]["selected_car_mileage"] = car.get("mileage_kmph", "")
        USER_STATE[phone]["selected_car_manufacturing_year"] = latest_year

        # 🔥 Get car type from DB
        car_type = (car.get("type_name") or "").strip().lower()

        # 🔥 Templates for each car type
        type_templates = {
            "suv": {
                "emoji": "🚙",
                "title": "Powerful SUV",
                "line": "Perfect for family trips, adventure drives and commanding road presence."
            },
            "sedan": {
                "emoji": "🚘",
                "title": "Elegant Sedan",
                "line": "Designed for comfort, smooth rides and premium style."
            },
            "hatchback": {
                "emoji": "🚗",
                "title": "Smart Hatchback",
                "line": "Compact, efficient and perfect for city driving."
            },
            "electric": {
                "emoji": "⚡",
                "title": "Electric Vehicle",
                "line": "Eco-friendly, silent and future-ready mobility."
            }
        }

        # ==========================================
        # STEP 1: GREAT CHOICE
        # ==========================================
        print("1️⃣ STEP 1: Sending Great Choice message...")

        template = type_templates.get(car_type)

        if template:
            message1 = (
                f"{template['emoji']} *Great Choice! 😍*\n\n"
                f"You've selected a *{template['title']}*!\n"
                f"{template['line']}\n\n"
                f"⏳ *Please wait a few seconds while we fetch the car details and brochure...*\n\n"
                f"*Details for {model_name}*"
            )
        else:
            message1 = (
                f"✨ *Great Choice! 😍*\n\n"
                f"You've selected a *{car.get('type_name', 'Car')}*.\n\n"
                f"⏳ *Please wait a few seconds while we fetch the car details and brochure...*\n\n"
                f"*Details for {model_name}*"
            )

        send_whatsapp_message(phone, message1)
        time.sleep(2.0)

        # ==========================================
        # STEP 2: IMAGE
        # ==========================================
        print("2️⃣ STEP 2: Sending image...")

        base64_image = car.get("car_image_base64")

        if base64_image:
            image_url = None
            if base64_image.startswith("[") or base64_image.startswith("/images/") or base64_image.startswith("http"):
                try:
                    import json
                    from pymongo import MongoClient
                    from bson.objectid import ObjectId
                    import gridfs
                    from config import MONGO_URI
                    
                    url_path = json.loads(base64_image)[0] if base64_image.startswith("[") else base64_image
                    
                    if url_path.startswith("http"):
                        image_url = url_path
                    else:
                        file_id = url_path.split("/")[-1]
                        client = MongoClient(MONGO_URI)
                        db = client["whatsapp_bot"]
                        fs = gridfs.GridFS(db)
                        
                        grid_out = fs.get(ObjectId(file_id))
                        os.makedirs("static/cars", exist_ok=True)
                        filename = f"{car['make'].lower()}_{car['model'].lower().replace(' ', '_')}.jpg"
                        filepath = os.path.join("static", "cars", filename)
                        with open(filepath, "wb") as f:
                            f.write(grid_out.read())
                        image_url = f"{BASE_URL}/static/cars/{filename}"
                        print(f"✅ Extracted image directly from MongoDB GridFS: {image_url}")
                except Exception as e:
                    print(f"❌ Failed to extract image from MongoDB: {e}")
            else:
                if base64_image.startswith("data:"):
                    base64_image = base64_image.split(",")[1]
                print("Image length:", len(base64_image))
                image_url = save_base64_to_image_and_get_url(f"{car['make']}_{car['model']}", base64_image)

            print("Image URL:", image_url)

            if image_url:
                send_whatsapp_image(phone, image_url)
                time.sleep(3.5)
        else:
            print("No image found in DB")

        # ==========================================
        # STEP 3: DETAILS MESSAGE
        # ==========================================
        print("3️⃣ STEP 3: Sending car details...")

        try:
            ex_price = f"₹ {int(car.get('ex_showroom_price', 0)):,}"
        except (ValueError, TypeError):
            ex_price = "Price on request"
            
        try:
            onroad_price = f"₹ {int(car.get('on_road_price', 0)):,}"
        except (ValueError, TypeError):
            onroad_price = "Price on request"
            
        mileage = car.get("mileage_kmph", "N/A")

        colors_text = "\n".join([f"• {c}" for c in colors]) if colors else "• Not Available"
        fuel_text = ", ".join(fuel_types) if fuel_types else "Not Available"
        transmission_text = ", ".join(transmissions) if transmissions else "Not Available"

        message2 = (
            f"*{car['make']} {car['model']}*\n\n"
            f"1. Ex-Showroom Price (Base Model): {ex_price}\n"
            f"2. Ex-Showroom Price (Top Model): {onroad_price}\n"
            f"3. Fuel Options - {fuel_text}\n"
            f"4. Transmission - {transmission_text}\n"
            f"5. Avg Mileage - {mileage} KMPL\n"
            f"6. Available Colors - {', '.join(colors) if colors else 'Not Available'}\n"
        )

        send_whatsapp_message(phone, message2)
        time.sleep(2.0)

        # ==========================================
        # STEP 4: BROCHURE
        # ==========================================
        

        base64_pdf = car.get("brochure_pdf_base64")

        if base64_pdf:
            pdf_url = None
            if base64_pdf.startswith("[") or base64_pdf.startswith("/images/") or base64_pdf.startswith("http"):
                try:
                    import json
                    from pymongo import MongoClient
                    from bson.objectid import ObjectId
                    import gridfs
                    from config import MONGO_URI
                    
                    url_path = json.loads(base64_pdf)[0] if base64_pdf.startswith("[") else base64_pdf
                    
                    if url_path.startswith("http"):
                        pdf_url = url_path
                    else:
                        file_id = url_path.split("/")[-1]
                        client = MongoClient(MONGO_URI)
                        db = client["whatsapp_bot"]
                        fs = gridfs.GridFS(db)
                        
                        grid_out = fs.get(ObjectId(file_id))
                        os.makedirs("static/brochures", exist_ok=True)
                        filename = f"{car['make'].lower()}_{car['model'].lower().replace(' ', '_')}_brochure.pdf"
                        filepath = os.path.join("static", "brochures", filename)
                        with open(filepath, "wb") as f:
                            f.write(grid_out.read())
                        pdf_url = f"{BASE_URL}/static/brochures/{filename}"
                        print(f"✅ Extracted PDF directly from MongoDB GridFS: {pdf_url}")
                except Exception as e:
                    print(f"❌ Failed to extract PDF from MongoDB: {e}")
            else:
                if base64_pdf.startswith("data:"):
                    base64_pdf = base64_pdf.split(",")[1]
                pdf_url = save_base64_to_pdf_and_get_url(f"{car['make']}_{car['model']}", base64_pdf)

            if pdf_url:
                send_whatsapp_document(phone, pdf_url, f"{car['make']}_{car['model']}_Brochure.pdf")
                time.sleep(7)
        else:
            print("⚠️ No brochure found in DB")

        # ==========================================
        # STEP 5: OPTIONS MENU
        # ==========================================
        print("5️⃣ STEP 5: Sending options menu...")

        sections = [{
            "title": "Choose an Option",
            "rows": [
                {"id": "FINANCE_OPTIONS",     "title": "💰 Finance Options"},
                {"id": "BOOK_TEST_DRIVE",      "title": "🚗 Book a Test Drive"},
                {"id": "BACK_TO_HYUNDAI_LIST", "title": "⬅️ Back to Cars List"},
                {"id": "MAIN_MENU",            "title": "🏠 Main Menu"},
                {"id": "END_CHAT",             "title": "❌ Exit Chat"}
            ]
        }]

        send_list_message(
            phone,
            "*Which option would you like to choose?*",
            "Select Option",
            sections
        )

        USER_STATE[phone]["state"] = "SALES_MODEL_DETAILS"

        print("🎉 Strict order flow completed successfully")

    except Exception as e:
        print(f"❌ ERROR in strict order messaging: {e}")
        import traceback
        print(traceback.format_exc())
        
def show_model_details_with_great_choice(phone):
    """Show model details in STRICT ORDER"""
    model_name = USER_STATE[phone].get("selected_model_name", "")
    
    if not model_name:
        print("❌ No model name found in user state")
        return
    
    print(f"🎬 Starting show_model_details_with_great_choice for {model_name}")
    
    # Start sending messages in strict order in background thread
    thread = threading.Thread(
        target=send_messages_in_strict_order,
        args=(phone,)
    )
    thread.daemon = True
    thread.start()
    
    print(f"✅ Started strict order thread for {model_name}")


def show_options_menu_only(phone):
    """Show only the options menu without car details"""
    from webhook import send_list_message
    
    sections = [{
        "title": "Choose an Option",
        "rows": [
            {"id": "FINANCE_OPTIONS",     "title": "💰 Finance Options"},
            {"id": "BOOK_TEST_DRIVE",      "title": "🚗 Book a Test Drive"},
            {"id": "BACK_TO_HYUNDAI_LIST", "title": "⬅️ Back to Cars List"},
            {"id": "MAIN_MENU",            "title": "🏠 Main Menu"},
            {"id": "END_CHAT",             "title": "❌ Exit Chat"}
        ]
    }]

    send_list_message(
        phone,
        "*Which option would you like to choose?*",
        "Select Option",
        sections
    )

    USER_STATE[phone]["state"] = "SALES_MODEL_DETAILS"


def show_car_type_selection(phone):
    """Show the 'What type of car are you looking for?' category selection screen."""
    from webhook import send_list_message, send_whatsapp_message
    from db import get_available_car_types

    USER_STATE[phone]["state"] = "SALES_SELECT_CAR_TYPE"

    car_types = get_available_car_types()

    if not car_types:
        # Fallback: show full list if no types found
        USER_STATE[phone]["state"] = "SALES_NEW_CARS"
        USER_STATE[phone]["car_page"] = 1
        result = get_all_cars_paginated(page=1, per_page=8)
        if not result["cars"]:
            send_whatsapp_message(phone, "No cars available at the moment 😕")
            return
        rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                for c in result["cars"]]
        if result["has_next"]:
            rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
        send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                          [{"title": f"Our Cars (Page {result['page']} of {result['total_pages']})", "rows": rows}])
        return

    type_emojis = {"suv": "🚙", "sedan": "🚘", "hatchback": "🚗", "electric": "⚡"}
    rows = []
    for ct in car_types:
        emoji = type_emojis.get(ct["type_name"].lower(), "🚘")
        rows.append({
            "id": f"CARTYPE_{ct['id']}",
            "title": f"{emoji} {ct['type_name']}"
        })

    send_list_message(
        phone,
        "🚗 *What type of car are you looking for?*\n\nPlease select a category:",
        "Select Type",
        [{"title": "Car Categories", "rows": rows}]
    )


def _resolve_car_type_keyword(phone: str, text: str) -> str | None:
    """
    Map a free-text car-type keyword (e.g. "electric", "suv", "sedan")
    to the correct CARTYPE_<id> button ID by looking up the DB.

    Returns the button ID string (e.g. "CARTYPE_3") or None if no match.
    This ensures that typing a car category always triggers the proper
    filtered car-list flow instead of an AI text response.
    """
    from db import get_available_car_types
    t = text.lower().strip()

    # Map common synonyms to canonical type names
    type_synonyms = {
        "electric":   ["electric", "ev", "e-car", "ecar", "electric car", "ioniq", "ecars"],
        "suv":        ["suv", "sport utility", "sports utility", "4x4", "offroad", "off road"],
        "sedan":      ["sedan", "saloon"],
        "hatchback":  ["hatchback", "hatch", "compact", "small car"],
        "mpv":        ["mpv", "van", "minivan", "people carrier"],
    }

    matched_type_name = None
    for type_name, keywords in type_synonyms.items():
        if any(kw in t for kw in keywords):
            matched_type_name = type_name
            break

    if not matched_type_name:
        return None

    # Look up the car type in DB to get its actual ID
    try:
        car_types = get_available_car_types()
        for ct in car_types:
            if ct["type_name"].lower() == matched_type_name:
                return f"CARTYPE_{ct['id']}"
            # Partial match fallback (e.g. "Electric Vehicle" contains "electric")
            if matched_type_name in ct["type_name"].lower():
                return f"CARTYPE_{ct['id']}"
    except Exception as e:
        print(f"[sales] _resolve_car_type_keyword DB error: {e}")

    return None


def sales_flow_handler(phone, text):
    """Handle all sales flow interactions"""
    from webhook import send_whatsapp_message, send_list_message, send_button_message
    
    state = USER_STATE.get(phone, {}).get("state", "")
    
    # ── HUMAN AGENT CONFIRMATION ────────────────────────────────────────
    if state == "SALES_AGENT_CONFIRMATION":
        from flows.service import bot
        if text == "AGENT_YES":
            vehicle_reg = USER_STATE.get(phone, {}).get("vehicle_reg") or USER_STATE.get(phone, {}).get("reg_number")
            bot.initiate_agent_handoff(phone, vehicle_reg=vehicle_reg,
                                       handoff_reason="Customer requested live agent from Sales flow")
            return
        elif text == "AGENT_NO":
            # Restore the saved state and re-prompt the customer
            prev_state = USER_STATE[phone].pop("_saved_state", None)
            if prev_state and prev_state != "SALES_AGENT_CONFIRMATION":
                USER_STATE[phone]["state"] = prev_state
                send_whatsapp_message(phone, "✅ No problem! Let's continue where we left off.")
                from webhook import _reprompt_flow_state
                _reprompt_flow_state(phone, prev_state)
            else:
                USER_STATE[phone]["state"] = "SALES_MENU"
                handle_sales(phone)
            return
        else:
            # Unknown input — re-show the confirmation prompt
            bot.trigger_human_agent_confirmation(phone)
            USER_STATE[phone]["state"] = "SALES_AGENT_CONFIRMATION"
            return

    print(f"🚗 Sales flow: state={state}, text='{text}'")
    
    # ==========================================
    # GLOBAL BUTTON HANDLERS (SALES) - Check these first
    # ==========================================
    if text == "MAIN_MENU":
        send_global_main_menu(phone)
        return
    
    if text == "BACK_TO_HYUNDAI_LIST":
        show_car_type_selection(phone)
        return

    if text == "END_CHAT":
        USER_STATE.pop(phone, None)
        send_whatsapp_message(
            phone,
            "✅ Thank you for choosing AutoSherpa! 🚗\n\nIf you need anything else, just type *Hi* anytime."
        )
        return
    
    if text == "PREVIOUS_MENU":
        current_state = USER_STATE[phone].get("state", "")
        print(f"🔙 PREVIOUS_MENU clicked - current state: {current_state}")

        # From car model related screens → show only options menu without car details
        if current_state in ["SALES_FINAL_CONFIRMATION", "SALES_MODEL_DETAILS"]:
            show_options_menu_only(phone)
            return
        
        # From Used Cars or Locations → go back to main menu cleanly
        elif current_state in ["SALES_LOCATIONS"]:
            handle_sales(phone)
            return
        
        # From Finance Options → go back to options menu
        elif current_state == "SALES_FINANCE":
            show_options_menu_only(phone)
            return
        
        # Any other case → safely go to main menu
        else:
            handle_sales(phone)
            return
    
    # ==========================================
    # SALES MAIN MENU
    # ==========================================
    if state == "SALES_MENU":

        if text == "SALES_NEW":
            # ✅ SHOW CAR TYPE SELECTION FIRST, then filter by type
            USER_STATE[phone]["state"] = "SALES_SELECT_CAR_TYPE"

            from db import get_available_car_types
            car_types = get_available_car_types()

            if not car_types:
                # Fallback: go straight to full list if no types found
                USER_STATE[phone]["state"] = "SALES_NEW_CARS"
                USER_STATE[phone]["car_page"] = 1
                from db import get_all_cars_paginated
                result = get_all_cars_paginated(page=1, per_page=8)
                if not result["cars"]:
                    send_whatsapp_message(phone, "No cars available at the moment 😕")
                    return
                rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                        for c in result["cars"]]
                if result["has_next"]:
                    rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
                send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                                  [{"title": f"Our Cars (Page {result['page']} of {result['total_pages']})", "rows": rows}])
                return

            type_emojis = {"suv": "🚙", "sedan": "🚘", "hatchback": "🚗", "electric": "⚡"}
            rows = []
            for ct in car_types:
                emoji = type_emojis.get(ct["type_name"].lower(), "🚘")
                rows.append({
                    "id": f"CARTYPE_{ct['id']}",
                    "title": f"{emoji} {ct['type_name']}"
                })

            send_list_message(
                phone,
                "🚗 *What type of car are you looking for?*\n\nPlease select a category:",
                "Select Type",
                [{"title": "Car Categories", "rows": rows}]
            )
            return

            
        elif text == "SALES_LOCATIONS":
            USER_STATE[phone]["state"] = "SALES_LOCATIONS"
            
            send_whatsapp_message(
                phone,
                "📍 *Our Locations*\n\n"
                "We'd love to welcome you! Here are our locations:\n\n"

                "📍 *SHERPA HYUNDAI LOCATIONS:*\n\n"

                "🏢 *Main Showroom - Bangalore:*\n"
                "📍 Address: 123 MG Road, Bangalore - 560001\n"
                "📞 Phone: +91-9876543210\n"
                "🕒 Mon-Sat: 9:00 AM - 8:00 PM, Sun: 10:00 AM - 6:00 PM\n"
                "🅿️ Facilities: Free parking, Test drive facility, Customer lounge\n\n"

                "🏢 *Branch - Electronic City:*\n"
                "📍 Address: 456 Hosur Road, Electronic City - 560100\n"
                "📞 Phone: +91-9876543211\n"
                "🕒 Timings: Mon-Sat: 9:00 AM - 8:00 PM\n\n"

                "🗺️ *How to Reach:*\n"
                "🚇 Metro: MG Road Metro Station (2 min walk)\n"
                "🚌 Bus: Multiple bus routes available\n"
                "🚗 Car: Easy access from Ring Road"
            )

            
            buttons = [
                {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅️ Previous Menu"}},
                {"type": "reply", "reply": {"id": "END_CHAT", "title": "❌ Exit Chat"}}
            ]
            
            send_button_message(phone, "What would you like to do next?", buttons)
            return

        elif text == "SALES_TALK_AGENT":
            # Save current state before switching to confirmation
            USER_STATE[phone]["_saved_state"] = state
            from flows.service import bot
            bot.trigger_human_agent_confirmation(phone)
            USER_STATE[phone]["state"] = "SALES_AGENT_CONFIRMATION"
            return

        # ── Free text → try car-type keyword first, then central AI router ──
        else:
            resolved_cartype = _resolve_car_type_keyword(phone, text)
            if resolved_cartype:
                # User typed a car category keyword → jump directly to SALES_SELECT_CAR_TYPE
                USER_STATE[phone]["state"] = "SALES_SELECT_CAR_TYPE"
                sales_flow_handler(phone, resolved_cartype)
                return
            _sales_route_via_ai(phone, text, state)
            return

    # ==========================================
    # SELECT CAR TYPE (SUV / Sedan / Hatchback / Electric)
    # ==========================================
    elif state == "SALES_SELECT_CAR_TYPE":

        if text.startswith("CARTYPE_"):
            type_id = text.replace("CARTYPE_", "")
            USER_STATE[phone]["selected_car_type_id"] = type_id
            USER_STATE[phone]["state"] = "SALES_NEW_CARS"
            USER_STATE[phone]["car_page"] = 1

            from db import get_cars_by_type_id
            cars = get_cars_by_type_id(type_id)

            if not cars:
                send_whatsapp_message(phone, "No cars available for this category at the moment 😕")
                return

            rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]} for c in cars]

            send_list_message(
                phone,
                "Here are our cars (Premium → Entry Level):",
                "Select Car",
                [{"title": "Available Cars", "rows": rows}]
            )
            return

        else:
            # ── Car-type keyword resolver: map free text → CARTYPE_<id> ──
            # This ensures typing "electric", "suv", "sedan", "hatchback"
            # triggers the actual car-type list flow instead of AI text.
            resolved_cartype = _resolve_car_type_keyword(phone, text)
            if resolved_cartype:
                # Re-inject as if the user clicked the CARTYPE_ button
                sales_flow_handler(phone, resolved_cartype)
                return
            # No car-type keyword matched → AI router
            _sales_route_via_ai(phone, text, state)
            return

    # ==========================================
    # NEW CARS - MODEL SELECTION WITH PAGINATION
    # ==========================================
    elif state == "SALES_NEW_CARS":
        
        # Handle Next Page
        if text == "NEXT_PAGE":
            current_page = USER_STATE[phone].get("car_page", 1)
            next_page = current_page + 1
            
            from db import get_all_cars_paginated
            result = get_all_cars_paginated(page=next_page, per_page=8)
            
            if not result["cars"]:
                send_whatsapp_message(phone, "No more cars available 😕")
                return
            
            USER_STATE[phone]["car_page"] = next_page
            
            rows = []
            for car in result["cars"]:
                title = f"{car['make']} {car['model']}"
                rows.append({
                    "id": f"MODEL_{car['id']}",
                    "title": title[:24]
                })
            
            # Add navigation buttons
            if result["has_next"]:
                rows.append({
                    "id": "NEXT_PAGE",
                    "title": "➡️ Next Page"
                })
            
            if result["has_prev"]:
                rows.append({
                    "id": "PREV_PAGE",
                    "title": "⬅️ Previous Page"
                })
            
            sections = [{
                "title": f"Our Cars (Page {result['page']} of {result['total_pages']})",
                "rows": rows
            }]
            
            send_list_message(
                phone,
                "Here are our cars (Premium → Entry Level):",
                "Select Car",
                sections
            )
            return
            
        # Handle Previous Page
        elif text == "PREV_PAGE":
            current_page = USER_STATE[phone].get("car_page", 1)
            prev_page = current_page - 1
            
            if prev_page < 1:
                send_whatsapp_message(phone, "You're already on the first page 😊")
                return
            
            from db import get_all_cars_paginated
            result = get_all_cars_paginated(page=prev_page, per_page=8)
            
            USER_STATE[phone]["car_page"] = prev_page
            
            rows = []
            for car in result["cars"]:
                title = f"{car['make']} {car['model']}"
                rows.append({
                    "id": f"MODEL_{car['id']}",
                    "title": title[:24]
                })
            
            # Add navigation buttons
            if result["has_next"]:
                rows.append({
                    "id": "NEXT_PAGE",
                    "title": "➡️ Next Page"
                })
            
            if result["has_prev"]:
                rows.append({
                    "id": "PREV_PAGE",
                    "title": "⬅️ Previous Page"
                })
            
            sections = [{
                "title": f"Our Cars (Page {result['page']} of {result['total_pages']})",
                "rows": rows
            }]
            
            send_list_message(
                phone,
                "Here are our cars (Premium → Entry Level):",
                "Select Car",
                sections
            )
            return
            
        # Handle Model Selection
        elif text.startswith("MODEL_"):
            car_id = text.replace("MODEL_", "")

            USER_STATE[phone]["selected_car_id"] = car_id

            from db import get_car_by_id, get_latest_manufacturing_year
            car = get_car_by_id(car_id)

            if not car:
                send_whatsapp_message(phone, "Sorry, car details not found 😕")
                return

            # Get latest manufacturing year for this car
            latest_year = get_latest_manufacturing_year(car_id)

            # Format on-road price with ₹ symbol and commas
            formatted_price = f"₹{int(car['on_road_price']):,}"

            USER_STATE[phone]["selected_model_name"] = car["model"]
            USER_STATE[phone]["selected_car_make"] = car["make"]
            USER_STATE[phone]["selected_car_model"] = car["model"]
            USER_STATE[phone]["selected_car_onroad_price_formatted"] = formatted_price
            USER_STATE[phone]["selected_car_onroad_price_raw"] = car["on_road_price"]
            USER_STATE[phone]["selected_car_variant"] = car.get("variant", "")
            USER_STATE[phone]["selected_car_mileage"] = car.get("mileage_kmph", "")
            USER_STATE[phone]["selected_car_manufacturing_year"] = latest_year

            show_model_details_with_great_choice(phone)
            return

        # ── Free text → central AI router ──────────────────────────────────
        else:
            _sales_route_via_ai(phone, text, state)
            return

        # ==========================================
    # MODEL DETAILS MENU
    # ==========================================
    elif state == "SALES_MODEL_DETAILS":
        
        if text == "FINANCE_OPTIONS":
            USER_STATE[phone]["state"] = "SALES_FINANCE"
            
            finance_msg = (
                "Thank you for your interest! 😊\n\n"
                "Our Relationship Manager will contact you shortly.\n\n"
                "🌐 Visit: www.Autosherpas.com\n"
                "📞 Call: +91-7757832783"
            )
            send_whatsapp_message(phone, finance_msg)
            
            # Only show Previous Menu and Exit Chat buttons for Finance Options
            buttons = [
                {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅️ Previous Menu"}},
                {"type": "reply", "reply": {"id": "END_CHAT", "title": "❌ Exit Chat"}}
            ]
            send_button_message(phone, "What would you like to do next?", buttons)
            # ✅ Store Finance Options click in DB
            try:
                from ai import store_interaction
                car_model = USER_STATE.get(phone, {}).get("selected_car_model", "")
                store_interaction(phone, f"💰 Finance Options - {car_model}",
                                  finance_msg, "sales")
            except Exception as _e:
                print(f"[sales] store_interaction error: {_e}")
            return            
        elif text == "BOOK_TEST_DRIVE":
            USER_STATE[phone]["state"] = "SALES_SELECT_APPOINTMENT_TYPE"
            
            # Show appointment type selection first
            sections = [
                {
                    "title": "Select Appointment Type",
                    "rows": [
                        {"id": "APPT_HOME_VISIT", "title": "🏠 Book a Home Visit"},
                        {"id": "APPT_SHOWROOM", "title": "🏢 Showroom Visit"}
                    ]
                }
            ]
            
            send_list_message(
                phone,
                "What kind of Appointment you'd like to book?",
                "Select Appointment",
                sections
            )
            return
            
        elif text == "BACK_TO_HYUNDAI_LIST":
            # ✅ Go back to car type category selection
            show_car_type_selection(phone)
            return

        # ── Free text → central AI router ──────────────────────────────────
        else:
            _sales_route_via_ai(phone, text, state)
            return

        # ==========================================
    # SELECT APPOINTMENT TYPE
    # ==========================================
    elif state == "SALES_SELECT_APPOINTMENT_TYPE":
        
        appointment_map = {
            "APPT_HOME_VISIT": "Home Visit",
            "APPT_SHOWROOM": "Showroom Visit"
        }
        
        if text in appointment_map:
            USER_STATE[phone]["appointment_type"] = appointment_map[text]
            USER_STATE[phone]["state"] = "SALES_SELECT_WHEN"

            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            now = datetime.now()
            schedule_rows = []
            if now.hour < 18:
                schedule_rows.append({"id": "WHEN_TODAY", "title": "Today"})
            schedule_rows += [
                {"id": "WHEN_TOMORROW",   "title": "Tomorrow"},
                {"id": "WHEN_LATER_WEEK", "title": "Later This Week"},
                {"id": "WHEN_NEXT_WEEK",  "title": "Next Week"}
            ]
            # Ask for when they'd like to schedule
            sections = [{"title": "Select Schedule", "rows": schedule_rows}]

            appointment_type = appointment_map[text]
            send_list_message(
                phone,
                f"*{appointment_type.upper()}*\n\nExcellent! Let's schedule your {appointment_type.lower()}. When would you prefer?",
                "Select Date",
                sections
            )
            return
        else:
            # ── Free-text appointment type resolver ──────────────────────
            t = text.lower().strip()
            resolved = None
            if any(k in t for k in ["home", "house", "my place", "come to me", "visit me", "home visit", "doorstep"]):
                resolved = "APPT_HOME_VISIT"
            elif any(k in t for k in ["showroom", "office", "visit", "come in", "in person", "walk in"]):
                resolved = "APPT_SHOWROOM"
            if resolved:
                USER_STATE[phone]["appointment_type"] = appointment_map[resolved]
                USER_STATE[phone]["state"] = "SALES_SELECT_WHEN"
                # ── 6 PM cutoff: hide Today option after 6 PM ─────────────
                now = datetime.now()
                schedule_rows = []
                if now.hour < 18:
                    schedule_rows.append({"id": "WHEN_TODAY", "title": "Today"})
                schedule_rows += [
                    {"id": "WHEN_TOMORROW",   "title": "Tomorrow"},
                    {"id": "WHEN_LATER_WEEK", "title": "Later This Week"},
                    {"id": "WHEN_NEXT_WEEK",  "title": "Next Week"}
                ]
                sections = [{"title": "Select Schedule", "rows": schedule_rows}]
                send_list_message(phone,
                    f"✅ Got it — *{appointment_map[resolved]}*.\n\nWhen would you prefer?",
                    "Select Date", sections)
            else:
                send_list_message(phone, "Please select an appointment type:", "Select Appointment",
                    [{"title": "Select Appointment Type", "rows": [
                        {"id": "APPT_HOME_VISIT", "title": "🏠 Book a Home Visit"},
                        {"id": "APPT_SHOWROOM",   "title": "🏢 Showroom Visit"}
                    ]}]
                )
    
    # ==========================================
    # SELECT WHEN
    # ==========================================
    elif state == "SALES_SELECT_WHEN":
        when_map = {
            "WHEN_TODAY": "Today",
            "WHEN_TOMORROW": "Tomorrow",
            "WHEN_LATER_WEEK": "Later This Week",
            "WHEN_NEXT_WEEK": "Next Week"
        }
        selected = when_map.get(text)
            
        if not selected:
            # ── Smart Date Resolver (Free-text) ──
            from datetime import date as dt_date
            today = dt_date.today()
            all_dates = sorted(list(set([today, today + timedelta(days=1)] + get_remaining_week_dates() + get_next_week_dates())))
            resolved_id = resolve_date_from_text(text, all_dates, prefix="RESOLVED_WHEN_")
            
            if resolved_id:
                date_val = resolved_id.replace("RESOLVED_WHEN_", "")
                actual_date = datetime.strptime(date_val, "%Y%m%d").date()
                
                valid, reason = is_valid_appointment_slot(actual_date)
                if not valid:
                    send_whatsapp_message(phone, reason)
                    return
                    
                date_display = actual_date.strftime("%d-%m-%Y")
                USER_STATE[phone]["preferred_when"] = date_display
                USER_STATE[phone]["preferred_when_raw"] = actual_date
                USER_STATE[phone]["state"] = "SALES_SELECT_TIME"
                
                send_whatsapp_message(phone, f"✅ Got it — *{date_display}*.")
                
                sections = [{"title": "Select Time Slot", "rows": [
                    {"id": "TIME_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                    {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                    {"id": "TIME_EVENING",   "title": "Evening (4 PM - 7 PM)"}]}]
                
                send_list_message(phone, "Which time works best for you?", "Select Time", sections)
                return
            
            # If still not resolved, re-prompt
            send_list_message(phone, "When would you prefer your appointment?", "Select Date",
                [{"title": "Select Schedule", "rows": [
                    {"id": "WHEN_TODAY",      "title": "Today"},
                    {"id": "WHEN_TOMORROW",   "title": "Tomorrow"},
                    {"id": "WHEN_LATER_WEEK", "title": "Later This Week"},
                    {"id": "WHEN_NEXT_WEEK",  "title": "Next Week"}]}])
            return

        # If we reach here, 'selected' is one of the button values
        if selected in ["Today", "Tomorrow"]:
            # Store the actual date for Today/Tomorrow
            today = datetime.now().date()
            if selected == "Today":
                # ── After-6PM auto-advance: move today → tomorrow silently ──
                from utils import get_smart_default_date
                actual_date, was_redirected = get_smart_default_date()
                if was_redirected:
                    date_display = actual_date.strftime("%d-%m-%Y")
                    USER_STATE[phone]["preferred_when"]     = date_display
                    USER_STATE[phone]["preferred_when_raw"] = actual_date
                    USER_STATE[phone]["state"] = "SALES_SELECT_TIME"
                    send_whatsapp_message(
                        phone,
                        f"⏰ Since it's after 6 PM, same-day appointments are unavailable.\n\n"
                        f"✅ I've scheduled your appointment for *tomorrow ({date_display})* instead.\n\n"
                        f"Please choose a preferred time slot 👇"
                    )
                    sections = [{"title": "Select Time Slot", "rows": [
                        {"id": "TIME_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                        {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                        {"id": "TIME_EVENING",   "title": "Evening (4 PM - 7 PM)"}
                    ]}]
                    send_list_message(phone, "Which time works best for you?", "Select Time", sections)
                    return
                date_display = actual_date.strftime("%d-%m-%Y")
            else:  # Tomorrow
                actual_date = today + timedelta(days=1)
                date_display = actual_date.strftime("%d-%m-%Y")

            # ── 6 PM cutoff: reject Today bookings after 6 PM ─────────────
            valid, reason = is_valid_appointment_slot(actual_date)
            if not valid:
                send_whatsapp_message(phone, reason)
                # Re-show schedule picker without Today
                now = datetime.now()
                schedule_rows = []
                if now.hour < 18:
                    schedule_rows.append({"id": "WHEN_TODAY", "title": "Today"})
                schedule_rows += [
                    {"id": "WHEN_TOMORROW",   "title": "Tomorrow"},
                    {"id": "WHEN_LATER_WEEK", "title": "Later This Week"},
                    {"id": "WHEN_NEXT_WEEK",  "title": "Next Week"}
                ]
                send_list_message(phone, "Please choose a different date:", "Select Date",
                                  [{"title": "Select Schedule", "rows": schedule_rows}])
                return

            USER_STATE[phone]["preferred_when"] = date_display  # Store formatted date for display
            USER_STATE[phone]["preferred_when_raw"] = actual_date  # Store actual date object if needed
            USER_STATE[phone]["state"] = "SALES_SELECT_TIME"

            sections = [
                {
                    "title": "Select Time Slot",
                    "rows": [
                        {"id": "TIME_MORNING", "title": "Morning (10 AM - 12 PM)"},
                        {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                        {"id": "TIME_EVENING", "title": "Evening (4 PM - 7 PM)"}
                    ]
                }
            ]

            send_list_message(phone, "Which time works best for you?", "Select Time", sections)
            return

        if selected == "Later This Week":
            USER_STATE[phone]["state"] = "SALES_LATER_WEEK"

            week_dates = get_remaining_week_dates()
            rows = []

            for d in week_dates:
                rows.append({
                    "id": f"SALES_WEEK_{d.strftime('%Y%m%d')}",
                    "title": d.strftime("%A (%d %b)")
                })

            send_list_message(
                phone,
                "Please choose a date:",
                "Select Date",
                [{"title": "Select Date", "rows": rows}]
            )
            return

        if selected == "Next Week":
            USER_STATE[phone]["state"] = "SALES_NEXT_WEEK"

            week_dates = get_next_week_dates()
            rows = []

            for d in week_dates:
                rows.append({
                    "id": f"SALES_NEXTWEEK_{d.strftime('%Y%m%d')}",
                    "title": d.strftime("%A (%d %b)")
                })

            send_list_message(
                phone,
                "Please choose a date:",
                "Select Date",
                [{"title": "Select Date", "rows": rows}]
            )
            return
    
    # ==========================================
    # LATER THIS WEEK - DATE PICK
    # ==========================================
    elif state == "SALES_LATER_WEEK" and text.startswith("SALES_WEEK_"):
        date_str = text.replace("SALES_WEEK_", "")
        selected_date = datetime.strptime(date_str, "%Y%m%d").date()
        date_display = selected_date.strftime("%d-%m-%Y")

        USER_STATE[phone]["preferred_when"] = date_display
        USER_STATE[phone]["preferred_when_raw"] = selected_date
        USER_STATE[phone]["state"] = "SALES_SELECT_TIME"

        sections = [
            {
                "title": "Select Time Slot",
                "rows": [
                    {"id": "TIME_MORNING", "title": "Morning (10 AM - 12 PM)"},
                    {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                    {"id": "TIME_EVENING", "title": "Evening (4 PM - 7 PM)"}
                ]
            }
        ]

        send_list_message(phone, "Which time works best for you?", "Select Time", sections)
        return

    # ==========================================
    # NEXT WEEK - DATE PICK
    # ==========================================
    elif state == "SALES_NEXT_WEEK" and text.startswith("SALES_NEXTWEEK_"):
        date_str = text.replace("SALES_NEXTWEEK_", "")
        selected_date = datetime.strptime(date_str, "%Y%m%d").date()
        date_display = selected_date.strftime("%d-%m-%Y")

        USER_STATE[phone]["preferred_when"] = date_display
        USER_STATE[phone]["preferred_when_raw"] = selected_date
        USER_STATE[phone]["state"] = "SALES_SELECT_TIME"

        sections = [
            {
                "title": "Select Time Slot",
                "rows": [
                    {"id": "TIME_MORNING", "title": "Morning (10 AM - 12 PM)"},
                    {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                    {"id": "TIME_EVENING", "title": "Evening (4 PM - 7 PM)"}
                ]
            }
        ]

        send_list_message(phone, "Which time works best for you?", "Select Time", sections)
        return
        
    # ==========================================
    # SELECT TIME SLOT
    # ==========================================
    elif state == "SALES_SELECT_TIME":
        
        time_map = {
            "TIME_MORNING": "Morning (10 AM - 12 PM)",
            "TIME_AFTERNOON": "Afternoon (12 PM - 4 PM)",
            "TIME_EVENING": "Evening (4 PM - 7 PM)"
        }
        
        if text in time_map:
            USER_STATE[phone]["preferred_time"] = time_map[text]
            
            # Check if this is a Home Visit appointment
            if USER_STATE[phone].get("appointment_type") == "Home Visit":
                USER_STATE[phone]["state"] = "SALES_COLLECT_ADDRESS"
                send_whatsapp_message(
                    phone,
                    "Great! 👍\n\nFor the Home Visit, please provide your *complete address* (including house number, street, area, city, and pincode):"
                )
            else:
                USER_STATE[phone]["state"] = "SALES_COLLECT_NAME"
                send_whatsapp_message(
                    phone,
                    "Great! 👍\n\nPlease provide your *full name*:"
                )
            return
        else:
            # ── Free-text time slot resolver ──────────────────────────────
            t = text.lower().strip()
            resolved_time = None
            if any(k in t for k in ["morning", "10 am", "10am", "before noon", "early", "forenoon"]):
                resolved_time = "TIME_MORNING"
            elif any(k in t for k in ["afternoon", "12 pm", "12pm", "1 pm", "2 pm", "3 pm", "noon", "midday"]):
                resolved_time = "TIME_AFTERNOON"
            elif any(k in t for k in ["evening", "4 pm", "5 pm", "6 pm", "4pm", "5pm", "6pm", "after 4", "late"]):
                resolved_time = "TIME_EVENING"
            if resolved_time:
                USER_STATE[phone]["preferred_time"] = time_map[resolved_time]
                if USER_STATE[phone].get("appointment_type") == "Home Visit":
                    USER_STATE[phone]["state"] = "SALES_COLLECT_ADDRESS"
                    send_whatsapp_message(phone,
                        f"✅ Got it — *{time_map[resolved_time]}*.\n\nFor the Home Visit, please provide your *complete address*:")
                else:
                    USER_STATE[phone]["state"] = "SALES_COLLECT_NAME"
                    send_whatsapp_message(phone,
                        f"✅ Got it — *{time_map[resolved_time]}*.\n\nPlease provide your *full name*:")
            else:
                send_list_message(phone, "Which time works best for you?", "Select Time",
                    [{"title": "Select Time Slot", "rows": [
                        {"id": "TIME_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                        {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                        {"id": "TIME_EVENING",   "title": "Evening (4 PM - 7 PM)"}
                    ]}])
            return
    
    # ==========================================
    # COLLECT ADDRESS (ONLY for Home Visit)
    # ==========================================
    elif state == "SALES_COLLECT_ADDRESS":
        USER_STATE[phone]["customer_address"] = text
        USER_STATE[phone]["state"] = "SALES_COLLECT_NAME"
        
        send_whatsapp_message(
            phone,
            "Thank you! 📍\n\nNow, please provide your *full name*:"
        )
        return
    
    # ==========================================
    # COLLECT NAME
    # ==========================================
    elif state == "SALES_COLLECT_NAME":
        USER_STATE[phone]["customer_name"] = text.strip()
        USER_STATE[phone]["state"] = "SALES_COLLECT_LICENSE"

        buttons = [
            {"type": "reply", "reply": {"id": "LICENSE_YES", "title": "✅ Yes"}},
            {"type": "reply", "reply": {"id": "LICENSE_NO",  "title": "❌ No"}}
        ]
        send_button_message(
            phone,
            "Do you have a valid driving license?",
            buttons
        )
        return

    # ==========================================
    # COLLECT DRIVING LICENSE & CONFIRMATION
    # ==========================================
    elif state == "SALES_COLLECT_LICENSE":
        if text == "LICENSE_YES":
            USER_STATE[phone]["driving_license"] = "Yes"
        elif text == "LICENSE_NO":
            USER_STATE[phone]["driving_license"] = "No"
        else:
            buttons = [
                {"type": "reply", "reply": {"id": "LICENSE_YES", "title": "✅ Yes"}},
                {"type": "reply", "reply": {"id": "LICENSE_NO",  "title": "❌ No"}}
            ]
            send_button_message(phone, "Please select Yes or No for driving license:", buttons)
            return

        USER_STATE[phone]["state"] = "SALES_FINAL_CONFIRMATION"

        # Save to database
        try:
            import mysql.connector
            from config import DB_CONFIG
            
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor()
            
            # Create table with address and driving_license columns
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sales_appointments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    mobile_number VARCHAR(20),
                    customer_name VARCHAR(100),
                    customer_address TEXT,
                    driving_license VARCHAR(10) DEFAULT NULL,
                    car_model VARCHAR(100),
                    manufacturing_year YEAR,
                    onroad_price VARCHAR(50),
                    appointment_type VARCHAR(50),
                    preferred_when VARCHAR(50),
                    preferred_time VARCHAR(50),
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Get values from user state
            car_model = USER_STATE[phone].get("selected_car_model", "")
            manufacturing_year = USER_STATE[phone].get("selected_car_manufacturing_year", "")
            onroad_price_formatted = USER_STATE[phone].get("selected_car_onroad_price_formatted", "")
            appointment_type = USER_STATE[phone].get("appointment_type", "")
            
            # Only get address if it's a Home Visit
            customer_address = None
            if appointment_type == "Home Visit":
                customer_address = USER_STATE[phone].get("customer_address", "")
            
            # Insert the appointment with all fields
            cursor.execute("""
                INSERT INTO sales_appointments 
                (mobile_number, customer_name, customer_address, driving_license, car_model, manufacturing_year, 
                 onroad_price, appointment_type, preferred_when, preferred_time, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed')
            """, (
                phone,
                USER_STATE[phone].get("customer_name", ""),
                customer_address,
                USER_STATE[phone].get("driving_license", None),
                car_model,
                manufacturing_year,
                onroad_price_formatted,
                appointment_type,
                USER_STATE[phone].get("preferred_when", ""),
                USER_STATE[phone].get("preferred_time", "")
            ))
            
            appointment_id = cursor.lastrowid
            conn.commit()
            cursor.close()
            conn.close()
            
            print(f"✅ Appointment saved with ID: {appointment_id}")
            print(f"✅ Car: {car_model} ({manufacturing_year}) - {onroad_price_formatted}")
            print(f"✅ Date: {USER_STATE[phone].get('preferred_when', '')}")
            if customer_address:
                print(f"✅ Address: {customer_address}")
            
        except Exception as e:
            print(f"❌ Database error: {e}")
            import traceback
            print(traceback.format_exc())
        
        # Send final confirmation with exactly the format you want
        customer_name = USER_STATE[phone].get("customer_name", "")
        car_model = USER_STATE[phone].get("selected_car_model", "")
        manufacturing_year = USER_STATE[phone].get("selected_car_manufacturing_year", "")
        onroad_price_formatted = USER_STATE[phone].get("selected_car_onroad_price_formatted", "")
        preferred_when = USER_STATE[phone].get("preferred_when", "")
        preferred_time = USER_STATE[phone].get("preferred_time", "")
        appointment_type = USER_STATE[phone].get("appointment_type", "")
        driving_license = USER_STATE[phone].get("driving_license", "")
        customer_address = USER_STATE[phone].get("customer_address", "") if USER_STATE[phone].get("appointment_type") == "Home Visit" else None
        
        # Build confirmation message based on appointment type
        confirmation_msg = f"📋 *{appointment_type.upper()} CONFIRMED!*\n\n"
        confirmation_msg += f"👤 Name: {customer_name}\n"
        confirmation_msg += f"🪪 Driving License: {driving_license}\n"
        confirmation_msg += f"🚗 Car: {car_model} ({manufacturing_year}) - {onroad_price_formatted}\n"
        
        # Add address ONLY for Home Visit appointments
        if appointment_type == "Home Visit" and customer_address:
            confirmation_msg += f"📍 Address: {customer_address}\n"
            
        confirmation_msg += f"📅 Date: {preferred_when}\n"
        confirmation_msg += f"🕐 Time: {preferred_time}\n"
        confirmation_msg += f"📱 Mobile: {phone}\n\n"
        confirmation_msg += f"Need Help? Call +91-9876543210"
        
        send_whatsapp_message(phone, confirmation_msg)
        
        send_list_message(
            phone,
            "What would you like to do next?",
            "Select Option",
            [{
                "title": "Next Steps",
                "rows": [
                    {"id": "FINANCE_OPTIONS",      "title": "💰 Finance Options"},
                    {"id": "BOOK_TEST_DRIVE",       "title": "🚗 Book a Test Drive"},
                    {"id": "BACK_TO_HYUNDAI_LIST",  "title": "⬅️ Back to Cars List"},
                    {"id": "MAIN_MENU",             "title": "🏠 Main Menu"},
                    {"id": "END_CHAT",              "title": "❌ Exit Chat"}
                ]
            }]
        )
        # ✅ Store appointment confirmation in DB
        try:
            from ai import store_interaction
            store_interaction(phone,
                              f"Appointment confirmed - {appointment_type} for {car_model}",
                              confirmation_msg, "sales")
        except Exception as _e:
            print(f"[sales] store_interaction error: {_e}")
        return
    
    # ── Catch-all: any other state + free text → central AI router ────────
    else:
        _sales_route_via_ai(phone, text, state)