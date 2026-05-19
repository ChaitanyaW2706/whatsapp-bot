from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
load_dotenv()
from webhook import handle_message
import mysql.connector
import uuid
import time
from config import DB_CONFIG, ACCESS_TOKEN, USER_STATE
import base64
import os
from fastapi import Response
from flows.service import router as service_router, WhatsAppBot, DatabaseManager
from werkzeug.utils import secure_filename
import shutil
from vector_db import vector_service
from mongo_knowledge import store_knowledge_in_mongo, check_knowledge_exists, delete_knowledge_by_file, knowledge_collection
from document_processor import (
    process_excel,
    process_word,
    process_pdf,
    process_text,
    process_csv,
    process_pptx,
)

load_dotenv()
BASE_URL = os.getenv("BASE_URL")
UPLOAD_FOLDER = "uploads/vehicle_images"

# ── In-memory session store (no extra library needed) ──
# { session_token: { "agent_id": 11, "email": "x@y.com" } }
AGENT_SESSIONS: dict = {}

def create_session(agent_id: int, email: str) -> str:
    token = str(uuid.uuid4())
    AGENT_SESSIONS[token] = {"agent_id": agent_id, "email": email}
    return token

def get_session(token: str) -> dict | None:
    return AGENT_SESSIONS.get(token)

def delete_session(token: str):
    AGENT_SESSIONS.pop(token, None)
from utils import format_mobile, generate_upload_token, get_user_from_token, revoke_upload_token

app = FastAPI()
app.include_router(service_router)


from fastapi.templating import Jinja2Templates
import hashlib

templates = Jinja2Templates(directory="templates")

# ── Register GET ──
@app.get("/agent/register", response_class=HTMLResponse)
async def agent_register_page(request: Request):
    return templates.TemplateResponse("agent_register.html", {"request": request})

@app.post("/agent/register")
async def agent_register(request: Request):
    try:
        data = await request.json()
        agent_name = data.get("agent_name")
        email = data.get("email")
        password = data.get("password")
        if not all([agent_name, email, password]):
            return JSONResponse(status_code=400, content={"success": False, "message": "All fields are required!"})
        hashed = hashlib.sha256(password.encode()).hexdigest()
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO agents (agent_name, email, password) VALUES (%s, %s, %s)", (agent_name, email, hashed))
        conn.commit(); cursor.close(); conn.close()
        return JSONResponse(content={"success": True, "message": "Agent registered successfully!"})
    except mysql.connector.IntegrityError:
        return JSONResponse(status_code=400, content={"success": False, "message": "Email already exists!"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"Registration failed: {e}"})

@app.get("/agent/login", response_class=HTMLResponse)
async def agent_login_page(request: Request):
    return templates.TemplateResponse("agent_login.html", {"request": request})

@app.post("/agent/login")
async def agent_login(request: Request):
    try:
        data = await request.json()
        email = data.get("email")
        password = data.get("password")
        if not all([email, password]):
            return JSONResponse(status_code=400, content={"success": False, "message": "Email and password required!"})
        hashed = hashlib.sha256(password.encode()).hexdigest()
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, agent_name, email FROM agents WHERE email = %s AND password = %s", (email, hashed))
        agent = cursor.fetchone()
        if agent:
            cursor.execute("UPDATE agents SET status = 'online', last_active = NOW() WHERE id = %s", (agent['id'],))
            conn.commit(); cursor.close(); conn.close()
            session_token = create_session(agent['id'], agent['email'])
            response = JSONResponse(content={"success": True, "agent_id": agent['id'], "agent_name": agent['agent_name'], "redirect": f"/agent/dashboard/{agent['id']}"})
            response.set_cookie(key="agent_session", value=session_token, httponly=True, max_age=86400, samesite="lax")
            return response
        cursor.close(); conn.close()
        return JSONResponse(status_code=401, content={"success": False, "message": "Invalid email or password!"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"Login failed: {e}"})

@app.get("/agent/dashboard/{agent_id}", response_class=HTMLResponse)
async def agent_dashboard(request: Request, agent_id: int):
    try:
        # Validate session cookie
        token = request.cookies.get("agent_session")
        if not token:
            return HTMLResponse('<script>window.location.href="/agent/login";</script>')
        
        session = get_session(token)
        if not session:
            return HTMLResponse('<script>window.location.href="/agent/login";</script>')
        
        # URL agent_id must match session agent_id
        if session["agent_id"] != agent_id:
            real_id = session["agent_id"]
            return HTMLResponse(f'<script>window.location.href="/agent/dashboard/{real_id}";</script>')
        
        return templates.TemplateResponse("agent_dashboard.html", {"request": request, "agent_id": agent_id})
    except Exception as e:
        return HTMLResponse(f"Template error: {e}", status_code=500)

@app.post("/agent/logout")
async def agent_logout(request: Request):
    token = request.cookies.get("agent_session")
    if token:
        delete_session(token)
    response = JSONResponse(content={"success": True})
    response.delete_cookie("agent_session")
    return response

