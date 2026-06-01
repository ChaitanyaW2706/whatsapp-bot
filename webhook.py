import requests
import time
import traceback
import json
import os
from config import ACCESS_TOKEN, PHONE_NUMBER_ID, USER_STATE, DB_CONFIG
from templates.messages import welcome_message, invalid_entry_message
from flows.used_cars import used_cars_flow_handler
from chat_history import start_chat_session, add_message, close_chat_session
from ai import store_interaction as _log_conv
from flows.service import router as service_router
from fastapi import APIRouter, FastAPI, Request
from db import get_db

app = FastAPI()
app.include_router(service_router)

# ══════════════════════════════════════════════════════════════════
# CENTRAL AI FREE-TEXT RESOLVER
#
# When a user types free text instead of clicking a button,
# this function uses Groq AI to understand their intent and
# convert it to the correct button-ID that the existing flow
# handlers already know how to process.
#
# RULES:
#  • Only called for message_type == "text" (not button/list replies)
#  • Returns the resolved button-ID string, or None if unresolvable
#  • No flow files are touched — all existing handlers stay intact
#  • Each flow has its own intent map matching its actual button IDs
# ══════════════════════════════════════════════════════════════════

from llm_config import groq_client as _groq_client, MODEL_NAME as _GROQ_MODEL


# ── Per-flow intent maps: what button IDs each flow understands ──

_INSURANCE_INTENTS = """
AVAILABLE ACTIONS (return ONLY the id from this list, or "GENERAL_QUERY"):
- "INSURANCE_HISTORY"   : user wants to see past policies / insurance history
- "GET_ESTIMATE"        : user wants an insurance estimate / premium / quote / cost
- "RENEW_INSURANCE"     : user wants to renew or buy insurance
- "TALK_TO_ADVISOR"     : user wants to speak with an advisor / agent / human
- "END_CHAT"            : user wants to end / bye / stop / exit
- "PREVIOUS_MENU"       : user wants to go back / main menu / home
- "GENERAL_QUERY"       : ANY question, query, or request for information (e.g., "what is NCB?", "how is premium calculated?"). ALWAYS return this for questions.

DATE MAPPING (appointment scheduling context):
- "today" / "aaj"                     → "today_appointment"
- "tomorrow" / "kal" / "next day"     → "tomorrow_appointment"
- "this week" / "later this week"     → "this_week_appointment"
- "next week"                         → "next_week_appointment"
"""

_SALES_INTENTS = """
AVAILABLE ACTIONS (return ONLY the id from this list, or "GENERAL_QUERY"):
- "NEW_CARS"            : user wants to browse new cars / models / see cars
- "USED_CARS"           : user wants used/second-hand/pre-owned cars
- "TALK_TO_ADVISOR"     : user wants to speak with an advisor / agent / human
- "LOCATIONS"           : user wants showroom address / location / directions
- "FINANCE_OPTIONS"     : user wants EMI / loan / finance / down payment info
- "BOOK_TEST_DRIVE"     : user wants to book a test drive / trial drive
- "BACK_TO_HYUNDAI_LIST": user wants to go back to car list / see other cars
- "PREVIOUS_MENU"       : user wants previous menu / go back
- "MAIN_MENU"           : user wants main menu / home / restart
- "END_CHAT"            : user wants to end / bye / exit
- "GENERAL_QUERY"       : ANY question, query, or request for information (e.g., "what is the down payment?", "how much is the mileage?", "what documents are needed?"). ALWAYS return this for questions.

DATE MAPPING (test drive / appointment scheduling context):
- "today" / "aaj"                     → "today_appointment"
- "tomorrow" / "kal" / "next day"     → "tomorrow_appointment"
- "this week" / "later this week"     → "this_week_appointment"
- "next week"                         → "next_week_appointment"
"""

_USED_CARS_INTENTS = """
AVAILABLE ACTIONS (return ONLY the id from this list, or "GENERAL_QUERY"):
- "BROWSE_USED_CARS"    : user wants to browse / see used cars
- "GET_VALUATION"       : user wants to sell car / get valuation / price for car
- "BOOK_TEST_DRIVE"     : user wants to book a test drive for a used car
- "SPECIFIC_CAR"        : user asks about a specific car model (e.g. "i20 used")
- "CHANGE_CRITERIA"     : user wants to change search / different budget / different model
- "TALK_TO_ADVISOR"     : user wants to speak with an advisor / agent / human
- "CONTACT_US"          : user wants to contact the team
- "ABOUT_US"            : user wants info about the company
- "MAIN_MENU"           : user wants main menu / home
- "END_CONVERSATION"    : user wants to end / bye / exit
- "GENERAL_QUERY"       : general question — let existing AI handle it

DATE MAPPING (test drive / appointment scheduling context):
- "today" / "aaj"                     → "today_appointment"
- "tomorrow" / "kal" / "next day"     → "tomorrow_appointment"
- "this week" / "later this week"     → "this_week_appointment"
- "next week"                         → "next_week_appointment"
"""

_SERVICE_INTENTS = """
AVAILABLE ACTIONS (return ONLY the id from this list, or "GENERAL_QUERY"):
- "book_service"        : user wants to book a service appointment
- "service_history"     : user wants to see service history / past service
- "get_estimate"        : user wants a service cost estimate / price / how much
- "contact_advisor"     : user wants to talk to service advisor / agent / human / support
- "view_offers"         : user wants offers / discounts / deals
- "end_conversation"    : user wants to end / bye / exit
- "PREVIOUS_MENU"       : user wants to go back / main menu
- "GENERAL_QUERY"       : general vehicle service question — let existing AI handle it

DATE MAPPING (appointment scheduling context):
- "today" / "aaj"                     → "today_appointment"
- "tomorrow" / "kal" / "next day"     → "tomorrow_appointment"
- "this week" / "later this week"     → "this_week_appointment"
- "next week"                         → "next_week_appointment"
- "this week" / "later this week"     → "this_week_appointment"
- "next week"                         → "next_week_appointment"
"""

_REFINANCING_INTENTS = """
AVAILABLE ACTIONS (return ONLY the id from this list, or "GENERAL_QUERY"):
- "REFI_REDUCE_EMI"         : user wants to reduce EMI / lower monthly payments
- "REFI_LOAN_AGAINST_CAR"   : user wants a loan against their car
- "REFI_TRANSFER_LOAN"      : user wants to transfer existing loan
- "REFI_CHECK_ELIGIBILITY"  : user wants to check loan eligibility
- "TALK_TO_ADVISOR"         : user wants to speak with an advisor / agent / human
- "PREVIOUS_MENU"           : user wants to go back / main menu / home
- "END_CONVERSATION"        : user wants to end / bye / exit
- "GENERAL_QUERY"           : general car loan question — let existing AI handle it
"""


def _resolve_menu_free_text(text: str) -> str | None:
    """
    Called when user is in MENU or START state and types free text
    instead of clicking a menu option.

    Maps natural language to one of:
      menu_new_cars | menu_used_cars | menu_service | menu_insurance
      menu_about    | menu_contact

    Returns None if no confident match found.
    """
    try:
        prompt = f"""You are an intent classifier for AutoSherpa, an automotive WhatsApp chatbot.
The user is on the MAIN MENU and typed a free-text message instead of clicking a button.

AVAILABLE MENU OPTIONS (return ONLY the id, nothing else):
- "menu_insurance"    : user wants insurance / renew policy / insurance estimate / vehicle insurance
- "menu_new_cars"     : user wants new cars / buy car / new Hyundai / car prices / test drive new car
- "menu_used_cars"    : user wants used cars / second hand car / pre-owned car / old car
- "menu_service"      : user wants vehicle service / car repair / service booking / maintenance
- "menu_refinancing"  : user wants refinancing / reduce EMI / car loan / loan against car / transfer loan / loan eligibility / finance loan
- "menu_about"        : user wants info about the company / about AutoSherpa / who are you
- "menu_contact"      : user wants contact details / phone number / address / support
- "NONE"              : message does not match any menu option clearly

USER MESSAGE: "{text}"

Return ONLY a JSON object:
  {{"menu_id": "one of the ids above", "confidence": 0.0-1.0}}

Examples:
  "i want to do insurance"     → {{"menu_id": "menu_insurance", "confidence": 0.97}}
  "insurance renewal"          → {{"menu_id": "menu_insurance", "confidence": 0.98}}
  "show me new cars"           → {{"menu_id": "menu_new_cars",  "confidence": 0.97}}
  "second hand car"            → {{"menu_id": "menu_used_cars", "confidence": 0.96}}
  "book service"               → {{"menu_id": "menu_service",      "confidence": 0.97}}
  "reduce my emi"              → {{"menu_id": "menu_refinancing",  "confidence": 0.97}}
  "car loan refinance"         → {{"menu_id": "menu_refinancing",  "confidence": 0.98}}
  "policy renew pananum"       → {{"menu_id": "menu_insurance",    "confidence": 0.95}}
  "car service panna porom"    → {{"menu_id": "menu_service",   "confidence": 0.95}}
  "naan insurance vennum"      → {{"menu_id": "menu_insurance", "confidence": 0.95}}

Return ONLY the JSON, no extra text."""

        response = _groq_client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=60,
            response_format={"type": "json_object"}
        )

        result     = json.loads(response.choices[0].message.content.strip())
        menu_id    = result.get("menu_id", "NONE")
        confidence = float(result.get("confidence", 0.0))

        print(f"🤖 Menu AI | text='{text}' → menu_id='{menu_id}' ({confidence:.2f})")

        if confidence >= 0.70 and menu_id != "NONE":
            return menu_id
        return None

    except Exception as e:
        print(f"❌ _resolve_menu_free_text error: {e}")
        return None


def _call_groq_json(prompt: str, max_tokens: int = 80) -> dict:
    """Shared helper — call Groq and return parsed JSON dict."""
    response = _groq_client.chat.completions.create(
        model=_GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content.strip())


