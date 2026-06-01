# flows/refinancing.py
# ============================================================
# Refinancing / Car Loan Flow
# Handles: EMI reduction, loan against car, loan transfer,
#          loan eligibility check.
# Mobile number is auto-fetched from the WhatsApp sender phone.
# All leads saved to MySQL table: refinancing_leads
# ============================================================

import mysql.connector
from datetime import datetime
from config import USER_STATE, DB_CONFIG
from webhook import send_whatsapp_message, send_button_message, send_list_message


# ============================================================
# DB HELPERS
# ============================================================

def _get_customer_name_from_db(phone: str) -> str | None:
    """
    Try to fetch the customer's name from the vehicle table
    using the last 10 digits of their WhatsApp number.
    Falls back to None if not found.
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        mobile_10 = str(phone)[-10:]

        # Try vehicle table first (uses ownerMobileNo column)
        cursor.execute(
            "SELECT ownerName FROM vehicle WHERE RIGHT(ownerMobileNo, 10) = %s LIMIT 1",
            (mobile_10,)
        )
        row = cursor.fetchone()
        if row and row.get("ownerName"):
            cursor.close(); conn.close()
            return row["ownerName"]

        # Fallback: appointment_bookings table
        cursor.execute(
            "SELECT full_name FROM appointment_bookings WHERE RIGHT(phone_number, 10) = %s LIMIT 1",
            (mobile_10,)
        )
        row = cursor.fetchone()
        cursor.close(); conn.close()
        if row and row.get("full_name"):
            return row["full_name"]

        return None
    except Exception as e:
        print(f"❌ _get_customer_name_from_db error: {e}")
        return None


def save_refinancing_lead(data: dict) -> int | None:
    """
    Insert a refinancing lead into the `refinancing_leads` table.
    Creates the table automatically if it does not exist.
    Returns the new row ID or None on failure.
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # ── Auto-create table if missing ─────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS refinancing_leads (
                id                  INT AUTO_INCREMENT PRIMARY KEY,
                phone_number        VARCHAR(20)  NOT NULL,
                customer_name       VARCHAR(100),
                city                VARCHAR(100),
                intent_type         VARCHAR(100),
                car_brand           VARCHAR(100),
                car_model           VARCHAR(100),
                year_of_manufacture VARCHAR(10),
                has_existing_loan   VARCHAR(5),
                remaining_loan_amt  VARCHAR(50),
                loan_requirement    VARCHAR(50),
                contact_preference  VARCHAR(50),
                created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                status              VARCHAR(20) DEFAULT 'new',
                INDEX idx_phone (phone_number),
                INDEX idx_status (status),
                INDEX idx_created (created_at)
            )
        """)
        conn.commit()

        # ── Insert lead ───────────────────────────────────────────────
        cursor.execute("""
            INSERT INTO refinancing_leads
                (phone_number, customer_name, city, intent_type,
                 car_brand, car_model, year_of_manufacture,
                 has_existing_loan, remaining_loan_amt,
                 loan_requirement, contact_preference)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.get("phone"),
            data.get("name"),
            data.get("city"),
            data.get("intent_type"),
            data.get("car_brand"),
            data.get("car_model"),
            data.get("year_of_manufacture"),
            data.get("has_existing_loan"),
            data.get("remaining_loan_amt"),
            data.get("loan_requirement"),
            data.get("contact_preference"),
        ))
        conn.commit()
        lead_id = cursor.lastrowid
        cursor.close(); conn.close()
        print(f"✅ Refinancing lead saved: ID={lead_id}  phone={data.get('phone')}")
        return lead_id

    except Exception as e:
        print(f"❌ save_refinancing_lead error: {e}")
        return None


# ============================================================
# ENTRY POINT  –  called from webhook.py when user picks
# "menu_refinancing" from the main menu list.
# ============================================================

