# flows/used_cars.py

from config import USER_STATE
from db import get_db_connection
import re
import json
import os
from datetime import datetime, date, timedelta
from utils import is_valid_appointment_slot, get_available_booking_dates, resolve_date_from_text, is_genuine_query, understand_customer_in_flow_context

# Initialize Groq client
from llm_config import groq_client as client, MODEL_NAME

# ============================
# INTELLIGENT FLOW COMPREHENSION
# ============================
def _handle_valuation_with_ai_understanding(phone, text, state):
    """
    MUST be called FIRST in valuation state handlers.
    Uses AI to understand what customer really wants BEFORE following flow rigidly.
    
    Returns: (should_proceed_flow, ai_response, flow_action)
    - should_proceed_flow: bool - True if normal flow should continue
    - ai_response: str - AI's response if action is not CONTINUE_FLOW, else None
    - flow_action: str - specific action (BACK, MAIN_MENU, TALK_TO_ADVISOR, etc.)
    """
    from webhook import send_whatsapp_message
    
    try:
        # Build flow context
        valuation_type = USER_STATE.get(phone, {}).get("valuation_type", "One Way Selling")
        flow_context = {
            "valuation_type": valuation_type,
            "brand": USER_STATE.get(phone, {}).get("valuation_brand", ""),
            "model": USER_STATE.get(phone, {}).get("valuation_model", ""),
            "year": USER_STATE.get(phone, {}).get("valuation_year", ""),
            "fuel": USER_STATE.get(phone, {}).get("valuation_fuel", ""),
            "transmission": USER_STATE.get(phone, {}).get("valuation_transmission", ""),
            "km": USER_STATE.get(phone, {}).get("valuation_km", ""),
            "ownership": USER_STATE.get(phone, {}).get("valuation_ownership", ""),
        }
        
        # Get AI understanding
        understanding = understand_customer_in_flow_context(phone, text, state, flow_context)
        
        action = understanding.get("action", "CONTINUE_FLOW")
        flow_action = understanding.get("flow_action")
        response = understanding.get("response")
        confidence = understanding.get("confidence", 0.5)
        
        print(f"🧠 Valuation AI Understanding: action={action}, flow_action={flow_action}, confidence={confidence}")
        
        # ── Case 1: Continue with normal flow ──────────────────────────────
        if action == "CONTINUE_FLOW":
            return (True, None, None)
        
        # ── Case 2: Customer asking a question about insurance/warranty/process ──
        if action == "ANSWER_QUERY":
            if response:
                send_whatsapp_message(phone, response)
            # After answering, re-prompt the current state
            return (False, response, None)
        
        # ── Case 3: Customer wants to switch context ──────────────────────────
        if action == "SWITCH_CONTEXT":
            if flow_action == "BACK":
                return (False, "✅ Going back to valuation menu.", "BACK")
            elif flow_action == "MAIN_MENU":
                return (False, None, "MAIN_MENU")
            elif flow_action == "TALK_TO_ADVISOR":
                return (False, None, "TALK_TO_ADVISOR")
            elif flow_action == "END_CONVERSATION":
                return (False, None, "END_CONVERSATION")
            else:
                return (False, "✅ Let me help you with that.", None)
        
        # ── Case 4: Need clarification ──────────────────────────────────────
        if action == "CLARIFY":
            if response:
                send_whatsapp_message(phone, response)
            # Re-prompt after clarification
            return (False, response, None)
        
        # ── Case 5: Ambiguous input ────────────────────────────────────────
        if action == "AMBIGUOUS":
            if response:
                send_whatsapp_message(phone, response or "I'm not sure I understood. Could you please clarify?")
            return (False, response, None)
        
        # Default: continue flow
        return (True, None, None)
        
    except Exception as e:
        print(f"❌ AI understanding error in valuation: {e}")
        # On error, proceed with normal flow
        return (True, None, None)


def is_valid_mobile(number):
    """
    Validates Indian 10-digit mobile number.
    Must:
    - Be exactly 10 digits
    - Start with 6, 7, 8, or 9
    """
    pattern = r"^[6-9]\d{9}$"
    return re.match(pattern, number) is not None


# ============================
# AI INTENT DETECTION FUNCTION
# ============================
def detect_used_cars_intent_with_ai(phone, user_text, user_state):
    """
    Use Groq AI to analyze user intent and decide which flow to route to.
    Returns: (intent_type, confidence_score, extracted_data)
    """
    try:
        # Get context about current state
        current_state = user_state.get("state", "")
        has_budget = user_state.get("selected_budget") is not None
        has_type = user_state.get("selected_type") is not None
        has_brand = user_state.get("selected_brand") is not None
        
        context = f"""
Current State: {current_state}
Has Budget Selected: {has_budget}
Has Car Type Selected: {has_type}
Has Brand Selected: {has_brand}
"""
        
        # System prompt for intent detection
        system_prompt = """You are an AI intent detector for a used car chatbot called Sherpa Hyundai Used Cars.

Your task: Analyze the user's message and determine their INTENT based on the available options in the used cars flow.

AVAILABLE USED CARS FLOW OPTIONS:
1. BROWSE_USED_CARS - User wants to browse used cars, search for cars, find cars
2. GET_VALUATION - User wants to get valuation for their car, sell their car
3. CONTACT_US - User wants to contact team, get callback, visit showroom
4. BOOK_TEST_DRIVE - User wants to book test drive for a specific car
5. CHANGE_CRITERIA - User wants to change search criteria (budget, type, brand)
6. BACK_TO_CARS - User wants to go back to car list
7. PREVIOUS_MENU - User wants to go back to previous menu
8. MAIN_MENU - User wants to go to main menu
9. END_CONVERSATION - User wants to end conversation
10. SPECIFIC_CAR - User mentioned a specific car model (like i20, Verna, Creta)
11. TALK_TO_ADVISOR - User wants to speak with human advisor, agent, customer care
12. GENERAL_QUERY - User has general question about used cars (let AI answer)

Return your response in this JSON format ONLY:
{
    "intent": "one of the above intent names",
    "confidence": 0.0-1.0,
    "explanation": "brief reason for this classification",
    "extracted_info": {
        "car_model": "specific car model mentioned, e.g., 'i20', 'Verna'",
        "budget": "if mentioned",
        "brand": "if mentioned",
        "car_type": "if mentioned (SUV, Sedan, etc.)"
    }
}

Examples:
- User: "show me used cars" → {"intent": "BROWSE_USED_CARS", "confidence": 0.95}
- User: "i want to sell my car" → {"intent": "GET_VALUATION", "confidence": 0.95}
- User: "i20 used cars" → {"intent": "SPECIFIC_CAR", "confidence": 0.95, "extracted_info": {"car_model": "i20"}}
- User: "contact your team" → {"intent": "CONTACT_US", "confidence": 0.95}
- User: "book test drive for creta" → {"intent": "BOOK_TEST_DRIVE", "confidence": 0.95, "extracted_info": {"car_model": "Creta"}}
- User: "change my search" → {"intent": "CHANGE_CRITERIA", "confidence": 0.95}
- User: "back to main menu" → {"intent": "MAIN_MENU", "confidence": 0.95}
- User: "end chat" → {"intent": "END_CONVERSATION", "confidence": 0.95}
- User: "i want to get valuation for my car" → {"intent": "GET_VALUATION", "confidence": 0.95}
"""

        prompt = f"""
{system_prompt}

CURRENT CONTEXT:
{context}

CURRENT USER MESSAGE: "{user_text}"

Analyze this message and return ONLY the JSON response with intent classification.
"""

        # Call Groq
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content.strip())
        print(f"🎯 Used Cars AI Intent Detection Result: {result}")
        
        return result
        
    except Exception as e:
        print(f"❌ Used Cars AI Intent Detection Error: {e}")
        return {
            "intent": "GENERAL_QUERY",
            "confidence": 0.5,
            "explanation": "AI detection failed, using fallback",
            "extracted_info": {}
        }