@app.post("/agent/api/heartbeat/{agent_id}")
async def agent_heartbeat(agent_id: int):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("UPDATE agents SET last_active = NOW(), status = 'online' WHERE id = %s", (agent_id,))
        conn.commit(); cursor.close(); conn.close()
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/agent/api/pending-customers/{agent_id}")
async def get_pending_customers(agent_id: int):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT h.id, h.phone_number, h.vehicle_reg, h.request_timestamp,
                   h.handoff_reason, h.status, h.ai_conversation_history
            FROM chat_handoffs h
            WHERE h.status IN ('requested', 'pending')
            ORDER BY h.request_timestamp ASC
        """)
        customers = cursor.fetchall()
        for c in customers:
            c['requesttimestamp'] = c['request_timestamp'].strftime('%Y-%m-%d %H:%M:%S') if c['request_timestamp'] else 'Unknown'
            phone = c.get('phone_number', 'Unknown')
            vehicle = c.get('vehicle_reg', '')
            c['phonenumber'] = phone
            c['vehiclereg'] = vehicle or 'Not provided'
            c['handoffreason'] = c.pop('handoff_reason', 'Not specified')
            c['aiconversationhistory'] = c.pop('ai_conversation_history', '')
            c.pop('request_timestamp', None)
            c.pop('phone_number', None)
            c.pop('vehicle_reg', None)

            # Fetch uploaded images for this customer
            try:
                if vehicle:
                    cursor.execute("""
                        SELECT iur.service_type, iur.image_count, iur.upload_timestamp,
                               GROUP_CONCAT(ui.relative_path ORDER BY ui.id ASC SEPARATOR '||') as image_paths,
                               GROUP_CONCAT(ui.saved_filename ORDER BY ui.id ASC SEPARATOR '||') as image_names
                        FROM image_upload_requests iur
                        LEFT JOIN uploaded_images ui ON iur.request_id = ui.request_id
                        WHERE iur.phone_number = %s AND iur.vehicle_reg = %s
                        GROUP BY iur.request_id, iur.service_type, iur.image_count, iur.upload_timestamp
                        ORDER BY iur.upload_timestamp DESC LIMIT 1
                    """, (phone, vehicle))
                else:
                    cursor.execute("""
                        SELECT iur.service_type, iur.image_count, iur.upload_timestamp,
                               GROUP_CONCAT(ui.relative_path ORDER BY ui.id ASC SEPARATOR '||') as image_paths,
                               GROUP_CONCAT(ui.saved_filename ORDER BY ui.id ASC SEPARATOR '||') as image_names
                        FROM image_upload_requests iur
                        LEFT JOIN uploaded_images ui ON iur.request_id = ui.request_id
                        WHERE iur.phone_number = %s
                        GROUP BY iur.request_id, iur.service_type, iur.image_count, iur.upload_timestamp
                        ORDER BY iur.upload_timestamp DESC LIMIT 1
                    """, (phone,))

                img_row = cursor.fetchone()
                if img_row and img_row.get('image_paths'):
                    paths = img_row['image_paths'].split('||')
                    names = (img_row['image_names'] or '').split('||')
                    c['uploaded_images'] = [
                        {'url': f"/{p.strip()}", 'filename': names[i] if i < len(names) else f"image_{i+1}"}
                        for i, p in enumerate(paths) if p.strip()
                    ]
                    c['image_service_type'] = img_row.get('service_type', '')
                    c['image_count'] = img_row.get('image_count', 0)
                    upload_ts = img_row.get('upload_timestamp')
                    c['image_upload_time'] = upload_ts.strftime('%Y-%m-%d %H:%M:%S') if upload_ts else ''
                else:
                    c['uploaded_images'] = []
                    c['image_service_type'] = ''
                    c['image_count'] = 0
                    c['image_upload_time'] = ''
            except Exception:
                c['uploaded_images'] = []
                c['image_service_type'] = ''
                c['image_count'] = 0
                c['image_upload_time'] = ''

        cursor.close(); conn.close()
        return JSONResponse(content=customers)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to get pending customers: {e}"})

@app.post("/agent/api/accept-customer")
async def accept_customer(request: Request):
    try:
        data = await request.json()
        agent_id = data.get('agent_id')
        handoff_id = data.get('handoff_id')
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT phone_number, vehicle_reg FROM chat_handoffs WHERE id = %s", (handoff_id,))
        handoff = cursor.fetchone()
        if not handoff:
            cursor.close(); conn.close()
            return JSONResponse(content={"success": False, "message": "Customer not found!"})
        cursor.execute("UPDATE chat_handoffs SET status = 'connected', agent_id = %s, connected_timestamp = NOW() WHERE id = %s", (agent_id, handoff_id))
        cursor.execute("SELECT agent_name FROM agents WHERE id = %s", (agent_id,))
        agent = cursor.fetchone()
        conn.commit(); cursor.close(); conn.close()
        return JSONResponse(content={"success": True, "message": "Customer accepted successfully!"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"Failed to accept customer: {e}"})

@app.get("/agent/api/chat-history/{handoff_id}")
async def get_chat_history(handoff_id: int):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT phone_number, vehicle_reg, ai_conversation_history, handoff_reason FROM chat_handoffs WHERE id = %s", (handoff_id,))
        handoff = cursor.fetchone()
        cursor.execute("SELECT sender_type, sender_name, message_text, message_type, media_url, timestamp FROM live_chat_messages WHERE handoff_id = %s ORDER BY timestamp ASC", (handoff_id,))
        live_messages = cursor.fetchall()
        for msg in live_messages:
            msg['timestamp'] = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        cursor.close(); conn.close()
        return JSONResponse(content={"handoff": handoff, "live_messages": live_messages})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to get chat history: {e}"})

@app.post("/agent/api/send-message")
async def agent_send_message(request: Request):
    try:
        data = await request.json()
        agent_id = data.get('agent_id')
        handoff_id = data.get('handoff_id')
        message = data.get('message')
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT h.phone_number, a.agent_name
            FROM chat_handoffs h JOIN agents a ON h.agent_id = a.id
            WHERE h.id = %s AND a.id = %s
        """, (handoff_id, agent_id))
        result = cursor.fetchone()
        if not result:
            cursor.close(); conn.close()
            return JSONResponse(content={"success": False, "message": "Invalid handoff or agent"})
        phone_number = result['phone_number']
        agent_name = result['agent_name']
        cursor.execute("""
            INSERT INTO live_chat_messages (handoff_id, sender_type, sender_name, message_text, timestamp, is_read)
            VALUES (%s, 'agent', %s, %s, NOW(), 1)
        """, (handoff_id, agent_name, message))
        conn.commit(); cursor.close(); conn.close()
        from webhook import send_whatsapp_message
        send_whatsapp_message(phone_number, message)
        return JSONResponse(content={"success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"Failed to send message: {e}"})


@app.post("/agent/api/send-media")
async def agent_send_media(
    agent_id: int = Form(...),
    handoff_id: int = Form(...),
    media_type: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...)
):
    """Agent sends media to customer"""
    try:
        db_mgr = DatabaseManager()
        conn = db_mgr.get_connection()
        if not conn:
            return JSONResponse(status_code=500, content={"success": False, "message": "DB error"})

        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT h.phone_number, a.agent_name FROM chat_handoffs h JOIN agents a ON h.agent_id = a.id WHERE h.id = %s AND a.id = %s", (handoff_id, agent_id))
        result = cursor.fetchone()

        if not result:
            cursor.close()
            conn.close()
            return JSONResponse(status_code=404, content={"success": False, "message": "Invalid IDs"})

        phone_number = result['phone_number']
        agent_name = result['agent_name']
        
        # Save file locally
        filename = secure_filename(file.filename)
        timestamp = int(data_timestamp := time.time())
        file_path = os.path.join(UPLOAD_FOLDER, f"{int(data_timestamp)}_{filename}")
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # For external WhatsApp API, we need a public URL
        public_url = f"{BASE_URL}/uploads/vehicle_images/{int(data_timestamp)}_{filename}"
        
        bot = WhatsAppBot()
        success = bot.send_media(
            phone_number=phone_number,
            media_type=media_type,
            media_url=public_url,
            caption=caption,
            filename=filename
        )

        if success:
            bot.store_live_chat_message(
                handoff_id=handoff_id,
                sender_type='agent',
                sender_name=agent_name,
                message_text=caption or filename,
                message_type=media_type,
                media_url=public_url
            )
            cursor.close()
            conn.close()
            return JSONResponse(content={"success": True, "media_url": public_url})
        else:
            cursor.close()
            conn.close()
            return JSONResponse(status_code=500, content={"success": False, "message": "WhatsApp API error"})

    except Exception as e:
        import logging
        logging.error(f"Error sending agent media: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})

@app.post("/agent/api/end-chat")
async def end_agent_chat(request: Request):
    try:
        data = await request.json()
        agent_id = data.get('agent_id')
        handoff_id = data.get('handoff_id')
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT phone_number FROM chat_handoffs WHERE id = %s", (handoff_id,))
        result = cursor.fetchone()
        if not result:
            cursor.close(); conn.close()
            return JSONResponse(content={"success": False, "message": "Handoff not found"})
        cursor.execute("UPDATE chat_handoffs SET status = 'completed', completed_timestamp = NOW() WHERE id = %s", (handoff_id,))
        cursor.execute("UPDATE agents SET total_chats = total_chats + 1 WHERE id = %s", (agent_id,))
        conn.commit(); cursor.close(); conn.close()

        phone_number = result['phone_number']

        # ── Clear AWAITING_AGENT state so the bot can resume normally ──
        from config import USER_STATE
        if phone_number in USER_STATE:
            # Preserve session_id but reset flow state to MENU
            session_id = USER_STATE[phone_number].get("session_id")
            USER_STATE[phone_number] = {"state": "MENU"}
            if session_id:
                USER_STATE[phone_number]["session_id"] = session_id

        # ── Notify customer and show main menu ──
        from webhook import send_whatsapp_message, send_list_message
        send_whatsapp_message(
            phone_number,
            "✅ *Your chat with the live agent has ended.*\n\n"
            "🤖 I'm back to assist you! How can I help you today?"
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
        send_list_message(phone_number, "What would you like to do next?", "View Options", sections)

        return JSONResponse(content={"success": True, "message": "Chat ended successfully"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": f"Failed to end chat: {e}"})

@app.get("/agent/api/all-customers/{agent_id}")
async def get_all_customers(agent_id: int):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT
                h.phone_number,
                MAX(h.id) as latest_handoff_id,
                (
                  SELECT GROUP_CONCAT(DISTINCT v.vehicle_reg ORDER BY v.last_ts DESC SEPARATOR ', ')
                  FROM (
                    SELECT sub.vehicle_reg,
                           GREATEST(
                             COALESCE(sub.connected_timestamp, '1900-01-01'),
                             COALESCE(sub.request_timestamp,   '1900-01-01'),
                             COALESCE(sub.completed_timestamp, '1900-01-01')
                           ) AS last_ts
                    FROM chat_handoffs AS sub
                    WHERE sub.phone_number = h.phone_number
                      AND sub.vehicle_reg IS NOT NULL
                      AND sub.vehicle_reg <> ''
                  ) AS v
                ) AS all_vehicles,
                MAX(h.connected_timestamp) as last_connected,
                MAX(h.completed_timestamp) as last_completed,
                (SELECT CASE WHEN lm.message_type = 'image' THEN '📷 Image' WHEN lm.message_type = 'document' THEN '📄 Document' ELSE lm.message_text END
                 FROM live_chat_messages lm
                 JOIN chat_handoffs h2 ON lm.handoff_id = h2.id
                 WHERE h2.phone_number = h.phone_number AND h2.agent_id = %s
                 ORDER BY lm.timestamp DESC LIMIT 1) as last_message,
                (SELECT lm.timestamp
                 FROM live_chat_messages lm
                 JOIN chat_handoffs h2 ON lm.handoff_id = h2.id
                 WHERE h2.phone_number = h.phone_number AND h2.agent_id = %s
                 ORDER BY lm.timestamp DESC LIMIT 1) as last_message_time,
                (SELECT lm.sender_type
                 FROM live_chat_messages lm
                 JOIN chat_handoffs h2 ON lm.handoff_id = h2.id
                 WHERE h2.phone_number = h.phone_number AND h2.agent_id = %s
                 ORDER BY lm.timestamp DESC LIMIT 1) as last_sender,
                COUNT(CASE WHEN lm.is_read = 0 AND lm.sender_type = 'customer' THEN 1 END) as unread_count,
                (SELECT COUNT(*)
                   FROM chat_handoffs h3
                  WHERE h3.phone_number = h.phone_number
                    AND h3.agent_id = %s
                    AND h3.status = 'connected') as active_chats
            FROM chat_handoffs h
            LEFT JOIN live_chat_messages lm ON h.id = lm.handoff_id
            WHERE h.agent_id = %s
            GROUP BY h.phone_number
            ORDER BY GREATEST(
                COALESCE((SELECT MAX(lm2.timestamp)
                            FROM live_chat_messages lm2
                            JOIN chat_handoffs h4 ON lm2.handoff_id = h4.id
                           WHERE h4.phone_number = h.phone_number
                             AND h4.agent_id = %s), '1900-01-01'),
                COALESCE(MAX(h.connected_timestamp), '1900-01-01')
            ) DESC
        """, (agent_id, agent_id, agent_id, agent_id, agent_id, agent_id))
        customers = cursor.fetchall()
        for customer in customers:
            if customer.get('last_message_time'):
                customer['last_message_time'] = customer['last_message_time'].strftime('%Y-%m-%d %H:%M:%S')
            if customer.get('last_connected'):
                customer['last_connected'] = customer['last_connected'].strftime('%Y-%m-%d %H:%M:%S')
            if customer.get('last_completed'):
                customer['last_completed'] = customer['last_completed'].strftime('%Y-%m-%d %H:%M:%S')
            if customer.get('last_message') and len(customer['last_message']) > 50:
                customer['last_message'] = customer['last_message'][:50] + '...'
            if customer.get('all_vehicles'):
                raw_list = [v.strip() for v in customer['all_vehicles'].split(',') if v.strip()]
                seen, ordered = set(), []
                for v in raw_list:
                    if v not in seen:
                        seen.add(v); ordered.append(v)
                if ordered:
                    display = ordered[0]
                    if len(ordered) > 1: display += f", {ordered[1]}"
                    if len(ordered) > 2: display += f" (+{len(ordered)-2} more)"
                    customer['display_vehicles'] = display
                else:
                    customer['display_vehicles'] = 'No vehicles'
            else:
                customer['display_vehicles'] = 'No vehicles'
        cursor.close(); conn.close()
        return JSONResponse(content={'customers': customers})
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': f'Failed to get customers: {e}'})

@app.get("/agent/api/customer-full-chat/{agent_id}/{phone_number}")
async def get_customer_full_chat(agent_id: int, phone_number: str):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, vehicle_reg, status, connected_timestamp, completed_timestamp,
                   ai_conversation_history, handoff_reason, request_timestamp
            FROM chat_handoffs
            WHERE phone_number = %s AND agent_id = %s
            ORDER BY CASE WHEN status = 'connected' THEN 1 WHEN status = 'completed' THEN 2 ELSE 3 END,
                     connected_timestamp DESC
            LIMIT 1
        """, (phone_number, agent_id))
        handoff = cursor.fetchone()
        if not handoff:
            cursor.close(); conn.close()
            return JSONResponse(content={'handoff': None, 'allmessages': []})
        cursor.execute("""
            SELECT id FROM chat_handoffs WHERE phone_number = %s AND agent_id = %s
        """, (phone_number, agent_id))
        all_handoff_ids = [h['id'] for h in cursor.fetchall()]
        
        cursor.execute("""
            SELECT handoff_id, sender_type, sender_name, message_text, message_type, media_url, timestamp, is_read
            FROM live_chat_messages WHERE handoff_id IN ({}) ORDER BY timestamp ASC
        """.format(','.join(['%s']*len(all_handoff_ids))), all_handoff_ids)
        all_messages = cursor.fetchall()
        
        # Get all handoffs for this customer to return full session history
        cursor.execute("""
            SELECT id, vehicle_reg, status, connected_timestamp, completed_timestamp,
                   ai_conversation_history, handoff_reason, request_timestamp
            FROM chat_handoffs
            WHERE phone_number = %s AND agent_id = %s
            ORDER BY request_timestamp ASC
        """, (phone_number, agent_id))
        handoffs = cursor.fetchall()

        for h in handoffs:
            for key in ['connected_timestamp', 'completed_timestamp', 'request_timestamp']:
                if h.get(key):
                    h[key] = h[key].strftime('%Y-%m-%d %H:%M:%S')
                    
        for msg in all_messages:
            if msg.get('timestamp'):
                msg['timestamp'] = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.close(); conn.close()
        return JSONResponse(content={'handoffs': handoffs, 'allmessages': all_messages})
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': f'Failed to get customer chat: {e}'})

@app.post("/agent/api/start-new-chat")
async def start_new_chat(request: Request):
    try:
        data = await request.json()
        agent_id = data.get('agent_id')
        phone_number = data.get('phone_number')
        if not all([agent_id, phone_number]):
            return JSONResponse(status_code=400, content={"success": False, "error": "Missing agent_id or phone_number"})
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT vehicle_reg FROM chat_handoffs WHERE phone_number = %s AND agent_id = %s AND vehicle_reg IS NOT NULL ORDER BY connected_timestamp DESC LIMIT 1", (phone_number, agent_id))
        result = cursor.fetchone()
        vehicle_reg = result['vehicle_reg'] if result else ''
        cursor.execute("""
            INSERT INTO chat_handoffs (phone_number, vehicle_reg, agent_id, status, request_timestamp, connected_timestamp, handoff_reason)
            VALUES (%s, %s, %s, 'connected', NOW(), NOW(), 'Agent initiated new chat')
        """, (phone_number, vehicle_reg, agent_id))
        new_handoff_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO live_chat_messages (handoff_id, sender_type, sender_name, message_text, timestamp, is_read)
            VALUES (%s, 'system', 'System', 'New chat session started by agent', NOW(), 1)
        """, (new_handoff_id,))
        conn.commit(); cursor.close(); conn.close()
        from webhook import send_whatsapp_message
        send_whatsapp_message(phone_number, "Hello! One of our service agents would like to assist you. How can we help you today?")
        return JSONResponse(content={'success': True, 'handoff_id': new_handoff_id})
    except Exception as e:
        return JSONResponse(status_code=500, content={'success': False, 'error': str(e)})

@app.post("/agent/api/mark-messages-read")
async def mark_messages_read(request: Request):
    try:
        data = await request.json()
        phone_number = data.get('phone_number')
        agent_id = data.get('agent_id')
        if not all([phone_number, agent_id]):
            return JSONResponse(status_code=400, content={"success": False, "error": "Missing phone_number or agent_id"})
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE live_chat_messages lm
            JOIN chat_handoffs h ON lm.handoff_id = h.id
            SET lm.is_read = 1
            WHERE h.phone_number = %s AND h.agent_id = %s
              AND lm.sender_type = 'customer' AND lm.is_read = 0
        """, (phone_number, agent_id))
        conn.commit(); cursor.close(); conn.close()
        return JSONResponse(content={'success': True})
    except Exception as e:
        return JSONResponse(status_code=500, content={'success': False, 'error': str(e)})


# ===================================
# MOUNT STATIC FILES FOR CAR IMAGES AND BROCHURES
# ===================================
# Create static directories if they don't exist
os.makedirs("static/cars", exist_ok=True)
os.makedirs("static/brochures", exist_ok=True)
os.makedirs("uploads/vehicle_images", exist_ok=True)

# Mount static files to serve car images and brochures
app.mount("/static", StaticFiles(directory="static"), name="static")
# Mount uploads directory to serve vehicle images uploaded by customers
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ===============================
# META WEBHOOK VERIFICATION
# ===============================
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params

    hub_mode = params.get("hub.mode")
    hub_token = params.get("hub.verify_token")
    hub_challenge = params.get("hub.challenge")

    from config import VERIFY_TOKEN

    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
        print("✅ Webhook verified successfully")
        return int(hub_challenge)

    return "Verification failed"


# ===============================
# WHATSAPP MESSAGE RECEIVER
# ===============================
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    handle_message(data)
    return {"status": "ok"}


# ===============================
# UPLOAD ENDPOINTS (NEW)
# ===============================

# 1. Serve HTML upload page
@app.get("/upload-estimate")
async def serve_upload_page(token: str):
    """
    Serve the HTML upload page with token validation
    URL: /upload-estimate?token=YOUR_TOKEN
    """
    # Verify token exists
    user_data = get_user_from_token(token)
    if not user_data:
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Invalid Link</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                    .error { color: #dc3545; }
                </style>
            </head>
            <body>
                <h1 class="error">⚠️ Invalid or Expired Link</h1>
                <p>This upload link is invalid or has expired.</p>
                <p>Please request a new link from the WhatsApp chat.</p>
            </body>
            </html>
        """)
    
    # Read and return HTML template
    try:
        with open("templates/upload_estimate.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        
        # Replace placeholders with actual data (optional)
        html_content = html_content.replace('id="displayName">Loading...', f'id="displayName">{user_data["name"]}')
        html_content = html_content.replace('id="displayMobile">Loading...', f'id="displayMobile">{user_data["mobile"]}')
        html_content = html_content.replace('id="displayVehicle">Loading...', f'id="displayVehicle">{user_data["vehicle"]}')
        
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Upload Page Not Found</h1>
                <p>Please contact administrator.</p>
            </body>
            </html>
        """)

# 2. API to get user info (for JavaScript)
@app.get("/api/estimate-info")
async def get_estimate_info(token: str):
    """API endpoint for HTML page to fetch user data"""
    user_data = get_user_from_token(token)
    if not user_data:
        return JSONResponse({"success": False, "message": "Invalid or expired token"})
    
    return JSONResponse({
        "success": True,
        "mobile": user_data["mobile"],
        "name": user_data["name"],
        "vehicle": user_data["vehicle"]
    })

# 3. Handle document upload
@app.post("/api/upload-estimate")
async def upload_estimate_document(
    document: UploadFile = File(...),
    mobile: str = Form(...),
    name: str = Form(...),
    vehicle: str = Form(...),
    token: str = Form(...)
):
    """Handle document upload and store in database"""
    try:
        print(f"📤 Upload request from {mobile}, token: {token}")
        
        # Verify token
        user_data = get_user_from_token(token)
        if not user_data:
            print(f"❌ Invalid token: {token}")
            return JSONResponse({"success": False, "message": "Invalid or expired token"})
        
        # Verify mobile matches token
        if user_data["mobile"] != mobile:
            print(f"❌ Mobile mismatch: {mobile} != {user_data['mobile']}")
            return JSONResponse({"success": False, "message": "Invalid request"})
        
        # Read file data
        file_data = await document.read()
        file_size = len(file_data)
        
        # Check file size (5MB limit)
        if file_size > 5 * 1024 * 1024:
            print(f"❌ File too large: {file_size} bytes")
            return JSONResponse({"success": False, "message": "File size exceeds 5MB limit"})
        
        # Check file type
        allowed_types = ['application/pdf', 'image/jpeg', 'image/jpg', 'image/png']
        if document.content_type not in allowed_types:
            print(f"❌ Invalid file type: {document.content_type}")
            return JSONResponse({"success": False, "message": "Only PDF, JPG, PNG files are allowed"})
        
        # Get file info
        filename = document.filename
        content_type = document.content_type
        
        print(f"📄 Uploading file: {filename} ({file_size} bytes, {content_type})")
        
        # Connect to database
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # Insert into database
        cursor.execute("""
            INSERT INTO insurance_estimate_requests 
            (mobile_number, customer_name, vehicle_reg_no, 
             document_name, document_data, document_type, file_size, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (mobile, name, vehicle, filename, file_data, content_type, file_size))
        
        request_id = cursor.lastrowid
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"✅ Document saved to DB with ID: {request_id}")
        
        # Send WhatsApp confirmation
        from webhook import send_button_message
        
        confirmation_msg = (
            "✅ *Thank you for your request!*\n\n"
            "Your insurance estimate details have been received successfully.\n\n"
            "📋 *Our insurance advisor will now:*\n"
            "• Review your policy details\n"
            "• Compare the best insurer options\n"
            "• Apply eligible discounts / NCB\n"
            "• Contact you within *2 working hours*\n\n"
            "📞 If you need urgent assistance, reply here anytime 😊\n\n"
            "— *AutoSherpa Insurance Team* 🚗"
        )

        
        buttons = [
            {"type": "reply", "reply": {"id": "PREVIOUS_MENU", "title": "⬅️ Previous Menu"}},
            {"type": "reply", "reply": {"id": "END_CHAT", "title": "❌ End Conversation"}}
        ]
        
        send_button_message(mobile, confirmation_msg, buttons)
        print(f"✅ WhatsApp confirmation sent to {mobile}")
        
        # Clean up token from all stores
        revoke_upload_token(token)
        print(f"✅ Token cleaned up: {token}")
        
        return JSONResponse({
            "success": True,
            "message": "Document uploaded successfully",
            "request_id": request_id
        })
        
    except mysql.connector.Error as db_error:
        print(f"❌ Database error: {db_error}")
        return JSONResponse({
            "success": False,
            "message": "Database error occurred"
        })
    except Exception as e:
        print(f"❌ Upload error: {e}")
        return JSONResponse({
            "success": False, 
            "message": f"Error uploading document: {str(e)}"
        })

# 4. Health check endpoint
@app.get("/")
async def health_check():
    return {"status": "ok", "service": "AutoSherpa WhatsApp Bot"}

# 5. Clean expired tokens periodically (optional)
@app.on_event("startup")
async def startup_event():
    """Clean up expired tokens on startup"""
    import time
    from utils import UPLOAD_TOKENS
    print("SERVER STARTING: Server starting up...")
    expired_tokens = [t for t, data in UPLOAD_TOKENS.items()
                      if time.time() - data.get("timestamp", 0) > 3600]
    for t in expired_tokens:
        UPLOAD_TOKENS.pop(t, None)
    if expired_tokens:
        print(f"CLEANUP: Cleaned {len(expired_tokens)} expired tokens from UPLOAD_TOKENS")


# ===================================
# KNOWLEDGE INGESTION ENDPOINTS (RAG)
# ===================================

@app.get("/admin/knowledge", response_class=HTMLResponse)
async def admin_knowledge_page(request: Request):
    """Serve the admin page for knowledge ingestion"""
    return templates.TemplateResponse("admin_knowledge.html", {"request": request})

async def rebuild_vector_db():
    """
    Clears the Vector DB and re-populates it from all documents in MongoDB.
    This ensures the Vector DB is always in sync and has no duplicates.
    """
    print("🔄 Rebuilding Vector DB from MongoDB source of truth...")
    vector_service.clear_all()
    
    # Get all documents from MongoDB
    all_docs = list(knowledge_collection.find({}))
    
    for doc in all_docs:
        module = doc['module']
        filename = doc['filename']
        content = doc['content']
        
        # Add to Vector DB
        # We pass the content as a single text; VectorDB will chunk it automatically
        vector_service.add_documents([content], metadatas=[{"module": module, "source": filename}])
        
    print(f"✅ Vector DB rebuilt successfully with {len(all_docs)} documents.")

@app.post("/api/admin/upload-knowledge")
async def upload_knowledge(
    files: list[UploadFile] = File(...),
    module: str = Form(...)  # insurance, usedcars, sales, etc.
):
    """
    Endpoint to upload multiple Excel/Word/PDF files for knowledge base (RAG).
    Stores in Vector DB (Fiass) and MongoDB.
    """
    if not files:
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "No files provided."
        })

    processed_files = []
    errors = []

    for file in files:
        try:
            filename = file.filename
            file_content = await file.read()

            print(f"📥 Processing knowledge upload: {filename} for module: {module}")

            # --- SAFEGUARD: Prevent duplicate uploads ---
            if check_knowledge_exists(module, filename):
                print(f"⚠️ Document already exists: {filename} in {module}")
                errors.append(f"'{filename}' already exists in {module} module")
                continue
            # --------------------------------------------

            texts = []
            low_filename = filename.lower()
            if low_filename.endswith(('.xlsx', '.xls')):
                texts = process_excel(file_content)
            elif low_filename.endswith('.docx'):
                texts = process_word(file_content)
            elif low_filename.endswith('.doc'):
                errors.append(f"'{filename}': Legacy .doc files not supported. Save as .docx")
                continue
            elif low_filename.endswith('.pdf'):
                texts = process_pdf(file_content)
            elif low_filename.endswith('.txt'):
                texts = process_text(file_content)
            elif low_filename.endswith('.csv'):
                texts = process_csv(file_content)
            elif low_filename.endswith('.pptx'):
                try:
                    texts = process_pptx(file_content)
                except ImportError as exc:
                    errors.append(f"'{filename}': {str(exc)}")
                    continue
            else:
                errors.append(f"'{filename}': Unsupported format. Use Excel, Word (.docx), PDF, TXT, CSV, or PPTX")
                continue

            if not texts:
                errors.append(f"'{filename}': No text content found")
                continue

            # --- AUTO-CLEAR & REPLACE LOGIC ---
            # 1. Check and delete existing version from MongoDB to prevent duplicates
            if check_knowledge_exists(module, filename):
                print(f"♻️ Replacing existing document: {filename} in {module}")
                delete_knowledge_by_file(module, filename)

            # 2. Store the latest version in MongoDB (Source of Truth)
            full_text = "\n".join(texts)
            mongo_id = store_knowledge_in_mongo(module, filename, full_text)

            processed_files.append({
                "filename": filename,
                "mongo_id": str(mongo_id),
                "text_chunks": len(texts)
            })

            print(f"✅ Processed {filename}: {len(texts)} chunks, MongoDB ID: {mongo_id}")

        except Exception as e:
            print(f"❌ Error processing {file.filename}: {e}")
            errors.append(f"'{file.filename}': {str(e)}")

    # 3. AUTO-CLEAR Vector DB and Rebuild from MongoDB
    # This ensures the Vector DB is perfectly synced and old chunks are removed
    if processed_files:
        await rebuild_vector_db()

    # Prepare response
    success_count = len(processed_files)
    total_count = len(files)

    if success_count == 0:
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "No files were successfully processed.",
            "errors": errors
        })

    message = f"Successfully processed {success_count}/{total_count} files for {module} module."
    if errors:
        message += f" Errors: {'; '.join(errors)}"

    return JSONResponse(content={
        "success": True,
        "message": message,
        "processed_files": processed_files,
        "errors": errors if errors else None
    })


# ===================================
# CAR IMAGE ENDPOINT (ALTERNATIVE METHOD)
# ===================================
def get_car_image_base64_by_model(model_name: str):
    """Retrieve base64-encoded car image from database by model name"""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT image_data FROM car_images WHERE model_name = %s",
            (model_name,)
        )
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            return result[0]
        return None
    except Exception as e:
        print(f"❌ Error fetching car image: {e}")
        return None

@app.get("/car-image/{model_name}")
def serve_car_image(model_name: str):
    """
    Alternative endpoint to serve car images directly from database
    This can be used if static file serving doesn't work
    """
    b64 = get_car_image_base64_by_model(model_name)

    if not b64:
        return Response(content="Image not found", status_code=404)

    try:
        image_bytes = base64.b64decode(b64)
        
        return Response(
            content=image_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",  # Cache for 1 day
                "Content-Disposition": f"inline; filename={model_name}.jpg"
            }
        )
    except Exception as e:
        print(f"❌ Error decoding image: {e}")
        return Response(content="Error processing image", status_code=500)


# ===================================
# CHAT HISTORY API (WhatsApp-style)
# ===================================

from chat_history import get_all_sessions, get_session_chat, get_session_stats
from datetime import datetime as dt


@app.get("/api/chat/stats")
async def chat_stats():
    """
    Dashboard stats — total sessions, active, closed, by lead type.
    GET /api/chat/stats
    """
    try:
        stats = get_session_stats()
        return JSONResponse({"success": True, "data": stats})
    except Exception as e:
        print(f"❌ Stats error: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/chat/sessions")
async def list_sessions(
    phone: str = None,
    status: str = None,
    lead_type: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 50,
    skip: int = 0
):
    """
    List all chat sessions with optional filters.

    GET /api/chat/sessions
    GET /api/chat/sessions?phone=916379113226
    GET /api/chat/sessions?status=active
    GET /api/chat/sessions?status=closed
    GET /api/chat/sessions?lead_type=insurance
    GET /api/chat/sessions?lead_type=sales
    GET /api/chat/sessions?date_from=2026-02-01&date_to=2026-02-28
    GET /api/chat/sessions?phone=9876&status=closed&lead_type=service&limit=20&skip=0
    """
    try:
        df = dt.fromisoformat(date_from) if date_from else None
        dt_ = dt.fromisoformat(date_to) if date_to else None

        result = get_all_sessions(
            phone=phone,
            status=status,
            lead_type=lead_type,
            date_from=df,
            date_to=dt_,
            limit=limit,
            skip=skip
        )
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        print(f"❌ Sessions list error: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/chat/session/{session_id}")
async def get_chat(
    session_id: str,
    sender: str = None,
    keyword: str = None,
    date_from: str = None,
    date_to: str = None
):
    """
    Get full WhatsApp-style chat for one session with optional filters.

    GET /api/chat/session/{session_id}
    GET /api/chat/session/{session_id}?sender=user          ← only user messages
    GET /api/chat/session/{session_id}?sender=bot           ← only bot messages
    GET /api/chat/session/{session_id}?keyword=insurance    ← messages containing word
    GET /api/chat/session/{session_id}?date_from=2026-02-27T00:00:00
    GET /api/chat/session/{session_id}?sender=user&keyword=price

    Response message shape:
    {
        "sender": "user" | "bot",
        "side":   "right" | "left",     ← WhatsApp style
        "text":   "...",
        "ts":     "2026-02-27T05:15:38.969000"
    }
    """
    try:
        df = dt.fromisoformat(date_from) if date_from else None
        dt_ = dt.fromisoformat(date_to) if date_to else None

        result = get_session_chat(
            session_id=session_id,
            sender_filter=sender,
            keyword=keyword,
            date_from=df,
            date_to=dt_
        )

        if "error" in result:
            return JSONResponse({"success": False, "message": result["error"]}, status_code=404)

        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        print(f"❌ Session chat error: {e}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)