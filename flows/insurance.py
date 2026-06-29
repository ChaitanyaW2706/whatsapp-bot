# flows/insurance.py

from db import get_db
from webhook import send_whatsapp_message, send_button_message, send_list_message
from config import DB_CONFIG, ACCESS_TOKEN, USER_STATE
import uuid
import time
import os
import json
from datetime import datetime, date, timedelta
from utils import (
    is_valid_appointment_slot,
    get_available_booking_dates,
    resolve_date_from_text,
    is_genuine_query,
    normalize_text,
    classify_insurance_intent_request,
    is_insurance_knowledge_query,
    understand_customer_in_flow_context,
    validate_and_format_renewal_type,
    is_valid_vehicle_reg,
)


BASE_URL = os.getenv("BASE_URL")

# Initialize Groq client
from llm_config import groq_client as client, MODEL_NAME


# ============================
# INTELLIGENT FLOW COMPREHENSION FOR INSURANCE
# ============================
def _handle_insurance_with_ai_understanding(phone, text, state):
    """
    MUST be called FIRST in insurance state handlers.
    Uses AI to understand what customer really wants BEFORE following flow rigidly.
    
    Returns: (should_proceed_flow, ai_response, flow_action)
    """
    try:
        # Build flow context
        flow_context = {
            "state": state,
            "user_data": USER_STATE.get(phone, {}),
        }
        
        # Get AI understanding
        understanding = understand_customer_in_flow_context(phone, text, state, flow_context)
        
        action = understanding.get("action", "CONTINUE_FLOW")
        flow_action = understanding.get("flow_action")
        response = understanding.get("response")
        confidence = understanding.get("confidence", 0.5)
        
        print(f"🧠 Insurance AI Understanding: action={action}, flow_action={flow_action}, confidence={confidence}")
        
        # If we should continue with flow, return True
        if action == "CONTINUE_FLOW":
            return (True, None, None)
        
        # For all other actions, return False and let caller handle response/action
        if response:
            send_whatsapp_message(phone, response)
        
        return (False, response, flow_action)
        
    except Exception as e:
        print(f"❌ AI understanding error in insurance: {e}")
        # On error, proceed with normal flow
        return (True, None, None)


# ============================
# RENEWAL TYPE FREE-TEXT RESOLVER
# Maps natural language → exact renewal type button ID
# Called when user types instead of clicking the list
# ============================
def _resolve_renewal_type_from_text(text: str) -> str | None:
    """
    Use keyword matching + Groq AI to map free-text renewal descriptions
    to the exact button ID the flow handler needs.

    Examples:
      "3rd renewel"          → "RENEW_3RD"
      "first time"           → "RENEW_FIRST"
      "it is my first renewl"→ "RENEW_FIRST"
      "new policy"           → "RENEW_NEW"
      "5th"                  → "RENEW_5TH"
    """
    t = text.lower().strip()

    # ── Fast keyword match (no API call needed) ──────────────────────
    keyword_map = [
        (["new policy", "first time", "first time buyer", "fresh", "no policy",
          "never had", "brand new policy", "new insurance"], "RENEW_NEW"),
        (["1st", "first renewal", "first renew", "1 renewal", "1st renewal",
          "first one", "my first"], "RENEW_FIRST"),
        (["2nd", "second", "2 renewal", "2nd renewal", "second renewal"], "RENEW_2ND"),
        (["3rd", "third", "3 renewal", "3rd renewal", "third renewal"], "RENEW_3RD"),
        (["4th", "fourth", "4 renewal", "4th renewal", "fourth renewal"], "RENEW_4TH"),
        (["5th", "fifth", "5 renewal", "5th renewal", "fifth renewal"], "RENEW_5TH"),
        (["6th", "6 renewal", "6th renewal"], "RENEW_6TH_ABOVE"),
        (["other", "specify", "custom", "manually"], "RENEW_OTHER"),
    ]
    for keywords, btn_id in keyword_map:
        if any(k in t for k in keywords):
            print(f"🔑 Keyword renewal match: '{text}' → '{btn_id}'")
            return btn_id

    # ── Groq AI fallback ─────────────────────────────────────────────
    try:
        prompt = f"""Map this message to a vehicle insurance renewal type ID.

IDs:
- "RENEW_FIRST"     → first renewal / 1st renewal / first time renewing
- "RENEW_2ND"       → 2nd / second renewal
- "RENEW_3RD"       → 3rd / third renewal
- "RENEW_4TH"       → 4th / fourth renewal
- "RENEW_5TH"       → 5th / fifth renewal
- "RENEW_6TH_ABOVE" → 6th renewal
- "RENEW_OTHER"     → other renewal type (7th, 8th, or any other custom number of renewals)
- "RENEW_NEW"       → new policy / first time buying insurance / never had policy

USER MESSAGE: "{text}"

Return ONLY JSON: {{"id": "<ID>", "confidence": 0.0-1.0}}
If not a renewal type → {{"id": "NONE", "confidence": 0.0}}"""

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=60,
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content.strip())
        btn_id     = result.get("id", "NONE")
        confidence = float(result.get("confidence", 0.0))
        print(f"🤖 AI renewal type: '{text}' → '{btn_id}' ({confidence:.2f})")
        if confidence >= 0.70 and btn_id != "NONE":
            return btn_id
    except Exception as e:
        print(f"❌ _resolve_renewal_type_from_text AI error: {e}")

    return None


# ============================
# REFUSAL DETECTOR
# ============================
_REFUSAL_WORDS = {
    "no", "nope", "nah", "skip", "cancel", "later", "not now",
    "not interested", "maybe later", "no thanks", "no thank you",
    "don't want", "dont want", "leave it", "not today", "not yet",
    "i'll do it later", "ill do it later", "i'll skip", "ill skip",
    "not going to", "i won't", "forget it", "pass", "i pass",
    "not ready", "do it later", "some other time",
    # Natural opt-out phrases
    "not required", "not needed", "no need", "i don't need",
    "i dont need", "changed my mind", "i changed my mind",
    "change my mind", "i don't want to proceed", "dont want to proceed",
    "don't want to proceed", "i don't want to continue",
    "dont want to continue", "don't want to continue",
    "i want to stop", "stop this", "let it be", "leave it",
    "its fine", "it's fine", "never mind", "nevermind",
    "i'm good", "im good", "i am good", "no worries",
    "i'll pass", "ill pass", "not now thanks", "maybe some other time",
    "not at this time", "not at the moment", "not right now",
    "i won't proceed", "i wont proceed", "abort", "exit flow",
    "don't proceed", "dont proceed", "no proceed",
}

# Longer opt-out phrases (partial match)
_REFUSAL_PHRASES = [
    "changed my mind", "not required", "not needed", "no need",
    "don't want to", "dont want to", "do not want to",
    "not interested", "not proceed", "don't proceed", "dont proceed",
    "i want to go back", "take me back", "not going to proceed",
    "i'll not proceed", "ill not proceed", "won't proceed",
    "wont proceed", "please stop", "stop asking", "leave me",
    "let me go", "i want out", "get me out", "not now",
    "some other time", "another time", "not today", "no more",
    "enough", "that's all", "thats all", "i'm done", "im done",
    "i am done", "all done", "all good", "thanks anyway",
    "thank you anyway", "its ok", "it's ok", "its okay", "it's okay",
]


def _is_refusal(text: str) -> bool:
    """
    Returns True if the user's message signals they don't want to proceed.
    Uses keyword matching first, then Groq LLM for ambiguous natural language.
    """
    t = text.lower().strip()

    # ── 1. Exact match against known refusal words ──────────────────────
    if t in _REFUSAL_WORDS:
        return True

    # ── 2. Partial phrase match ──────────────────────────────────────────
    for phrase in _REFUSAL_PHRASES:
        if phrase in t:
            return True

    # ── 3. Also check _REFUSAL_WORDS phrases (len > 4) ──────────────────
    for phrase in _REFUSAL_WORDS:
        if len(phrase) > 4 and phrase in t:
            return True

    # ── 4. LLM fallback for ambiguous natural language ───────────────────
    # Only call LLM if text is more than 3 words (short texts handled above)
    if len(t.split()) >= 3:
        try:
            prompt = f"""Determine if this customer message means they want to STOP, CANCEL, or NOT PROCEED with the current process.

CUSTOMER MESSAGE: "{text}"

Answer ONLY with valid JSON: {{"is_refusal": true/false, "confidence": 0.0-1.0}}

Examples of REFUSAL (is_refusal: true):
- "not required i changed my mind"
- "i have changed my mind not to proceed"
- "i don't want this anymore"
- "please stop, i'll do it later"
- "no thanks i'll pass for now"
- "i've decided not to go ahead"
- "let's not do this now"

Examples of NOT REFUSAL (is_refusal: false):
- "what is NCB?"
- "how much will it cost?"
- "my name is John Smith"
- "I want to renew my insurance"
- "morning slot please"
"""
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=60,
                response_format={"type": "json_object"}
            )
            result = json.loads(response.choices[0].message.content.strip())
            is_ref   = result.get("is_refusal", False)
            conf     = float(result.get("confidence", 0.0))
            print(f"[insurance] 🤖 LLM refusal check: '{text}' → {is_ref} ({conf:.2f})")
            if is_ref and conf >= 0.75:
                return True
        except Exception as e:
            print(f"[insurance] _is_refusal LLM error: {e}")

    return False


# ============================
# NAME VALIDATOR
# ============================
def _is_likely_name(text: str) -> bool:
    """Helper to catch conversational input masquerading as a name."""
    t = text.strip()
    words = t.lower().split()
    if len(t) < 2 or len(t) > 40:
        return False
    if len(words) > 4:
        return False
    conv_words = {"i", "my", "me", "want", "dont", "don't", "need",
                  "please", "cancel", "stop", "no", "yes", "what",
                  "how", "why", "estimate", "price", "cost"}
    if any(w in conv_words for w in words):
        return False
    if _is_refusal(t.lower()):
        return False
    return True


# Keep old name for backward compat
_is_valid_name = _is_likely_name