def handle_refinancing(phone: str):
    """Reset state and show the refinancing intent menu."""
    USER_STATE.setdefault(phone, {})
    USER_STATE[phone]["flow"]  = "refinancing"
    USER_STATE[phone]["state"] = "REFINANCING_INTENT"
    # Store the WhatsApp number so later steps can use it
    USER_STATE[phone].setdefault("refinancing", {})["phone"] = phone

    sections = [{
        "title": "Refinancing",
        "rows": [
            {
                "id": "REFI_REDUCE_EMI",
                "title": "📉 Reduce My EMI",
                "description": "Refinance to lower your monthly payments"
            },
            {
                "id": "REFI_LOAN_AGAINST_CAR",
                "title": "💰 Loan Against My Car",
                "description": "Get a loan using your car as collateral"
            },
            {
                "id": "REFI_TRANSFER_LOAN",
                "title": "🔄 Transfer My Loan",
                "description": "Move your existing loan for better rates"
            },
            {
                "id": "REFI_CHECK_ELIGIBILITY",
                "title": "✅ Check Loan Eligibility",
                "description": "Find out if you qualify for a loan"
            },
            {
                "id": "MAIN_MENU",
                "title": "🏠 Main Menu",
                "description": "Return to the main services menu"
            },
            {
                "id": "END_CHAT",
                "title": "❌ End Chat",
                "description": "Exit the conversation"
            },
        ]
    }]

    send_list_message(
        phone,
        "💳 *Car Refinancing & Loan Services*\n\n"
        "We help you get the best financing deal for your car. "
        "What would you like to do?",
        "Choose Option",
        sections
    )


# ============================================================
# AI ROUTER
# ============================================================

def _refinancing_route_via_ai(phone: str, text: str):
    """
    Checks if the user message is a question and routes it to the Refinancing Expert.
    Returns True if handled by AI, False otherwise.
    """
    from utils import is_genuine_query
    
    state = USER_STATE.get(phone, {}).get("state", "")
    
    # Skip routing if it's a known button ID from the intent menu
    if text in ("REFI_REDUCE_EMI", "REFI_LOAN_AGAINST_CAR",
                "REFI_TRANSFER_LOAN", "REFI_CHECK_ELIGIBILITY"):
        return False

    # ── PRIORITY: If it's a genuine question, let the Expert answer ──
    if is_genuine_query(text, state) and text not in ["MAIN_MENU", "END_CHAT", "PREVIOUS_MENU"]:
        try:
            from ai import _handle_refinancing_ai_query
            ai_reply, follow_on = _handle_refinancing_ai_query(phone, text, USER_STATE.get(phone, {}))
            send_whatsapp_message(phone, ai_reply)
            
            if follow_on == "TALK_TO_ADVISOR":
                # Save current state before switching to confirmation
                USER_STATE[phone]["_saved_state"] = state
                from flows.service import bot
                bot.trigger_human_agent_confirmation(phone)
                USER_STATE[phone]["state"] = "REFINANCING_AGENT_CONFIRMATION"
            
            return True
        except Exception as e:
            print(f"[refinancing] AI Expert error: {e}")
    return False


# ============================================================
# MAIN FLOW HANDLER  –  called for every message while the
# user is in the refinancing flow.
# ============================================================

