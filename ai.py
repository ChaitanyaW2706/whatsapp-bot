# ai.py
# ============================================================
# Central AI module for AutoSherpa WhatsApp Bot.
#
# KEY FIXES IN THIS VERSION:
#   1. _store_conversation() now inserts TWO separate rows:
#        Row 1 → sender_type='user'  (user_message only, bot_response=NULL)
#        Row 2 → sender_type='bot'   (bot_response only, user_message=NULL)
#   2. store_interaction() is a public helper — call this from ALL flows
#      (insurance, sales, used_cars) after EVERY button/menu reply so that
#      button clicks are also logged correctly in conversation_log.
#   3. handle_insurance_ai_query() now receives vehicle_reg correctly from
#      USER_STATE[phone]["data"]["reg_no"] (fix applied in insurance.py).
#   4. All 4 flow AI functions: insurance, sales, used_cars, service.
# ============================================================

import os
import json
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv
from vector_db import vector_service
from utils import normalize_text, classify_insurance_intent_request, is_insurance_knowledge_query

load_dotenv()

# ── Groq client ────────────────────────────────────────────
from llm_config import groq_client as client, MODEL_NAME


# ════════════════════════════════════════════════════════════
# 1.  ORIGINAL FUNCTION — completely untouched
# ════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════
# KEYWORD-BASED FALLBACKS — used when Groq is unavailable
# These ensure users always get a helpful response, never
# "temporarily unavailable" error messages.
# ════════════════════════════════════════════════════════════

def _keyword_fallback_general(text: str) -> str:
    """General fallback for START state — always gives a helpful reply."""
    t = text.lower()
    if any(w in t for w in ['service', 'repair', 'maintenance', 'oil', 'brake', 'tyre', 'battery']):
        return (
            "🔧 We offer complete vehicle service & maintenance at AutoSherpa!\n\n"
            "Please type *Hi* and select *Vehicle Service* from the menu to book your appointment."
        )
    if any(w in t for w in ['insurance', 'policy', 'renew', 'premium', 'coverage']):
        return (
            "🔰 AutoSherpa helps with motor insurance renewal and policy advisory.\n\n"
            "Please type *Hi* and select *Vehicle Insurance* from the menu to get started."
        )
    if any(w in t for w in ['car', 'price', 'buy', 'new', 'hyundai', 'model', 'variant', 'booking', 'test drive']):
        return (
            "🚗 AutoSherpa — Sherpa Hyundai offers the latest Hyundai cars.\n\n"
            "Please type *Hi* and select *New Cars* from the menu to explore models & prices."
        )
    if any(w in t for w in ['used', 'second hand', 'pre-owned', 'old car', 'certified']):
        return (
            "🚗 We have a wide range of certified pre-owned vehicles!\n\n"
            "Please type *Hi* and select *Used Cars* from the menu to browse our stock."
        )
    # General / out-of-scope query
    return (
        "👋 Hello! I'm AutoSherpa's virtual assistant.\n\n"
        "I specialise in automotive services — new cars, used cars, insurance, and vehicle service.\n\n"
        "Please type *Hi* to explore all our options."
    )


def _keyword_fallback_service(text: str) -> str:
    """Service flow fallback — always gives a car-service relevant reply."""
    t = text.lower()
    if any(w in t for w in ['book', 'appointment', 'schedule', 'slot']):
        return (
            "📅 Happy to help you book a service appointment!\n\n"
            "Please type *book service appointment* or select from the menu to choose a date and time."
        )
    if any(w in t for w in ['history', 'past service', 'previous', 'record', 'last service']):
        return (
            "📋 To view your service history, please share your vehicle registration number.\n\n"
            "Example: KA01AB1234"
        )
    if any(w in t for w in ['cost', 'price', 'estimate', 'how much', 'charge', 'fee']):
        return (
            "💰 For a service cost estimate, please type *get estimate* and I'll guide you through the details."
        )
    if any(w in t for w in ['next service', 'due', 'when', 'reminder']):
        return (
            "📅 Your next service due date is available once I fetch your vehicle details.\n\n"
            "Please share your vehicle registration number (e.g. KA01AB1234) to continue."
        )
    if any(w in t for w in ['oil', 'engine oil', 'coolant', 'brake fluid']):
        return (
            "🔧 Engine oil, coolant, and fluid checks are all part of our Periodic Maintenance Service (PMS).\n\n"
            "Type *book service appointment* to schedule your visit, or *get estimate* for pricing."
        )
    if any(w in t for w in ['tyre', 'tire', 'wheel', 'alignment', 'balancing']):
        return (
            "🔧 We offer tyre replacement, wheel alignment, and balancing services.\n\n"
            "Type *get estimate* for pricing, or *book service appointment* to schedule a visit."
        )
    if any(w in t for w in ['battery', 'ac', 'air condition', 'wiper', 'brake']):
        return (
            "🔧 Our service centre handles all repairs — battery, AC, brakes, wipers, and more.\n\n"
            "Type *book service appointment* to schedule your visit."
        )
    # Out-of-scope (like "who is PM of india")
    return (
        "🔧 I'm your AutoSherpa Service Assistant!\n\n"
        "I can help you with:\n"
        "• 📅 Book a service appointment — type *book service appointment*\n"
        "• 📋 View service history — share your registration number\n"
        "• 💰 Get a cost estimate — type *get estimate*\n"
        "• 📞 Contact advisor — type *contact advisor*\n\n"
        "Type *Hi* to explore all options."
    )