# ============================
# GRACEFUL EXIT HELPER
# ============================
def _offer_graceful_exit(phone: str, message: str):
    """Show menu/end-chat buttons when user signals they don't want to proceed."""
    send_button_message(
        phone,
        message,
        [
            {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅ Main Menu"}},
            {"type": "reply", "reply": {"id": "END_CHAT",      "title": "❌ End Chat"}}
        ]
    )


# ============================
# MID-FLOW EXIT INTENT DETECTOR
# Called at the top of every structured state before form resolvers.
# ============================
def _detect_mid_flow_intent(text: str) -> str | None:
    """
    Detect navigation/cancellation intent from free-text in mid-flow states.
    Returns: 'CANCEL', 'BACK', 'END_CHAT', 'TALK_TO_ADVISOR', 'GET_ESTIMATE' or None.
    """
    t = text.lower().strip()

    cancel_kw = [
        "cancel", "don't want", "dont want", "i don't want", "i dont want",
        "not interested", "not now", "no thanks", "nope", "nah",
        "stop", "abort", "quit", "leave", "exit this", "i changed my mind",
        "changed my mind", "nevermind", "never mind", "forget it",
        "skip this", "i want to go back", "take me back",
        # Additional natural language opt-outs
        "not required", "not needed", "no need", "don't need",
        "dont need", "i don't need this", "i dont need this",
        "not going to proceed", "won't proceed", "wont proceed",
        "don't want to proceed", "dont want to proceed",
        "i don't want to continue", "dont want to continue",
        "don't want to continue", "i'm done", "im done",
        "let it be", "leave it", "it's fine", "its fine",
        "thanks anyway", "thank you anyway", "i'll pass", "ill pass",
        "not at this time", "not right now", "some other time",
        "maybe later", "do it later", "not today",
    ]
    if any(k in t for k in cancel_kw):
        return "CANCEL"

    end_kw = ["bye", "goodbye", "good bye", "end chat", "close chat",
              "finish", "done", "that's all", "thats all", "thank you bye",
              "thanks bye", "ok bye", "okay bye"]
    if any(k in t for k in end_kw):
        return "END_CHAT"

    back_kw = ["go back", "back", "main menu", "previous menu",
               "return", "home", "start over", "restart"]
    if any(k in t for k in back_kw):
        return "BACK"

    if any(k in t for k in ["talk to", "speak to", "human", "agent", "advisor", "customer care",
                              "connect me", "real person", "speak with", "need agent"]):
        return "TALK_TO_ADVISOR"

    if any(k in t for k in [
        "get estimate", "get a estimate", "get an estimate",
        "want estimate", "want a quote", "want an estimate",
        "need estimate", "need a quote", "i want estimate",
        "i want a quote", "i want quote", "i need estimate",
        "i need a quote", "give me estimate", "give me a quote",
        "give me quote", "can i get estimate", "can i get a quote",
        "can you give estimate", "can you give quote",
        "give a quote", "get a quote", "get quote",
        "check the estimate", "check estimate", "check quote",
        "how much for my", "calculate premium", "premium details"
    ]):
        return "GET_ESTIMATE"

    if any(k in t for k in [
        "renew my", "renew insurance", "renewal", "want to renew",
        "need to renew", "policy renewal", "renew policy"
    ]):
        # Only switch to renewal if we aren't already in it, 
        # but for intent detection, we just return the intent.
        return "RENEW_INSURANCE"

    return None


# ============================
# HANDLE MID-FLOW EXIT
# ============================
def _handle_mid_flow_exit(phone: str, intent: str):
    """Gracefully exit mid-flow: CANCEL/BACK → menu, END_CHAT → goodbye."""
    if intent == "END_CHAT":
        USER_STATE.pop(phone, None)
        send_whatsapp_message(
            phone,
            "✅ *Thank you for choosing AutoSherpa Insurance Desk 🙏*\n\n"
            "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
            "🚗 Have a great day!"
        )
        return

    # CANCEL or BACK → return to most appropriate menu
    reg_no        = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
    vehicle_found = USER_STATE.get(phone, {}).get("data", {}).get("vehicle_found")

    USER_STATE[phone].pop("renew", None)
    USER_STATE[phone].pop("renew_prefill", None)

    if vehicle_found is True and reg_no:
        vehicle = get_vehicle_from_db(reg_no)
        if vehicle:
            USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
            send_whatsapp_message(phone, "No problem! Taking you back to the main menu. 👇")
            _action_show_main_menu(phone, vehicle)
            return

    if vehicle_found is False and reg_no:
        USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
        send_whatsapp_message(phone, "No problem! Taking you back. 👇")
        _action_show_not_found_menu(phone, reg_no)
        return

    # Fallback: restart from lookup
    USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
    USER_STATE[phone]["data"]  = {}
    send_whatsapp_message(
        phone,
        "No problem! Let's start fresh.\n\n"
        "Please enter your *Vehicle Registration Number* to continue.\n"
        "Example: KA01AB1234"
    )


# ============================
# REPROMPT CURRENT FORM FIELD
# Called when mid-flow guard suppresses a flow switch.
# ============================
def _reprompt_current_field(phone: str, state: str):
    """Re-ask the form question for the given mid-flow state."""
    try:
        if state == "STATE_4_RENEW_TYPE":
            send_list_message(
                phone,
                "Please select your renewal type to continue:",
                "Select Renewal Type",
                [{"title": "Renewal Type", "rows": [
                    {"id": "RENEW_FIRST",     "title": "First Renewal"},
                    {"id": "RENEW_2ND",       "title": "2nd Renewal"},
                    {"id": "RENEW_3RD",       "title": "3rd Renewal"},
                    {"id": "RENEW_4TH",       "title": "4th Renewal"},
                    {"id": "RENEW_5TH",       "title": "5th Renewal"},
                    {"id": "RENEW_6TH_ABOVE", "title": "6th Renewal"},
                    {"id": "RENEW_NEW",       "title": "New Policy / First Time"},
                    {"id": "RENEW_OTHER",     "title": "Other (Type Manually)"}
                ]}]
            )
        elif state == "STATE_4_RENEW_TYPE_OTHER":
            send_whatsapp_message(phone, "✏️ Please type your renewal type:")
        elif state == "STATE_4_MODE":
            send_list_message(
                phone,
                "How would you like to proceed?",
                "Select",
                [{"title": "Proceed Options", "rows": [
                    {"id": "MODE_ONLINE", "title": "Online"},
                    {"id": "MODE_WALKIN", "title": "Walk-In"},
                    {"id": "MODE_FIELD",  "title": "Field Visit"}
                ]}]
            )
        elif state in ("STATE_4_DATE", "STATE_4_LATER_WEEK", "STATE_4_NEXT_WEEK"):
            now = datetime.now()
            date_rows = []
            if now.hour < 18:
                date_rows.append({"id": "DATE_TODAY", "title": "Today"})
            date_rows += [
                {"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}
            ]
            send_list_message(phone, "When would you prefer your appointment?", "Select",
                              [{"title": "Preferred Date", "rows": date_rows}])
        elif state == "STATE_4_SLOT":
            _action_send_slot_list(phone)
        elif state == "STATE_4_NAME":
            send_whatsapp_message(phone, "Please confirm your name:")
        elif state == "STATE_4_ADDRESS":
            send_whatsapp_message(
                phone,
                "Please share the address where our advisor should visit:"
            )
        elif state == "INSURANCE_ESTIMATE_NAME":
            send_whatsapp_message(phone, "Please share your full name:")
    except Exception as e:
        print(f"[insurance] _reprompt_current_field error: {e}")


# ============================
# MID-FLOW KNOWLEDGE QUERY INTERCEPTOR
# The single most important function for UX intelligence.
#
# When a customer is in the MIDDLE of a form flow (renewal/estimate)
# and asks a knowledge question ("what is NCB?", "what happens if I
# didn't claim?", "how is premium calculated?"), this function:
#   1. Detects it is a knowledge question (not a form answer)
#   2. Answers it via the AI expert
#   3. Re-prompts the SAME form field so the flow continues seamlessly
#
# This runs at the TOP of every mid-flow state handler, BEFORE any
# form validation, refusal check, or exit-intent detection.
# ============================

# All mid-flow states and the human-readable question they are waiting for.
# Used to build the context-aware re-prompt after answering a query.
_MID_FLOW_STATE_CONTEXT = {
    "STATE_4_RENEW_TYPE":    "renewal type",
    "STATE_4_RENEW_TYPE_OTHER": "custom renewal type",
    "STATE_4_MODE":          "appointment mode (Online / Walk-In / Field Visit)",
    "STATE_4_DATE":          "preferred appointment date",
    "STATE_4_LATER_WEEK":    "specific date this week",
    "STATE_4_NEXT_WEEK":     "specific date next week",
    "STATE_4_SLOT":          "preferred time slot",
    "STATE_4_NAME":          "your name",
    "STATE_4_ADDRESS":       "your address for the field visit",
    "INSURANCE_ESTIMATE_NAME": "your full name",
}


def _handle_mid_flow_knowledge_query(phone: str, text: str, state: str) -> bool:
    """
    Intercept knowledge/informational questions asked mid-flow.

    Returns True  → query was handled; caller must return immediately
                    (the AI answered it AND re-prompted the form field)
    Returns False → not a knowledge question; caller proceeds normally

    This is the fix for scenarios like:
      - Customer is in STATE_4_RENEW_TYPE and asks "what is NCB?"
      - Customer is in INSURANCE_ESTIMATE_NAME and asks "how is premium calculated?"
      - Customer is in STATE_4_NAME and asks "what happens if I didn't claim last year?"

    In ALL these cases the bot should:
      1. Answer the question intelligently
      2. Then re-ask the same form field it was waiting for
    """

    # ── Step 1: Quick guard — is this even worth checking? ────────────────
    # If it looks like a valid form answer for this state, don't intercept.
    t = text.strip()
    t_lower = t.lower()

    # Very short inputs (1-2 words) without a question mark are almost
    # certainly form answers, not knowledge questions. Fast-exit.
    words = t_lower.split()
    has_question_mark = "?" in t
    if len(words) <= 2 and not has_question_mark:
        return False

    # ── Step 2: Detect if this is a knowledge/informational question ──────
    # We use a layered approach: keyword patterns first (fast, no API cost),
    # then LLM if ambiguous.

    # Pattern 1: starts with or contains a clear question phrase
    knowledge_starters = [
        "what is", "what are", "what does", "what do", "what will", "what happens",
        "how is", "how are", "how does", "how do", "how much", "how many", "how to",
        "why is", "why are", "why does", "why do",
        "when is", "when are", "when does", "when do",
        "which is", "which are",
        "explain", "tell me about", "tell me what", "describe",
        "can you explain", "can you tell", "could you explain",
        "difference between", "compare", "what's the difference",
        "is it", "is there", "are there", "does it", "do i",
        "should i", "would i", "will i", "can i",
        "what if", "what about",
    ]
    is_knowledge_by_pattern = (
        has_question_mark or
        any(t_lower.startswith(starter) for starter in knowledge_starters) or
        any(f" {starter}" in t_lower for starter in knowledge_starters) or
        _is_insurance_knowledge_question(text)
    )

    # Pattern 2: Explicitly NOT a knowledge question — it's a form input
    # (e.g. the user typed a name, address, slot, etc.)
    if state == "STATE_4_NAME" or state == "INSURANCE_ESTIMATE_NAME":
        # If it looks like a name (passes name validator), skip
        if _is_likely_name(text):
            return False

    if state == "STATE_4_ADDRESS":
        # Addresses are usually 5+ words with no question pattern
        if len(words) >= 3 and not is_knowledge_by_pattern:
            return False

    if not is_knowledge_by_pattern:
        # Do a lightweight LLM check for longer ambiguous sentences
        if len(words) >= 4:
            try:
                check_prompt = f"""Is this customer message a KNOWLEDGE/INFORMATIONAL QUESTION about insurance,
or is it a FORM INPUT (name, address, date preference, slot selection)?

MESSAGE: "{text}"
CURRENT STEP: The bot is waiting for: {_MID_FLOW_STATE_CONTEXT.get(state, 'a response')}

KNOWLEDGE QUESTION examples: "what is NCB?", "how does premium work?",
"what happens if I didn't claim?", "explain zero depreciation",
"is third party enough?", "what is IDV?"

FORM INPUT examples (for their respective steps):
- name step: "John Smith", "Rahul Kumar"
- slot step: "morning", "anytime", "evening"
- mode step: "online", "walk in"
- renewal type: "3rd renewal", "first time"

Answer ONLY with JSON: {{"is_knowledge_question": true/false, "confidence": 0.0-1.0}}"""
                resp = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[{"role": "user", "content": check_prompt}],
                    temperature=0.1,
                    max_tokens=50,
                    response_format={"type": "json_object"}
                )
                result = json.loads(resp.choices[0].message.content.strip())
                is_knowledge_by_pattern = (
                    result.get("is_knowledge_question", False) and
                    float(result.get("confidence", 0.0)) >= 0.75
                )
                print(f"[insurance] 🔍 Mid-flow knowledge LLM check: {result} | '{text}'")
            except Exception as e:
                print(f"[insurance] Mid-flow knowledge LLM check error: {e}")

    if not is_knowledge_by_pattern:
        return False  # Not a knowledge question — let the state handler deal with it

    # ── Step 3: It IS a knowledge question — answer it via AI expert ──────
    print(f"[insurance] 💡 Mid-flow knowledge query intercepted | state={state} | '{text}'")

    vehicle_reg = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")

    try:
        from ai import handle_insurance_ai_query
        ai_reply, follow_on = handle_insurance_ai_query(phone, text, vehicle_reg)
        # Send the AI answer
        send_whatsapp_message(phone, ai_reply)

        if follow_on and follow_on != "NONE":
            print(f"[insurance] 💡 AI suggested follow-on: {follow_on} | switching flow...")
            _trigger_follow_on(phone, follow_on, vehicle_reg)
            return True  # Handled — follow-on triggered
    except Exception as e:
        print(f"[insurance] Mid-flow AI answer error: {e}")
        # Fallback: give a generic helpful reply
        send_whatsapp_message(
            phone,
            "🛡️ Great question! I'd be happy to help with that.\n\n"
            "For detailed information, our insurance advisor can assist you — "
            "please continue with the form and ask our advisor directly.\n\n"
        )

    # ── Step 4: Re-prompt the SAME form field ─────────────────────────────
    # After answering the question, remind the customer what they were doing
    # and re-ask the form field. State does NOT change.
    context = _MID_FLOW_STATE_CONTEXT.get(state, "")
    if context:
        send_whatsapp_message(
            phone,
            f"Now, coming back to where we were — to continue with your booking, "
            f"please provide *{context}* 👇"
        )
    _reprompt_current_field(phone, state)

    return True  # Handled — caller must return immediately

def _normalize_insurance_text(text: str) -> str:
    t = (text or "").lower()
    corrections = {
        "renwal": "renew",
        "renwe": "renew",
        "renw": "renew",
        "estimtae": "estimate",
        "estmate": "estimate",
        "estimat": "estimate",
        "estimte": "estimate",
        "quate": "quote",
        "quoute": "quote",
        "polciy": "policy",
        "premuim": "premium",
        "prmium": "premium",
    }
    for wrong, right in corrections.items():
        t = t.replace(wrong, right)
    return t.strip()


def _is_insurance_knowledge_question(text: str) -> bool:
    """Return True for informational insurance questions that should not trigger a flow."""
    return is_insurance_knowledge_query(text)


# AI INTENT DETECTION FUNCTION
# ============================
def detect_insurance_intent_with_ai(phone, user_text, vehicle_reg=None):
    """
    Use Groq AI to analyze user intent and decide which flow to route to.
    Works in ALL states — not just INSURANCE_VEHICLE_FOUND.
    """
    try:
        vehicle_data = None
        if vehicle_reg:
            vehicle_data = get_vehicle_from_db(vehicle_reg)

        vehicle_context = ""
        if vehicle_data:
            vehicle_context = f"""
Vehicle Registration: {vehicle_data.get('VehicleRegnNo', 'N/A')}
Vehicle Model: {vehicle_data.get('submodel', 'N/A')} ({vehicle_data.get('yearofmanufacture', 'N/A')})
Last Policy: {vehicle_data.get('policyno', 'N/A')} ({vehicle_data.get('insurancecompany', 'N/A')})
Policy Type: {vehicle_data.get('policytype', 'N/A')}
Last Renewal: {vehicle_data.get('policyissuedate', 'N/A')}
Next Renewal Due: {vehicle_data.get('policyexpirydate', 'N/A')}
Renewal Type: {vehicle_data.get('renewaltype', 'N/A')}
"""

        # Get recent conversation from MongoDB for context
        conversation_context = ""
        try:
            from chat_history import db as mongo_db
            chat_sessions = mongo_db["chat_sessions"]
            session = chat_sessions.find_one(
                {"user_phone": phone, "status": "active"},
                sort=[("start_time", -1)]
            )
            if session and "messages" in session:
                recent_msgs = session["messages"][-3:]
                if recent_msgs:
                    conversation_context = "Recent conversation:\n"
                    for msg in recent_msgs:
                        sender = "User" if msg.get("sender") == "user" else "Bot"
                        conversation_context += f"{sender}: {msg.get('text', '')}\n"
        except Exception as _e:
            print(f"[insurance] MongoDB context error: {_e}")

        if _is_insurance_knowledge_question(user_text):
            return {
                "intent": "GENERAL_QUERY",
                "confidence": 0.99,
                "explanation": "General insurance knowledge question"
            }

        system_prompt = """You are an AI intent detector for an automobile insurance chatbot called AutoSherpa.

Your task: Analyze the user's message and determine their INTENT.

CRITICAL RULE — Distinguish between ACTION intents and KNOWLEDGE intents:
- ACTION intents: User explicitly wants to DO something (renew, get a quote, see history)
- KNOWLEDGE intents: User is ASKING A QUESTION about insurance concepts → always GENERAL_QUERY

AVAILABLE INTENTS:
1. INSURANCE_HISTORY  - User explicitly wants to VIEW their own past policies/records
2. GET_ESTIMATE       - User explicitly wants a QUOTE/ESTIMATE for THEIR specific vehicle (not general cost questions)
3. RENEW_INSURANCE    - User explicitly wants to RENEW their own policy (not just asking what renewal means)
4. TALK_TO_ADVISOR    - User wants to speak with a human advisor/agent
5. END_CHAT           - User wants to end/stop/exit/bye
6. PREVIOUS_MENU      - User wants to go back to previous menu / main menu
7. GENERAL_QUERY      - ANY question about insurance concepts, coverage types, how things work,
                        what terms mean, how premiums are calculated in general, etc.
                        When in doubt, use GENERAL_QUERY.

Return ONLY this JSON format:
{
    "intent": "one of the above intent names",
    "confidence": 0.0-1.0,
    "explanation": "brief reason",
    "extracted_info": {}
}

EXAMPLES — GENERAL_QUERY (knowledge questions, NOT actions):
- "What is comprehensive insurance?"          → GENERAL_QUERY
- "What is NCB?"                              → GENERAL_QUERY
- "How is premium calculated?"                → GENERAL_QUERY
- "What does IDV mean?"                       → GENERAL_QUERY
- "What is zero depreciation?"                → GENERAL_QUERY
- "How much does insurance cost?"             → GENERAL_QUERY (general knowledge, not requesting a quote)
- "What is covered under third party?"        → GENERAL_QUERY
- "How do I file a claim?"                    → GENERAL_QUERY
- "What happens if policy lapses?"            → GENERAL_QUERY

EXAMPLES — ACTION intents:
- "I want to renew my insurance"              → RENEW_INSURANCE
- "Renew my policy"                           → RENEW_INSURANCE
- "Get me an estimate for my car"             → GET_ESTIMATE
- "I want a quote for my vehicle"             → GET_ESTIMATE
- "Show me my past policies"                  → INSURANCE_HISTORY
- "Talk to customer care"                     → TALK_TO_ADVISOR
- "Go back" / "Main menu"                     → PREVIOUS_MENU
- "Bye" / "Exit" / "Stop"                    → END_CHAT
"""

        prompt = f"""
{system_prompt}

VEHICLE CONTEXT (if available):
{vehicle_context}

CONVERSATION CONTEXT:
{conversation_context}

CURRENT USER MESSAGE: "{user_text}"

Analyze this message and return ONLY the JSON response with intent classification.
"""

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content.strip())
        print(f"🎯 AI Intent Detection Result: {result}")
        return result

    except Exception as e:
        print(f"❌ AI Intent Detection Error: {e}")
        return {
            "intent": "GENERAL_QUERY",
            "confidence": 0.5,
            "explanation": "AI detection failed, using fallback",
            "extracted_info": {}
        }