def refinancing_flow_handler(phone: str, text: str):
    """
    Central dispatcher for the refinancing flow.
    `text` is the raw button-ID or free-text from the user.
    """
    USER_STATE.setdefault(phone, {})
    state = USER_STATE[phone].get("state", "")
    data  = USER_STATE[phone].setdefault("refinancing", {})

    # ── AI EXPERT ROUTING ──
    if _refinancing_route_via_ai(phone, text):
        return

    # ── HUMAN AGENT CONFIRMATION ────────────────────────────────────────
    if state == "REFINANCING_AGENT_CONFIRMATION":
        from flows.service import bot
        if text == "AGENT_YES":
            vehicle_reg = USER_STATE.get(phone, {}).get("vehicle_reg") or USER_STATE.get(phone, {}).get("reg_number")
            bot.initiate_agent_handoff(phone, vehicle_reg=vehicle_reg,
                                       handoff_reason="Customer requested live agent from Refinancing flow")
            return
        elif text == "AGENT_NO":
            # Restore the saved state and re-prompt the customer
            prev_state = USER_STATE[phone].pop("_saved_state", None)
            if prev_state and prev_state != "REFINANCING_AGENT_CONFIRMATION":
                USER_STATE[phone]["state"] = prev_state
                send_whatsapp_message(phone, "✅ No problem! Let's continue where we left off.")
                from webhook import _reprompt_flow_state
                _reprompt_flow_state(phone, prev_state)
            else:
                USER_STATE[phone]["state"] = "REFINANCING_INTENT"
                handle_refinancing(phone)
            return
        else:
            # Unknown input — re-show the confirmation prompt
            bot.trigger_human_agent_confirmation(phone)
            USER_STATE[phone]["state"] = "REFINANCING_AGENT_CONFIRMATION"
            return

    # ========================================
    # GLOBAL BUTTON HANDLER: TALK_TO_ADVISOR
    # ========================================
    if text == "TALK_TO_ADVISOR":
        # Save current state before switching to confirmation
        USER_STATE[phone]["_saved_state"] = state
        from flows.service import bot
        bot.trigger_human_agent_confirmation(phone)
        USER_STATE[phone]["state"] = "REFINANCING_AGENT_CONFIRMATION"
        return

    # ── Q1: Intent selection ─────────────────────────────────────────
    if text in ("REFI_REDUCE_EMI", "REFI_LOAN_AGAINST_CAR",
                "REFI_TRANSFER_LOAN", "REFI_CHECK_ELIGIBILITY"):
        intent_label = {
            "REFI_REDUCE_EMI":         "Reduce EMI by refinancing",
            "REFI_LOAN_AGAINST_CAR":   "Loan against car",
            "REFI_TRANSFER_LOAN":      "Transfer My Loan",
            "REFI_CHECK_ELIGIBILITY":  "Check Loan Eligibility",
        }[text]
        data["intent_type"] = intent_label
        data["phone"]       = phone          # auto-fetched from WhatsApp sender

        # Try to pre-fill name from DB
        known_name = _get_customer_name_from_db(phone)
        if known_name:
            data["name"] = known_name
            USER_STATE[phone]["state"] = "REFINANCING_ASK_CITY"
            send_whatsapp_message(
                phone,
                f"👋 Welcome back, *{known_name}*!\n\n"
                f"📍 Which city are you currently located in?"
            )
        else:
            USER_STATE[phone]["state"] = "REFINANCING_ASK_NAME"
            send_whatsapp_message(
                phone,
                f"Great choice! Let's get started with your *{intent_label}* request.\n\n"
                "👤 Please share your *full name*:"
            )
        return

    # ── Q2a: Collect name (only if not pre-filled) ───────────────────
    if state == "REFINANCING_ASK_NAME":
        if not text or len(text.strip()) < 2:
            send_whatsapp_message(phone, "⚠️ Please enter a valid name.")
            return
        data["name"] = text.strip().title()
        USER_STATE[phone]["state"] = "REFINANCING_ASK_CITY"
        send_whatsapp_message(
            phone,
            f"Thanks, *{data['name']}*! 😊\n\n"
            "📍 Which city are you located in?"
        )
        return

    # ── Q2b / Q3: Collect city ───────────────────────────────────────
    if state == "REFINANCING_ASK_CITY":
        if not text or len(text.strip()) < 2:
            send_whatsapp_message(phone, "⚠️ Please enter a valid city name.")
            return
        data["city"] = text.strip().title()
        USER_STATE[phone]["state"] = "REFINANCING_ASK_BRAND"
        _send_car_brand_buttons(phone)
        return

    # ── Q4: Car brand ────────────────────────────────────────────────
    if state == "REFINANCING_ASK_BRAND":
        if text in ("REFI_BRAND_MARUTI", "REFI_BRAND_HYUNDAI", "REFI_BRAND_TATA",
                    "REFI_BRAND_MAHINDRA", "REFI_BRAND_HONDA",
                    "REFI_BRAND_TOYOTA", "REFI_BRAND_OTHER"):
            if text == "REFI_BRAND_OTHER":
                # Ask user to manually type their brand name
                USER_STATE[phone]["state"] = "REFINANCING_ASK_BRAND_OTHER"
                send_whatsapp_message(
                    phone,
                    "✏️ Please *type the brand name* of your car:"
                )
            else:
                brand_map = {
                    "REFI_BRAND_MARUTI":   "Maruti Suzuki",
                    "REFI_BRAND_HYUNDAI":  "Hyundai",
                    "REFI_BRAND_TATA":     "Tata",
                    "REFI_BRAND_MAHINDRA": "Mahindra",
                    "REFI_BRAND_HONDA":    "Honda",
                    "REFI_BRAND_TOYOTA":   "Toyota",
                }
                data["car_brand"] = brand_map[text]
                USER_STATE[phone]["state"] = "REFINANCING_ASK_MODEL"
                _send_car_model_buttons(phone)
        else:
            send_whatsapp_message(phone, "⚠️ Please select your car brand from the list.")
            _send_car_brand_buttons(phone)
        return

    # ── Q4b: Other brand – manual text input ─────────────────────────
    if state == "REFINANCING_ASK_BRAND_OTHER":
        if not text or len(text.strip()) < 2:
            send_whatsapp_message(phone, "⚠️ Please enter a valid brand name.")
            return
        data["car_brand"] = text.strip().title()
        USER_STATE[phone]["state"] = "REFINANCING_ASK_MODEL"
        _send_car_model_buttons(phone)
        return

    # ── Q5: Car model ────────────────────────────────────────────────
    if state == "REFINANCING_ASK_MODEL":
        if text in ("REFI_MODEL_SWIFT", "REFI_MODEL_CRETA",
                    "REFI_MODEL_NEXON", "REFI_MODEL_CITY", "REFI_MODEL_OTHER"):
            model_map = {
                "REFI_MODEL_SWIFT":  "Swift",
                "REFI_MODEL_CRETA":  "Creta",
                "REFI_MODEL_NEXON":  "Nexon",
                "REFI_MODEL_CITY":   "City",
                "REFI_MODEL_OTHER":  "Other",
            }
            data["car_model"] = model_map[text]
            USER_STATE[phone]["state"] = "REFINANCING_ASK_YEAR"
            _send_year_buttons(phone)
        else:
            send_whatsapp_message(phone, "⚠️ Please select your car model from the list.")
            _send_car_model_buttons(phone)
        return

    # ── Q6: Year of manufacture ──────────────────────────────────────
    if state == "REFINANCING_ASK_YEAR":
        if text in ("REFI_YEAR_2023", "REFI_YEAR_2022", "REFI_YEAR_2021",
                    "REFI_YEAR_2020", "REFI_YEAR_2019", "REFI_YEAR_OLDER"):
            year_map = {
                "REFI_YEAR_2023":  "2023",
                "REFI_YEAR_2022":  "2022",
                "REFI_YEAR_2021":  "2021",
                "REFI_YEAR_2020":  "2020",
                "REFI_YEAR_2019":  "2019",
                "REFI_YEAR_OLDER": "Older (pre-2019)",
            }
            data["year_of_manufacture"] = year_map[text]
            USER_STATE[phone]["state"] = "REFINANCING_ASK_HAS_LOAN"
            send_button_message(
                phone,
                "🏦 *Existing Loan Information*\n\n"
                " Do you currently have a car loan?",
                [
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_YES", "title": "✅ Yes"}},
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_NO",  "title": "❌ No"}},
                ]
            )
        else:
            send_whatsapp_message(phone, "⚠️ Please select the year from the list.")
            _send_year_buttons(phone)
        return

    # ── Q7: Has existing loan? ───────────────────────────────────────
    if state == "REFINANCING_ASK_HAS_LOAN":
        if text == "REFI_HAS_LOAN_YES":
            data["has_existing_loan"] = "Yes"
            USER_STATE[phone]["state"] = "REFINANCING_ASK_REMAINING_AMT"
            sections = [{
                "title": "Remaining Amount",
                "rows": [
                    {"id": "REFI_REM_BELOW2L",  "title": "Below ₹2 Lakh",  "description": ""},
                    {"id": "REFI_REM_2TO5L",    "title": "₹2–5 Lakh",      "description": ""},
                    {"id": "REFI_REM_5TO8L",    "title": "₹5–8 Lakh",      "description": ""},
                    {"id": "REFI_REM_ABOVE8L",  "title": "Above ₹8 Lakh",  "description": ""},
                ]
            }]
            send_list_message(
                phone,
                "💰 What is the *remaining loan amount*?",
                "Select Range",
                sections
            )
        elif text == "REFI_HAS_LOAN_NO":
            data["has_existing_loan"] = "No"
            USER_STATE[phone]["state"] = "REFINANCING_ASK_LOAN_NEEDED"
            _send_loan_requirement_buttons(phone)
        else:
            send_whatsapp_message(phone, "⚠️ Please select Yes or No.")
            send_button_message(
                phone,
                " Do you currently have a car loan?",
                [
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_YES", "title": "✅ Yes"}},
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_NO",  "title": "❌ No"}},
                ]
            )
        return

    # ── Q8: Remaining loan amount (only if has loan = Yes) ───────────
    if state == "REFINANCING_ASK_REMAINING_AMT":
        amt_map = {
            "REFI_REM_BELOW2L": "Below ₹2 Lakh",
            "REFI_REM_2TO5L":   "₹2–5 Lakh",
            "REFI_REM_5TO8L":   "₹5–8 Lakh",
            "REFI_REM_ABOVE8L": "Above ₹8 Lakh",
        }
        if text in amt_map:
            data["remaining_loan_amt"] = amt_map[text]
            USER_STATE[phone]["state"] = "REFINANCING_ASK_CONTACT_TIME"
            _send_contact_preference(phone)
        else:
            send_whatsapp_message(phone, "⚠️ Please select from the options.")
        return

    # ── Q9: Loan requirement (only if has loan = No) ─────────────────
    if state == "REFINANCING_ASK_LOAN_NEEDED":
        req_map = {
            "REFI_LOAN_1TO2L":   "₹1–2 Lakh",
            "REFI_LOAN_2TO5L":   "₹2–5 Lakh",
            "REFI_LOAN_5TO10L":  "₹5–10 Lakh",
            "REFI_LOAN_ABOVE10": "Above ₹10 Lakh",
        }
        if text in req_map:
            data["loan_requirement"] = req_map[text]
            USER_STATE[phone]["state"] = "REFINANCING_ASK_CONTACT_TIME"
            _send_contact_preference(phone)
        else:
            send_whatsapp_message(phone, "⚠️ Please select from the options.")
            _send_loan_requirement_buttons(phone)
        return

    # ── Q10: Contact preference ──────────────────────────────────────
    if state == "REFINANCING_ASK_CONTACT_TIME":
        pref_map = {
            "REFI_CONTACT_30MIN":   "Within 30 minutes",
            "REFI_CONTACT_TODAY":   "Today",
            "REFI_CONTACT_TOMORROW":"Tomorrow",
        }
        if text in pref_map:
            data["contact_preference"] = pref_map[text]
            # ── Save lead to DB ───────────────────────────────────────
            lead_id = save_refinancing_lead(data)
            # ── Reset flow state ──────────────────────────────────────
            USER_STATE[phone]["flow"]  = None
            USER_STATE[phone]["state"] = "MENU"
            USER_STATE[phone].pop("refinancing", None)

            # ── Format mobile number for display ─────────────────────
            raw_phone = data.get("phone", phone)
            mobile_10 = str(raw_phone)[-10:]
            display_mobile = f"+91-{mobile_10}"

            # ── Final confirmation message (with mobile number) ───────
            send_whatsapp_message(
                phone,
                "🎉 *Thank you!*\n\n"
                "Our finance team will check the refinancing options for your car "
                "and contact you shortly.\n\n"
                f"📋 *Your Request Summary:*\n"
                f"👤 Name    : {data.get('name', 'N/A')}\n"
                f"📱 Mobile  : {display_mobile}\n"
                f"📍 City    : {data.get('city', 'N/A')}\n"
                f"🎯 Service : {data.get('intent_type', 'N/A')}\n"
                f"🚗 Car     : {data.get('car_brand', '')} {data.get('car_model', '')} "
                f"({data.get('year_of_manufacture', '')})\n"
                f"📞 Call me : {data.get('contact_preference', 'N/A')}\n\n"
                "If you have any questions in the meantime, feel free to ask! 😊"
            )

            # ── Show main menu again ──────────────────────────────────
            sections = [{
                "title": "Main Menu",
                "rows": [
                    {"id": "menu_new_cars",     "title": "🔹 New Cars – Sales",       "description": "Browse new car models & prices"},
                    {"id": "menu_used_cars",    "title": "🚗 Used Cars",              "description": "Browse, value or test drive pre-owned cars"},
                    {"id": "menu_service",      "title": "🔧 Vehicle Service",        "description": "Book a service appointment"},
                    {"id": "menu_insurance",    "title": "🔰 Vehicle Insurance",      "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",            "description": "Reduce EMI or get a loan against your car"},
                    {"id": "menu_about",        "title": "ℹ️ About Us",              "description": "Learn more about AutoSherpa"},
                    {"id": "menu_contact",      "title": "📞 Contact & Support",      "description": "Get in touch with our team"},
                    {"id": "END_CHAT",          "title": "❌ End Chat",               "description": "Exit the conversation"},
                ]
            }]
            send_list_message(phone, "Is there anything else I can help you with?", "View Options", sections)
        else:
            send_whatsapp_message(phone, "⚠️ Please select a contact preference.")
            _send_contact_preference(phone)
        return

    # ── Fallback: user sent something unexpected ─────────────────────────
    # If it's a genuine question, answer via AI. Otherwise re-show
    # the relevant step so the user is never stuck or confused.
    from utils import is_genuine_query
    if is_genuine_query(text, state):
        try:
            from ai import handle_general_query_in_flow
            ai_reply, follow_on = handle_general_query_in_flow(phone, text, flow_type="refinancing")
            from webhook import send_whatsapp_message as _send
            _send(phone, ai_reply)
            
            if follow_on == "TALK_TO_ADVISOR":
                refinancing_flow_handler(phone, "TALK_TO_ADVISOR")
        except Exception as _e:
            print(f"[refinancing] AI query error: {_e}")
            handle_refinancing(phone)
    else:
        print(f"[refinancing] blocked non-query fallback | state={state} | text='{text}'")
        # Re-prompt based on what step we're on
        if state == "REFINANCING_ASK_NAME":
            send_whatsapp_message(phone, "⚠️ Please enter your full name to continue.")
        elif state == "REFINANCING_ASK_CITY":
            send_whatsapp_message(phone, "⚠️ Please enter your city name to continue.")
        elif state == "REFINANCING_ASK_BRAND":
            _send_car_brand_buttons(phone)
        elif state == "REFINANCING_ASK_BRAND_OTHER":
            send_whatsapp_message(phone, "⚠️ Please type your car brand name.")
        elif state == "REFINANCING_ASK_MODEL":
            _send_car_model_buttons(phone)
        elif state == "REFINANCING_ASK_YEAR":
            _send_year_buttons(phone)
        elif state == "REFINANCING_ASK_HAS_LOAN":
            send_button_message(
                phone,
                "Do you currently have a car loan?",
                [
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_YES", "title": "✅ Yes"}},
                    {"type": "reply", "reply": {"id": "REFI_HAS_LOAN_NO",  "title": "❌ No"}},
                ]
            )
        elif state == "REFINANCING_ASK_REMAINING_AMT":
            send_whatsapp_message(phone, "⚠️ Please select the remaining loan amount from the options.")
        elif state == "REFINANCING_ASK_LOAN_NEEDED":
            _send_loan_requirement_buttons(phone)
        elif state == "REFINANCING_ASK_CONTACT_TIME":
            _send_contact_preference(phone)
        else:
            handle_refinancing(phone)