def _resolve_free_text_as_button(phone: str, text: str, state: str) -> str | None:
    """
    Converts a user's free-text message into the correct button/action ID.
    """
    from utils import is_genuine_query
    
    # ── PRIORITY: If it's a genuine query/question, return GENERAL_QUERY ──
    # This ensures that "What is the price?" is never converted to a button click
    # and is always handled by the expert AI instead.
    if is_genuine_query(text, state):
        print(f"❓ Genuine query detected in mid-flow resolution → GENERAL_QUERY | '{text}'")
        return "GENERAL_QUERY"

    try:
        # ══════════════════════════════════════════════════════
        # EXCLUDE FREE-TEXT INPUT STATES
        # Skip intent resolution and immediately return None if the user
        # is in a state that explicitly expects free-text like name, city, address, etc.
        # This ensures the flow handles structured input (reg numbers, names, cities, etc.)
        # without AI interference.
        # ══════════════════════════════════════════════════════
        skip_substrs = [
            # ── Generic patterns ──────────────────────────────────────────────
            "_COLLECT_",
            "ASK_NAME",
            "ASK_CITY",
            "ASK_PHONE",
            "ASK_BRAND",
            "ASK_MODEL",
            "ASK_ADDRESS",
            "COLLECT_",
            "LOOKUP",           # covers INSURANCE_LOOKUP (reg no input)
            "NOT_FOUND",        # covers INSURANCE_NOT_FOUND

            # ── Specific structured input states ──────────────────────────────
            "ENTER_BRAND",
            "ENTER_MODEL",
            "BRAND_OTHER",
            "SELECT_BRAND",
            "SELECT_MODEL",
            "SELECT_TYPE",
            "SELECT_BUDGET",
            "FINAL_CONFIRMATION",   # SALES_FINAL_CONFIRMATION

            # ── Exchange / Trade-in states (all expect free-text input) ───────
            "EXCH_BRAND",
            "EXCH_MODEL",
            "EXCH_YEAR",
            "EXCH_ENTER",
            "EXCH_COLLECT",
            "EXCH_TD_ADDRESS",
            "EXCH_TD_LOCATION",     # address free-text
            "EXCH_COLLECT_CITY",    # city name free-text
            "EXCH_KM",              # odometer reading — numeric free text
            "EXCH_FUEL",
            "EXCH_TRANS",
            "EXCH_OWNERS",
            "EXCH_CONDITION",
            "EXCH_INSURANCE",
            "EXCH_DOCS",

            # ── Appointment / form collection patterns ────────────────────────
            "VALUATION_",
            "LINK_SENT",
            "CONFIRM",
            "APPOINTMENT",
            # NOTE: Do NOT add bare substrings like "DATE", "WEEK", "TIME", "MODE", "TYPE" here.
            # They would accidentally match insurance renewal sub-states
            # (STATE_4_RENEW_TYPE, STATE_4_MODE, STATE_4_DATE, STATE_4_LATER_WEEK,
            #  STATE_4_NEXT_WEEK, STATE_4_SLOT) which have their own AI resolvers
            # at lines 376-426 and must NOT be short-circuited here.
            # Use precise full-state names instead (see INSURANCE FLOW section below).

            # ── SERVICE FLOW — comprehensive coverage ─────────────────────────
            "waiting_",         # waiting_for_registration, waiting_full_name, waiting_phone,
                                # waiting_address, waiting_city, waiting_model, waiting_odo,
                                # waiting_fuel, waiting_service
            "vehicle_",         # vehicle_found, vehicle_not_found, vehicle_info_confirmation
            "booking_",         # booking_priority, booking_slots, booking_preferences
            "callback",         # callback_priority, callback_slots
            "video_",           # video_call_priority, video_call_slots
            "contact_",         # contact_method_selection
            "discount",         # discounts_book_service, discounts_followup_request
            "estimate_",        # all estimate states
            "other_service",    # other service type selections
            "SERVICE_START",    # initial service state before waiting_for_registration is set

            # ── SALES FLOW — form input states ────────────────────────────────
            "SALES_COLLECT_NAME",
            "SALES_COLLECT_PHONE",
            "SALES_COLLECT_ADDRESS",
            "SALES_COLLECT_LICENSE",    # driving licence number — free text

            # ── USED CARS FLOW — form input states ────────────────────────────
            "USED_CARS_COLLECT_NAME",
            "USED_CARS_COLLECT_PHONE",
            "USED_CARS_COLLECT_ADDRESS",
            "USED_CALLBACK_COLLECT_NAME",
            "USED_CALLBACK_COLLECT_PHONE",
            "USED_CALLBACK_COLLECT_NEED",       # query description — free text
            "USED_VALUATION_COLLECT_NAME",
            "USED_VALUATION_COLLECT_PHONE",
            "USED_VALUATION_COLLECT_LOCATION",  # location free text
            "USED_VALUATION_BRAND",             # brand name typed by user
            "USED_VALUATION_ENTER_BRAND",
            "USED_VALUATION_MODEL",             # model name typed by user
            "USED_VALUATION_ENTER_MODEL",
            "USED_VALUATION_YEAR",              # year — numeric free text
            "USED_BOOK_COLLECT_NAME",
            "USED_BOOK_COLLECT_PHONE",
            "USED_BOOK_COLLECT_LICENSE",
            "USED_BOOK_COLLECT_LOCATION",
            "EXCH_COLLECT_NAME",
            "EXCH_COLLECT_CITY",

            # ── REFINANCING FLOW — form input states ──────────────────────────
            "REFINANCING_ASK_NAME",
            "REFINANCING_ASK_PHONE",
            "REFINANCING_ASK_CITY",
            "REFINANCING_ASK_BRAND_OTHER",
            "REFINANCING_ASK_MODEL",
            "REFINANCING_ASK_YEAR",
            "REFINANCING_ASK_HAS_LOAN",
            "REFINANCING_ASK_REMAINING_AMT",
            "REFINANCING_ASK_LOAN_NEEDED",
            "REFINANCING_ASK_CONTACT_TIME",

            # ── INSURANCE FLOW — form input states ────────────────────────────
            "INSURANCE_ESTIMATE_NAME",
            "INSURANCE_ESTIMATE_LINK_SENT",
            "INSURANCE_START",      # initial insurance state (before lookup)
            "STATE_4_NAME",
            "STATE_4_ADDRESS",
            "STATE_4_SLOT",         # time-slot selection — structured pick

            # ── Agent confirmation states ─────────────────────────────────────
            "AGENT_CONFIRMATION",
            "_AGENT_CONFIRMATION",
            "SERVICE_AGENT_CONFIRMATION",   # service-specific agent confirm state

            # ── Mid-flow exit confirmation ────────────────────────────────────
            "AWAITING_EXIT_CONFIRM",        # handled before AI resolver — never AI-route
        ]
        if any(sub in state for sub in skip_substrs):
            return None

        # ══════════════════════════════════════════════════════
        # STATES WITH INLINE KEYWORD RESOLVERS IN FLOW HANDLERS
        # Return None so the flow's own keyword logic handles it
        # instead of AI producing a GENERAL_QUERY text response.
        # ══════════════════════════════════════════════════════
        flow_handled_states = [
            "SALES_SELECT_CAR_TYPE",   # car type keywords (electric/suv/sedan/hatchback)
            "USED_SELECT_TYPE",        # used-car type keywords
            "USED_SELECT_BRAND",       # used-car brand keywords
            "USED_SELECT_BUDGET",      # budget range free-text
        ]
        if state in flow_handled_states:
            return None

        # ══════════════════════════════════════════════════════
        # STATE-SPECIFIC HANDLERS  (insurance sub-states)
        # Checked FIRST — return exact IDs the flow handler needs.
        # ══════════════════════════════════════════════════════

        # ── Renewal Type ─────────────────────────────────────
        if state == "STATE_4_RENEW_TYPE":
            prompt = f"""Map the user message to one renewal type ID.

- "RENEW_FIRST"     → first renewal / 1st renewal / first time renewing / first one
- "RENEW_2ND"       → 2nd / second renewal
- "RENEW_3RD"       → 3rd / third renewal
- "RENEW_4TH"       → 4th / fourth renewal
- "RENEW_5TH"       → 5th / fifth renewal
- "RENEW_6TH_ABOVE" → 6th or above / 7th / 8th / many renewals
- "RENEW_NEW"       → new policy / first time buying / no existing policy / fresh policy

USER MESSAGE: "{text}"
Return ONLY JSON: {{"id": "<ID>", "confidence": 0.0-1.0}}
Unknown → {{"id": "NONE", "confidence": 0.0}}"""
            r = _call_groq_json(prompt, 60)
            rid, rconf = r.get("id", "NONE"), float(r.get("confidence", 0.0))
            print(f"🤖 RENEW_TYPE | '{text}' → '{rid}' ({rconf:.2f})")
            return rid if rconf >= 0.70 and rid != "NONE" else "GENERAL_QUERY"

        # ── Appointment Mode (Insurance, Sales, Used Cars) ───────────────────
        if state in ["STATE_4_MODE", "SALES_SELECT_APPOINTMENT_TYPE", "EXCH_TD_LOCATION"]:
            # Default to Insurance IDs
            online_id = "MODE_ONLINE"; walkin_id = "MODE_WALKIN"; field_id = "MODE_FIELD"
            
            if state == "SALES_SELECT_APPOINTMENT_TYPE":
                online_id = "APPT_SHOWROOM"; walkin_id = "APPT_SHOWROOM"; field_id = "APPT_HOME_VISIT"
            elif state == "EXCH_TD_LOCATION":
                online_id = "EXCH_LOC_SHOWROOM"; walkin_id = "EXCH_LOC_SHOWROOM"; field_id = "EXCH_LOC_HOME"

            # Quick keyword bypass to prevent LLM misclassification
            t_lower = text.lower()
            if any(k in t_lower for k in ["online", "virtual", "call", "video", "phone", "remote", "digital"]):
                return online_id
            if any(k in t_lower for k in ["walk", "in person", "visit", "showroom", "office", "offline", "come"]):
                return walkin_id
            if any(k in t_lower for k in ["field", "home", "house", "location", "doorstep", "my place"]):
                return field_id
                
            prompt = f"""Map the user message to one appointment mode ID.

- "{online_id}"  → online / virtual / call / video / phone / remote / digital
- "{walkin_id}"  → walk-in / in person / visit showroom / come to office / offline
- "{field_id}"   → field visit / home visit / come to my house / at my location / doorstep

USER MESSAGE: "{text}"
Return ONLY JSON: {{"id": "<ID>", "confidence": 0.0-1.0}}
Unknown → {{"id": "NONE", "confidence": 0.0}}"""
            r = _call_groq_json(prompt, 60)
            rid, rconf = r.get("id", "NONE"), float(r.get("confidence", 0.0))
            print(f"🤖 APPT_MODE | '{text}' → '{rid}' ({rconf:.2f})")
            return rid if rconf >= 0.70 and rid != "NONE" else "GENERAL_QUERY"

        # ── Appointment Date (Insurance, Sales, Used Cars) ───────────────────
        if state in ["STATE_4_DATE", "SALES_SELECT_WHEN", "EXCH_TD_WHEN", "USED_TD_WHEN"]:
            # Determine prefix based on flow
            prefix = "DATE_"
            if state == "SALES_SELECT_WHEN":
                prefix = "WHEN_"
            elif state in ["EXCH_TD_WHEN", "USED_TD_WHEN"]:
                prefix = "EXCH_WHEN_"
                
            # Quick keyword bypass
            t_lower = text.lower()
            if any(k in t_lower for k in ["today", "aaj", "right now", "asap", "immediately", "same day"]):
                return f"{prefix}TODAY"
            if any(k in t_lower for k in ["tomorrow", "kal", "next day"]):
                return f"{prefix}TOMORROW"
            if any(k in t_lower for k in ["later this week", "this week", "few days"]):
                return f"{prefix}LATER_WEEK"
            if any(k in t_lower for k in ["next week", "coming week", "following week"]):
                return f"{prefix}NEXT_WEEK"
                
            prompt = f"""Map the user message to one date preference ID.

- "{prefix}TODAY"      → today / aaj / right now / asap / immediately / same day
- "{prefix}TOMORROW"   → tomorrow / kal / next day / day after today
- "{prefix}LATER_WEEK" → later this week / this week / within few days / end of week
- "{prefix}NEXT_WEEK"  → next week / coming week / following week / after this week

USER MESSAGE: "{text}"
Return ONLY JSON: {{"id": "<ID>", "confidence": 0.0-1.0}}
Unknown → {{"id": "NONE", "confidence": 0.0}}"""
            r = _call_groq_json(prompt, 60)
            rid, rconf = r.get("id", "NONE"), float(r.get("confidence", 0.0))
            print(f"🤖 APPT_DATE | '{text}' → '{rid}' ({rconf:.2f})")
            return rid if rconf >= 0.70 and rid != "NONE" else "GENERAL_QUERY"

        # ══════════════════════════════════════════════════════
        # GENERIC FLOW ROUTER  (all other states)
        # ══════════════════════════════════════════════════════
        if state.startswith("INSURANCE") or state.startswith("STATE_4") or state.startswith("STATE_5"):
            flow_name    = "INSURANCE"
            intent_block = _INSURANCE_INTENTS
        elif state.startswith("SALES"):
            flow_name    = "SALES"
            intent_block = _SALES_INTENTS
        elif state.startswith("USED") or state.startswith("EXCH"):
            flow_name    = "USED_CARS"
            intent_block = _USED_CARS_INTENTS
        elif state.startswith("SERVICE"):
            flow_name    = "SERVICE"
            intent_block = _SERVICE_INTENTS
        elif state.startswith("REFINANCING"):
            flow_name    = "REFINANCING"
            intent_block = _REFINANCING_INTENTS
        else:
            return None

        user_state_ctx = USER_STATE.get(phone, {})
        context_parts  = []
        if user_state_ctx.get("vehicle_reg") or user_state_ctx.get("reg_number"):
            reg = user_state_ctx.get("vehicle_reg") or user_state_ctx.get("reg_number")
            context_parts.append(f"Vehicle: {reg}")
        if user_state_ctx.get("selected_car_model"):
            context_parts.append(f"Selected car: {user_state_ctx['selected_car_model']}")
        context_str = f"\nCONTEXT: {', '.join(context_parts)}" if context_parts else ""

        prompt = f"""You are an intent classifier for AutoSherpa WhatsApp bot.
User is in {flow_name} flow (state: {state}).{context_str}

{intent_block}

USER MESSAGE: "{text}"
Return ONLY JSON: {{"action": "<id or GENERAL_QUERY>", "confidence": 0.0-1.0}}"""

        result     = _call_groq_json(prompt, 80)
        action     = result.get("action", "GENERAL_QUERY")
        confidence = float(result.get("confidence", 0.5))

        print(f"🤖 Free-Text Resolver | {flow_name} | state={state} | '{text}' → '{action}' ({confidence:.2f})")
        if confidence < 0.65:
            return "GENERAL_QUERY"
        return action

    except Exception as e:
        print(f"❌ _resolve_free_text_as_button error: {e}")
        return None


# ======================================
# DUPLICATE MESSAGE GUARD
# WhatsApp sometimes delivers the same webhook event twice.
# We track recently seen message IDs to silently drop duplicates.
# ======================================
_SEEN_MESSAGE_IDS = set()
_SEEN_MESSAGE_IDS_MAX = 500   # cap memory usage

# ======================================
# SEND NORMAL TEXT MESSAGE
# ======================================
def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }

    response = requests.post(url, headers=headers, json=payload)
    # 🔥 Store Bot Message
    session_id = USER_STATE.get(to, {}).get("session_id")
    if session_id:
        add_message(
            session_id=session_id,
            sender="bot",
            text=text
        )

    # 🔥 Store bot reply in MySQL conversation_log
    try:
        user_info = USER_STATE.get(to, {})
        vehicle_reg = user_info.get("vehicle_reg") or user_info.get("reg_number")
        state = user_info.get("state", "")
        if state.startswith("INSURANCE") or state.startswith("STATE_4") or state.startswith("STATE_5"):
            flow = "insurance"
        elif state.startswith("SALES"):
            flow = "sales"
        elif state.startswith("USED"):
            flow = "used_cars"
        elif state.startswith("SERVICE"):
            flow = "service"
        elif state.startswith("REFINANCING"):
            flow = "refinancing"
        else:
            flow = "general"
        _log_conv(to, "", text, flow, vehicle_reg)
    except Exception as _log_e:
        print(f"[webhook] conv_log bot error: {_log_e}")

    if response.status_code != 200:
        print(f"Text message failed: {response.text}")


# ======================================
# SEND BUTTON MESSAGE
# ======================================
def send_button_message(to, body, buttons):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # Safety: Truncate body if it exceeds WhatsApp's 1024 char limit
    if body and len(body) > 1024:
        body = body[:1020] + "..."

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": buttons}
        }
    }

    response = requests.post(url, headers=headers, json=payload)
        # 🔥 Store Bot Button Message
    session_id = USER_STATE.get(to, {}).get("session_id")
    if session_id:
        add_message(
            session_id=session_id,
            sender="bot",
            text=body
        )

    if response.status_code != 200:
        print(f"Button message failed: {response.text}")


# ======================================
# SEND INTERACTIVE LIST MESSAGE
# ======================================
def send_list_message(to, body_text, button_text, sections):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text,
                "sections": sections
            }
        }
    }

    response = requests.post(url, headers=headers, json=payload)
        # 🔥 Store Bot List Message
    session_id = USER_STATE.get(to, {}).get("session_id")
    if session_id:
        add_message(
            session_id=session_id,
            sender="bot",
            text=body_text
        )

    if response.status_code != 200:
        print(f"List message failed: {response.text}")

# ======================================
# SEND IMAGE MESSAGE
# ======================================
def send_whatsapp_image(to, image_url, caption=""):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }

    response = requests.post(url, headers=headers, json=payload)
        # 🔥 Store Bot Image Message
    session_id = USER_STATE.get(to, {}).get("session_id")
    if session_id:
        add_message(
            session_id=session_id,
            sender="bot",
            text=f"[IMAGE] {caption if caption else image_url}"
        )

    print("📸 WhatsApp image response:", response.status_code, response.text)