# ============================
# CENTRAL AI INTENT ROUTER
# Called from ALL states when free-text is received
# ============================
def _keyword_intent_fallback(text: str) -> str:
    """
    Simple keyword-based intent detection used when Groq AI is unavailable.
    Returns one of the same intent strings so the router still works correctly.

    IMPORTANT: Only route to GET_ESTIMATE / RENEW_INSURANCE when the user
    explicitly signals they WANT to perform that action on their specific vehicle.
    General insurance knowledge questions ("what is NCB?", "how is premium calculated?")
    should return GENERAL_QUERY so the AI expert answers them properly.
    """
    t = _normalize_insurance_text(text)

    advisor_kw = ["advisor", "adviser", "agent", "customer care", "talk to", "speak to",
                  "human", "support", "contact", "call", "person", "staff",
                  "connect me", "real person", "speak with"]

    # Explicit renewal ACTION keywords — broad natural language coverage
    renew_kw   = [
        # Core
        "renew my", "renew my insurance", "renew insurance", "renew my policy",
        "want to renew", "want to renew my", "i want to renew", "i want renew",
        "renewal of my", "renewal for my",
        "renew now", "renew today", "renewal request", "request renewal",
        "need renewal", "need to renew", "policy renew", "renew policy",
        "policy renew karna", "new policy for", "buy insurance for",
        "purchase policy", "policy lena",
        # Broader natural language
        "looking to renew", "looking for renewal", "planning to renew",
        "help me renew", "help with renewal", "start renewal", "do renewal",
        "begin renewal", "start my renewal", "renew my vehicle",
        "my insurance is expiring", "my policy is expiring",
        "my insurance expired", "my policy expired",
        "insurance due", "policy is due", "due for renewal", "up for renewal",
        # Hindi
        "renwal karna", "renwal chahiye", "renew karna", "renew chahiye",
        "insurance renew", "policy renew karna hai",
    ]

    # Explicit estimate REQUEST keywords — broad natural language coverage
    estimate_kw = [
        # Core
        "get estimate", "get an estimate", "get a estimate",
        "request estimate", "request a quote", "request quote",
        "need estimate", "need a quote", "want estimate", "want a quote",
        "want an estimate", "i want estimate", "i want a quote", "i want quote",
        "send estimate", "give me estimate", "give me a quote",
        "calculate premium for my", "how much for my car",
        "how much for my vehicle", "quote for my car", "quote for my vehicle",
        "estimate for my car", "estimate for my vehicle", "premium estimate",
        "insurance estimate for my",
        # Broader natural language
        "get me estimate", "get me quote", "get me a quote",
        "i need an estimate", "i need estimate", "i need a quote",
        "can i get estimate", "can i get a quote", "can i get quote",
        "can you give estimate", "can you give me estimate",
        "can you give quote", "can you give me quote",
        "please give estimate", "please give quote",
        "how much will it cost", "how much is insurance",
        "how much will insurance cost", "how much will the premium be",
        "what will be the premium", "what is the premium for my",
        "calculate my premium", "calculate insurance premium",
        "insurance cost for my", "premium for my",
        # Hindi
        "estimate chahiye", "quote chahiye", "premium kitna hoga",
        "kitna premium", "kitna lagega",
    ]

    history_kw = ["history", "past policy", "previous policy", "old policy", "my records",
                  "insurance history", "policy history", "show my policy", "show my insurance"]
    end_kw     = ["bye", "exit", "stop", "end", "close", "done", "finish", "thank you", "thanks"]
    menu_kw    = ["back", "menu", "main menu", "go back", "previous", "return", "home"]

    if any(k in t for k in advisor_kw):
        return "TALK_TO_ADVISOR"
    if any(k in t for k in renew_kw):
        return "RENEW_INSURANCE"
    if any(k in t for k in estimate_kw):
        return "GET_ESTIMATE"
    if any(k in t for k in history_kw):
        return "INSURANCE_HISTORY"
    if any(k in t for k in end_kw):
        return "END_CHAT"
    if any(k in t for k in menu_kw):
        return "PREVIOUS_MENU"
    # Default: treat as a general knowledge query — let the AI answer it
    return "GENERAL_QUERY"


def _trigger_follow_on(phone: str, follow_on: str, vehicle_reg: str):
    """
    Triggered after an AI reply is sent.  Routes to the correct sub-flow.
    Mid-flow guard: never hijacks active form states.
    """
    print(f"[insurance] _trigger_follow_on | phone={phone} | action={follow_on}")

    _mid_flow_states = {
        "STATE_4_RENEW_TYPE", "STATE_4_RENEW_TYPE_OTHER", "STATE_4_MODE", "STATE_4_DATE",
        "STATE_4_LATER_WEEK", "STATE_4_NEXT_WEEK", "STATE_4_SLOT",
        "STATE_4_NAME", "STATE_4_ADDRESS", "STATE_4_DONE",
        "INSURANCE_ESTIMATE_NAME", "INSURANCE_ESTIMATE_LINK_SENT",
    }
    current_state = USER_STATE.get(phone, {}).get("state", "")

    if follow_on == "RENEW_INSURANCE":
        _action_start_renewal(phone)

    elif follow_on == "GET_ESTIMATE":
        _action_start_estimate(phone)

    elif follow_on == "INSURANCE_HISTORY":
        if vehicle_reg:
            _action_insurance_history(phone, vehicle_reg)
        else:
            send_whatsapp_message(
                phone,
                "Please enter your *Vehicle Registration Number* to view your policy history.\n"
                "Example: *KA01AB1234*"
            )

    elif follow_on == "TALK_TO_ADVISOR":
        USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
        _action_send_advisor(phone)

    elif follow_on == "END_CHAT":
        USER_STATE.pop(phone, None)
        send_whatsapp_message(
            phone,
            "\u2705 *Thank you for choosing AutoSherpa Insurance Desk \U0001f64f*\n\n"
            "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
            "\U0001f697 Have a great day!"
        )

    elif follow_on == "PREVIOUS_MENU":
        reg_no        = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
        vehicle_found = USER_STATE.get(phone, {}).get("data", {}).get("vehicle_found")
        if vehicle_found is True and reg_no:
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
                return
        if vehicle_found is False and reg_no:
            USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
            _action_show_not_found_menu(phone, reg_no)
            return
        USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
        USER_STATE[phone]["data"]  = {}
        handle_insurance(phone)

    else:
        # NONE or unknown — do not automatically re-show the menu after a
        # pure knowledge response. Let the user continue from the AI answer.
        print(f"[insurance] _trigger_follow_on: no follow-on action for {follow_on}")
        return