# =========================
# ENTRY: Used Cars Main Menu
# =========================
def handle_used_cars(phone):
    from webhook import send_list_message

    USER_STATE.setdefault(phone, {})
    USER_STATE[phone]["state"] = "USED_CARS_MENU"

    sections = [{
        "title": "Select",
        "rows": [
            {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
            {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
            {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
            {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
            {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"}
        ]
    }]

    send_list_message(
        phone,
        "Hello! 👋 Welcome to *Sherpa Hyundai Used Cars*.\n"
        "I'm here to help you find your perfect used car. How can I assist you today?",
        "Select",
        sections
    )

# ============================================================
# USED CARS — KEYWORD FALLBACK (used when Groq is unavailable)
# ============================================================
def _used_keyword_intent(text: str) -> str:
    t = text.lower().strip()
    end_kw        = ["bye", "exit", "stop", "end", "close", "done", "finish", "thank you", "thanks"]
    main_menu_kw  = ["main menu", "home", "start", "beginning", "restart"]
    test_drive_kw = ["test drive", "testdrive", "drive", "trial", "book test"]
    contact_kw    = ["contact", "call", "speak", "talk", "reach", "callback", "customer care"]
    valuation_kw  = ["valuation", "sell my car", "sell car", "car value", "appraisal", "exchange", "trade in"]
    change_kw     = ["change", "new search", "different", "reset", "modify", "update search"]
    back_cars_kw  = ["back to cars", "show cars again", "car list"]
    browse_kw     = ["browse", "show cars", "find car", "search", "used cars", "available cars", "list"]
    agent_kw      = ["advisor", "agent", "human", "talk to someone", "customer care", "person", "connect to agent", "live agent"]
    if any(k in t for k in agent_kw):      return "TALK_TO_ADVISOR"
    if any(k in t for k in end_kw):        return "END_CONVERSATION"
    if any(k in t for k in main_menu_kw):  return "MAIN_MENU"
    if any(k in t for k in test_drive_kw): return "BOOK_TEST_DRIVE"
    if any(k in t for k in contact_kw):    return "CONTACT_US"
    if any(k in t for k in valuation_kw):  return "GET_VALUATION"
    if any(k in t for k in change_kw):     return "CHANGE_CRITERIA"
    if any(k in t for k in back_cars_kw):  return "BACK_TO_CARS"
    if any(k in t for k in browse_kw):     return "BROWSE_USED_CARS"
    return "GENERAL_QUERY"



# ============================================================
# USED CARS — CENTRAL AI ROUTER (called from every state)
# Groq AI → keyword fallback → action.  User NEVER gets stuck.
# ============================================================
def _used_route_via_ai(phone, text, state):
    from webhook import send_whatsapp_message, send_list_message, send_button_message
    from utils import is_genuine_query

    # ── Gate: check keyword intent first (no AI cost) ─────────────────
    kw_check = _used_keyword_intent(text)
    if kw_check == "GENERAL_QUERY" and not is_genuine_query(text, state):
        print(f"[used_cars] _used_route_via_ai: blocked non-query input | state={state} | text='{text}'")
        # Re-show used cars menu — never call AI for non-questions
        handle_used_cars(phone)
        return

    # Step 1 ─ Try Groq AI
    intent = "GENERAL_QUERY"
    try:
        result     = detect_used_cars_intent_with_ai(phone, text, USER_STATE.get(phone, {}))
        intent     = result.get("intent", "GENERAL_QUERY")
        confidence = result.get("confidence", 0.5)
        print(f"🎯 Used Cars AI → {intent} ({confidence}) | state={state}")
        if confidence < 0.6:
            kw = _used_keyword_intent(text)
            if kw != "GENERAL_QUERY":
                print(f"🔑 Low-conf → keyword: {kw}")
                intent = kw
    except Exception as ai_err:
        print(f"❌ Used Cars Groq failed → keyword fallback: {ai_err}")
        intent = _used_keyword_intent(text)
    print(f"✅ Final used-cars intent: {intent}")

    def _reprompt():
        _reprompt_used_cars_state(phone)

    def _main_menu():
        USER_STATE[phone]["state"] = "USED_CARS_MENU"
        sections = [{"title": "Select", "rows": [
            {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
            {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
            {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
            {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
            {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
            {"id": "END_CONVERSATION",       "title": "👋 End Conversation"}
        ]}]
        send_list_message(phone,
            "Hello! 👋 Welcome to *Sherpa Hyundai Used Cars*.\n"
            "How can I assist you today?", "Select", sections)

    # Step 2 ─ Execute
    if intent == "BROWSE_USED_CARS":
        USER_STATE[phone]["state"] = "USED_SELECT_BUDGET"
        log_user_activity(phone, "used_car")
        send_list_message(phone,
            "Great choice! Let's find your perfect car. First, what's your budget range?",
            "Select",
            [{"title": "Select Budget", "rows": [
                {"id": "under1",  "title": "Under ₹1 Lakhs"},
                {"id": "1-3",     "title": "₹1–3 Lakhs"},
                {"id": "3-5",     "title": "₹3–5 Lakhs"},
                {"id": "5-10",    "title": "₹5–10 Lakhs"},
                {"id": "above10", "title": "Above ₹10 Lakhs"}
            ]}])

    elif intent == "GET_VALUATION":
        log_user_activity(phone, "valuation")
        USER_STATE[phone]["state"] = "USED_VALUATION_MODE"
        send_list_message(phone,
            "Great! I'll help you get a valuation for your car.\n\n"
            "Would you like to exchange or go for One Way Selling?",
            "Select",
            [{"title": "Select", "rows": [
                {"id": "VALUATION_EXCHANGE", "title": "🔁 Exchange"},
                {"id": "VALUATION_ONEWAY",   "title": "💰 One Way Selling"}
            ]}])

    elif intent == "CONTACT_US":
        log_user_activity(phone, "contact_us")
        handle_contact_us(phone)

    elif intent == "TALK_TO_ADVISOR":
        from flows.service import bot
        USER_STATE[phone]["_saved_state"] = state
        bot.trigger_human_agent_confirmation(phone)
        USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"

    elif intent == "BOOK_TEST_DRIVE":
        if USER_STATE[phone].get("selected_used_car_id"):
            USER_STATE[phone]["state"] = "USED_BOOK_SELECT_WHEN"
            car_name = USER_STATE[phone].get("selected_used_car_name", "this car")
            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            from datetime import datetime as _dt_used
            _now_used = _dt_used.now()
            _sched_rows = []
            if _now_used.hour < 18:
                _sched_rows.append({"id": "USED_WHEN_TODAY", "title": "Today (if available)"})
            _sched_rows += [
                {"id": "USED_WHEN_TOMORROW",   "title": "Tomorrow"},
                {"id": "USED_WHEN_LATER_WEEK", "title": "Later This Week"},
                {"id": "USED_WHEN_NEXT_WEEK",  "title": "Next Week"}
            ]
            send_list_message(phone,
                f"Let's schedule your {car_name} test drive. When would you prefer?",
                "Select",
                [{"title": "Select Schedule", "rows": _sched_rows}])
        else:
            send_whatsapp_message(phone, "Please select a car first. Let me show you available options.")
            USER_STATE[phone]["state"] = "USED_SELECT_BUDGET"
            log_user_activity(phone, "used_car")
            send_list_message(phone, "What's your budget range?", "Select",
                [{"title": "Select Budget", "rows": [
                    {"id": "under1",  "title": "Under ₹1 Lakhs"},
                    {"id": "1-3",     "title": "₹1–3 Lakhs"},
                    {"id": "3-5",     "title": "₹3–5 Lakhs"},
                    {"id": "5-10",    "title": "₹5–10 Lakhs"},
                    {"id": "above10", "title": "Above ₹10 Lakhs"}
                ]}])

    elif intent == "CHANGE_CRITERIA":
        USER_STATE[phone]["state"] = "USED_CONFIRM_CHANGE"
        send_button_message(phone,
            "I'll help you update your search criteria.\nDo you want to change your search now?",
            [{"type": "reply", "reply": {"id": "CONFIRM_CHANGE_YES", "title": "Yes"}},
             {"type": "reply", "reply": {"id": "CONFIRM_CHANGE_NO",  "title": "No"}}])

    elif intent == "BACK_TO_CARS":
        if USER_STATE[phone].get("selected_budget"):
            USER_STATE[phone]["offset"] = 0
            _show_used_cars(phone)
        else:
            _main_menu()

    elif intent == "MAIN_MENU":
        _main_menu()

    elif intent == "END_CONVERSATION":
        session_id = USER_STATE.get(phone, {}).get("session_id")
        if session_id:
            try:
                from chat_history import close_chat_session
                close_chat_session(session_id)
            except Exception:
                pass
        USER_STATE.pop(phone, None)
        send_whatsapp_message(phone,
            "Thank you for chatting! 👋 Say Hi or Start to begin a new conversation anytime.")

    else:
        # GENERAL_QUERY — ONLY answer with AI if it's a genuine question.
        # If the user typed a form input or short ambiguous text, re-show the menu.
        if is_genuine_query(text, state):
            try:
                from ai import handle_used_cars_ai_query
                ai_reply, follow_on = handle_used_cars_ai_query(phone, text, USER_STATE.get(phone, {}))
                send_whatsapp_message(phone, ai_reply)
                
                # Handle follow-on actions (e.g., Talk to Advisor)
                if follow_on == "TALK_TO_ADVISOR":
                    from flows.service import bot
                    USER_STATE[phone]["_saved_state"] = state
                    bot.trigger_human_agent_confirmation(phone)
                    USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
                else:
                    # No follow-on → re-prompt the original flow state
                    _reprompt()
                    
            except Exception as _e:
                print(f"[used_cars] AI general query error: {_e}")
                _main_menu()
        else:
            print(f"[used_cars] GENERAL_QUERY blocked — not a genuine query: '{text}'")
            _main_menu()


def get_remaining_week_dates_used():
    from datetime import datetime, timedelta

    today = datetime.now().date()
    dates = []

    # Start from day after tomorrow
    start_day = today + timedelta(days=2)

    # Calculate days until Sunday
    days_until_sunday = 6 - start_day.weekday()
    if days_until_sunday < 0:
        days_until_sunday = 0

    end_day = start_day + timedelta(days=days_until_sunday)

    current = start_day
    while current <= end_day:
        dates.append(current)
        current += timedelta(days=1)

    return dates

def get_next_week_dates_used():
    from datetime import datetime, timedelta

    today = datetime.now().date()

    # Find next Monday
    days_until_next_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_until_next_monday)

    dates = []

    for i in range(7):  # Monday to Sunday
        dates.append(next_monday + timedelta(days=i))

    return dates




def get_total_cars_count(min_price, max_price, car_type, brand):
    conn = get_db_connection()
    cur = conn.cursor()

    query = """
        SELECT COUNT(*)
        FROM carstockdata
        WHERE LOWER(ready_for_sales) = 'available'
          AND estimated_selling_price BETWEEN %s AND %s
    """
    params = [min_price, max_price]

    if car_type != "all":
        query += " AND LOWER(`type`) = LOWER(%s)"
        params.append(car_type)

    if brand != "all":
        query += " AND LOWER(make) = LOWER(%s)"
        params.append(brand)

    cur.execute(query, params)
    total = cur.fetchone()[0]

    cur.close()
    conn.close()
    return total

# =========================
# VALUATION HELPER FUNCTIONS
# =========================
def log_valuation_selection(phone, step, selected_option):
    """
    Lightweight in-memory logger for mid-flow valuation steps.
    The full row is written to valuation_selections at the end of the flow
    (EXCH_COLLECT_CITY handler) with all columns populated at once.
    No partial inserts here — the table schema requires all fields together.
    """
    print(f"[valuation_log] phone={phone} | {step}={selected_option}")

# =========================
# USER ACTIVITY LOGGING
# =========================
def log_user_activity(phone, action_type):
    """
    Log which section the user visits: browse, valuation, contact, about, start.
    Matches reference: action_type ENUM('used_car','valuation','contact_us','about_us','start','other')
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_activity_log (contact_number, action_type)
            VALUES (%s, %s)
        """, (str(phone)[-10:], action_type))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Activity logged: {phone} → {action_type}")
    except Exception as e:
        print(f"❌ Failed to log user activity: {e}")

# =========================
# NOTIFY REQUEST (Brand not available)
# =========================
def save_notify_request(phone, budget, brand_requested, car_type, customer_name=None):
    """
    Save a 'notify me when available' request when a user's desired brand
    is not currently in stock.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO notify_requests
                (price_range, brand_requested, car_type, customer_name, phone_number, created_on)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """, (budget, brand_requested, car_type, customer_name, str(phone)[-10:]))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Notify request saved: {phone} wants {brand_requested} in {budget}")
    except Exception as e:
        print(f"❌ Failed to save notify request: {e}")

def get_distinct_makes():
    """Fetch distinct car makes from database for valuation."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT make 
            FROM carstockdata 
            WHERE make IS NOT NULL 
              AND TRIM(make) != ''
              AND LOWER(ready_for_sales) = 'available'
            ORDER BY make
        """)
        makes = [row[0] for row in cur.fetchall() if row[0]]
        cur.close()
        conn.close()
        return makes
    except Exception as e:
        print(f"Error fetching distinct makes: {e}")
        return []

def get_models_for_make(make_name):
    """Fetch distinct models for a specific make."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT model 
            FROM carstockdata 
            WHERE LOWER(make) = LOWER(%s) 
              AND model IS NOT NULL 
              AND TRIM(model) != ''
              AND LOWER(ready_for_sales) = 'available'
            ORDER BY model
        """, (make_name,))
        models = [row[0] for row in cur.fetchall() if row[0]]
        cur.close()
        conn.close()
        return models
    except Exception as e:
        print(f"Error fetching models for {make_name}: {e}")
        return []

def get_distinct_years():
    """Fetch distinct manufacturing years from database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT manufacturing_year 
            FROM carstockdata 
            WHERE manufacturing_year IS NOT NULL
              AND LOWER(ready_for_sales) = 'available'
            ORDER BY manufacturing_year DESC
        """)
        years = [str(row[0]) for row in cur.fetchall() if row[0]]
        cur.close()
        conn.close()
        return years[:7]  # Return top 7 years
    except Exception as e:
        print(f"Error fetching distinct years: {e}")
        return ["2024", "2023", "2022", "2021", "2020"]

def get_distinct_fuel_types():
    """Fetch distinct fuel types from database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT fuel_type 
            FROM carstockdata 
            WHERE fuel_type IS NOT NULL 
              AND TRIM(fuel_type) != ''
              AND LOWER(ready_for_sales) = 'available'
            ORDER BY fuel_type
        """)
        fuel_types = [row[0] for row in cur.fetchall() if row[0]]
        cur.close()
        conn.close()
        return fuel_types
    except Exception as e:
        print(f"Error fetching distinct fuel types: {e}")
        return ["Petrol", "Diesel", "CNG", "Electric"]

def get_distinct_transmission_types():
    """Fetch distinct transmission types from database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT transmission_type 
            FROM carstockdata 
            WHERE transmission_type IS NOT NULL 
              AND TRIM(transmission_type) != ''
              AND LOWER(ready_for_sales) = 'available'
            ORDER BY transmission_type
        """)
        transmissions = [row[0] for row in cur.fetchall() if row[0]]
        cur.close()
        conn.close()
        return transmissions
    except Exception as e:
        print(f"Error fetching distinct transmission types: {e}")
        return ["Manual", "Automatic"]

# =========================
# HELPERS (DB FETCH)
# =========================
def map_budget_to_range(budget_id):
    budget_map = {
        "under1": (0, 100000),
        "1-3": (100001, 300000),
        "3-5": (300001, 500000),
        "5-10": (500001, 1000000),
        "above10": (1000001, 10**12)
    }
    return budget_map.get(budget_id, (0, 10**12))


def get_types_for_budget(min_price, max_price):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT `type`
        FROM carstockdata
        WHERE estimated_selling_price BETWEEN %s AND %s
          AND LOWER(ready_for_sales) = 'available'
          AND IFNULL(TRIM(`type`), '') != ''
        ORDER BY `type`
    """, (min_price, max_price))
    rows = [r[0] for r in cur.fetchall() if r[0]]
    cur.close()
    conn.close()
    return rows


def get_brands_for_budget_and_type(min_price, max_price, car_type):
    conn = get_db_connection()
    cur = conn.cursor()

    if car_type == "all":
        cur.execute("""
            SELECT DISTINCT make
            FROM carstockdata
            WHERE estimated_selling_price BETWEEN %s AND %s
              AND LOWER(ready_for_sales) = 'available'
              AND IFNULL(TRIM(make), '') != ''
            ORDER BY make
        """, (min_price, max_price))
    else:
        cur.execute("""
            SELECT DISTINCT make
            FROM carstockdata
            WHERE estimated_selling_price BETWEEN %s AND %s
              AND LOWER(`type`) = LOWER(%s)
              AND LOWER(ready_for_sales) = 'available'
              AND IFNULL(TRIM(make), '') != ''
            ORDER BY make
        """, (min_price, max_price, car_type))

    rows = [r[0] for r in cur.fetchall() if r[0]]
    cur.close()
    conn.close()
    return rows


def get_cars_for_filters(min_price, max_price, car_type, brand, limit=5, offset=0):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    query = """
        SELECT serial_number, make, model, variant, manufacturing_year,
               fuel_type, mileage_km, transmission_type,
               estimated_selling_price, image_url
        FROM carstockdata
        WHERE LOWER(ready_for_sales) = 'available'
          AND estimated_selling_price BETWEEN %s AND %s
    """
    params = [min_price, max_price]

    if car_type != "all":
        query += " AND LOWER(`type`) = LOWER(%s)"
        params.append(car_type)

    if brand != "all":
        query += " AND LOWER(make) = LOWER(%s)"
        params.append(brand)

    query += " ORDER BY estimated_selling_price ASC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# =========================
# CONTACT US FUNCTIONS
# =========================
def handle_contact_us(phone):
    """Handle the Contact Our Team menu"""
    from webhook import send_button_message
    
    USER_STATE[phone]["state"] = "USED_CONTACT_MENU"

    buttons = [
        {
            "type": "reply",
            "reply": {"id": "CONTACT_CALL_NOW", "title": "📞 Call us now"}
        },
        {
            "type": "reply",
            "reply": {"id": "CONTACT_VISIT", "title": "📍 Visit showroom"}
        }
    ]

    send_button_message(
        phone,
        "I'd be happy to connect you with our team! How would you like to get in touch?",
        buttons
    )

def handle_call_now(phone):
    """Handle Call Now option"""
    from webhook import send_whatsapp_message, send_list_message

    message = (
        "Perfect! Here are our direct contact numbers for immediate assistance:\n\n"

        "📞 *CALL US DIRECTLY:*\n\n"

        "🏢 *Main Showroom - Bangalore:*\n"
        "📞 Sales: +91-9876543210\n"
        "📞 Service: +91-9876543211\n"
        "🕒 Available: Mon-Sat: 9 AM - 8 PM, Sun: 10 AM - 6 PM\n\n"

        "🏢 *Branch - Electronic City:*\n"
        "📞 Sales: +91-9876543212\n"
        "🕒 Available: Mon-Sat: 9 AM - 8 PM\n\n"

        "🆘 *Emergency Support:*\n"
        "📞 24/7 Helpline: +91-9876543213\n\n"

        "💡 *Pro Tip:* Mention you contacted us via WhatsApp for priority assistance!"
    )

    send_whatsapp_message(phone, message)

    # 👇 Now ask properly before showing menu
    sections = [{
        "title": "Select",
        "rows": [
            {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
            {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
            {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
            {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
            {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
            {"id": "END_CONVERSATION", "title": "👋 End Conversation"}
        ]
    }]

    USER_STATE[phone]["state"] = "USED_CARS_MENU"

    send_list_message(
        phone,
        "Is there anything else I can help you with today?",
        "Select",
        sections
    )

def handle_callback_time_selection(phone, text):
    """Handle callback time selection"""
    from webhook import send_whatsapp_message
    
    time_map = {
        "CALLBACK_MORNING": "Morning (9-12)",
        "CALLBACK_AFTERNOON": "Afternoon (12-4)",
        "CALLBACK_EVENING": "Evening (4-8)"
    }
    
    if text in time_map:
        USER_STATE[phone]["callback_time"] = time_map[text]
        USER_STATE[phone]["state"] = "USED_CALLBACK_COLLECT_NAME"
        send_whatsapp_message(
            phone,
            "Great! Please provide your details:\n"
            "1. Your Name:"
        )
    else:
        send_whatsapp_message(phone, "Please select a valid time slot.")

def handle_callback_name(phone, text):
    """Handle callback name collection"""
    from webhook import send_whatsapp_message
    
    USER_STATE[phone]["callback_name"] = text.strip()
    USER_STATE[phone]["state"] = "USED_CALLBACK_COLLECT_PHONE"
    send_whatsapp_message(phone, "2. Phone Number:")

def handle_callback_phone(phone, text):
    """Handle callback phone collection"""
    from webhook import send_whatsapp_message
    
    entered_phone = text.strip()
    
    if not is_valid_mobile(entered_phone):
        send_whatsapp_message(
            phone,
            "❌ Invalid number.\nPlease enter a valid 10-digit mobile number:"
        )
        return
    
    USER_STATE[phone]["callback_phone"] = entered_phone
    USER_STATE[phone]["state"] = "USED_CALLBACK_COLLECT_NEED"
    send_whatsapp_message(
        phone,
        "3. What do you need help with?\n"
    )

def handle_callback_need(phone, text):
    """Handle callback need and send styled confirmation"""
    from webhook import send_whatsapp_message, send_list_message

    USER_STATE[phone]["callback_need"] = text.strip()

    # =========================
    # SAVE TO DATABASE
    # =========================
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO callbacks
            (name, phone, preferred_time, query, created_at)
            VALUES (%s, %s, %s, %s, NOW())
        """, (
            USER_STATE[phone]["callback_name"],
            USER_STATE[phone]["callback_phone"],
            USER_STATE[phone]["callback_time"],
            USER_STATE[phone]["callback_need"]
        ))

        conn.commit()
        cursor.close()
        conn.close()

    except Exception as e:
        print(f"❌ Callback insert error: {e}")

    # =========================
    # CONFIRMATION MESSAGE
    # =========================
    confirmation = (
        f"Perfect {USER_STATE[phone]['callback_name']}! Your callback is scheduled:\n\n"

        f"📋 *CALLBACK SCHEDULED:*\n"
        f"👤 Name: {USER_STATE[phone]['callback_name']}\n"
        f"📱 Phone: {USER_STATE[phone]['callback_phone']}\n"
        f"⏰ Preferred Time: {USER_STATE[phone]['callback_time']}\n"
        f"❓ Query: {USER_STATE[phone]['callback_need']}\n\n"

        f"📞 *What to Expect:*\n"
        f"✅ Call within 2 hours if during business hours\n"
        f"✅ Our expert will discuss on “{USER_STATE[phone]['callback_need']}”\n"
        f"😊 Thank you!\n"
        f"✅ Personalized assistance for your needs\n\n"

        f"🕒 Business Hours: Mon–Sun: 9 AM – 8 PM\n\n"

        f"Need immediate help?\n"
        f"📞 Call: +91-9876543210\n"
        f"📍 Visit: Main Showroom – Bangalore\n"
        f"Address: 123 MG Road, Bangalore - 560001\n\n"

        f"Thank you for choosing Sherpa Hyundai! 😊"
    )

    send_whatsapp_message(phone, confirmation)

    # =========================
    # RESET STATE
    # =========================
    USER_STATE[phone] = {"state": "USED_CARS_MENU"}

    sections = [{
        "title": "Select",
        "rows": [
            {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
            {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
            {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
            {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
            {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
            {"id": "END_CONVERSATION", "title": "👋 End Conversation"}
        ]
    }]

    send_list_message(
        phone,
        "Is there anything else I can help you with today?",
        "Select",
        sections
    )


def handle_visit_showroom(phone):
    """Handle Visit Showroom option"""
    from webhook import send_whatsapp_message, send_list_message

    # ===============================
    # 1️⃣ Show Showroom Details
    # ===============================
    showroom_message = (
        "We'd love to welcome you! Here are our locations:\n\n"

        "📍 *SHERPA HYUNDAI LOCATIONS:*\n\n"

        "🏢 Main Showroom - Bangalore\n"
        "📍 Address: 123 MG Road, Bangalore - 560001\n"
        "📞 Phone: +91-9876543210\n"
        "🕒 Timings: Mon-Sat: 9:00 AM - 8:00 PM, Sun: 10:00 AM - 6:00 PM\n"
        "🅿️ Facilities: Free parking, Test drive facility, Customer lounge\n\n"

        "🏢 Branch - Electronic City\n"
        "📍 Address: 456 Hosur Road, Electronic City - 560100\n"
        "📞 Phone: +91-9876543211\n"
        "🕒 Timings: Mon-Sat: 9:00 AM - 8:00 PM\n\n"

        "🗺️ *How to Reach:*\n"
        "🚇 Metro: MG Road Metro Station (2 min walk)\n"
        "🚌 Bus: Multiple bus routes available\n"
        "🚗 Car: Easy access from Ring Road"
    )

    send_whatsapp_message(phone, showroom_message)

    # ===============================
    # 2️⃣ Ask if Anything Else
    # ===============================
    USER_STATE[phone] = {"state": "USED_CARS_MENU"}

    sections = [{
        "title": "Select",
        "rows": [
            {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
            {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
            {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
            {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
            {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
            {"id": "END_CONVERSATION", "title": "👋 End Conversation"}
        ]
    }]

    send_list_message(
        phone,
        "Is there anything else I can help you with today?",
        "Select",
        sections
    )


def get_budget_label(budget_id):
    label_map = {
        "under1": "Under ₹1 Lakhs",
        "1-3": "₹1–3 Lakhs",
        "3-5": "₹3–5 Lakhs",
        "5-10": "₹5–10 Lakhs",
        "above10": "Above ₹10 Lakhs"
    }
    return label_map.get(budget_id, "this range")


# =========================
# FLOW HANDLER
# =========================
def used_cars_flow_handler(phone, text):
    from webhook import send_whatsapp_message, send_list_message, send_button_message

    USER_STATE.setdefault(phone, {})
    state = USER_STATE.get(phone, {}).get("state")
    
    # ── HUMAN AGENT CONFIRMATION ────────────────────────────────────────
    if state == "USED_AGENT_CONFIRMATION":
        from flows.service import bot
        if text == "AGENT_YES":
            vehicle_reg = USER_STATE.get(phone, {}).get("vehicle_reg") or USER_STATE.get(phone, {}).get("reg_number")
            bot.initiate_agent_handoff(phone, vehicle_reg=vehicle_reg,
                                       handoff_reason="Customer requested live agent from Used Cars flow")
            return
        elif text == "AGENT_NO":
            # Restore the saved state and re-prompt the customer
            prev_state = USER_STATE[phone].pop("_saved_state", None)
            if prev_state and prev_state != "USED_AGENT_CONFIRMATION":
                USER_STATE[phone]["state"] = prev_state
                send_whatsapp_message(phone, "✅ No problem! Let's continue where we left off.")
                from webhook import _reprompt_flow_state
                _reprompt_flow_state(phone, prev_state)
            else:
                USER_STATE[phone]["state"] = "USED_CARS_MENU"
                handle_used_cars(phone)
            return
        else:
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
            else:
                # Unknown input — re-show the confirmation prompt
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            return

    # ========================================
    # GLOBAL BUTTON HANDLER: CONTACT_LIVE_AGENT
    # ========================================
    if text == "CONTACT_LIVE_AGENT":
        # Save current state before switching to confirmation
        USER_STATE[phone]["_saved_state"] = state
        from flows.service import bot
        bot.trigger_human_agent_confirmation(phone)
        USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
        return
    
    # ========================================
    # GLOBAL END CONVERSATION (Used Cars)
    # ========================================
    if text == "END_CONVERSATION":

        # 🔥 Close MongoDB Session — sets status=closed & end_time
        session_id = USER_STATE.get(phone, {}).get("session_id")
        if session_id:
            from chat_history import close_chat_session
            close_chat_session(session_id)
            print(f"✅ MongoDB session closed (used_cars): {session_id}")

        # Clear user state completely
        USER_STATE.pop(phone, None)

        send_whatsapp_message(
            phone,
            "Thank you for chatting! 👋 Say Hi or Start to begin a new conversation anytime."
        )

        return

    # ========================================
    # MAIN MENU HANDLING
    # ========================================
    if state == "USED_CARS_MENU":
        if text == "USED_BROWSE":
            USER_STATE[phone]["state"] = "USED_SELECT_BUDGET"
            log_user_activity(phone, "used_car")   # ✅ track browse selection

            sections = [{
                "title": "Select Budget",
                "rows": [
                    {"id": "under1", "title": "Under ₹1 Lakhs"},
                    {"id": "1-3", "title": "₹1–3 Lakhs"},
                    {"id": "3-5", "title": "₹3–5 Lakhs"},
                    {"id": "5-10", "title": "₹5–10 Lakhs"},
                    {"id": "above10", "title": "Above ₹10 Lakhs"}
                ]
            }]


            send_list_message(phone, "Great choice! Let's find your perfect car. First, what's your budget range?", "Select", sections)
            return

        elif text == "USED_VALUATION":
            log_user_activity(phone, "valuation")  # ✅ track valuation selection
            USER_STATE[phone]["state"] = "USED_VALUATION_MODE"

            sections = [{
                "title": "Select",
                "rows": [
                    {"id": "VALUATION_EXCHANGE", "title": "🔁 Exchange"},
                    {"id": "VALUATION_ONEWAY", "title": "💰 One Way Selling"}
                ]
            }]

            send_list_message(
                phone,
                "Great! I'll help you get a valuation for your car.\n\n"
                "Would you like to exchange your car for another one, "
                "or go for One Way Selling?\n"
                "Choose how you want to proceed.",
                "Select",
                sections
            )
            return

        elif text == "USED_CONTACT":
            log_user_activity(phone, "contact_us")  # ✅ track contact selection
            handle_contact_us(phone)
            return

        elif text == "USED_MAIN_MENU":
            # ── Back to Main Menu ──
            from webhook import send_list_message as _slm
            USER_STATE[phone]["state"] = "MENU"
            _slm(phone,
                "👋 Welcome to *AutoSherpa* — your one-stop automotive assistant 🚗✨\n\nHow can I help you today?",
                "View Options",
                [{"title": "Main Menu", "rows": [
                    {"id": "menu_new_cars",     "title": "🔹 New Cars – Sales",   "description": "Browse new car models & prices"},
                    {"id": "menu_used_cars",    "title": "🚗 Used Cars",           "description": "Browse, value or test drive pre-owned cars"},
                    {"id": "menu_service",      "title": "🔧 Vehicle Service",     "description": "Book a service appointment"},
                    {"id": "menu_insurance",    "title": "🔰 Vehicle Insurance",   "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",         "description": "Reduce EMI or get a loan against your car"},
                    {"id": "menu_about",        "title": "ℹ️ About Us",            "description": "Learn more about AutoSherpa"},
                    {"id": "menu_contact",      "title": "📞 Contact & Support",   "description": "Get in touch with our team"},
                    {"id": "END_CHAT",          "title": "👋 End Conversation"}
                ]}]
            )
            return


        elif text == "USED_SALES_FLOW":
            # ── Redirect to Sales main menu ──
            print("🔹 New Cars Sales selected from Used Cars menu")
            from flows.sales import handle_sales
            session_id = USER_STATE.get(phone, {}).get("session_id")
            if session_id:
                try:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "sales"})
                except Exception:
                    pass
            handle_sales(phone)
            return

        # ── Free text → central AI router ──────────────────────────────────
        else:
            _used_route_via_ai(phone, text, state)
            return

        # ========================================
    # CONTACT US FLOW
    # ========================================
    if state == "USED_CONTACT_MENU":
        if text == "CONTACT_CALL_NOW":
            handle_call_now(phone)
            return
        elif text == "CONTACT_CALL_NOW" or (text not in ["CONTACT_CALL_NOW","CONTACT_CALLBACK","CONTACT_VISIT"] and
             any(k in text.lower() for k in ["call","phone","dial","number","direct"])):
            handle_call_now(phone)
            return
        elif text == "CONTACT_CALLBACK":
            USER_STATE[phone]["state"] = "USED_CALLBACK_SELECT_TIME"

            buttons = [
                {
                    "type": "reply",
                    "reply": {"id": "CALLBACK_MORNING", "title": "Morning (9-12 PM)"}
                },
                {
                    "type": "reply",
                    "reply": {"id": "CALLBACK_AFTERNOON", "title": "Afternoon (12-4 PM)"}
                },
                {
                    "type": "reply",
                    "reply": {"id": "CALLBACK_EVENING", "title": "Evening (4-8 PM)"}
                }
            ]

            send_button_message(
                phone,
                "Perfect! Our team will call you back. What's the best time to reach you?",
                buttons
            )
            return

        elif text == "CONTACT_VISIT":
            handle_visit_showroom(phone)
            return
        
        else:
            _used_route_via_ai(phone, text, state)
            return

    if state == "USED_CALLBACK_SELECT_TIME":
        if text in ["CALLBACK_MORNING", "CALLBACK_AFTERNOON", "CALLBACK_EVENING"]:
            handle_callback_time_selection(phone, text)
        else:
            _used_route_via_ai(phone, text, state)
        return

    if state == "USED_CALLBACK_COLLECT_NAME":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            handle_callback_name(phone, text)
        return

    if state == "USED_CALLBACK_COLLECT_PHONE":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            handle_callback_phone(phone, text)
        return

    if state == "USED_CALLBACK_COLLECT_NEED":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            handle_callback_need(phone, text)
        return

    # ========================================
    # BROWSE FLOW
    # ========================================
    if state == "USED_SELECT_BUDGET":
        # ── AI free-text budget resolver ─────────────────────────────────
        budget_id_map = {
            "under1": ["under 1","below 1","less than 1","upto 1","up to 1","within 1 lakh"],
            "1-3":    ["1 to 3","1-3","between 1","1 lakh to 3","1 to 3 lakh"],
            "3-5":    ["3 to 5","3-5","between 3","3 lakh to 5","3 to 5 lakh"],
            "5-10":   ["5 to 10","5-10","between 5","5 lakh to 10","5 to 10 lakh","five to ten"],
            "above10":["above 10","more than 10","over 10","10 lakh above","above ten lakh","10+"]
        }
        if text not in ["under1","1-3","3-5","5-10","above10"]:
            t = text.lower().strip()
            resolved_budget = None
            for bid, keywords in budget_id_map.items():
                if any(k in t for k in keywords):
                    resolved_budget = bid
                    break
            if not resolved_budget:
                # Try Groq AI for budget
                try:
                    prompt = f"""Map this message to a car budget range ID.
IDs: "under1" (under 1 lakh), "1-3" (1-3 lakhs), "3-5" (3-5 lakhs), "5-10" (5-10 lakhs), "above10" (above 10 lakhs)
User message: "{text}"
Return ONLY JSON: {{"id": "<ID>", "confidence": 0.0-1.0}}
If not a budget: {{"id": "NONE", "confidence": 0.0}}"""
                    response = client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1, max_tokens=60,
                        response_format={"type": "json_object"}
                    )
                    result = json.loads(response.choices[0].message.content.strip())
                    if float(result.get("confidence", 0)) >= 0.70 and result.get("id") != "NONE":
                        resolved_budget = result["id"]
                except Exception as _e:
                    print(f"❌ Budget AI error: {_e}")
            if resolved_budget:
                text = resolved_budget
                send_whatsapp_message(phone, f"✅ Got it — budget range noted!")
            else:
                _used_route_via_ai(phone, text, state)
                return
        USER_STATE[phone]["selected_budget"] = text
        USER_STATE[phone]["state"] = "USED_SELECT_TYPE"

        min_p, max_p = map_budget_to_range(text)
        types = get_types_for_budget(min_p, max_p)

        rows = [{"id": "TYPE_ALL", "title": "📋 All types"}]
        # WhatsApp allows max 10 rows total, we already have 1 for "All types", so max 9 more
        max_types = min(len(types), 9)
        for t in types[:max_types]:
            # Truncate if too long
            title = f"🚘 {t}"
            if len(title) > 24:
                title = title[:21] + "..."
            rows.append({"id": f"TYPE_{t.lower().replace(' ', '_')}", "title": title})

        budget_label = get_budget_label(text)

        message = (
            f"Perfect! {budget_label} gives you excellent options.\n"
            "What type of car do you prefer?"
        )

        send_list_message(
            phone,
            message,
            "Select",
            [{"title": "Select Car Type", "rows": rows}]
        )

        return

    if state == "USED_SELECT_TYPE":
        # ── AI free-text car type resolver ───────────────────────────────
        valid_type_prefixes = ["TYPE_ALL", "TYPE_"]
        is_valid_type = text == "TYPE_ALL" or text.startswith("TYPE_")
        if not is_valid_type:
            t = text.lower().strip()
            resolved_type = None
            if any(k in t for k in ["all","any","no preference","doesn't matter","all type"]):
                resolved_type = "TYPE_ALL"
            elif any(k in t for k in ["suv","sport utility","crossover"]):
                resolved_type = "TYPE_suv"
            elif any(k in t for k in ["sedan","saloon"]):
                resolved_type = "TYPE_sedan"
            elif any(k in t for k in ["hatch","hatchback","compact"]):
                resolved_type = "TYPE_hatchback"
            elif any(k in t for k in ["mpv","minivan","van","people carrier"]):
                resolved_type = "TYPE_mpv"
            elif any(k in t for k in ["coupe","sports","convertible"]):
                resolved_type = "TYPE_coupe"
            if resolved_type:
                text = resolved_type
                send_whatsapp_message(phone, f"✅ Got it — car type noted!")
            else:
                _used_route_via_ai(phone, text, state)
                return
        USER_STATE[phone]["selected_type"] = text
        USER_STATE[phone]["state"] = "USED_SELECT_BRAND"

        budget_id = USER_STATE[phone]["selected_budget"]
        min_p, max_p = map_budget_to_range(budget_id)

        car_type = "all" if text == "TYPE_ALL" else text.replace("TYPE_", "").replace("_", " ").title()
        brands = get_brands_for_budget_and_type(min_p, max_p, car_type)

        rows = [{"id": "BRAND_ALL", "title": "All Brands"}]
        # WhatsApp allows max 10 rows total, we already have 1 for "All brands", so max 9 more
        max_brands = min(len(brands), 9)
        for b in brands[:max_brands]:
            # Truncate if too long
            if len(b) > 24:
                b = b[:21] + "..."
            rows.append({"id": f"BRAND_{b.lower().replace(' ', '_')}", "title": b})

        send_list_message(phone, "Excellent choice! Which brand do you prefer?", "Select",
                          [{"title": "Select Brand", "rows": rows}])
        return

    if state == "USED_SELECT_BRAND":
        # ── AI free-text brand resolver ───────────────────────────────────
        is_valid_brand = text in ("BRAND_ALL",) or text.startswith("BRAND_")
        if not is_valid_brand:
            t = text.lower().strip()
            resolved_brand = None
            if any(k in t for k in ["all","any","no preference","all brand","any brand"]):
                resolved_brand = "BRAND_ALL"
            else:
                # Try to match common brands
                common_brands = ["hyundai","maruti","suzuki","honda","toyota","ford","volkswagen","tata",
                                 "mahindra","kia","renault","nissan","skoda","jeep","mg","bmw","audi",
                                 "mercedes","volvo","fiat","chevrolet"]
                for brand_kw in common_brands:
                    if brand_kw in t:
                        resolved_brand = f"BRAND_{brand_kw}"
                        break
            if resolved_brand:
                text = resolved_brand
                send_whatsapp_message(phone, f"✅ Got it — brand noted!")
            else:
                _used_route_via_ai(phone, text, state)
                return
        USER_STATE[phone]["selected_brand"] = text
        USER_STATE[phone]["state"] = "USED_SHOW_CARS"
        USER_STATE[phone]["offset"] = 0

        _show_used_cars(phone)
        return

    # ========================================
    # SHOW CARS STATE
    # ========================================
    if state == "USED_SHOW_CARS":

        if text.startswith("SELECT_"):
            car_id = text.replace("SELECT_", "")

            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT make, model, variant,
                    manufacturing_year,
                    estimated_selling_price
                FROM carstockdata
                WHERE serial_number = %s
            """, (car_id,))
            car = cur.fetchone()
            cur.close()
            conn.close()

            if not car:
                send_whatsapp_message(phone, "Car details not found.")
                return

            car_name = f"{car['make']} {car['model']} {car.get('variant','')}".strip()

            USER_STATE[phone]["selected_used_car_id"] = car_id
            USER_STATE[phone]["selected_used_car_name"] = car_name
            USER_STATE[phone]["state"] = "USED_BOOK_SELECT_WHEN"

            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            from datetime import datetime as _dt_sel
            _now_sel = _dt_sel.now()
            _sel_rows = []
            if _now_sel.hour < 18:
                _sel_rows.append({"id": "USED_WHEN_TODAY", "title": "Today (if available)"})
            _sel_rows += [
                {"id": "USED_WHEN_TOMORROW",   "title": "Tomorrow"},
                {"id": "USED_WHEN_LATER_WEEK", "title": "Later This Week"},
                {"id": "USED_WHEN_NEXT_WEEK",  "title": "Next Week"}
            ]
            sections = [{"title": "Select Schedule", "rows": _sel_rows}]

            send_list_message(
                phone,
                f"Excellent! Let's schedule your {car_name} test drive.\nWhen would you prefer?",
                "Select",
                sections
            )
            return


        if text.startswith("VIEWMORE_"):
            car_id = text.replace("VIEWMORE_", "")
            _show_more_images(phone, car_id)
            return


        if text == "BACK_TO_CARS":
            USER_STATE[phone]["offset"] = 0
            _show_used_cars(phone)
            return


        if text == "BROWSE_MORE":
            # offset is already updated at the end of previous _show_used_cars call
            # just call the function again — it will read the updated offset
            _show_used_cars(phone)
            return

        if text == "CHANGE_CRITERIA":

            USER_STATE[phone]["state"] = "USED_CONFIRM_CHANGE"

            buttons = [
                {"type": "reply", "reply": {"id": "CONFIRM_CHANGE_YES", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "CONFIRM_CHANGE_NO", "title": "No"}}
            ]

            send_button_message(
                phone,
                "I'll help you update your search criteria.\nDo you want to change your search now?",
                buttons
            )
            return


        if text == "END_CONVERSATION":

            USER_STATE.pop(phone, None)

            send_whatsapp_message(
                phone,
                "👋 Thank you for using Sherpa Hyundai Used Cars!\n\n"
                "Conversation ended.\n\n"
                "Type *Hi* or *Start* to begin again anytime."
            )
            return
            
        # ── Free text → central AI router ──────────────────────────────────
        else:
            _used_route_via_ai(phone, text, state)
            return

        # ========================================
    # CONFIRM CHANGE CRITERIA
    # ========================================
    if state == "USED_CONFIRM_CHANGE":

        if text == "CONFIRM_CHANGE_YES":

            USER_STATE[phone]["state"] = "USED_SELECT_BUDGET"
            USER_STATE[phone]["offset"] = 0

            sections = [{
                "title": "Select Budget",
                "rows": [
                    {"id": "under1", "title": "Under ₹1 Lakhs"},
                    {"id": "1-3", "title": "₹1–3 Lakhs"},
                    {"id": "3-5", "title": "₹3–5 Lakhs"},
                    {"id": "5-10", "title": "₹5–10 Lakhs"},
                    {"id": "above10", "title": "Above ₹10 Lakhs"}
                ]
            }]

            send_list_message(
                phone,
                "Great 👍 Let's update your search.\n\nFirst, what's your budget range?",
                "Select",
                sections
            )
            return


        elif text == "CONFIRM_CHANGE_NO":

            # 🔥 IMPORTANT: Go back to main menu
            USER_STATE[phone]["state"] = "USED_CARS_MENU"

            sections = [{
                "title": "Select",
                "rows": [
                    {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
                    {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
                    {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
                    {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
                    {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
                    {"id": "END_CONVERSATION",       "title": "👋 End Conversation"}
                ]
            }]

            send_list_message(
                phone,
                "Is there anything else I can help you with today?",
                "Select",
                sections
            )
            return


        else:
            _used_route_via_ai(phone, text, state)
        return


    # ========================================
    # VALUATION MODE SELECTION
    # ========================================
    if state == "USED_VALUATION_MODE":

        if text == "VALUATION_EXCHANGE":

            USER_STATE[phone]["valuation_type"] = "Exchange"
            log_valuation_selection(phone, "valuation_type", "Exchange")
            USER_STATE[phone]["state"] = "EXCH_BRAND"

            # ========================================
            # HARDCODED BRAND OPTIONS FOR EXCHANGE FLOW
            # ========================================
            brand_rows = [
                {"id": "EXCH_BRAND_MARUTI_SUZUKI", "title": "Maruti Suzuki"},
                {"id": "EXCH_BRAND_HYUNDAI", "title": "Hyundai"},
                {"id": "EXCH_BRAND_TATA", "title": "Tata"},
                {"id": "EXCH_BRAND_MAHINDRA", "title": "Mahindra"},
                {"id": "EXCH_BRAND_HONDA", "title": "Honda"},
                {"id": "EXCH_BRAND_TOYOTA", "title": "Toyota"},
                {"id": "EXCH_BRAND_KIA", "title": "Kia"},
                {"id": "EXCH_BRAND_VOLKSWAGEN", "title": "Volkswagen"},
                {"id": "EXCH_BRAND_SKODA", "title": "Skoda"},
                {"id": "EXCH_BRAND_OTHER", "title": "Other"}
            ]

            send_list_message(
                phone,
                "Great! Let's start with your current car details.\n\nWhat is the brand of your current car?",
                "Select Brand",
                [{"title": "Select Brand", "rows": brand_rows}]
            )
            return

        elif text == "VALUATION_ONEWAY":

            USER_STATE[phone]["valuation_type"] = "One Way Selling"

            log_valuation_selection(phone, "valuation_type", "One Way Selling")

            # Directly go to brand selection
            USER_STATE[phone]["state"] = "USED_VALUATION_BRAND"

            makes = get_distinct_makes()

            rows = []
            max_brands = min(len(makes), 9)

            for make in makes[:max_brands]:
                make_id = f"BRAND_{make.lower().replace(' ', '_')}"
                rows.append({"id": make_id, "title": make})

            rows.append({"id": "BRAND_OTHER", "title": "Other brands"})

            send_list_message(
                phone,
                "Perfect! Let's get started with your car details.\nWhich brand is your car?",
                "Select Brand",
                [{"title": "Select Brand", "rows": rows}]
            )
            return

        else:
            _used_route_via_ai(phone, text, state)
            return

    # ========================================
    # EXCHANGE FLOW — COMPLETE HARDCODED VERSION
    # ========================================

    # Q1 — Current car brand
    if state == "EXCH_BRAND":
        if text == "EXCH_BRAND_OTHER":
            USER_STATE[phone]["state"] = "EXCH_BRAND_ENTER"
            send_whatsapp_message(phone, "Please type your car brand:")
            return

        # ── Free-text brand resolver ─────────────────────────────────────
        if not text.startswith("EXCH_BRAND_"):
            t = text.strip().lower()
            brand_id_map = {
                "EXCH_BRAND_MARUTI_SUZUKI": ["maruti", "suzuki", "maruti suzuki"],
                "EXCH_BRAND_HYUNDAI": ["hyundai"],
                "EXCH_BRAND_TATA": ["tata"],
                "EXCH_BRAND_MAHINDRA": ["mahindra"],
                "EXCH_BRAND_HONDA": ["honda"],
                "EXCH_BRAND_TOYOTA": ["toyota"],
                "EXCH_BRAND_KIA": ["kia"],
                "EXCH_BRAND_VOLKSWAGEN": ["volkswagen", "vw"],
                "EXCH_BRAND_SKODA": ["skoda"],
            }
            resolved = None
            for bid, keywords in brand_id_map.items():
                if any(k in t for k in keywords):
                    resolved = bid
                    break
            if resolved:
                text = resolved
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                # Treat as free-text brand entry
                USER_STATE[phone]["exch_cur_brand"] = text.strip().title()
                log_valuation_selection(phone, "exch_cur_brand", text.strip())
                USER_STATE[phone]["state"] = "EXCH_MODEL_ENTER"
                send_whatsapp_message(phone, "Please type your car model:")
                return

        # Extract brand name from ID
        brand = text.replace("EXCH_BRAND_", "").replace("_", " ").title()
        USER_STATE[phone]["exch_cur_brand"] = brand
        log_valuation_selection(phone, "exch_cur_brand", brand)
        USER_STATE[phone]["state"] = "EXCH_MODEL"
        
        # ========================================
        # HARDCODED MODEL OPTIONS BASED ON BRAND
        # ========================================
        brand_lower = brand.lower()
        model_rows = []
        
        if "maruti" in brand_lower or "suzuki" in brand_lower:
            models = ["Swift", "Baleno", "Brezza", "Dzire"]
        elif "hyundai" in brand_lower:
            models = ["i20", "i10", "Creta", "Verna", "Venue"]
        elif "tata" in brand_lower:
            models = ["Nexon", "Tiago", "Tigor", "Harrier"]
        elif "mahindra" in brand_lower:
            models = ["XUV700", "Scorpio", "Thar", "Bolero"]
        elif "honda" in brand_lower:
            models = ["City", "Amaze", "WR-V", "Jazz"]
        elif "toyota" in brand_lower:
            models = ["Innova", "Fortuner", "Glanza", "Camry"]
        elif "kia" in brand_lower:
            models = ["Seltos", "Sonet", "Carens"]
        elif "volkswagen" in brand_lower:
            models = ["Virtus", "Taigun", "Polo"]
        elif "skoda" in brand_lower:
            models = ["Slavia", "Kushaq", "Rapid"]
        else:
            models = []
        
        for m in models[:8]:
            model_rows.append({"id": f"EXCH_MODEL_{m.lower().replace(' ', '_')}", "title": m})
        model_rows.append({"id": "EXCH_MODEL_OTHER", "title": "Other (Type Model Name)"})
        
        send_list_message(phone, f"Which model of {brand} do you have?", "Select Model",
                          [{"title": "Select Model", "rows": model_rows}])
        return

    if state == "EXCH_BRAND_ENTER":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_cur_brand"] = text.strip().title()
            log_valuation_selection(phone, "exch_cur_brand", text.strip())
            USER_STATE[phone]["state"] = "EXCH_MODEL_ENTER"
            send_whatsapp_message(phone, "Please type your car model:")
        return

    # Q2 — Current car model
    if state == "EXCH_MODEL":
        if text == "EXCH_MODEL_OTHER":
            USER_STATE[phone]["state"] = "EXCH_MODEL_ENTER"
            send_whatsapp_message(phone, "Please type your car model:")
            return

        # ── Free-text model resolver ─────────────────────────────────────
        if not text.startswith("EXCH_MODEL_"):
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
            else:
                USER_STATE[phone]["exch_cur_model"] = text.strip().title()
                log_valuation_selection(phone, "exch_cur_model", text.strip())
                USER_STATE[phone]["state"] = "EXCH_YEAR"
                year_rows = [
                    {"id": "EXCH_YEAR_2024", "title": "2024"},
                    {"id": "EXCH_YEAR_2023", "title": "2023"},
                    {"id": "EXCH_YEAR_2022", "title": "2022"},
                    {"id": "EXCH_YEAR_2021", "title": "2021"},
                    {"id": "EXCH_YEAR_2020", "title": "2020"},
                    {"id": "EXCH_YEAR_OLDER", "title": "Older than 2020"}
                ]
                send_list_message(phone, "What year is your car?", "Select Year",
                                  [{"title": "Select Year", "rows": year_rows}])
            return

        if text.startswith("EXCH_MODEL_"):
            model = text.replace("EXCH_MODEL_", "").replace("_", " ").title()
        else:
            model = text.strip().title()
        
        USER_STATE[phone]["exch_cur_model"] = model
        log_valuation_selection(phone, "exch_cur_model", model)
        USER_STATE[phone]["state"] = "EXCH_YEAR"
        
        # ========================================
        # HARDCODED YEAR OPTIONS
        # ========================================
        year_rows = [
            {"id": "EXCH_YEAR_2024", "title": "2024"},
            {"id": "EXCH_YEAR_2023", "title": "2023"},
            {"id": "EXCH_YEAR_2022", "title": "2022"},
            {"id": "EXCH_YEAR_2021", "title": "2021"},
            {"id": "EXCH_YEAR_2020", "title": "2020"},
            {"id": "EXCH_YEAR_OLDER", "title": "Older than 2020"}
        ]
        
        send_list_message(phone, "What year is your car?", "Select Year",
                          [{"title": "Select Year", "rows": year_rows}])
        return

    if state == "EXCH_MODEL_ENTER":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_cur_model"] = text.strip().title()
            log_valuation_selection(phone, "exch_cur_model", text.strip())
            USER_STATE[phone]["state"] = "EXCH_YEAR"
            
            year_rows = [
                {"id": "EXCH_YEAR_2024", "title": "2024"},
                {"id": "EXCH_YEAR_2023", "title": "2023"},
                {"id": "EXCH_YEAR_2022", "title": "2022"},
                {"id": "EXCH_YEAR_2021", "title": "2021"},
                {"id": "EXCH_YEAR_2020", "title": "2020"},
                {"id": "EXCH_YEAR_OLDER", "title": "Older than 2020"}
            ]
            
            send_list_message(phone, "What year is your car?", "Select Year",
                              [{"title": "Select Year", "rows": year_rows}])
        return

    # Q3 — Year
    if state == "EXCH_YEAR":
        year_map = {
            "EXCH_YEAR_2024": "2024",
            "EXCH_YEAR_2023": "2023",
            "EXCH_YEAR_2022": "2022",
            "EXCH_YEAR_2021": "2021",
            "EXCH_YEAR_2020": "2020",
            "EXCH_YEAR_OLDER": "Older than 2020"
        }

        if text in year_map:
            year = year_map[text]
        elif text.strip().isdigit() and len(text.strip()) == 4:
            year = text.strip()
        elif is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        else:
            year = text.strip()
        
        USER_STATE[phone]["exch_cur_year"] = year
        log_valuation_selection(phone, "exch_cur_year", year)
        USER_STATE[phone]["state"] = "EXCH_FUEL"
        
        # ========================================
        # HARDCODED FUEL OPTIONS
        # ========================================
        fuel_rows = [
            {"id": "EXCH_FUEL_PETROL", "title": "⛽ Petrol"},
            {"id": "EXCH_FUEL_DIESEL", "title": "🛢️ Diesel"},
            {"id": "EXCH_FUEL_CNG", "title": "🔋 CNG"},
            {"id": "EXCH_FUEL_ELECTRIC", "title": "⚡ Electric"}
        ]
        
        send_list_message(phone, "What is the fuel type of your car?", "Select Fuel",
                          [{"title": "Select Fuel", "rows": fuel_rows}])
        return

    # Q4 — Fuel
    if state == "EXCH_FUEL":
        fuel_map = {
            "EXCH_FUEL_PETROL": "Petrol",
            "EXCH_FUEL_DIESEL": "Diesel",
            "EXCH_FUEL_CNG": "CNG",
            "EXCH_FUEL_ELECTRIC": "Electric"
        }

        if text in fuel_map:
            fuel = fuel_map[text]
        else:
            # Free-text resolver
            t = text.strip().lower()
            if "petrol" in t or "gasoline" in t:
                fuel = "Petrol"
            elif "diesel" in t:
                fuel = "Diesel"
            elif "cng" in t or "gas" in t:
                fuel = "CNG"
            elif "electric" in t or "ev" in t or "battery" in t:
                fuel = "Electric"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                fuel = text.strip().title()
        
        USER_STATE[phone]["exch_cur_fuel"] = fuel
        log_valuation_selection(phone, "exch_cur_fuel", fuel)
        USER_STATE[phone]["state"] = "EXCH_TRANS"
        
        # ========================================
        # HARDCODED TRANSMISSION OPTIONS
        # ========================================
        trans_rows = [
            {"id": "EXCH_TRANS_MANUAL", "title": "🕹️ Manual"},
            {"id": "EXCH_TRANS_AUTOMATIC", "title": "⚙️ Automatic"}
        ]
        
        send_list_message(phone, "What is the transmission type?", "Select Transmission",
                          [{"title": "Select Transmission", "rows": trans_rows}])
        return

    # Q5 — Transmission
    if state == "EXCH_TRANS":
        trans_map = {
            "EXCH_TRANS_MANUAL": "Manual",
            "EXCH_TRANS_AUTOMATIC": "Automatic"
        }

        if text in trans_map:
            trans = trans_map[text]
        else:
            t = text.strip().lower()
            if "manual" in t or "mt" in t or "stick" in t:
                trans = "Manual"
            elif "auto" in t or "at" in t or "cvt" in t or "amt" in t:
                trans = "Automatic"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                trans = text.strip().title()
        
        USER_STATE[phone]["exch_cur_trans"] = trans
        log_valuation_selection(phone, "exch_cur_trans", trans)
        USER_STATE[phone]["state"] = "EXCH_KM"
        
        # ========================================
        # HARDCODED KM RANGE OPTIONS
        # ========================================
        km_rows = [
            {"id": "EXCH_KM_U10", "title": "Under 10,000 KM"},
            {"id": "EXCH_KM_10_25", "title": "10,000 - 25,000 KM"},
            {"id": "EXCH_KM_25_50", "title": "25,000 - 50,000 KM"},
            {"id": "EXCH_KM_50_75", "title": "50,000 - 75,000 KM"},
            {"id": "EXCH_KM_75_100", "title": "75,000 - 1,00,000 KM"},
            {"id": "EXCH_KM_O100", "title": "Over 1,00,000 KM"}
        ]
        
        send_list_message(phone, "How many kilometers has your car been driven?", "Select KM",
                          [{"title": "Select KM Range", "rows": km_rows}])
        return

    # Q6 — KM
    if state == "EXCH_KM":
        km_map = {
            "EXCH_KM_U10": "Under 10,000 KM",
            "EXCH_KM_10_25": "10,000 - 25,000 KM",
            "EXCH_KM_25_50": "25,000 - 50,000 KM",
            "EXCH_KM_50_75": "50,000 - 75,000 KM",
            "EXCH_KM_75_100": "75,000 - 1,00,000 KM",
            "EXCH_KM_O100": "Over 1,00,000 KM"
        }

        if text in km_map:
            km = km_map[text]
        else:
            t = text.strip().lower().replace(",", "").replace(" ", "")
            # Try to extract a number and map it
            import re as _re
            nums = _re.findall(r'\d+', t)
            if nums:
                val = int(nums[0])
                if val < 10000:
                    km = "Under 10,000 KM"
                elif val < 25000:
                    km = "10,000 - 25,000 KM"
                elif val < 50000:
                    km = "25,000 - 50,000 KM"
                elif val < 75000:
                    km = "50,000 - 75,000 KM"
                elif val < 100000:
                    km = "75,000 - 1,00,000 KM"
                else:
                    km = "Over 1,00,000 KM"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                km = text.strip()
        
        USER_STATE[phone]["exch_cur_km"] = km
        log_valuation_selection(phone, "exch_cur_km", km)
        USER_STATE[phone]["state"] = "EXCH_OWNERS"
        
        # ========================================
        # HARDCODED OWNER OPTIONS
        # ========================================
        owner_rows = [
            {"id": "EXCH_OWN_1", "title": "1st Owner (Me)"},
            {"id": "EXCH_OWN_2", "title": "2nd Owner"},
            {"id": "EXCH_OWN_3", "title": "3rd Owner"},
            {"id": "EXCH_OWN_MORE", "title": "More than 3 owners"}
        ]
        
        send_list_message(phone, "How many owners has this car had?", "Select Owners",
                          [{"title": "Select Ownership", "rows": owner_rows}])
        return

    # Q7 — Owners
    if state == "EXCH_OWNERS":
        own_map = {
            "EXCH_OWN_1": "1st Owner (Me)",
            "EXCH_OWN_2": "2nd Owner",
            "EXCH_OWN_3": "3rd Owner",
            "EXCH_OWN_MORE": "More than 3 owners"
        }

        if text in own_map:
            owners = own_map[text]
        else:
            t = text.strip().lower()
            if "1" in t or "first" in t or "one" in t or "me" in t:
                owners = "1st Owner (Me)"
            elif "2" in t or "second" in t or "two" in t:
                owners = "2nd Owner"
            elif "3" in t or "third" in t or "three" in t:
                owners = "3rd Owner"
            elif "more" in t or "4" in t or "four" in t or "multiple" in t:
                owners = "More than 3 owners"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                owners = text.strip()
        
        USER_STATE[phone]["exch_cur_owners"] = owners
        log_valuation_selection(phone, "exch_cur_owners", owners)
        USER_STATE[phone]["state"] = "EXCH_CONDITION"
        
        # ========================================
        # HARDCODED CONDITION OPTIONS
        # ========================================
        condition_rows = [
            {"id": "EXCH_COND_EX", "title": "⭐⭐⭐⭐⭐ Excellent"},
            {"id": "EXCH_COND_GD", "title": "⭐⭐⭐⭐ Good"},
            {"id": "EXCH_COND_AV", "title": "⭐⭐⭐ Average"},
            {"id": "EXCH_COND_FR", "title": "⭐⭐ Fair"}
        ]
        
        send_list_message(phone, "How would you rate your car's overall condition?",
                          "Select Condition", [{"title": "Select Condition", "rows": condition_rows}])
        return

    # Q8 — Condition
    if state == "EXCH_CONDITION":
        cond_map = {
            "EXCH_COND_EX": "Excellent",
            "EXCH_COND_GD": "Good",
            "EXCH_COND_AV": "Average",
            "EXCH_COND_FR": "Fair"
        }

        if text in cond_map:
            condition = cond_map[text]
        else:
            t = text.strip().lower()
            if "excell" in t or "like new" in t or "5" in t:
                condition = "Excellent"
            elif "good" in t or "4" in t or "minor" in t:
                condition = "Good"
            elif "average" in t or "normal" in t or "3" in t:
                condition = "Average"
            elif "fair" in t or "work" in t or "2" in t:
                condition = "Fair"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                condition = text.strip().title()
        
        USER_STATE[phone]["exch_cur_condition"] = condition
        log_valuation_selection(phone, "exch_cur_condition", condition)
        USER_STATE[phone]["state"] = "EXCH_INSURANCE"
        
        # ========================================
        # HARDCODED INSURANCE OPTIONS
        # ========================================
        insurance_rows = [
            {"id": "EXCH_INS_YES", "title": "Yes"},
            {"id": "EXCH_INS_NO", "title": "No"},
            {"id": "EXCH_INS_EXPIRED", "title": "Expired"}
        ]
        
        send_list_message(phone, "Does the car have valid insurance?", "Select",
                          [{"title": "Select Insurance Status", "rows": insurance_rows}])
        return

    # Q9 — Insurance
    if state == "EXCH_INSURANCE":
        ins_map = {
            "EXCH_INS_YES": "Yes",
            "EXCH_INS_NO": "No",
            "EXCH_INS_EXPIRED": "Expired"
        }

        if text not in ins_map:
            t = text.strip().lower()
            if "yes" in t or "have" in t or "valid" in t or "y" == t:
                text = "EXCH_INS_YES"
            elif "no" in t or "don't" in t or "dont" in t or "n" == t:
                text = "EXCH_INS_NO"
            elif "expir" in t or "lapse" in t or "old" in t:
                text = "EXCH_INS_EXPIRED"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                send_whatsapp_message(phone, "Please select a valid option.")
                return

        USER_STATE[phone]["exch_cur_insurance"] = ins_map[text]
        log_valuation_selection(phone, "exch_cur_insurance", ins_map[text])
        USER_STATE[phone]["state"] = "EXCH_DOCS"
        
        # ========================================
        # HARDCODED DOCUMENT OPTIONS
        # ========================================
        doc_rows = [
            {"id": "EXCH_DOC_RC", "title": "RC available"},
            {"id": "EXCH_DOC_INS", "title": "Insurance copy available"},
            {"id": "EXCH_DOC_SERVICE", "title": "Service history"},
            {"id": "EXCH_DOC_ALL", "title": "All documents available"}
        ]
        
        send_list_message(phone, "Do you have the following documents available?", "Select",
                          [{"title": "Select Documents", "rows": doc_rows}])
        return

    # Q10 — Documents
    if state == "EXCH_DOCS":
        doc_map = {
            "EXCH_DOC_RC": "RC available",
            "EXCH_DOC_INS": "Insurance copy available",
            "EXCH_DOC_SERVICE": "Service history",
            "EXCH_DOC_ALL": "All documents available"
        }

        if text not in doc_map:
            t = text.strip().lower()
            if "all" in t or "everything" in t or "complete" in t:
                text = "EXCH_DOC_ALL"
            elif "rc" in t or "registration" in t:
                text = "EXCH_DOC_RC"
            elif "insurance" in t or "ins" in t:
                text = "EXCH_DOC_INS"
            elif "service" in t or "history" in t:
                text = "EXCH_DOC_SERVICE"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                send_whatsapp_message(phone, "Please select a valid option.")
                return

        USER_STATE[phone]["exch_cur_docs"] = doc_map[text]
        log_valuation_selection(phone, "exch_cur_docs", doc_map[text])
        USER_STATE[phone]["state"] = "EXCH_TYPE"
        
        # ========================================
        # EXCHANGE TYPE OPTIONS
        # ========================================
        type_rows = [
            {"id": "EXCH_TYPE_NEW", "title": "🔹 Exchange for a New Car"},
            {"id": "EXCH_TYPE_USED", "title": "🚗 Exchange – Used Car"}
        ]
        
        send_list_message(phone,
            "How would you like to proceed with your exchange?",
            "Select", [{"title": "Select Exchange Type", "rows": type_rows}])
        return

    # Q11 — Exchange type → Q11A budget
    if state == "EXCH_TYPE":
        type_map = {
            "EXCH_TYPE_NEW": "New Car",
            "EXCH_TYPE_USED": "Used Car"
        }

        if text not in type_map:
            t = text.strip().lower()
            if "new" in t:
                text = "EXCH_TYPE_NEW"
            elif "used" in t or "second" in t or "pre" in t or "old" in t:
                text = "EXCH_TYPE_USED"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                send_whatsapp_message(phone, "Please select a valid option.")
                return

        USER_STATE[phone]["exch_new_type"] = type_map[text]
        log_valuation_selection(phone, "exch_new_type", type_map[text])
        USER_STATE[phone]["state"] = "EXCH_BUDGET"
        
        # ========================================
        # HARDCODED BUDGET RANGE OPTIONS
        # ========================================
        budget_rows = [
            {"id": "EXCH_BUD_5", "title": "Below ₹5 Lakhs"},
            {"id": "EXCH_BUD_5_10", "title": "₹5 – ₹10 Lakhs"},
            {"id": "EXCH_BUD_10", "title": "Above ₹10 Lakhs"}
        ]
        
        send_list_message(phone, "What is your budget range?", "Select Budget",
                          [{"title": "Select Budget", "rows": budget_rows}])
        return

    # Q11A — Budget → Q12 new car brand
    if state == "EXCH_BUDGET":
        bud_map = {
            "EXCH_BUD_5": "Below ₹5 Lakhs",
            "EXCH_BUD_5_10": "₹5 – ₹10 Lakhs",
            "EXCH_BUD_10": "Above ₹10 Lakhs"
        }

        if text not in bud_map:
            t = text.strip().lower().replace(",", "").replace("₹", "").replace("lakh", "").replace("lakhs", "").strip()
            import re as _re2
            nums = _re2.findall(r'\d+', t)
            if nums:
                val = int(nums[0])
                if val <= 5:
                    text = "EXCH_BUD_5"
                elif val <= 10:
                    text = "EXCH_BUD_5_10"
                else:
                    text = "EXCH_BUD_10"
            elif "below" in t or "under" in t or "less" in t:
                text = "EXCH_BUD_5"
            elif "above" in t or "more" in t or "over" in t:
                text = "EXCH_BUD_10"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                send_whatsapp_message(phone, "Please select a valid option.")
                return

        USER_STATE[phone]["exch_new_budget"] = bud_map[text]
        log_valuation_selection(phone, "exch_new_budget", bud_map[text])
        USER_STATE[phone]["state"] = "EXCH_NEW_BRAND"
        
        # ========================================
        # HARDCODED BRAND OPTIONS FOR NEW CAR
        # ========================================
        new_brand_rows = [
            {"id": "EXCH_NBRAND_HYUNDAI", "title": "Hyundai"},
            {"id": "EXCH_NBRAND_MARUTI_SUZUKI", "title": "Maruti Suzuki"},
            {"id": "EXCH_NBRAND_TATA", "title": "Tata"},
            {"id": "EXCH_NBRAND_MAHINDRA", "title": "Mahindra"},
            {"id": "EXCH_NBRAND_KIA", "title": "Kia"},
            {"id": "EXCH_NBRAND_OTHER", "title": "Other (Type)"}
        ]
        
        send_list_message(phone, "Which brand are you interested in?", "Select Brand",
                          [{"title": "Select Brand", "rows": new_brand_rows}])
        return

    # Q12 — New car brand
    if state == "EXCH_NEW_BRAND":
        if text == "EXCH_NBRAND_OTHER":
            USER_STATE[phone]["state"] = "EXCH_NEW_BRAND_ENTER"
            send_whatsapp_message(phone, "Please type the brand you are interested in:")
            return

        # ── Free-text resolver ────────────────────────────────────────────
        if not text.startswith("EXCH_NBRAND_"):
            t = text.strip().lower()
            nbrand_map = {
                "EXCH_NBRAND_HYUNDAI": ["hyundai"],
                "EXCH_NBRAND_MARUTI_SUZUKI": ["maruti", "suzuki", "maruti suzuki"],
                "EXCH_NBRAND_TATA": ["tata"],
                "EXCH_NBRAND_MAHINDRA": ["mahindra"],
                "EXCH_NBRAND_KIA": ["kia"],
            }
            resolved = None
            for bid, keywords in nbrand_map.items():
                if any(k in t for k in keywords):
                    resolved = bid
                    break
            if resolved:
                text = resolved
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                USER_STATE[phone]["exch_new_brand"] = text.strip().title()
                log_valuation_selection(phone, "exch_new_brand", text.strip())
                USER_STATE[phone]["state"] = "EXCH_NEW_MODEL_ENTER"
                send_whatsapp_message(phone, "Please type the model you are interested in:")
                return

        brand = text.replace("EXCH_NBRAND_", "").replace("_", " ").title()
        USER_STATE[phone]["exch_new_brand"] = brand
        log_valuation_selection(phone, "exch_new_brand", brand)
        USER_STATE[phone]["state"] = "EXCH_NEW_MODEL"
        
        # ========================================
        # HARDCODED MODEL OPTIONS BASED ON SELECTED BRAND
        # ========================================
        brand_lower = brand.lower()
        model_rows = []
        
        if "hyundai" in brand_lower:
            models = ["Creta", "Venue", "Verna", "i20", "Grand i10", "Tucson"]
        elif "maruti" in brand_lower or "suzuki" in brand_lower:
            models = ["Swift", "Baleno", "Brezza", "Dzire", "Ertiga", "Vitara"]
        elif "tata" in brand_lower:
            models = ["Nexon", "Tiago", "Tigor", "Harrier", "Safari", "Punch"]
        elif "mahindra" in brand_lower:
            models = ["XUV700", "Scorpio", "Thar", "Bolero", "XUV300"]
        elif "kia" in brand_lower:
            models = ["Seltos", "Sonet", "Carens", "Carnival"]
        else:
            models = []
        
        for m in models[:8]:
            model_rows.append({"id": f"EXCH_NMODEL_{m.lower().replace(' ', '_')}", "title": m})
        model_rows.append({"id": "EXCH_NMODEL_OTHER", "title": "Other (Type Model Name)"})
        
        send_list_message(phone, "Which model interests you?", "Select Model",
                          [{"title": "Select Model", "rows": model_rows}])
        return

    if state == "EXCH_NEW_BRAND_ENTER":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_new_brand"] = text.strip().title()
            log_valuation_selection(phone, "exch_new_brand", text.strip())
            USER_STATE[phone]["state"] = "EXCH_NEW_MODEL_ENTER"
            send_whatsapp_message(phone, "Please type the model you are interested in:")
        return

    # Q13 — New car model
    if state == "EXCH_NEW_MODEL":
        if text == "EXCH_NMODEL_OTHER":
            USER_STATE[phone]["state"] = "EXCH_NEW_MODEL_ENTER"
            send_whatsapp_message(phone, "Please type the model you are interested in:")
            return

        # ── Free-text resolver ────────────────────────────────────────────
        if not text.startswith("EXCH_NMODEL_"):
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
            else:
                USER_STATE[phone]["exch_new_model"] = text.strip().title()
                log_valuation_selection(phone, "exch_new_model", text.strip())
                USER_STATE[phone]["state"] = "EXCH_NEW_FUEL"
                fuel_rows = [
                    {"id": "EXCH_NFUEL_PETROL", "title": "⛽ Petrol"},
                    {"id": "EXCH_NFUEL_DIESEL", "title": "🛢️ Diesel"},
                    {"id": "EXCH_NFUEL_CNG", "title": "🔋 CNG"},
                    {"id": "EXCH_NFUEL_ELECTRIC", "title": "⚡ Electric"}
                ]
                send_list_message(phone, "What is your preferred fuel type?", "Select Fuel",
                                  [{"title": "Select Fuel", "rows": fuel_rows}])
            return

        if text.startswith("EXCH_NMODEL_"):
            model = text.replace("EXCH_NMODEL_", "").replace("_", " ").title()
        else:
            model = text.strip().title()
        
        USER_STATE[phone]["exch_new_model"] = model
        log_valuation_selection(phone, "exch_new_model", model)
        USER_STATE[phone]["state"] = "EXCH_NEW_FUEL"
        
        # ========================================
        # HARDCODED FUEL OPTIONS
        # ========================================
        fuel_rows = [
            {"id": "EXCH_NFUEL_PETROL", "title": "⛽ Petrol"},
            {"id": "EXCH_NFUEL_DIESEL", "title": "🛢️ Diesel"},
            {"id": "EXCH_NFUEL_CNG", "title": "🔋 CNG"},
            {"id": "EXCH_NFUEL_ELECTRIC", "title": "⚡ Electric"}
        ]
        
        send_list_message(phone, "What is your preferred fuel type?", "Select Fuel",
                          [{"title": "Select Fuel", "rows": fuel_rows}])
        return

    if state == "EXCH_NEW_MODEL_ENTER":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_new_model"] = text.strip().title()
            log_valuation_selection(phone, "exch_new_model", text.strip())
            USER_STATE[phone]["state"] = "EXCH_NEW_FUEL"
            
            fuel_rows = [
                {"id": "EXCH_NFUEL_PETROL", "title": "⛽ Petrol"},
                {"id": "EXCH_NFUEL_DIESEL", "title": "🛢️ Diesel"},
                {"id": "EXCH_NFUEL_CNG", "title": "🔋 CNG"},
                {"id": "EXCH_NFUEL_ELECTRIC", "title": "⚡ Electric"}
            ]
            
            send_list_message(phone, "What is your preferred fuel type?", "Select Fuel",
                              [{"title": "Select Fuel", "rows": fuel_rows}])
        return

    # Q14 — New car fuel
    if state == "EXCH_NEW_FUEL":
        fuel_map = {
            "EXCH_NFUEL_PETROL": "Petrol",
            "EXCH_NFUEL_DIESEL": "Diesel",
            "EXCH_NFUEL_CNG": "CNG",
            "EXCH_NFUEL_ELECTRIC": "Electric"
        }

        if text in fuel_map:
            fuel = fuel_map[text]
        else:
            t = text.strip().lower()
            if "petrol" in t or "gasoline" in t:
                fuel = "Petrol"
            elif "diesel" in t:
                fuel = "Diesel"
            elif "cng" in t or "gas" in t:
                fuel = "CNG"
            elif "electric" in t or "ev" in t or "battery" in t:
                fuel = "Electric"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                fuel = text.strip().title()
        
        USER_STATE[phone]["exch_new_fuel"] = fuel
        log_valuation_selection(phone, "exch_new_fuel", fuel)
        USER_STATE[phone]["state"] = "EXCH_NEW_TRANS"
        
        # ========================================
        # HARDCODED TRANSMISSION OPTIONS
        # ========================================
        trans_rows = [
            {"id": "EXCH_NTRANS_MANUAL", "title": "🕹️ Manual"},
            {"id": "EXCH_NTRANS_AUTOMATIC", "title": "⚙️ Automatic"}
        ]
        
        send_list_message(phone, "What is your preferred transmission type?", "Select Transmission",
                          [{"title": "Select Transmission", "rows": trans_rows}])
        return

    # Q15 — New car transmission → Q16 proceed options
    if state == "EXCH_NEW_TRANS":
        trans_map = {
            "EXCH_NTRANS_MANUAL": "Manual",
            "EXCH_NTRANS_AUTOMATIC": "Automatic"
        }

        if text in trans_map:
            trans = trans_map[text]
        else:
            t = text.strip().lower()
            if "manual" in t or "mt" in t or "stick" in t:
                trans = "Manual"
            elif "auto" in t or "at" in t or "cvt" in t or "amt" in t:
                trans = "Automatic"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                trans = text.strip().title()
        
        USER_STATE[phone]["exch_new_trans"] = trans
        log_valuation_selection(phone, "exch_new_trans", trans)
        USER_STATE[phone]["state"] = "EXCH_PROCEED"
        
        # ========================================
        # PROCEED OPTIONS
        # ========================================
        proceed_rows = [
            {"id": "EXCH_PROC_FINANCE", "title": "💰 Check Finance Options"},
            {"id": "EXCH_PROC_TEST_DRIVE", "title": "🚗 Book a Test Drive"}
        ]
        
        send_list_message(phone, "How would you like to proceed?", "Select",
                          [{"title": "Select Option", "rows": proceed_rows}])
        return

    # Q16 — Finance or Test Drive
    if state == "EXCH_PROCEED":
        if text == "EXCH_PROC_FINANCE":
            log_valuation_selection(phone, "exch_proceed", "Finance Options")
            USER_STATE[phone]["state"] = "EXCH_FINANCE_CALLBACK"
            
            # ========================================
            # FINANCE CALLBACK TIME OPTIONS
            # ========================================
            callback_rows = [
                {"id": "EXCH_FIN_30MIN", "title": "Within 30 minutes"},
                {"id": "EXCH_FIN_TODAY", "title": "Today"},
                {"id": "EXCH_FIN_TOMORROW", "title": "Tomorrow"}
            ]
            
            send_list_message(phone,
                "💰 *Check Finance Options*\n\nThank you for your interest! Our Finance Advisor will contact you shortly.\n"
                "🌐 Visit: www.Autosherpas.com\n📞 Call: 7757832783\n\n"
                "When would you like our finance advisor to contact you?",
                "Select Time", [{"title": "Select Time", "rows": callback_rows}])
            return

        elif text == "EXCH_PROC_TEST_DRIVE":
            log_valuation_selection(phone, "exch_proceed", "Book Test Drive")
            USER_STATE[phone]["state"] = "EXCH_TD_LOCATION"
            buttons = [
                {"type": "reply", "reply": {"id": "EXCH_LOC_HOME", "title": "🏠 Home Visit"}},
                {"type": "reply", "reply": {"id": "EXCH_LOC_SHOWROOM", "title": "🏢 Showroom Visit"}},
            ]
            send_button_message(phone, "Where would you like the test drive?\n\n🏠 Home Visit — we come to you\n🏢 Showroom Visit — visit us", buttons)
            return
        else:
            _used_route_via_ai(phone, text, state)
            return

    # Q17 — Finance callback time → collect name + city
    if state == "EXCH_FINANCE_CALLBACK":
        fin_map = {
            "EXCH_FIN_30MIN": "Within 30 minutes",
            "EXCH_FIN_TODAY": "Today",
            "EXCH_FIN_TOMORROW": "Tomorrow"
        }
        
        if text in fin_map:
            USER_STATE[phone]["exch_fin_callback"] = fin_map[text]
            log_valuation_selection(phone, "exch_fin_callback", fin_map[text])
            USER_STATE[phone]["state"] = "EXCH_COLLECT_NAME"
            send_whatsapp_message(phone, "Please confirm your contact details:\n\nFull Name:")
        else:
            _used_route_via_ai(phone, text, state)
        return

    # Q18 — Test drive location type
    if state == "EXCH_TD_LOCATION":
        if text == "EXCH_LOC_HOME":
            USER_STATE[phone]["exch_td_loc_type"] = "Home Visit"
            log_valuation_selection(phone, "exch_td_location_type", "Home Visit")
            USER_STATE[phone]["state"] = "EXCH_TD_ADDRESS"
            send_whatsapp_message(phone, "Please type your address for the home visit:")
            return
        elif text == "EXCH_LOC_SHOWROOM":
            USER_STATE[phone]["exch_td_loc_type"] = "Showroom Visit"
            USER_STATE[phone]["exch_td_address"] = "Sherpa Hyundai Showroom"
            log_valuation_selection(phone, "exch_td_location_type", "Showroom Visit")
            USER_STATE[phone]["state"] = "EXCH_TD_WHEN"
            from datetime import datetime as _dt_now
            _now = _dt_now.now()
            rows = []
            if _now.hour < 18:
                rows.append({"id": "EXCH_WHEN_TODAY", "title": "Today"})
            rows += [
                {"id": "EXCH_WHEN_TOMORROW", "title": "Tomorrow"},
                {"id": "EXCH_WHEN_LATER_WEEK", "title": "Later this week"},
                {"id": "EXCH_WHEN_NEXT_WEEK", "title": "Next week"},
            ]
            send_list_message(phone, "When would you prefer your appointment?", "Select Date",
                              [{"title": "Select Date", "rows": rows}])
            return
        else:
            _used_route_via_ai(phone, text, state)
            return

    if state == "EXCH_TD_ADDRESS":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_td_address"] = text.strip()
            log_valuation_selection(phone, "exch_td_address", text.strip())
            USER_STATE[phone]["state"] = "EXCH_TD_WHEN"
            from datetime import datetime as _dt_now2
            _now2 = _dt_now2.now()
            rows = []
            if _now2.hour < 18:
                rows.append({"id": "EXCH_WHEN_TODAY", "title": "Today"})
            rows += [
                {"id": "EXCH_WHEN_TOMORROW", "title": "Tomorrow"},
                {"id": "EXCH_WHEN_LATER_WEEK", "title": "Later this week"},
                {"id": "EXCH_WHEN_NEXT_WEEK", "title": "Next week"},
            ]
            send_list_message(phone, "When would you prefer your appointment?", "Select Date",
                              [{"title": "Select Date", "rows": rows}])
        return

    # Q19 — Appointment date
    if state == "EXCH_TD_WHEN":
        from datetime import date as _date, timedelta as _td
        when_map = {
            "EXCH_WHEN_TODAY": "Today",
            "EXCH_WHEN_TOMORROW": "Tomorrow",
            "EXCH_WHEN_LATER_WEEK": "Later this week",
            "EXCH_WHEN_NEXT_WEEK": "Next week",
        }
        
        if text == "EXCH_WHEN_TODAY":
            # ── After-6PM auto-advance: move today → tomorrow silently ──────
            from utils import get_smart_default_date
            smart_date, was_redirected = get_smart_default_date()
            if was_redirected:
                tomorrow_str = smart_date.strftime("%d-%m-%Y")
                USER_STATE[phone]["exch_td_date"] = tomorrow_str
                send_whatsapp_message(
                    phone,
                    f"⏰ Since it's after 6 PM, same-day appointments are unavailable.\n\n"
                    f"✅ I've scheduled your appointment for *tomorrow ({tomorrow_str})* instead."
                )
            else:
                valid, reason = is_valid_appointment_slot(_date.today())
                if not valid:
                    send_whatsapp_message(phone, reason)
                    rows = [
                        {"id": "EXCH_WHEN_TOMORROW",   "title": "Tomorrow"},
                        {"id": "EXCH_WHEN_LATER_WEEK", "title": "Later this week"},
                        {"id": "EXCH_WHEN_NEXT_WEEK",  "title": "Next week"},
                    ]
                    send_list_message(phone, "Please choose another date:", "Select Date",
                                      [{"title": "Select Date", "rows": rows}])
                    return
                USER_STATE[phone]["exch_td_date"] = _date.today().strftime("%d-%m-%Y")
        elif text == "EXCH_WHEN_TOMORROW":
            USER_STATE[phone]["exch_td_date"] = (_date.today() + _td(days=1)).strftime("%d-%m-%Y")
        elif text == "EXCH_WHEN_LATER_WEEK":
            week_dates = get_remaining_week_dates_used()
            if not week_dates:
                send_whatsapp_message(phone, "No dates remaining this week. Please select Next Week.")
                return
            rows = [{"id": f"EXCH_DATE_{d.strftime('%Y%m%d')}", "title": d.strftime("%A %d %b")}
                    for d in week_dates[:10]]
            USER_STATE[phone]["state"] = "EXCH_TD_EXACT_DATE"
            send_list_message(phone, "Please select a date:", "Select Date",
                              [{"title": "Available Dates", "rows": rows}])
            return
        elif text == "EXCH_WHEN_NEXT_WEEK":
            week_dates = get_next_week_dates_used()
            rows = [{"id": f"EXCH_DATE_{d.strftime('%Y%m%d')}", "title": d.strftime("%A %d %b")}
                    for d in week_dates[:10]]
            USER_STATE[phone]["state"] = "EXCH_TD_EXACT_DATE"
            send_list_message(phone, "Please select a date:", "Select Date",
                              [{"title": "Available Dates", "rows": rows}])
            return
        else:
            # ── Smart Date Resolver (Free-text) ──
            all_dates = sorted(list(set([_date.today(), _date.today() + _td(days=1)] + get_remaining_week_dates_used() + get_next_week_dates_used())))
            resolved_id = resolve_date_from_text(text, all_dates, prefix="EXCH_RESOLVED_")
            
            if resolved_id:
                date_val = resolved_id.replace("EXCH_RESOLVED_", "")
                target_date = datetime.strptime(date_val, "%Y%m%d").date()
                valid, reason = is_valid_appointment_slot(target_date)
                if not valid:
                    send_whatsapp_message(phone, reason)
                    return
                USER_STATE[phone]["exch_td_date"] = target_date.strftime("%d-%m-%Y")
            else:
                _used_route_via_ai(phone, text, state)
                return
        
        log_valuation_selection(phone, "exch_td_date", USER_STATE[phone]["exch_td_date"])
        USER_STATE[phone]["state"] = "EXCH_TD_TIME"
        
        # ========================================
        # HARDCODED TIME SLOT OPTIONS
        # ========================================
        buttons = [
            {"type": "reply", "reply": {"id": "EXCH_TIME_MORNING", "title": "Morning (10-12)"}},
            {"type": "reply", "reply": {"id": "EXCH_TIME_AFTERNOON", "title": "Afternoon (12-3)"}},
            {"type": "reply", "reply": {"id": "EXCH_TIME_EVENING", "title": "Evening (3-6)"}},
        ]
        send_button_message(phone, "Please choose a time slot:", buttons)
        return

    if state == "EXCH_TD_EXACT_DATE":
        if text.startswith("EXCH_DATE_"):
            from datetime import datetime as _dt_ed
            raw = text.replace("EXCH_DATE_", "")
            USER_STATE[phone]["exch_td_date"] = _dt_ed.strptime(raw, "%Y%m%d").strftime("%d-%m-%Y")
            log_valuation_selection(phone, "exch_td_date", USER_STATE[phone]["exch_td_date"])
            USER_STATE[phone]["state"] = "EXCH_TD_TIME"
            buttons = [
                {"type": "reply", "reply": {"id": "EXCH_TIME_MORNING", "title": "Morning (10-12)"}},
                {"type": "reply", "reply": {"id": "EXCH_TIME_AFTERNOON", "title": "Afternoon (12-3)"}},
                {"type": "reply", "reply": {"id": "EXCH_TIME_EVENING", "title": "Evening (3-6)"}},
            ]
            send_button_message(phone, "Please choose a time slot:", buttons)
        else:
            _used_route_via_ai(phone, text, state)
        return

    # Q20 — Time slot → Q21 name + city
    if state == "EXCH_TD_TIME":
        time_map = {
            "EXCH_TIME_MORNING": "Morning (10-12)",
            "EXCH_TIME_AFTERNOON": "Afternoon (12-3)",
            "EXCH_TIME_EVENING": "Evening (3-6)",
        }
        
        if text in time_map:
            USER_STATE[phone]["exch_td_time"] = time_map[text]
            log_valuation_selection(phone, "exch_td_time", time_map[text])
            USER_STATE[phone]["state"] = "EXCH_COLLECT_NAME"
            send_whatsapp_message(phone, "Please confirm your contact details:\n\nFull Name:")
        else:
            _used_route_via_ai(phone, text, state)
        return

    # Q21 — Name
    if state == "EXCH_COLLECT_NAME":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_name"] = text.strip()
            USER_STATE[phone]["exch_phone"] = str(phone)[-10:]
            log_valuation_selection(phone, "exch_name", text.strip())
            USER_STATE[phone]["state"] = "EXCH_COLLECT_CITY"
            send_whatsapp_message(phone, "City:")
        return

    # City → Save + Final Confirmation
    if state == "EXCH_COLLECT_CITY":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["exch_city"] = text.strip()
            log_valuation_selection(phone, "exch_city", text.strip())

        # ── Save to DB ──────────────────────────────────────────────────
        try:
            import mysql.connector
            from config import DB_CONFIG
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO valuation_selections
                (contact_number, cur_brand, cur_model, cur_year, cur_fuel, cur_transmission,
                 cur_km, cur_owners, cur_condition, cur_insurance, cur_documents,
                 new_type, new_budget, new_brand, new_model, new_fuel, new_transmission,
                 proceed_option, fin_callback_time, td_location_type, td_address,
                 td_date, td_time, customer_name, city, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (
                str(phone)[-10:],
                USER_STATE[phone].get("exch_cur_brand", ""),
                USER_STATE[phone].get("exch_cur_model", ""),
                USER_STATE[phone].get("exch_cur_year", ""),
                USER_STATE[phone].get("exch_cur_fuel", ""),
                USER_STATE[phone].get("exch_cur_trans", ""),
                USER_STATE[phone].get("exch_cur_km", ""),
                USER_STATE[phone].get("exch_cur_owners", ""),
                USER_STATE[phone].get("exch_cur_condition", ""),
                USER_STATE[phone].get("exch_cur_insurance", ""),
                USER_STATE[phone].get("exch_cur_docs", ""),
                USER_STATE[phone].get("exch_new_type", ""),
                USER_STATE[phone].get("exch_new_budget", ""),
                USER_STATE[phone].get("exch_new_brand", ""),
                USER_STATE[phone].get("exch_new_model", ""),
                USER_STATE[phone].get("exch_new_fuel", ""),
                USER_STATE[phone].get("exch_new_trans", ""),
                USER_STATE[phone].get("exch_proceed", ""),
                USER_STATE[phone].get("exch_fin_callback", ""),
                USER_STATE[phone].get("exch_td_loc_type", ""),
                USER_STATE[phone].get("exch_td_address", ""),
                USER_STATE[phone].get("exch_td_date", ""),
                USER_STATE[phone].get("exch_td_time", ""),
                USER_STATE[phone].get("exch_name", ""),
                USER_STATE[phone].get("exch_city", ""),
            ))
            conn.commit()
            cursor.close()
            conn.close()
            print(f"✅ Exchange valuation saved for {phone}")
        except Exception as e:
            print(f"❌ Exchange valuation insert error: {e}")

        # ── Build confirmation ──────────────────────────────────────────
        cur_car = (f"{USER_STATE[phone].get('exch_cur_year','')} "
                   f"{USER_STATE[phone].get('exch_cur_brand','')} "
                   f"{USER_STATE[phone].get('exch_cur_model','')} "
                   f"{USER_STATE[phone].get('exch_cur_fuel','')}").strip()

        new_car = (f"{USER_STATE[phone].get('exch_new_brand','')} "
                   f"{USER_STATE[phone].get('exch_new_model','')} "
                   f"{USER_STATE[phone].get('exch_new_fuel','')}").strip()

        td_date = USER_STATE[phone].get("exch_td_date", "")
        td_time = USER_STATE[phone].get("exch_td_time", "")
        fin_cb = USER_STATE[phone].get("exch_fin_callback", "")

        confirmation_message = (
            f"Perfect {USER_STATE[phone].get('exch_name', '')}! Here's what happens next:\n\n"
            f"📋 *SELLER CONFIRMATION:*\n"
            f"👤 Name: {USER_STATE[phone].get('exch_name', '')}\n"
            f"📱 Phone: {USER_STATE[phone].get('exch_phone', '')}\n"
            f"🚗 Car: {cur_car}\n"
            f"📍 Location: {USER_STATE[phone].get('exch_city', '')}\n\n"
            f"📞 Questions? Call: +91-9876543210\n\n"
            f"Thank you for choosing Sherpa Hyundai! 😊"
        )

        send_whatsapp_message(phone, confirmation_message)

        USER_STATE[phone] = {"state": "USED_CARS_MENU"}
        sections = [{
            "title": "Select",
            "rows": [
                {"id": "USED_BROWSE", "title": "🚗 Browse Used Cars"},
                {"id": "USED_VALUATION", "title": "💰 Get Car Valuation"},
                {"id": "USED_CONTACT", "title": "📞 Contact Our Team"},
                {"id": "USED_SALES_FLOW", "title": "🔹 New Cars – Sales"},
                {"id": "USED_MAIN_MENU", "title": "🏠 Back to Main Menu"},
                {"id": "END_CONVERSATION", "title": "👋 End Conversation"},
            ]
        }]
        send_list_message(phone, "Is there anything else I can help you with today?",
                          "Select", sections)
        return


    # ========================================
    # VALUATION BRAND SELECTION (FROM DB)
    # ========================================
    if state == "USED_VALUATION_BRAND":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_MODE"
                send_list_message(phone,
                    "Would you like to exchange or go for One Way Selling?",
                    "Select",
                    [{"title": "Select", "rows": [
                        {"id": "VALUATION_EXCHANGE", "title": "🔁 Exchange"},
                        {"id": "VALUATION_ONEWAY",   "title": "💰 One Way Selling"}
                    ]}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            elif flow_action == "END_CONVERSATION":
                USER_STATE.pop(phone, None)
                send_whatsapp_message(phone, "Thank you for chatting! 👋")
            else:
                # Re-prompt current state after clarification/query answer
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        if text == "BRAND_OTHER":
            USER_STATE[phone]["state"] = "USED_VALUATION_ENTER_BRAND"
            send_whatsapp_message(phone, "Please write down the brand of your car.")
            return
        
        if not text.startswith("BRAND_"):
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                brand_name = text.strip().title()
        else:
            # Extract brand name from ID
            brand_name = text.replace("BRAND_", "").replace("_", " ").title()

        USER_STATE[phone]["valuation_brand"] = brand_name
        USER_STATE[phone]["state"] = "USED_VALUATION_MODEL"

        log_valuation_selection(phone, "brand", brand_name)

        # Fetch models dynamically from DB
        models = get_models_for_make(brand_name)

        if not models:
            # If no models found, ask manually
            USER_STATE[phone]["state"] = "USED_VALUATION_ENTER_MODEL"
            send_whatsapp_message(phone, "Please write down which model car do you have?")
            return

        text_message = f"Perfect! Select your {brand_name} model."

        rows = []
        max_models = min(len(models), 9)

        for model in models[:max_models]:
            model_id = f"MODEL_{model.lower().replace(' ', '_')}"
            title = model
            if len(title) > 24:
                title = title[:21] + "..."
            rows.append({"id": model_id, "title": title})

        rows.append({"id": "MODEL_OTHER", "title": "Other"})


        send_list_message(
            phone,
            text_message,
            "Select Model",
            [{"title": "Select Model", "rows": rows}]
        )
        return

    if state == "USED_VALUATION_ENTER_BRAND":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["valuation_brand"] = text.strip()
            USER_STATE[phone]["state"] = "USED_VALUATION_ENTER_MODEL"
            send_whatsapp_message(phone, "Perfect! Write down which model car do you have?")
        return

    if state == "USED_VALUATION_MODEL":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_BRAND"
                makes = get_distinct_makes()
                rows = []
                max_brands = min(len(makes), 9)
                for make in makes[:max_brands]:
                    make_id = f"BRAND_{make.lower().replace(' ', '_')}"
                    rows.append({"id": make_id, "title": make})
                rows.append({"id": "BRAND_OTHER", "title": "Other brands"})
                send_list_message(phone,
                    "Perfect! Let's get started with your car details.\nWhich brand is your car?",
                    "Select Brand",
                    [{"title": "Select Brand", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        if text == "MODEL_OTHER":
            USER_STATE[phone]["state"] = "USED_VALUATION_ENTER_MODEL"
            send_whatsapp_message(phone, "Perfect! Write down which model car do you have?")
            return
        
        if not text.startswith("MODEL_"):
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                model_name = text.strip().title()
        else:
            # Extract model name from ID and remove numbering if present
            model_name = text.replace("MODEL_", "").replace("_", " ").title()
            if ". " in model_name:
                model_name = model_name.split(". ", 1)[1]
            # Remove ellipsis if present
            if model_name.endswith("..."):
                model_name = model_name[:-3]
        
        USER_STATE[phone]["valuation_model"] = model_name
        USER_STATE[phone]["state"] = "USED_VALUATION_YEAR"
        
        log_valuation_selection(phone, "model", model_name)
        
        # Fetch years from database
        years = get_distinct_years()
        
        text = f"Excellent! What year is your car?"
        
        rows = []
        # ✅ Filter only years >= 2020
        filtered_years = [
            y for y in years
            if y.isdigit() and int(y) >= 2020
        ]
        # WhatsApp allows max 10 rows total
        # Keep 1 slot for "Older than 2020"
        max_years = min(len(filtered_years), 9)

        for year in filtered_years[:max_years]:
            rows.append({
                "id": f"YEAR_{year}",
                "title": year
            })
        # ✅ Add clean label
        rows.append({
            "id": "YEAR_OLDER_2020",
            "title": "Older than 2020"
        })
        
        send_list_message(
            phone,
            text,
            "Select Year",
            [{"title": "Select Year", "rows": rows}]
        )
        return

    if state == "USED_VALUATION_ENTER_MODEL":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["valuation_model"] = text.strip()
            USER_STATE[phone]["state"] = "USED_VALUATION_YEAR"
            
            # Fetch years from database
            years = get_distinct_years()
            
            text_prompt = f"Excellent! What year is your car?"
            
            rows = []
            # ✅ Filter only years >= 2020
            filtered_years = [
                y for y in years
                if y.isdigit() and int(y) >= 2020
            ]
            # WhatsApp max 10 rows → keep 1 for Older than 2020
            max_years = min(len(filtered_years), 9)
            for year in filtered_years[:max_years]:
                rows.append({"id": f"YEAR_{year}", "title": year})
            # ✅ Add clean label
            rows.append({"id": "YEAR_OLDER_2020", "title": "Older than 2020"})
            send_list_message(phone, text_prompt, "Select Year", [{"title": "Select Year", "rows": rows}])
        return

    if state == "USED_VALUATION_YEAR":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_MODEL"
                brand_name = USER_STATE.get(phone, {}).get("valuation_brand", "your car")
                models = get_models_for_make(brand_name)
                rows = []
                max_models = min(len(models), 9)
                for model in models[:max_models]:
                    model_id = f"MODEL_{model.lower().replace(' ', '_')}"
                    title = model
                    if len(title) > 24:
                        title = title[:21] + "..."
                    rows.append({"id": model_id, "title": title})
                rows.append({"id": "MODEL_OTHER", "title": "Other"})
                send_list_message(phone, f"Perfect! Select your {brand_name} model.",
                    "Select Model", [{"title": "Select Model", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        if text == "YEAR_OLDER_2020":
            USER_STATE[phone]["valuation_year"] = "Older than 2020"
        elif text.startswith("YEAR_"):
            year = text.replace("YEAR_", "")
            if ". " in year:
                year = year.split(". ", 1)[1]
            USER_STATE[phone]["valuation_year"] = year
        else:
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                USER_STATE[phone]["valuation_year"] = text.strip()
        
        USER_STATE[phone]["state"] = "USED_VALUATION_FUEL"
        log_valuation_selection(phone, "year", USER_STATE[phone]["valuation_year"])
        
        # Fetch fuel types from database
        fuel_types = get_distinct_fuel_types()
        text_prompt = f"Great! What's the fuel type of your car?"
        fuel_icons = {'petrol': '⛽', 'diesel': '🛢️', 'cng': '🔋', 'electric': '⚡'}
        rows = []
        max_fuel = min(len(fuel_types), 10)
        for fuel in fuel_types[:max_fuel]:
            icon = fuel_icons.get(fuel.lower(), '🚗')
            fuel_id = f"FUEL_{fuel.lower().replace(' ', '_')}"
            title = f"{icon} {fuel}"
            if len(title) > 24: title = title[:21] + "..."
            rows.append({"id": fuel_id, "title": title})
        
        send_list_message(phone, text_prompt, "Select Fuel", [{"title": "Select Fuel Type", "rows": rows}])
        return

    if state == "USED_VALUATION_FUEL":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_YEAR"
                years = get_distinct_years()
                rows = []
                filtered_years = [y for y in years if y.isdigit() and int(y) >= 2020]
                max_years = min(len(filtered_years), 9)
                for year in filtered_years[:max_years]:
                    rows.append({"id": f"YEAR_{year}", "title": year})
                rows.append({"id": "YEAR_OLDER_2020", "title": "Older than 2020"})
                send_list_message(phone, "Excellent! What year is your car?",
                    "Select Year", [{"title": "Select Year", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        if text.startswith("FUEL_"):
            fuel_name = text.replace("FUEL_", "").replace("_", " ").title()
            if ". " in fuel_name:
                fuel_name = fuel_name.split(". ", 1)[1]
            # Remove icon if present
            if fuel_name and fuel_name[0] in ['⛽', '🛢️', '🔋', '⚡', '🚗']:
                fuel_name = fuel_name[2:].strip()
            # Remove ellipsis if present
            if fuel_name.endswith("..."):
                fuel_name = fuel_name[:-3]
        else:
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                fuel_name = text.strip().title()
        
        USER_STATE[phone]["valuation_fuel"] = fuel_name
        USER_STATE[phone]["state"] = "USED_VALUATION_TRANSMISSION"
        log_valuation_selection(phone, "fuel", fuel_name)
        
        # Fetch transmission types from database
        transmissions = get_distinct_transmission_types()
        text_prompt = f"Cool! What's the transmission type?"
        trans_icons = {'manual': '⚙️', 'automatic': '🚗', 'amt': '🔄', 'cvt': '⚡'}
        rows = []
        max_trans = min(len(transmissions), 10)
        for trans in transmissions[:max_trans]:
            icon = trans_icons.get(trans.lower(), '🔄')
            trans_id = f"TRANS_{trans.lower().replace(' ', '_')}"
            title = f"{icon} {trans}"
            if len(title) > 24: title = title[:21] + "..."
            rows.append({"id": trans_id, "title": title})

        send_list_message(phone, text_prompt, "Select Transmission", [{"title": "Select Transmission", "rows": rows}])
        return

    if state == "USED_VALUATION_TRANSMISSION":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_FUEL"
                fuel_types = get_distinct_fuel_types()
                rows = []
                max_fuel = min(len(fuel_types), 10)
                for fuel in fuel_types[:max_fuel]:
                    rows.append({"id": f"FUEL_{fuel.lower().replace(' ', '_')}", "title": fuel})
                send_list_message(phone, "Great! What's the fuel type of your car?",
                    "Select Fuel", [{"title": "Select Fuel Type", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        if text.startswith("TRANS_"):
            trans_name = text.replace("TRANS_", "").replace("_", " ").title()
            if ". " in trans_name:
                trans_name = trans_name.split(". ", 1)[1]
            # Remove icon if present
            if trans_name and trans_name[0] in ['⚙️', '🚗', '🔄', '⚡']:
                trans_name = trans_name[2:].strip()
            # Remove ellipsis if present
            if trans_name.endswith("..."):
                trans_name = trans_name[:-3]
        else:
            if is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                trans_name = text.strip().title()
        
        USER_STATE[phone]["valuation_transmission"] = trans_name
        USER_STATE[phone]["state"] = "USED_VALUATION_KM"
        
        log_valuation_selection(phone, "transmission", trans_name)
        
        text_prompt = "Perfect! How many kilometers has your car been driven?"
        
        rows = [
            {"id": "KM_UNDER_10K", "title": "Under 10,000 KM"},
            {"id": "KM_10_25K", "title": "10-25,000 KM"},
            {"id": "KM_25_50K", "title": "25-50,000 KM"},
            {"id": "KM_50_75K", "title": "50-75,000 KM"},
            {"id": "KM_75_100K", "title": "75,000 - 1,00,000 KM"},
            {"id": "KM_OVER_100K", "title": "Over 1,00,000 KM"}
        ]

        
        send_list_message(phone, text_prompt, "Select KM", [{"title": "Select KM Range", "rows": rows}])
        return

    if state == "USED_VALUATION_KM":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_TRANSMISSION"
                transmissions = get_distinct_transmission_types()
                rows = []
                max_trans = min(len(transmissions), 10)
                for trans in transmissions[:max_trans]:
                    rows.append({"id": f"TRANS_{trans.lower().replace(' ', '_')}", "title": trans})
                send_list_message(phone, "Cool! What's the transmission type?",
                    "Select Transmission", [{"title": "Select Transmission", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        km_map = {
            "KM_UNDER_10K": "Under 10,000 KM",
            "KM_10_25K": "10-25,000 KM",
            "KM_25_50K": "25-50,000 KM",
            "KM_50_75K": "50-75,000 KM",
            "KM_75_100K": "75,000 - 1,00,000 KM",
            "KM_OVER_100K": "Over 1,00,000 KM"
        }
        
        if text in km_map:
            USER_STATE[phone]["valuation_km"] = km_map[text]
        elif is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        else:
            # Handle if user typed the number
            km_options = list(km_map.values())
            try:
                option_num = int(text.split('.')[0]) - 1
                if 0 <= option_num < len(km_options):
                    USER_STATE[phone]["valuation_km"] = km_options[option_num]
                else:
                    USER_STATE[phone]["valuation_km"] = text
            except:
                USER_STATE[phone]["valuation_km"] = text
        
        USER_STATE[phone]["state"] = "USED_VALUATION_OWNERSHIP"
        
        log_valuation_selection(phone, "km", USER_STATE[phone]["valuation_km"])
        
        text_prompt = "How many owners has this car had?"
        
        rows = [
            {"id": "OWNER_1ST", "title": "1st Owner (Me)"},
            {"id": "OWNER_2ND", "title": "2nd Owner"},
            {"id": "OWNER_3RD", "title": "3rd Owner"},
            {"id": "OWNER_MORE", "title": "More than 3 owners"}
        ]

        
        send_list_message(phone, text_prompt, "Select Owners", [{"title": "Select Ownership", "rows": rows}])
        return

    if state == "USED_VALUATION_OWNERSHIP":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_KM"
                rows = [
                    {"id": "KM_UNDER_10K", "title": "Under 10,000 KM"},
                    {"id": "KM_10_25K", "title": "10-25,000 KM"},
                    {"id": "KM_25_50K", "title": "25-50,000 KM"},
                    {"id": "KM_50_75K", "title": "50-75,000 KM"},
                    {"id": "KM_75_100K", "title": "75,000 - 1,00,000 KM"},
                    {"id": "KM_OVER_100K", "title": "Over 1,00,000 KM"}
                ]
                send_list_message(phone, "Perfect! How many kilometers has your car been driven?",
                    "Select KM", [{"title": "Select KM Range", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        owner_map = {
            "OWNER_1ST": "1st Owner (Me)",
            "OWNER_2ND": "2nd Owner",
            "OWNER_3RD": "3rd Owner",
            "OWNER_MORE": "More than 3 owners"
        }
        if text in owner_map:
            USER_STATE[phone]["valuation_ownership"] = owner_map[text]
        elif is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        else:
            # Handle if user typed the number
            owner_options = list(owner_map.values())
            try:
                option_num = int(text.split('.')[0]) - 1
                if 0 <= option_num < len(owner_options):
                    USER_STATE[phone]["valuation_ownership"] = owner_options[option_num]
                else:
                    USER_STATE[phone]["valuation_ownership"] = text
            except:
                USER_STATE[phone]["valuation_ownership"] = text
        
        USER_STATE[phone]["state"] = "USED_VALUATION_TIMELINE"
        log_valuation_selection(phone, "ownership", USER_STATE[phone]["valuation_ownership"])
        rows = [
            {"id": "TIMELINE_IMMEDIATELY", "title": "🚗 Immediately"},
            {"id": "TIMELINE_WEEK", "title": "📅 Within a week"},
            {"id": "TIMELINE_MONTH", "title": "📅 Within a month"}
        ]
        send_list_message(phone, "Almost done! When are you planning to sell your car?", "Select Timeline", [{"title": "Select Timeline", "rows": rows}])
        return

    if state == "USED_VALUATION_TIMELINE":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_OWNERSHIP"
                rows = [
                    {"id": "OWNER_1ST", "title": "1st Owner (Me)"},
                    {"id": "OWNER_2ND", "title": "2nd Owner"},
                    {"id": "OWNER_3RD", "title": "3rd Owner"},
                    {"id": "OWNER_MORE", "title": "More than 3 owners"}
                ]
                send_list_message(phone, "How many owners has this car had?",
                    "Select Owners", [{"title": "Select Ownership", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        timeline_map = {
            "TIMELINE_IMMEDIATELY": "Immediately",
            "TIMELINE_WEEK": "Within a week",
            "TIMELINE_MONTH": "Within a month"
        }
        
        if text in timeline_map:
            USER_STATE[phone]["valuation_timeline"] = timeline_map[text]
        else:
            t = text.lower().strip()
            if any(k in t for k in ["immediately","now","asap","urgent","right away"]):
                USER_STATE[phone]["valuation_timeline"] = "Immediately"
            elif any(k in t for k in ["week","7 days","this week"]):
                USER_STATE[phone]["valuation_timeline"] = "Within a week"
            elif any(k in t for k in ["month","30 days","this month"]):
                USER_STATE[phone]["valuation_timeline"] = "Within a month"
            elif is_genuine_query(text, state):
                _used_route_via_ai(phone, text, state)
                return
            else:
                USER_STATE[phone]["valuation_timeline"] = text
        
        USER_STATE[phone]["state"] = "USED_VALUATION_CONDITION"
        log_valuation_selection(phone, "timeline", USER_STATE[phone]["valuation_timeline"])
        
        rows = [
            {"id": "CONDITION_EXCELLENT", "title": "⭐⭐⭐⭐⭐ Excellent"},
            {"id": "CONDITION_GOOD", "title": "⭐⭐⭐⭐ Good"},
            {"id": "CONDITION_AVERAGE", "title": "⭐⭐⭐ Average"},
            {"id": "CONDITION_FAIR", "title": "⭐⭐ Fair"}
        ]
        
        send_list_message(phone, "Last question! How would you rate your car's overall condition?", "Select Condition", [{"title": "Select Condition", "rows": rows}])
        return

    if state == "USED_VALUATION_CONDITION":
        # ── 🧠 FIRST: Use AI to understand customer intent ──────────────────
        should_proceed, ai_response, flow_action = _handle_valuation_with_ai_understanding(phone, text, state)
        if not should_proceed:
            if flow_action == "BACK":
                USER_STATE[phone]["state"] = "USED_VALUATION_TIMELINE"
                rows = [
                    {"id": "TIMELINE_IMMEDIATELY", "title": "🚗 Immediately"},
                    {"id": "TIMELINE_WEEK", "title": "📅 Within a week"},
                    {"id": "TIMELINE_MONTH", "title": "📅 Within a month"}
                ]
                send_list_message(phone, "Almost done! When are you planning to sell your car?",
                    "Select Timeline", [{"title": "Select Timeline", "rows": rows}])
            elif flow_action == "MAIN_MENU":
                _used_route_via_ai(phone, text, state)
            elif flow_action == "TALK_TO_ADVISOR":
                from flows.service import bot
                USER_STATE[phone]["_saved_state"] = state
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "USED_AGENT_CONFIRMATION"
            else:
                _reprompt_used_cars_state(phone)
            return
        
        # ── Normal flow continues ──────────────────────────────────────────
        condition_map = {
            "CONDITION_EXCELLENT": "Excellent",
            "CONDITION_GOOD": "Good",
            "CONDITION_AVERAGE": "Average",
            "CONDITION_FAIR": "Fair"
        }
        
        if text in condition_map:
            USER_STATE[phone]["valuation_condition"] = condition_map[text]
        elif is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        else:
            # Handle if user typed the number
            condition_options = list(condition_map.values())
            try:
                option_num = int(text.split('.')[0]) - 1
                if 0 <= option_num < len(condition_options):
                    USER_STATE[phone]["valuation_condition"] = condition_options[option_num]
                else:
                    USER_STATE[phone]["valuation_condition"] = text
            except:
                USER_STATE[phone]["valuation_condition"] = text
        
        USER_STATE[phone]["state"] = "USED_VALUATION_COLLECT_NAME"
        log_valuation_selection(phone, "condition", USER_STATE[phone]["valuation_condition"])
        
        send_whatsapp_message(phone, "Great! We'd love to purchase your car. Let me collect your details:\nYour Name:")
        return

    if state == "USED_VALUATION_COLLECT_NAME":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["valuation_name"] = text.strip()
            USER_STATE[phone]["valuation_phone"] = str(phone)[-10:]
            USER_STATE[phone]["state"] = "USED_VALUATION_COLLECT_LOCATION"
            send_whatsapp_message(phone, "Current Location/City:")
        return
    
    if state == "USED_VALUATION_COLLECT_LOCATION":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        else:
            USER_STATE[phone]["valuation_location"] = text.strip()

            # ===============================
            # SAVE TO DATABASE
            # ===============================
            try:
                import mysql.connector
                from config import DB_CONFIG

                conn = mysql.connector.connect(**DB_CONFIG)
                cursor = conn.cursor()

                cursor.execute("""
                    INSERT INTO valuations 
                    (name, phone, location, brand, model, year, fuel, transmission, km, owners, `condition`, sell_plan, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    USER_STATE[phone]["valuation_name"],
                    USER_STATE[phone]["valuation_phone"],
                    USER_STATE[phone]["valuation_location"],
                    USER_STATE[phone]["valuation_brand"],
                    USER_STATE[phone]["valuation_model"],
                    USER_STATE[phone]["valuation_year"],
                    USER_STATE[phone]["valuation_fuel"],
                    USER_STATE[phone]["valuation_transmission"],
                    USER_STATE[phone]["valuation_km"],
                    USER_STATE[phone]["valuation_ownership"],
                    USER_STATE[phone]["valuation_condition"],
                    USER_STATE[phone].get("valuation_timeline", ""),
                ))

                conn.commit()
                cursor.close()
                conn.close()

            except Exception as e:
                print(f"❌ Valuation insert error: {e}")

            # ===============================
            # BUILD CAR DISPLAY
            # ===============================
            car_year = USER_STATE[phone]["valuation_year"]
            car_brand = USER_STATE[phone]["valuation_brand"]
            car_model = USER_STATE[phone]["valuation_model"]
            car_fuel = USER_STATE[phone]["valuation_fuel"]
            car_transmission = USER_STATE[phone]["valuation_transmission"]

            if car_year == "Older than 2020":
                car_display = f"{car_brand} {car_model}"
            else:
                car_display = f"{car_brand} {car_model} {car_year}"

            # ===============================
            # SEND CONFIRMATION MESSAGE
            # ===============================
            confirmation_message = (
                f"Perfect {USER_STATE[phone]['valuation_name']}! Here's what happens next:\n\n"
                f"📋 SELLER CONFIRMATION:\n"
                f"👤 Name: {USER_STATE[phone]['valuation_name']}\n"
                f"📱 Phone: {USER_STATE[phone]['valuation_phone']}\n"
                f"🚗 Car: {car_display}\n"
                f"⛽ Fuel: {car_fuel}\n"
                f"⚙️ Transmission: {car_transmission}\n"
                f"📅 Selling Timeline: {USER_STATE[phone].get('valuation_timeline', 'N/A')}\n"
                f"📍 Location: {USER_STATE[phone]['valuation_location']}\n\n"
                f"📞 Questions? Call: +91-9876543210\n\n"
                f"Thank you for choosing Sherpa Hyundai! 😊"
            )

            send_whatsapp_message(phone, confirmation_message)

            # ===============================
            # SHOW MAIN MENU CLEANLY
            # ===============================
            USER_STATE[phone] = {"state": "USED_CARS_MENU"}

            sections = [{
                "title": "Select",
                "rows": [
                    {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
                    {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
                    {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
                    {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
                    {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
                    {"id": "END_CONVERSATION",       "title": "👋 End Conversation"}
                ]
            }]

            send_list_message(
                phone,
                "Is there anything else I can help you with today?",
                "Select",
                sections
            )

            return

    # ========================================
    # USED CAR BOOKING - SELECT WHEN
    # ========================================
    if state == "USED_BOOK_SELECT_WHEN":


        when_map = {
            "USED_WHEN_TODAY": "Today",
            "USED_WHEN_TOMORROW": "Tomorrow",
            "USED_WHEN_LATER_WEEK": "Later This Week",
            "USED_WHEN_NEXT_WEEK": "Next Week"
        }

        selected = when_map.get(text)

        if not selected:
            # ── AI free-text resolver for schedule preference ─────────────
            t = text.lower().strip()
            resolved_when = None
            if any(k in t for k in ["today","aaj","now","asap","same day","right now","immediately"]):
                resolved_when = "USED_WHEN_TODAY"
            elif any(k in t for k in ["tomorrow","kal","next day","tommorow","tomorow"]):
                resolved_when = "USED_WHEN_TOMORROW"
            elif any(k in t for k in ["next week","coming week","following week","after this week"]):
                resolved_when = "USED_WHEN_NEXT_WEEK"
            elif any(k in t for k in ["this week","later this week","later","few days","end of week","within week"]):
                resolved_when = "USED_WHEN_LATER_WEEK"

            if resolved_when:
                # Re-process with resolved ID
                text = resolved_when
                selected = when_map.get(text)
            
            else:
                # ── Smart Date Resolver (Free-text) ──
                all_dates = sorted(list(set([date.today(), date.today() + timedelta(days=1)] + get_remaining_week_dates_used() + get_next_week_dates_used())))
                resolved_id = resolve_date_from_text(text, all_dates, prefix="USED_WHEN_")
                
                if resolved_id:
                    # Map resolved ID back to our local structure if it matches Today/Tomorrow
                    today_str = date.today().strftime("%Y%m%d")
                    tom_str = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
                    
                    if resolved_id.replace("USED_WHEN_", "") == today_str:
                        text = "USED_WHEN_TODAY"
                    elif resolved_id.replace("USED_WHEN_", "") == tom_str:
                        text = "USED_WHEN_TOMORROW"
                    else:
                        # Exact date from week/next week
                        target_date = datetime.strptime(resolved_id.replace("USED_WHEN_", ""), "%Y%m%d")
                        valid, reason = is_valid_appointment_slot(target_date.date())
                        if not valid:
                            send_whatsapp_message(phone, reason)
                            return
                        USER_STATE[phone]["test_drive_date"] = target_date.strftime("%d-%m-%Y")
                        USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"
                        send_whatsapp_message(phone, f"✅ Got it — *{target_date.strftime('%d-%m-%Y')}*.")
                        
                        # Proceed to time selection
                        buttons = [
                            {"type": "reply", "reply": {"id": "USED_TIME_MORNING",   "title": "Morning (10AM-12PM)"}},
                            {"type": "reply", "reply": {"id": "USED_TIME_AFTERNOON", "title": "Afternoon (12PM-4PM)"}},
                            {"type": "reply", "reply": {"id": "USED_TIME_EVENING",   "title": "Evening (4PM-7PM)"}}
                        ]
                        send_button_message(phone, "Perfect! Which time works best for you?", buttons)
                        return
                    
                    selected = when_map.get(text)
                
                if not selected:
                    _used_route_via_ai(phone, text, state)
                    return

        # ✅ TODAY
        if selected == "Today":
            today_date = date.today()
            # ── After-6PM auto-advance: move today → tomorrow silently ──────
            from utils import get_smart_default_date
            smart_date, was_redirected = get_smart_default_date()
            if was_redirected:
                tomorrow_str = smart_date.strftime("%d-%m-%Y")
                USER_STATE[phone]["test_drive_date"] = tomorrow_str
                USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"
                send_whatsapp_message(
                    phone,
                    f"⏰ Since it's after 6 PM, same-day appointments are unavailable.\n\n"
                    f"✅ I've scheduled your test drive for *tomorrow ({tomorrow_str})* instead.\n\n"
                    f"Please choose a preferred time slot 👇"
                )
            else:
                # ── 6 PM cutoff: reject Today bookings after 6 PM ─────────
                valid, reason = is_valid_appointment_slot(today_date)
                if not valid:
                    send_whatsapp_message(phone, reason)
                    from datetime import datetime as _dt2
                    _now2 = _dt2.now()
                    _rows2 = []
                    if _now2.hour < 18:
                        _rows2.append({"id": "USED_WHEN_TODAY", "title": "Today (if available)"})
                    _rows2 += [
                        {"id": "USED_WHEN_TOMORROW",   "title": "Tomorrow"},
                        {"id": "USED_WHEN_LATER_WEEK", "title": "Later This Week"},
                        {"id": "USED_WHEN_NEXT_WEEK",  "title": "Next Week"}
                    ]
                    send_list_message(phone,
                        "Please choose a different date:",
                        "Select",
                        [{"title": "Select Schedule", "rows": _rows2}])
                    return
                USER_STATE[phone]["test_drive_date"] = today_date.strftime("%d-%m-%Y")
                USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"

        # ✅ TOMORROW
        elif selected == "Tomorrow":
            tomorrow = date.today() + timedelta(days=1)
            USER_STATE[phone]["test_drive_date"] = tomorrow.strftime("%d-%m-%Y")
            USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"

        # ✅ LATER THIS WEEK
        elif selected == "Later This Week":

            USER_STATE[phone]["state"] = "USED_SELECT_EXACT_DATE"

            week_dates = get_remaining_week_dates_used()

            if not week_dates:
                send_whatsapp_message(
                    phone,
                    "No available dates remaining this week. Please choose Next Week."
                )
                return

            rows = []
            # WhatsApp allows max 10 rows
            for d in week_dates[:10]:
                rows.append({
                    "id": f"DATE_{d.strftime('%Y%m%d')}",
                    "title": d.strftime("%A %d %b"),  # Full day name
                    "description": "Choose this date"
                })

            send_list_message(
                phone,
                "Please select a date:",
                "Select Date",
                [{"title": "Available Dates", "rows": rows}]
            )
            return

        # ✅ NEXT WEEK
        elif selected == "Next Week":

            USER_STATE[phone]["state"] = "USED_SELECT_EXACT_DATE"

            week_dates = get_next_week_dates_used()

            rows = []
            # WhatsApp allows max 10 rows
            for d in week_dates[:10]:
                rows.append({
                    "id": f"DATE_{d.strftime('%Y%m%d')}",
                    "title": d.strftime("%A %d %b"),  # Full day
                    "description": "Choose this date"
                })

            send_list_message(
                phone,
                "Please select a date:",
                "Select Date",
                [{"title": "Available Dates", "rows": rows}]
            )
            return

        # If Today or Tomorrow → ask time
        buttons = [
            {
                "type": "reply",
                "reply": {"id": "USED_TIME_MORNING", "title": "Morning (10AM-12PM)"}
            },
            {
                "type": "reply",
                "reply": {"id": "USED_TIME_AFTERNOON", "title": "Afternoon (12PM-4PM)"}
            },
            {
                "type": "reply",
                "reply": {"id": "USED_TIME_EVENING", "title": "Evening (4PM-7PM)"}
            }
        ]

        send_button_message(
            phone,
            "Perfect! Which time works best for you?",
            buttons
        )

        return


    # ========================================
    # SELECT EXACT DATE (Later / Next Week)
    # ========================================
    if state == "USED_SELECT_EXACT_DATE":

        if text.startswith("DATE_"):

            raw_date = text.replace("DATE_", "")  # 20260220
            formatted_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%d-%m-%Y")

            USER_STATE[phone]["test_drive_date"] = formatted_date
            USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"

            buttons = [
                {
                    "type": "reply",
                    "reply": {"id": "USED_TIME_MORNING", "title": "Morning (10AM-12PM)"}
                },
                {
                    "type": "reply",
                    "reply": {"id": "USED_TIME_AFTERNOON", "title": "Afternoon (12PM-4PM)"}
                },
                {
                    "type": "reply",
                    "reply": {"id": "USED_TIME_EVENING", "title": "Evening (4PM-7PM)"}
                }
            ]

            send_button_message(
                phone,
                "Perfect! Which time works best for you?",
                buttons
            )
            return

        else:
            # ── AI free-text date resolver ────────────────────────────────
            # Handles "24", "Monday", "24th march", "24th" etc.
            # Determine which week's dates were shown based on prior state history
            # We check both this week and next week dates to find a match
            all_dates = get_remaining_week_dates_used() + get_next_week_dates_used()
            resolved = resolve_date_from_text(text, all_dates, "DATE_")
            if resolved and resolved.startswith("DATE_"):
                raw_date = resolved.replace("DATE_", "")
                formatted_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%d-%m-%Y")
                d_obj = datetime.strptime(raw_date, "%Y%m%d")
                USER_STATE[phone]["test_drive_date"] = formatted_date
                USER_STATE[phone]["state"] = "USED_BOOK_SELECT_TIME"
                send_whatsapp_message(phone, f"✅ Got it — *{d_obj.strftime('%A, %d %B %Y')}* selected.")
                buttons = [
                    {"type": "reply", "reply": {"id": "USED_TIME_MORNING",   "title": "Morning (10AM-12PM)"}},
                    {"type": "reply", "reply": {"id": "USED_TIME_AFTERNOON", "title": "Afternoon (12PM-4PM)"}},
                    {"type": "reply", "reply": {"id": "USED_TIME_EVENING",   "title": "Evening (4PM-7PM)"}}
                ]
                send_button_message(phone, "Perfect! Which time works best for you?", buttons)
            else:
                # Re-show date list or let AI handle
                _used_route_via_ai(phone, text, state)
            return

    # ========================================
    # SELECT TIME SLOT
    # ========================================
    if state == "USED_BOOK_SELECT_TIME":

        time_map = {
            "USED_TIME_MORNING": "Morning (10-12 PM)",
            "USED_TIME_AFTERNOON": "Afternoon (12-4 PM)",
            "USED_TIME_EVENING": "Evening (4-7PM)"
        }

        if text not in time_map:
            # ── AI free-text time resolver ────────────────────────────────
            t = text.lower().strip()
            resolved_slot = None
            if any(k in t for k in ["morning","10 am","10am","before noon","early","am slot","forenoon"]):
                resolved_slot = "USED_TIME_MORNING"
            elif any(k in t for k in ["afternoon","12 pm","12pm","1 pm","2 pm","3 pm","after noon","lunch","midday","noon"]):
                resolved_slot = "USED_TIME_AFTERNOON"
            elif any(k in t for k in ["evening","4 pm","5 pm","6 pm","4pm","5pm","6pm","after 4","late"]):
                resolved_slot = "USED_TIME_EVENING"
            elif any(k in t for k in ["anytime","any time","flexible","any slot","doesn't matter","doesnt matter","no preference"]):
                resolved_slot = "USED_TIME_MORNING"   # default to morning for anytime

            if resolved_slot:
                text = resolved_slot
                send_whatsapp_message(phone, f"✅ Got it — *{time_map[resolved_slot]}*.")
            else:
                _used_route_via_ai(phone, text, state)
                return

        USER_STATE[phone]["test_drive_time"] = time_map[text]
        USER_STATE[phone]["state"] = "USED_BOOK_COLLECT_NAME"

        send_whatsapp_message(phone, "Great! I need some details to confirm your booking:\n" "1.Your Name:")
        return

    if state == "USED_BOOK_COLLECT_NAME":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
        else:
            USER_STATE[phone]["customer_name"] = text.strip()
            USER_STATE[phone]["customer_phone"] = str(phone)[-10:]
            USER_STATE[phone]["state"] = "USED_BOOK_COLLECT_LICENSE"

            buttons = [
                {"type": "reply", "reply": {"id": "LICENSE_YES", "title": "✅ Yes"}},
                {"type": "reply", "reply": {"id": "LICENSE_NO", "title": "❌ No"}}
            ]
            send_button_message(phone, "2. Do you have a valid driving license?", buttons)
        return
    
    if state == "USED_BOOK_COLLECT_LICENSE":
        if text == "LICENSE_YES":
            USER_STATE[phone]["license"] = "Yes"
        elif text == "LICENSE_NO":
            USER_STATE[phone]["license"] = "No"
        else:
            _used_route_via_ai(phone, text, state)
            return

        USER_STATE[phone]["state"] = "USED_BOOK_SELECT_LOCATION_TYPE"

        buttons = [
            {
                "type": "reply",
                "reply": {"id": "FROM_SHOWROOM", "title": "🏢 From showroom"}
            },
            {
                "type": "reply",
                "reply": {"id": "TO_MY_LOCATION", "title": "🏠 To my location"}
            }
        ]

        send_button_message(
            phone,
            "Thank you! Your details are noted.\n\n"
            "Where would you like to take the test drive?",
            buttons
        )
        return


    # ========================================
    # SELECT LOCATION TYPE
    # ========================================
    if state == "USED_BOOK_SELECT_LOCATION_TYPE":
        if text == "FROM_SHOWROOM":
            USER_STATE[phone]["location_type"] = "showroom"
            USER_STATE[phone]["location"] = "Sherpa Hyundai - Main Showroom, 123 MG Road, Bangalore"
            
            # Fetch car details
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT make, model FROM carstockdata WHERE serial_number = %s", (USER_STATE[phone]["selected_used_car_id"],))
            car = cur.fetchone()
            cur.close()
            conn.close()

            car_display = f"{car['make']} {car['model']}" if car else USER_STATE[phone]["selected_used_car_name"]
            
            booking_details = (
                "Please confirm your test drive details:\n\n"
                f"👤 Name: {USER_STATE[phone]['customer_name']}\n"
                f"🚗 Car: {car_display}\n"
                f"📅 Date: {USER_STATE[phone]['test_drive_date']}\n"
                f"⏰ Time: {USER_STATE[phone]['test_drive_time']}\n"
                f"📍 Location: Our Showroom\n\n"
                "🏢 Address: 123 MG Road, Bangalore\n"
            )
            send_whatsapp_message(phone, booking_details)
            
            USER_STATE[phone]["state"] = "USED_BOOK_CONFIRM"
            buttons = [
                {"type": "reply", "reply": {"id": "BOOK_CONFIRM", "title": "✅ Confirm"}},
                {"type": "reply", "reply": {"id": "BOOK_CANCEL", "title": "❌ Cancel"}}
            ]
            send_button_message(phone, "Would you like to confirm this booking?", buttons)
            return
        elif text == "TO_MY_LOCATION":
            USER_STATE[phone]["location_type"] = "customer"
            USER_STATE[phone]["state"] = "USED_BOOK_COLLECT_LOCATION"
            send_whatsapp_message(phone, "Please type your location/address for the test drive:")
            return
        else:
            _used_route_via_ai(phone, text, state)
            return


    if state == "USED_BOOK_COLLECT_LOCATION":
        if is_genuine_query(text, state):
            _used_route_via_ai(phone, text, state)
            return
        
        USER_STATE[phone]["location"] = text.strip()
        
        # Build and send the booking details message first
        name = USER_STATE[phone]["customer_name"]
        phone_number = USER_STATE[phone]["customer_phone"]
        # Fetch car details from DB
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT make, model, variant, manufacturing_year, estimated_selling_price
            FROM carstockdata
            WHERE serial_number = %s
        """, (USER_STATE[phone]["selected_used_car_id"],))
        car = cur.fetchone()
        cur.close()
        conn.close()

        if car:
            car_name = f"{car['make']} {car['model']} {car.get('variant','')} {car.get('manufacturing_year','')}".strip()
            price = int(car.get("estimated_selling_price") or 0)
            car_display = f"{car_name} (₹{price:,})"
        else:
            car_display = USER_STATE[phone]["selected_used_car_name"]

        date = USER_STATE[phone]["test_drive_date"]
        time = USER_STATE[phone]["test_drive_time"]
        location = USER_STATE[phone]["location"]
        
        # Format date
        try:
            from datetime import datetime
            date_obj = datetime.strptime(date, "%d-%m-%Y")
            formatted_date = date_obj.strftime("%A, %d %b %Y")
        except:
            formatted_date = date
        
        booking_details = (
            "Please confirm your test drive details:\n\n"
            f"👤 Name: {name}\n"
            f"📱 Phone: {phone_number}\n"
            f"🚗 Car: {car_display}\n"
            f"📅 Date: {formatted_date}\n"
            f"⏰ Time: {time}\n"
            f"📍 Location: {location}\n\n"
            "📍 Showroom Address:\n"
            "🏢 Sherpa Hyundai - Main Showroom\n"
            "Address:123 MG Road, Bangalore - 560001\n"
            "📞 Phone: +91-9876543210\n"
            "🕒 Timings: Mon-Sat: 9AM-8PM, Sun: 10AM-6PM\n"
            "🅿️ Free parking available\n\n"
            "📞 Need help? Call us: +91-9876543210\n\n"
            "*Quick reminder*: We'll also have financing options ready if you like the car during your test drive!\n\n"
        )
        
        send_whatsapp_message(phone, booking_details)
        
        # Then send the Confirm/Cancel buttons
        USER_STATE[phone]["state"] = "USED_BOOK_CONFIRM"
        
        buttons = [
            {
                "type": "reply",
                "reply": {"id": "BOOK_CONFIRM", "title": "✅ Confirm"}
            },
            {
                "type": "reply",
                "reply": {"id": "BOOK_CANCEL", "title": "❌ Cancel"}
            }
        ]

        send_button_message(
            phone,
            "Would you like to confirm this booking?",
            buttons
        )
        return


    # ========================================
    # CONFIRM BOOKING
    # ========================================
    if state == "USED_BOOK_CONFIRM":

        if text == "BOOK_CONFIRM":

            # ===============================
            # STEP 1: Save Booking in DB
            # ===============================

            name = USER_STATE[phone]["customer_name"]
            phone_number = USER_STATE[phone]["customer_phone"]
            car_name = USER_STATE[phone]["selected_used_car_name"]
            date = USER_STATE[phone]["test_drive_date"]
            time = USER_STATE[phone]["test_drive_time"]
            location = USER_STATE[phone]["location"]
            license_status = USER_STATE[phone]["license"]

            try:
                import mysql.connector
                from config import DB_CONFIG

                conn = mysql.connector.connect(**DB_CONFIG)
                cursor = conn.cursor()

                cursor.execute("""
                    INSERT INTO bookings
                    (name, phone, license, car_serial_number,
                    test_drive_date, test_drive_time, location, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    name,
                    phone_number,
                    license_status,
                    USER_STATE[phone]["selected_used_car_id"],
                    date,
                    time,
                    location
                ))

                conn.commit()
                cursor.close()
                conn.close()

            except Exception as e:
                print("❌ Booking insert error:", e)

            # ===============================
            # STEP 2: Send Success Message
            # ===============================
            
            success_message = (
                "✅ Test drive booked successfully! We'll reach out with confirmation details."
            )
            
            send_whatsapp_message(phone, success_message)

            # ✅ Store booking confirmation in DB
            try:
                from ai import store_interaction
                store_interaction(phone,
                                  f"✅ Test drive confirmed - {car_name} on {date} at {time}",
                                  success_message, "used_cars")
            except Exception as _e:
                print(f"[used_cars] store_interaction error: {_e}")

            # ===============================
            # STEP 3: Ask if anything else needed
            # ===============================
            
            ask_message = "Is there anything else I can help you with today?"
            
            # Reset state to main menu
            USER_STATE[phone] = {"state": "USED_CARS_MENU"}
            
            # Show main menu options with End Conversation
            sections = [{
                "title": "Select",
                "rows": [
                    {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
                    {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
                    {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
                    {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
                    {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
                    {"id": "END_CONVERSATION",       "title": "👋 End Conversation"}
                ]
            }]

            send_list_message(
                phone,
                ask_message,
                "Select",
                sections
            )
            return


        elif text == "BOOK_CANCEL":

            send_whatsapp_message(
                phone,
                "❌ Your booking has been cancelled."
            )

            USER_STATE[phone] = {"state": "USED_CARS_MENU"}
            sections = [{
                "title": "Select",
                "rows": [
                    {"id": "USED_BROWSE",           "title": "🚗 Browse Used Cars"},
                    {"id": "USED_VALUATION",         "title": "💰 Get Car Valuation"},
                    {"id": "USED_CONTACT",           "title": "📞 Contact Our Team"},
                    {"id": "USED_SALES_FLOW",        "title": "🔹 New Cars – Sales"},
                    {"id": "USED_MAIN_MENU",         "title": "🏠 Back to Main Menu"},
                    {"id": "END_CONVERSATION",       "title": "👋 End Conversation"}
                ]
            }]

            send_list_message(
                phone,
                "Is there anything else I can help you with today?",
                "Select",
                sections
            )

            return

        # ── Free text → central AI router ──────────────────────────────────
        else:
            _used_route_via_ai(phone, text, state)
            return

def extract_gridfs_image(url_path, prefix):
    if not url_path: return None
    if url_path.startswith("http"): return url_path
    if not url_path.startswith("/images/"): return None
    
    try:
        from pymongo import MongoClient
        from bson.objectid import ObjectId
        import gridfs
        import os
        from config import MONGO_URI, BASE_URL
        
        file_id = url_path.split("/")[-1]
        client = MongoClient(MONGO_URI)
        db = client["whatsapp_bot"]
        fs = gridfs.GridFS(db)
        
        grid_out = fs.get(ObjectId(file_id))
        os.makedirs("static/cars", exist_ok=True)
        filename = f"{prefix}_{file_id}.jpg"
        filepath = os.path.join("static", "cars", filename)
        with open(filepath, "wb") as f:
            f.write(grid_out.read())
        return f"{BASE_URL}/static/cars/{filename}"
    except Exception as e:
        print(f"Error extracting GridFS image: {e}")
        return None

def _show_used_cars(phone):
    from webhook import send_whatsapp_message, send_whatsapp_image, send_button_message
    import time

    data = USER_STATE.get(phone, {})
    if not data:
        return

    min_p, max_p = map_budget_to_range(data["selected_budget"])

    car_type = "all" if data["selected_type"] == "TYPE_ALL" else \
        data["selected_type"].replace("TYPE_", "").replace("_", " ").title()

    brand = "all" if data["selected_brand"] == "BRAND_ALL" else \
        data["selected_brand"].replace("BRAND_", "").replace("_", " ").title()

    # READ current offset BEFORE fetching
    offset = data.get("offset", 0)
    limit = 5

    print(f"📦 _show_used_cars called | phone={phone} | offset={offset} | car_type={car_type} | brand={brand}")

    cars = get_cars_for_filters(min_p, max_p, car_type, brand, limit, offset)
    total = get_total_cars_count(min_p, max_p, car_type, brand)

    print(f"📊 Total cars: {total} | Fetched: {len(cars)} | Showing: {offset+1} to {offset+len(cars)}")

    if not cars:
        send_whatsapp_message(phone, "❌ No more cars available.")
        return

    # ─────────────────────────────────────
    # SHOW CARS
    # ─────────────────────────────────────
    for car in cars:
        import json
        raw_image = car.get("image_url") or ""
        images = []
        try:
            if raw_image.startswith("{"):
                images = list(json.loads(raw_image).values())
            elif raw_image.startswith("["):
                images = json.loads(raw_image)
            else:
                images = [i.strip() for i in raw_image.split(",") if i.strip()]
        except:
            images = [raw_image]
            
        valid_images = []
        for img in images:
            if img.startswith("/images/"):
                extracted = extract_gridfs_image(img, f"used_{car['serial_number']}")
                if extracted:
                    valid_images.append(extracted)
            elif img.startswith("http"):
                valid_images.append(img)

        if valid_images:
            send_whatsapp_image(phone, valid_images[0])
            time.sleep(2.0)

        car_title = f"{car['make']} {car['model']} {car.get('variant', '')}".strip()

        details_text = (
            f"🚗 *{car_title}*\n"
            f"📅 Year: {car.get('manufacturing_year', '')}\n"
            f"⛽ Fuel: {car.get('fuel_type', '')}\n"
            f"📍 KM: {car.get('mileage_km', '')}\n"
            f"💰 Price: ₹{int(car.get('estimated_selling_price') or 0):,}"
        )

        buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": f"SELECT_{car['serial_number']}",
                    "title": "SELECT"
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": f"VIEWMORE_{car['serial_number']}",
                    "title": "📸 View More Photos"
                }
            }
        ]

        send_button_message(phone, details_text, buttons)
        time.sleep(1.0)

    # ─────────────────────────────────────
    # PAGINATION INFO + FOOTER BUTTONS
    # Combined into ONE message to avoid empty bubble
    # ─────────────────────────────────────
    start_num = offset + 1
    end_num = offset + len(cars)

    more_available = end_num < total

    time.sleep(1.5)

    pagination_text = f"Showing cars {start_num}–{end_num} out of {total}"

    if more_available:
        footer_buttons = [
            {
                "type": "reply",
                "reply": {"id": "BROWSE_MORE", "title": "Browse More Cars"}
            },
            {
                "type": "reply",
                "reply": {"id": "END_CONVERSATION", "title": "👋 End conversation"}
            }
        ]
    else:
        footer_buttons = [
            {
                "type": "reply",
                "reply": {"id": "CHANGE_CRITERIA", "title": "🔄 Change Criteria"}
            },
            {
                "type": "reply",
                "reply": {"id": "END_CONVERSATION", "title": "👋 End conversation"}
            }
        ]

    send_button_message(phone, pagination_text, footer_buttons)

    # ─────────────────────────────────────
    # UPDATE OFFSET FOR NEXT BATCH
    # ─────────────────────────────────────
    USER_STATE[phone]["offset"] = end_num
    print(f"✅ Offset updated to {end_num} for next batch")

def _show_more_images(phone, car_id):
    from webhook import send_whatsapp_image, send_button_message
    import time

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT image_url FROM carstockdata WHERE serial_number = %s", (car_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row.get("image_url"):
        return

    import json
    raw_image = row["image_url"] or ""
    images = []
    try:
        if raw_image.startswith("{"):
            images = list(json.loads(raw_image).values())
        elif raw_image.startswith("["):
            images = json.loads(raw_image)
        else:
            images = [i.strip() for i in raw_image.split(",") if i.strip()]
    except:
        images = [raw_image]
        
    valid_images = []
    for i, img in enumerate(images):
        if img.startswith("/images/"):
            extracted = extract_gridfs_image(img, f"used_{car_id}_{i}")
            if extracted:
                valid_images.append(extracted)
        elif img.startswith("http"):
            valid_images.append(img)

    # 🔥 Store current car for back button
    USER_STATE[phone]["last_viewed_car_id"] = car_id

    # Send remaining images (skip first image)
    for img in valid_images[1:]:
        send_whatsapp_image(phone, img)
        time.sleep(3.2)   # ✅ IMPORTANT → prevents middle overlap

    # After ALL images finish → show buttons
    send_button_message(
        phone,
        "What would you like to do next?",
        [
            {"type": "reply", "reply": {"id": f"SELECT_{car_id}", "title": "SELECT"}},
            {"type": "reply", "reply": {"id": "BACK_TO_CARS", "title": "⬅️ Back to Cars"}}
        ]
    )

def _reprompt_used_cars_state(phone):
    """
    Re-prompts the user for the current state in the used cars flow.
    Used after an AI response to a mid-flow query.
    """
    from webhook import send_whatsapp_message, send_list_message, send_button_message
    state = USER_STATE.get(phone, {}).get("state", "USED_CARS_MENU")
    
    if state == "USED_CARS_MENU":
        handle_used_cars(phone)
    
    elif state == "USED_SELECT_BUDGET":
        sections = [{"title": "Select Budget", "rows": [
            {"id": "under1", "title": "Under ₹1 Lakhs"},
            {"id": "1-3", "title": "₹1–3 Lakhs"},
            {"id": "3-5", "title": "₹3–5 Lakhs"},
            {"id": "5-10", "title": "₹5–10 Lakhs"},
            {"id": "above10", "title": "Above ₹10 Lakhs"}
        ]}]
        send_list_message(phone, "What's your budget range?", "Select", sections)
        
    elif state == "USED_SELECT_TYPE":
        send_whatsapp_message(phone, "Please select your preferred car type from the list above.")
        
    elif state == "USED_SELECT_BRAND":
        send_whatsapp_message(phone, "Please select your preferred brand from the list above.")
        
    elif state == "USED_VALUATION_MODE":
        sections = [{"title": "Select", "rows": [
            {"id": "VALUATION_EXCHANGE", "title": "🔁 Exchange"},
            {"id": "VALUATION_ONEWAY", "title": "💰 One Way Selling"}
        ]}]
        send_list_message(phone, "Would you like to exchange or go for One Way Selling?", "Select", sections)
        
    # --- Exchange Flow Reprompts ---
    elif state == "EXCH_BRAND":
        send_whatsapp_message(phone, "Please select or type your current car brand to continue:")
        
    elif state == "EXCH_MODEL":
        send_whatsapp_message(phone, "Please select your car model from the list above:")
        
    elif state == "EXCH_MODEL_ENTER":
        send_whatsapp_message(phone, "Please type your car model:")
        
    elif state == "EXCH_YEAR":
        send_whatsapp_message(phone, "Please select the year of your car from the list above:")
        
    elif state == "EXCH_FUEL":
        send_whatsapp_message(phone, "Please select the fuel type of your car:")
        
    elif state == "EXCH_TRANS":
        send_whatsapp_message(phone, "Please select the transmission type:")
        
    elif state == "EXCH_KM":
        send_whatsapp_message(phone, "How many kilometers has your car been driven? Please select a range:")
        
    elif state == "EXCH_OWNERS":
        send_whatsapp_message(phone, "How many owners has this car had?")
        
    elif state == "EXCH_CONDITION":
        send_whatsapp_message(phone, "How would you rate your car's condition?")
        
    elif state == "EXCH_INSURANCE":
        send_whatsapp_message(phone, "Does the car have valid insurance?")
        
    elif state == "EXCH_DOCS":
        send_whatsapp_message(phone, "Which documents do you have available?")
        
    elif state == "EXCH_TYPE":
        send_whatsapp_message(phone, "Would you like to exchange for a New or Used car?")
        
    elif state == "EXCH_BUDGET":
        send_whatsapp_message(phone, "What is your budget range for the next car?")
        
    elif state == "EXCH_NEW_BRAND":
        send_whatsapp_message(phone, "Which brand are you interested in?")
        
    elif state == "EXCH_NEW_MODEL":
        send_whatsapp_message(phone, "Which model interests you?")
        
    elif state == "EXCH_PROCEED":
        send_whatsapp_message(phone, "How would you like to proceed? (Finance or Test Drive)")

    elif state == "EXCH_FINANCE_CALLBACK":
        send_whatsapp_message(phone, "Please provide your name for the finance callback:")

    elif state == "EXCH_TD_LOCATION":
        send_whatsapp_message(phone, "Where would you like to take the test drive? (Showroom or My Location)")

    elif state == "EXCH_TD_ADDRESS":
        send_whatsapp_message(phone, "Please type your address for the test drive:")

    elif state == "EXCH_TD_WHEN":
        send_whatsapp_message(phone, "When would you prefer to take the test drive?")

    elif state == "EXCH_TD_EXACT_DATE":
        send_whatsapp_message(phone, "Please select an exact date for your test drive:")

    elif state == "EXCH_TD_TIME":
        send_whatsapp_message(phone, "Which time slot works best for you?")

    elif state == "EXCH_COLLECT_NAME":
        send_whatsapp_message(phone, "Almost done! Please provide your name:")

    elif state == "EXCH_COLLECT_CITY":
        send_whatsapp_message(phone, "And finally, which city are you located in?")

    # --- One-Way Valuation Reprompts ---
    elif state == "USED_VALUATION_BRAND":
        send_whatsapp_message(phone, "Please select the brand of your car:")
        
    elif state == "USED_VALUATION_MODEL":
        send_whatsapp_message(phone, "Please select your car model:")
        
    elif state == "USED_VALUATION_YEAR":
        send_whatsapp_message(phone, "What year is your car?")
        
    elif state == "USED_VALUATION_FUEL":
        send_whatsapp_message(phone, "What's the fuel type of your car?")
        
    elif state == "USED_VALUATION_KM":
        send_whatsapp_message(phone, "How many kilometers has it been driven?")
        
    elif state == "USED_VALUATION_TIMELINE":
        send_whatsapp_message(phone, "When are you planning to sell?")

    elif state == "USED_VALUATION_TRANSMISSION":
        send_whatsapp_message(phone, "What is the transmission type of your car?")

    elif state == "USED_VALUATION_OWNERSHIP":
        send_whatsapp_message(phone, "How many owners has the car had?")

    elif state == "USED_VALUATION_CONDITION":
        send_whatsapp_message(phone, "How would you rate your car's condition?")

    elif state == "USED_VALUATION_COLLECT_NAME":
        send_whatsapp_message(phone, "We'd love to purchase your car. Please provide your name:")

    elif state == "USED_VALUATION_COLLECT_LOCATION":
        send_whatsapp_message(phone, "And your current location/city?")

    elif state == "USED_BOOK_SELECT_WHEN":
        send_whatsapp_message(phone, "When would you prefer to take the test drive?")

    elif state == "USED_SELECT_EXACT_DATE":
        send_whatsapp_message(phone, "Please select an exact date for the test drive:")

    elif state == "USED_BOOK_SELECT_TIME":
        send_whatsapp_message(phone, "Which time slot works best for you?")

    elif state == "USED_BOOK_COLLECT_NAME":
        send_whatsapp_message(phone, "Please provide your name for the booking:")

    elif state == "USED_BOOK_COLLECT_LICENSE":
        send_whatsapp_message(phone, "Do you have a valid driving license?")

    elif state == "USED_BOOK_SELECT_LOCATION_TYPE":
        send_whatsapp_message(phone, "Where would you like the test drive? (Showroom or Home)")

    elif state == "USED_BOOK_COLLECT_LOCATION":
        send_whatsapp_message(phone, "Please provide your address for the test drive:")

    elif state == "USED_BOOK_CONFIRM":
        send_whatsapp_message(phone, "Would you like to confirm this booking?")

    elif state == "USED_AGENT_CONFIRMATION":
        from flows.service import bot
        bot.trigger_human_agent_confirmation(phone)
    
    else:
        # Fallback for unknown states
        send_whatsapp_message(phone, "You were in the middle of a process. Please continue or type 'Hi' to start over.")