# ============================================================
# HELPER SENDERS
# ============================================================

def _send_car_brand_buttons(phone: str):
    sections = [{
        "title": "Car Brand",
        "rows": [
            {"id": "REFI_BRAND_MARUTI",   "title": "🚗 Maruti Suzuki", "description": ""},
            {"id": "REFI_BRAND_HYUNDAI",  "title": "🚙 Hyundai",       "description": ""},
            {"id": "REFI_BRAND_TATA",     "title": "🚘 Tata",          "description": ""},
            {"id": "REFI_BRAND_MAHINDRA", "title": "🛻 Mahindra",      "description": ""},
            {"id": "REFI_BRAND_HONDA",    "title": "🚗 Honda",         "description": ""},
            {"id": "REFI_BRAND_TOYOTA",   "title": "🚙 Toyota",        "description": ""},
            {"id": "REFI_BRAND_OTHER",    "title": "🔹 Other",         "description": ""},
        ]
    }]
    send_list_message(
        phone,
        "🚗 *Car Details*\n\n What is the *brand* of your car?",
        "Select Brand",
        sections
    )


def _send_car_model_buttons(phone: str):
    sections = [{
        "title": "Car Model",
        "rows": [
            {"id": "REFI_MODEL_SWIFT", "title": "Swift",  "description": ""},
            {"id": "REFI_MODEL_CRETA", "title": "Creta",  "description": ""},
            {"id": "REFI_MODEL_NEXON", "title": "Nexon",  "description": ""},
            {"id": "REFI_MODEL_CITY",  "title": "City",   "description": ""},
            {"id": "REFI_MODEL_OTHER", "title": "Other",  "description": ""},
        ]
    }]
    send_list_message(
        phone,
        " What is the *model* of your car?",
        "Select Model",
        sections
    )