def _execute_intent(phone, intent, text, vehicle_reg):
    """Execute the given intent — shared by AI router and keyword fallback."""
    if intent == "INSURANCE_HISTORY":
        if vehicle_reg:
            _action_insurance_history(phone, vehicle_reg)
        else:
            send_whatsapp_message(phone, "Please enter your vehicle registration number first.")

    elif intent == "RENEW_INSURANCE":
        _action_start_renewal(phone)

    elif intent == "GET_ESTIMATE":
        _action_start_estimate(phone)

    elif intent == "TALK_TO_ADVISOR":
        USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
        _action_send_advisor(phone)

    elif intent == "END_CHAT":
        USER_STATE.pop(phone, None)
        send_whatsapp_message(
            phone,
            "✅ *Thank you for choosing AutoSherpa Insurance Desk 🙏*\n\n"
            "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
            "🚗 Have a great day!"
        )

    elif intent == "PREVIOUS_MENU":
        reg_no        = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
        vehicle_found = USER_STATE.get(phone, {}).get("data", {}).get("vehicle_found")
        if vehicle_found is True and reg_no:
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
                return
        if vehicle_found is False and reg_no:
            USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
            _action_show_not_found_menu(phone, reg_no)
            return
        USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
        USER_STATE[phone]["data"]  = {}
        handle_insurance(phone)

    else:
        # GENERAL_QUERY — answer with AI expert, then trigger contextually
        # relevant follow-on flow action based on what the query was about.
        # No hardcoding: the follow-on is determined by _detect_follow_on_action()
        # inside handle_insurance_ai_query() using keyword + AI classification.
        from utils import is_genuine_query
        state = USER_STATE.get(phone, {}).get("state", "")
        if is_genuine_query(text, state):
            try:
                from ai import handle_insurance_ai_query
                ai_reply, follow_on = handle_insurance_ai_query(phone, text, vehicle_reg)
                send_whatsapp_message(phone, ai_reply)
                print(f"[insurance] 🔀 Follow-on after AI reply: {follow_on}")
                if follow_on and follow_on != "NONE":
                    _trigger_follow_on(phone, follow_on, vehicle_reg)
            except Exception as _e:
                print(f"[insurance] AI general query error: {_e}")
                reg_no = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
                if reg_no:
                    vehicle = get_vehicle_from_db(reg_no)
                    if vehicle:
                        USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                        _action_show_main_menu(phone, vehicle)
                        return
                send_whatsapp_message(
                    phone,
                    "Sorry, I didn't understand that. Please choose an option from the menu."
                )
        else:
            # Not a genuine query — re-prompt with menu or current step context
            reg_no = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
            if reg_no:
                vehicle = get_vehicle_from_db(reg_no)
                if vehicle:
                    USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                    _action_show_main_menu(phone, vehicle)
                else:
                    _action_show_not_found_menu(phone, reg_no)
            else:
                send_whatsapp_message(
                    phone,
                    "Please select an option from the menu above, or type *Hi* to start over."
                )


def _route_via_ai(phone, text, state):
    """
    Detects intent with semantic keyword + LLM and routes to the correct sub-flow.
    Called from every state when the input is not a recognised button/list ID —
    so the user can type naturally at any point.

    ROUTING ORDER (strict priority):
      1. Explicit keyword intent  — classify_insurance_intent_request()
         catches direct action phrases like "renew my insurance",
         "i want to renew", "get estimate", "i want a quote", etc.
      2. LLM semantic intent classifier  — catches paraphrases and
         natural language that keywords miss ("my policy is up for renewal",
         "can I get a price for my car", etc.)
      3. Knowledge-query gate  — is_insurance_knowledge_query() check.
         Only reach this if neither keyword nor LLM detected an action.
      4. Genuine-query gate  — is_genuine_query() for AI fallback.
      5. AI expert answer  — handle_insurance_ai_query() for knowledge Qs.

    If Groq AI is unavailable, falls back to keyword matching so the
    user is NEVER shown a 'temporarily unavailable' error.
    """
    from utils import is_genuine_query

    vehicle_reg = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")

    # ── Pre-check: Knowledge query gate ──────────────────────────────────
    # If the message is clearly an informational/knowledge question
    # (e.g. "what is the premium amount and how it is calculated"),
    # skip ALL intent detection and route straight to GENERAL_QUERY.
    # This prevents the LLM from misclassifying knowledge questions as
    # action intents (GET_ESTIMATE, RENEW_INSURANCE) and launching flows.
    #
    # We ONLY do this when classify_insurance_intent_request() also returns
    # None — meaning there is no explicit action keyword in the message.
    # If the user typed a clear action phrase ("get estimate", "renew now"),
    # that keyword check takes priority over the knowledge gate.
    if _is_insurance_knowledge_question(text):
        explicit_action = classify_insurance_intent_request(text)
        if not explicit_action:
            print(f"[insurance] ⚡ Knowledge pre-check → GENERAL_QUERY (skipping LLM intent) | '{text}'")
            _execute_intent(phone, "GENERAL_QUERY", text, vehicle_reg)
            return

    # ── Step 1: Fast keyword intent — authoritative, no LLM cost ─────────
    explicit_intent = classify_insurance_intent_request(text)
    if explicit_intent:
        print(f"[insurance] ✅ Keyword action intent: {explicit_intent} | '{text}'")
        # Pre-fill date preference from natural language renewal messages
        if explicit_intent == "RENEW_INSURANCE":
            t_lower = text.lower()
            date_pref = None
            if any(p in t_lower for p in ["later this week", "later in the week", "this week"]):
                date_pref = "DATE_LATER_WEEK"
            elif any(p in t_lower for p in ["next week", "following week", "coming week"]):
                date_pref = "DATE_NEXT_WEEK"
            elif any(p in t_lower for p in ["tomorrow", "tmrw"]):
                date_pref = "DATE_TOMORROW"
            elif any(p in t_lower for p in ["today", "now", "asap"]):
                date_pref = "DATE_TODAY"
            if date_pref:
                USER_STATE.setdefault(phone, {})
                USER_STATE[phone].setdefault("renew_prefill", {})
                USER_STATE[phone]["renew_prefill"]["date_choice"] = date_pref
                print(f"[insurance] Pre-filling date preference: {date_pref}")
        _execute_intent(phone, explicit_intent, text, vehicle_reg)
        return

    # ── Step 2: LLM semantic intent classifier ────────────────────────────
    # Catches natural language paraphrases that keyword lists miss.
    # e.g. "my insurance is about to expire, help me renew",
    #      "can I get a price for my vehicle's insurance",
    #      "i want to start the renewal process"
    try:
        lm_intent = _classify_intent_with_llm(phone, text, vehicle_reg)
        if lm_intent and lm_intent != "GENERAL_QUERY":
            print(f"[insurance] 🤖 LLM action intent: {lm_intent} | '{text}'")
            # Pre-fill date preference for LLM-detected renewals too
            if lm_intent == "RENEW_INSURANCE":
                t_lower = text.lower()
                date_pref = None
                if any(p in t_lower for p in ["later this week", "this week"]):
                    date_pref = "DATE_LATER_WEEK"
                elif any(p in t_lower for p in ["next week", "coming week"]):
                    date_pref = "DATE_NEXT_WEEK"
                elif any(p in t_lower for p in ["tomorrow", "tmrw"]):
                    date_pref = "DATE_TOMORROW"
                elif any(p in t_lower for p in ["today", "now", "asap"]):
                    date_pref = "DATE_TODAY"
                if date_pref:
                    USER_STATE.setdefault(phone, {})
                    USER_STATE[phone].setdefault("renew_prefill", {})
                    USER_STATE[phone]["renew_prefill"]["date_choice"] = date_pref
            _execute_intent(phone, lm_intent, text, vehicle_reg)
            return
    except Exception as llm_err:
        print(f"[insurance] LLM semantic intent error: {llm_err}")

    # ── Step 3: Knowledge-query override ─────────────────────────────────
    # Only reach here if neither keyword nor LLM found an action.
    kw_intent = _keyword_intent_fallback(text)
    if _is_insurance_knowledge_question(text):
        print(f"[insurance] Knowledge question detected → GENERAL_QUERY | '{text}'")
        kw_intent = "GENERAL_QUERY"

    if kw_intent != "GENERAL_QUERY":
        print(f"[insurance] 🔑 Keyword fallback intent: {kw_intent} | '{text}'")
        _execute_intent(phone, kw_intent, text, vehicle_reg)
        return

    # ── Step 4: Genuine-query gate ────────────────────────────────────────
    if not is_genuine_query(text, state):
        print(f"[insurance] _route_via_ai: blocked non-query input | state={state} | text='{text}'")
        reg_no = USER_STATE.get(phone, {}).get("data", {}).get("reg_no")
        if reg_no:
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
            else:
                _action_show_not_found_menu(phone, reg_no)
        else:
            send_whatsapp_message(
                phone,
                "Please select an option from the menu, or type *Hi* to start over."
            )
        return

    # ── Step 5: AI expert knowledge answer ───────────────────────────────
    _execute_intent(phone, "GENERAL_QUERY", text, vehicle_reg)


def _classify_intent_with_llm(phone: str, text: str, vehicle_reg: str = None) -> str | None:
    """
    Use Groq LLM to semantically classify the customer's message into an
    insurance flow intent.

    Returns one of:
      "RENEW_INSURANCE"  — customer wants to renew / buy a policy
      "GET_ESTIMATE"     — customer wants a premium quote / estimate
      "INSURANCE_HISTORY"— customer wants their past policy records
      "TALK_TO_ADVISOR"  — customer wants a human agent
      "GENERAL_QUERY"    — customer is asking an informational question
      None               — LLM call failed or returned low confidence

    This is called AFTER the keyword check fails, so only ambiguous or
    paraphrased inputs reach here.
    """
    try:
        prompt = f"""You are an intent classifier for a vehicle insurance WhatsApp chatbot.

Classify the customer message into exactly ONE intent.

CUSTOMER MESSAGE: "{text}"

INTENT DEFINITIONS:
- RENEW_INSURANCE   : Customer explicitly wants to RENEW OR BUY their own vehicle insurance policy RIGHT NOW.
                      They say things like: "i want to renew my insurance", "help me renew my policy",
                      "my policy expired please renew", "i need a new policy for my car",
                      "can you renew my insurance", "i want to buy insurance"
- GET_ESTIMATE      : Customer explicitly wants YOU TO GENERATE a premium quote/estimate for THEIR specific vehicle RIGHT NOW.
                      They say: "give me a quote for my car", "i want an estimate for my vehicle",
                      "calculate insurance for my car", "send me a quote", "get me an estimate"
- INSURANCE_HISTORY : Customer wants to see their own past policies or insurance records.
                      They say: "show me my past policies", "what was my last premium paid", "my insurance history"
- TALK_TO_ADVISOR   : Customer wants to speak with a human OR has an active accident/claim.
                      They say: "connect me to an advisor", "i had an accident", "file a claim"
- GENERAL_QUERY     : Customer is asking an INFORMATIONAL or KNOWLEDGE question — they want to LEARN or UNDERSTAND something.
                      They are NOT requesting an action on their specific vehicle.

⚠️ CRITICAL DISTINCTION — GENERAL_QUERY examples (NOT action intents):
- "what is the premium amount" → GENERAL_QUERY (asking what premium is, not requesting a quote)
- "what is the premium amount and how it is calculated" → GENERAL_QUERY
- "how is premium calculated" → GENERAL_QUERY (informational)
- "how is insurance premium determined" → GENERAL_QUERY
- "how much does insurance cost" → GENERAL_QUERY (general knowledge question)
- "what is comprehensive insurance" → GENERAL_QUERY
- "what is NCB?" → GENERAL_QUERY
- "explain IDV" → GENERAL_QUERY
- "what is third party insurance?" → GENERAL_QUERY
- "how does zero depreciation work?" → GENERAL_QUERY
- "what are the factors that affect premium?" → GENERAL_QUERY
- "what is covered under my policy?" → GENERAL_QUERY
- "how does the claim process work?" → GENERAL_QUERY

KEY RULE: If the customer is ASKING A QUESTION to understand something → GENERAL_QUERY.
Only use GET_ESTIMATE when they clearly say they WANT A QUOTE GENERATED for their OWN vehicle.
Only use RENEW_INSURANCE when they clearly say they WANT TO RENEW right now.

Respond ONLY with valid JSON (no other text):
{{"intent": "<INTENT>", "confidence": <0.0-1.0>}}"""

        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=60,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        intent     = result.get("intent", "GENERAL_QUERY")
        confidence = float(result.get("confidence", 0.0))

        print(f"[insurance] 🤖 LLM intent classify: {intent} ({confidence:.2f}) | '{text}'")

        valid = {"RENEW_INSURANCE", "GET_ESTIMATE", "INSURANCE_HISTORY",
                 "TALK_TO_ADVISOR", "GENERAL_QUERY"}
        if intent not in valid:
            return None

        # Only trust the LLM if it's confident enough
        # For action intents, require >= 0.65 confidence
        # For GENERAL_QUERY, require >= 0.70 (we'd rather ask AI than miss an action)
        if intent == "GENERAL_QUERY" and confidence >= 0.70:
            return "GENERAL_QUERY"
        if intent != "GENERAL_QUERY" and confidence >= 0.65:
            return intent

        return None  # Low confidence — fall through to keyword fallback

    except Exception as e:
        print(f"[insurance] _classify_intent_with_llm error: {e}")
        return None


# ============================
# SHARED ACTION HELPERS
# ============================

def _action_show_main_menu(phone, vehicle):
    send_list_message(
        phone,
        f"Great! ✅ I found your vehicle in our system:\n\n"
        f"🚘 {vehicle.get('submodel', 'N/A')} – {vehicle.get('yearofmanufacture', 'N/A')}\n"
        f"🔢 Reg No: {vehicle.get('VehicleRegnNo', 'N/A')}\n\n"
        f"📄 *Policy Information*\n\n"
        f"📝 Last Policy No: {vehicle.get('policyno', 'N/A')} ({vehicle.get('insurancecompany', 'N/A')})\n"
        f"📅 Last Renewal Date: {format_date_only(vehicle.get('policyissuedate'))}\n"
        f"🏷 Current Coverage: {vehicle.get('policytype', 'N/A')}\n"
        f"📅 Next Renewal Due: {format_date_only(vehicle.get('policyexpirydate'))}\n"
        f"🏷 Next Renewal Type: {vehicle.get('renewaltype', 'N/A')}\n\n"
        f"How can I assist you today?",
        "Choose an option",
        [{
            "title": "Insurance Services",
            "rows": [
                {"id": "1", "title": "📜 Insurance History"},
                {"id": "2", "title": "📄 Get Insurance Estimate"},
                {"id": "3", "title": "🔄 Renew Insurance"},
                {"id": "5", "title": "❌ End Chat"}
            ]
        }]
    )