# ======================================
# SEND DOCUMENT MESSAGE (PDF)
# ======================================
def send_whatsapp_document(to, document_url, filename, caption=""):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption
        }
    }

    response = requests.post(url, headers=headers, json=payload)
        # 🔥 Store Bot Document Message
    session_id = USER_STATE.get(to, {}).get("session_id")
    if session_id:
        add_message(
            session_id=session_id,
            sender="bot",
            text=f"[DOCUMENT] {filename}"
        )

    print("📄 WhatsApp document response:", response.status_code, response.text)
    return response

# ======================================
# DOWNLOAD/RETRIEVE MEDIA URL
# ======================================
def get_whatsapp_media_url(media_id):
    """
    Retrieves the temporary download URL for a WhatsApp media ID.
    Returns: URL string or None
    """
    try:
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("url")
        else:
            print(f"❌ Media URL retrieval failed: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error in get_whatsapp_media_url: {e}")
        return None
        
def download_whatsapp_media(media_url, filename):
    """
    Downloads media from WhatsApp URL and saves it locally.
    Returns: Local file path relative to BASE_URL or None
    """
    try:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        response = requests.get(media_url, headers=headers)
        if response.status_code == 200:
            os.makedirs("uploads/vehicle_images", exist_ok=True)
            timestamp = int(time.time())
            # Clean filename
            from werkzeug.utils import secure_filename
            safe_filename = secure_filename(filename or "file")
            local_filename = f"cust_{timestamp}_{safe_filename}"
            local_path = os.path.join("uploads/vehicle_images", local_filename)
            
            with open(local_path, "wb") as f:
                f.write(response.content)
            
            return f"/uploads/vehicle_images/{local_filename}"
        else:
            print(f"❌ Media download failed: {response.status_code}")
            return None
    except Exception as e:
        print(f"❌ Error downloading media: {e}")
        return None

# ======================================
# BLOCK CHECK FUNCTION
# ======================================
def is_number_blocked(phone):
    try:
        db = get_db()
        cur = db.cursor()

        # Compare last 10 digits to avoid format mismatch
        cur.execute(
            "SELECT id FROM blocked_numbers WHERE RIGHT(phone,10) = RIGHT(%s,10)",
            (phone,)
        )

        row = cur.fetchone()
        cur.close()
        db.close()

        return row is not None

    except Exception as e:
        print("🚫 Block check error:", e)
        return False
# ======================================
# MAIN MENU — ABOUT US & CONTACT FLOW HELPER
# ======================================

def _main_menu_sections():
    """Return the standard main menu sections list."""
    return [{"title": "Main Menu", "rows": [
        {"id": "menu_new_cars",     "title": "\U0001f539 New Cars \u2013 Sales",   "description": "Browse new car models & prices"},
        {"id": "menu_used_cars",    "title": "\U0001f697 Used Cars",           "description": "Browse, value or test drive pre-owned cars"},
        {"id": "menu_service",      "title": "\U0001f527 Vehicle Service",     "description": "Book a service appointment"},
        {"id": "menu_insurance",    "title": "\U0001f6e1\ufe0f Vehicle Insurance",   "description": "Renew or get insurance for your vehicle"},
        {"id": "menu_refinancing",  "title": "\U0001f4b3 Refinancing",         "description": "Reduce EMI or get a loan against your car"},
        {"id": "menu_about",        "title": "\u2139\ufe0f About Us",            "description": "Learn more about AutoSherpa"},
        {"id": "menu_contact",      "title": "\U0001f4de Contact & Support",   "description": "Get in touch with our team"},
        {"id": "END_CHAT",          "title": "\U0001f44b End Conversation"}
    ]}]


def _send_main_menu_back(phone):
    """Send the 'anything else?' prompt with main menu list."""
    USER_STATE[phone]["state"] = "MENU"
    send_list_message(
        phone,
        "Is there anything else I can help you with today?",
        "View Options",
        _main_menu_sections()
    )