def _send_year_buttons(phone: str):
    sections = [{
        "title": "Year of Manufacture",
        "rows": [
            {"id": "REFI_YEAR_2023",  "title": "2023",        "description": ""},
            {"id": "REFI_YEAR_2022",  "title": "2022",        "description": ""},
            {"id": "REFI_YEAR_2021",  "title": "2021",        "description": ""},
            {"id": "REFI_YEAR_2020",  "title": "2020",        "description": ""},
            {"id": "REFI_YEAR_2019",  "title": "2019",        "description": ""},
            {"id": "REFI_YEAR_OLDER", "title": "Older",       "description": "Before 2019"},
        ]
    }]
    send_list_message(
        phone,
        " What is the *year of manufacture*?",
        "Select Year",
        sections
    )


def _send_loan_requirement_buttons(phone: str):
    sections = [{
        "title": "Loan Amount",
        "rows": [
            {"id": "REFI_LOAN_1TO2L",   "title": "₹1–2 Lakh",    "description": ""},
            {"id": "REFI_LOAN_2TO5L",   "title": "₹2–5 Lakh",    "description": ""},
            {"id": "REFI_LOAN_5TO10L",  "title": "₹5–10 Lakh",   "description": ""},
            {"id": "REFI_LOAN_ABOVE10", "title": "Above ₹10 Lakh","description": ""},
        ]
    }]
    send_list_message(
        phone,
        "💵 *Loan Requirement*\n\n"
        " How much loan amount are you looking for?",
        "Select Amount",
        sections
    )


def _send_contact_preference(phone: str):
    send_button_message(
        phone,
        "📅 *Lead Conversion*\n\n"
        "When would you like our finance advisor to contact you?",
        [
            {"type": "reply", "reply": {"id": "REFI_CONTACT_30MIN",    "title": "⚡ Within 30 mins"}},
            {"type": "reply", "reply": {"id": "REFI_CONTACT_TODAY",    "title": "📅 Today"}},
            {"type": "reply", "reply": {"id": "REFI_CONTACT_TOMORROW", "title": "🗓 Tomorrow"}},
        ]
    )