def _action_show_not_found_menu(phone, reg_no):
    send_list_message(
        phone,
        f"❌ *Insurance Record Not Found*\n\n"
        f"We couldn't find any insurance records for *{reg_no}* in our system.\n\n"
        f"This may be because:\n"
        f"• You are a new customer\n"
        f"• Your insurance was done elsewhere\n"
        f"• The registration number needs verification\n\n"
        f"💡 *No worries — I can still help you with the following:*",
        "Choose an option",
        [{
            "title": "Available Options",
            "rows": [
                {"id": "1", "title": "📄 Get Insurance Estimate"},
                {"id": "2", "title": "🔄 Renew Insurance(New)"},
                {"id": "3", "title": "🔁 Re-enter Registration"},
                {"id": "5", "title": "❌ End Chat"}
            ]
        }]
    )


def _action_insurance_history(phone, reg_no):
    history = get_insurance_history(reg_no)
    msg = "📄 *Here are your recent insurance policies with us* 👇\n\n"
    if history:
        for row in history:
            msg += (
                f"📝 *Policy No:* {row.get('policyno', 'N/A')}\n"
                f"🏢 *Insurer:* {row.get('insurancecompany', 'N/A')}\n"
                f"📅 *Renewal Date:* {format_date_only(row.get('policyissuedate'))}\n"
                f"💵 *Total Premium:* ₹{row.get('grosstotalpremium', 'N/A')}\n\n"
            )
    else:
        msg += "No insurance history found.\n\n"
    msg += "Please choose an option below 👇"

    send_list_message(
        phone,
        msg,
        "Choose an option",
        [{
            "title": "Available Options",
            "rows": [
                {"id": "3",             "title": "🔄 Renew Insurance"},
                {"id": "2",             "title": "📄 Get Insurance Estimate"},
                {"id": "PREVIOUS_MENU", "title": "⬅ Previous Menu"},
                {"id": "END_CHAT",      "title": "❌ End Chat"}
            ]
        }]
    )
    try:
        from ai import store_interaction
        store_interaction(phone, "📜 Insurance History", msg, "insurance", vehicle_reg=reg_no)
    except Exception as _e:
        print(f"[insurance] store_interaction error: {_e}")


def _action_start_estimate(phone):
    USER_STATE[phone]["state"] = "INSURANCE_ESTIMATE_NAME"
    send_whatsapp_message(phone, "Please share your full name:")


def _action_start_renewal(phone):
    USER_STATE[phone]["state"] = "STATE_4_RENEW_TYPE"
    reg = USER_STATE[phone].get("data", {}).get("reg_no", "N/A")
    USER_STATE[phone]["renew"] = {"vehicle": reg, "mobile": phone}
    send_list_message(
        phone,
        "Please select your renewal type:",
        "Select Renewal Type",
        [{
            "title": "Renewal Type",
            "rows": [
                {"id": "RENEW_FIRST",     "title": "First Renewal"},
                {"id": "RENEW_2ND",       "title": "2nd Renewal"},
                {"id": "RENEW_3RD",       "title": "3rd Renewal"},
                {"id": "RENEW_4TH",       "title": "4th Renewal"},
                {"id": "RENEW_5TH",       "title": "5th Renewal"},
                {"id": "RENEW_6TH_ABOVE", "title": "6th Renewal"},
                {"id": "RENEW_NEW",       "title": "New Policy / First Time"},
                {"id": "RENEW_OTHER",     "title": "Other (Type Manually)"}
            ]
        }]
    )
    try:
        from ai import store_interaction
        store_interaction(phone, "🔄 Renew Insurance", "Renewal type selection menu shown",
                          "insurance", vehicle_reg=reg)
    except Exception as _e:
        print(f"[insurance] store_interaction error: {_e}")


def _action_send_advisor(phone):
    # Save current state before switching to confirmation
    USER_STATE[phone]["_saved_state"] = USER_STATE[phone].get("state")
    # Use the centralized bot logic since it's already implemented
    from flows.service import bot
    bot.trigger_human_agent_confirmation(phone)
    # Customize the state for insurance
    USER_STATE[phone]["state"] = "INSURANCE_AGENT_CONFIRMATION"


def _action_send_slot_list(phone):
    send_list_message(
        phone,
        "Please choose a time slot:",
        "Select Time Slot",
        [{
            "title": "Time Slots",
            "rows": [
                {"id": "SLOT_MORNING",   "title": "Morning (10 AM - 12 PM)"},
                {"id": "SLOT_AFTERNOON", "title": "Afternoon (12 PM - 4 PM)"},
                {"id": "SLOT_EVENING",   "title": "Evening (4 PM - 7 PM)"},
                {"id": "SLOT_ANYTIME",   "title": "Anytime"}
            ]
        }]
    )


def _action_finish_renewal(phone):
    renew = USER_STATE[phone].get("renew", {})

    # ── Guard: ensure all required fields are present ────────────────────
    required = ["renewal_type", "mode", "date", "slot", "name"]
    missing  = [f for f in required if not renew.get(f)]
    if missing:
        print(f"[insurance] _action_finish_renewal: missing fields {missing}")
        send_whatsapp_message(
            phone,
            f"\u26a0\ufe0f Something went wrong collecting your details.\n"
            f"Missing: {', '.join(missing)}\n\n"
            "Let's try again. Please type *Renew* to restart."
        )
        USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
        return

    date_show = renew.get("date_display", renew.get("date", "N/A"))
    addr_line = (
        f"\n\U0001f4cd *Address:* {renew['address']}"
        if renew.get("address") else ""
    )
    mode_emoji = {
        "Online":     "\U0001f4bb Online",
        "Walk-In":    "\U0001f6aa Walk-In",
        "Field Visit": "\U0001f697 Field Visit"
    }.get(renew.get("mode", ""), renew.get("mode", "N/A"))

    summary = (
        "🎉 *Your Insurance Renewal is Confirmed!* 🎯\n\n"
        "📋 *Booking Summary*\n"
        "--------------------------------\n"
        f"👤 *Name:* {renew.get('name', 'N/A')}\n"
        f"📱 *Mobile:* {format_mobile(phone)}\n"
        f"🚗 *Vehicle:* {renew.get('vehicle', 'N/A')}\n"
        f"🔄 *Renewal:* {renew.get('renewal_type', 'N/A')}\n"
        f"💻 *Mode:* {mode_emoji}\n"
        f"📅 *Date:* {date_show}\n"
        f"⏰ *Slot:* {renew.get('slot', 'N/A')}"
        f"{addr_line}\n"
        "--------------------------------\n\n"
        "✅ Our advisor will call you shortly to confirm.\n"
        "📞 *Need help?* Type *Hi* anytime."
    )

    renew["mobile"] = phone
    save_renewal_to_db(renew)

    send_button_message(
        phone,
        summary,
        [
            {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "\u2b05 Main Menu"}},
            {"type": "reply", "reply": {"id": "END_CHAT",      "title": "\u274c End Chat"}}
        ]
    )
    try:
        from ai import store_interaction
        store_interaction(phone,
                          f"Renewal confirmed - {renew.get('renewal_type', 'N/A')}",
                          summary, "insurance",
                          vehicle_reg=renew.get("vehicle"))
    except Exception as _e:
        print(f"[insurance] store_interaction error: {_e}")


# ============================
# ENTRY POINT
# ============================
def handle_insurance(phone):
    USER_STATE.setdefault(phone, {})
    USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
    USER_STATE[phone]["data"]  = {}

    send_whatsapp_message(
        phone,
        "👋 Welcome to *AutoSherpa Insurance Desk*\n\n"
        "Thank you for contacting us.\n"
        "We're here to assist you with all your insurance-related needs, including:\n\n"
        "• *Policy Renewal*\n"
        "• *Insurance Estimate*\n"
        "• *Insurance History*\n"
        "• *Claims Support*\n\n"
        "Please enter your *Vehicle Registration Number* to continue.\n"
        "Example: KA01AB1234"
    )


# ============================
# HELPER FUNCTIONS
# ============================
def generate_upload_token(phone: str, name: str, vehicle: str) -> str:
    token_val = str(uuid.uuid4())
    if phone not in USER_STATE:
        USER_STATE[phone] = {}
    USER_STATE[phone]["upload_token"] = {
        "token":     token_val,
        "name":      name,
        "vehicle":   vehicle,
        "timestamp": time.time()
    }
    print(f"✅ Generated upload token for {phone}: {token_val}")
    return token_val


def get_remaining_week_dates():
    today     = datetime.now().date()
    start_day = today + timedelta(days=2)
    days_until_sunday = 6 - start_day.weekday()
    if days_until_sunday < 0:
        days_until_sunday = 0
    end_day = start_day + timedelta(days=days_until_sunday)
    dates, current = [], start_day
    while current <= end_day:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def get_next_week_dates():
    today = datetime.now().date()
    days_until_next_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_until_next_monday)
    return [next_monday + timedelta(days=i) for i in range(7)]