def _handle_main_about_contact_flow(phone, text):
    """
    Handles all MENU_ABOUT_* and MENU_CONTACT_* states.
    Mirrors the used_cars about/contact flow but returns to the main menu.
    """
    state = USER_STATE.get(phone, {}).get("state", "")

    if text == "MAIN_ABOUT_STORY":
        send_whatsapp_message(
            phone,
            "Here's our journey and what makes AutoSherpa special: \U0001f697\u2728\n\n"
            "\U0001f3c1 *Where It All Began:*\n"
            "AutoSherpa started with a simple mission \u2014 to make car buying and ownership "
            "a smooth, honest, and enjoyable experience for every customer.\n\n"
            "\U0001f3e2 *Our Roots:*\n"
            "With over 15 years in the automotive industry, we've grown from a single dealership "
            "to a trusted name in Bangalore for Hyundai cars \u2014 both new and certified pre-owned.\n\n"
            "\U0001f46a *Customer First Approach:*\n"
            "We've proudly served 10,000+ happy customers, thanks to our commitment to "
            "transparency, value, and after-sales care.\n\n"
            "\U0001f680 *What Drives Us:*\n"
            "Our passion is to help families and individuals find the right vehicle that fits "
            "their needs, lifestyle, and budget \u2014 while delivering 5-star service at every step.\n\n"
            "\U0001f331 *Our Vision:*\n"
            "To be the most loved and recommended Hyundai dealership in South India \u2014 "
            "trusted for both our people and our processes."
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_ABOUT_WHY_CHOOSE":
        send_whatsapp_message(
            phone,
            "Here's why thousands of customers trust AutoSherpa:\n\n"
            "\u2b50 *WHY CHOOSE AUTOSHERPA:*\n\n"
            "\U0001f50e *Quality Assurance:*\n"
            "\u2705 200+ point inspection on every car\n"
            "\u2705 Only certified pre-owned vehicles\n"
            "\u2705 Complete service history verification\n\n"
            "\U0001f4b0 *Best Value:*\n"
            "\u2705 Competitive pricing\n"
            "\u2705 Fair trade-in values\n"
            "\u2705 Transparent pricing - no hidden costs\n\n"
            "\U0001f6e1\ufe0f *Trust & Reliability:*\n"
            "\u2705 15+ years in automotive industry\n"
            "\u2705 10,000+ happy customers\n"
            "\u2705 Extended warranty options\n\n"
            "\U0001f3af *Complete Service:*\n"
            "\u2705 End-to-end car buying support\n"
            "\u2705 Financing assistance\n"
            "\u2705 Insurance & documentation help\n\n"
            "\U0001f527 *After-Sales Support:*\n"
            "\u2705 Dedicated service team\n"
            "\u2705 Genuine spare parts\n"
            "\u2705 Regular maintenance reminders"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_ABOUT_LOCATIONS":
        send_whatsapp_message(
            phone,
            "We'd love to welcome you! Here are our locations:\n\n"
            "\U0001f4cd *AUTOSHERPA LOCATIONS:*\n\n"
            "\U0001f3e2 Main Showroom - Bangalore:\n"
            "\U0001f4cd Address: 123 MG Road, Bangalore - 560001\n"
            "\U0001f4de Phone: +91-9876543210\n"
            "\U0001f552 Timings: Mon-Sat: 9:00 AM - 8:00 PM, Sun: 10:00 AM - 6:00 PM\n"
            "\U0001f17f\ufe0f Facilities: Free parking, Test drive facility, Customer lounge\n\n"
            "\U0001f3e2 Branch - Electronic City:\n"
            "\U0001f4cd Address: 456 Hosur Road, Electronic City - 560100\n"
            "\U0001f4de Phone: +91-9876543211\n"
            "\U0001f552 Timings: Mon-Sat: 9:00 AM - 8:00 PM\n\n"
            "\U0001f5fa\ufe0f *How to Reach:*\n"
            "\U0001f687 Metro: MG Road Metro Station (2 min walk)\n"
            "\U0001f68c Bus: Multiple bus routes available\n"
            "\U0001f697 Car: Easy access from Ring Road\n\n"
            "\U0001f4f1 *Before You Visit:*\n"
            "\u2705 Call ahead to ensure car availability\n"
            "\u2705 Bring valid ID for test drives\n"
            "\u2705 Our team will be ready to assist you"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_ABOUT_SERVICES":
        send_whatsapp_message(
            phone,
            "At AutoSherpa, we offer everything you need \u2014 from car buying to servicing \u2014 all under one roof! \U0001f698\U0001f4bc\n\n"
            "\U0001f3af *OUR SERVICES INCLUDE:*\n\n"
            "\U0001f195 *New Car Sales*\n"
            "\u2705 Full range of Hyundai models\n"
            "\u2705 Expert sales consultation\n"
            "\u2705 Test drive at your convenience\n\n"
            "\U0001f697 *Certified Pre-Owned Cars*\n"
            "\u2705 Thoroughly inspected & certified\n"
            "\u2705 Transparent pricing & service history\n"
            "\u2705 Finance & exchange options\n\n"
            "\U0001f9f0 *Vehicle Servicing & Repairs*\n"
            "\u2705 Hyundai-certified technicians\n"
            "\u2705 Genuine spare parts\n"
            "\u2705 Quick turnaround & pickup-drop facility\n\n"
            "\U0001f527 *Bodyshop & Insurance Claims*\n"
            "\u2705 Accident repairs & dent-paint services\n"
            "\u2705 Hassle-free insurance claim assistance\n"
            "\u2705 Cashless facility with major insurers\n\n"
            "\U0001f4b0 *Finance & Loan Assistance*\n"
            "\u2705 Tie-ups with top banks & NBFCs\n"
            "\u2705 Best interest rates & fast approvals\n"
            "\u2705 On-road pricing breakdown\n\n"
            "\U0001f6e1\ufe0f *Car Insurance & Renewals*\n"
            "\u2705 Instant insurance quotes\n"
            "\u2705 Renewal reminders\n"
            "\u2705 Claim support from start to finish\n\n"
            "\U0001f9fe *RC Transfer & Documentation*\n"
            "\u2705 Ownership transfer assistance\n"
            "\u2705 RTO support\n"
            "\u2705 Documentation help for resale or exchange"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_ABOUT_AWARDS":
        send_whatsapp_message(
            phone,
            "\U0001f3c6 *Achievements & Awards*\n\n"
            "We're proud to be recognized for our commitment to excellence! \U0001f3c6\u2728\n\n"
            "Here are some milestones that make us stand out:\n\n"
            "\U0001f31f *AutoSherpa Achievements:*\n\n"
            "\U0001f3c5 Best Customer Experience Dealer \u2013 South India (2023)\n"
            "\U0001f3c5 Top Performer in Certified Pre-Owned Sales (2022)\n"
            "\U0001f3c5 Highest Customer Satisfaction Score \u2013 Hyundai India (2021)\n"
            "\U0001f3c5 Hyundai Elite Partner Recognition \u2013 3 Years in a Row\n\n"
            "\U0001f389 *What These Awards Mean for You:*\n"
            "\u2705 Transparent & customer-friendly processes\n"
            "\u2705 Consistent service excellence\n"
            "\u2705 Trusted by thousands of happy customers\n\n"
            "\U0001f9e9 *Our real achievement?*\n"
            "Your trust, referrals, and repeat visits \u2014 that's what drives us every day! \U0001f64c"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_CONTACT_CALL_NOW":
        send_whatsapp_message(
            phone,
            "Perfect! Here are our direct contact numbers for immediate assistance:\n\n"
            "\U0001f4de *CALL US DIRECTLY:*\n\n"
            "\U0001f3e2 *Main Showroom - Bangalore:*\n"
            "\U0001f4de Sales: +91-9876543210\n"
            "\U0001f4de Service: +91-9876543211\n"
            "\U0001f552 Available: Mon-Sat: 9 AM - 8 PM, Sun: 10 AM - 6 PM\n\n"
            "\U0001f3e2 *Branch - Electronic City:*\n"
            "\U0001f4de Sales: +91-9876543212\n"
            "\U0001f552 Available: Mon-Sat: 9 AM - 8 PM\n\n"
            "\U0001f198 *Emergency Support:*\n"
            "\U0001f4de 24/7 Helpline: +91-9876543213\n\n"
            "\U0001f4a1 *Pro Tip:* Mention you contacted us via WhatsApp for priority assistance!"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_CONTACT_CALLBACK":
        USER_STATE[phone]["state"] = "MENU_CONTACT_CALLBACK_TIME"
        sections = [{"title": "Preferred Time", "rows": [
            {"id": "MAIN_CALLBACK_MORNING",   "title": "\U0001f305 Morning (9 AM \u2013 12 PM)"},
            {"id": "MAIN_CALLBACK_AFTERNOON", "title": "\u2600\ufe0f Afternoon (12 PM \u2013 4 PM)"},
            {"id": "MAIN_CALLBACK_EVENING",   "title": "\U0001f306 Evening (4 PM \u2013 8 PM)"}
        ]}]
        send_list_message(phone, "Sure! When would you prefer us to call you back?", "Select Time", sections)
        return

    if text in ("MAIN_CALLBACK_MORNING", "MAIN_CALLBACK_AFTERNOON", "MAIN_CALLBACK_EVENING"):
        time_map = {
            "MAIN_CALLBACK_MORNING":   "Morning (9-12)",
            "MAIN_CALLBACK_AFTERNOON": "Afternoon (12-4)",
            "MAIN_CALLBACK_EVENING":   "Evening (4-8)"
        }
        USER_STATE[phone]["callback_time"] = time_map[text]
        USER_STATE[phone]["state"] = "MENU_CONTACT_CALLBACK_NAME"
        send_whatsapp_message(phone, "Great! Please share your details:\n\n1. Your Name:")
        return

    if state == "MENU_CONTACT_CALLBACK_NAME":
        USER_STATE[phone]["callback_name"] = text.strip()
        USER_STATE[phone]["state"] = "MENU_CONTACT_CALLBACK_PHONE"
        send_whatsapp_message(phone, "2. Phone Number:")
        return

    if state == "MENU_CONTACT_CALLBACK_PHONE":
        import re as _re
        entered_phone = text.strip()
        if not _re.match(r'^\d{10}$', entered_phone):
            send_whatsapp_message(phone, "\u274c Invalid number.\nPlease enter a valid 10-digit mobile number:")
            return
        USER_STATE[phone]["callback_phone"] = entered_phone
        USER_STATE[phone]["state"] = "MENU_CONTACT_CALLBACK_NEED"
        send_whatsapp_message(phone, "3. What do you need help with?")
        return

    if state == "MENU_CONTACT_CALLBACK_NEED":
        USER_STATE[phone]["callback_need"] = text.strip()
        try:
            from db import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO callbacks (name, phone, preferred_time, query, created_at) VALUES (%s, %s, %s, %s, NOW())",
                (
                    USER_STATE[phone]["callback_name"],
                    USER_STATE[phone]["callback_phone"],
                    USER_STATE[phone]["callback_time"],
                    USER_STATE[phone]["callback_need"]
                )
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as _e:
            print(f"\u274c Callback insert error (main menu flow): {_e}")

        send_whatsapp_message(
            phone,
            f"Perfect {USER_STATE[phone]['callback_name']}! Your callback is scheduled:\n\n"
            f"\U0001f4cb *CALLBACK SCHEDULED:*\n"
            f"\U0001f464 Name: {USER_STATE[phone]['callback_name']}\n"
            f"\U0001f4f1 Phone: {USER_STATE[phone]['callback_phone']}\n"
            f"\u23f0 Preferred Time: {USER_STATE[phone]['callback_time']}\n"
            f"\u2753 Query: {USER_STATE[phone]['callback_need']}\n\n"
            f"\U0001f4de *What to Expect:*\n"
            f"\u2705 Call within 2 hours if during business hours\n"
            f"\u2705 Our expert will discuss your query with you\n"
            f"\u2705 Personalized assistance for your needs\n\n"
            f"\U0001f552 Business Hours: Mon\u2013Sun: 9 AM \u2013 8 PM\n\n"
            f"Need immediate help?\n"
            f"\U0001f4de Call: +91-9876543210\n"
            f"\U0001f4cd Visit: Main Showroom \u2013 123 MG Road, Bangalore - 560001\n\n"
            f"Thank you for choosing AutoSherpa! \U0001f60a"
        )
        _send_main_menu_back(phone)
        return

    if text == "MAIN_CONTACT_VISIT":
        send_whatsapp_message(
            phone,
            "We'd love to welcome you! Here are our locations:\n\n"
            "\U0001f4cd *AUTOSHERPA LOCATIONS:*\n\n"
            "\U0001f3e2 Main Showroom - Bangalore\n"
            "\U0001f4cd Address: 123 MG Road, Bangalore - 560001\n"
            "\U0001f4de Phone: +91-9876543210\n"
            "\U0001f552 Timings: Mon-Sat: 9:00 AM - 8:00 PM, Sun: 10:00 AM - 6:00 PM\n"
            "\U0001f17f\ufe0f Facilities: Free parking, Test drive facility, Customer lounge\n\n"
            "\U0001f3e2 Branch - Electronic City\n"
            "\U0001f4cd Address: 456 Hosur Road, Electronic City - 560100\n"
            "\U0001f4de Phone: +91-9876543211\n"
            "\U0001f552 Timings: Mon-Sat: 9:00 AM - 8:00 PM\n\n"
            "\U0001f5fa\ufe0f *How to Reach:*\n"
            "\U0001f687 Metro: MG Road Metro Station (2 min walk)\n"
            "\U0001f68c Bus: Multiple bus routes available\n"
            "\U0001f697 Car: Easy access from Ring Road"
        )
        _send_main_menu_back(phone)
        return

    # Unrecognised text — re-show the current sub-menu
    if state == "MENU_ABOUT_MENU":
        sections = [{"title": "About AutoSherpa", "rows": [
            {"id": "MAIN_ABOUT_STORY",      "title": "\U0001f3e2 Our Company Story"},
            {"id": "MAIN_ABOUT_WHY_CHOOSE", "title": "\U0001f31f Why Choose Us"},
            {"id": "MAIN_ABOUT_LOCATIONS",  "title": "\U0001f4cd Our Locations"},
            {"id": "MAIN_ABOUT_SERVICES",   "title": "\U0001f3af Our Services"},
            {"id": "MAIN_ABOUT_AWARDS",     "title": "\U0001f3c6 Achievements & Awards"}
        ]}]
        send_list_message(phone, "Please select an option:", "Select", sections)
    elif state == "MENU_CONTACT_MENU":
        buttons = [
            {"type": "reply", "reply": {"id": "MAIN_CONTACT_CALL_NOW", "title": "\U0001f4de Call us now"}},
            {"type": "reply", "reply": {"id": "MAIN_CONTACT_CALLBACK", "title": "\U0001f4e7 Request call"}},
            {"type": "reply", "reply": {"id": "MAIN_CONTACT_VISIT",    "title": "\U0001f4cd Visit showroom"}}
        ]
        send_button_message(phone, "Please choose how you'd like to get in touch:", buttons)
    else:
        _send_main_menu_back(phone)


def _reprompt_flow_state(phone: str, state: str):
    """
    Re-prompt the customer with the last question for their current flow state.
    Called when a customer chooses "Continue" after the mid-flow exit interceptor.

    Mirrors the existing FLOW_CONTINUE restore logic but covers ALL flows
    in a single central function so there's no duplication.

    Only states that can be interrupted mid-flow (i.e. the customer typed
    something while a question was waiting) are handled here. Unknown states
    fall back to showing the main menu.
    """
    print(f"[reprompt] Restoring state '{state}' for {phone}")

    # ── INSURANCE FLOW ───────────────────────────────────────────────────
    if state == "INSURANCE_LOOKUP":
        send_whatsapp_message(phone,
            "Please enter your *Vehicle Registration Number* to continue.\n"
            "Example: *KA01AB1234*")
        return

    if state in ("INSURANCE_VEHICLE_FOUND", "INSURANCE_NOT_FOUND"):
        from flows.insurance import insurance_flow_handler
        insurance_flow_handler(phone, "__SHOW_MENU__")
        return

    if state == "INSURANCE_ESTIMATE_NAME":
        send_whatsapp_message(phone, "Please share your *full name* to continue:")
        return

    if state == "INSURANCE_ESTIMATE_LINK_SENT":
        send_whatsapp_message(phone,
            "📎 Please upload your document via the link we sent, or choose an option below.")
        return

    if state == "STATE_4_RENEW_TYPE":
        send_list_message(phone, "Please select your renewal type:", "Select Renewal Type",
            [{"title": "Renewal Type", "rows": [
                {"id": "RENEW_FIRST",     "title": "First Renewal"},
                {"id": "RENEW_2ND",       "title": "2nd Renewal"},
                {"id": "RENEW_3RD",       "title": "3rd Renewal"},
                {"id": "RENEW_4TH",       "title": "4th Renewal"},
                {"id": "RENEW_5TH",       "title": "5th Renewal"},
                {"id": "RENEW_6TH_ABOVE", "title": "6th Renewal & Above"},
                {"id": "RENEW_NEW",       "title": "New Policy / First Time"}
            ]}])
        return

    if state == "STATE_4_MODE":
        send_list_message(phone, "How would you like to proceed with the renewal?",
            "Select Mode",
            [{"title": "Appointment Mode", "rows": [
                {"id": "MODE_ONLINE",  "title": "🌐 Online"},
                {"id": "MODE_WALKIN",  "title": "🏢 Walk-in"},
                {"id": "MODE_FIELD",   "title": "🏠 Field Visit"}
            ]}])
        return

    if state == "STATE_4_DATE":
        send_list_message(phone, "When would you prefer your appointment?", "Select Date",
            [{"title": "Preferred Date", "rows": [
                {"id": "DATE_TODAY",      "title": "Today"},
                {"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}
            ]}])
        return

    if state in ("STATE_4_LATER_WEEK", "STATE_4_NEXT_WEEK"):
        from flows.insurance import get_remaining_week_dates, get_next_week_dates
        from datetime import datetime
        if state == "STATE_4_LATER_WEEK":
            dates = get_remaining_week_dates()
            prefix = "WEEK_"
        else:
            dates = get_next_week_dates()
            prefix = "NEXTWEEK_"
        rows = [{"id": f"{prefix}{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                for d in dates]
        send_list_message(phone, "Please choose a date:", "Select Date",
                          [{"title": "Select Date", "rows": rows}])
        return

    if state == "STATE_4_SLOT":
        send_list_message(phone, "Please choose a time slot:", "Select Time Slot",
            [{"title": "Time Slots", "rows": [
                {"id": "SLOT_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                {"id": "SLOT_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                {"id": "SLOT_EVENING",   "title": "Evening (4 PM - 7 PM)"},
                {"id": "SLOT_ANYTIME",   "title": "Anytime"}
            ]}])
        return

    if state == "STATE_4_NAME":
        send_whatsapp_message(phone, "Please confirm your *name* to continue:")
        return

    if state == "STATE_4_ADDRESS":
        send_whatsapp_message(phone,
            "Please share the *address* for the field visit to continue:")
        return

    # ── SALES FLOW ──────────────────────────────────────────────────────
    if state.startswith("SALES"):
        from flows.sales import sales_flow_handler
        # Re-show the last relevant sales prompt via the flow's own restore logic
        try:
            from webhook import _handle_flow_continue_sales
            _handle_flow_continue_sales(phone, state)
        except Exception:
            from flows.sales import handle_sales
            handle_sales(phone)
        return

    # ── USED CARS FLOW ──────────────────────────────────────────────────
    if state.startswith("USED") or state.startswith("EXCH"):
        from flows.used_cars import handle_used_cars
        handle_used_cars(phone)
        return

    # ── SERVICE FLOW ────────────────────────────────────────────────────
    _svc_prefixes = ("waiting_", "vehicle_", "booking_", "callback",
                     "video_", "contact_", "discount", "estimate_", "other_service")
    if state.startswith("SERVICE") or any(state.startswith(p) for p in _svc_prefixes):
        if state == "waiting_for_registration":
             send_whatsapp_message(phone,
                "Please enter your *Vehicle Registration Number* to continue.\n"
                "Example: *KA01AB1234*")
             return
        if state == "waiting_full_name":
            send_whatsapp_message(phone, "Please share your *full name* to continue:")
            return
        if state == "waiting_phone_number":
            send_whatsapp_message(phone, "Please share your *phone number* to continue:")
            return
        if state == "waiting_pickup_address":
            send_whatsapp_message(phone, "Please share your *pickup address* to continue:")
            return
        # For other service states, we fall back to handle_service but try to avoid reset if possible
        from flows.service import handle_service
        handle_service(phone)
        return

    # ── REFINANCING FLOW ─────────────────────────────────────────────────
    if state.startswith("REFINANCING"):
        if state == "REFINANCING_ASK_NAME":
            send_whatsapp_message(phone, "Please share your *full name* to continue:")
            return
        if state == "REFINANCING_ASK_CITY":
            send_whatsapp_message(phone, "Which *city* are you located in?")
            return
        if state == "REFINANCING_ASK_BRAND":
            from flows.refinancing import _send_car_brand_buttons
            _send_car_brand_buttons(phone)
            return
        if state == "REFINANCING_ASK_MODEL":
            from flows.refinancing import _send_car_model_buttons
            _send_car_model_buttons(phone)
            return
        if state == "REFINANCING_ASK_YEAR":
            from flows.refinancing import _send_year_buttons
            _send_year_buttons(phone)
            return
        
        from flows.refinancing import handle_refinancing
        handle_refinancing(phone)
        return

    # ── Fallback: show main menu ─────────────────────────────────────────
    USER_STATE[phone]["state"] = "MENU"
    sections = [{"title": "Main Menu", "rows": [
        {"id": "menu_new_cars",    "title": "🔹 New Cars – Sales",   "description": "Browse new car models & prices"},
        {"id": "menu_used_cars",   "title": "🚗 Used Cars",          "description": "Browse, value or test drive pre-owned cars"},
        {"id": "menu_service",     "title": "🔧 Vehicle Service",    "description": "Book a service appointment"},
        {"id": "menu_insurance",   "title": "🛡️ Vehicle Insurance",  "description": "Renew or get insurance for your vehicle"},
        {"id": "menu_refinancing", "title": "💳 Refinancing",        "description": "Reduce EMI or get a loan against your car"},
        {"id": "menu_about",       "title": "ℹ️ About Us",           "description": "Learn more about AutoSherpa"},
        {"id": "menu_contact",     "title": "📞 Contact & Support",  "description": "Get in touch with our team"}
    ]}]
    send_list_message(phone,
        "How can I help you today?", "View Options", sections)


# ======================================
# WEBHOOK HANDLER
# ======================================
def handle_message(data):
    try:
        print(f"📨 Received webhook data: {data}")
        
        entry = data.get("entry", [])
        if not entry:
            print("❌ No entry in webhook data")
            return

        value = entry[0]["changes"][0]["value"]
        
        # Check if this is a status update (not a message)
        if "statuses" in value:
            print(f"📊 Status update received, ignoring: {value.get('statuses')}")
            return

        if "messages" not in value:
            print("❌ No messages in webhook data")
            return

        messages = value["messages"]
        if not messages:
            print("❌ Empty messages array")
            return
            
        message = messages[0]
        phone = message["from"]
        # 🚫 BLOCK CHECK (DO NOT MOVE THIS BELOW)
        if is_number_blocked(phone):
            print(f"🚫 Blocked number tried to message: {phone}")

            send_whatsapp_message(
                phone,
                "This number is currently restricted from accessing AutoSherpa services. If you believe this is an error, please reach out to our support team for assistance."
            )

            return
        print(f"📱 Message from: {phone}")
        print(f"📦 Full message: {message}")

        # ──────────────────────────────────────────────────────
        # DUPLICATE MESSAGE GUARD
        # WhatsApp Cloud API sometimes sends the same webhook
        # event 2× (especially for interactive button replies).
        # We silently drop any message we have already processed.
        # ──────────────────────────────────────────────────────
        msg_id = message.get("id", "")
        if msg_id and msg_id in _SEEN_MESSAGE_IDS:
            print(f"⚠️ Duplicate message ignored: {msg_id}")
            return
        if msg_id:
            _SEEN_MESSAGE_IDS.add(msg_id)
            # Keep the set from growing forever
            if len(_SEEN_MESSAGE_IDS) > _SEEN_MESSAGE_IDS_MAX:
                # Remove oldest half
                to_remove = list(_SEEN_MESSAGE_IDS)[:_SEEN_MESSAGE_IDS_MAX // 2]
                for old_id in to_remove:
                    _SEEN_MESSAGE_IDS.discard(old_id)
                print(f"🧹 Cleaned _SEEN_MESSAGE_IDS, now {len(_SEEN_MESSAGE_IDS)} entries")

        # Handle different message types
        text = None
        message_type = message.get("type", "")
        
        if message_type == "text":
            text = message["text"]["body"].strip()
            print(f"📝 Text message: '{text}'")
            
        elif message_type == "interactive":
            interactive = message["interactive"]
            print(f"🔄 Interactive received: {interactive}")

            if "button_reply" in interactive:
                text = interactive["button_reply"]["id"]
                print(f"🔘 Button reply: '{text}'")
            elif "list_reply" in interactive:
                text = interactive["list_reply"]["id"]
                title = interactive["list_reply"].get("title", "")
                print(f"📋 List reply: ID='{text}', Title='{title}'")
            else:
                print("❓ Unknown interactive type, ignoring")
                return
                
        elif message_type in ["document", "image"]:
            media_obj = message.get(message_type, {})
            media_id = media_obj.get("id")
            caption  = media_obj.get("caption", "")
            filename = media_obj.get("filename", "")
            
            # Retrieve actual URL
            temp_media_url = get_whatsapp_media_url(media_id)
            media_url = None
            
            if temp_media_url:
                # Download locally immediately
                ext = "jpg" if message_type == "image" else filename.split('.')[-1] if '.' in filename else "bin"
                local_filename = filename or f"upload_{media_id}.{ext}"
                media_url = download_whatsapp_media(temp_media_url, local_filename)
            
            text = f"__FILE_UPLOADED__ | Type: {message_type.upper()} | URL: {media_url if media_url else ('External: ' + temp_media_url if temp_media_url else 'Pending')}"
            if caption:
                text += f" | Caption: {caption}"
            if filename:
                text += f" | Filename: {filename}"
            
            message["media_url"] = media_url or temp_media_url
            print(f"📎 File saved locally: {message_type}, Path: {media_url}")
            
        elif message_type == "audio":
            print("🎵 Audio message received, ignoring")
            return
            
        elif message_type == "location":
            print("📍 Location message received, ignoring")
            return

        elif message_type == "contacts":
            print("👤 Contacts message received, ignoring")
            return
            
        else:
            print(f"⚠️ Unknown message type '{message_type}', ignoring")
            return

        # ──────────────────────────────────────────────────────
        # 🟢 LIVE AGENT INTERCEPTION (REFINED)
        # Check if the user is currently talking to a human agent.
        # ──────────────────────────────────────────────────────
        from flows.service import db
        handoff = db.get_active_handoff_info(phone)

        # ── Detect stale AWAITING_AGENT after agent ended the chat ────────
        # When an agent clicks "End Chat", the DB handoff becomes 'completed'
        # so get_active_handoff_info() returns None, but USER_STATE still has
        # 'AWAITING_AGENT'. Detect this and reset the user to MENU so they
        # can continue using the bot normally.
        _current_state = USER_STATE.get(phone, {}).get("state", "")
        if _current_state == "AWAITING_AGENT" and not handoff:
            print(f"🔄 Agent chat ended for {phone}. Resetting to MENU.")
            USER_STATE[phone]["state"] = "MENU"
            _current_state = "MENU"
            # Send a friendly re-entry message then fall through to MENU handler
            send_whatsapp_message(
                phone,
                "🤖 You're back with the AutoSherpa assistant!\n\n"
                "How can I help you today? Please select an option below 👇"
            )
            sections = [{"title": "Main Menu", "rows": [
                {"id": "menu_new_cars",    "title": "🔹 New Cars – Sales",    "description": "Browse new car models & prices"},
                {"id": "menu_used_cars",   "title": "🚗 Used Cars",           "description": "Browse, value or test drive pre-owned cars"},
                {"id": "menu_service",     "title": "🔧 Vehicle Service",     "description": "Book a service appointment"},
                {"id": "menu_insurance",   "title": "🔰 Vehicle Insurance",   "description": "Renew or get insurance for your vehicle"},
                {"id": "menu_refinancing", "title": "💳 Refinancing",         "description": "Reduce EMI or get a loan against your car"},
                {"id": "menu_about",       "title": "ℹ️ About Us",           "description": "Learn more about AutoSherpa"},
                {"id": "menu_contact",     "title": "📞 Contact & Support",   "description": "Get in touch with our team"}
            ]}]
            send_list_message(phone, "What would you like to do?", "View Options", sections)
            return

        # ── AWAITING_AGENT with active handoff: store message, reassure user ─
        # The handoff exists in DB (requested/pending) but agent hasn't connected
        # yet. Store the message for the agent and reassure the user — don't
        # route to any flow handler.
        if _current_state == "AWAITING_AGENT" and handoff:
            h_id     = handoff['id']
            h_status = handoff['status']
            if h_status == 'connected':
                # Agent is already connected — store silently
                print(f"👨‍💼 AGENT CONNECTED (ID: {h_id}). Storing customer message.")
                if message_type in ["image", "document"]:
                    db.store_live_message(
                        handoff_id=h_id, 
                        sender_type="customer", 
                        message_text=message.get(message_type, {}).get("caption", ""),
                        message_type=message_type,
                        media_url=message.get("media_url")
                    )
                else:
                    db.store_live_message(h_id, "customer", text or f"__UNSUPPORTED_{message_type.upper()}__")
                return
            else:
                # Still waiting — store and reassure
                print(f"⌛ AWAITING AGENT (ID: {h_id}). Storing message for agent.")
                if message_type in ["image", "document"]:
                    db.store_live_message(
                        handoff_id=h_id, 
                        sender_type="customer", 
                        message_text=message.get(message_type, {}).get("caption", ""),
                        message_type=message_type,
                        media_url=message.get("media_url")
                    )
                else:
                    db.store_live_message(h_id, "customer", text or f"__UNSUPPORTED_{message_type.upper()}__")
                send_whatsapp_message(
                    phone,
                    "⏳ You're still in the queue. An agent will respond to you shortly.\n\n"
                    "Your message has been saved and they will see it when they connect. 💬"
                )
                return

        if handoff and _current_state != "AWAITING_AGENT":
            h_id     = handoff['id']
            h_status = handoff['status']

            if h_status == 'connected':
                # 🤫 SILENT MODE: Human is talking, bot must stay quiet.
                print(f"👨‍💼 AGENT CONNECTED (ID: {h_id}). Routing message...")
                if message_type in ["image", "document"]:
                    db.store_live_message(
                        handoff_id=h_id, 
                        sender_type="customer", 
                        message_text=message.get(message_type, {}).get("caption", ""),
                        message_type=message_type,
                        media_url=message.get("media_url")
                    )
                else:
                    db.store_live_message(h_id, "customer", text or f"__UNSUPPORTED_{message_type.upper()}__")
                return
            else:
                # 🤖 SHADOW MODE: Agent is requested but not yet here.
                # Log the context for the agent, but LET THE BOT KEEP HELPING.
                print(f"⌛ AGENT REQUESTED (ID: {h_id}). Logging context for agent...")
                if message_type in ["image", "document"]:
                    db.store_live_message(
                        handoff_id=h_id, 
                        sender_type="customer", 
                        message_text=message.get(message_type, {}).get("caption", ""),
                        message_type=message_type,
                        media_url=message.get("media_url")
                    )
                else:
                    db.store_live_message(h_id, "customer", text or f"__UNSUPPORTED_{message_type.upper()}__")
                # NO RETURN HERE -> Flow continues below

        # ══════════════════════════════════════════════════════
        # HI / HELLO / HEY  →  RESTART-OR-CONTINUE CHECK
        # Must run BEFORE the AI free-text resolver so that a
        # greeting sent mid-flow always shows the restart prompt
        # instead of being treated as a GENERAL_QUERY.
        # ══════════════════════════════════════════════════════
        import re as _re_hi
        _normalized_for_hi = _re_hi.sub(r'[^a-z]', '', text.lower()) if message_type == "text" else ""
        if _normalized_for_hi.startswith(("hi", "hello", "hey")):
            _current_state_hi = USER_STATE.get(phone, {}).get("state", "START")
            _active_flow_states_hi = ("INSURANCE", "STATE_4", "STATE_5", "SALES", "USED", "SERVICE", "REFINANCING")
            if any(_current_state_hi.startswith(s) for s in _active_flow_states_hi):
                # Save snapshot so "Continue Flow" can restore it
                USER_STATE[phone]["_saved_state"] = _current_state_hi
                USER_STATE[phone]["state"] = "AWAITING_RESTART_CONFIRM"
                print(f"⚠️ User in active flow ({_current_state_hi}), asking restart or continue")
                send_button_message(
                    phone,
                    "⚠️ You are currently in the middle of a flow.\n\n"
                    "Would you like to *continue* where you left off, or *restart* from the beginning?",
                    [
                        {"type": "reply", "reply": {"id": "FLOW_CONTINUE", "title": "▶️ Continue Flow"}},
                        {"type": "reply", "reply": {"id": "FLOW_RESTART",  "title": "🔄 Restart"}}
                    ]
                )
                return

        # ══════════════════════════════════════════════════════
        # MID-FLOW SOCIAL / EXIT WORD INTERCEPTOR
        #
        # Problem: When a customer types "bye", "thanks", "ok",
        # "thank you", "restart", "stop" etc. while inside an
        # active flow (e.g. STATE_4_RENEW_TYPE), these words:
        #   (a) are blocked by skip_substrs in _resolve_free_text_as_button
        #       → the flow handler re-prompts the same question forever
        #   (b) OR get routed to END_CHAT immediately with no warning
        #
        # Fix: Intercept these words BEFORE the AI resolver and
        # show a confirmation prompt so the customer can choose to
        # continue the flow or end the conversation. This mirrors
        # the existing Hi/Hello restart-confirm pattern exactly.
        #
        # IMPORTANT: Only fires when user is INSIDE an active flow.
        # Does NOT fire at MENU / START state (no flow to interrupt).
        # Does NOT fire for button/list replies (message_type != "text").
        # ══════════════════════════════════════════════════════
        if message_type == "text" and text and text != "__FILE_UPLOADED__":
            _state_for_exit = USER_STATE.get(phone, {}).get("state", "START")
            _active_flow_prefixes_exit = (
                "INSURANCE", "STATE_4", "STATE_5", "SALES", "USED", "EXCH",
                "SERVICE", "REFINANCING",
                "waiting_", "vehicle_", "booking_", "callback",
                "video_", "contact_", "discount", "estimate_", "other_service",
            )
            if any(_state_for_exit.startswith(p) for p in _active_flow_prefixes_exit):
                _t_exit = text.strip().lower()
                # ── Normalise: remove punctuation for matching ──────────────
                import re as _re_exit
                _t_exit_clean = _re_exit.sub(r"[^a-z\s]", "", _t_exit).strip()

                # ── Social/exit word patterns ───────────────────────────────
                # These are short, conversational words that have NO valid
                # meaning as a flow input in ANY state but signal the customer
                # may want to leave or is just reacting socially.
                _EXIT_EXACT = {
                    "bye", "byee", "byeee", "goodbye", "good bye", "bye bye",
                    "thanks", "thank you", "thank u", "thankyou", "thx", "ty",
                    "ok", "okay", "ok bye", "okay bye", "ok thanks", "okay thanks",
                    "ok thank you", "okay thank you", "ok done", "okay done",
                    "done", "stop", "quit", "exit", "end", "close",
                    "restart", "reset", "start over", "start again",
                    "no thanks", "no thank you", "nah", "nope",
                    "that's all", "thats all", "that is all",
                    "nothing", "never mind", "nevermind", "forget it",
                    "cancel", "cancelled", "abort",
                }
                # Also match if the entire message IS one of these phrases
                # (guard against "okay let me book tomorrow" — that's not an exit)
                _is_exit_word = (
                    _t_exit_clean in _EXIT_EXACT
                    or _t_exit in _EXIT_EXACT
                    # Handle with trailing punctuation: "bye!", "thanks."
                    or _re_exit.sub(r"[^a-z\s]", "", _t_exit).strip() in _EXIT_EXACT
                )

                if _is_exit_word:
                    print(f"[mid-flow exit] Intercepted '{text}' in state '{_state_for_exit}' — showing confirm")
                    USER_STATE[phone]["_saved_state"] = _state_for_exit
                    USER_STATE[phone]["state"] = "AWAITING_EXIT_CONFIRM"
                    send_button_message(
                        phone,
                        "👋 Just to confirm — would you like to *end this conversation* or *continue* where you left off?\n\n"
                        "Your progress has been saved. 💾",
                        [
                            {"type": "reply", "reply": {"id": "EXIT_CONFIRM_CONTINUE", "title": "▶️ Continue"}},
                            {"type": "reply", "reply": {"id": "EXIT_CONFIRM_END",      "title": "❌ End Chat"}}
                        ]
                    )
                    return

        # ══════════════════════════════════════════════════════
        # AWAITING_EXIT_CONFIRM — handle button replies
        # ══════════════════════════════════════════════════════
        if USER_STATE.get(phone, {}).get("state") == "AWAITING_EXIT_CONFIRM":
            if text == "EXIT_CONFIRM_CONTINUE":
                # Restore the saved flow state and re-prompt the last question
                _restored = USER_STATE[phone].get("_saved_state", "MENU")
                USER_STATE[phone]["state"] = _restored
                print(f"[mid-flow exit] Customer chose CONTINUE → restoring state '{_restored}'")
                _reprompt_flow_state(phone, _restored)
                return
            elif text == "EXIT_CONFIRM_END":
                # End the session cleanly
                print(f"[mid-flow exit] Customer chose END")
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    close_chat_session(session_id)
                USER_STATE.pop(phone, None)
                send_whatsapp_message(
                    phone,
                    "✅ *Thank you for choosing AutoSherpa!* 🙏\n\n"
                    "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
                    "🚗 Have a great day!"
                )
                return
            else:
                # Any other text while awaiting exit confirm → re-show the prompt
                send_button_message(
                    phone,
                    "Please choose one of the options below to continue:",
                    [
                        {"type": "reply", "reply": {"id": "EXIT_CONFIRM_CONTINUE", "title": "▶️ Continue"}},
                        {"type": "reply", "reply": {"id": "EXIT_CONFIRM_END",      "title": "❌ End Chat"}}
                    ]
                )
                return

        # ══════════════════════════════════════════════════════
        # AI FREE-TEXT RESOLUTION
        # If user typed a plain text message (not a button click),
        # and they are inside an active flow, try to map their
        # free-text to the correct button/action ID using Groq AI.
        # This lets users type naturally instead of only using buttons.
        # Example: "I want to book tomorrow" → "tomorrow_appointment"
        # GENERAL_QUERY is returned when the existing flow AI should answer.
        # None is returned when resolution fails — flow continues normally.
        # ══════════════════════════════════════════════════════
        _ai_resolved_action = None
        if message_type == "text" and text and text != "__FILE_UPLOADED__":
            current_state_for_ai = USER_STATE.get(phone, {}).get("state", "START")
            # ── Active flow prefixes include BOTH uppercase flow states AND
            #    the lowercase states used internally by the service flow handler.
            active_flow_prefixes = (
                "INSURANCE", "STATE_4", "STATE_5", "SALES", "USED", "EXCH",
                "SERVICE", "REFINANCING",
                # Service flow uses lowercase state names internally
                "waiting_", "vehicle_", "booking_", "callback",
                "video_", "contact_", "discount", "estimate_", "other_service",
            )
            if any(current_state_for_ai.startswith(p) for p in active_flow_prefixes):
                _ai_resolved_action = _resolve_free_text_as_button(phone, text, current_state_for_ai)

                if _ai_resolved_action in ["GENERAL_QUERY", "SPECIFIC_CAR"]:
                    # ── The message is a free-text question or specific car query.
                    # Route to the correct flow-specific AI and reply immediately.
                    # We keep the ORIGINAL text so the flow AI can see the car model.
                    print(f"🤖 {_ai_resolved_action} in flow '{current_state_for_ai}' — calling flow AI for: '{text}'")
                    from ai import handle_general_query_in_flow

                    # Determine flow type from state prefix
                    _s = current_state_for_ai
                    if _s.startswith("INSURANCE") or _s.startswith("STATE_4") or _s.startswith("STATE_5"):
                        _flow = "insurance"
                    elif _s.startswith("SALES"):
                        _flow = "sales"
                    elif _s.startswith("USED"):
                        _flow = "used_cars"
                    elif _s.startswith("SERVICE"):
                        _flow = "service"
                    elif _s.startswith("REFINANCING"):
                        _flow = "refinancing"
                    else:
                        _flow = "general"

                    _user_info = USER_STATE.get(phone, {})
                    _vreg = _user_info.get("vehicle_reg") or _user_info.get("reg_number") or \
                            _user_info.get("data", {}).get("reg_no")

                    _ai_reply, _follow_on = handle_general_query_in_flow(
                        phone=phone,
                        user_text=text,
                        flow_type=_flow,
                        vehicle_reg=_vreg,
                        user_state=_user_info
                    )
                    
                    if _follow_on == "TALK_TO_ADVISOR":
                        print(f"🤖 LLM requested agent handoff for: '{text}'")
                        text = "TALK_TO_ADVISOR"
                        # Fall through to the flow handlers below to process the TALK_TO_ADVISOR action
                    else:
                        send_whatsapp_message(phone, _ai_reply)

                        # ── Soft hint after AI answers a mid-renewal question ─────
                        # Send a gentle one-liner so user knows to continue.
                        # Do NOT re-send the full list — it feels like the bot is
                        # ignoring the question and forcing the flow.
                        _step_hints = {
                            "STATE_4_RENEW_TYPE": "😊 Hope that helps! Please select your *renewal type* from the list above to continue.",
                            "STATE_4_MODE":       "😊 Hope that helps! Please select your *appointment mode* from the options above to continue.",
                            "STATE_4_DATE":       "😊 Hope that helps! Please select your *preferred date* from the options above to continue.",
                            "STATE_4_SLOT":       "😊 Hope that helps! Please select your *preferred time slot* from the options above to continue.",
                            "STATE_4_NAME":       "😊 Hope that helps! Please share your *name* to confirm the appointment.",
                        }
                        _hint = _step_hints.get(current_state_for_ai)
                        if _hint:
                            send_whatsapp_message(phone, _hint)

                        return  # State untouched — customer stays exactly where they were

                elif _ai_resolved_action and _ai_resolved_action != "GENERAL_QUERY":
                    print(f"✅ AI resolved free-text '{text}' → button '{_ai_resolved_action}'")
                    text = _ai_resolved_action   # Replace text with resolved button ID
                    # Do NOT change message_type — let flow handlers process it normally

        


        # ======================
        # SPECIAL CASE: If user just ended chat and immediately sends something
        # ======================
        # Check if this might be an accidental trigger
        if not text:
            print("⚠️ Empty text, ignoring")
            return
            
        # Check if text is just whitespace or very short
        if len(text.strip()) == 0:
            print("⚠️ Whitespace-only message, ignoring")
            return

        # Init user if new
        if phone not in USER_STATE:
            USER_STATE[phone] = {"state": "START"}
            print(f"🆕 New user initialized: {phone}")
            # 🔥 Start Mongo Session
            session_id = start_chat_session(phone)
            USER_STATE[phone]["session_id"] = session_id

        # 🔥 Store User Message (for ALL users — new and existing)
        session_id = USER_STATE.get(phone, {}).get("session_id")
        if session_id:
            # Store raw display text: for interactive replies, store the actual label if available
            display_text = text
            if message_type == "interactive":
                interactive_data = message.get("interactive", {})
                if "button_reply" in interactive_data:
                    display_text = interactive_data["button_reply"].get("title", text)
                elif "list_reply" in interactive_data:
                    display_text = interactive_data["list_reply"].get("title", text)
            add_message(
                session_id=session_id,
                sender="user",
                text=display_text
            )

        # 🔥 Store user message in MySQL conversation_log
        try:
            user_info2 = USER_STATE.get(phone, {})
            vehicle_reg2 = user_info2.get("vehicle_reg") or user_info2.get("reg_number")
            state2 = user_info2.get("state", "")
            if state2.startswith("INSURANCE") or state2.startswith("STATE_4") or state2.startswith("STATE_5"):
                flow2 = "insurance"
            elif state2.startswith("SALES"):
                flow2 = "sales"
            elif state2.startswith("USED"):
                flow2 = "used_cars"
            elif state2.startswith("SERVICE"):
                flow2 = "service"
            elif state2.startswith("REFINANCING"):
                flow2 = "refinancing"
            else:
                flow2 = "general"
            _log_conv(phone, display_text if session_id else text, "", flow2, vehicle_reg2)
        except Exception as _log_e2:
            print(f"[webhook] conv_log user error: {_log_e2}")



        state = USER_STATE[phone]["state"]
        print(f"📊 Current state: {state}")

        # ======================
        # START / HI
        # ======================
        import re

        normalized_text = re.sub(r'[^a-z]', '', text.lower())

        # ======================
        # HI / HELLO / HEY with no active flow → show main menu
        # (mid-flow greeting is intercepted earlier, before the AI resolver)
        # ======================
        if normalized_text.startswith(("hi", "hello", "hey")):
            # ── No active flow → go to main menu ──
            USER_STATE[phone]["state"] = "MENU"
            sections = [{
                "title": "Main Menu",
                "rows": [
                    {"id": "menu_new_cars",   "title": "🔹 New Cars – Sales",        "description": "Browse new car models & prices"},
                    {"id": "menu_used_cars",  "title": "🚗 Used Cars",               "description": "Browse, value or test drive pre-owned cars"},
                    {"id": "menu_service",    "title": "🔧 Vehicle Service",         "description": "Book a service appointment"},
                    {"id": "menu_insurance",  "title": "🔰 Vehicle Insurance",       "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",            "description": "Reduce EMI or get a loan against your car"},
                    {"id": "menu_about",      "title": "ℹ️ About Us",               "description": "Learn more about AutoSherpa"},
                    {"id": "menu_contact",    "title": "📞 Contact & Support",       "description": "Get in touch with our team"}
                ]
            }]
            send_list_message(
                phone,
                "👋 Welcome to *AutoSherpa* — your one-stop automotive assistant 🚗✨\n\n"
                "How can I help you today?",
                "View Options",
                sections
            )
            return


        # ======================
        # If state is START and user sends any message (not hi)
        # ======================
        if state == "START":
            print(f"🤖 User in START state, routing to AI. Text='{text}'")
            # Try to resolve to a menu option first
            menu_resolved = _resolve_menu_free_text(text)
            if menu_resolved:
                print(f"🤖 START state: AI resolved '{text}' → '{menu_resolved}'")
                USER_STATE[phone]["state"] = "MENU"
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if menu_resolved == "menu_new_cars":
                    from flows.sales import handle_sales
                    if session_id:
                        from chat_history import update_summary
                        update_summary(session_id, {"lead_type": "sales"})
                    handle_sales(phone)
                    return
                elif menu_resolved == "menu_used_cars":
                    from flows.used_cars import handle_used_cars
                    if session_id:
                        from chat_history import update_summary
                        update_summary(session_id, {"lead_type": "used_cars"})
                    handle_used_cars(phone)
                    return
                elif menu_resolved == "menu_service":
                    USER_STATE[phone]["state"] = "SERVICE_START"
                    from flows.service import handle_service
                    if session_id:
                        from chat_history import update_summary
                        update_summary(session_id, {"lead_type": "service"})
                    handle_service(phone)
                    return
                elif menu_resolved == "menu_insurance":
                    from flows.insurance import handle_insurance
                    if session_id:
                        from chat_history import update_summary
                        update_summary(session_id, {"lead_type": "insurance"})
                    handle_insurance(phone)
                    return
                elif menu_resolved == "menu_refinancing":
                    from flows.refinancing import handle_refinancing
                    if session_id:
                        from chat_history import update_summary
                        update_summary(session_id, {"lead_type": "refinancing"})
                    handle_refinancing(phone)
                    return

            # Could not map to menu — use general AI response
            from ai import get_ai_response
            ai_reply = get_ai_response(text)
            final_reply = (
                f"{ai_reply}\n\n"
                "👉 Type *Hi* anytime to explore all our services."
            )
            send_whatsapp_message(phone, final_reply)
            return


        # ======================
        # MAIN MENU
        # ======================
        if state == "MENU":

            if text == "menu_new_cars":
                # ─── New Cars → Sales flow ───
                print("🔹 New Cars selected")
                from flows.sales import handle_sales
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "sales"})
                handle_sales(phone)
                return

            elif text == "menu_used_cars":
                # ─── Used Cars flow ───
                print("🚗 Used Cars selected")
                from flows.used_cars import handle_used_cars
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "used_cars"})
                handle_used_cars(phone)
                return

            elif text == "menu_service":
                print("🔧 Vehicle Service selected")

                # 🔥 Ensure user state exists
                USER_STATE.setdefault(phone, {})
                USER_STATE[phone]["state"] = "SERVICE_START"

                # 🔥 Start service flow
                from flows.service import handle_service, service_flow_handler
                handle_service(phone)

                # 🔥 Optional: Track in Mongo summary (unchanged)
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "service"})

                return
                        

            elif text == "menu_insurance":
                # ─── Insurance flow ───
                print("🛡 Vehicle Insurance selected")
                from flows.insurance import handle_insurance
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "insurance"})
                handle_insurance(phone)
                return

            elif text == "menu_refinancing":
                # ─── Refinancing flow ───
                print("💳 Refinancing selected")
                from flows.refinancing import handle_refinancing
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    from chat_history import update_summary
                    update_summary(session_id, {"lead_type": "refinancing"})
                handle_refinancing(phone)
                return

            elif text == "menu_about":
                # ─── About Us → rich sub-menu ───
                print("ℹ️ About Us selected")
                USER_STATE[phone]["state"] = "MENU_ABOUT_MENU"
                sections = [{
                    "title": "About AutoSherpa",
                    "rows": [
                        {"id": "MAIN_ABOUT_STORY",      "title": "🏢 Our Company Story"},
                        {"id": "MAIN_ABOUT_WHY_CHOOSE", "title": "🌟 Why Choose Us"},
                        {"id": "MAIN_ABOUT_LOCATIONS",  "title": "📍 Our Locations"},
                        {"id": "MAIN_ABOUT_SERVICES",   "title": "🎯 Our Services"},
                        {"id": "MAIN_ABOUT_AWARDS",     "title": "🏆 Achievements & Awards"}
                    ]
                }]
                send_list_message(
                    phone,
                    "Welcome to AutoSherpa! Here's what you'd like to know about us:",
                    "Select",
                    sections
                )
                return

            elif text == "menu_contact":
                # ─── Contact & Support → rich sub-menu with buttons ───
                print("📞 Contact & Support selected")
                USER_STATE[phone]["state"] = "MENU_CONTACT_MENU"
                buttons = [
                    {"type": "reply", "reply": {"id": "MAIN_CONTACT_CALL_NOW",  "title": "📞 Call us now"}},
                    {"type": "reply", "reply": {"id": "MAIN_CONTACT_CALLBACK",  "title": "📧 Request call"}},
                    {"type": "reply", "reply": {"id": "MAIN_CONTACT_VISIT",     "title": "📍 Visit showroom"}}
                ]
                send_button_message(
                    phone,
                    "I'd be happy to connect you with our team! How would you like to get in touch?",
                    buttons
                )
                return

            # ─── End Conversation from main menu ───
            elif text in ("END_CHAT", "end_conversation", "END_CONVERSATION"):
                print(f"🛑 User ended chat from MENU (trigger='{text}')")
                session_id = USER_STATE.get(phone, {}).get("session_id")
                if session_id:
                    close_chat_session(session_id)
                    print(f"✅ MongoDB session closed: {session_id}")
                USER_STATE.pop(phone, None)
                send_whatsapp_message(
                    phone,
                    "✅ *Thank you for choosing AutoSherpa!*\n\n"
                    "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
                    "🚗 Have a great day!"
                )
                return

            # ─── Also handle legacy IDs in case any old button reply arrives ───
            elif text == "insurance":
                from flows.insurance import handle_insurance
                handle_insurance(phone)
                return

            elif text == "sales":
                from flows.sales import handle_sales
                handle_sales(phone)
                return

            else:
                # ── AI FREE-TEXT INTENT DETECTION FOR MENU STATE ──
                # User typed something like "i want insurance" or "book service"
                # instead of clicking a menu button. AI maps it to correct menu item.
                menu_resolved = _resolve_menu_free_text(text)
                if menu_resolved:
                    print(f"🤖 Menu AI resolved '{text}' → '{menu_resolved}'")
                    if menu_resolved == "menu_new_cars":
                        from flows.sales import handle_sales
                        session_id = USER_STATE.get(phone, {}).get("session_id")
                        if session_id:
                            from chat_history import update_summary
                            update_summary(session_id, {"lead_type": "sales"})
                        handle_sales(phone)
                        return
                    elif menu_resolved == "menu_used_cars":
                        from flows.used_cars import handle_used_cars
                        session_id = USER_STATE.get(phone, {}).get("session_id")
                        if session_id:
                            from chat_history import update_summary
                            update_summary(session_id, {"lead_type": "used_cars"})
                        handle_used_cars(phone)
                        return
                    elif menu_resolved == "menu_service":
                        USER_STATE.setdefault(phone, {})
                        USER_STATE[phone]["state"] = "SERVICE_START"
                        from flows.service import handle_service
                        handle_service(phone)
                        session_id = USER_STATE.get(phone, {}).get("session_id")
                        if session_id:
                            from chat_history import update_summary
                            update_summary(session_id, {"lead_type": "service"})
                        return
                    elif menu_resolved == "menu_insurance":
                        from flows.insurance import handle_insurance
                        session_id = USER_STATE.get(phone, {}).get("session_id")
                        if session_id:
                            from chat_history import update_summary
                            update_summary(session_id, {"lead_type": "insurance"})
                        handle_insurance(phone)
                        return
                    elif menu_resolved == "menu_refinancing":
                        from flows.refinancing import handle_refinancing
                        session_id = USER_STATE.get(phone, {}).get("session_id")
                        if session_id:
                            from chat_history import update_summary
                            update_summary(session_id, {"lead_type": "refinancing"})
                        handle_refinancing(phone)
                        return

                # Fallback: AI responds then shows menu
                from ai import get_ai_response as _gen_ai
                _menu_ai_reply = _gen_ai(text)
                send_whatsapp_message(phone, _menu_ai_reply)
                sections = [{"title": "Main Menu", "rows": [
                    {"id": "menu_new_cars",  "title": "🔹 New Cars – Sales",        "description": "Browse new car models & prices"},
                    {"id": "menu_used_cars", "title": "🚗 Used Cars",               "description": "Browse, value or test drive pre-owned cars"},
                    {"id": "menu_service",   "title": "🔧 Vehicle Service",         "description": "Book a service appointment"},
                    {"id": "menu_insurance", "title": "🔰 Vehicle Insurance",       "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",            "description": "Reduce EMI or get a loan against your car"},
                    {"id": "menu_about",     "title": "ℹ️ About Us",                "description": "Learn more about AutoSherpa"},
                    {"id": "menu_contact",   "title": "📞 Contact & Support",       "description": "Get in touch with our team"}
                ]}]
                send_list_message(phone,
                    "Here's what I can help you with 👆",
                    "View Options", sections)
                return
        # ======================
        # FLOW CONTINUE / RESTART (reply to the "restart or continue?" prompt)
        # ======================
        if text == "FLOW_CONTINUE":
            restored_state = USER_STATE[phone].get("_saved_state", "START")
            USER_STATE[phone]["state"] = restored_state
            USER_STATE[phone].pop("_saved_state", None)
            print(f"▶️ User chose to continue flow. Restored state: {restored_state}")
            send_whatsapp_message(phone, "✅ Continuing where you left off...")

            # ── Insurance sub-states: re-show the exact question the user was on ──
            if restored_state.startswith("STATE_4") or restored_state.startswith("STATE_5") or restored_state.startswith("INSURANCE"):
                from flows.insurance import (
                    insurance_flow_handler,
                    _action_send_slot_list,
                    _action_start_renewal,
                    _action_start_estimate,
                    _action_send_advisor,
                )
                from utils import get_remaining_week_dates, get_next_week_dates
                from datetime import datetime, timedelta

                if restored_state == "STATE_4_RENEW_TYPE":
                    send_list_message(phone,
                        "Please select your renewal type:",
                        "Select Renewal Type",
                        [{"title": "Renewal Type", "rows": [
                            {"id": "RENEW_FIRST",     "title": "First Renewal"},
                            {"id": "RENEW_2ND",       "title": "2nd Renewal"},
                            {"id": "RENEW_3RD",       "title": "3rd Renewal"},
                            {"id": "RENEW_4TH",       "title": "4th Renewal"},
                            {"id": "RENEW_5TH",       "title": "5th Renewal"},
                            {"id": "RENEW_6TH_ABOVE", "title": "6th Renewal & Above"},
                            {"id": "RENEW_NEW",       "title": "New Policy / First Time"}
                        ]}]
                    )

                elif restored_state == "STATE_4_MODE":
                    send_list_message(phone,
                        "How would you like to proceed?",
                        "Select",
                        [{"title": "Proceed Options", "rows": [
                            {"id": "MODE_ONLINE", "title": "Online"},
                            {"id": "MODE_WALKIN", "title": "Walk-In"},
                            {"id": "MODE_FIELD",  "title": "Field Visit"}
                        ]}]
                    )

                elif restored_state == "STATE_4_DATE":
                    send_list_message(phone,
                        "When would you prefer your appointment?",
                        "Select",
                        [{"title": "Preferred Date", "rows": [
                            {"id": "DATE_TODAY",      "title": "Today"},
                            {"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                            {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                            {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}
                        ]}]
                    )

                elif restored_state == "STATE_4_LATER_WEEK":
                    rows = [{"id": f"WEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                            for d in get_remaining_week_dates()]
                    send_list_message(phone, "Please choose a date:", "Select Date",
                                      [{"title": "Select Date", "rows": rows}])

                elif restored_state == "STATE_4_NEXT_WEEK":
                    rows = [{"id": f"NEXTWEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                            for d in get_next_week_dates()]
                    send_list_message(phone, "Please choose a date:", "Select Date",
                                      [{"title": "Select Date", "rows": rows}])

                elif restored_state == "STATE_4_SLOT":
                    _action_send_slot_list(phone)

                elif restored_state == "STATE_4_NAME":
                    send_whatsapp_message(phone, "Please confirm your name:")

                elif restored_state == "STATE_4_ADDRESS":
                    send_whatsapp_message(phone, "Please share the address for the field visit:")

                elif restored_state == "STATE_5_ADVISOR":
                    _action_send_advisor(phone)

                else:
                    # INSURANCE_VEHICLE_FOUND or any other insurance state → show main insurance menu
                    insurance_flow_handler(phone, "__SHOW_MENU__")

            elif restored_state.startswith("SALES"):
                from flows.sales import sales_flow_handler, handle_sales
                from utils import get_remaining_week_dates, get_next_week_dates

                if restored_state == "SALES_MENU":
                    handle_sales(phone)

                elif restored_state == "SALES_NEW_CARS":
                    from db import get_all_cars_paginated
                    page = USER_STATE[phone].get("car_page", 1)
                    result = get_all_cars_paginated(page=page, per_page=8)
                    rows = [{"id": f"MODEL_{c['id']}", "title": f"{c['make']} {c['model']}"[:24]}
                            for c in result.get("cars", [])]
                    if result.get("has_next"): rows.append({"id": "NEXT_PAGE", "title": "➡️ Next Page"})
                    if result.get("has_prev"): rows.append({"id": "PREV_PAGE", "title": "⬅️ Previous Page"})
                    send_list_message(phone, "Here are our cars (Premium → Entry Level):", "Select Car",
                        [{"title": f"Our Cars (Page {result['page']} of {result['total_pages']})", "rows": rows}])

                elif restored_state == "SALES_MODEL_DETAILS":
                    from flows.sales import show_options_menu_only
                    show_options_menu_only(phone)

                elif restored_state == "SALES_SELECT_APPOINTMENT_TYPE":
                    send_list_message(phone, "What kind of appointment would you like?", "Select Appointment",
                        [{"title": "Select Appointment Type", "rows": [
                            {"id": "APPT_HOME_VISIT", "title": "🏠 Book a Home Visit"},
                            {"id": "APPT_SHOWROOM",   "title": "🏢 Showroom Visit"}
                        ]}])

                elif restored_state == "SALES_SELECT_WHEN":
                    appt_type = USER_STATE[phone].get("appointment_type", "appointment")
                    send_list_message(phone, f"When would you prefer your {appt_type.lower()}?", "Select Date",
                        [{"title": "Select Schedule", "rows": [
                            {"id": "WHEN_TODAY",      "title": "Today"},
                            {"id": "WHEN_TOMORROW",   "title": "Tomorrow"},
                            {"id": "WHEN_LATER_WEEK", "title": "Later This Week"},
                            {"id": "WHEN_NEXT_WEEK",  "title": "Next Week"}
                        ]}])

                elif restored_state == "SALES_LATER_WEEK":
                    from flows.sales import get_remaining_week_dates as sales_week_dates
                    rows = [{"id": f"SALES_WEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                            for d in sales_week_dates()]
                    send_list_message(phone, "Please choose a date:", "Select Date",
                        [{"title": "Select Date", "rows": rows}])

                elif restored_state == "SALES_NEXT_WEEK":
                    from flows.sales import get_next_week_dates as sales_next_dates
                    rows = [{"id": f"SALES_NEXTWEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                            for d in sales_next_dates()]
                    send_list_message(phone, "Please choose a date:", "Select Date",
                        [{"title": "Select Date", "rows": rows}])

                elif restored_state == "SALES_SELECT_TIME":
                    send_list_message(phone, "Which time works best for you?", "Select Time",
                        [{"title": "Select Time Slot", "rows": [
                            {"id": "TIME_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                            {"id": "TIME_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                            {"id": "TIME_EVENING",   "title": "Evening (4 PM - 7 PM)"}
                        ]}])

                elif restored_state == "SALES_COLLECT_ADDRESS":
                    send_whatsapp_message(phone,
                        "Please provide your *complete address* (house number, street, area, city, pincode):")

                elif restored_state == "SALES_COLLECT_NAME":
                    send_whatsapp_message(phone, "Please provide your *full name*:")

                else:
                    handle_sales(phone)

            elif restored_state.startswith("USED"):
                from flows.used_cars import used_cars_flow_handler, handle_used_cars

                if restored_state == "USED_CARS_MENU":
                    handle_used_cars(phone)

                elif restored_state == "USED_SELECT_BUDGET":
                    send_list_message(phone, "What's your budget range?", "Select",
                        [{"title": "Select Budget", "rows": [
                            {"id": "under1",  "title": "Under ₹1 Lakhs"},
                            {"id": "1-3",     "title": "₹1–3 Lakhs"},
                            {"id": "3-5",     "title": "₹3–5 Lakhs"},
                            {"id": "5-10",    "title": "₹5–10 Lakhs"},
                            {"id": "above10", "title": "Above ₹10 Lakhs"}
                        ]}])

                elif restored_state == "USED_SELECT_TYPE":
                    from flows.used_cars import map_budget_to_range, get_types_for_budget
                    budget_id = USER_STATE[phone].get("selected_budget", "5-10")
                    min_p, max_p = map_budget_to_range(budget_id)
                    types = get_types_for_budget(min_p, max_p)
                    rows = [{"id": "TYPE_ALL", "title": "📋 All types"}]
                    for t in types[:9]:
                        rows.append({"id": f"TYPE_{t.lower().replace(' ','_')}", "title": f"🚘 {t}"[:24]})
                    send_list_message(phone, "What type of car do you prefer?", "Select",
                        [{"title": "Select Car Type", "rows": rows}])

                elif restored_state == "USED_SELECT_BRAND":
                    from flows.used_cars import map_budget_to_range, get_brands_for_budget_and_type
                    budget_id = USER_STATE[phone].get("selected_budget", "5-10")
                    car_type_raw = USER_STATE[phone].get("selected_type", "TYPE_ALL")
                    car_type = "all" if car_type_raw == "TYPE_ALL" else car_type_raw.replace("TYPE_","").replace("_"," ").title()
                    min_p, max_p = map_budget_to_range(budget_id)
                    brands = get_brands_for_budget_and_type(min_p, max_p, car_type)
                    rows = [{"id": "BRAND_ALL", "title": "All Brands"}]
                    for b in brands[:9]:
                        rows.append({"id": f"BRAND_{b.lower().replace(' ','_')}", "title": b[:24]})
                    send_list_message(phone, "Which brand do you prefer?", "Select",
                        [{"title": "Select Brand", "rows": rows}])

                elif restored_state in ("USED_SHOW_CARS", "USED_CONFIRM_CHANGE"):
                    from flows.used_cars import _show_used_cars
                    USER_STATE[phone]["offset"] = 0
                    _show_used_cars(phone)

                elif restored_state == "USED_BOOK_SELECT_WHEN":
                    car_name = USER_STATE[phone].get("selected_used_car_name", "this car")
                    send_list_message(phone,
                        f"When would you prefer your {car_name} test drive?", "Select",
                        [{"title": "Select Schedule", "rows": [
                            {"id": "USED_WHEN_TODAY",      "title": "Today (if available)"},
                            {"id": "USED_WHEN_TOMORROW",   "title": "Tomorrow"},
                            {"id": "USED_WHEN_LATER_WEEK", "title": "Later This Week"},
                            {"id": "USED_WHEN_NEXT_WEEK",  "title": "Next Week"}
                        ]}])

                elif restored_state in ("USED_SELECT_EXACT_DATE",):
                    send_whatsapp_message(phone, "Please select a date from the list above, or type a date.")

                elif restored_state == "USED_BOOK_SELECT_TIME":
                    send_button_message(phone, "Which time works best for you?", [
                        {"type": "reply", "reply": {"id": "USED_TIME_MORNING",   "title": "Morning (10AM-12PM)"}},
                        {"type": "reply", "reply": {"id": "USED_TIME_AFTERNOON", "title": "Afternoon (12PM-4PM)"}},
                        {"type": "reply", "reply": {"id": "USED_TIME_EVENING",   "title": "Evening (4PM-7PM)"}}
                    ])

                elif restored_state == "USED_BOOK_COLLECT_NAME":
                    send_whatsapp_message(phone, "Please provide your *full name*:")

                elif restored_state == "USED_BOOK_COLLECT_PHONE":
                    send_whatsapp_message(phone, "Please provide your *phone number*:")

                elif restored_state == "USED_BOOK_COLLECT_LICENSE":
                    send_button_message(phone, "Do you have a valid driving license?", [
                        {"type": "reply", "reply": {"id": "LICENSE_YES", "title": "✅ Yes"}},
                        {"type": "reply", "reply": {"id": "LICENSE_NO",  "title": "❌ No"}}
                    ])

                elif restored_state == "USED_BOOK_SELECT_LOCATION_TYPE":
                    send_button_message(phone, "Where would you like to take the test drive?", [
                        {"type": "reply", "reply": {"id": "FROM_SHOWROOM",   "title": "🏢 From showroom"}},
                        {"type": "reply", "reply": {"id": "TO_MY_LOCATION",  "title": "🏠 To my location"}}
                    ])

                elif restored_state == "USED_BOOK_COLLECT_LOCATION":
                    send_whatsapp_message(phone, "Please type your location/address for the test drive:")

                elif restored_state == "USED_BOOK_CONFIRM":
                    send_button_message(phone, "Would you like to confirm this booking?", [
                        {"type": "reply", "reply": {"id": "BOOK_CONFIRM", "title": "✅ Confirm"}},
                        {"type": "reply", "reply": {"id": "BOOK_CANCEL",  "title": "❌ Cancel"}}
                    ])

                elif restored_state == "USED_VALUATION_MODE":
                    send_list_message(phone,
                        "Would you like to exchange or go for One Way Selling?", "Select",
                        [{"title": "Select", "rows": [
                            {"id": "VALUATION_EXCHANGE", "title": "🔁 Exchange"},
                            {"id": "VALUATION_ONEWAY",   "title": "💰 One Way Selling"}
                        ]}])

                elif restored_state == "USED_VALUATION_COLLECT_NAME":
                    send_whatsapp_message(phone, "Please provide your *full name*:")

                elif restored_state == "USED_VALUATION_COLLECT_PHONE":
                    send_whatsapp_message(phone, "Please provide your *phone number*:")

                elif restored_state == "USED_VALUATION_COLLECT_LOCATION":
                    send_whatsapp_message(phone, "Please provide your *current location/city*:")

                else:
                    handle_used_cars(phone)

            elif restored_state.startswith("SERVICE"):
                from flows.service import service_flow_handler, handle_service
                # Service uses its own bot.user_sessions — just re-greet or re-ask reg
                handle_service(phone)

            elif restored_state.startswith("REFINANCING"):
                # Refinancing: restart the flow from the beginning
                from flows.refinancing import handle_refinancing
                handle_refinancing(phone)

            return

        if text == "FLOW_RESTART":
            print(f"🔄 User chose to restart. Clearing state for {phone}")
            # Clear flow state but keep session_id
            saved_session = USER_STATE[phone].get("session_id")
            USER_STATE[phone] = {"state": "MENU", "session_id": saved_session}
            sections = [{"title": "Main Menu", "rows": [
                {"id": "menu_new_cars",  "title": "🔹 New Cars – Sales",       "description": "Browse new car models & prices"},
                {"id": "menu_used_cars", "title": "🚗 Used Cars",              "description": "Browse, value or test drive pre-owned cars"},
                {"id": "menu_service",   "title": "🔧 Vehicle Service",        "description": "Book a service appointment"},
                {"id": "menu_insurance", "title": "🔰 Vehicle Insurance",      "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",            "description": "Reduce EMI or get a loan against your car"},
                {"id": "menu_about",     "title": "ℹ️ About Us",              "description": "Learn more about AutoSherpa"},
                {"id": "menu_contact",   "title": "📞 Contact & Support",      "description": "Get in touch with our team"}
            ]}]
            send_list_message(
                phone,
                "👋 Welcome to *AutoSherpa* — your one-stop automotive assistant 🚗✨\n\n"
                "How can I help you today?",
                "View Options",
                sections
            )
            return

        # Handle case where user sends something else while awaiting restart confirm
        if USER_STATE[phone].get("state") == "AWAITING_RESTART_CONFIRM":
            send_button_message(
                phone,
                "Please choose one of the options below to proceed:",
                [
                    {"type": "reply", "reply": {"id": "FLOW_CONTINUE", "title": "▶️ Continue Flow"}},
                    {"type": "reply", "reply": {"id": "FLOW_RESTART",  "title": "🔄 Restart"}}
                ]
            )
            return

        # ======================
        # END CHAT — MUST be checked BEFORE any flow routing.
        # Each module uses a different end button ID:
        #   sales / insurance / webhook → "END_CHAT"
        #   service                     → "end_conversation"
        #   used_cars                   → "END_CONVERSATION"
        # By catching all IDs here FIRST, close_chat_session() always runs
        # regardless of which flow the user was in.
        # ======================
        _END_IDS = {"END_CHAT", "end_conversation", "END_CONVERSATION"}
        if text in _END_IDS:
            print(f"🛑 User ended chat (trigger=\'{text}\')")
            # 🔥 Close MongoDB Session — sets status=closed & end_time
            session_id = USER_STATE.get(phone, {}).get("session_id")
            if session_id:
                close_chat_session(session_id)
                print(f"✅ MongoDB session closed: {session_id}")
            else:
                print(f"⚠️ No session_id for {phone}")
            USER_STATE.pop(phone, None)
            send_whatsapp_message(
                phone,
                "✅ *Thank you for choosing AutoSherpa!*\n\n"
                "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
                "🚗 Have a great day!"
            )
            return

        # ======================
        # PREVIOUS MENU
        # ======================
        if text == "PREVIOUS_MENU":
            print(f"🔙 User clicked Previous Menu")
            state = USER_STATE.get(phone, {}).get("state", "")

            # 🔥 Insurance flow (unchanged)
            if state.startswith("INSURANCE") or state.startswith("STATE_4") or state.startswith("STATE_5"):
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                print(f"🔄 Returning to insurance menu")

                from flows.insurance import insurance_flow_handler
                insurance_flow_handler(phone, "__SHOW_MENU__")
                return

            # 💳 Refinancing flow
            if state.startswith("REFINANCING"):
                from flows.refinancing import handle_refinancing
                handle_refinancing(phone)
                return

            # 🚗 Sales flow (FIXED)
            if state.startswith("SALES"):
                from flows.sales import sales_flow_handler
                sales_flow_handler(phone, "PREVIOUS_MENU")
                return

            # 🔧 Service flow — catches both SERVICE_* and lowercase service states
            _svc_prefixes_pm = (
                "SERVICE",
                "waiting_", "vehicle_", "booking_", "callback",
                "video_", "contact_", "discount", "estimate_", "other_service",
            )
            if any(state.startswith(p) for p in _svc_prefixes_pm) or state in ("SERVICE_AGENT_CONFIRMATION", "initial"):
                print(f"🔧 Processing service flow: state={state}, text='{text}'")
                try:
                    from flows.service import service_flow_handler
                    service_flow_handler(phone, text)
                except Exception as e:
                    print("❌ Service flow error:", e)
                    send_whatsapp_message(phone, "⚠️ Something went wrong in Service flow. Please try again.")
                return
                        # 📍 Else go to main menu
            USER_STATE[phone]["state"] = "MENU"
            print(f"🔄 Returning to main menu")
            sections = [{"title": "Main Menu", "rows": [
                {"id": "menu_new_cars",  "title": "🔹 New Cars – Sales",   "description": "Browse new car models & prices"},
                {"id": "menu_used_cars", "title": "🚗 Used Cars",          "description": "Browse, value or test drive pre-owned cars"},
                {"id": "menu_service",   "title": "🔧 Vehicle Service",    "description": "Book a service appointment"},
                {"id": "menu_insurance", "title": "🔰 Vehicle Insurance",  "description": "Renew or get insurance for your vehicle"},
                    {"id": "menu_refinancing",  "title": "💳 Refinancing",            "description": "Reduce EMI or get a loan against your car"},
                {"id": "menu_about",     "title": "ℹ️ About Us",          "description": "Learn more about AutoSherpa"},
                {"id": "menu_contact",   "title": "📞 Contact & Support",  "description": "Get in touch with our team"}
            ]}]
            send_list_message(
                phone,
                "👋 Welcome to *AutoSherpa* — your one-stop automotive assistant 🚗✨\n\n"
                "How can I help you today?",
                "View Options",
                sections
            )
            return
        # ======================
        # INSURANCE FLOW
        # ======================
        if state.startswith("INSURANCE") or state.startswith("STATE_4") or state.startswith("STATE_5"):
            print(f"🔰 Processing insurance flow: state={state}, text='{text}'")
            from flows.insurance import insurance_flow_handler
            insurance_flow_handler(phone, text)
            return
        # ======================
        # USED CARS FLOW  ✅ (NEW)
        # ======================
        if state.startswith("USED") or state.startswith("EXCH"):
            print(f"🔄 Processing used cars/exchange flow: state={state}, text='{text}'")
            from flows.used_cars import used_cars_flow_handler as _ucfh
            _ucfh(phone, text)
            return
        # ======================
        # SERVICE FLOW
        # NOTE: The service handler uses BOTH "SERVICE_*" uppercase states AND
        # lowercase states like "waiting_for_registration", "vehicle_found", etc.
        # ALL of them must route here — not fall through to the AI fallback.
        # ======================
        _svc_prefixes = (
            "SERVICE",
            "waiting_", "vehicle_", "booking_", "callback",
            "video_", "contact_", "discount", "estimate_", "other_service",
        )
        if any(state.startswith(p) for p in _svc_prefixes) or state in ("SERVICE_AGENT_CONFIRMATION", "initial"):
            print(f"🔧 Processing service flow: state={state}, text=\'{text}\'")
            from flows.service import service_flow_handler
            service_flow_handler(phone, text)
            return
        
        # ======================
        # SALES FLOW
        # ======================
        if state.startswith("SALES"):
            print(f"🚗 Processing sales flow: state={state}, text='{text}'")
            from flows.sales import sales_flow_handler
            sales_flow_handler(phone, text)
            return

        # ======================
        # REFINANCING FLOW
        # ======================
        if state.startswith("REFINANCING"):
            print(f"💳 Processing refinancing flow: state={state}, text='{text}'")
            from flows.refinancing import refinancing_flow_handler
            refinancing_flow_handler(phone, text)
            return

        # ======================
        # MAIN MENU — ABOUT US FLOW
        # ======================
        if state.startswith("MENU_ABOUT") or state.startswith("MENU_CONTACT"):
            _handle_main_about_contact_flow(phone, text)
            return

        # ======================
        # SAFETY NET — catch any remaining service-flow lowercase states
        # that were not caught by the main dispatch above (e.g. brand-new
        # states added to service.py in the future).
        # ======================
        _svc_safety_prefixes = (
            "waiting_", "vehicle_", "booking_", "callback",
            "video_", "contact_", "discount", "estimate_", "other_service",
        )
        if any(state.startswith(p) for p in _svc_safety_prefixes):
            print(f"🔧 Safety-net: routing unmatched service state '{state}' to service handler")
            from flows.service import service_flow_handler
            service_flow_handler(phone, text)
            return

        # ======================
        # FALLBACK - Unknown state
        # ======================
        print(f"❓ Unknown state '{state}' for user {phone}")

        # ✅ AWAITING_AGENT with no active handoff = agent ended the chat
        # (belt-and-suspenders: the interception block above handles this,
        # but if somehow it reaches here, reset gracefully)
        if state == "AWAITING_AGENT":
            USER_STATE[phone]["state"] = "MENU"
            send_whatsapp_message(
                phone,
                "🤖 You're back with the AutoSherpa assistant!\n\n"
                "Type *Hi* to see the main menu and continue."
            )
            return

        # ✅ If user is in START state (not inside any flow), use AI to respond
        if state == "START":
            from ai import get_ai_response
            ai_reply = get_ai_response(text)
            final_reply = (
                f"{ai_reply}\n\n"
                "👉 Type *Hi* anytime to explore all our services."
            )
            send_whatsapp_message(phone, final_reply)
        else:
            # User in unknown flow state - reset and guide
            USER_STATE[phone]["state"] = "START"
            send_whatsapp_message(
                phone,
                "Please type 'Hi' to restart the conversation."
            )

    except Exception as e:
        print("❌ CRITICAL ERROR in handle_message:")
        print(traceback.format_exc())
        # Optional: notify user
        # send_whatsapp_message(phone, "Sorry, something went wrong 😔 Please type 'hi' to restart.")