def _keyword_fallback_insurance(text: str) -> str:
    """
    Insurance flow fallback — used ONLY when Groq is completely unavailable.
    Gives a useful, topic-aware nudge without hardcoded answers.
    All real answers come from the LLM; this is just a graceful degradation layer.
    """
    t = text.lower()
    if any(w in t for w in ['renew', 'renewal', 'expire', 'expiry', 'due']):
        return (
            "🔄 For insurance renewal, please select *Renew Insurance* from our menu "
            "and our team will assist you with the best options.\n\n"
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['estimate', 'premium', 'cost', 'price', 'quote', 'how much']):
        return (
            "💰 For a personalised insurance estimate, please select *Get Insurance Estimate* "
            "from our menu — our advisors will calculate the best premium for you.\n\n"
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['ncb', 'no claim', 'bonus', 'discount']):
        return (
            "💡 *NCB (No Claim Bonus)* rewards you with a premium discount for every "
            "claim-free year — ranging from 20% (1 year) up to 50% (5+ years).\n\n"
            "For your specific NCB details, please speak with our advisor. "
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['idv', 'insured declared value', 'value']):
        return (
            "💡 *IDV (Insured Declared Value)* is the maximum amount your insurer "
            "will pay in case of total loss or theft — it's based on the car's current market value.\n\n"
            "For your vehicle's IDV, our advisor can help. Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['claim', 'accident', 'damage', 'repair']):
        return (
            "🛡️ For insurance claims, contact your insurer's 24x7 claim helpline immediately. "
            "Document the damage with photos and keep the FIR copy (if applicable).\n\n"
            "Our advisors can guide you through the process. Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['comprehensive', 'third party', 'tp', 'od', 'own damage']):
        return (
            "🛡️ *Comprehensive* insurance covers own damage + third-party liability. "
            "*Third-Party (TP)* covers only damage/injury to others — it's mandatory by law.\n\n"
            "For a detailed comparison, please select *Get Insurance Estimate* from the menu. "
            "Type *Hi* to go back."
        )
    if any(w in t for w in ['policy', 'number', 'details', 'info', 'history']):
        return (
            "📋 Please share your vehicle registration number so I can fetch your policy details.\n\n"
            "Example: *KA01AB1234*"
        )
    if any(w in t for w in ['advisor', 'agent', 'talk', 'speak', 'human', 'person', 'call']):
        return (
            "📞 I'll connect you with an insurance advisor right away.\n\n"
            "Please select *Talk to Advisor* from the menu or type *Hi* to go back to the main menu."
        )
    return (
        "🛡️ I'm your AutoSherpa Vehicle Insurance Expert!\n\n"
        "I can help you with:\n"
        "• Policy details & renewal\n"
        "• Premium estimates\n"
        "• NCB, IDV, claim queries\n"
        "• Comprehensive vs Third-Party coverage\n"
        "• Add-ons and coverage advice\n\n"
        "Please ask your question and I'll do my best to assist. "
        "Type *Hi* to explore all menu options."
    )


def _keyword_fallback_sales(text: str) -> str:
    """Sales flow fallback."""
    t = text.lower()
    if any(w in t for w in ['price', 'cost', 'how much', 'rate', 'ex showroom', 'on road']):
        return (
            "🚗 Please select a car model from our menu to see the latest prices.\n\n"
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['test drive', 'test', 'drive', 'book']):
        return (
            "🚗 To book a test drive, please select a model from our menu.\n\n"
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['emi', 'finance', 'loan', 'down payment']):
        return (
            "💳 We offer flexible EMI and finance options on all Hyundai cars.\n\n"
            "Please speak with our sales advisor for personalised finance plans.\n"
            "Type *Hi* to go back to the main menu."
        )
    return (
        "🚗 I'm your AutoSherpa Sales Assistant!\n\n"
        "I can help you explore new cars, prices, variants, and test drives.\n\n"
        "Type *Hi* to explore all options."
    )


def _keyword_fallback_used_cars(text: str) -> str:
    """Used cars flow fallback."""
    t = text.lower()
    if any(w in t for w in ['budget', 'price', 'cost', 'how much', 'affordable', 'cheap']):
        return (
            "🚗 We have certified pre-owned cars starting from ₹3 lakhs.\n\n"
            "Please share your budget range so I can show you matching options.\n"
            "Type *Hi* to go back to the main menu."
        )
    if any(w in t for w in ['test drive', 'drive', 'inspect', 'visit']):
        return (
            "🚗 You can schedule a test drive for any of our certified pre-owned vehicles.\n\n"
            "Type *Hi* to go back to the main menu and select *Used Cars*."
        )
    return (
        "🚗 I'm your AutoSherpa Used Cars Assistant!\n\n"
        "I can help you find certified pre-owned vehicles matching your budget.\n\n"
        "Type *Hi* to explore all options."
    )


def get_ai_response(message: str):
    """General AI used in webhook START state and MENU state free-text fallback."""

    system_prompt = """You are Sherpa, the friendly virtual assistant for AutoSherpa — a trusted Hyundai dealership and automotive services hub in Bengaluru, India.

Your personality: warm, polite, helpful — like a knowledgeable friend at the dealership who genuinely wants to help. You speak naturally and professionally, never robotic.

AutoSherpa services you can discuss:
• New Hyundai car sales — models, prices, variants, colours, test drives, finance/EMI
• Certified pre-owned / used cars — budget options, inspection, test drives, valuation
• Motor insurance — renewal, estimates, policy info, NCB, premium queries
• Vehicle service & maintenance — bookings, history, cost estimates, repairs
• Car loan refinancing — EMI reduction, loan against car, loan transfer, eligibility

STRICT RULES:
1. ONLY answer questions related to automobiles, AutoSherpa services listed above, or general car ownership topics (fuel efficiency, tyre care, battery maintenance, etc.).
2. For greetings (hi, hello, hey, good morning, etc.) — respond warmly and briefly invite them to explore our services. Keep it to 2-3 sentences.
3. For questions completely outside automotive / AutoSherpa scope (politics, recipes, general knowledge, etc.) — politely say you're specialised in automotive services and guide them back.
4. NEVER be rude, dismissive, or use harsh language — always stay kind and professional.
5. If someone is abusive or uses inappropriate language — respond calmly: "I'm here to help with your automotive needs. Please feel free to ask me anything related to cars, insurance, or service."
6. Do NOT fabricate prices, specs, or policies. If you don't have exact data, invite them to use the menu or speak to an advisor.
7. Keep responses concise — under 120 words. No unnecessary padding.
8. End with a gentle nudge to use the menu: suggest typing *Hi* to explore options — but only if it feels natural, not forced every time.
9. Use light, appropriate emojis where they add warmth (🚗 🔧 🛡️ 💳) — don't overdo it.
10. Never mention "modules", "flows", "states", or internal system details."""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            temperature=0.5,
            max_tokens=500
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print("⚠️ Groq AI Error:", e)
        return _keyword_fallback_general(message)


def handle_general_query_in_flow(phone: str, user_text: str,
                                  flow_type: str, vehicle_reg: str = None,
                                  user_state: dict = None) -> tuple:
    """
    Called when a user sends a free-text message inside an active flow
    that doesn't match any button action (GENERAL_QUERY).

    Routes to the correct flow-specific AI handler so the response is
    context-aware (e.g. uses real DB data for insurance/service).

    Args:
        phone      : WhatsApp phone number
        user_text  : The customer's free-text message
        flow_type  : 'insurance' | 'sales' | 'used_cars' | 'service' | 'refinancing' | 'general'
        vehicle_reg: Vehicle registration if known
        user_state : Full user state dict for budget / model context

    Returns:
        (reply, follow_on) tuple with AI-generated reply and suggested next action
    """
    flow_type = (flow_type or "general").lower()

    if flow_type == "insurance":
        return handle_insurance_ai_query(phone, user_text, vehicle_reg=vehicle_reg)

    elif flow_type == "sales":
        return handle_sales_ai_query(phone, user_text, user_state=user_state)

    elif flow_type == "used_cars":
        return handle_used_cars_ai_query(phone, user_text, user_state=user_state)

    elif flow_type == "service":
        return handle_service_ai_query(phone, user_text, vehicle_reg=vehicle_reg)

    elif flow_type == "refinancing":
        # Refinancing uses general AI with a refinancing-aware prompt
        return _handle_refinancing_ai_query(phone, user_text, user_state=user_state)

    else:
        reply = get_ai_response(user_text)
        _store_conversation(phone, user_text, reply, flow_type="general")
        return reply, "NONE"


def _detect_refinancing_follow_on_action(user_text: str, ai_reply: str) -> str:
    """Detect if the user needs a human agent or other specific action in the refinancing flow."""
    user_lower = normalize_text(user_text)
    
    # 1. Human / Advisor / Agent
    agent_keywords = ["advisor", "agent", "human", "talk to someone", "customer care", "person", "representative"]
    if any(k in user_lower for k in agent_keywords):
        return "TALK_TO_ADVISOR"
    
    return "NONE"

def _handle_refinancing_ai_query(phone: str, user_text: str, user_state: dict = None) -> tuple:
    """
    RAG-aware Expert AI for Refinancing flow.
    Returns: (reply, follow_on)
    """
    rag_context = get_rag_context(user_text, module="refinancing")
    current_state = user_state.get("state", "UNKNOWN") if user_state else "UNKNOWN"

    system_content = (
        f"{_REFINANCING_SYSTEM}\n\n"
        f"USER'S CURRENT FLOW STEP: {current_state}\n"
        f"Use this step to understand where the user is in their journey.\n\n"
        f"=== REFINANCING KNOWLEDGE CONTEXT (FAQ/RAG) ===\n{rag_context}\n\n"
        f"Use the above knowledge context to answer the customer query accurately."
    )

    history = _get_recent_conversation_history(phone, limit=6, flow_type="refinancing")
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.4,
            max_tokens=800,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ai] Groq error (refinancing): {e}")
        reply = (
            "💳 I can help with car loan refinancing — reducing your EMI, "
            "loan against car, or transferring an existing loan.\n\n"
            "Please select an option from our menu or ask me a specific question."
        )

    follow_on = _detect_refinancing_follow_on_action(user_text, reply)
    _store_conversation(phone, user_text, reply, flow_type="refinancing")
    return reply, follow_on


# ════════════════════════════════════════════════════════════
# 2.  DB HELPERS
# ════════════════════════════════════════════════════════════

def _get_db():
    from config import DB_CONFIG
    return mysql.connector.connect(**DB_CONFIG)


def _ensure_conversation_log_table():
    """Create conversation_log table if not exists."""
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversation_log (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                phone_number VARCHAR(20),
                user_message TEXT,
                bot_response TEXT,
                sender_type  ENUM('user','bot','agent') DEFAULT 'bot',
                vehicle_reg  VARCHAR(20),
                flow_type    VARCHAR(20) COMMENT 'sales|insurance|used_cars|service',
                timestamp    DATETIME,
                INDEX idx_phone     (phone_number),
                INDEX idx_timestamp (timestamp),
                INDEX idx_flow      (flow_type)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ai] conversation_log table ensure error: {e}")


def _store_conversation(phone: str, user_msg: str, bot_reply: str,
                        flow_type: str, vehicle_reg: str = None):
    """
    Store conversation in conversation_log.
    Aligned with sample code: single-row logging.
    """
    user_msg = (user_msg or "").strip()
    bot_reply = (bot_reply or "").strip()

    # Nothing to store
    if not user_msg and not bot_reply:
        return

    try:
        conn = _get_db()
        cur = conn.cursor()
        now = datetime.now()

        # Aligned with sample code structure: single-row insert
        query = """
            INSERT INTO conversation_log
            (phone_number, user_message, bot_response, vehicle_reg, flow_type, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        cur.execute(query, (
            phone,
            user_msg or None,
            bot_reply or None,
            vehicle_reg,
            flow_type,
            now
        ))

        conn.commit()
        cur.close()
        conn.close()
        print(f"[ai] ✅ conversation_log saved | phone={phone} | user='{user_msg[:40]}' | bot='{bot_reply[:40]}'")
    except Exception as e:
        print(f"[ai] ❌ store_conversation FAILED: {e}")



# ════════════════════════════════════════════════════════════
# 3.  PUBLIC store_interaction() — CALL FROM ALL FLOWS
#
#     Use this in insurance.py / sales.py / used_cars.py
#     after EVERY send_whatsapp_message / send_button_message
#     / send_list_message call to log button clicks too.
# ════════════════════════════════════════════════════════════

def store_interaction(phone: str, user_query: str, bot_response: str,
                      flow_type: str, vehicle_reg: str = None):
    """
    Public helper — log ANY user query + bot response to conversation_log.

    Call this in insurance/sales/used_cars AFTER every bot reply
    (button clicks, menu selections, text queries) so all conversations
    are stored in the DB with correct TWO-row pattern.

    Args:
        phone        : WhatsApp phone number
        user_query   : What the user sent (text or button label e.g. "3" or "Renew Insurance")
        bot_response : What the bot replied (text or short summary of menu shown)
        flow_type    : 'insurance' | 'sales' | 'used_cars' | 'service'
        vehicle_reg  : Vehicle registration number if available
    """
    _store_conversation(phone, user_query, bot_response, flow_type, vehicle_reg)


# ════════════════════════════════════════════════════════════
# 4.  RAG CONTEXT FETCHERS
# ════════════════════════════════════════════════════════════

def get_rag_context(query: str, module: str, k: int = 3) -> str:
    """
    Fetch relevant knowledge from Vector DB for a specific module.
    """
    try:
        results = vector_service.search(query, k=k, filter={"module": module})
        if not results:
            return ""
        
        context = f"\n=== SUPPLEMENTARY KNOWLEDGE FOR {module.upper()} (from RAG) ===\n"
        for i, doc in enumerate(results):
            context += f"[{i+1}] {doc.page_content}\n"
        return context
    except Exception as e:
        print(f"[ai] RAG search error ({module}): {e}")
        return ""

# ════════════════════════════════════════════════════════════
# 5.  DB CONTEXT FETCHERS (internal)
# ════════════════════════════════════════════════════════════

def _get_insurance_db_context(vehicle_reg: str = None) -> str:
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        if vehicle_reg:
            cur.execute("""
                SELECT ir.VehicleRegnNo, ir.policyno, ir.policytype,
                       ir.policyissuedate, ir.insurancecompany,
                       ir.submodel, ir.yearofmanufacture,
                       ir.grosstotalpremium, ir.customername,
                       ir.insureddeclaredvalue, ir.engineno, ir.chassisno,
                       ifd.policyexpirydate, ifd.renewaltype
                FROM   individualreport ir
                LEFT JOIN insuranceforecasteddata ifd
                       ON ir.VehicleRegnNo = ifd.VehicleRegnNo
                WHERE  ir.VehicleRegnNo = %s
                ORDER BY ir.policyissuedate DESC
                LIMIT 5
            """, (vehicle_reg,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            if not rows:
                return f"No insurance records found for vehicle {vehicle_reg}."
            lines = [f"Insurance Records for {vehicle_reg}:"]
            for r in rows:
                lines.append(
                    f"- Policy: {r['policyno']} | Company: {r['insurancecompany']} | "
                    f"Type: {r['policytype']} | Model: {r['submodel']} ({r['yearofmanufacture']}) | "
                    f"Issued: {r['policyissuedate']} | "
                    f"Expires: {r.get('policyexpirydate','N/A')} | "
                    f"Premium: Rs.{r['grosstotalpremium']} | "
                    f"IDV: Rs.{r.get('insureddeclaredvalue','N/A')} | "
                    f"Customer: {r.get('customername','N/A')} | "
                    f"Engine: {r.get('engineno','N/A')} | "
                    f"Chassis: {r.get('chassisno','N/A')} | "
                    f"Renewal: {r.get('renewaltype','N/A')}"
                )
            return "\n".join(lines)
        else:
            cur.execute("""
                SELECT renewaltype, COUNT(*) AS cnt
                FROM insuranceforecasteddata
                GROUP BY renewaltype
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()
            summary = (
                ", ".join([f"{r['renewaltype']}: {r['cnt']}" for r in rows])
                if rows else "N/A"
            )
            return (
                "AutoSherpa Insurance Services:\n"
                "- Motor insurance renewal assistance\n"
                "- Insurance estimate (upload RC/policy document via link)\n"
                "- Policy comparison and NCB advisory\n"
                f"Current renewal pipeline summary: {summary}"
            )
    except Exception as e:
        print(f"[ai] Insurance DB context error: {e}")
        return "Insurance data temporarily unavailable."


def _get_sales_db_context() -> str:
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT s.make, s.model, s.variant,
                   s.mileage_kmph,
                   `Ex-Showroom Price Base Model` AS ex_showroom_price,
                   `Ex-Showroom Price Top Model`  AS on_road_price,
                   GROUP_CONCAT(DISTINCT cf.fuel_type)         AS fuel_types,
                   GROUP_CONCAT(DISTINCT ct.transmission_type) AS transmissions,
                   GROUP_CONCAT(DISTINCT cc.color_name)        AS colors
            FROM   sales_car_details s
            LEFT JOIN car_fuel_types    cf ON cf.car_id = s.id
            LEFT JOIN car_transmissions ct ON ct.car_id = s.id
            LEFT JOIN car_colors        cc ON cc.car_id = s.id
            GROUP BY s.id
            ORDER BY s.make, s.model
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return "No new car inventory found in database."
        lines = ["Available New Car Inventory:"]
        for r in rows:
            lines.append(
                f"- {r['make']} {r['model']} ({r['variant']}): "
                f"Ex-Showroom Rs.{int(r['ex_showroom_price']):,}, "
                f"On-Road Rs.{int(r['on_road_price']):,}, "
                f"Mileage {r['mileage_kmph']} kmpl | "
                f"Fuels: {r['fuel_types'] or 'N/A'} | "
                f"Transmissions: {r['transmissions'] or 'N/A'} | "
                f"Colors: {r['colors'] or 'N/A'}"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[ai] Sales DB context error: {e}")
        return "Car inventory data temporarily unavailable."


def _get_used_cars_db_context(budget_min: int = None, budget_max: int = None) -> str:
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        if budget_min and budget_max:
            cur.execute("""
                SELECT make, model, manufacturing_year, fuel_type,
                       transmission_type, mileage_km,
                       estimated_selling_price
                FROM   carstockdata
                WHERE  LOWER(ready_for_sales) = 'available'
                  AND  estimated_selling_price BETWEEN %s AND %s
                ORDER BY estimated_selling_price ASC
                LIMIT 10
            """, (budget_min, budget_max))
        else:
            cur.execute("""
                SELECT make, model, manufacturing_year, fuel_type,
                       transmission_type, mileage_km,
                       estimated_selling_price
                FROM   carstockdata
                WHERE  LOWER(ready_for_sales) = 'available'
                ORDER BY estimated_selling_price ASC
                LIMIT 15
            """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return "No used cars currently available in inventory."
        lines = ["Available Used Car Stock:"]
        for r in rows:
            lines.append(
                f"- {r['make']} {r['model']} ({r['manufacturing_year']}) | "
                f"{r['fuel_type']} | {r['transmission_type']} | "
                f"{r['mileage_km']} km | "
                f"Price: Rs.{int(r['estimated_selling_price']):,}"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[ai] Used cars DB context error: {e}")
        return "Used car inventory data temporarily unavailable."


def _get_targeted_car_details(user_text: str) -> str:
    """
    Search for a specific car mentioned in user_text and return its full details.
    """
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        
        # 1. Fetch all unique models to check for matches
        cur.execute("SELECT DISTINCT model, make FROM carstockdata WHERE LOWER(ready_for_sales) = 'available'")
        available_cars = cur.fetchall()
        
        target_model = None
        user_text_lower = user_text.lower()
        
        for car in available_cars:
            model = car['model'].lower()
            if model in user_text_lower:
                target_model = car['model']
                break
        
        if not target_model:
            cur.close(); conn.close()
            return ""

        # 2. Fetch full details for the matched model
        cur.execute("""
            SELECT make, model, variant, color, fuel_type, 
                   manufacturing_year, mileage_km, transmission_type,
                   estimated_selling_price, cubic_capacity_cc, insurance_type
            FROM   carstockdata
            WHERE  model = %s AND LOWER(ready_for_sales) = 'available'
            LIMIT 1
        """, (target_model,))
        r = cur.fetchone()
        cur.close(); conn.close()

        if r:
            return (
                f"=== SPECIFIC VEHICLE DETAILS FOUND ===\n"
                f"- Model: {r['make']} {r['model']} {r.get('variant','')}\n"
                f"- Year: {r['manufacturing_year']}\n"
                f"- Fuel: {r['fuel_type']}\n"
                f"- Transmission: {r['transmission_type']}\n"
                f"- Mileage: {r['mileage_km']} km\n"
                f"- Engine: {r['cubic_capacity_cc']} cc\n"
                f"- Price: Rs.{int(r['estimated_selling_price']):,}\n"
                f"- Color: {r['color']}\n"
                f"- Insurance: {r['insurance_type']}\n"
            )
        return ""
    except Exception as e:
        print(f"[ai] _get_targeted_car_details error: {e}")
        return ""


def _get_service_db_context(vehicle_reg: str = None) -> str:
    """Fetch service history and forecast data for a vehicle from DB."""
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        if vehicle_reg:
            cur.execute("""
                SELECT r.vehicleRegNo, r.customerName, r.model,
                       r.lastServiceDate, r.lastServiceType,
                       r.lastServiceMeterReading, r.billAmt,
                       r.workshopName, r.location_name,
                       b.nextServiceDue, b.nextServiceType
                FROM robillscube r
                LEFT JOIN bicoe_forecast_cube b ON r.vehicleRegNo = b.vehicleRegNo
                WHERE r.vehicleRegNo = %s
                ORDER BY r.lastServiceDate DESC
                LIMIT 5
            """, (vehicle_reg,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            if not rows:
                return f"No service records found for vehicle {vehicle_reg}."
            lines = [f"Service Records for {vehicle_reg}:"]
            for r in rows:
                lines.append(
                    f"- Date: {r.get('lastServiceDate','N/A')} | "
                    f"Type: {r.get('lastServiceType','N/A')} | "
                    f"KM: {r.get('lastServiceMeterReading','N/A')} | "
                    f"Amount: Rs.{r.get('billAmt','N/A')} | "
                    f"Workshop: {r.get('workshopName','N/A')} ({r.get('location_name','N/A')}) | "
                    f"Next Due: {r.get('nextServiceDue','N/A')} | "
                    f"Next Type: {r.get('nextServiceType','N/A')}"
                )
            return "\n".join(lines)
        else:
            return (
                "AutoSherpa Service Centre:\n"
                "- Vehicle service, maintenance and repairs\n"
                "- Service history tracking\n"
                "- Next service due reminders\n"
                "- Service cost estimates"
            )
    except Exception as e:
        print(f"[ai] Service DB context error: {e}")
        return "Service data temporarily unavailable."


# ════════════════════════════════════════════════════════════
# 6.  SYSTEM PROMPTS
# ════════════════════════════════════════════════════════════

_INSURANCE_SYSTEM = """You are *AutoSherpa's Vehicle Insurance Expert* — a knowledgeable, friendly, and professional WhatsApp insurance advisor for AutoSherpa, Bengaluru.

YOUR ROLE:
You act as a trusted insurance expert who can answer ANY vehicle insurance query a customer might have — whether they are asking about their own policy (from the DB context provided) or asking general insurance knowledge questions. Think of yourself as a qualified motor insurance advisor, not just a bot that routes to menus.

WHAT YOU CAN ANSWER (no hardcoded responses — reason from your knowledge):
1. POLICY-SPECIFIC (use DB context): policy number, insurer, expiry date, premium paid, renewal type, vehicle model, NCB eligibility, coverage type, IDV, engine/chassis number.
2. GENERAL INSURANCE KNOWLEDGE — answer these fully and professionally:
   - Comprehensive vs Third-Party vs OD-only insurance — differences, what's covered, what's not
   - IDV (Insured Declared Value) — what it means, how it's calculated, why it matters
   - NCB (No Claim Bonus) — how it works, what percentage, how to protect it, transfer rules
   - Claim process — how to raise a claim, cashless vs reimbursement, documents needed
   - Zero depreciation / Nil depreciation add-on — when to take it, cost vs benefit
   - Engine protection, roadside assistance, return to invoice add-ons — explained clearly
   - Policy lapse — consequences, grace period, fresh vs break-in insurance implications
   - Renewals — online vs offline, multi-year, same insurer vs switching
   - Premium factors — age of car, city, fuel type, cubic capacity, driver history
   - IRDAI regulations — basic consumer rights, free look period, policy portability
   - Two-wheeler vs four-wheeler insurance differences
   - Commercial vehicle insurance specifics
   - PA (Personal Accident) cover — mandatory requirements, coverage limits
   - GVW, seating capacity impact on commercial policies
   - How to check if a vehicle is insured (VAHAN lookup, IIB)
3. RENEWAL INTENT: If customer says they want to renew, first share their policy details from DB, then guide them to select the *Renew Insurance* option from the menu.
4. ESTIMATE INTENT: Guide them to use the estimate option from the menu.

PERSONA & TONE:
- Warm, professional, and knowledgeable — like a trusted insurance broker
- Never robotic, never just redirect without answering
- Speak in clear, simple language (avoid excessive jargon unless explaining terms)
- Use emojis sparingly for warmth: 🛡️ 📋 ✅ 🚗 💡
- Keep answers concise but complete — aim for 80–200 words
- For complex topics (claim process, NCB), use a short numbered list for clarity

STRICT RULES:
- **Vehicle Information Requests**: When a customer asks for their policy information, use the following sample format as a guideline:
  "Policy information
  A: Your policy number is {policyno}, issued by {insurancecompany} on {policyissuedate} under {policytype}."
- NEVER fabricate policy numbers, premium amounts, expiry dates, or vehicle details — use ONLY what the DB context provides.
- If the customer asks about their specific policy but no vehicle data is provided in the context (or vehicle_reg is unknown), politely ask them to share their vehicle registration number so you can fetch the details.
- For policy-specific data NOT in DB context: say clearly "I don't have that information in our system — please visit the branch or call our advisor".
- NEVER answer questions outside automobiles and insurance (politics, recipes, cricket, etc.) — gently redirect: "I'm specialised in vehicle insurance — happy to help with any motor insurance queries! 🛡️"
- NEVER reveal internal system details, state names, or code
- NEVER say "temporarily unavailable" — always give a useful answer or acknowledge gracefully
- Do NOT end every reply with "Type *Hi* to explore all options" — only add it when genuinely useful to guide the user to the menu (e.g. after answering a renewal/estimate intent)"""

_SALES_SYSTEM = """You are *AutoSherpa's New Cars Expert* — a friendly, knowledgeable, and professional advisor for the latest Hyundai vehicles at Sherpa Hyundai, Bengaluru.

🎯 YOUR OPERATIONAL STRATEGY:
1. **Semantic Search First**: You MUST first check the 'SALES KNOWLEDGE CONTEXT (FAQ/RAG)' for relevant information. If specific answers are found there (e.g., about policies, documentation, specific features, or sales procedures), respond using that data accurately.
2. **Real-time Expert Fallback**: If the RAG context does NOT contain the specific answer, do NOT say "I don't know". Instead, respond as a professional Car Sales Expert using your general knowledge and the provided 'LIVE DATABASE CONTEXT (Inventory)'.
3. **Inventory Queries**: For questions about pricing, variants, and stock, prioritize the 'LIVE DATABASE CONTEXT'.
4. **Professional & Perfect**: Your goal is to provide a "perfect" response — being helpful, precise, and encouraging while maintaining a premium brand voice.

PERSONA & TONE:
- Warm, premium, and expert-level professional.
- Use Indian Rupees (₹) for prices and format large numbers with commas.
- Be concise but complete (80-150 words).
- Use light emojis: 🚗 ✨ 💰 🛡️ 📍

STRICT RULES:
- NEVER fabricate car details or prices not in the inventory.
- If a specific user-requested variant is missing from inventory, suggest the closest available option or upcoming models.
- For actions like "Book Test Drive" or "Apply for Finance": Answer the user's immediate question first, then inform them that you can help them start the process.
- End your response with a helpful follow-up question to keep the conversation going."""




_USED_CARS_SYSTEM = """You are *AutoSherpa's Used Cars Expert* — a friendly, knowledgeable, and professional advisor for pre-owned vehicles at Sherpa Hyundai, Bengaluru.

YOUR ROLE:
You help customers with any queries related to used cars. This includes:
1. DIRECT QUERIES (Database search): Finding specific cars in our stock based on budget, model, year, etc.
2. STATIC QUERIES (FAQ/Knowledge): Answering general questions about our used car process, certification, warranty, documentation, and exchange policies using the provided knowledge base (RAG).
3. LIVE AGENT QUERIES: Identifying when a customer needs to speak with a human (e.g., complex pricing, specific negotiation, or direct request for an agent).

PERSONA & TONE:
- Warm, professional, and helpful.
- Use Indian Rupees (₹) for prices and format large numbers with commas (e.g., ₹5,00,000).
- Be concise but complete (80-150 words).
- Use light emojis: 🚗 💰 🛡️ 📍

STRICT RULES:
- ONLY answer based on the provided DB context (Stock) and RAG context (FAQ).
- NEVER fabricate car details, mileage, or prices not in the context.
- If a matching car is not in stock: "We don't have that exact match right now, but I can notify you when it arrives."
- If the customer wants to book a test drive or get a valuation, guide them to use the menu options.
- End your response with a helpful nudge if appropriate."""

_SERVICE_SYSTEM = """You are the official Service Expert for AutoSherpa (Sherpa Hyundai), Bengaluru.
You assist customers with all aspects of vehicle service, maintenance, and technical queries.

🎯 CORE GUIDELINES:
1. **Prioritize Context**: Use the provided Database and Knowledge Context for specific service history, dates, and costs.
2. **Professional Fallback**: If specific information is missing from our records, respond professionally using your general automotive knowledge. Never say "I don't know" or "I don't have info" bluntly. Instead, provide helpful general guidance (e.g., "Typically, AC servicing is recommended every...") and suggest a professional inspection.
3. **Professional Tone**: Be empathetic, professional, and reassuring. If a user describes a problem (noise, leak, etc.), acknowledge the concern and explain potential causes briefly before suggesting an expert check-up.
4. **Be Concise**: Keep responses clear and under 200 words.
5. **Accuracy**: Never fabricate specific dates or bill amounts. If they aren't in the context, speak in general terms.
6. **Next Steps**: Always encourage the user to take action (Book Appointment, Get Estimate, or Contact Advisor).

Format lists clearly and use emojis (🔧 🚗 📅 💰) to keep the interaction engaging.
End every reply with a helpful next step (e.g. "Let me know if you'd like to see more details")."""


_REFINANCING_SYSTEM = """You are *AutoSherpa's Refinancing Expert* — a specialized consultant for car loans, EMI reduction, and loan-against-car solutions at Sherpa Hyundai, Bengaluru.

🎯 YOUR OPERATIONAL STRATEGY:
1. **Semantic Search First**: You MUST first check the 'REFINANCING KNOWLEDGE CONTEXT (FAQ/RAG)' for relevant information about policies, eligibility, documentation, and procedures.
2. **Professional Expert Fallback**: If specific info is missing, act as a professional loan advisor. Explain concepts like EMI reduction, top-up loans, or interest rate transfers to help the user understand their options perfectly.
3. **Action Orientation**: Your goal is to provide a "perfect" response — being helpful and clear, then guiding the user toward our refinancing options or a human advisor.

PERSONA & TONE:
- Professional, trustworthy, and financially savvy.
- Use clear language to explain complex loan terms.
- Be concise but complete (80-150 words).
- Use light emojis: 💳 💰 📉 🛡️ 🏦

STRICT RULES:
- NEVER guarantee a specific interest rate or loan amount without document verification.
- Always mention: "Subject to bank approval and vehicle valuation."
- For complex technical eligibility: "Our dedicated loan manager will verify your documents to give you the exact ROI."
- End your response with a helpful follow-up question to keep the conversation going."""



# ════════════════════════════════════════════════════════════
# 7.  PUBLIC FLOW-SPECIFIC AI FUNCTIONS
# ════════════════════════════════════════════════════════════

def _get_recent_conversation_history(phone: str, limit: int = 6, flow_type: str = "insurance") -> list:
    """
    Fetch the last `limit` conversation turns for this phone number
    from conversation_log, returning them as OpenAI-style message dicts
    [{role: 'user'|'assistant', content: '...'}].

    Used to give the AI short-term memory within a session
    so follow-up questions are answered in context.
    """
    try:
        conn = _get_db()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT user_message, bot_response, timestamp
            FROM   conversation_log
            WHERE  phone_number = %s
              AND  flow_type    = %s
              AND  (user_message IS NOT NULL OR bot_response IS NOT NULL)
            ORDER BY timestamp DESC
            LIMIT %s
        """, (phone, flow_type, limit))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        # Rows are newest-first; reverse to get chronological order
        rows = list(reversed(rows))
        history = []
        for row in rows:
            if row.get("user_message"):
                history.append({"role": "user", "content": row["user_message"]})
            if row.get("bot_response"):
                history.append({"role": "assistant", "content": row["bot_response"]})
        return history
    except Exception as e:
        print(f"[ai] _get_recent_conversation_history error: {e}")
        return []


def _normalize_insurance_text(text: str) -> str:
    return normalize_text(text)


def _detect_follow_on_action(user_text: str, ai_reply: str,
                              vehicle_reg: str = None) -> str:
    """
    After the AI has answered a general insurance query, determine which
    follow-on flow action is most contextually relevant to surface next.

    Returns one of:
      "RENEW_INSURANCE"   — renewal flow (user explicitly wants to renew)
      "GET_ESTIMATE"      — estimate flow (user explicitly wants a quote for THEIR vehicle)
      "INSURANCE_HISTORY" — history flow (past policies)
      "TALK_TO_ADVISOR"   — advisor flow (claims, disputes, escalation)
      "NONE"              — pure knowledge query — NO follow-on action needed

    KEY RULE: Return NONE for any pure knowledge/informational question.
    Only return GET_ESTIMATE / RENEW_INSURANCE when the user explicitly
    wants to perform an ACTION (e.g. "get a quote for my car", "renew my policy").
    Questions like "what is NCB?", "how is premium calculated?", "what does IDV mean?"
    should ALWAYS return NONE so the flow does NOT launch after the AI answers.

    DETECTION ORDER:
      1. classify_insurance_intent_request() — fast keyword check (authoritative)
      2. Extended semantic keyword check — broader natural language patterns
      3. Groq LLM semantic classifier — catches everything keywords miss
      4. General knowledge check → NONE
    """
    user_lower = normalize_text(user_text)

    # ── Step 0: Pure knowledge question → NONE immediately ───────────────
    # This MUST run before Step 1 & 2 to prevent knowledge questions like
    # "what is the premium amount and how is it calculated" from being
    # misclassified as GET_ESTIMATE follow-on actions.
    # The knowledge check runs ONLY when there is no explicit action keyword;
    # if classify_insurance_intent_request() fires, Step 1 handles it.
    if _is_general_insurance_question(user_text):
        explicit_action = classify_insurance_intent_request(user_text)
        if not explicit_action:
            print(f"[ai] ⚡ Knowledge pre-check in follow-on → NONE | '{user_text}'")
            return "NONE"

    # ── Step 1: Fast keyword check (authoritative) ───────────────────────
    explicit_intent = classify_insurance_intent_request(user_text)
    if explicit_intent in ("RENEW_INSURANCE", "GET_ESTIMATE", "INSURANCE_HISTORY", "TALK_TO_ADVISOR"):
        print(f"[ai] ✅ Explicit follow-on via keyword: {explicit_intent} | '{user_text}'")
        return explicit_intent

    # ── Step 2: Extended semantic keyword patterns ────────────────────────
    # These catch natural language that the core classifier might miss.

    renew_semantic = [
        # "i want to renew", "i want renewal", "i want my insurance renewed"
        "i want to renew", "i want renewal", "want my insurance renewed",
        "want my policy renewed", "i want my renew", "i want renew",
        "looking to renew", "looking for renewal", "planning to renew",
        "i need to renew", "need my insurance renewed", "need my policy renewed",
        "can you renew", "can i renew", "help me renew", "help with renewal",
        "start my renewal", "begin my renewal", "do my renewal",
        "do renewal for", "renewal for my vehicle", "renewal for my car",
        "renew my vehicle", "my insurance is expiring", "my policy is expiring",
        "my insurance expired", "my policy expired", "insurance due",
        "policy is due", "due for renewal", "up for renewal",
        # Hindi/mixed phrasing
        "renwal karna", "renwal chahiye", "renew karna", "renew chahiye",
        "insurance renew", "policy renew karna hai", "renew karana",
    ]

    estimate_semantic = [
        # "i want an estimate", "can i get a quote", "how much will insurance cost"
        "i want an estimate", "i want estimate", "i need an estimate",
        "i need estimate", "can i get estimate", "can i get a quote",
        "can i get quote", "can you give me estimate", "can you give estimate",
        "can you give me quote", "can you give quote",
        "can you provide estimate", "can you provide quote",
        "provide me estimate", "provide me quote", "provide me a quote",
        "send me estimate", "send me quote", "send me a quote",
        "give me estimate", "give me quote", "give me a quote",
        "get me estimate", "get me quote", "get me a quote",
        "please give estimate", "please give quote", "please give a quote",
        "check the estimate", "check estimate", "check my quote", "check my estimate",
        "how much will it cost", "how much is insurance",
        "how much will insurance cost", "how much will the premium be",
        "what will be the premium", "what is the premium for my",
        "what will be the cost", "tell me the cost", "tell me the premium",
        "calculate my premium", "calculate insurance premium",
        "calculate premium for", "insurance cost for my",
        "premium for my", "cost for my insurance",
        "quote for me", "estimate for me",
        # Hindi/mixed phrasing
        "estimate chahiye", "quote chahiye", "premium kitna hoga",
        "kitna premium", "kitna lagega", "cost kya hoga",
    ]

    history_semantic = [
        "my insurance history", "show my history", "my past policies",
        "previous insurance", "old insurance", "show my insurance",
        "show my policy", "my policy records", "insurance records",
        "past insurance", "old policies", "previous policies",
    ]

    advisor_semantic = [
        "connect me to", "i want to talk", "i want to speak",
        "i need to speak", "i need to talk", "let me speak",
        "let me talk", "can i speak", "can i talk",
        "please connect", "please transfer", "need a human",
        "need human help", "need agent", "want an agent",
        "want a human", "speak to someone", "talk to someone",
        "connect with advisor", "speak with advisor",
    ]

    critical_issue = [
        "file a claim", "raise a claim", "register a claim",
        "had an accident", "met with an accident",
        "my car is damaged", "car stolen", "theft claim", "stolen vehicle",
        "vehicle stolen", "car got stolen",
    ]

    if any(k in user_lower for k in renew_semantic):
        print(f"[ai] ✅ Follow-on via semantic renew: RENEW_INSURANCE | '{user_text}'")
        return "RENEW_INSURANCE"
    if any(k in user_lower for k in estimate_semantic):
        print(f"[ai] ✅ Follow-on via semantic estimate: GET_ESTIMATE | '{user_text}'")
        return "GET_ESTIMATE"
    if any(k in user_lower for k in history_semantic):
        print(f"[ai] ✅ Follow-on via semantic history: INSURANCE_HISTORY | '{user_text}'")
        return "INSURANCE_HISTORY"
    if any(k in user_lower for k in advisor_semantic):
        print(f"[ai] ✅ Follow-on via semantic advisor: TALK_TO_ADVISOR | '{user_text}'")
        return "TALK_TO_ADVISOR"
    if any(k in user_lower for k in critical_issue):
        print(f"[ai] ✅ Follow-on via critical issue: TALK_TO_ADVISOR | '{user_text}'")
        return "TALK_TO_ADVISOR"

    # ── Step 3: Pure knowledge question → short-circuit to NONE ─────────
    if _is_general_insurance_question(user_text):
        print(f"[ai] Knowledge question → NONE | '{user_text}'")
        return "NONE"

    # ── Step 4: Groq LLM semantic classifier (catches everything else) ───
    # This handles paraphrases, multilingual, and edge cases that keywords
    # cannot catch.  The prompt is intentionally strict: only fire an action
    # when the customer's INTENT is clearly to DO something, not just learn.
    try:
        prompt = f"""You are an intent classifier for a vehicle insurance chatbot.

Your job: decide if the customer is requesting a SPECIFIC ACTION or just asking an informational question.

CUSTOMER MESSAGE: "{user_text}"
BOT REPLY (for context): "{ai_reply[:200]}"

INTENT OPTIONS:
- RENEW_INSURANCE   → Customer explicitly says they WANT TO RENEW their own insurance policy (action)
- GET_ESTIMATE      → Customer explicitly asks YOU TO GENERATE a quote for THEIR specific vehicle (action)
- INSURANCE_HISTORY → Customer explicitly wants to see their own past policy records (action)
- TALK_TO_ADVISOR   → Customer wants a human agent, OR has an active accident/claim emergency (action)
- NONE              → Customer is asking a KNOWLEDGE / INFORMATION question — they want to LEARN something

⚠️ CRITICAL: These are ALL NONE (knowledge questions, NOT actions):
- "what is the premium amount" → NONE
- "what is the premium amount and how it is calculated" → NONE
- "how is premium calculated" → NONE
- "how is insurance premium determined" → NONE
- "how much does insurance cost in general" → NONE
- "what is comprehensive insurance?" → NONE
- "what is NCB?" → NONE
- "how does IDV work?" → NONE
- "explain zero depreciation" → NONE
- "what factors affect premium?" → NONE
- "how does the claim process work?" → NONE
- "what is covered under third party?" → NONE

These ARE actions (not NONE):
- "I want to renew my insurance" → RENEW_INSURANCE
- "Give me an estimate for my car" → GET_ESTIMATE
- "I want a quote for my vehicle" → GET_ESTIMATE
- "Show me my past policies" → INSURANCE_HISTORY
- "Connect me to an advisor" → TALK_TO_ADVISOR

RULE: If the customer is ASKING A QUESTION to UNDERSTAND something → NONE.
Only use action intents when they clearly want to DO something specific for their own vehicle right now.

Respond ONLY with valid JSON:
{{"action": "<RENEW_INSURANCE|GET_ESTIMATE|INSURANCE_HISTORY|TALK_TO_ADVISOR|NONE>", "confidence": <0.0-1.0>, "reason": "<5 words max>"}}"""

        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=80,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        action     = result.get("action", "NONE")
        confidence = float(result.get("confidence", 0.0))
        reason     = result.get("reason", "")
        print(f"[ai] 🤖 LLM follow-on: {action} ({confidence:.2f}) — {reason} | '{user_text}'")

        valid_actions = {"RENEW_INSURANCE", "GET_ESTIMATE", "INSURANCE_HISTORY", "TALK_TO_ADVISOR", "NONE"}
        if action in valid_actions and confidence >= 0.65:
            return action
        # Low-confidence → safe default
        return "NONE"

    except Exception as e:
        print(f"[ai] _detect_follow_on_action LLM error: {e}")
        return "NONE"  # Safe default: never force a flow after an AI answer


def _is_general_insurance_question(text: str) -> bool:
    """Return True for informational insurance questions that should stay in AI answer mode."""
    return is_insurance_knowledge_query(text)


def handle_insurance_ai_query(phone: str, user_text: str,
                               vehicle_reg: str = None) -> tuple:
    """
    Full vehicle insurance expert AI for the insurance flow.

    Returns a tuple: (reply: str, suggested_follow_on: str)

    - reply             : AI-generated expert answer to send to the customer
    - suggested_follow_on: One of RENEW_INSURANCE | GET_ESTIMATE | INSURANCE_HISTORY
                           | TALK_TO_ADVISOR | NONE
                           Tells insurance.py which flow action to trigger after
                           sending the AI reply, so the bot naturally continues
                           into the relevant flow without any hardcoding.

    Features:
    - Uses _INSURANCE_SYSTEM expert prompt — answers ANY vehicle insurance query
    - Pulls real vehicle/policy data from DB as grounding context
    - Maintains short-term conversation history (last 6 turns) for follow-up questions
    - Falls back gracefully to keyword response if Groq is unavailable
    - Stores conversation to conversation_log for tracking
    """
    # 1. Fetch live DB context for this vehicle (grounding facts)
    db_context = _get_insurance_db_context(vehicle_reg)

    # 2. Fetch RAG context (semantic knowledge from uploaded files)
    rag_context = get_rag_context(user_text, module="insurance")

    # 3. Build system message with DB context and RAG context embedded
    system_content = (
        f"{_INSURANCE_SYSTEM}\n\n"
        f"=== LIVE VEHICLE & POLICY DATA (from AutoSherpa DB) ===\n"
        f"{db_context}\n"
        f"{rag_context}\n\n"
        f"Use the above DB data and RAG knowledge to answer questions accurately. "
        f"DB data is for vehicle-specific info. RAG data contains specific company policies or process details."
    )

    # 3. Fetch recent conversation history for multi-turn context
    history = _get_recent_conversation_history(phone, limit=6)

    # 4. Build messages array: system → history → current user message
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    # 5. Call Groq LLM for the expert answer
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.4,
            max_tokens=800,
        )
        reply = resp.choices[0].message.content.strip()
        print(f"[ai] ✅ Insurance expert replied | phone={phone} | query='{user_text[:50]}'")
    except Exception as e:
        print(f"[ai] Groq error (insurance expert): {e}")
        reply = _keyword_fallback_insurance(user_text)

    # 6. Detect which follow-on flow action fits best (separate focused call)
    suggested_follow_on = _detect_follow_on_action(user_text, reply, vehicle_reg)

    # 7. Store to conversation_log
    _store_conversation(phone, user_text, reply,
                        flow_type="insurance", vehicle_reg=vehicle_reg)

    return reply, suggested_follow_on


def _detect_sales_follow_on_action(user_text: str, ai_reply: str) -> str:
    """
    Detect if the user needs a test drive, finance info, or other specific action in the sales flow.
    """
    user_lower = normalize_text(user_text)
    
    # 0. Pure Knowledge / Question Check
    # If the user is asking a question (especially with "what", "how", "why"),
    # we should generally NOT trigger a follow-on immediately unless it's a direct request.
    question_starters = ["what", "how", "why", "when", "can i", "is there", "are there", "tell me about"]
    is_question = any(user_lower.startswith(s) for s in question_starters) or "?" in user_lower
    
    # 1. Human / Advisor / Agent
    agent_keywords = ["advisor", "agent", "human", "talk to someone", "customer care", "person", "representative"]
    if any(k in user_lower for k in agent_keywords):
        return "TALK_TO_ADVISOR"
    
    # 2. Test Drive intent (Action request, not just a question)
    test_drive_keywords = ["book test drive", "book testdrive", "schedule a drive", "i want a test drive", "take a trial"]
    if any(k in user_lower for k in test_drive_keywords):
        return "BOOK_TEST_DRIVE"
    
    # 3. Finance / EMI intent (Action request)
    finance_keywords = ["apply for finance", "apply for loan", "get a loan", "start emi process", "finance application"]
    if any(k in user_lower for k in finance_keywords):
        return "FINANCE_OPTIONS"

    # 4. LLM check for complex intent
    try:
        prompt = f"""Analyze the user message and decide if we should trigger an AUTOMATIC menu/action next.
USER: "{user_text}"
AI REPLY: "{ai_reply[:300]}"

INTENT OPTIONS:
- BOOK_TEST_DRIVE   -> User EXPLICITLY wants to book/schedule a drive now.
- FINANCE_OPTIONS   -> User EXPLICITLY wants to apply for a loan/finance now.
- TALK_TO_ADVISOR   -> User needs a human agent immediately.
- NONE              -> General question, inquiry, or information gathering.

CRITICAL: If the user is just asking a question (e.g., "what is the price", "what is the down payment", "how is the mileage"), return NONE.

Return JSON: {{"action": "BOOK_TEST_DRIVE|FINANCE_OPTIONS|TALK_TO_ADVISOR|NONE", "confidence": 0.0-1.0}}"""
        
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=80,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        if result.get("confidence", 0) >= 0.8:
            return result.get("action", "NONE")
    except:
        pass
        
    return "NONE"



def handle_sales_ai_query(phone: str, user_text: str,
                           user_state: dict = None) -> tuple:
    """
    DB and RAG-aware AI for Sales flow fallback.
    Returns: (reply, follow_on)
    """
    db_context = _get_sales_db_context()
    rag_context = get_rag_context(user_text, module="sales")
    current_state = user_state.get("state", "UNKNOWN") if user_state else "UNKNOWN"

    system_content = (
        f"{_SALES_SYSTEM}\n\n"
        f"USER'S CURRENT FLOW STEP: {current_state}\n"
        f"Use this step to understand where the user is in their journey.\n\n"
        f"=== LIVE DATABASE CONTEXT (Inventory) ===\n{db_context}\n\n"
        f"=== SALES KNOWLEDGE CONTEXT (FAQ/RAG) ===\n{rag_context}\n\n"
        f"Use the above database and knowledge context to answer the customer query accurately."
    )

    history = _get_recent_conversation_history(phone, limit=6, flow_type="sales")
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.4,
            max_tokens=800,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ai] Groq error (sales): {e}")
        reply = _keyword_fallback_sales(user_text)

    follow_on = _detect_sales_follow_on_action(user_text, reply)
    _store_conversation(phone, user_text, reply, flow_type="sales")
    return reply, follow_on



def _detect_used_cars_follow_on_action(user_text: str, ai_reply: str) -> str:
    """
    Detect if the user needs a live agent or other specific action in the used cars flow.
    """
    user_lower = normalize_text(user_text)
    reply_lower = ai_reply.lower()
    
    # Priority 1: Explicit human/agent keywords in user message
    agent_keywords = [
        "talk to advisor", "talk to agent", "speak with human", "customer care",
        "human help", "connect to agent", "talk to someone", "call me",
        "representative", "person", "live agent", "real person", "human agent"
    ]
    if any(k in user_lower for k in agent_keywords):
        return "TALK_TO_ADVISOR"
    
    # Priority 2: Complex negotiation/pricing topics (Direct/LiveAgent Required)
    # Questions about specific discounts or price matching are best handled by humans
    negotiation_keywords = [
        "discount", "offer", "best price", "cheapest", "negotiate",
        "price match", "competitor price", "exchange bonus", "exchange offer",
        "valuation mismatch", "price is high", "too expensive"
    ]
    if any(k in user_lower for k in negotiation_keywords):
        return "TALK_TO_ADVISOR"

    # Priority 3: AI suggesting a human in its reply
    # If the AI itself says "I can connect you" or "speak with our team", 
    # we should trigger the confirmation flow.
    suggestion_keywords = [
        "connect you with our", "speak with our team", "sales team will help",
        "contact you shortly", "advisor will call", "transfer you", "connect you to"
    ]
    if any(k in reply_lower for k in suggestion_keywords):
        return "TALK_TO_ADVISOR"

    # Priority 4: LLM check for complex intent
    try:
        prompt = f"""Analyze the user message and determine if they need a LIVE AGENT.
USER: "{user_text}"
AI REPLY: "{ai_reply[:300]}"

Criteria for LIVE AGENT:
- User wants to negotiate price.
- User has a complex complaint.
- User explicitly asked for a human.
- AI suggested connecting with a team/human.

Return JSON: {{"action": "TALK_TO_ADVISOR" or "NONE", "confidence": 0.0-1.0}}"""
        
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=80,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        if result.get("action") == "TALK_TO_ADVISOR" and result.get("confidence", 0) >= 0.7:
            return "TALK_TO_ADVISOR"
    except:
        pass
        
    return "NONE"

def handle_used_cars_ai_query(phone: str, user_text: str,
                               user_state: dict = None) -> tuple:
    """
    DB and RAG aware AI for Used Cars flow.
    Returns: (reply, suggested_follow_on)
    """
    budget_min = budget_max = None
    if user_state:
        budget_min = user_state.get("budget_min")
        budget_max = user_state.get("budget_max")

    db_context = _get_used_cars_db_context(budget_min, budget_max)
    targeted_context = _get_targeted_car_details(user_text)
    rag_context = get_rag_context(user_text, module="usedcars")

    current_state = user_state.get("state", "UNKNOWN") if user_state else "UNKNOWN"
    system_content = (
        f"{_USED_CARS_SYSTEM}\n\n"
        f"USER'S CURRENT FLOW STEP: {current_state}\n"
        f"Use this step to understand where the user is in their journey.\n\n"
        f"=== TARGETED VEHICLE INFO ===\n{targeted_context}\n\n"
        f"=== STOCK CONTEXT (Direct) ===\n{db_context}\n\n"
        f"=== KNOWLEDGE CONTEXT (Static) ===\n{rag_context}"
    )

    history = _get_recent_conversation_history(phone, limit=6, flow_type="used_cars")

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.4,
            max_tokens=800,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ai] Groq error (used_cars): {e}")
        reply = _keyword_fallback_used_cars(user_text)

    follow_on = _detect_used_cars_follow_on_action(user_text, reply)
    _store_conversation(phone, user_text, reply, flow_type="used_cars")
    return reply, follow_on


def _detect_service_follow_on_action(user_text: str, ai_reply: str) -> str:
    """
    Classifies the user's intent for the service flow after an AI answer.
    Returns: BOOK_APPOINTMENT | GET_ESTIMATE | SERVICE_HISTORY | TALK_TO_ADVISOR | NONE
    """
    t = user_text.lower()
    
    # 1. Human / Advisor / Emergency escalation (TALK_TO_ADVISOR)
    human_keywords = [
        "live agent", "human", "real person", "talk to someone", "speak to someone",
        "advisor", "contact advisor", "technical support", "manager", "complaint",
        "strange noise", "smoke", "brake", "clutch", "engine light", "overheating",
        "accident", "breakdown", "emergency", "useless bot", "not helping",
        "leak", "vibration", "steering", "gear", "abs", "airbag", "fault"
    ]
    if any(k in t for k in human_keywords):
        return "TALK_TO_ADVISOR"
    
    # 2. Booking intent
    book_keywords = ["book", "appointment", "schedule", "slot", "reserve", "timing"]
    if any(k in t for k in book_keywords):
        return "BOOK_APPOINTMENT"
    
    # 3. Estimate intent
    estimate_keywords = ["estimate", "cost", "price", "how much", "quote", "charge"]
    if any(k in t for k in estimate_keywords):
        return "GET_ESTIMATE"
    
    # 4. History intent
    history_keywords = ["history", "past service", "previous", "record", "last service", "bill amount"]
    if any(k in t for k in history_keywords):
        return "SERVICE_HISTORY"
    
    # 5. LLM-based refined classification for complex natural language
    try:
        prompt = f"""You are an intent classifier for a car service chatbot.
DECIDE if the customer wants to perform a SPECIFIC ACTION or just asking a general question.

CUSTOMER: "{user_text}"
BOT REPLY: "{ai_reply[:200]}"

INTENTS:
- BOOK_APPOINTMENT -> Wants to schedule a service visit
- GET_ESTIMATE      -> Wants to know the price for their specific service
- SERVICE_HISTORY   -> Wants to see past records or bill amounts
- TALK_TO_ADVISOR   -> Needs a human agent, has a complex technical problem, or is frustrated
- NONE              -> General knowledge question about cars/service center

Respond ONLY with valid JSON:
{{"action": "<BOOK_APPOINTMENT|GET_ESTIMATE|SERVICE_HISTORY|TALK_TO_ADVISOR|NONE>", "confidence": <0.0-1.0>}}"""

        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=50,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        if result.get("confidence", 0) >= 0.7:
            return result.get("action", "NONE")
    except:
        pass

    return "NONE"


def handle_service_ai_query(phone: str, user_text: str,
                             vehicle_reg: str = None) -> tuple:
    """
    DB and RAG-aware AI for Service flow.
    Returns: (reply, suggested_action)
    """
    db_context = _get_service_db_context(vehicle_reg)
    rag_context = get_rag_context(user_text, module="service")
    prompt = (
        f"{_SERVICE_SYSTEM}\n\n"
        f"=== LIVE DATABASE CONTEXT ===\n{db_context}\n\n"
        f"=== SERVICE KNOWLEDGE CONTEXT ===\n{rag_context}\n\n"
        f"=== CUSTOMER QUERY ===\n{user_text}"
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=600,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ai] Groq error (service): {e}")
        reply = _keyword_fallback_service(user_text)

    # Detect follow-on action (e.g. should we hand off to live agent?)
    action = _detect_service_follow_on_action(user_text, reply)
    
    _store_conversation(phone, user_text, reply,
                        flow_type="service", vehicle_reg=vehicle_reg)
    
    return reply, action


# ════════════════════════════════════════════════════════════
# 7.  INIT — ensure table exists on import
# ════════════════════════════════════════════════════════════
_ensure_conversation_log_table()