def format_date_only(dt):
    if not dt:
        return "N/A"
    if isinstance(dt, date):
        return dt.strftime("%d-%m-%Y")
    try:
        return datetime.strptime(str(dt), "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y")
    except Exception:
        try:
            return datetime.strptime(str(dt), "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return str(dt)


def format_mobile(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("91") and len(phone) == 12:
        return f"+91-{phone[2:]}"
    elif len(phone) == 10:
        return f"+91-{phone}"
    return phone


def save_renewal_to_db(data: dict):
    try:
        # ── Validate required fields before insert ──────────────────────
        required = ["mobile", "vehicle", "name", "renewal_type", "mode", "date", "slot"]
        missing  = [f for f in required if not data.get(f)]
        if missing:
            print(f"[insurance] save_renewal_to_db: skipping — missing {missing}")
            return

        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO insurance_renewal_requests
            (mobile_number, vehicle_reg_no, customer_name, renewal_type,
             appointment_mode, appointment_date, appointment_time, address, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (
            data.get("mobile"),
            data.get("vehicle"),
            data.get("name"),
            data.get("renewal_type"),
            data.get("mode"),
            data.get("date"),
            data.get("slot"),
            data.get("address", None)
        ))
        conn.commit()
        cursor.close()
        conn.close()
        print("\u2705 Renewal request saved to DB")
        notify_advisor_of_renewal(data)
    except Exception as e:
        print("\u274c Error saving renewal to DB:", e)


def notify_advisor_of_renewal(data: dict):
    """
    Sends a WhatsApp notification to the advisor when a renewal is booked.
    Advisor phone is read from env var ADVISOR_PHONE.
    Silently skips if not configured.
    """
    advisor_phone = os.getenv("ADVISOR_PHONE")
    if not advisor_phone:
        print("[insurance] notify_advisor_of_renewal: ADVISOR_PHONE not set, skipping.")
        return
    try:
        addr_line = f"\n\U0001f4cd Address: {data['address']}" if data.get("address") else ""
        msg = (
            "\U0001f6a8 *New Renewal Booking Alert!*\n"
            "\u2508" * 18 + "\n"
            f"\U0001f464 Customer: {data.get('name', 'N/A')}\n"
            f"\U0001f4f1 Mobile:   {data.get('mobile', 'N/A')}\n"
            f"\U0001f697 Vehicle:  {data.get('vehicle', 'N/A')}\n"
            f"\U0001f504 Renewal:  {data.get('renewal_type', 'N/A')}\n"
            f"\U0001f5a5\ufe0f  Mode:     {data.get('mode', 'N/A')}\n"
            f"\U0001f4c5 Date:     {data.get('date', 'N/A')}\n"
            f"\u23f0 Slot:     {data.get('slot', 'N/A')}"
            f"{addr_line}\n"
            "\u2508" * 18 + "\n"
            "Please follow up with the customer."
        )
        send_whatsapp_message(advisor_phone, msg)
        print(f"\u2705 Advisor notified at {advisor_phone}")
    except Exception as e:
        print(f"[insurance] notify_advisor_of_renewal error: {e}")


def get_user_from_token(phone: str, token_val: str):
    if phone not in USER_STATE:
        print(f"❌ User state not found for {phone}")
        return None
    token_data = USER_STATE[phone].get("upload_token")
    if not token_data:
        print(f"❌ No upload token found for {phone}")
        return None
    if token_data["token"] != token_val:
        print(f"❌ Token mismatch for {phone}")
        return None
    if time.time() - token_data["timestamp"] > 3600:
        print(f"❌ Token expired for {phone}")
        USER_STATE[phone].pop("upload_token", None)
        return None
    print(f"✅ Valid token for {phone}")
    return token_data


# ============================
# MAIN FLOW HANDLER
# ============================
def insurance_flow_handler(phone, text):
    from utils import is_genuine_query
    # ── Normalise interactive payload ───────────────────────────────────
    if isinstance(text, dict):
        if "interactive" in text:
            if "list_reply" in text["interactive"]:
                text = text["interactive"]["list_reply"]["id"]
            elif "button_reply" in text["interactive"]:
                text = text["interactive"]["button_reply"]["id"]
        else:
            text = ""

    text  = (text or "").strip()
    state = USER_STATE.get(phone, {}).get("state", "")

    print(f"[DEBUG] insurance_flow_handler | phone={phone} | text='{text}' | state='{state}'")

    USER_STATE.setdefault(phone, {})

    # ── Global: redisplay menu ──────────────────────────────────────────
    if text == "__SHOW_MENU__":
        print(f"[DEBUG] __SHOW_MENU__ triggered | phone={phone} | state={state}")
        # Use the same logic as PREVIOUS_MENU to decide which menu to show
        data = USER_STATE[phone].get("data", {})
        reg_no = data.get("reg_no")
        vehicle_found = data.get("vehicle_found")
        
        if vehicle_found is None:
            if "ESTIMATE" in state or "STATE_4" in state:
                reg_no = reg_no or USER_STATE[phone].get("renew", {}).get("vehicle")
                if reg_no:
                    vehicle = get_vehicle_from_db(reg_no)
                    vehicle_found = (vehicle is not None)
        
        if vehicle_found is True and reg_no:
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
                return
        
        if vehicle_found is False and reg_no:
            USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
            _action_show_not_found_menu(phone, reg_no)
            return
            
        # Default fallback to welcome
        USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
        handle_insurance(phone)
        return

    # ── Global: document uploaded confirmation ──────────────────────────
    if text == "__DOCUMENT_UPLOADED__":
        send_button_message(
            phone,
            "✅ *Thank you for your request!*\n\n"
            "We have successfully received your *insurance estimate request*.\n\n"
            "🔍 *What happens next?*\n"
            "✔ Our advisor will review your policy\n"
            "✔ Compare best insurer options\n"
            "✔ Apply eligible discounts / NCB\n"
            "✔ Contact you within *2 hours*\n\n"
            "📞 Need any assistance?",
            [
                {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅ Previous Menu"}},
                {"type": "reply", "reply": {"id": "END_CHAT",      "title": "❌ End Conversation"}}
            ]
        )
        return

    # ── Global: PREVIOUS_MENU button ────────────────────────────────────
    if text == "PREVIOUS_MENU":
        print(f"[DEBUG] PREVIOUS_MENU clicked | phone={phone} | state={state}")
        data = USER_STATE[phone].get("data", {})
        reg_no        = data.get("reg_no")
        vehicle_found = data.get("vehicle_found")
        
        # If vehicle_found is missing but we are in an estimate/renewal flow, 
        # try to recover context from the state or renew dict.
        if vehicle_found is None:
            if "ESTIMATE" in state or "STATE_4" in state:
                # If we have a reg_no, check if it's in DB
                if reg_no:
                    vehicle = get_vehicle_from_db(reg_no)
                    vehicle_found = (vehicle is not None)
                else:
                    # Check renew dict
                    reg_no = USER_STATE[phone].get("renew", {}).get("vehicle")
                    if reg_no:
                        vehicle = get_vehicle_from_db(reg_no)
                        vehicle_found = (vehicle is not None)

        print(f"[DEBUG] PREVIOUS_MENU Resolved: reg_no={reg_no}, vehicle_found={vehicle_found}")

        if vehicle_found is True and reg_no:
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
                return

        if vehicle_found is False and reg_no:
            # If we are in a COMPLETED state or ALREADY in the not found menu, go back to lookup (Welcome)
            if state in ("STATE_4_DONE", "INSURANCE_NOT_FOUND", "INSURANCE_ESTIMATE_LINK_SENT"):
                USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
                USER_STATE[phone]["data"]  = {}
                handle_insurance(phone)
                return
            
            # Otherwise, go back to the not found menu
            USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
            _action_show_not_found_menu(phone, reg_no)
            return

        # Fallback: no vehicle context or not found above
        USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
        USER_STATE[phone]["data"]  = {}
        handle_insurance(phone)
        return

    # ── Global: END_CHAT button ─────────────────────────────────────────
    if text == "END_CHAT":
        print(f"[DEBUG] END_CHAT clicked | phone={phone}")
        USER_STATE.pop(phone, None)
        send_whatsapp_message(
            phone,
            "✅ *Thank you for choosing AutoSherpa Insurance Desk 🙏*\n\n"
            "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
            "🚗 Have a great day!"
        )
        return

    # ── HUMAN AGENT CONFIRMATION ────────────────────────────────────────
    if state == "INSURANCE_AGENT_CONFIRMATION":
        from flows.service import bot
        if text == "AGENT_YES":
            reg_no = USER_STATE[phone].get("data", {}).get("reg_no")
            bot.initiate_agent_handoff(phone, vehicle_reg=reg_no,
                                       handoff_reason="Customer requested live agent from Insurance flow")
            return
        elif text == "AGENT_NO":
            # Restore the saved state and re-prompt the customer
            prev_state = USER_STATE[phone].pop("_saved_state", None)
            if prev_state and prev_state != "INSURANCE_AGENT_CONFIRMATION":
                USER_STATE[phone]["state"] = prev_state
                send_whatsapp_message(phone, "✅ No problem! Let's continue where we left off.")
                from webhook import _reprompt_flow_state
                _reprompt_flow_state(phone, prev_state)
            else:
                # Fallback if no state was saved
                USER_STATE[phone]["state"] = "INSURANCE_START"
                handle_insurance(phone)
            return
        else:
            # Unknown input — re-show the confirmation prompt
            from flows.service import bot as _bot
            _bot.trigger_human_agent_confirmation(phone)
            USER_STATE[phone]["state"] = "INSURANCE_AGENT_CONFIRMATION"
            return

    # ════════════════════════════════════════════════════════════════════
    # STATE MACHINE
    # Each state first checks for recognised button/list IDs.
    # If none match → _route_via_ai() handles free-text intelligently.
    # ════════════════════════════════════════════════════════════════════

    # ── VEHICLE LOOKUP ───────────────────────────────────────────────────
    if state == "INSURANCE_LOOKUP":
        is_valid, cleaned_reg = is_valid_vehicle_reg(text)
        if is_valid:
            reg_no = cleaned_reg
            USER_STATE[phone]["data"]["reg_no"] = reg_no
            vehicle = get_vehicle_from_db(reg_no)
            if vehicle:
                USER_STATE[phone]["data"]["vehicle_found"] = True
                USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                _action_show_main_menu(phone, vehicle)
            else:
                USER_STATE[phone]["data"]["vehicle_found"] = False
                USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
                _action_show_not_found_menu(phone, reg_no)
        else:
            if is_genuine_query(text, state):
                _route_via_ai(phone, text, state)
            else:
                send_whatsapp_message(
                    phone,
                    "⚠️ The registration number you entered does not match standard Indian formats.\n\n"
                    "Please provide a valid registration number to continue.\n\n"
                    "Examples:\n"
                    "• Standard: *KA01AB1234* or *MH 01 AB 1234*\n"
                    "• Bharat Series: *22BH1234AA*\n"
                    "• Commercial: *DL 1T 2468*"
                )
        return

    # ── VEHICLE FOUND — main menu ────────────────────────────────────────
    if state == "INSURANCE_VEHICLE_FOUND":
        if text == "1":
            reg_no = USER_STATE[phone]["data"]["reg_no"]
            _action_insurance_history(phone, reg_no)
        elif text == "2":
            _action_start_estimate(phone)
        elif text == "3":
            _action_start_renewal(phone)
        elif text == "4":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
        elif text == "5":
            USER_STATE.pop(phone, None)
            send_whatsapp_message(
                phone,
                "✅ *Thank you for choosing AutoSherpa Insurance Desk 🙏*\n\n"
                "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
                "🚗 Have a great day!"
            )
        else:
            # Free text → AI routes
            _route_via_ai(phone, text, state)
        return

    # ── VEHICLE NOT FOUND — limited menu ────────────────────────────────
    if state == "INSURANCE_NOT_FOUND":
        if text == "1":
            _action_start_estimate(phone)
        elif text == "2":
            _action_start_renewal(phone)
        elif text == "3":
            USER_STATE[phone]["state"] = "INSURANCE_LOOKUP"
            send_whatsapp_message(
                phone,
                "🔁 Please re-enter your Vehicle Registration Number\nExample: KA01AB1234"
            )
        elif text == "4":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
        elif text == "5":
            USER_STATE.pop(phone, None)
            send_whatsapp_message(
                phone,
                "✅ *Thank you for choosing AutoSherpa Insurance Desk 🙏*\n\n"
                "If you need any further assistance, feel free to message *Hi* anytime.\n\n"
                "🚗 Have a great day!"
            )
        else:
            # Free text input in NOT_FOUND state:
            # - If it's a genuine question → route to AI
            # - Check if it looks like a registration number
            # - Otherwise → re-show menu
            from utils import is_genuine_query
            if is_genuine_query(text, state):
                _route_via_ai(phone, text, state)
            else:
                cleaned = "".join(c for c in text if c.isalnum()).upper()
                if len(cleaned) >= 4:
                    reg_no = cleaned
                    USER_STATE[phone]["data"]["reg_no"] = reg_no
                    vehicle = get_vehicle_from_db(reg_no)
                    if vehicle:
                        USER_STATE[phone]["data"]["vehicle_found"] = True
                        USER_STATE[phone]["state"] = "INSURANCE_VEHICLE_FOUND"
                        _action_show_main_menu(phone, vehicle)
                    else:
                        USER_STATE[phone]["data"]["vehicle_found"] = False
                        USER_STATE[phone]["state"] = "INSURANCE_NOT_FOUND"
                        _action_show_not_found_menu(phone, reg_no)
                else:
                    # Not a genuine query and not a valid reg no — re-show the not found menu
                    reg_no = USER_STATE[phone].get("data", {}).get("reg_no", "")
                    _action_show_not_found_menu(phone, reg_no)
        return

    # ── ESTIMATE: collect customer name ──────────────────────────────────
    if state == "INSURANCE_ESTIMATE_NAME":
        # ── 1. Refusal check FIRST — before any name validation ──────────
        if _is_refusal(text):
            _offer_graceful_exit(
                phone,
                "No problem! 😊 Would you like to go back to the main menu or end the chat?"
            )
            return

        # ── 1b. Knowledge query interceptor ──────────────────────────────
        # If customer asks a question instead of giving their name,
        # answer it and re-ask for the name. State does NOT change.
        if not _is_likely_name(text) and _handle_mid_flow_knowledge_query(phone, text, state):
            return

        # ── 2. Mid-flow exit intent (cancel/back/end) ─────────────────────
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── 3. Valid name → proceed ───────────────────────────────────────
        # ── 3. Valid name → proceed ───────────────────────────────────────
        from utils import validate_and_clean_name
        is_valid, clean_name, fallback_msg = validate_and_clean_name(text)
        if is_valid:
            USER_STATE[phone]["data"]["customer_name"] = clean_name
            vehicle_reg      = USER_STATE[phone]["data"].get("reg_no", "N/A")
            tok              = generate_upload_token(phone, clean_name, vehicle_reg)
            formatted_mobile = format_mobile(phone)
            upload_link      = f"{BASE_URL}/upload-estimate?token={tok}&mobile={formatted_mobile}"

            message = (
                f"Thank you, {clean_name}! 📄\n\n"
                f"*Click this link to upload your document:*\n"
                f"🔗 {upload_link}\n\n"
                f"💡 *Your details are pre-filled:*\n"
                f"• Name: {clean_name}\n"
                f"• Mobile: {format_mobile(phone)}\n"
                f"• Vehicle: {vehicle_reg}\n\n"
                f"After uploading, we'll contact you within 2 hours! 🚗"
            )
            send_whatsapp_message(phone, message)
            send_button_message(
                phone,
                "To help our insurance advisor calculate the best estimate, "
                "please upload your previous insurance policy copy via the link above 👆",
                [
                    {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅ Back to Menu"}},
                    {"type": "reply", "reply": {"id": "END_CHAT",      "title": "❌ End Chat"}}
                ]
            )
            USER_STATE[phone]["state"] = "INSURANCE_ESTIMATE_LINK_SENT"
        else:
            # Not a valid name — check if it's a genuine question or route via AI
            from utils import is_genuine_query
            if is_genuine_query(text, state):
                _route_via_ai(phone, text, state)
            else:
                send_whatsapp_message(phone, fallback_msg)
        return

    # ── ESTIMATE: link sent — wait for upload or nav buttons ────────────
    if state == "INSURANCE_ESTIMATE_LINK_SENT":
        _route_via_ai(phone, text, state)
        return

    # ── ADVISOR state ────────────────────────────────────────────────────
    if state == "STATE_5_ADVISOR":
        # PREVIOUS_MENU / END_CHAT handled globally above.
        _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: renewal type ────────────────────────────────────────────
    if state == "STATE_4_RENEW_TYPE":
        # ── ALWAYS check exit intent FIRST ──────────────────────────────
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        # If customer asks a question mid-flow, answer it and re-prompt.
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        type_map = {
            "RENEW_FIRST":     "First Renewal",
            "RENEW_2ND":       "2nd Renewal",
            "RENEW_3RD":       "3rd Renewal",
            "RENEW_4TH":       "4th Renewal",
            "RENEW_5TH":       "5th Renewal",
            "RENEW_6TH_ABOVE": "6th Renewal",
            "RENEW_NEW":       "New Policy / First Time",
            "RENEW_OTHER":     "Other (Type Manually)",
            "First Renewal":                 "First Renewal",
            "2nd Renewal":                   "2nd Renewal",
            "3rd Renewal":                   "3rd Renewal",
            "4th Renewal":                   "4th Renewal",
            "5th Renewal":                   "5th Renewal",
            "6th Renewal":                   "6th Renewal",
            "New Policy / First Time":       "New Policy / First Time",
            "Other (Type Manually)": "Other (Type Manually)",
        }
        
        selected = type_map.get(text)
        
        # Free-text check for "other" or "specify" or "manually"
        if not selected:
            t = text.lower().strip()
            if any(k in t for k in ["other", "specify", "custom", "manually"]):
                selected = "Other (Type Manually)"
                
        if selected:
            if selected == "Other (Type Manually)":
                is_opt_selection = (text == "RENEW_OTHER" or text == "Other (Type Manually)" or text.lower().strip() in ["other", "specify", "custom", "manually"])
                if not is_opt_selection:
                    is_valid, formatted_val, _ = validate_and_format_renewal_type(text)
                    if is_valid:
                        USER_STATE[phone]["renew"]["renewal_type"] = formatted_val
                        USER_STATE[phone]["state"] = "STATE_4_MODE"
                        send_list_message(
                            phone,
                            f"✅ Got it — *{formatted_val}*.\n\nHow would you like to proceed?",
                            "Select",
                            [{"title": "Proceed Options", "rows": [
                                {"id": "MODE_ONLINE", "title": "Online"},
                                {"id": "MODE_WALKIN", "title": "Walk-In"},
                                {"id": "MODE_FIELD",  "title": "Field Visit"}
                            ]}]
                        )
                        return
                USER_STATE[phone]["state"] = "STATE_4_RENEW_TYPE_OTHER"
                send_whatsapp_message(phone, "✏️ Please type your renewal type:")
                return
                
            USER_STATE[phone]["renew"]["renewal_type"] = selected
            USER_STATE[phone]["state"] = "STATE_4_MODE"
            send_list_message(
                phone,
                "How would you like to proceed?",
                "Select",
                [{"title": "Proceed Options", "rows": [
                    {"id": "MODE_ONLINE", "title": "Online"},
                    {"id": "MODE_WALKIN", "title": "Walk-In"},
                    {"id": "MODE_FIELD",  "title": "Field Visit"}
                ]}]
            )
        else:
            # ── AI free-text renewal type resolver ───────────────────────
            resolved = _resolve_renewal_type_from_text(text)
            if resolved:
                selected = type_map.get(resolved)
                if selected:
                    if selected == "Other (Type Manually)":
                        is_opt_selection = (text == "RENEW_OTHER" or text == "Other (Type Manually)" or text.lower().strip() in ["other", "specify", "custom", "manually"])
                        if not is_opt_selection:
                            is_valid, formatted_val, _ = validate_and_format_renewal_type(text)
                            if is_valid:
                                USER_STATE[phone]["renew"]["renewal_type"] = formatted_val
                                USER_STATE[phone]["state"] = "STATE_4_MODE"
                                send_list_message(
                                    phone,
                                    f"✅ Got it — *{formatted_val}*.\n\nHow would you like to proceed?",
                                    "Select",
                                    [{"title": "Proceed Options", "rows": [
                                        {"id": "MODE_ONLINE", "title": "Online"},
                                        {"id": "MODE_WALKIN", "title": "Walk-In"},
                                        {"id": "MODE_FIELD",  "title": "Field Visit"}
                                    ]}]
                                )
                                return
                        USER_STATE[phone]["state"] = "STATE_4_RENEW_TYPE_OTHER"
                        send_whatsapp_message(phone, "✏️ Please type your renewal type:")
                        return
                    USER_STATE[phone]["renew"]["renewal_type"] = selected
                    USER_STATE[phone]["state"] = "STATE_4_MODE"
                    send_list_message(
                        phone,
                        f"✅ Got it — *{selected}*.\n\nHow would you like to proceed?",
                        "Select",
                        [{"title": "Proceed Options", "rows": [
                            {"id": "MODE_ONLINE", "title": "Online"},
                            {"id": "MODE_WALKIN", "title": "Walk-In"},
                            {"id": "MODE_FIELD",  "title": "Field Visit"}
                        ]}]
                    )
                    return
            
            # As a last fallback, try to validate the text directly as a custom renewal type
            is_valid, formatted_val, _ = validate_and_format_renewal_type(text)
            if is_valid:
                USER_STATE[phone]["renew"]["renewal_type"] = formatted_val
                USER_STATE[phone]["state"] = "STATE_4_MODE"
                send_list_message(
                    phone,
                    f"✅ Got it — *{formatted_val}*.\n\nHow would you like to proceed?",
                    "Select",
                    [{"title": "Proceed Options", "rows": [
                        {"id": "MODE_ONLINE", "title": "Online"},
                        {"id": "MODE_WALKIN", "title": "Walk-In"},
                        {"id": "MODE_FIELD",  "title": "Field Visit"}
                    ]}]
                )
                return

            # Could not resolve → show list again
            send_list_message(
                phone,
                "Please select your renewal type from the list:",
                "Select Renewal Type",
                [{"title": "Renewal Type", "rows": [
                    {"id": "RENEW_FIRST",     "title": "First Renewal"},
                    {"id": "RENEW_2ND",       "title": "2nd Renewal"},
                    {"id": "RENEW_3RD",       "title": "3rd Renewal"},
                    {"id": "RENEW_4TH",       "title": "4th Renewal"},
                    {"id": "RENEW_5TH",       "title": "5th Renewal"},
                    {"id": "RENEW_6TH_ABOVE", "title": "6th Renewal"},
                    {"id": "RENEW_NEW",       "title": "New Policy / First Time"},
                    {"id": "RENEW_OTHER",     "title": "Other (Type Manually)"}
                ]}]
            )
        return

    # ── RENEWAL: custom renewal type ─────────────────────────────────────
    if state == "STATE_4_RENEW_TYPE_OTHER":
        # ── ALWAYS check exit intent FIRST ──────────────────────────────
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        is_valid, formatted_val, fallback_msg = validate_and_format_renewal_type(text)
        if not is_valid:
            send_whatsapp_message(phone, fallback_msg)
            return

        USER_STATE[phone]["renew"]["renewal_type"] = formatted_val
        USER_STATE[phone]["state"] = "STATE_4_MODE"
        send_list_message(
            phone,
            "How would you like to proceed?",
            "Select",
            [{"title": "Proceed Options", "rows": [
                {"id": "MODE_ONLINE", "title": "Online"},
                {"id": "MODE_WALKIN", "title": "Walk-In"},
                {"id": "MODE_FIELD",  "title": "Field Visit"}
            ]}]
        )
        return

    # ── RENEWAL: mode ────────────────────────────────────────────────────
    if state == "STATE_4_MODE":
        # ── ALWAYS check exit intent FIRST ──────────────────────────────
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        mode_map = {
            "MODE_ONLINE":  "Online",
            "MODE_WALKIN":  "Walk-In",
            "MODE_FIELD":   "Field Visit",
            "Online":       "Online",
            "Walk-In":      "Walk-In",
            "Field Visit":  "Field Visit",
        }
        selected = mode_map.get(text)
        if selected:
            USER_STATE[phone]["renew"]["mode"] = selected
            USER_STATE[phone]["state"] = "STATE_4_DATE"
            # ── Check for pre-filled date from initial message ────────────
            prefill = USER_STATE[phone].pop("renew_prefill", {})
            prefilled_date = prefill.get("date_choice")
            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            now = datetime.now()
            date_rows = []
            if now.hour < 18:
                date_rows.append({"id": "DATE_TODAY", "title": "Today"})
            date_rows += [
                {"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}
            ]
            if prefilled_date:
                print(f"[insurance] Auto-applying pre-filled date: {prefilled_date}")
                send_whatsapp_message(phone, f"✅ Got it — *{selected}* mode.\n\nNow let's pick your preferred date 📅")
                insurance_flow_handler(phone, prefilled_date)
            else:
                send_list_message(
                    phone,
                    "When would you prefer your appointment?",
                    "Select",
                    [{"title": "Preferred Date", "rows": date_rows}]
                )
        else:
            # ── Refusal check ─────────────────────────────────────────────────
            if _is_refusal(text):
                _offer_graceful_exit(
                    phone,
                    "No problem! 😊 Would you like to go back to the main menu or end the chat?"
                )
                return
            # ── AI free-text mode resolver ────────────────────────────────
            t = text.lower().strip()
            resolved_mode = None
            if any(k in t for k in ["online", "virtual", "call", "video", "phone", "remote", "digital"]):
                resolved_mode = "MODE_ONLINE"
            elif any(k in t for k in ["walk", "in person", "visit", "showroom", "office", "offline", "come"]):
                resolved_mode = "MODE_WALKIN"
            elif any(k in t for k in ["field", "home", "house", "location", "doorstep", "my place"]):
                resolved_mode = "MODE_FIELD"

            if resolved_mode:
                selected = mode_map.get(resolved_mode)
                USER_STATE[phone]["renew"]["mode"] = selected
                USER_STATE[phone]["state"] = "STATE_4_DATE"
                # ── 6 PM cutoff: hide Today option after 6 PM ─────────────
                now = datetime.now()
                date_rows = []
                if now.hour < 18:
                    date_rows.append({"id": "DATE_TODAY", "title": "Today"})
                date_rows += [
                    {"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                    {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                    {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}
                ]
                send_list_message(
                    phone,
                    f"✅ Got it — *{selected}* mode.\n\nWhen would you prefer your appointment?",
                    "Select",
                    [{"title": "Preferred Date", "rows": date_rows}]
                )
            else:
                _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: date ────────────────────────────────────────────────────
    if state == "STATE_4_DATE":
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        today    = datetime.now().date()
        date_map = {
            "DATE_TODAY":      "Today",
            "DATE_TOMORROW":   "Tomorrow",
            "DATE_LATER_WEEK": "Later This Week",
            "DATE_NEXT_WEEK":  "Next Week",
            "Today":           "Today",
            "Tomorrow":        "Tomorrow",
            "Later This Week": "Later This Week",
            "Next Week":       "Next Week",
        }
        selected = date_map.get(text)

        if selected == "Later This Week":
            USER_STATE[phone]["state"] = "STATE_4_LATER_WEEK"
            rows = [{"id": f"WEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                    for d in get_remaining_week_dates()]
            send_list_message(phone, "Please choose a date:", "Select Date",
                              [{"title": "Select Date", "rows": rows}])
            return

        if selected == "Next Week":
            USER_STATE[phone]["state"] = "STATE_4_NEXT_WEEK"
            rows = [{"id": f"NEXTWEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                    for d in get_next_week_dates()]
            send_list_message(phone, "Please choose a date:", "Select Date",
                              [{"title": "Select Date", "rows": rows}])
            return

        if selected in ("Today", "Tomorrow"):
            actual_date = (today if selected == "Today" else today + timedelta(days=1))
            # ── After-6PM auto-advance ────────────────────────────
            from utils import get_smart_default_date
            if selected == "Today":
                actual_date, was_redirected = get_smart_default_date()
                if was_redirected:
                    actual_date_str = actual_date.strftime("%d-%m-%Y")
                    USER_STATE[phone]["renew"]["date"]         = actual_date_str
                    USER_STATE[phone]["renew"]["date_display"] = actual_date_str
                    USER_STATE[phone]["state"] = "STATE_4_SLOT"
                    send_whatsapp_message(phone,
                        f"⏰ Since it's after 6 PM, same-day appointments are unavailable.\n\n"
                        f"✅ I've scheduled your appointment for *tomorrow ({actual_date_str})* instead.\n\n"
                        f"Please choose a preferred time slot 👇")
                    _action_send_slot_list(phone)
                    return
            valid, reason = is_valid_appointment_slot(actual_date)
            if not valid:
                send_whatsapp_message(phone, reason)
                now = datetime.now()
                date_rows = []
                if now.hour < 18: date_rows.append({"id": "DATE_TODAY", "title": "Today"})
                date_rows += [{"id": "DATE_TOMORROW",   "title": "Tomorrow"},
                              {"id": "DATE_LATER_WEEK", "title": "Later this Week"},
                              {"id": "DATE_NEXT_WEEK",  "title": "Next Week"}]
                send_list_message(phone, "Please choose a different date:", "Select",
                                  [{"title": "Preferred Date", "rows": date_rows}])
                return
            actual_date_str = actual_date.strftime("%d-%m-%Y")
            USER_STATE[phone]["renew"]["date"]         = actual_date_str
            USER_STATE[phone]["renew"]["date_display"] = actual_date_str
            USER_STATE[phone]["state"] = "STATE_4_SLOT"
            _action_send_slot_list(phone)
            return

        # ── Smart Date Resolver (Free-text) ──
        all_dates = sorted(list(set([today, today + timedelta(days=1)] + get_remaining_week_dates() + get_next_week_dates())))
        resolved_id = resolve_date_from_text(text, all_dates, prefix="RESOLVED_DATE_")
        
        if resolved_id:
            date_val = resolved_id.replace("RESOLVED_DATE_", "")
            actual_date = datetime.strptime(date_val, "%Y%m%d").date()
            
            # Cutoff Check
            if actual_date == today and datetime.now().hour >= 18:
                actual_date = today + timedelta(days=1)
                actual_date_str = actual_date.strftime("%d-%m-%Y")
                USER_STATE[phone]["renew"]["date"]         = actual_date_str
                USER_STATE[phone]["renew"]["date_display"] = actual_date_str
                USER_STATE[phone]["state"] = "STATE_4_SLOT"
                send_whatsapp_message(phone, f"⏰ Since it's after 6 PM, we've moved your appointment to *tomorrow ({actual_date_str})*.")
                _action_send_slot_list(phone)
                return

            valid, reason = is_valid_appointment_slot(actual_date)
            if not valid:
                send_whatsapp_message(phone, reason)
                return
            actual_date_str = actual_date.strftime("%d-%m-%Y")
            USER_STATE[phone]["renew"]["date"]         = actual_date_str
            USER_STATE[phone]["renew"]["date_display"] = actual_date_str
            USER_STATE[phone]["state"] = "STATE_4_SLOT"
            send_whatsapp_message(phone, f"✅ Got it — *{actual_date_str}*.")
            _action_send_slot_list(phone)
            return
        
        _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: specific date from "later this week" ────────────────────
    if state == "STATE_4_LATER_WEEK":
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if not text.startswith("WEEK_") and _handle_mid_flow_knowledge_query(phone, text, state):
            return

        if text.startswith("WEEK_"):
            d = datetime.strptime(text.replace("WEEK_", ""), "%Y%m%d").date()
            selected_date = d.strftime("%d-%m-%Y")
            USER_STATE[phone]["renew"]["date"]         = selected_date
            USER_STATE[phone]["renew"]["date_display"] = selected_date
            USER_STATE[phone]["state"] = "STATE_4_SLOT"
            _action_send_slot_list(phone)
        else:
            # ── AI free-text date resolver ────────────────────────────────
            # Handles inputs like "24", "Wednesday", "24th", "24 march" etc.
            available = get_remaining_week_dates()
            resolved  = resolve_date_from_text(text, available, "WEEK_")
            if resolved and resolved.startswith("WEEK_"):
                d = datetime.strptime(resolved.replace("WEEK_", ""), "%Y%m%d").date()
                selected_date = d.strftime("%d-%m-%Y")
                USER_STATE[phone]["renew"]["date"]         = selected_date
                USER_STATE[phone]["renew"]["date_display"] = selected_date
                USER_STATE[phone]["state"] = "STATE_4_SLOT"
                send_whatsapp_message(phone, f"✅ Got it — *{d.strftime('%A, %d %B')}* selected.")
                _action_send_slot_list(phone)
            else:
                rows = [{"id": f"WEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                        for d in available]
                if rows:
                    send_list_message(phone,
                        "Please choose a date from the list below 👇",
                        "Select Date",
                        [{"title": "Select Date", "rows": rows}])
                else:
                    _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: specific date from "next week" ──────────────────────────
    if state == "STATE_4_NEXT_WEEK":
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if not text.startswith("NEXTWEEK_") and _handle_mid_flow_knowledge_query(phone, text, state):
            return

        if text.startswith("NEXTWEEK_"):
            d = datetime.strptime(text.replace("NEXTWEEK_", ""), "%Y%m%d").date()
            selected_date = d.strftime("%d-%m-%Y")
            USER_STATE[phone]["renew"]["date"]         = selected_date
            USER_STATE[phone]["renew"]["date_display"] = selected_date
            USER_STATE[phone]["state"] = "STATE_4_SLOT"
            _action_send_slot_list(phone)
        else:
            # ── AI free-text date resolver ────────────────────────────────
            # Handles inputs like "24", "Monday", "24th", "24 march" etc.
            available = get_next_week_dates()
            resolved  = resolve_date_from_text(text, available, "NEXTWEEK_")
            if resolved and resolved.startswith("NEXTWEEK_"):
                d = datetime.strptime(resolved.replace("NEXTWEEK_", ""), "%Y%m%d").date()
                selected_date = d.strftime("%d-%m-%Y")
                USER_STATE[phone]["renew"]["date"]         = selected_date
                USER_STATE[phone]["renew"]["date_display"] = selected_date
                USER_STATE[phone]["state"] = "STATE_4_SLOT"
                send_whatsapp_message(phone, f"✅ Got it — *{d.strftime('%A, %d %B')}* selected.")
                _action_send_slot_list(phone)
            else:
                rows = [{"id": f"NEXTWEEK_{d.strftime('%Y%m%d')}", "title": d.strftime("%A (%d %b)")}
                        for d in available]
                if rows:
                    send_list_message(phone,
                        "Please choose a date from the list below 👇",
                        "Select Date",
                        [{"title": "Select Date", "rows": rows}])
                else:
                    _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: time slot ─────────────────────────────────────────────────
    if state == "STATE_4_SLOT":
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        slot_map = {
            "SLOT_MORNING":   "Morning (10 AM - 12 PM)",
            "SLOT_AFTERNOON": "Afternoon (12 PM - 4 PM)",
            "SLOT_EVENING":   "Evening (4 PM - 7 PM)",
            "SLOT_ANYTIME":   "Anytime",
            "Morning (10-12)":  "Morning (10 AM - 12 PM)",
            "Afternoon (12-4)": "Afternoon (12 PM - 4 PM)",
            "Evening (4-7)":    "Evening (4 PM - 7 PM)",
            "Anytime":          "Anytime",
        }
        selected = slot_map.get(text)
        if selected:
            from utils import is_booking_date_today, is_slot_available_today
            if is_booking_date_today(phone) and not is_slot_available_today(text, selected):
                send_whatsapp_message(phone, "⚠️ That time slot has already passed for today. Please choose an available slot:")
                _action_send_slot_list(phone)
                return
            USER_STATE[phone]["renew"]["slot"] = selected
            USER_STATE[phone]["state"] = "STATE_4_NAME"
            send_whatsapp_message(phone, "Please confirm your name:")
        else:
            # ── Free-text slot resolver ───────────────────────────────────
            t = text.lower().strip()
            resolved_slot = None
            if any(k in t for k in ["anytime", "any time", "flexible", "any slot",
                                     "doesn't matter", "doesnt matter", "no preference",
                                     "fine for me", "works for me", "any", "suitable"]):
                resolved_slot = "SLOT_ANYTIME"
            elif any(k in t for k in ["morning", "10 am", "10am", "before noon",
                                       "early", "am slot", "forenoon"]):
                resolved_slot = "SLOT_MORNING"
            elif any(k in t for k in ["afternoon", "12 pm", "12pm", "1 pm", "2 pm",
                                       "3 pm", "after noon", "lunch", "midday", "noon"]):
                resolved_slot = "SLOT_AFTERNOON"
            elif any(k in t for k in ["evening", "4 pm", "5 pm", "6 pm", "4pm",
                                       "5pm", "6pm", "after 4", "late"]):
                resolved_slot = "SLOT_EVENING"

            if resolved_slot:
                selected = slot_map.get(resolved_slot)
                from utils import is_booking_date_today, is_slot_available_today
                if is_booking_date_today(phone) and not is_slot_available_today(resolved_slot, selected):
                    send_whatsapp_message(phone, "⚠️ That time slot has already passed for today. Please choose an available slot:")
                    _action_send_slot_list(phone)
                    return
                USER_STATE[phone]["renew"]["slot"] = selected
                USER_STATE[phone]["state"] = "STATE_4_NAME"
                send_whatsapp_message(phone, f"✅ Got it — *{selected}*.\n\nPlease confirm your name:")
            else:
                _route_via_ai(phone, text, state)
        return

    # ── RENEWAL: name ────────────────────────────────────────────────────
    if state == "STATE_4_NAME":
        # ── ALWAYS check exit intent FIRST ──────────────────────────────
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "GET_ESTIMATE":
            _action_start_estimate(phone)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        # Only run if text doesn't look like a name (avoids double-checking names)
        if not _is_likely_name(text) and _handle_mid_flow_knowledge_query(phone, text, state):
            return

        name_input = text.strip()
        from utils import validate_and_clean_name
        is_valid, clean_name, fallback_msg = validate_and_clean_name(name_input)

        if is_valid:
            USER_STATE[phone]["renew"]["name"] = clean_name
            if "FIELD" in USER_STATE[phone]["renew"].get("mode", "").upper():
                USER_STATE[phone]["state"] = "STATE_4_ADDRESS"
                send_whatsapp_message(
                    phone,
                    f"Thank you, {clean_name}! 👤\n\n"
                    "Since you selected *Field Visit*, please share the address where our advisor should visit:"
                )
            else:
                USER_STATE[phone]["state"] = "STATE_4_DONE"
                _action_finish_renewal(phone)
        else:
            # _is_likely_name already catches refusals via _is_refusal(),
            # but the text failed both name-check and _is_refusal — check one more time
            # with LLM before blindly re-prompting.
            if _is_refusal(text):
                _handle_mid_flow_exit(phone, "CANCEL")
                return
            if is_genuine_query(text, state):
                _route_via_ai(phone, text, state)
            else:
                send_whatsapp_message(phone, fallback_msg)
        return

    # ── RENEWAL: address for field visit ────────────────────────────
    if state == "STATE_4_ADDRESS":
        exit_intent = _detect_mid_flow_intent(text)
        if exit_intent in ("CANCEL", "BACK", "END_CHAT"):
            _handle_mid_flow_exit(phone, exit_intent)
            return
        if exit_intent == "TALK_TO_ADVISOR":
            USER_STATE[phone]["state"] = "STATE_5_ADVISOR"
            _action_send_advisor(phone)
            return

        # ── Knowledge query interceptor ─────────────────────────────────
        if _handle_mid_flow_knowledge_query(phone, text, state):
            return

        if len(text) >= 5:
            USER_STATE[phone]["renew"]["address"] = text.strip()
            USER_STATE[phone]["state"] = "STATE_4_DONE"
            _action_finish_renewal(phone)
        else:
            send_whatsapp_message(
                phone,
                "⚠️ Please enter a complete address (minimum 5 characters).\n"
                "Example: 123 Main Street, Bangalore"
            )
        return

    # ── RENEWAL: done ────────────────────────────────────────────────────
    if state == "STATE_4_DONE":
        # PREVIOUS_MENU / END_CHAT handled globally above.
        _route_via_ai(phone, text, state)
        return

    # ── Catch-all ────────────────────────────────────────────────────────
    _route_via_ai(phone, text, state)


# ============================
# DB FUNCTIONS
# ============================
def get_vehicle_from_db(reg_no):
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            ir.VehicleRegnNo,
            ir.policyno,
            ir.policytype,
            ir.policyissuedate,
            ifd.policyexpirydate,
            ifd.renewaltype,
            ir.insurancecompany,
            ir.submodel,
            ir.yearofmanufacture
        FROM individualreport ir
        LEFT JOIN insuranceforecasteddata ifd
               ON ir.VehicleRegnNo = ifd.VehicleRegnNo
        WHERE REPLACE(ir.VehicleRegnNo, ' ', '') = %s
        LIMIT 1
    """, (reg_no,))
    data = cursor.fetchone()
    conn.close()
    return data


def get_insurance_history(reg_no):
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            policyno,
            insurancecompany,
            policyissuedate,
            grosstotalpremium
        FROM individualreport
        WHERE VehicleRegnNo = %s
        ORDER BY policyissuedate DESC
        LIMIT 5
    """, (reg_no,))
    data = cursor.fetchall()
    conn.close()
    return data