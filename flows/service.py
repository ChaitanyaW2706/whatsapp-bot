import os
import json
import logging
from datetime import datetime, timedelta
import threading
import time
import re
from urllib import response
import uuid
import hashlib
from typing import List, Optional, Dict, Any
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi import UploadFile, File, Form
from fastapi import APIRouter, Request
from typing import List
import uuid
from config import DB_CONFIG
router = APIRouter()
templates = Jinja2Templates(directory="templates")

import mysql.connector
from mysql.connector import Error
import traceback
from dotenv import load_dotenv
import requests
from utils import resolve_date_from_text, is_valid_appointment_slot

# FastAPI imports
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# Werkzeug for secure filenames
from werkzeug.utils import secure_filename

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(title="WhatsApp Bot API", version="1.0.0")

BASE_URL = os.getenv("BASE_URL")

# Setup templates
templates = Jinja2Templates(directory="templates")

# Logging configuration - UTF-8 encoding fix for Windows (emoji support)
import sys

_file_handler = logging.FileHandler("botlogs.txt", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

_stream_handler = logging.StreamHandler(stream=open(
    sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False
))
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _stream_handler]
)
logger = logging.getLogger(__name__)

# File upload configuration
UPLOAD_FOLDER = "uploads/vehicle_images"
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max

# Create upload directory
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ============ CONFIGURATION ============

load_dotenv()

# Then it reads each variable like this:
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')

# Configure Groq client
from llm_config import groq_client, MODEL_NAME as GROQ_MODEL

def groq_generate(prompt: str) -> str:
    """Call Groq API and return response text"""
    completion = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return completion.choices[0].message.content.strip()

logger.info(f"✅ Using Groq model: {GROQ_MODEL}")

# WhatsApp API Configuration
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'your_webhook_verify_token')
WHATSAPP_TOKEN = os.getenv('ACCESS_TOKEN') or os.getenv('WHATSAPP_TOKEN')  # Fixed: reads ACCESS_TOKEN first
PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')

if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    logger.warning("⚠️ WhatsApp credentials not fully configured")

# ============ DATABASE MANAGER ============

class DatabaseManager:
    """Handles all database operations"""
    
    def __init__(self):
        self.config = DB_CONFIG

    def get_connection(self):
        """Get database connection"""
        try:
            return mysql.connector.connect(**self.config)
        except Error as e:
            logger.error(f"Database connection error: {e}")
            return None

    def get_vehicle_details(self, registration_number: str) -> Optional[Dict]:
        """Get vehicle details from database"""
        conn = self.get_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor(dictionary=True)
            query = """
                SELECT r.*, 
                       b.nextServiceDue, 
                       b.nextServiceType
                FROM robillscube r
                LEFT JOIN bicoe_forecast_cube b 
                    ON r.vehicleRegNo = b.vehicleRegNo
                WHERE r.vehicleRegNo = %s
                ORDER BY r.lastServiceDate DESC
                LIMIT 1
            """
            cursor.execute(query, (registration_number,))
            result = cursor.fetchone()
            logger.info(f"Database query result for {registration_number}: {result}")
            return result
        except Error as e:
            logger.error(f"Error fetching vehicle details: {e}")
            return None
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def get_service_history(self, registration_number: str) -> List[Dict]:
        """Get service history for a vehicle"""
        conn = self.get_connection()
        if not conn:
            return []
        
        try:
            cursor = conn.cursor(dictionary=True)
            query = """
                SELECT billDate, billNumber, billAmt, lastServiceType,
                       lastServiceMeterReading, workshopName, location_name
                FROM robillscube
                WHERE vehicleRegNo = %s
                ORDER BY billDate DESC
            """
            cursor.execute(query, (registration_number,))
            return cursor.fetchall()
        except Error as e:
            logger.error(f"Error fetching service history: {e}")
            return []
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def store_conversation(self, phone_number: str, message: str, 
                          response: str, vehicle_reg: Optional[str] = None) -> bool:
        """
        Store conversation in conversation_log.
        Aligned with sample code: single-row logging.
        """
        conn = self.get_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            query = """
                INSERT INTO conversation_log
                (phone_number, user_message, bot_response, vehicle_reg, flow_type, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                phone_number, 
                message, 
                response, 
                vehicle_reg, 
                'service',
                datetime.now()
            ))
            conn.commit()
            logger.info(f"[service] ✅ Stored conversation for {phone_number}")
            return True
        except Error as e:
            logger.error(f"Error storing conversation: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()


    def get_service_estimate_data(self, registration_number: str) -> Optional[Dict]:
        """Get service estimate data using complex query"""
        conn = self.get_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor(dictionary=True)
            query = """
            SELECT 
                pl.cityid,
                pl.modelid,
                pl.fuelType,
                pl.mileageid,
                pl.labourAmount,
                pl.partsAmount,
                pl.total,
                CASE 
                    WHEN pl.total IS NOT NULL THEN 'Details Found'
                    ELSE 'Details Not found'
                END AS remarks
            FROM robillscube r
            LEFT JOIN pmsmodel pm ON r.model = pm.model
            LEFT JOIN pmsmileage pml ON (
                CASE 
                    WHEN r.lastServiceMeterReading <= 20000 THEN '20K'
                    WHEN r.lastServiceMeterReading <= 30000 THEN '30K'
                    WHEN r.lastServiceMeterReading <= 40000 THEN '40K'
                    WHEN r.lastServiceMeterReading <= 50000 THEN '50K'
                    WHEN r.lastServiceMeterReading <= 60000 THEN '60K'
                    WHEN r.lastServiceMeterReading <= 70000 THEN '70K'
                    WHEN r.lastServiceMeterReading <= 80000 THEN '80K'
                    WHEN r.lastServiceMeterReading <= 90000 THEN '90K'
                    WHEN r.lastServiceMeterReading <= 100000 THEN '100K'
                    ELSE CONCAT(CEILING(r.lastServiceMeterReading/10000) * 10, 'K')
                END = pml.mileage
                AND pml.modelid = pm.id
            )
            LEFT JOIN vehicle v ON r.vehicleRegNo = v.vehicleRegNo
            LEFT JOIN pmslabour pl ON pm.id = pl.modelid 
                                AND pml.id = pl.mileageid 
                                AND pl.fuelType = COALESCE(v.fuelType, 'Petrol')
                                AND pl.cityid = r.location_cityId
            WHERE r.vehicleRegNo = %s
            LIMIT 1
            """
            cursor.execute(query, (registration_number,))
            result = cursor.fetchone()
            logger.info(f"Service estimate query result for {registration_number}: {result}")
            return result
        except Error as e:
            logger.error(f"Error fetching service estimate data: {e}")
            return None
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def store_service_estimate_request(self, phone_number: str, vehicle_reg: str,
                                      estimate_type: str, image_uploaded=False,
                                      image_count: int = 0) -> bool:
        """Store service estimate request"""
        conn = self.get_connection()
        if not conn:
            return False
        
        try:
            # Handle image_uploaded type dynamically
            if isinstance(image_uploaded, bool):
                img_val = '1' if image_uploaded else '0'
            elif isinstance(image_uploaded, (list, dict)):
                img_val = json.dumps(image_uploaded)
            else:
                img_val = str(image_uploaded)

            cursor = conn.cursor()
            query = """
            INSERT INTO service_estimate_requests
            (phone_number, vehicle_reg, estimate_type, image_uploaded, 
             image_count, request_timestamp, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                phone_number,
                vehicle_reg,
                estimate_type,
                img_val,
                image_count,
                datetime.now(),
                'pending'
            ))
            conn.commit()
            logger.info(f"Service estimate request stored for {phone_number}")
            return True
        except Error as e:
            logger.error(f"Error storing service estimate request: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def store_uploaded_images(self, phone_number: str, vehicle_reg: str,
                             service_type: str, image_files: List[Dict]) -> bool:
        """Store uploaded image information"""
        conn = self.get_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            request_id = str(uuid.uuid4())
            
            # Insert main request
            main_query = """
            INSERT INTO image_upload_requests
            (request_id, phone_number, vehicle_reg, service_type, 
             image_count, upload_timestamp, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(main_query, (
                request_id, phone_number, vehicle_reg, service_type,
                len(image_files), datetime.now(), 'uploaded'
            ))
            
            # Insert individual images
            for image in image_files:
                image_query = """
                INSERT INTO uploaded_images
                (request_id, original_filename, saved_filename, 
                 file_path, relative_path, upload_timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
                """
                cursor.execute(image_query, (
                    request_id,
                    image['original_name'],
                    image['saved_name'],
                    image['file_path'],
                    image['relative_path'],
                    datetime.now()
                ))
            
            conn.commit()
            logger.info(f"Stored {len(image_files)} images for request {request_id}")
            return True
            
        except Error as e:
            logger.error(f"Error storing uploaded images: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    # Agent dashboard tables
    def create_tables(self):
        """Create all necessary database tables"""
        conn = self.get_connection()
        if not conn:
            logger.error("Cannot create tables - no connection")
            return False
        
        try:
            cursor = conn.cursor()
            
            # Conversation log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20),
                    user_message TEXT,
                    bot_response TEXT,
                    sender_type ENUM('user','bot','agent') DEFAULT 'bot',
                    vehicle_reg VARCHAR(20),
                    flow_type   VARCHAR(20) COMMENT 'sales|insurance|used_cars|service',
                    timestamp   DATETIME,
                    INDEX idx_phone     (phone_number),
                    INDEX idx_timestamp (timestamp),
                    INDEX idx_flow      (flow_type)
                )
            """)
            
            # Migration: Ensure flow_type exists in conversation_log
            try:
                cursor.execute("ALTER TABLE conversation_log ADD COLUMN flow_type VARCHAR(20) AFTER vehicle_reg")
                cursor.execute("CREATE INDEX idx_flow ON conversation_log(flow_type)")
                conn.commit()
                logger.info("✅ Added flow_type column to conversation_log")
            except:
                # Column likely already exists
                pass
            
            # Appointment bookings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS appointment_bookings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20),
                    full_name VARCHAR(100),
                    customer_phone VARCHAR(20),
                    vehicle_reg VARCHAR(20),
                    appointment_date VARCHAR(50),
                    timing VARCHAR(50),
                    pickup_address TEXT,
                    drop_address TEXT,
                    service_preference VARCHAR(50),
                    special_instructions TEXT,
                    booking_timestamp DATETIME,
                    status VARCHAR(20) DEFAULT 'pending',
                    INDEX idx_phone (phone_number),
                    INDEX idx_booking_date (booking_timestamp)
                )
            """)
            
            # Service requests table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS service_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20),
                    request_type VARCHAR(20),
                    scheduled_date VARCHAR(50),
                    scheduled_time VARCHAR(50),
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at DATETIME,
                    completed_at DATETIME NULL,
                    notes TEXT,
                    INDEX idx_phone (phone_number),
                    INDEX idx_request_type (request_type),
                    INDEX idx_status (status)
                )
            """)
            
            # Service estimate requests table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS service_estimate_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20),
                    vehicle_reg VARCHAR(20),
                    estimate_type VARCHAR(100),
                    image_uploaded LONGTEXT,
                    image_count INT DEFAULT 0,
                    request_timestamp DATETIME,
                    status VARCHAR(20) DEFAULT 'pending',
                    advisor_response TEXT NULL,
                    estimated_cost DECIMAL(10,2) NULL,
                    response_timestamp DATETIME NULL,
                    INDEX idx_phone (phone_number),
                    INDEX idx_status (status),
                    INDEX idx_request_timestamp (request_timestamp)
                )
            """)
            
            # Migration: Ensure image_uploaded is LONGTEXT
            try:
                cursor.execute("ALTER TABLE service_estimate_requests MODIFY COLUMN image_uploaded LONGTEXT")
                conn.commit()
                logger.info("✅ Altered image_uploaded column to LONGTEXT in service_estimate_requests")
            except Exception as e:
                pass
            
            # Image upload requests table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS image_upload_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    request_id VARCHAR(36) UNIQUE,
                    phone_number VARCHAR(20),
                    vehicle_reg VARCHAR(20),
                    service_type VARCHAR(100),
                    image_count INT,
                    upload_timestamp DATETIME,
                    status VARCHAR(20) DEFAULT 'pending',
                    INDEX idx_phone (phone_number),
                    INDEX idx_request_id (request_id),
                    INDEX idx_vehicle (vehicle_reg)
                )
            """)
            
            # Uploaded images table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS uploaded_images (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    request_id VARCHAR(36),
                    original_filename VARCHAR(255),
                    saved_filename VARCHAR(255),
                    file_path TEXT,
                    relative_path TEXT,
                    upload_timestamp DATETIME,                   
                    INDEX idx_request_id (request_id)
                )
            """)
            
            # New vehicle service requests table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS new_vehicle_service_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20),
                    vehicle_reg VARCHAR(20),
                    city VARCHAR(100),
                    vehicle_model VARCHAR(100),
                    odo_reading VARCHAR(20),
                    fuel_type VARCHAR(50),
                    last_service VARCHAR(100),
                    service_type VARCHAR(100),
                    request_timestamp DATETIME,
                    status VARCHAR(20) DEFAULT 'pending',
                    INDEX idx_phone (phone_number),
                    INDEX idx_vehicle (vehicle_reg)
                )
            """)
            
            # Agents table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    agent_name VARCHAR(100) NOT NULL,
                    email VARCHAR(100) UNIQUE NOT NULL,
                    password VARCHAR(255) NOT NULL,
                    status ENUM('online', 'offline', 'busy') DEFAULT 'offline',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_active DATETIME DEFAULT CURRENT_TIMESTAMP,
                    total_chats INT DEFAULT 0
                )
            """)
            
            # Chat handoffs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_handoffs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(20) NOT NULL,
                    vehicle_reg VARCHAR(20),
                    agent_id INT,
                    status ENUM('requested', 'pending', 'connected', 'completed') DEFAULT 'requested',
                    request_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    connected_timestamp DATETIME NULL,
                    completed_timestamp DATETIME NULL,
                    ai_conversation_history LONGTEXT,
                    handoff_reason VARCHAR(500),
                    FOREIGN KEY (agent_id) REFERENCES agents(id),
                    INDEX idx_phone (phone_number),
                    INDEX idx_status (status)
                )
            """)
            
            # Live chat messages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS live_chat_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    handoff_id INT NOT NULL,
                    sender_type ENUM('customer', 'agent', 'system') NOT NULL,
                    sender_name VARCHAR(100),
                    message_type ENUM('text', 'image', 'document') DEFAULT 'text',
                    message_text LONGTEXT,
                    media_url VARCHAR(500),
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (handoff_id) REFERENCES chat_handoffs(id),
                    INDEX idx_handoff (handoff_id),
                    INDEX idx_timestamp (timestamp)
                )
            """)
            
            # Agent notifications table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_notifications (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    agent_id INT NOT NULL,
                    handoff_id INT NOT NULL,
                    message VARCHAR(500) NOT NULL,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id),
                    FOREIGN KEY (handoff_id) REFERENCES chat_handoffs(id)
                )
            """)
            
            conn.commit()
            logger.info("✅ All database tables created/verified")
            return True
            
        except Error as e:
            logger.error(f"Error creating tables: {e}")
            return False
    def is_handoff_active(self, phone_number: str) -> bool:
        """Check if user has an active handoff session"""
        conn = self.get_connection()
        if not conn:
            return False
            
        try:
            cursor = conn.cursor()
            # Active states: requested (by user), pending (waiting for agent), connected (active)
            query = "SELECT id FROM chat_handoffs WHERE phone_number = %s AND status IN ('requested', 'pending', 'connected') LIMIT 1"
            cursor.execute(query, (phone_number,))
            return cursor.fetchone() is not None
        except Error as e:
            logger.error(f"Error checking handoff status: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def get_active_handoff_info(self, phone_number: str) -> Optional[Dict]:
        """Get the ID and status of an active handoff session"""
        conn = self.get_connection()
        if not conn:
            return None
            
        try:
            cursor = conn.cursor(dictionary=True)
            # Only consider handoffs requested within the last 12 hours
            query = """
                SELECT id, status 
                FROM chat_handoffs 
                WHERE phone_number = %s 
                  AND status IN ('requested', 'pending', 'connected')
                  AND request_timestamp >= NOW() - INTERVAL 12 HOUR
                LIMIT 1
            """
            cursor.execute(query, (phone_number,))
            return cursor.fetchone()
        except Error as e:
            logger.error(f"Error getting handoff info: {e}")
            return None
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def initiate_handoff(self, phone_number: str, vehicle_reg: Optional[str] = None, reason: Optional[str] = None) -> bool:
        """Create a new handoff request in the database"""
        conn = self.get_connection()
        if not conn:
            return False
            
        try:
            cursor = conn.cursor()
            # First, check if there's already an active one to avoid duplicates
            if self.is_handoff_active(phone_number):
                return True
                
            query = """
                INSERT INTO chat_handoffs (phone_number, vehicle_reg, handoff_reason, status, request_timestamp)
                VALUES (%s, %s, %s, 'requested', %s)
            """
            cursor.execute(query, (phone_number, vehicle_reg, reason, datetime.now()))
            conn.commit()
            logger.info(f"✅ Handoff requested for {phone_number}")
            return True
        except Error as e:
            logger.error(f"Error initiating handoff: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def store_live_message(self, handoff_id: int, sender_type: str, message_text: str, message_type: str = 'text', media_url: str = None) -> bool:
        """Store a message in the live chat log during an active session"""
        conn = self.get_connection()
        if not conn:
            return False
            
        try:
            cursor = conn.cursor()
            query = """
                INSERT INTO live_chat_messages (handoff_id, sender_type, message_text, message_type, media_url, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (handoff_id, sender_type, message_text, message_type, media_url, datetime.now()))
            conn.commit()
            return True
        except Error as e:
            logger.error(f"Error storing live message: {e}")
            return False
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

# Initialize database
db = DatabaseManager()
db.create_tables()

# ============ WHATSAPP BOT CLASS ============

class WhatsAppBot:
    """Main WhatsApp bot class handling all messaging and flows"""
    
    def __init__(self):
        self.db = DatabaseManager()
        self.user_sessions: Dict[str, Dict] = {}

    def trigger_human_agent_confirmation(self, phone_number: str):
        """Send the confirmation message asking if they want to connect to a human agent"""
        message = (
            "🤖 I understand you'd like to speak with a human representative.\n\n"
            "👷 *Would you like me to connect you with our live agent?*\n\n"
            "He'll be able to provide personalized assistance with your query."
        )
        buttons = [
            {"type": "reply", "reply": {"id": "AGENT_YES", "title": "✅ Yes, Connect Me"}},
            {"type": "reply", "reply": {"id": "AGENT_NO",  "title": "❌ No, Continue AI"}}
        ]

        # Save current state before switching to confirmation
        from config import USER_STATE
        _current_state = USER_STATE.get(phone_number, {}).get("state")
        if _current_state and not _current_state.endswith("_AGENT_CONFIRMATION"):
             USER_STATE.setdefault(phone_number, {})["_saved_state"] = _current_state

        # Also save in internal session
        if phone_number in self.user_sessions:
            _session_state = self.user_sessions[phone_number].get("state")
            if _session_state and _session_state != 'SERVICE_AGENT_CONFIRMATION':
                self.user_sessions[phone_number]["_saved_state"] = _session_state

        # send_button_message is defined in webhook.py
        from webhook import send_button_message
        send_button_message(phone_number, message, buttons)
        
        # Update session state to track that we are waiting for this confirmation
        if phone_number not in self.user_sessions:
            self.user_sessions[phone_number] = {}
        self.user_sessions[phone_number]['state'] = 'SERVICE_AGENT_CONFIRMATION'
        logger.info(f"Sent agent confirmation to {phone_number}")

    def initiate_agent_handoff(self, phone_number: str, vehicle_reg: str = None,
                               handoff_reason: str = "User requested via bot menu"):
        """Perform the actual handoff in DB and notify user.

        Works for ALL flows (insurance, sales, used cars, refinancing, service).
        vehicle_reg can be supplied explicitly or is auto-read from sessions.
        handoff_reason is passed through to the agent dashboard.
        """
        from config import USER_STATE

        # ── Resolve vehicle registration ────────────────────────────────
        # Prefer explicit arg; fall back through every known key in both stores
        reg = (
            vehicle_reg
            or self.user_sessions.get(phone_number, {}).get('registration')
            or USER_STATE.get(phone_number, {}).get('vehicle_reg')
            or USER_STATE.get(phone_number, {}).get('reg_number')
            or USER_STATE.get(phone_number, {}).get('data', {}).get('reg_no')
        )

        # ── Build conversation context for agent dashboard ───────────────
        flow_state = USER_STATE.get(phone_number, {}).get('state', '')
        if flow_state.startswith('INSURANCE') or flow_state.startswith('STATE_4') or flow_state.startswith('STATE_5'):
            flow_label = 'Insurance'
        elif flow_state.startswith('SALES'):
            flow_label = 'Sales'
        elif flow_state.startswith('USED'):
            flow_label = 'Used Cars'
        elif flow_state.startswith('REFINANCING'):
            flow_label = 'Refinancing'
        else:
            flow_label = 'Service'

        success = self.db.initiate_handoff(
            phone_number,
            vehicle_reg=reg,
            reason=f"{handoff_reason} [{flow_label} flow]"
        )

        from webhook import send_whatsapp_message
        if success:
            send_whatsapp_message(
                phone_number,
                "✅ *You're now being connected to a live agent!*\n\n"
                "⏳ Please wait a moment — our team will respond to you shortly right here on WhatsApp.\n\n"
                "You can continue typing and your messages will be seen by the agent. 💬"
            )
            # ── Update BOTH session stores so the handoff is reflected everywhere ──
            # webhook.py live-agent interception reads DB (get_active_handoff_info)
            # so no extra USER_STATE key is required, but we clear the confirmation
            # state so stale *_AGENT_CONFIRMATION states don't linger.
            if phone_number in USER_STATE:
                USER_STATE[phone_number]['state'] = 'AWAITING_AGENT'
            if phone_number in self.user_sessions:
                self.user_sessions[phone_number]['state'] = 'AGENT_HANDOFF'
            else:
                self.user_sessions[phone_number] = {'state': 'AGENT_HANDOFF'}
        else:
            send_whatsapp_message(
                phone_number,
                "⚠️ Sorry, I couldn't connect you right now.\n\n"
                "Please try again in a moment, or call us directly:\n"
                "📞 +91-9876543210"
            )

    def get_this_week_dates(self) -> List[Dict]:
        """Generate dates for 'later this week' (excluding today and tomorrow)"""
        today = datetime.now()
        dates = []
        
        start_date = today + timedelta(days=2)
        current_weekday = today.weekday()
        
        days_until_sunday = 0 if current_weekday == 6 else (6 - current_weekday)
        
        if days_until_sunday <= 1:
            return []
        
        end_date = today + timedelta(days=days_until_sunday)
        
        current = start_date
        while current <= end_date:
            day_name = current.strftime('%A')
            date_str = current.strftime('%d %b')
            dates.append({
                'id': f"date_{current.strftime('%Y%m%d')}",
                'title': f"{day_name} ({date_str})",
                'description': f"Book for {day_name}"
            })
            current += timedelta(days=1)
        
        return dates

    def get_next_week_dates(self) -> List[Dict]:
        """Generate dates for 'next week' (all 7 days)"""
        today = datetime.now()
        dates = []
        
        current_weekday = today.weekday()
        days_until_next_monday = (7 - current_weekday) if current_weekday != 6 else 1
        
        next_monday = today + timedelta(days=days_until_next_monday)
        
        for i in range(7):
            current = next_monday + timedelta(days=i)
            day_name = current.strftime('%A')
            date_str = current.strftime('%d %b')
            dates.append({
                'id': f"date_{current.strftime('%Y%m%d')}",
                'title': f"{day_name} ({date_str})",
                'description': f"Book for {day_name}"
            })
        
        return dates

    def send_message(self, phone_number: str, message: str, 
                    buttons: Optional[List] = None, 
                    list_message: Optional[List] = None,
                    include_end_button: bool = False) -> bool:
        """Send WhatsApp message (text, buttons, or list) via webhook centralized functions"""
        
        # Import webhook send functions to use centralized URL & token management
        from webhook import send_whatsapp_message, send_button_message, send_list_message
        
        try:
            if list_message:
                # Send WhatsApp List Message via webhook
                logger.info(f"Sending list message to {phone_number}")
                try:
                    send_list_message(phone_number, message, "Choose", list_message)
                except Exception as e:
                    logger.error(f"Error sending list message: {e}")
                    # Fallback for date selection
                    if list_message and list_message[0].get('rows'):
                        fallback = message + "\n\n📅 Available dates:\n"
                        for i, row in enumerate(list_message[0]['rows'], 1):
                            fallback += f"{i}. {row['title']}\n"
                        fallback += "\n💬 Reply with the number of your choice"
                        self.user_sessions[phone_number]['fallback_options'] = list_message[0]['rows']
                        return self.send_message(phone_number, fallback)
                    else:
                        fallback = message + "\n\nPlease reply with options"
                        return self.send_message(phone_number, fallback)
                
                if include_end_button:
                    return self.send_end_button(phone_number)
                return True

            elif buttons:
                # Send WhatsApp Button Message via webhook
                logger.info(f"Sending button message to {phone_number}")
                send_button_message(phone_number, message, buttons)
                return True

            else:
                # Send Plain Text Message via webhook
                logger.info(f"Sending text message to {phone_number}")
                send_whatsapp_message(phone_number, message)
                return True

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    def send_media(self, phone_number: str, media_type: str, media_url: str, caption: str = "", filename: str = "") -> bool:
        """Send WhatsApp media (image or document)"""
        from webhook import send_whatsapp_image, send_whatsapp_document
        
        try:
            if media_type == "image":
                send_whatsapp_image(phone_number, media_url, caption)
            elif media_type == "document":
                send_whatsapp_document(phone_number, media_url, filename, caption)
            else:
                logger.error(f"Unsupported media type: {media_type}")
                return False
            return True
        except Exception as e:
            logger.error(f"Error in send_media: {e}")
            return False

    def _normalize_digits(self, s: str) -> str:
        """Keep only digits"""
        return ''.join(ch for ch in s if ch.isdigit())

    def _is_valid_10_digit_mobile(self, s: str) -> bool:
        """Check if string has exactly 10 digits"""
        digits = self._normalize_digits(s)
        return len(digits) == 10

    def send_end_button(self, phone_number: str) -> bool:
        """Send End Chat button"""
        message = "Awaiting your next action."
        buttons = [{"type": "reply", "reply": {"id": "end_conversation", "title": "🔚 End Chat"}}]
        return self.send_message(phone_number, message, buttons=buttons)

    def send_back_end_buttons(self, phone_number: str) -> bool:
        """Send Back and End buttons"""
        message = "What would you like to do next?"
        buttons = [
            {"type": "reply", "reply": {"id": "back_to_main", "title": "🔙 Back to Main Menu"}},
            {"type": "reply", "reply": {"id": "end_conversation", "title": "🔚 End Chat"}}
        ]
        return self.send_message(phone_number, message, buttons=buttons)

    # ============ VEHICLE REGISTRATION DETECTION ============
    
    def normalize_reg(self, s: str) -> str:
        """Enhanced normalization for all Indian registration formats"""
        if not s:
            return ""
        # Remove all non-alphanumeric characters and convert to uppercase
        normalized = re.sub(r'[^A-Z0-9]', '', s.upper())
        return normalized

    def validate_reg_candidate(self, s: str) -> bool:
        """
        Comprehensive validation for ALL Indian vehicle registration formats:
        - Standard: KA01AB1234, MH01AB1234
        - Diplomatic: 77CD1, 11CC432  
        - Military: 25B123456P
        - Temporary: TS07TR2025123
        - New BH Series: 24BH1234AA
        - Commercial: DL1T2468
        - Electric: DL3CAF1234
        """
        if not s:
            return False
        
        # Length validation - be more flexible (4-15 characters)
        if len(s) < 4 or len(s) > 15:
            return False
        
        # Must contain both letters and digits
        if not re.search(r'[A-Z]', s) or not re.search(r'\d', s):
            return False
        
        # Reject plain phone numbers (exactly 10 digits)
        if re.fullmatch(r'\d{10}', s):
            return False
        
        # Enhanced pattern matching for ALL Indian formats
        
        # 1. Standard format: KA01AB1234, MH04DA2121, UP32XY0786
        if re.match(r'^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,6}$', s):
            return True
        
        # 2. Diplomatic format: 77CD1, 11CC432
        if re.match(r'^\d{1,3}[A-Z]{2}\d{1,6}$', s):
            return True
        
        # 3. Military format: 25B123456P (year + base + serial + P)
        if re.match(r'^\d{2}[A-Z]\d{1,6}[A-Z]$', s):
            return True
        
        # 4. New BH series: 24BH1234AA, 25BH1234AB
        if re.match(r'^\d{2}BH\d{1,6}[A-Z]{1,2}$', s):
            return True
        
        # 5. Temporary registration: TS07TR2025123
        if re.match(r'^[A-Z]{2}\d{1,2}[A-Z]{2}\d{4,6}$', s):
            return True
        
        # 6. Commercial variants: DL1T2468, DL1CS5678
        if re.match(r'^[A-Z]{2}\d{1,2}[A-Z]{1,2}\d{1,6}$', s):
            return True
        
        # 7. Electric vehicle: DL3CAF1234
        if re.match(r'^[A-Z]{2}\d{1,2}[A-Z]{2,3}\d{1,6}$', s):
            return True
        
        # 8. Flexible fallback for any reasonable combination
        # At least 2 letters, 2 digits, reasonable length
        letter_count = sum(1 for c in s if c.isalpha())
        digit_count = sum(1 for c in s if c.isdigit())
        
        if letter_count >= 2 and digit_count >= 2 and 4 <= len(s) <= 12:
            return True
        
        return False

    def extract_registration_from_text(self, text: str) -> str | None:
        """
        Enhanced extraction for ALL Indian vehicle registration formats from free text.
        Handles: KA01AB1234, ka-05-mj-4321, "my reg is KA 05 MJ 4321", etc.
        """
        if not text:
            return None
        
        # Convert to uppercase for processing
        text_up = text.upper()
        
        # Enhanced logging
        logger.info(f"🔍 EXTRACTING from: '{text}'")
        
        # STRATEGY 1: Direct word-by-word search
        words = text_up.split()
        for word in words:
            cleaned_word = self.normalize_reg(word)
            if self.validate_reg_candidate(cleaned_word):
                logger.info(f"✅ Found via word search: '{cleaned_word}' from word '{word}'")
                return cleaned_word
        
        # STRATEGY 2: Handle common separators and phrases
        # Replace common registration indicators and separators
        text_clean = text_up
        
        # Remove common phrases that indicate registration
        reg_indicators = [
            r'MY\s+REG\s+NUMBER\s+IS\s*',
            r'MY\s+REG\s+NO\s+IS\s*',
            r'MY\s+REGISTRATION\s+IS\s*',
            r'MY\s+CAR\s+IS\s*',
            r'MY\s+VEHICLE\s+IS\s*',
            r'REG\s+NUMBER\s*:\s*',
            r'REG\s+NO\s*:\s*',
            r'REGISTRATION\s*:\s*',
            r'CAR\s+NUMBER\s*:\s*',
            r'VEHICLE\s+NUMBER\s*:\s*'
        ]
        
        for pattern in reg_indicators:
            text_clean = re.sub(pattern, ' ', text_clean)
        
        # Normalize separators
        text_clean = re.sub(r'[\-./,;:]+', ' ', text_clean)
        
        # STRATEGY 3: Enhanced pattern matching with multiple regex patterns
        patterns = [
            # Standard formats with flexible spacing
            r'\b[A-Z]{2}\s*\d{1,2}\s*[A-Z]{1,3}\s*\d{1,6}\b',
            
            # Diplomatic/Consular format
            r'\b\d{1,3}\s*[A-Z]{2}\s*\d{1,6}\s*[A-Z]{0,2}\b',
            
            # Military format: 25 B 123456 P
            r'\b\d{2}\s*[A-Z]\s*\d{1,6}\s*[A-Z]\b',
            
            # New BH series: 24 BH 1234 AA
            r'\b\d{2}\s*BH\s*\d{1,6}\s*[A-Z]{1,2}\b',
            
            # Temporary format: TS 07 TR 2025/123
            r'\b[A-Z]{2}\s*\d{1,2}\s*[A-Z]{2}\s*\d{4}/?\d{0,4}\b',
            
            # Commercial taxi format: DL 1T 2468
            r'\b[A-Z]{2}\s*\d{1,2}[A-Z]\s*\d{1,6}\b',
            
            # Electric/Special format: DL 3C AF 1234
            r'\b[A-Z]{2}\s*\d{1,2}[A-Z]\s*[A-Z]{1,2}\s*\d{1,6}\b',
            
            # Very flexible pattern for edge cases
            r'\b[A-Z]+\s*\d+\s*[A-Z]*\s*\d+\s*[A-Z]*\b'
        ]
        
        for i, pattern in enumerate(patterns):
            matches = list(re.finditer(pattern, text_clean))
            logger.info(f"🔍 Pattern {i+1}: '{pattern}' found {len(matches)} matches")
            
            for match in matches:
                candidate = self.normalize_reg(match.group(0))
                logger.info(f"🧪 Testing candidate: '{candidate}' from match: '{match.group(0)}'")
                
                if self.validate_reg_candidate(candidate):
                    logger.info(f"✅ FOUND valid registration: '{candidate}'")
                    return candidate
        
        # STRATEGY 4: Sliding window approach for spaced registrations
        words = text_clean.split()
        for window_size in range(2, 8):  # Try 2-7 word combinations
            for i in range(len(words) - window_size + 1):
                window_text = ' '.join(words[i:i + window_size])
                candidate = self.normalize_reg(window_text)
                
                if self.validate_reg_candidate(candidate):
                    logger.info(f"✅ Found via sliding window: '{candidate}' from: '{window_text}'")
                    return candidate
        
        # STRATEGY 5: Sequential character extraction
        # Look for sequences that might be registrations
        alphanumeric_sequences = re.findall(r'[A-Z0-9\s\-./]{4,20}', text_up)
        for sequence in alphanumeric_sequences:
            candidate = self.normalize_reg(sequence)
            if self.validate_reg_candidate(candidate):
                logger.info(f"✅ Found via sequence: '{candidate}' from: '{sequence}'")
                return candidate
        
        logger.info(f"❌ No registration found in: '{text}'")
        return None


    def is_vehicle_registration(self, query):
        """
        Enhanced detection that identifies if input contains vehicle registration
        """
        if not query or len(query.strip()) == 0:
            return False
        
        logger.info(f"🔍 CHECKING if vehicle registration: '{query}'")
        
        # Clean the query
        q_clean = self.normalize_reg(query)
        
        # STRATEGY 1: Direct validation of cleaned input
        if self.validate_reg_candidate(q_clean):
            logger.info(f"✅ Direct match: '{q_clean}'")
            return True
        
        # STRATEGY 2: Try extraction from the text
        extracted = self.extract_registration_from_text(query)
        if extracted:
            logger.info(f"✅ Extraction successful: '{extracted}'")
            return True
        
        # STRATEGY 3: Check if it's a short, simple registration-like input
        words = query.strip().split()
        if len(words) <= 4:  # Short input, might be spaced registration
            combined = ''.join(words).upper()
            combined_clean = self.normalize_reg(combined)
            if self.validate_reg_candidate(combined_clean):
                logger.info(f"✅ Short input match: '{combined_clean}'")
                return True
        
        logger.info(f"❌ Not vehicle registration: '{query}'")
        return False

        # ============ MAIN MESSAGE PROCESSING ============

    def process_message(self, phone_number: str, message: str):
        """Main message processing entry point"""
        
        original_message = message
        message = message.strip().lower()
        
        # Initialize session if new
        if phone_number not in self.user_sessions:
            self.user_sessions[phone_number] = {
                'state': 'initial',
                'vehicle_reg': None,
                'vehicle_data': None
            }
        
        session = self.user_sessions[phone_number]
        
        logger.info(f"=" * 50)
        logger.info(f"📱 Processing message from {phone_number}")
        logger.info(f"Original: '{original_message}'")
        logger.info(f"State: {session['state']}")
        logger.info(f"=" * 50)

        # ===== AUTO-EXTRACT REGISTRATION =====
        # Guard: states where free-text/date/name input is expected.
        # Prevents date strings like "Tuesday (14 Apr)", addresses, names, etc.
        # from being falsely detected as vehicle registration numbers.
        _BOOKING_AND_INPUT_STATES = {
            'booking_priority', 'booking_slots',
            'booking_this_week_dates', 'booking_next_week_dates',
            'waiting_full_name', 'waiting_phone_number',
            'waiting_pickup_address', 'waiting_drop_address_selection',
            'waiting_drop_address_input', 'waiting_special_instructions',
            'booking_preferences',
            'vehicle_found', 'vehicle_not_found',
            'SERVICE_AGENT_CONFIRMATION', 'waiting_for_agent',
            'waiting_agent_connection_response',
            'contact_method_selection', 'callback_priority', 'callback_slots',
            'video_call_priority', 'video_call_slots',
            'discounts_book_service', 'discounts_followup_request',
            'discounts_followup_timing',
            'estimate_same_service_confirmation', 'estimate_2hr_confirmation',
            'estimate_contact_advisor_prompt', 'estimate_service_type_selection',
            'estimate_image_upload_confirmation', 'estimate_image_upload_waiting',
            'estimate_image_confirm_ready', 'estimate_no_vehicle',
            'waiting_city_input', 'waiting_vehicle_model_input',
            'waiting_odo_reading_input', 'waiting_fuel_type_input',
            'waiting_last_service_selection', 'waiting_other_service_selection',
            'vehicle_info_confirmation', 'waiting_service_type_selection',
        }

        current_state = session.get('state', '')
        if current_state not in _BOOKING_AND_INPUT_STATES:
            extracted_reg = self.extract_registration_from_text(original_message)
            if extracted_reg:
                if (not session.get('vehicle_reg')
                        or extracted_reg != session.get('vehicle_reg')):
                    logger.info(f"🎯 Auto-extracted registration: '{extracted_reg}'")
                    self.user_sessions[phone_number]['vehicle_reg'] = extracted_reg
                    return self.handle_registration_input(phone_number, extracted_reg)

        # ===== GREETING HANDLER =====
        if message in ['hi', 'hello', 'hey', 'start']:
            self.user_sessions[phone_number] = {
                'state': 'waiting_for_registration',
                'vehicle_reg': None,
                'vehicle_data': None
            }
            return self.handle_greeting(phone_number)

        # ===== END CONVERSATION =====
        if message == 'end_conversation':
            return self.handle_end_conversation(phone_number)

        # ===== HUMAN AGENT CONFIRMATION STATE =====
        if session.get('state') == 'SERVICE_AGENT_CONFIRMATION':
            # Button IDs are uppercase (AGENT_YES/AGENT_NO); also accept lowercase
            if message in ('AGENT_YES', 'agent_yes'):
                return self.initiate_agent_handoff(phone_number)
            elif message in ('AGENT_NO', 'agent_no'):
                # Restore the saved state and re-prompt the customer
                from config import USER_STATE
                prev_state = self.user_sessions[phone_number].pop("_saved_state", None) or USER_STATE.get(phone_number, {}).pop("_saved_state", None)
                
                if prev_state and prev_state != 'SERVICE_AGENT_CONFIRMATION':
                    self.user_sessions[phone_number]['state'] = prev_state
                    USER_STATE.setdefault(phone_number, {})["state"] = prev_state
                    self.send_message(phone_number, "✅ No problem! Let's continue where we left off.")
                    from webhook import _reprompt_flow_state
                    _reprompt_flow_state(phone_number, prev_state)
                    return "CONTINUE_FLOW"
                else:
                    self.user_sessions[phone_number]['state'] = 'READY'
                    reg = session.get('vehicle_reg') or session.get('registration')
                    if reg:
                        return self.handle_registration_input(phone_number, reg)
                    else:
                        return self.handle_greeting(phone_number)
            else:
                # Re-send confirmation buttons so user can still choose
                self.trigger_human_agent_confirmation(phone_number)
                return

        # ===== REGISTRATION STATE =====
        if session['state'] == 'waiting_for_registration':
            from utils import is_genuine_query
            if self.is_vehicle_registration(original_message):
                return self.handle_registration_input(phone_number, original_message)
            elif is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            else:
                msg = ("Please enter your Vehicle Registration Number to continue.\n\n"
                       "Examples: KA01AB1234, MH 01 AB 1234, DL 1T 2468")
                self.send_message(phone_number, msg)
                return msg

        # ===== VEHICLE NOT FOUND WORKFLOW =====
        if session['state'] == 'waiting_service_type_selection':
            return self.handle_service_type_selection(phone_number, message)
        
        if session['state'] == 'waiting_city_input':
            return self.handle_city_input(phone_number, message)
        
        if session['state'] == 'waiting_vehicle_model_input':
            return self.handle_vehicle_model_input(phone_number, message)
        
        if session['state'] == 'waiting_odo_reading_input':
            return self.handle_odo_reading_input(phone_number, message)
        
        if session['state'] == 'waiting_fuel_type_input':
            return self.handle_fuel_type_input(phone_number, message)
        
        if session['state'] == 'waiting_last_service_selection':
            return self.handle_last_service_selection(phone_number, message)
        
        if session['state'] == 'waiting_other_service_selection':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_other_service_selection(phone_number, message)
        
        if session['state'] == 'vehicle_info_confirmation':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_vehicle_info_confirmation(phone_number, message)

        # ===== BUTTON HANDLERS =====
        if message == 'book_appointment':
            return self.book_service_appointment(phone_number)

        if message == 'contact_advisor':
            return self.trigger_human_agent_confirmation(phone_number)

        # Button from the estimate no-pricing prompt -> open contact advisor menu
        if message == 'estimate_contact_advisor':
            return self.contact_service_advisor(phone_number)

        if message == 'service_estimate':
            if not session.get('vehicle_reg'):
                self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
                msg = "Please enter your Vehicle Registration Number"
                self.send_message(phone_number, msg)
                return msg
            else:
                vehicle_data = self.db.get_vehicle_details(session['vehicle_reg'])
                if vehicle_data:
                    session['vehicle_data'] = vehicle_data
                    session['state'] = 'vehicle_found'
                    return self.get_service_estimate(phone_number)
                else:
                    return self.handle_vehicle_not_found_estimate(phone_number)
        
        if message == 'back_to_main':
            return self.handle_back_to_main(phone_number)
        
        if message == 'try_different_reg':
            return self.handle_try_different_registration(phone_number)

        # ===== VEHICLE FOUND STATE =====
        if session['state'] == 'vehicle_found':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_service_options(phone_number, message)

        # ===== BOOKING STATES =====
        if session['state'] == 'booking_priority':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_booking_priority(phone_number, message)
        
        if session['state'] == 'booking_slots':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_booking_slots(phone_number, message)
        
        if session['state'] == 'waiting_full_name':
            # Note: Generally we avoid AI for names, but let's allow it if it's clearly a question
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_full_name_input(phone_number, message)
        
        if session['state'] == 'waiting_phone_number':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_phone_input(phone_number, message)
        
        if session['state'] == 'waiting_pickup_address':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_pickup_address_input(phone_number, message)
        
        if session['state'] == 'waiting_drop_address_selection':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_drop_address_selection(phone_number, message)
        
        if session['state'] == 'waiting_drop_address_input':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_drop_address_input(phone_number, message)
        
        if session['state'] == 'booking_preferences':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_booking_preferences(phone_number, message)
        
        if session['state'] == 'waiting_special_instructions':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_special_instructions(phone_number, message)

        # ===== DATE SELECTION STATES =====
        if session['state'] == 'booking_this_week_dates':
            if self.handle_numeric_date_selection(phone_number, message):
                return "Date selected"
            return self.handle_this_week_date_selection(phone_number, message)

        if session['state'] == 'booking_next_week_dates':
            if self.handle_numeric_date_selection(phone_number, message):
                return "Date selected"
            return self.handle_next_week_date_selection(phone_number, message)

        # ===== CONTACT ADVISOR STATES =====
        if session['state'] == 'contact_method_selection':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_contact_method(phone_number, message)
        
        if session['state'] == 'callback_priority':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_callback_priority(phone_number, message)
        
        if session['state'] == 'callback_slots':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_callback_slots(phone_number, message)
        
        if session['state'] == 'video_call_priority':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_video_call_priority(phone_number, message)
        
        if session['state'] == 'video_call_slots':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_video_call_slots(phone_number, message)

        # ===== DISCOUNTS STATES =====
        if session['state'] == 'discounts_book_service':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_discounts_book_service(phone_number, message)
        
        if session['state'] == 'discounts_followup_request':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_discounts_followup_request(phone_number, message)
        
        if session['state'] == 'discounts_followup_timing':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_discounts_followup_timing(phone_number, message)

        # ===== SERVICE ESTIMATE STATES =====
        if session['state'] == 'estimate_same_service_confirmation':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_estimate_same_service_confirmation(phone_number, message)
        
        if session['state'] == 'estimate_2hr_confirmation':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_estimate_2hr_confirmation(phone_number, message)

        # New state: no-pricing prompt with Contact Advisor / Back to Main buttons
        if session['state'] == 'estimate_contact_advisor_prompt':
            if message == 'estimate_contact_advisor':
                return self.contact_service_advisor(phone_number)
            elif message == 'back_to_main':
                return self.handle_back_to_main(phone_number)
            else:
                return self.contact_service_advisor(phone_number)

        if session['state'] == 'estimate_service_type_selection':
            from utils import is_genuine_query
            if is_genuine_query(original_message, session['state']):
                return self._service_route_via_ai(phone_number, original_message, session['state'])
            return self.handle_estimate_service_type_selection(phone_number, message)

        if session['state'] == 'estimate_image_upload_confirmation':
            return self.handle_estimate_image_upload_confirmation(phone_number, message)

        if session['state'] == 'estimate_image_upload_waiting':
            if message == 'estimate_image_confirm':
                return self.handle_estimate_image_confirm(phone_number)
            elif message in ['image_uploaded', 'document_uploaded']:
                current_count = session.get('uploaded_image_count', 0) + 1
                self.user_sessions[phone_number]['uploaded_image_count'] = current_count
                self.user_sessions[phone_number]['state'] = 'estimate_image_confirm_ready'
                
                confirm_msg = (f"✅ Images received!\n📸 Total: {current_count}\n"
                              f"Click Confirm to submit")
                buttons = [{"type": "reply", "reply": {"id": "estimate_image_confirm", 
                                                        "title": "✅ Confirm"}}]
                self.send_message(phone_number, confirm_msg, buttons=buttons)
                return confirm_msg
            else:
                reminder = "📸 Please upload images of your vehicle"
                self.send_message(phone_number, reminder)
                return reminder

        if session['state'] == 'estimate_image_confirm_ready':
            if message == 'estimate_image_confirm':
                return self.handle_estimate_image_confirm(phone_number)
            elif message in ['image_uploaded', 'document_uploaded']:
                current_count = session.get('uploaded_image_count', 0) + 1
                self.user_sessions[phone_number]['uploaded_image_count'] = current_count
                
                more_msg = (f"✅ Image #{current_count} received!\n"
                           f"Total: {current_count}\nClick Confirm when ready")
                buttons = [{"type": "reply", "reply": {"id": "estimate_image_confirm", 
                                                        "title": "✅ Confirm"}}]
                self.send_message(phone_number, more_msg, buttons=buttons)
                return more_msg
            else:
                reminder = (f"📸 Your images are ready. Total: {session.get('uploaded_image_count', 0)}\n"
                           f"Click Confirm to submit")
                buttons = [{"type": "reply", "reply": {"id": "estimate_image_confirm", 
                                                        "title": "✅ Confirm"}}]
                self.send_message(phone_number, reminder, buttons=buttons)
                return reminder

        # ===== LIVE AGENT HANDOFF =====
        if session['state'] == 'waiting_agent_connection_response':
            return self.handle_agent_connection_response(phone_number, message)

        if session['state'] == 'waiting_for_agent':
            if self.is_customer_with_live_agent(phone_number):
                return self.route_to_live_agent(phone_number, message)
            else:
                vehicle_reg = session.get('vehicle_reg')
                vehicle_data = session.get('vehicle_data')
                self.user_sessions[phone_number] = {
                    'state': 'vehicle_found' if vehicle_data else 'initial',
                    'vehicle_reg': vehicle_reg,
                    'vehicle_data': vehicle_data
                }
                logger.info(f"✅ Fixed stale agent session for {phone_number}")

        if session['state'] == 'estimate_no_vehicle':
            return self.handle_estimate_service_type_selection(phone_number, message)

        if message == 'book_next_service':
            return self.book_service_appointment(phone_number)

        if session['state'] == 'vehicle_not_found':
            return self.handle_new_customer_options(phone_number, message)

        # ===== SERVICE REQUEST DETECTION =====
        service_keywords = ['book', 'appointment', 'service', 'history', 'estimate', 
                           'pricing', 'cost', 'schedule']
        
        if any(keyword in message for keyword in service_keywords):
            logger.info("🛠️ Service request detected")
            
            if not session.get('vehicle_reg'):
                self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
                msg = "Please enter your Vehicle Registration Number"
                self.send_message(phone_number, msg)
                return msg

            vehicle_data = self.db.get_vehicle_details(session['vehicle_reg'])
            if vehicle_data:
                session['vehicle_data'] = vehicle_data
                session['state'] = 'vehicle_found'
                return self.get_service_estimate(phone_number)
            else:
                return self.handle_vehicle_not_found_estimate(phone_number)

        # ===== AI QUERY =====
        return self.handle_ai_query(phone_number, original_message)

    # ============ GREETING & REGISTRATION HANDLERS ============

    def handle_greeting(self, phone_number: str) -> str:
        """Handle initial greeting"""
        msg = ("Hello! 👋 Welcome to Sherpa Service Center.\n\n"
               "Please enter your Vehicle Registration Number:\n"
               "Example: KA01AB1234")
        self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
        self.send_message(phone_number, msg)
        return msg

    def handle_registration_input(self, phone_number: str, reg_input: str) -> str:
        """Handle vehicle registration input"""
        logger.info(f"🔧 Handling registration: '{reg_input}'")
        
        extracted = self.extract_registration_from_text(reg_input)
        reg_number = extracted or self.normalize_reg(reg_input)
        
        if not self.validate_reg_candidate(reg_number):
            msg = ("❗ Invalid registration format.\n\n"
                   "Supported: KA01AB1234, MH 01 AB 1234, DL 1T 2468\n"
                   "Please try again:")
            self.send_message(phone_number, msg)
            return msg
        
        self.user_sessions[phone_number]['vehicle_reg'] = reg_number
        self.send_message(phone_number, f"🔍 Checking our database for vehicle {reg_number}...")
        
        vehicle_data = self.db.get_vehicle_details(reg_number)
        
        if vehicle_data:
            logger.info(f"✅ Vehicle found: {vehicle_data}")
            self.user_sessions[phone_number]['vehicle_data'] = vehicle_data
            return self.handle_vehicle_found(phone_number, vehicle_data)
        else:
            logger.info(f"❌ Vehicle not found: {reg_number}")
            self.user_sessions[phone_number]['vehicle_data'] = None
            return self.handle_vehicle_not_found(phone_number, reg_number)

    def handle_vehicle_found(self, phone_number, vehicle_data):
        self.user_sessions[phone_number]['state'] = 'vehicle_found'
        self.user_sessions[phone_number]['vehicle_data'] = vehicle_data
        
        last_service_date = vehicle_data.get('lastServiceDate', None)
        if last_service_date:
            last_service_date = last_service_date.strftime('%d-%m-%Y')
        else:
            last_service_date = "Not available"
            
        message = (
            f"Great! ✅ I found your vehicle in our system:\n\n"
            f"🚗 {vehicle_data.get('model', 'N/A')} - {vehicle_data.get('variant', 'N/A')}\n"
            f"📋 Reg. No: {vehicle_data.get('vehicleRegNo', 'N/A')}\n"
            f"⏱️ Last Service: {last_service_date}\n"
            f"📏 Current ODO: {vehicle_data.get('lastServiceMeterReading', 'N/A')} km\n"
            f"📅 Next Service Due: {vehicle_data.get('nextServiceDue', 'N/A')}\n"
            f"🛠️ Next Service Type: {vehicle_data.get('nextServiceType', 'N/A')}\n"
            f"🏪 Workshop: {vehicle_data.get('workshopName', 'N/A')}\n\n"
    
            "How can I help you today?"
        )
        
        # Corrected WhatsApp List Message for Vehicle Found
        list_sections = [{
            "title": "Service Options",
            "rows": [
                {
                    "id": "service_history",
                    "title": "Service History",
                    "description": "View your complete service records"
                },
                {
                    "id": "service_estimate", 
                    "title": "Service Estimate",
                    "description": "Get pricing for upcoming services"
                },
                {
                    "id": "book_appointment",
                    "title": "Book Appointment", 
                    "description": "Schedule your next service visit"
                },
                {
                    "id": "contact_advisor",
                    "title": "Contac"
                    "t Advisor",
                    "description": "Speak with our service experts"
                },
                {
                    "id": "discounts_offer",
                    "title": "Discounts & Offers",
                    "description": "View current promotions and deals"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_vehicle_not_found(self, phone_number, reg_number):
        self.user_sessions[phone_number]['state'] = 'vehicle_not_found'
        
        message = (
            f"Sorry, I couldn't find vehicle registration '{reg_number}' in our service database.\n\n"
            "This could mean:\n"
            "• First-time service with us\n"
            "• Vehicle purchased from another dealer\n"
            "• Registration number may need verification\n\n"
            "No worries! I can still help you with:"
        )
        
        # Corrected WhatsApp List Message for Vehicle Not Found  
        list_sections = [{
            "title": "New Customer Options",
            "rows": [
                {
                    "id": "service_estimate",
                    "title": "Get Service Estimate",
                    "description": "Get pricing for your vehicle service"
                },
                {
                    "id": "book_appointment",
                    "title": "Book Service Appointment", 
                    "description": "Schedule your first service with us"
                },
                {
                    "id": "contact_advisor",
                    "title": "Contact Service Advisor",
                    "description": "Speak with our service experts"
                },
                {
                    "id": "re_enter_reg",
                    "title": "Re-Enter Registration",
                    "description": "Try entering your registration again"
                },
                {
                    "id": "new_customer_benefits",
                    "title": "New Customer Benefits",
                    "description": "Learn about special offers"
                }
            ]
        }]
        
        logger.info(f"Sending vehicle not found message to {phone_number}")
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        # IMPORTANT: Send additional back/end buttons for vehicle not found state
        self.send_vehicle_not_found_buttons(phone_number)
        return message

    def send_vehicle_not_found_buttons(self, phone_number):
        """Send specialized Back and End buttons for vehicle not found state"""
        message = "Choose an option above, or:"
        buttons = [
            {"type": "reply", "reply": {"id": "try_different_reg", "title": "🔄 Try Different Registration"}},
            {"type": "reply", "reply": {"id": "end_conversation", "title": "🔚 End Chat"}}
        ]
        return self.send_message(phone_number, message, buttons=buttons)

    def handle_try_different_registration(self, phone_number):
        """Handle try different registration button"""
        # Reset to registration state
        self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
        self.user_sessions[phone_number]['vehicle_reg'] = None
        self.user_sessions[phone_number]['vehicle_data'] = None
        
        message = (
            "Let's try a different registration number! 🔄\n\n"
            "Please enter your Vehicle Registration Number:\n\n"
            "Example: KA01AB1234"
        )
        
        self.send_message(phone_number, message)
        return message

    # ============ SERVICE OPTIONS HANDLER ============

    def handle_service_options(self, phone_number, option):
        logger.info(f"Handling service option: {option} for {phone_number}")
        
        if option == 'service_history':
            return self.get_service_history(phone_number)
        elif option == 'service_estimate':
                    return self.get_service_estimate(phone_number)
        elif option == 'book_appointment':
            return self.book_service_appointment(phone_number)
        elif option == 'contact_advisor':
            return self.contact_service_advisor(phone_number)
        elif option == 'discounts_offer':
            return self.send_discounts_offer(phone_number)
        # ADD THESE TWO LINES IF THEY'RE MISSING:
        elif option == 'book_appointment':  # ← ADD THIS LINE
                return self.book_service_appointment(phone_number)  # ← ADD THIS LINE
        elif option == 'service_estimate':  # ← ADD THIS LINE  
                return self.get_service_estimate(phone_number)  # ← ADD THIS LINE        

        else:
            # Before calling AI, check if it's a genuine question
            from utils import is_genuine_query
            state = self.user_sessions.get(phone_number, {}).get('state', '')
            if is_genuine_query(option, state):
                return self.handle_ai_query(phone_number, option)
            else:
                # Not a genuine query - re-show the service menu
                return self.handle_vehicle_found(phone_number, self.user_sessions[phone_number].get('vehicle_data'))


    def handle_new_customer_options(self, phone_number, option):
        logger.info(f"Handling new customer option: {option} for {phone_number}")
        
        if option == 'service_estimate':
            return self.get_service_estimate(phone_number)
        elif option == 'book_appointment':
            # For new customers, we can still book appointment
            # Initialize booking session data
            if 'booking_data' not in self.user_sessions[phone_number]:
                self.user_sessions[phone_number]['booking_data'] = {}
            
            self.user_sessions[phone_number]['state'] = 'booking_priority'
            
            message = "Let's book your service appointment! 📅\n\nFirst, when would you prefer your appointment?"
            
            list_sections = [{"title": "Service Options", "rows": [
                    {"id": "priority_today",     "title": "Today",           "description": "Schedule for available slots before closing"},
                    {"id": "priority_tomorrow",  "title": "Tomorrow",        "description": "Next‑day booking with standard availability"},
                    {"id": "priority_this_week", "title": "Later this week", "description": "Reserve a slot for the next business day"},
                    {"id": "priority_next_week", "title": "Next week",       "description": "Advance booking to secure preferred time."}
                ]
            }]
            
            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            return message
        elif option == 'contact_advisor':
            return self.contact_service_advisor(phone_number)
        elif option == 're_enter_reg':
            self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
            # Clear previous data
            self.user_sessions[phone_number]['vehicle_reg'] = None
            self.user_sessions[phone_number]['vehicle_data'] = None
            self.send_message(phone_number, "Please enter your Vehicle Registration Number again:\n\nExample: KA01AB1234")
            return "Please enter your Vehicle Registration Number again"
        elif option == 'new_customer_benefits':
            return self.send_new_customer_benefits(phone_number)
        
        # Handle text-based fallback options
        elif option in ['estimate', 'pricing']:
            return self.get_service_estimate(phone_number)
        elif option in ['book', 'appointment', 'schedule']:
            return self.book_service_appointment(phone_number)
        elif option in ['contact', 'advisor', 'help']:
            return self.contact_service_advisor(phone_number)
        else:
            # Before calling AI, check if it's a genuine question
            from utils import is_genuine_query
            state = self.user_sessions.get(phone_number, {}).get('state', '')
            if is_genuine_query(option, state):
                return self.handle_ai_query(phone_number, option)
            else:
                # Not a genuine query - re-show the new customer menu
                return self.handle_vehicle_not_found(phone_number, self.user_sessions[phone_number].get('vehicle_reg', 'Unknown'))

    def send_new_customer_benefits(self, phone_number):
        message = (
            "🎉 Welcome! Here are your new customer benefits:\n\n"
            "✅ Complimentary car wash with first service\n"
            "✅ Priority booking slots\n"
            "✅ 10% discount on first service\n"
            "✅ Extended warranty coverage\n"
            "✅ Free vehicle health check\n"
            "✅ 24/7 roadside assistance for 6 months\n\n"
            "📞 Contact our team at 1800-xxxx-xxx for more details!"
        )
        self.send_message(phone_number, message)
        self.send_back_end_buttons(phone_number)
        return message

    def contact_service_advisor(self, phone_number):
     # Initialize contact session data
        if 'contact_data' not in self.user_sessions[phone_number]:
            self.user_sessions[phone_number]['contact_data'] = {}
        
        self.user_sessions[phone_number]['state'] = 'contact_method_selection'
        
        message = "Great! I'll help you connect with our service advisor. 👨‍💼\n\nHow would you like to connect?"
        
        list_sections = [{
            "title": "Contact Methods",
            "rows": [
                {
                    "id": "instant_call",
                    "title": "📞 Instant Call",
                    "description": "Direct phone connection"
                },
                {
                    "id": "whatsapp_chat",
                    "title": "💬 WhatsApp Chat", 
                    "description": "Chat with advisor"
                },
                {
                    "id": "request_callback",
                    "title": "📱 Request Callback",
                    "description": "Advisor will call you"
                },
                {
                    "id": "video_call",
                    "title": "🎥 Video Call",
                    "description": "For complex issues (Scheduled)"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_contact_method(self, phone_number, method):
        contact_data = self.user_sessions[phone_number]['contact_data']
        contact_data['method'] = method
        
        if method == 'instant_call':
            return self.handle_instant_call(phone_number)
        elif method == 'whatsapp_chat':
            return self.handle_whatsapp_chat(phone_number)
        elif method == 'request_callback':
            return self.start_callback_scheduling(phone_number)
        elif method == 'video_call':
            return self.start_video_call_scheduling(phone_number)
        else:
            # Before calling AI, check if it's a genuine question
            from utils import is_genuine_query
            state = self.user_sessions.get(phone_number, {}).get('state', '')
            if is_genuine_query(method, state):
                return self.handle_ai_query(phone_number, method)
            else:
                # Not a genuine query - re-show the contact method selection
                return self.contact_service_advisor(phone_number)

    def handle_instant_call(self, phone_number):
        # Reset state
        self.user_sessions[phone_number]['state'] = 'initial'
        
        message = (
            "📞 **INSTANT CALL CONNECTION**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔗 Connecting you to our service advisor...\n\n"
            "📱 **Direct Number:** +91-7483564817\n"
            "👨‍💼 **Service Advisor:** Rahul Gupta\n"
            "⏰ **Available:** 9 AM - 8 PM (Mon-Sat)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ You can call this number directly now!\n\n"
            "🙏 Thank you for choosing our service. Our expert advisor Rahul is ready to assist you with all your vehicle service needs!"
        )
        
        self.send_message(phone_number, message)

        # ADD BACK/END BUTTONS AFTER CONFIRMATION
        self.send_back_end_buttons(phone_number)

        return message

    def handle_whatsapp_chat(self, phone_number):
        # Reset state
        self.user_sessions[phone_number]['state'] = 'initial'
        
        # Create WhatsApp chat link
        whatsapp_number = "917483564817"  # Service advisor's WhatsApp number
        advisor_name = "Rahul Gupta"
        whatsapp_link = f"https://wa.me/{whatsapp_number}?text=Hi%20Rahul,%20I%20need%20assistance%20with%20my%20vehicle%20service."
        
        message = (
            "💬 **WHATSAPP CHAT CONNECTION**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔗 **Click here to start WhatsApp chat:**\n{whatsapp_link}\n\n"
            f"👨‍💼 **Service Advisor:** {advisor_name}\n"
            "⏰ **Available:** 9 AM - 8 PM (Mon-Sat)\n"
            "💬 **Response Time:** Usually within 5 minutes\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Click the link above to start chatting!\n\n"
            f"🙏 Thank you! {advisor_name} will personally assist you with all your queries and provide expert guidance for your vehicle service needs."
        )
        
        self.send_message(phone_number, message)

        # ADD BACK/END BUTTONS AFTER CONFIRMATION
        self.send_back_end_buttons(phone_number)

        return message

    def start_callback_scheduling(self, phone_number):
        self.user_sessions[phone_number]['state'] = 'callback_priority'
        
        message = "📱 **REQUEST CALLBACK**\n\nWhen would you prefer our advisor to call you?"
        
        list_sections = [{
            "title": "Callback Priority",
            "rows": [
                {
                    "id": "callback_today",
                    "title": "Today",
                    "description": "Get callback today"
                },
                {
                    "id": "callback_tomorrow",
                    "title": "Tomorrow", 
                    "description": "Get callback tomorrow"
                },
                {
                    "id": "callback_this_week",
                    "title": "Later this week",
                    "description": "Get callback later this week"
                },
                {
                    "id": "callback_next_week",
                    "title": "Next week",
                    "description": "Get callback next week"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_callback_priority(self, phone_number, priority):
        contact_data = self.user_sessions[phone_number]['contact_data']
        
        # Store priority selection
        if priority == 'callback_today':
            # ── After-6PM auto-advance: move today → tomorrow silently ──────
            from utils import get_smart_default_date as _smart_cb
            _smart_date_cb, _was_redir_cb = _smart_cb()
            if _was_redir_cb:
                from webhook import send_whatsapp_message as _swm_cb
                contact_data['callback_date'] = _smart_date_cb.strftime("%d-%m-%Y")
                _swm_cb(
                    phone_number,
                    f"⏰ Since it's after 6 PM, same-day callbacks are unavailable.\n\n"
                    f"✅ I've scheduled your callback for *tomorrow ({contact_data['callback_date']})* instead."
                )
            else:
                from utils import is_valid_appointment_slot as _isv_cb
                valid, reason = _isv_cb(datetime.now().date())
                if not valid:
                    from webhook import send_whatsapp_message as _swm_cb, send_list_message as _slm_cb
                    _swm_cb(phone_number, reason)
                    _now_cb = datetime.now()
                    _rows_cb = []
                    if _now_cb.hour < 18:
                        _rows_cb.append({"id": "callback_today", "title": "Today", "description": "Get callback today"})
                    _rows_cb += [
                        {"id": "callback_tomorrow",  "title": "Tomorrow",        "description": "Get callback tomorrow"},
                        {"id": "callback_this_week", "title": "Later this week", "description": "Get callback later this week"},
                        {"id": "callback_next_week", "title": "Next week",       "description": "Get callback next week"}
                    ]
                    _slm_cb(phone_number, "Please choose a different date:", "Choose", [{"title": "Callback Priority", "rows": _rows_cb}])
                    return
                contact_data['callback_date'] = datetime.now().strftime("%d-%m-%Y")
        elif priority == 'callback_tomorrow':
            tomorrow = datetime.now() + timedelta(days=1)
            contact_data['callback_date'] = tomorrow.strftime("%d-%m-%Y")
        elif priority == 'callback_this_week':
            contact_data['callback_date'] = 'Later this week'
        elif priority == 'callback_next_week':
            contact_data['callback_date'] = 'Next week'
        
        # Move to slot selection
        self.user_sessions[phone_number]['state'] = 'callback_slots'
        
        message = f"Perfect! Callback Date: {contact_data['callback_date']} 📅\n\nPlease choose your preferred time slot:"
        
        list_sections = [{
            "title": "Available Time Slots",
            "rows": [
                {
                    "id": "callback_morning",
                    "title": "Morning (9AM - 12PM)",
                    "description": "Morning callback slot"
                },
                {
                    "id": "callback_afternoon",
                    "title": "Afternoon (1PM - 3PM)", 
                    "description": "Afternoon callback slot"
                },
                {
                    "id": "callback_evening",
                    "title": "Evening (4PM - 6PM)",
                    "description": "Evening callback slot"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_callback_slots(self, phone_number, slot):
        contact_data = self.user_sessions[phone_number]['contact_data']
        
        # Store slot selection
        if slot == 'callback_morning':
            contact_data['callback_timing'] = 'Morning (9AM - 12PM)'
        elif slot == 'callback_afternoon':
            contact_data['callback_timing'] = 'Afternoon (1PM - 3PM)'
        elif slot == 'callback_evening':
            contact_data['callback_timing'] = 'Evening (4PM - 6PM)'
        
        # Reset state and show confirmation
        self.user_sessions[phone_number]['state'] = 'initial'
        
        message = (
            "📱 **REQUEST CALLBACK - CONFIRMATION**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 **Date:** {contact_data['callback_date']}\n"
            f"⏰ **Timing:** {contact_data['callback_timing']}\n"
            "👨‍💼 **Service Advisor:** Rahul Gupta\n"
            "📱 **Advisor Number:** +91-7483564817\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ **Callback request confirmed!**\n\n"
            "🙏 Thank you! Rahul will call you at the scheduled time to assist with your vehicle service needs. Please keep your phone available during the selected time slot."
        )
        
        self.send_message(phone_number, message)

        # ADD BACK/END BUTTONS AFTER CONFIRMATION
        self.send_back_end_buttons(phone_number)
        
        # Store callback request in database
        self.store_callback_request(phone_number, contact_data)
        
        return message

    def start_video_call_scheduling(self, phone_number):
        self.user_sessions[phone_number]['state'] = 'video_call_priority'
        
        message = "🎥 **VIDEO CALL SCHEDULING**\n\nWhen would you prefer the video call consultation?"
        
        list_sections = [{
            "title": "Video Call Priority",
            "rows": [
                {
                    "id": "video_today",
                    "title": "Today",
                    "description": "Schedule video call for today"
                },
                {
                    "id": "video_tomorrow",
                    "title": "Tomorrow", 
                    "description": "Schedule video call for tomorrow"
                },
                {
                    "id": "video_this_week",
                    "title": "Later this week",
                    "description": "Schedule for later this week"
                },
                {
                    "id": "video_next_week",
                    "title": "Next week",
                    "description": "Schedule for next week"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections,include_end_button=True)
        return message

    def handle_video_call_priority(self, phone_number, priority):
        contact_data = self.user_sessions[phone_number]['contact_data']
        
        # Store priority selection
        if priority == 'video_today':
            # ── After-6PM auto-advance: move today → tomorrow silently ──────
            from utils import get_smart_default_date as _smart_vc
            _smart_date_vc, _was_redir_vc = _smart_vc()
            if _was_redir_vc:
                from webhook import send_whatsapp_message as _swm_vc
                contact_data['video_date'] = _smart_date_vc.strftime("%d-%m-%Y")
                _swm_vc(
                    phone_number,
                    f"⏰ Since it's after 6 PM, same-day video calls are unavailable.\n\n"
                    f"✅ I've scheduled your video call for *tomorrow ({contact_data['video_date']})* instead."
                )
            else:
                from utils import is_valid_appointment_slot as _isv_vc
                valid, reason = _isv_vc(datetime.now().date())
                if not valid:
                    from webhook import send_whatsapp_message as _swm_vc, send_list_message as _slm_vc
                    _swm_vc(phone_number, reason)
                    _now_vc = datetime.now()
                    _rows_vc = []
                    if _now_vc.hour < 18:
                        _rows_vc.append({"id": "video_today", "title": "Today", "description": "Schedule video call for today"})
                    _rows_vc += [
                        {"id": "video_tomorrow",  "title": "Tomorrow",        "description": "Schedule video call for tomorrow"},
                        {"id": "video_this_week", "title": "Later this week", "description": "Schedule for later this week"},
                        {"id": "video_next_week", "title": "Next week",       "description": "Schedule for next week"}
                    ]
                    _slm_vc(phone_number, "Please choose a different date:", "Choose", [{"title": "Video Call Priority", "rows": _rows_vc}])
                    return
                contact_data['video_date'] = datetime.now().strftime("%d-%m-%Y")
        elif priority == 'video_tomorrow':
            tomorrow = datetime.now() + timedelta(days=1)
            contact_data['video_date'] = tomorrow.strftime("%d-%m-%Y")
        elif priority == 'video_this_week':
            contact_data['video_date'] = 'Later this week'
        elif priority == 'video_next_week':
            contact_data['video_date'] = 'Next week'
        
        # Move to slot selection
        self.user_sessions[phone_number]['state'] = 'video_call_slots'
        
        message = f"Excellent! Video Call Date: {contact_data['video_date']} 📅\n\nPlease choose your preferred time slot:"
        
        list_sections = [{
            "title": "Available Time Slots",
            "rows": [
                {
                    "id": "video_morning",
                    "title": "Morning (9AM - 12PM)",
                    "description": "Morning video call slot"
                },
                {
                    "id": "video_afternoon",
                    "title": "Afternoon (1PM - 3PM)", 
                    "description": "Afternoon video call slot"
                },
                {
                    "id": "video_evening",
                    "title": "Evening (4PM - 6PM)",
                    "description": "Evening video call slot"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_video_call_slots(self, phone_number, slot):
        contact_data = self.user_sessions[phone_number]['contact_data']
        
        # Store slot selection
        if slot == 'video_morning':
            contact_data['video_timing'] = 'Morning (9AM - 12PM)'
        elif slot == 'video_afternoon':
            contact_data['video_timing'] = 'Afternoon (1PM - 3PM)'
        elif slot == 'video_evening':
            contact_data['video_timing'] = 'Evening (4PM - 6PM)'
        
        # Reset state and show confirmation
        self.user_sessions[phone_number]['state'] = 'initial'
        
        message = (
            "🎥 **VIDEO CALL - CONFIRMATION**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 **Date:** {contact_data['video_date']}\n"
            f"⏰ **Timing:** {contact_data['video_timing']}\n"
            "👨‍💼 **Service Advisor:** Rahul Gupta\n"
            "🎥 **Platform:** Google Meet/WhatsApp Video\n"
            "📱 **Contact:** +91-7483564817\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ **Video call scheduled successfully!**\n\n"
            "🙏 Thank you! Rahul will send you the video call link before the scheduled time. This consultation is perfect for complex technical issues where visual inspection is helpful."
        )
        
        self.send_message(phone_number, message)
        
        # ADD BACK/END BUTTONS AFTER CONFIRMATION
        self.send_back_end_buttons(phone_number)

        # Store video call request in database
        self.store_video_call_request(phone_number, contact_data)
        
        return message

    def store_callback_request(self, phone_number, contact_data):
        """Store callback request in database"""
        try:
            conn = self.db.get_connection()
            if conn:
                cursor = conn.cursor()
                query = """
                    INSERT INTO service_requests 
                    (phone_number, request_type, scheduled_date, scheduled_time, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """
                cursor.execute(query, (
                    phone_number,
                    'callback',
                    contact_data['callback_date'],
                    contact_data['callback_timing'],
                    'pending',
                    datetime.now()
                ))
                conn.commit()
                logger.info(f"Callback request stored for {phone_number}")
        except Exception as e:
            logger.error(f"Error storing callback request: {e}")
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()

    def store_video_call_request(self, phone_number, contact_data):
        """Store video call request in database"""
        try:
            conn = self.db.get_connection()
            if conn:
                cursor = conn.cursor()
                query = """
                    INSERT INTO service_requests 
                    (phone_number, request_type, scheduled_date, scheduled_time, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """
                cursor.execute(query, (
                    phone_number,
                    'video_call',
                    contact_data['video_date'],
                    contact_data['video_timing'],
                    'pending',
                    datetime.now()
                ))
                conn.commit()
                logger.info(f"Video call request stored for {phone_number}")
        except Exception as e:
            logger.error(f"Error storing video call request: {e}")
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()


        # ============ SERVICE HISTORY ============

    def get_service_history(self, phone_number):
        session = self.user_sessions[phone_number]
        reg_number = session.get('vehicle_reg')
        history = self.db.get_service_history(reg_number)
        
        if history:
            message = f"📋 Service History for {reg_number}:\n\n"
            for i, record in enumerate(history[:5], 1):
                bill_date = record.get('billDate')
                bill_date_str = bill_date.strftime('%d-%m-%Y') if bill_date else "N/A"
                message += f"{i}. Bill Date: {bill_date_str}\n"
                message += f"   Service: {record.get('lastServiceType', 'N/A')}\n"
                message += f"   Amount: ₹{record.get('billAmt', 'N/A')}\n"
                message += f"   ODO: {record.get('lastServiceMeterReading', 'N/A')} km\n\n"
            message += "📞 Need more details? Contact our service advisor!"
        else:
            message = "❌ No service history found in our records.\n\n📞 Contact our service advisor for more information."
        
           
        self.send_message(phone_number, message)
         # ADD THESE LINES - Send action buttons after service history
        self.send_service_history_options(phone_number)
        #    # ADD BACK/END BUTTONS AFTER CONFIRMATION
        # self.send_back_end_buttons(phone_number)
        return message
    
    def send_service_history_options(self, phone_number):
        """Send action buttons after service history is displayed"""
        message = "What would you like to do next?"
        buttons = [
            {"type": "reply", "reply": {"id": "book_appointment", "title": "🛠️ Book Service"}},
            {"type": "reply", "reply": {"id": "service_estimate", "title": "💰 Get Estimate"}},
            {"type": "reply", "reply": {"id": "back_to_main", "title": "🔙 Back to Main"}}
            ]
        self.send_message(phone_number, message, buttons=buttons)
        return message

    # ============ SERVICE ESTIMATE ============

    def get_service_estimate(self, phone_number):
        """Enhanced service estimate with same service type confirmation"""
        session = self.user_sessions[phone_number]
        vehicle_data = session.get('vehicle_data')
        
        # Check if vehicle is found in database
        if not vehicle_data:
            # Vehicle not found - limited estimate options
            self.user_sessions[phone_number]['state'] = 'estimate_no_vehicle'
            message = (
                "💰 **SERVICE ESTIMATE**\n\n"
                "Since your vehicle is not in our service database, I can still help you with estimates for common services.\n\n"
                "Please select the service type you need an estimate for:"
            )
            
            list_sections = [{
                "title": "Service Types",
                "rows": [
                    {"id": "estimate_accidental", "title": "🚗 Accidental/Bodywork", "description": "Collision repair, dent removal, painting"},
                    {"id": "estimate_running_repair", "title": "🔧 Running Repair", "description": "Engine, transmission, general repairs"},
                    {"id": "estimate_tire_wheel", "title": "🛞 Tire & Wheel Service", "description": "Tire replacement, wheel alignment, balancing"},
                    {"id": "estimate_battery_electrical", "title": "⚡ Battery & Electrical", "description": "Battery, alternator, electrical issues"}
                ]
            }]
            
            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            return message
        
        # Vehicle found - check for next service type
        next_service_type = vehicle_data.get('nextServiceType')
        
        # if next_service_type and next_service_type != 'N/A':
        # if True:  # Always proceed to show estimate regardless of next_service_type    or 
          # Vehicle has next service type, ask if estimate for same service
        self.user_sessions[phone_number]['state'] = 'estimate_same_service_confirmation' 
        self.user_sessions[phone_number]['next_service_type'] = next_service_type or 'General Service'
            
        # Use actual next_service_type from DB, or fall back to 'General Service'
        display_service_type = next_service_type if next_service_type and next_service_type != 'N/A' else 'General Service'

        message = (
            f"💰 *SERVICE ESTIMATE*\n\n"
            f"I see your next scheduled service is: *{display_service_type}*\n\n"
            f"Would you like an estimate for this same service type?"
        )

        buttons = [
            {"type": "reply", "reply": {"id": "estimate_same_yes", "title": "✅ Yes"}},
            {"type": "reply", "reply": {"id": "estimate_same_no", "title": "❌ Different Service"}}
        ]
        
        self.send_message(phone_number, message, buttons=buttons)
        return message
        # else:
        #     # No next service type available, ask to choose service type
        #     return self.show_service_type_selection(phone_number)

    def handle_vehicle_not_found_estimate(self, phone_number):
        """Handle service estimate for vehicles not found in database"""
        session = self.user_sessions[phone_number]
        session['state'] = 'waiting_service_type_selection'
        
        message = "🚗 **SERVICE TYPE SELECTION**\n\nSince your vehicle isn't in our service history, please select the type of service you need:"
        
        buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": "service_type_free",
                    "title": "🆓 Free Service"
                }
            },
            {
                "type": "reply", 
                "reply": {
                    "id": "service_type_paid",
                    "title": "💰 Paid Maintenance"
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": "service_type_other",
                    "title": "🔧 Other"
                }
            }
        ]
        
        return self.send_message(phone_number, message, buttons=buttons)

    def handle_estimate_same_service_confirmation(self, phone_number, response):
        """Handle yes/no response for same service type estimate"""
        session = self.user_sessions[phone_number]
        
        if response == 'estimate_same_yes':
            # User wants estimate for same service type - get estimate data
            return self.show_service_estimate_details(phone_number)
        
        elif response == 'estimate_same_no':
            # User wants different service type
            return self.show_service_type_selection(phone_number)
        
        else:
            return self.handle_ai_query(phone_number, response)

    def show_service_estimate_details(self, phone_number):
        """Show detailed service estimate using database query"""
        session = self.user_sessions[phone_number]
        vehicle_reg = session.get('vehicle_reg')
        
        # Get estimate data from database
        estimate_data = self.db.get_service_estimate_data(vehicle_reg)
        
        if estimate_data and estimate_data.get('labourAmount'):
            # Data found - show detailed estimate
            labour_amount = estimate_data.get('labourAmount', 0)
            parts_amount = estimate_data.get('partsAmount', 0)
            total_amount = estimate_data.get('total', 0)
            
            message = (
                f"💰 **SERVICE ESTIMATE DETAILS**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🚗 **Vehicle:** {vehicle_reg}\n"
                f"🔧 **Service Type:** {session.get('next_service_type', 'Periodic Service')}\n\n"
                f"📊 **COST BREAKDOWN:**\n"
                f"👨‍🔧 **Labor Charge:** ₹{labour_amount:,.2f}\n"
                f"🔩 **Parts Charge:** ₹{parts_amount:,.2f}\n"
                f"💰 **Total Estimate:** ₹{total_amount:,.2f}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 *This is an estimated cost. Final cost may vary based on actual inspection.*\n\n"
                f"What would you like to do next?"
            )
            
            buttons = [
                {"type": "reply", "reply": {"id": "book_next_service", "title": "📅 Book Next Service"}},
                {"type": "reply", "reply": {"id": "back_to_main", "title": "🔙 Back to Main Menu"}}
            ]
            
            self.send_message(phone_number, message, buttons=buttons)
            
            # Store estimate request in database
            self.db.store_service_estimate_request(phone_number, vehicle_reg, 'same_service_type')
            
            # Reset state
            self.user_sessions[phone_number]['state'] = 'initial'
            return message
        else:
            # No pricing data in DB - ask customer to connect with a service advisor
            self.user_sessions[phone_number]['state'] = 'estimate_contact_advisor_prompt'

            message = (
                f"💰 *SERVICE ESTIMATE*\n\n"
                f"I couldn't find detailed pricing for your specific vehicle and service type in our database.\n\n"
                f"📞 Our service advisor will provide you with an accurate estimate.\n\n"
                f"Would you like me to arrange for them to call you?"
            )

            buttons = [
                {"type": "reply", "reply": {"id": "estimate_contact_advisor", "title": "📞 Contact Advisor"}},
                {"type": "reply", "reply": {"id": "back_to_main",            "title": "🔙 Back to Main Menu"}}
            ]

            self.send_message(phone_number, message, buttons=buttons)
            return message

    def handle_estimate_2hr_confirmation(self, phone_number, response):
        """Handle 2-hour advisor confirmation response"""
        if response == 'estimate_2hr_yes':
            self.user_sessions[phone_number]['state'] = 'initial'
            message = (
                "Great! ✅ I've notified our service team.\n\n"
                "A service advisor will review your vehicle history and message you here within 2 hours with a detailed estimate.\n\n"
                "Thank you for choosing AutoSherpa!"
            )
            # Store the request
            vehicle_reg = self.user_sessions[phone_number].get('vehicle_reg')
            self.db.store_service_estimate_request(phone_number, vehicle_reg, 'advisor_manual_review')
            
            self.send_message(phone_number, message)
            return message
        else:
            return self.handle_back_to_main(phone_number)


    def show_service_type_selection(self, phone_number):
        """Show service type selection for estimates"""
        self.user_sessions[phone_number]['state'] = 'estimate_service_type_selection'
        
        message = "💰 **SERVICE ESTIMATE**\n\nPlease select the service type you need an estimate for:"
        
        list_sections = [{
            "title": "Service Types", 
            "rows": [
                {"id": "estimate_accidental", "title": "🚗 Accidental/Bodywork", "description": "Collision repair, dent removal, painting"},
                {"id": "estimate_running_repair", "title": "🔧 Running Repair", "description": "Engine, transmission, general repairs"},
                {"id": "estimate_tire_wheel", "title": "🛞 Tire & Wheel Service", "description": "Tire replacement, wheel alignment, balancing"},
                {"id": "estimate_battery_electrical", "title": "⚡ Battery & Electrical", "description": "Battery, alternator, electrical issues"}
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_estimate_service_type_selection(self, phone_number, service_type):
        """Handle service type selection for estimates"""
        session = self.user_sessions[phone_number]
        
        # Store selected service type
        service_type_map = {
            'estimate_accidental': 'Accidental/Bodywork',
            'estimate_running_repair': 'Running Repair', 
            'estimate_tire_wheel': 'Tire & Wheel Service',
            'estimate_battery_electrical': 'Battery & Electrical'
        }
        
        selected_service = service_type_map.get(service_type, 'Unknown Service')
        session['selected_estimate_service'] = selected_service
        
        # Ask if they want to upload images
        self.user_sessions[phone_number]['state'] = 'estimate_image_upload_confirmation'
        
        message = (
            f"💰 **SERVICE ESTIMATE**\n\n"
            f"Selected Service: **{selected_service}**\n\n"
            f"📸 Do you want to upload images of your vehicle to help us provide a more accurate estimate?"
        )
        
        buttons = [
            {"type": "reply", "reply": {"id": "estimate_image_yes", "title": "✅ Yes, Upload Images"}},
            {"type": "reply", "reply": {"id": "estimate_image_no", "title": "❌ No, Thanks"}}
        ]
        
        self.send_message(phone_number, message, buttons=buttons)
        return message

    def handle_estimate_image_upload_confirmation(self, phone_number, response):
        """Handle image upload confirmation for estimates"""

        session = self.user_sessions[phone_number]
        selected_service = session.get('selected_estimate_service', 'Service')

        if response == 'estimate_image_yes':
            vehicle_reg = session.get('vehicle_reg', 'N/A')

            # Read Cloudflare BASE_URL from .env
            BASE_URL = os.getenv("BASE_URL")

            # Build secure upload form URL
            form_url = (
                f"{BASE_URL}/image-upload-form"
                f"?phone={phone_number}"
                f"&vehicle={vehicle_reg}"
                f"&service={selected_service}"
            )

            message = (
                f"📸 *IMAGE UPLOAD*\n\n"
                f"Please click the link below to upload images of your vehicle:\n\n"
                f"🔗 Upload Form:\n{form_url}\n\n"
                f"📋 What you'll do:\n"
                f"• Click the link to open the upload form\n"
                f"• Upload multiple images of areas needing {selected_service}\n"
                f"• Submit the form\n\n"
                f"✅ You'll receive a confirmation once images are uploaded successfully."
            )

            # Set state
            self.user_sessions[phone_number]['state'] = 'waiting_for_form_submission'

            self.send_message(phone_number, message)
            return message

        elif response == 'estimate_image_no':
            return self.finalize_estimate_request(phone_number, image_uploaded=False)

        else:
            return self.handle_ai_query(phone_number, response) 
               
    def handle_multiple_image_uploads(self, phone_number):
        """Handle multiple images uploaded at once with delay to count all"""
        import threading
        import time
        
        def delayed_response():
            # Wait 2 seconds to allow all images to be processed
            time.sleep(2)
            
            session = self.user_sessions[phone_number]
            current_count = session.get('uploaded_image_count', 0)
            
            if current_count > 0:
                confirmation_message = (
                    f"✅ **All images received!**\n\n"
                    f"📸 Thank you for uploading your vehicle images.\n"
                    f"**Total images uploaded: {current_count}**\n\n"
                    f"Click **Confirm** below to submit your estimate request."
                )
                
                buttons = [{"type": "reply", "reply": {"id": "estimate_image_confirm", "title": "✅ Confirm & Submit"}}]
                self.send_message(phone_number, confirmation_message, buttons=buttons)
        
        # Start delayed response in background
        threading.Thread(target=delayed_response, daemon=True).start()


    def handle_estimate_image_confirm(self, phone_number):
        """Handle image upload confirmation"""
        return self.finalize_estimate_request(phone_number, image_uploaded=True)

    def finalize_estimate_request(self, phone_number, image_uploaded=False):
        """Finalize the estimate request and show confirmation"""
        session = self.user_sessions[phone_number]
        selected_service = session.get('selected_estimate_service', 'Service')
        vehicle_reg = session.get('vehicle_reg', 'N/A')
        
        # Store the request in database
        # Store the request in database
        image_count = session.get('uploaded_image_count', 0)
        self.db.store_service_estimate_request(phone_number, vehicle_reg, selected_service, image_uploaded, image_count)

        image_text = "with uploaded images" if image_uploaded else "without images"
        
        # Get image count from session
        image_count = session.get('uploaded_image_count', 0)

        if image_uploaded and image_count > 0:
            images_text = f"📸 **Images:** ✅ {image_count} uploaded"
        else:
            images_text = f"📸 **Images:** ❌ Not uploaded"

        message = (
            f"✅ **ESTIMATE REQUEST SUBMITTED**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔧 **Service Type:** {selected_service}\n"
            f"🚗 **Vehicle:** {vehicle_reg}\n"
            f"{images_text}\n\n"
            f"📞 **Our service advisor will call you within the next 2 hours** to discuss your estimate and provide detailed pricing.\n\n"
            f"🙏 Thank you for choosing our service!"
        )

        
        # Reset state
        self.user_sessions[phone_number]['state'] = 'initial'
        
        self.send_message(phone_number, message)
        self.send_back_end_buttons(phone_number)
        
        return message
    
    # ============ BOOK APPOINTMENT ============

    def book_service_appointment(self, phone_number):
        """Book service appointment - check vehicle registration first"""
        session = self.user_sessions[phone_number]
        vehicle_reg = session.get('vehicle_reg')
        vehicle_data = session.get('vehicle_data')
        current_state = session.get('state')
        
        # Scenario 1: Vehicle is already registered and found in database
        if vehicle_reg and vehicle_data:
            # Vehicle already registered and found, proceed with booking
            if 'booking_data' not in self.user_sessions[phone_number]:
                self.user_sessions[phone_number]['booking_data'] = {}
            
            self.user_sessions[phone_number]['state'] = 'booking_priority'
            
            message = "Let's book your service appointment! 📅\n\nFirst, when would you prefer your appointment?"
            
            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            _now_bsa = datetime.now()
            _rows_bsa = []
            if _now_bsa.hour < 18:
                _rows_bsa.append({"id": "priority_today", "title": "Today", "description": "Schedule for available slots before closing"})
            _rows_bsa += [
                {"id": "priority_tomorrow",  "title": "Tomorrow",        "description": "Next‑day booking with standard availability"},
                {"id": "priority_this_week", "title": "Later this week", "description": "Reserve a slot for the next business day"},
                {"id": "priority_next_week", "title": "Next week",       "description": "Advance booking to secure preferred time."}
            ]
            list_sections = [{"title": "Service Options", "rows": _rows_bsa}]
            
            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            return message
        
        # Scenario 2: Vehicle registration was provided but NOT FOUND in database
        elif vehicle_reg and not vehicle_data:
            # Vehicle was searched but not found — still allow booking
            if 'booking_data' not in self.user_sessions[phone_number]:
                self.user_sessions[phone_number]['booking_data'] = {}

            self.user_sessions[phone_number]['state'] = 'booking_priority'
            message = "Let's book your service appointment! 📅\n\nFirst, when would you prefer your appointment?"

            # ── 6 PM cutoff: hide Today option after 6 PM ─────────────────
            _now_s2 = datetime.now()
            _rows_s2 = []
            if _now_s2.hour < 18:
                _rows_s2.append({"id": "priority_today", "title": "Today", "description": "Schedule for available slots before closing"})
            _rows_s2 += [
                {"id": "priority_tomorrow",  "title": "Tomorrow",        "description": "Next‑day booking with standard availability"},
                {"id": "priority_this_week", "title": "Later this week", "description": "Reserve a slot for the next business day"},
                {"id": "priority_next_week", "title": "Next week",       "description": "Advance booking to secure preferred time."}
            ]
            list_sections = [{"title": "Service Options", "rows": _rows_s2}]

            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            return message
        # Scenario 3: No vehicle registration provided yet
        else:
            # No vehicle registration, ask for it first
            self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
            message = (
                "To book your service appointment, I need your vehicle registration number first.\n\n"
                "Please enter your Vehicle Registration Number:\n\n"
                "Example: KA01AB1234"
            )
            self.send_message(phone_number, message)
            return message

    def handle_booking_priority(self, phone_number, priority):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        if priority == 'priority_today':
            # ── After-6PM auto-advance: move today → tomorrow silently ──────
            from utils import get_smart_default_date as _smart_bk
            _smart_date_bk, _was_redir_bk = _smart_bk()
            if _was_redir_bk:
                from webhook import send_whatsapp_message as _swm_bk
                tomorrow_str = _smart_date_bk.strftime("%d-%m-%Y")
                booking_data['appointment_date'] = tomorrow_str
                booking_data['selected_date'] = _smart_date_bk.strftime('%Y-%m-%d')
                _swm_bk(
                    phone_number,
                    f"⏰ Since it's after 6 PM, same-day bookings are unavailable.\n\n"
                    f"✅ I've scheduled your service for *tomorrow ({tomorrow_str})* instead.\n\n"
                    f"Please choose a preferred time slot 👇"
                )
                return self.show_time_slots(phone_number)
            else:
                today_date = datetime.now().date()
                from utils import is_valid_appointment_slot as _isv
                valid, reason = _isv(today_date)
                if not valid:
                    from webhook import send_whatsapp_message as _swm, send_list_message as _slm
                    _swm(phone_number, reason)
                    _now = datetime.now()
                    _rows = []
                    if _now.hour < 18:
                        _rows.append({"id": "priority_today", "title": "Today", "description": "Schedule for available slots before closing"})
                    _rows += [
                        {"id": "priority_tomorrow",  "title": "Tomorrow",        "description": "Next‑day booking with standard availability"},
                        {"id": "priority_this_week", "title": "Later this week", "description": "Reserve a slot for the next business day"},
                        {"id": "priority_next_week", "title": "Next week",       "description": "Advance booking to secure preferred time."}
                    ]
                    _slm(phone_number, "Please choose a different date:", "Choose", [{"title": "Service Priority", "rows": _rows}])
                    return self.send_end_button(phone_number)
                booking_data['appointment_date'] = datetime.now().strftime("%d-%m-%Y")
                booking_data['selected_date'] = datetime.now().strftime('%Y-%m-%d')
                return self.show_time_slots(phone_number)
            
        elif priority == 'priority_tomorrow':
            tomorrow = datetime.now() + timedelta(days=1)
            booking_data['appointment_date'] = tomorrow.strftime("%d-%m-%Y")
            booking_data['selected_date'] = tomorrow.strftime('%Y-%m-%d')
            return self.show_time_slots(phone_number)
            
        elif priority == 'priority_this_week':
            booking_data['appointment_date'] = 'Later this week'
            self.user_sessions[phone_number]['state'] = 'booking_this_week_dates'
            
            dates = self.get_this_week_dates()
            
            # Log for debugging
            logger.info(f"This week dates generated: {dates}")
            
            if not dates or len(dates) == 0:
                # No dates this week, redirect to next week
                logger.info("No dates available this week, showing next week")
                return self.handle_booking_priority(phone_number, 'priority_next_week')
                
            message = "Please choose your preferred date for this week:"
            
            list_sections = [{
                "title": "Service Options",
                "rows": dates
            }]
            
            return self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            
        elif priority == 'priority_next_week':
            booking_data['appointment_date'] = 'Next week'
            self.user_sessions[phone_number]['state'] = 'booking_next_week_dates'
            
            dates = self.get_next_week_dates()
            
            # Log for debugging
            logger.info(f"Next week dates generated: {dates}")
            
            message = "Please choose your preferred date for next week:"
            
            list_sections = [{
                "title": "Service Options",
                "rows": dates
            }]
            
            return self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)

        else:
            # ── Smart Date Resolver (Free-text) ──
            all_dates = sorted(list(set([datetime.now().date(), (datetime.now() + timedelta(days=1)).date()] + [datetime.strptime(d['id'].replace('date_', ''), '%Y%m%d').date() for d in self.get_this_week_dates()] + [datetime.strptime(d['id'].replace('date_', ''), '%Y%m%d').date() for d in self.get_next_week_dates()])))
            resolved_id = resolve_date_from_text(priority, all_dates, prefix="date_")
            
            if resolved_id:
                date_val = resolved_id.replace("date_", "")
                target_datetime = datetime.strptime(date_val, "%Y%m%d")
                
                # Check 6PM cutoff for Today
                if target_datetime.date() == datetime.now().date() and datetime.now().hour >= 18:
                    from webhook import send_whatsapp_message as _swm_alt
                    tomorrow_date = target_datetime.date() + timedelta(days=1)
                    tomorrow_str = tomorrow_date.strftime("%d-%m-%Y")
                    booking_data['appointment_date'] = tomorrow_str
                    booking_data['selected_date'] = tomorrow_date.strftime('%Y-%m-%d')
                    _swm_alt(phone_number, f"⏰ Since it's after 6 PM, same-day bookings are unavailable. I've scheduled your service for *tomorrow ({tomorrow_str})*.")
                    return self.show_time_slots(phone_number)

                valid, reason = is_valid_appointment_slot(target_datetime.date())
                if not valid:
                    from webhook import send_whatsapp_message as _swm_err
                    _swm_err(phone_number, reason)
                    return
                
                booking_data['selected_date'] = target_datetime.strftime('%Y-%m-%d')
                booking_data['appointment_date'] = f"{target_datetime.strftime('%A')} ({target_datetime.strftime('%d %b')})"
                return self.show_time_slots(phone_number)

            return self.handle_ai_query(phone_number, priority)


    def handle_this_week_date_selection(self, phone_number, selected_date):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        # Parse the date from the ID
        if selected_date.startswith('date_'):
            date_str = selected_date.replace('date_', '')
            selected_datetime = datetime.strptime(date_str, '%Y%m%d')
            booking_data['selected_date'] = selected_datetime.strftime('%Y-%m-%d')
            booking_data['appointment_date'] = f"{selected_datetime.strftime('%A')} ({selected_datetime.strftime('%d %b')})"
            
            # Continue to time slots
            return self.show_time_slots(phone_number)
        else:
            # ── Smart Date Resolver (Free-text) ──
            all_dates = [datetime.strptime(d['id'].replace('date_', ''), '%Y%m%d').date() for d in self.get_this_week_dates()]
            resolved_id = resolve_date_from_text(selected_date, all_dates, prefix="date_")
            if resolved_id:
                return self.handle_this_week_date_selection(phone_number, resolved_id)
            
            return self.handle_ai_query(phone_number, selected_date)

    def handle_next_week_date_selection(self, phone_number, selected_date):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        # Parse the date from the ID
        if selected_date.startswith('date_'):
            date_str = selected_date.replace('date_', '')
            selected_datetime = datetime.strptime(date_str, '%Y%m%d')
            booking_data['selected_date'] = selected_datetime.strftime('%Y-%m-%d')
            booking_data['appointment_date'] = f"{selected_datetime.strftime('%A')} ({selected_datetime.strftime('%d %b')})"
            
            # Continue to time slots
            return self.show_time_slots(phone_number)
        else:
            # ── Smart Date Resolver (Free-text) ──
            all_dates = [datetime.strptime(d['id'].replace('date_', ''), '%Y%m%d').date() for d in self.get_next_week_dates()]
            resolved_id = resolve_date_from_text(selected_date, all_dates, prefix="date_")
            if resolved_id:
                return self.handle_next_week_date_selection(phone_number, resolved_id)
            
            return self.handle_ai_query(phone_number, selected_date)

    def handle_numeric_date_selection(self, phone_number, message):
        """Handle numeric selection when list fails"""
        session = self.user_sessions[phone_number]
        
        # Check if we have stored fallback options
        if 'fallback_options' not in session:
            return False
            
        try:
            choice = int(message.strip())
            fallback_options = session['fallback_options']
            
            if 1 <= choice <= len(fallback_options):
                selected_option = fallback_options[choice - 1]
                selected_date_id = selected_option['id']
                
                # Clear fallback options
                del session['fallback_options']
                
                # Process the selection based on current state
                if session['state'] == 'booking_this_week_dates':
                    return self.handle_this_week_date_selection(phone_number, selected_date_id)
                elif session['state'] == 'booking_next_week_dates':
                    return self.handle_next_week_date_selection(phone_number, selected_date_id)
                    
        except (ValueError, KeyError, IndexError):
            pass
        
        return False
    
    def show_time_slots(self, phone_number):
        booking_data = self.user_sessions[phone_number]['booking_data']
        self.user_sessions[phone_number]['state'] = 'booking_slots'
        
        message = f"Great! You selected: {booking_data['appointment_date']} 📅\n\nNow, please choose your preferred time slot:"
        
        list_sections = [{
            "title": "Service Options",
            "rows": [
                {
                    "id": "slot_morning",
                    "title": "Morning (9AM - 12PM)",
                    "description": "Morning appointment slot"
                },
                {
                    "id": "slot_afternoon", 
                    "title": "Afternoon (1PM - 3PM)",
                    "description": "Afternoon appointment slot"
                },
                {
                    "id": "slot_evening",
                    "title": "Evening (4PM - 6PM)", 
                    "description": "Evening appointment slot"
                }
            ]
        }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_booking_slots(self, phone_number, slot):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        if slot == 'slot_morning':
            booking_data['timing'] = 'Morning (9AM - 12PM)'
        elif slot == 'slot_afternoon':
            booking_data['timing'] = 'Afternoon (1PM - 3PM)'
        elif slot == 'slot_evening':
            booking_data['timing'] = 'Evening (4PM - 6PM)'
        
        self.user_sessions[phone_number]['state'] = 'waiting_full_name'
        
        message = f"Perfect! You selected: {booking_data['timing']} ⏰\n\nPlease confirm below details:\n\n📝 Please provide your Full Name:"
        self.send_message(phone_number, message)
        return message

    def handle_full_name_input(self, phone_number, full_name):
        booking_data = self.user_sessions[phone_number]['booking_data']
        booking_data['full_name'] = full_name

        # Auto-capture the customer's WhatsApp number - no need to ask.
        # phone_number is the WhatsApp sender ID (e.g. 919876543210).
        # Strip the country-code prefix (91) to store a clean 10-digit number.
        raw = str(phone_number).strip()
        if raw.startswith('91') and len(raw) == 12:
            customer_phone = raw[2:]   # '919876543210' -> '9876543210'
        elif len(raw) == 10 and raw.isdigit():
            customer_phone = raw
        else:
            customer_phone = raw       # fallback: store as-is
        booking_data['customer_phone'] = customer_phone

        # Skip the phone-number question and go straight to Service Preferences.
        self.user_sessions[phone_number]['state'] = 'booking_preferences'
        message = f"Thank you, {full_name}! 👤\n\nNow, please select your Service Preference:"
        list_sections = [{
            "title": "Service Preferences",
            "rows": [
                {"id": "pref_drive_in",    "title": "Drive In",        "description": "You bring vehicle to our center"},
                {"id": "pref_pickup_only", "title": "Pickup Only",     "description": "We pickup from your location"},
                {"id": "pref_pickup_drop", "title": "Pickup and Drop", "description": "We pickup and drop back"}
            ]
        }]
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message


    def handle_phone_input(self, phone_number, phone_input):
        booking_data = self.user_sessions[phone_number]['booking_data']

        # Normalize: keep digits only (removes +91, spaces, dashes, etc.)
        digits = self._normalize_digits(phone_input)

        # Validate: must be exactly 10 digits
        if not self._is_valid_10_digit_mobile(digits):
            # Stay in the same state and ask again
            self.user_sessions[phone_number]['state'] = 'waiting_phone_number'
            message = (
                "❗ Invalid phone number.\n\n"
                "Please enter a 10-digit mobile number (example: 9876543210)."
            )
            self.send_message(phone_number, message)
            return message

        # Save normalized 10-digit number
        booking_data['customer_phone'] = digits

        # Proceed to next step (Service Preferences)
        self.user_sessions[phone_number]['state'] = 'booking_preferences'
        message = f"Great! Phone: {digits} 📱\n\nNow, please select your Service Preference:"
        list_sections = [{
            "title": "Service Preferences",
            "rows": [
                {"id": "pref_drive_in", "title": "Drive In", "description": "You bring vehicle to our center"},
                {"id": "pref_pickup_only", "title": "Pickup Only", "description": "We pickup from your location"},
                {"id": "pref_pickup_drop", "title": "Pickup and Drop", "description": "We pickup and drop back"}
            ]
        }]
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    def handle_booking_preferences(self, phone_number, preference):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        # Store preference selection
        if preference == 'pref_drive_in':
            booking_data['service_preference'] = 'Drive In'
            # Drive In: No address needed, go directly to special instructions
            self.user_sessions[phone_number]['state'] = 'waiting_special_instructions'
            message = f"Service Preference: {booking_data['service_preference']} ✅\n\n📝 Any Special Instructions? (Type your instructions or 'none' if no special requests):"
            self.send_message(phone_number, message)
            
        elif preference == 'pref_pickup_only':
            booking_data['service_preference'] = 'Pickup Only'
            # Pickup Only: Ask for pickup address
            self.user_sessions[phone_number]['state'] = 'waiting_pickup_address'
            message = f"Service Preference: {booking_data['service_preference']} ✅\n\n🏠 Please provide your Pickup Address:"
            self.send_message(phone_number, message)
            
        elif preference == 'pref_pickup_drop':
            booking_data['service_preference'] = 'Pickup and Drop'
            # Pickup and Drop: Ask for pickup address first
            self.user_sessions[phone_number]['state'] = 'waiting_pickup_address'
            message = f"Service Preference: {booking_data['service_preference']} ✅\n\n🏠 Please provide your Pickup Address:"
            self.send_message(phone_number, message)
        
        return message
    
    def handle_pickup_address_input(self, phone_number, pickup_address):
        booking_data = self.user_sessions[phone_number]['booking_data']
        booking_data['pickup_address'] = pickup_address
        
        if booking_data['service_preference'] == 'Pickup Only':
            # For Pickup Only, go to special instructions
            self.user_sessions[phone_number]['state'] = 'waiting_special_instructions'
            message = f"Pickup Address saved: {pickup_address} 🏠\n\n📝 Any Special Instructions? (Type your instructions or 'none' if no special requests):"
            self.send_message(phone_number, message)
            
        elif booking_data['service_preference'] == 'Pickup and Drop':
            # For Pickup and Drop, ask for drop address options
            self.user_sessions[phone_number]['state'] = 'waiting_drop_address_selection'
            message = f"Pickup Address saved: {pickup_address} 🏠\n\nNow for Drop Address, please choose:"
            
            list_sections = [{
                "title": "Drop Address Options",
                "rows": [
                    {
                        "id": "same_as_pickup",
                        "title": "Same as Pickup",
                        "description": "Use same address for drop"
                    },
                    {
                        "id": "different_drop_address",
                        "title": "Enter Drop Address",
                        "description": "Specify different drop location"
                    }
                ]
            }]
            
            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        
        return message

    def handle_drop_address_selection(self, phone_number, selection):
        booking_data = self.user_sessions[phone_number]['booking_data']
        
        if selection == 'same_as_pickup':
            # Use same address as pickup
            booking_data['drop_address'] = booking_data['pickup_address']
            self.user_sessions[phone_number]['state'] = 'waiting_special_instructions'
            message = f"Drop Address: Same as pickup address ✅\n\n📝 Any Special Instructions? (Type your instructions or 'none' if no special requests):"
            self.send_message(phone_number, message)
            
        elif selection == 'different_drop_address':
            # Ask for different drop address
            self.user_sessions[phone_number]['state'] = 'waiting_drop_address_input'
            message = "🏠 Please enter your Drop Address:"
            self.send_message(phone_number, message)
        
        return message

    def handle_drop_address_input(self, phone_number, drop_address):
        booking_data = self.user_sessions[phone_number]['booking_data']
        booking_data['drop_address'] = drop_address
        
        self.user_sessions[phone_number]['state'] = 'waiting_special_instructions'
        message = f"Drop Address saved: {drop_address} 🏠\n\n📝 Any Special Instructions? (Type your instructions or 'none' if no special requests):"
        self.send_message(phone_number, message)
        return message
    
    def handle_special_instructions(self, phone_number, instructions):
        booking_data = self.user_sessions[phone_number]['booking_data']
        booking_data['special_instructions'] = instructions if instructions.lower() != 'none' else 'No special instructions'
        
        # Generate appointment confirmation
        return self.send_appointment_confirmation(phone_number)

    def send_appointment_confirmation(self, phone_number):
        session = self.user_sessions[phone_number]
        booking_data = session['booking_data']
        
        # Reset state
        session['state'] = 'initial'
        
        # Get vehicle info from session
        vehicle_reg = session.get('vehicle_reg', 'N/A')
        service_type = 'General Service' # You can make this dynamic later
        
        # Build address section based on service preference
        address_section = ""
        if booking_data['service_preference'] == 'Drive In':
            address_section = f"🏢 **Service Location:** Sherpa Service Center\n"
        elif booking_data['service_preference'] == 'Pickup Only':
            address_section = f"📍 **Pickup Address:** {booking_data['pickup_address']}\n🏢 **Service Location:** Sherpa Service Center\n"
        elif booking_data['service_preference'] == 'Pickup and Drop':
            address_section = f"📍 **Pickup Address:** {booking_data['pickup_address']}\n🏠 **Drop Address:** {booking_data['drop_address']}\n🏢 **Service Location:** Sherpa Service Center\n"
        
        # Format the auto-captured WhatsApp phone for display
        raw_phone = str(booking_data.get('customer_phone', str(phone_number)))
        if raw_phone.startswith('91') and len(raw_phone) == 12:
            display_phone = f'+91-{raw_phone[2:]}'
        elif len(raw_phone) == 10 and raw_phone.isdigit():
            display_phone = f'+91-{raw_phone}'
        else:
            display_phone = raw_phone

        confirmation_message = (
            "🎉 **APPOINTMENT CONFIRMATION**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 **Full Name:** {booking_data['full_name']}\n"
            f"📱 **Phone Number:** {display_phone}\n"
            f"🚗 **Vehicle Number:** {vehicle_reg}\n"
            f"🔧 **Service Type:** {service_type}\n"
            f"📅 **Appointment Date:** {booking_data['appointment_date']}\n"
            f"⏰ **Timing:** {booking_data['timing']}\n"
            f"🚙 **Service Preference:** {booking_data['service_preference']}\n"
            f"{address_section}"
            f"💰 **Estimated Cost:** To be determined after inspection\n"
            f"📝 **Special Instructions:** {booking_data['special_instructions']}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📞 For any changes, call: 1800-xxxx-xxx\n"
            "Thank you for choosing our service! 🙏"
        )
        
        self.send_message(phone_number, confirmation_message)
        
        # ADD BACK/END BUTTONS AFTER CONFIRMATION
        self.send_back_end_buttons(phone_number)
        
        # Optional: Store booking in database
        self.store_booking_in_database(phone_number, booking_data)
        
        return confirmation_message

    def store_booking_in_database(self, phone_number, booking_data):
        """Store booking details in database"""
        try:
            conn = self.db.get_connection()
            if conn:
                cursor = conn.cursor()
                
                # Prepare address data based on service preference
                pickup_address = booking_data.get('pickup_address', 'N/A')
                drop_address = booking_data.get('drop_address', 'N/A')
                
                query = """
                    INSERT INTO appointment_bookings 
                    (phone_number, full_name, customer_phone, vehicle_reg, appointment_date, 
                    timing, pickup_address, drop_address, service_preference, special_instructions, booking_timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                session = self.user_sessions[phone_number]
                cursor.execute(query, (
                    phone_number,
                    booking_data['full_name'],
                    booking_data['customer_phone'], 
                    session.get('vehicle_reg'),
                    booking_data['appointment_date'],
                    booking_data['timing'],
                    pickup_address,
                    drop_address,
                    booking_data['service_preference'],
                    booking_data['special_instructions'],
                    datetime.now()
                ))
                conn.commit()
                logger.info(f"Booking stored for {phone_number}")
        except Exception as e:
            logger.error(f"Error storing booking: {e}")
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()

    # ============ DISCOUNTS & OFFERS ============

    def send_discounts_offer(self, phone_number):
        # Initialize discounts session data
        if 'discounts_data' not in self.user_sessions[phone_number]:
            self.user_sessions[phone_number]['discounts_data'] = {}
        
        self.user_sessions[phone_number]['state'] = 'discounts_book_service'
        
        message = (
            "🏷️ **CURRENT OFFERS & DISCOUNTS**\n\n"
            "🎉 **Ongoing Offers:**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔧 **10% OFF** on periodic maintenance\n"
            "🛞 **Special prices** on genuine spare parts\n"
            "🚗 **FREE car wash** with major services\n"
            "🛡️ **Extended warranty** at discounted rates\n"
            "⚡ **Express service** completed in under 2 hours\n"
            "🎁 **Complimentary** vehicle health check\n"
            "💳 **Cashback offers** on digital payments\n"
            "🏆 **Premium service packages** at reduced rates\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💰 **Save up to ₹5000** on comprehensive services!\n\n"
            "Do you want to book the service to avail these offers?"
        )
        
        buttons = [
            {"type": "reply", "reply": {"id": "discounts_yes", "title": "✅ Yes, Book Now"}},
            {"type": "reply", "reply": {"id": "discounts_no", "title": "❌ No, Thanks"}}
        ]
        
        self.send_message(phone_number, message, buttons=buttons)
        return message
        
    def handle_discounts_book_service(self, phone_number, response):
        discounts_data = self.user_sessions[phone_number]['discounts_data']
        discounts_data['book_response'] = response
        
        if response == 'discounts_yes':
            # User wants to book service
            self.user_sessions[phone_number]['state'] = 'initial'  # Reset for booking flow
            
            # Trigger book service appointment
            return self.book_service_appointment(phone_number)
            
        elif response == 'discounts_no':
            # User doesn't want to book, ask for followup
            self.user_sessions[phone_number]['state'] = 'discounts_followup_request'
            
            message = "Thank you for checking our offers! 🙏\n\nDo you want us to follow up later when you're ready for service?"
            
            buttons = [
                {"type": "reply", "reply": {"id": "followup_yes", "title": "✅ Yes, Follow Up"}},
                {"type": "reply", "reply": {"id": "followup_no", "title": "❌ No, Thanks"}}
            ]
            
            self.send_message(phone_number, message, buttons=buttons)
            return message
        
        else:
            return self.handle_ai_query(phone_number, response)

    def handle_discounts_followup_request(self, phone_number, response):
        discounts_data = self.user_sessions[phone_number]['discounts_data']
        discounts_data['followup_response'] = response
        
        if response == 'followup_yes':
            # User wants followup, ask for timing
            self.user_sessions[phone_number]['state'] = 'discounts_followup_timing'
            
            message = "Great! When would you like us to follow up with you about our service offers?"
            
            list_sections = [{
                "title": "Follow-up Timeline",
                "rows": [
                    {
                        "id": "followup_next_week",
                        "title": "Next week",
                        "description": "Follow up in 1 week"
                    },
                    {
                        "id": "followup_15_30",
                        "title": "15 - 30 Days",
                        "description": "Follow up in 2-4 weeks"
                    },
                    {
                        "id": "followup_30_45",
                        "title": "30 - 45 Days", 
                        "description": "Follow up in 1-1.5 months"
                    },
                    {
                        "id": "followup_45_60",
                        "title": "45-60 Days",
                        "description": "Follow up in 1.5-2 months"
                    }
                ]
            }]
            
            self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
            return message
            
        elif response == 'followup_no':
            # User doesn't want followup
            self.user_sessions[phone_number]['state'] = 'initial'
            
            message = (
                "🙏 **Thank you for your time!**\n\n"
                "We appreciate you checking out our current offers and discounts. "
                "Feel free to contact us anytime when you're ready for service.\n\n"
                "📞 **Call us:** 1800-xxxx-xxx\n"
                "💬 **WhatsApp:** Just say 'Hi' to restart\n\n"
                "🚗 We're always here to take care of your vehicle needs!\n"
                "Have a wonderful day! 😊"
            )
            
            self.send_message(phone_number, message)
            
            # Add Back/End buttons
            self.send_back_end_buttons(phone_number)
            
            return message
        
        else:
            return self.handle_ai_query(phone_number, response)

    def handle_discounts_followup_timing(self, phone_number, timing):
        discounts_data = self.user_sessions[phone_number]['discounts_data']
        discounts_data['followup_timing'] = timing
        
        # Reset state
        self.user_sessions[phone_number]['state'] = 'initial'
        
        # Determine timing text
        timing_text = ""
        if timing == 'followup_next_week':
            timing_text = "next week"
        elif timing == 'followup_15_30':
            timing_text = "in 15-30 days"
        elif timing == 'followup_30_45':
            timing_text = "in 30-45 days"  
        elif timing == 'followup_45_60':
            timing_text = "in 45-60 days"
        
        message = (
            f"🎉 **Follow-up Scheduled!**\n\n"
            f"✅ We will contact you **{timing_text}** with our latest offers and service reminders.\n\n"
            f"📅 **Follow-up Timeline:** {timing_text.title()}\n"
            f"📞 **Our team will reach you at:** {phone_number}\n"
            f"💰 **You'll get exclusive offers** and priority booking slots!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🙏 **Thank you for choosing Sherpa Service Center!**\n\n"
            f"In the meantime, feel free to contact us anytime:\n"
            f"📞 Call: 1800-xxxx-xxx\n"
            f"💬 WhatsApp: Just say 'Hi'\n\n"
            f"🚗 We look forward to serving you soon!"
        )
        
        self.send_message(phone_number, message)
        
        # Add Back/End buttons
        self.send_back_end_buttons(phone_number)
        
        # Store followup request in database
        self.store_followup_request(phone_number, discounts_data)
        
        return message

    def store_followup_request(self, phone_number, discounts_data):
        """Store followup request in database"""
        try:
            conn = self.db.get_connection()
            if conn:
                cursor = conn.cursor()
                
                # Determine timing for database
                timing_map = {
                    'followup_next_week': 'Next week',
                    'followup_15_30': '15-30 Days',
                    'followup_30_45': '30-45 Days', 
                    'followup_45_60': '45-60 Days'
                }
                
                followup_timing = timing_map.get(discounts_data.get('followup_timing'), 'Unknown')
                
                query = """
                    INSERT INTO service_requests 
                    (phone_number, request_type, scheduled_date, scheduled_time, status, created_at, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(query, (
                    phone_number,
                    'followup',
                    followup_timing,
                    'As per customer preference',
                    'pending',
                    datetime.now(),
                    'Customer requested followup for discounts and offers'
                ))
                conn.commit()
                logger.info(f"Followup request stored for {phone_number}")
        except Exception as e:
            logger.error(f"Error storing followup request: {e}")
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()
                
    # ============ VEHICLE NOT FOUND WORKFLOW ============

    def start_vehicle_info_collection(self, phone_number, service_type):
        """Start collecting vehicle information for Free Service or Paid Maintenance"""
        session = self.user_sessions[phone_number]
        session['state'] = 'waiting_city_input'
        session['vehicle_info'] = {'service_type': service_type}
        
        message = f"🏙️ **VEHICLE INFORMATION COLLECTION**\n\n📍 **For {service_type}**\n\nCould you please tell me which city you're located in?\n\n*Example: Bangalore, Mumbai, Delhi*"
        
        return self.send_message(phone_number, message)

    def handle_city_input(self, phone_number, city):
        """Handle city input"""
        session = self.user_sessions[phone_number]
        session['vehicle_info']['city'] = city.strip()
        session['state'] = 'waiting_vehicle_model_input'
        
        message = f"🚗 **VEHICLE MODEL**\n\nThank you! Now please tell me your vehicle model.\n\n*Example: Maruti Swift, Honda City, Hyundai Creta*"
        
        return self.send_message(phone_number, message)

    def handle_vehicle_model_input(self, phone_number, model):
        """Handle vehicle model input"""
        session = self.user_sessions[phone_number]
        session['vehicle_info']['model'] = model.strip()
        session['state'] = 'waiting_odo_reading_input'
        
        message = f"🔢 **CURRENT ODO READING**\n\nPlease share your current odometer reading (in kilometers).\n\n*Example: 25000 km, 45,000, 1,20,000*"
        
        return self.send_message(phone_number, message)

    def handle_odo_reading_input(self, phone_number, odo):
        """Handle ODO reading input"""
        session = self.user_sessions[phone_number]
        session['vehicle_info']['odo_reading'] = odo.strip()
        session['state'] = 'waiting_fuel_type_input'
        
        message = f"⛽ **FUEL TYPE**\n\nWhat type of fuel does your vehicle use?\n\n*Example: Petrol, Diesel, CNG, Electric*"
        
        return self.send_message(phone_number, message)

    def handle_fuel_type_input(self, phone_number, fuel_type):
        """Handle fuel type input"""
        session = self.user_sessions[phone_number]
        session['vehicle_info']['fuel_type'] = fuel_type.strip()
        session['state'] = 'waiting_last_service_selection'
        logger.info(f"⛽ Fuel type saved: {fuel_type}")
        message = f"🔧 **LAST SERVICE**\n\nWhen was your last service?"
        
        list_sections = [{
            "title": "Choose Last Service Time",
            "rows": [
                {"id": "last_service_3months", "title": "Within 3 months"},
                {"id": "last_service_3to6months", "title": "3-6 months ago"}, 
                {"id": "last_service_6to12months", "title": "6-12 months ago"},
                {"id": "last_service_1year_plus", "title": "1+ year ago"}
            ]
        }]
        
        return self.send_message(phone_number, message, list_message=list_sections)

    def handle_last_service_selection(self, phone_number, selection):
        """Handle last service time selection"""
        session = self.user_sessions[phone_number]
        
        service_time_map = {
            'last_service_3months': 'Within 3 months',
            'last_service_3to6months': '3-6 months ago',
            'last_service_6to12months': '6-12 months ago', 
            'last_service_1year_plus': 'More than 1 year ago / Don\'t remember',
            # Add number-based fallbacks
            '1': 'Within 3 months',
            '2': '3-6 months ago',
            '3': '6-12 months ago',
            '4': 'More than 1 year ago / Don\'t remember'
        }
        
        if selection in service_time_map:
            session['vehicle_info']['last_service'] = service_time_map[selection]
            logger.info(f"🔧 Last service saved: {service_time_map[selection]}")
            return self.show_vehicle_info_confirmation(phone_number)
        else:
            # Show helpful message with both list and number options
            message = (
                "🔧 **LAST SERVICE**\n\n"
                "Please select when was your last service:\n\n"
                "📋 **Available options:**\n"
                "1. Within 3 months\n"
                "2. 3-6 months ago\n" 
                "3. 6-12 months ago\n"
                "4. More than 1 year ago / Don't remember\n\n"
                "💬 **Reply with the number (1, 2, 3...) of your choice**"
            )
            
            list_sections = [{
                "title": "Choose Last Service Time",
                "rows": [
                    {"id": "last_service_3months", "title": "Within 3 months"},
                    {"id": "last_service_3to6months", "title": "3-6 months ago"}, 
                    {"id": "last_service_6to12months", "title": "6-12 months ago"},
                    {"id": "last_service_1year_plus", "title": "1+ year ago"}
                ]
            }]
            
            return self.send_message(phone_number, message, list_message=list_sections)

    def show_vehicle_info_confirmation(self, phone_number):
        """Show confirmation of collected vehicle information"""
        session = self.user_sessions[phone_number]
        session['state'] = 'vehicle_info_confirmation'
        vehicle_info = session['vehicle_info']
        
        message = f"✅ **CONFIRMATION**\n────────────────────────────\n\n"
        message += f"🏙️ **City:** {vehicle_info['city']}\n"
        message += f"🚗 **Vehicle Model:** {vehicle_info['model']}\n" 
        message += f"🔢 **Current ODO:** {vehicle_info['odo_reading']}\n"
        message += f"⛽ **Fuel Type:** {vehicle_info['fuel_type']}\n"
        message += f"🔧 **Last Service:** {vehicle_info['last_service']}\n\n"
        message += f"🙏 **Thank you for providing your vehicle details!**\n\n"
        message += f"📞 **Our service advisor will call you within the next 2 hours** to discuss your {vehicle_info['service_type']} requirements and provide detailed information.\n\n"
        message += f"We appreciate your trust in our services! 🚗✨"
        
        buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": "book_service_now",
                    "title": "📅 Book Service"
                }
            },
            {
                "type": "reply", 
                "reply": {
                    "id": "back_to_main",
                    "title": "🔙 Back to Menu"
                }
            }
        ]
        
        # Store the service request in database
        self.store_new_vehicle_service_request(phone_number, vehicle_info)
        
        return self.send_message(phone_number, message, buttons=buttons)



    def handle_other_service_workflow(self, phone_number):
        """Handle 'Other' service selection - same as Different Service workflow"""
        session = self.user_sessions[phone_number] 
        session['state'] = 'waiting_other_service_selection'
        
        logger.info("🔧 Starting Other service workflow")
        
        message = "🔧 **SERVICE ESTIMATE**\n\nPlease select the service type you need an estimate for:\n\n*Tap to choose from list*"
        
        list_sections = [{
            "title": "Please select below",
            "rows": [
                {"id": "service_accidental", "title": "Accidental/Bodywork", "description": "Accident damage and body repairs"},
                {"id": "service_running_repair", "title": "Running Repair", "description": "General mechanical repairs"},
                {"id": "service_tire_wheel", "title": "Tire & Wheel Service", "description": "Tire replacement and wheel services"},
                {"id": "service_battery_electrical", "title": "Battery & Electrical", "description": "Battery and electrical system repairs"}
            ]
        }]
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message
        # return self.send_message(phone_number, message, list_message=list_sections)

    def handle_other_service_selection(self, phone_number, selection):
        """Handle Other service type selection - connect to existing estimate flow"""
        session = self.user_sessions[phone_number]
        
        service_map = {
            'service_accidental': 'Accidental/Bodywork',
            'service_running_repair': 'Running Repair', 
            'service_tire_wheel': 'Tire & Wheel Service',
            'service_battery_electrical': 'Battery & Electrical'
        }
        
        if selection in service_map:
            service_type = service_map[selection]
            session['selected_service_type'] = service_type
            logger.info(f"🔧 Other service selected: {service_type}")
            
            # Connect to existing estimate service type selection workflow
            # This is the same as "Different Service" option
            session['state'] = 'estimate_service_type_selection'
            session['service_type'] = service_type
            return self.handle_estimate_service_type_selection(phone_number, service_type)
        else:
            message = "Please select one of the service options above."
            return self.send_message(phone_number, message)

    def handle_vehicle_info_confirmation(self, phone_number, message):
        """Handle actions from vehicle info confirmation screen"""
        if message == 'book_service_now':
            return self.book_service_appointment(phone_number)
        elif message == 'back_to_main':
            return self.handle_back_to_main(phone_number)
        else:
            # Before calling AI, check if it's a genuine question
            from utils import is_genuine_query
            state = self.user_sessions.get(phone_number, {}).get('state', '')
            if is_genuine_query(message, state):
                return self.handle_ai_query(phone_number, message)
            else:
                # Not a genuine query - re-show the confirmation screen
                return self.handle_back_to_main(phone_number)

    def store_new_vehicle_service_request(self, phone_number, vehicle_info):
        """Store service request for new vehicle in database"""
        try:
            conn = self.db.get_connection()
            if conn:
                cursor = conn.cursor()
                
                # Insert into a new table for new vehicle service requests
                query = """
                INSERT INTO new_vehicle_service_requests 
                (phone_number, vehicle_reg, city, vehicle_model, odo_reading, fuel_type, 
                last_service, service_type, request_timestamp, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                cursor.execute(query, (
                    phone_number,
                    self.user_sessions[phone_number].get('vehicle_reg'),
                    vehicle_info['city'],
                    vehicle_info['model'], 
                    vehicle_info['odo_reading'],
                    vehicle_info['fuel_type'],
                    vehicle_info['last_service'],
                    vehicle_info['service_type'],
                    datetime.now(),
                    'pending'
                ))
                
                conn.commit()
                cursor.close()
                conn.close()
                logger.info(f"Stored new vehicle service request for {phone_number}")
                
        except Exception as e:
            logger.error(f"Error storing new vehicle service request: {e}")

    def _service_route_via_ai(self, phone, text, state):
        """
        Routes a mid-flow genuine query to the AI and then re-prompts the current state.
        """
        logger.info(f"Routing mid-flow query to AI: '{text}' in state: {state}")
        
        # 1. Generate AI response (this uses handle_service_ai_query inside handle_ai_query)
        self.handle_ai_query(phone, text)
        
        # 2. Re-prompt the user for the current state to continue the flow
        self._reprompt_service_state(phone)

    def _reprompt_service_state(self, phone):
        """
        Re-prompts the user based on their current session state.
        """
        session = self.user_sessions.get(phone, {})
        state = session.get('state', 'initial')
        
        if state == 'initial' or state == 'waiting_for_registration':
            self.handle_greeting(phone)
        elif state == 'vehicle_found':
            self.handle_vehicle_found(phone, session.get('vehicle_data'))
        elif state == 'vehicle_not_found':
            self.handle_vehicle_not_found(phone, session.get('vehicle_reg', 'Unknown'))
        elif state == 'booking_priority':
            self.book_service_appointment(phone)
        elif state == 'booking_slots':
            # This might need a specialized sender if not already defined as a method
            if hasattr(self, 'send_booking_slots_message'):
                self.send_booking_slots_message(phone)
            else:
                self.book_service_appointment(phone)
        elif state == 'waiting_full_name':
            self.send_message(phone, "Please provide your full name to continue your booking:")
        elif state == 'waiting_phone_number':
            self.send_message(phone, "Please provide your contact phone number:")
        elif state == 'waiting_pickup_address':
            self.send_message(phone, "Please share your pickup address:")
        elif state == 'waiting_drop_address_selection':
            # Add logic if needed
            self.send_message(phone, "Please select the drop-off location.")
        elif state == 'waiting_drop_address_input':
            self.send_message(phone, "Please provide the drop-off address:")
        elif state == 'booking_preferences':
            self.send_message(phone, "Please select your service preferences.")
        elif state == 'waiting_special_instructions':
            self.send_message(phone, "Any special instructions or concerns for our service team?")
        elif state == 'contact_method_selection':
            self.contact_service_advisor(phone)
        elif state == 'callback_priority':
            self.start_callback_scheduling(phone)
        elif state == 'callback_slots':
            self.send_message(phone, "Please choose your preferred time slot for the callback.")
        elif state == 'estimate_service_type_selection':
            self.handle_vehicle_not_found_estimate(phone)
        elif state == 'estimate_image_upload_confirmation':
            self.send_message(phone, "Would you like to upload images for your estimate?")
        elif state == 'estimate_image_upload_waiting' or state == 'estimate_image_confirm_ready':
            self.send_message(phone, "Please upload vehicle images or click 'Confirm' if done.")
        elif state == 'SERVICE_AGENT_CONFIRMATION':
            self.trigger_human_agent_confirmation(phone)
        else:
            self.send_message(phone, "How would you like to continue?")
            self.send_interactive_main_menu(phone)

    def handle_ai_query(self, phone_number, query):
        """Enhanced AI handler with smart intent detection and contextual responses"""
        
    # STEP 1: Check if customer is already connected to live agent
        if self.is_customer_with_live_agent(phone_number):
            # Route message to live agent, don't process with AI
            return self.route_to_live_agent(phone_number, query)
        
        # STEP 2: Detect if customer needs human agent
        if self.should_connect_to_human_agent(query):
            return self.offer_human_agent_connection(phone_number, query)
        
        # ---- Your existing AI logic resumes here ----
        # SAFETY CHECK - if somehow end_conversation reaches here, redirect
        if query == 'end_conversation':
            return self.handle_end_conversation(phone_number)
            
        session = self.user_sessions[phone_number]
        vehicle_data = session.get('vehicle_data', {})
        vehicle_reg = session.get('vehicle_reg')
        
        # SMART INTENT DETECTION - Check what user is asking for
        query_lower = query.lower()
        
        # PRIORITY 1: Check if user is providing vehicle registration directly (STRICT CHECK)
        if self.is_vehicle_registration(query):
            self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
            return self.handle_registration_input(phone_number, query)
        
        # PRIORITY 2: User wants to book appointment - ALWAYS CHECK REGISTRATION FIRST
        if any(word in query_lower for word in ['book appointment', 'book service', 'schedule service', 'appointment', 'booking', 'i want to book', 'want to book', 'book my car', 'service for my', 'book service for', 'service appointment']):
            
            # CRITICAL: ALWAYS ask for registration first, regardless of session state
            if not vehicle_reg or not vehicle_data:
                # Clear any existing session data to ensure clean state
                self.user_sessions[phone_number]['vehicle_reg'] = None
                self.user_sessions[phone_number]['vehicle_data'] = None
                self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
                
                message = (
                    "To get started and provide you with the most relevant service options, please enter your Vehicle Registration Number below:\n\n"
                    "Example: KA01AB1234"
                )
                self.send_message(phone_number, message)
                return message
            else:
                # Registration exists and found in database - proceed with booking
                return self.book_service_appointment(phone_number)
        
        # PRIORITY 3: User wants service history
        if any(word in query_lower for word in ['service history', 'history', 'past service', 'previous service', 'service record']):
            if not vehicle_reg:
                message = (
                    "📋 To view your service history, I need your vehicle registration number first.\n\n"
                    "Please enter your Vehicle Registration Number (example: KA01AB1234):"
                )
                self.user_sessions[phone_number]['state'] = 'waiting_for_registration'
                self.send_message(phone_number, message)
                return message
            else:
                return self.get_service_history(phone_number)
        
        # PRIORITY 4: User wants service estimate
        if any(word in query_lower for word in ['estimate', 'cost', 'price', 'pricing', 'how much']):
            return self.get_service_estimate(phone_number)
        
        # PRIORITY 5: User wants to contact advisor - only explicit phrases
        if any(word in query_lower for word in ['contact advisor', 'speak to advisor', 'call advisor', 'contact service advisor']):
            return self.contact_service_advisor(phone_number)
        
        # PRIORITY 6: User wants offers/discounts
        if any(word in query_lower for word in ['offer', 'discount', 'deal', 'promotion']):
            return self.send_discounts_offer(phone_number)
        
        # PRIORITY 7: User wants menu
        if any(word in query_lower for word in ['menu', 'options', 'help', 'what can you do']):
            return self.send_interactive_main_menu(phone_number)
        
        # PRIORITY 8: Handle date and time inputs for appointment booking
        if self.detect_date_time_intent(query_lower):
            return self.handle_date_time_input(phone_number, query)
        
        # PRIORITY 9: Handle thank you responses
        if any(word in query_lower for word in ['thank you', 'thanks', 'thx']):
            return self.handle_thank_you_response(phone_number)
        
        # PRIORITY 10: Handle "no thank you" responses
        if any(phrase in query_lower for phrase in ['no thank you', 'no thanks', 'not now']):
            return self.handle_no_thank_you_response(phone_number)
        
        # If no specific intent detected, only use AI if it's a genuine question.
        # If the user typed a vehicle reg number, name, date, or other structured
        # input that wasn't caught by the priority chain above, re-prompt them
        # rather than calling the AI which would produce an irrelevant response.
        from utils import is_genuine_query
        session = self.user_sessions.get(phone_number, {})
        current_state = session.get('state', '')
        if is_genuine_query(query, current_state):
            return self.generate_smart_ai_response(phone_number, query)
        else:
            logger.info(f"[service] Blocked non-query input from AI | state={current_state} | query='{query}'")
            # Re-show the main service menu so the user is never stuck
            return self.send_interactive_main_menu(phone_number)

    def detect_date_time_intent(self, query_lower):
        """Detect if user is providing date and time information"""
        date_keywords = ['date', 'day', 'tomorrow', 'today', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        time_keywords = ['time', 'timing', 'am', 'pm', 'morning', 'afternoon', 'evening', 'o\'clock']
        month_keywords = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec', 'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
        
        has_date = any(keyword in query_lower for keyword in date_keywords + month_keywords)
        has_time = any(keyword in query_lower for keyword in time_keywords)
        has_numbers = any(char.isdigit() for char in query_lower)
        
        return (has_date or has_time) and has_numbers
    

# not found in db--------------
    def handle_service_type_selection(self, phone_number, selection):
        """Handle Free Service/Paid Maintenance/Other selection"""
        session = self.user_sessions[phone_number]
        
        if selection == 'service_type_free':
            session['selected_service_type'] = 'Free Service'
            return self.start_vehicle_info_collection(phone_number, 'Free Service')
            
        elif selection == 'service_type_paid':
            session['selected_service_type'] = 'Paid Maintenance' 
            return self.start_vehicle_info_collection(phone_number, 'Paid Maintenance')
            
        elif selection == 'service_type_other':
            return self.handle_other_service_workflow(phone_number)
            
        else:
            # Invalid selection
            message = "Please select one of the service type options above."
            return self.send_message(phone_number, message)

    def handle_date_time_input(self, phone_number, query):
        """Handle when user provides date and time for appointment"""
        message = (
            f"I understand you'd like to book an appointment with the details: '{query}' 📅\n\n"
            "Let me help you book this properly through our booking system for the best experience!"
        )
        self.send_message(phone_number, message)
        # Start the proper booking flow
        return self.book_service_appointment(phone_number)

    def handle_thank_you_response(self, phone_number):
        """Handle thank you responses appropriately"""
        session = self.user_sessions[phone_number]
        vehicle_reg = session.get('vehicle_reg')
        
        if vehicle_reg:
            # Send the welcome message first
            message = "You're welcome! 😊 Is there anything else I can help you with regarding your vehicle service today?"
            self.send_message(phone_number, message)
            
            # Then send the interactive menu
            self.send_interactive_main_menu(phone_number)
        else:
            # For users without vehicle registration
            message = (
                "You're welcome! 😊\n\n"
                "Feel free to ask me anything about vehicle service, or I can help you get started by entering your vehicle registration number."
            )
            self.send_message(phone_number, message)
            
            # Offer simple options
            buttons = [
                {"type": "reply", "reply": {"id": "back_to_main", "title": "🏠 Main Menu"}},
                {"type": "reply", "reply": {"id": "end_conversation", "title": "🔚 End Chat"}}
            ]
            followup_message = "How can I help you today?"
            self.send_message(phone_number, followup_message, buttons=buttons)
        
        return message

    def handle_no_thank_you_response(self, phone_number):
        """Handle 'no thank you' responses appropriately"""
        
        # Reset state to avoid wrong context
        session = self.user_sessions[phone_number]
        session['state'] = 'initial'
        
        # Clear any booking or other session data
        if 'booking_data' in session:
            del session['booking_data']
        
        # Send the main response message first
        message = (
            "No problem at all! 😊\n\n"
            "I'm here whenever you need assistance with your vehicle service. "
            "Feel free to reach out anytime!"
        )
        self.send_message(phone_number, message)
        
        # Then offer gentle options without being pushy
        buttons = [
            {"type": "reply", "reply": {"id": "back_to_main", "title": "🏠 Main Menu"}},
            {"type": "reply", "reply": {"id": "end_conversation", "title": "🔚 End Chat"}}
        ]
        
        followup_message = "If you change your mind, I'm here to help!"
        self.send_message(phone_number, followup_message, buttons=buttons)
        
        return message

    
    def send_interactive_main_menu(self, phone_number):
        """Send interactive main menu instead of plain text"""
        session = self.user_sessions[phone_number]
        vehicle_reg = session.get('vehicle_reg')
        
        if vehicle_reg:
            # Vehicle is registered, show full menu with polite message
            message = "Here are the ways I can assist you with your vehicle today:"
            list_sections = [{
                "title": "Service Options",
                "rows": [
                    {"id": "service_history", "title": "📋 Service History", "description": "View your complete service records"},
                    {"id": "book_appointment", "title": "🛠️ Book Appointment", "description": "Schedule your next service visit"},
                    {"id": "service_estimate", "title": "💰 Get Estimate", "description": "Service pricing information"},
                    {"id": "contact_advisor", "title": "📞 Contact Advisor", "description": "Speak with our service experts"},
                    {"id": "discounts_offer", "title": "🏷️ Offers & Discounts", "description": "View current promotions"}
                ]
            }]
        else:
            # No vehicle registered, limited menu
            message = "Welcome! Here's how I can assist you:"
            list_sections = [{
                "title": "Getting Started",
                "rows": [
                    {"id": "service_estimate", "title": "💰 Get Estimate", "description": "Service pricing information"},
                    {"id": "book_appointment", "title": "🛠️ Book Appointment", "description": "Schedule your service"},
                    {"id": "contact_advisor", "title": "📞 Contact Advisor", "description": "Speak with our experts"},
                    {"id": "re_enter_reg","title": "Re-Enter Registration","description": "Try entering your registration again"},
                    {"id": "new_customer_benefits", "title": "🎁 New Customer Benefits", "description": "Special offers for first-time customers"}
                ]
            }]
        
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)
        return message

    # ============ AI RESPONSE GENERATION ============

    def generate_smart_ai_response(self, phone_number, query):
        """
        Smart AI Agent — uses ai.py handle_service_ai_query for DB-aware responses.
        Falls back gracefully if Groq is unavailable.

        GATE: Only responds with AI-generated content if the query is a genuine
        question. Non-question inputs (reg numbers, names, dates, etc.) are
        re-routed to the main menu instead of producing irrelevant AI responses.
        """
        session = self.user_sessions[phone_number]
        vehicle_data = session.get('vehicle_data', {})
        vehicle_reg = session.get('vehicle_reg')
        q = (query or '').strip()
        q_lower = q.lower()

        logger.info(f"Smart AI processing query: '{q}' for {phone_number}")

        # ── Final gate: ensure this is actually a question ───────────────
        from utils import is_genuine_query
        current_state = session.get('state', '')
        if not is_genuine_query(q, current_state):
            logger.info(f"[service] generate_smart_ai_response: blocked non-query | state={current_state} | q='{q}'")
            return self.send_interactive_main_menu(phone_number)

        # Live agent check BEFORE calling AI
        if self.should_connect_to_human_agent(query):
            logger.info(f"User requested live agent for query: '{q}'")
            return self.offer_human_agent_connection(phone_number, query)

        try:
            # Use DB-aware AI from ai.py — fetches real service records from DB
            # Use DB-aware AI from ai.py — fetches real service records from DB
            from ai import handle_service_ai_query
            ai_response, suggested_action = handle_service_ai_query(
                phone=phone_number,
                user_text=q,
                vehicle_reg=vehicle_reg
            )

            logger.info(f"AI generated response: '{ai_response[:100]}...' | Suggested Action: {suggested_action}")

            # ── PRIORITY 1: Check if AI suggests a live agent (TALK_TO_ADVISOR) ──
            if suggested_action == "TALK_TO_ADVISOR":
                logger.info(f"AI suggested live agent for query: '{q}'")
                return self.offer_human_agent_connection(phone_number, q)

            # ── PRIORITY 2: Check if AI failed to provide a good answer ──
            if self.detect_ai_failure(ai_response):
                logger.info(f"AI failure detected for query: '{q}'")
                enhanced_response = (
                    ai_response + "\n\n"
                    "I'm sorry I couldn't find the exact answer.\n\n"
                    "Would you like me to connect you with our live service advisor?"
                )
                buttons = [
                    {"type": "reply", "reply": {"id": "connect_live_agent_yes", "title": "Yes, Connect Agent"}},
                    {"type": "reply", "reply": {"id": "connect_live_agent_no",  "title": "No, I'm Good"}}
                ]
                session['state'] = 'waiting_agent_connection_response'
                session['handoff_reason'] = f"AI couldn't properly answer: {q}"
                self.send_message(phone_number, enhanced_response, buttons=buttons)
                return enhanced_response

            # ── PRIORITY 3: Handle other follow-on actions ──
            if suggested_action == "BOOK_APPOINTMENT":
                self.send_message(phone_number, ai_response)
                return self.book_service_appointment(phone_number)
            
            elif suggested_action == "GET_ESTIMATE":
                self.send_message(phone_number, ai_response)
                return self.get_service_estimate(phone_number)
            
            elif suggested_action == "SERVICE_HISTORY":
                self.send_message(phone_number, ai_response)
                return self.get_service_history(phone_number)

            # Default: Enhance response with contextual quick actions
            enhanced_response = self.enhance_ai_response_with_actions(ai_response, q_lower, vehicle_reg, vehicle_data)
            self.send_message(phone_number, enhanced_response)
            return enhanced_response

        except Exception as e:
            logger.error(f"AI generation error in generate_smart_ai_response: {e}")
            # Graceful fallback — helpful car service info instead of error message
            fallback = (
                "I'm here to help with your car service needs!\n\n"
                "I can assist you with:\n"
                "- Book a service appointment\n"
                "- View your service history\n"
                "- Get a service estimate\n"
                "- Contact a service advisor\n\n"
                "Please type *Hi* to explore all options."
            )
            self.send_message(phone_number, fallback)
            return fallback




    def build_enhanced_context(self, phone_number, query, vehicle_data, vehicle_reg):
        """
        Enhanced AI context builder with your new professional system prompt
        """
        
        base_instructions = """You are Car Service Bot, a polite, professional virtual assistant for a car service centre. Use the database described below to answer customer queries, book appointments, and provide estimates. Keep replies short, clear and helpful (≤200 words). Never expose PII.

    🎯 **CORE MISSION:**
    - Provide accurate, helpful responses using database information
    - Be conversational, friendly, and professional (not robotic)
    - Keep responses under 200 words but complete and informative
    - Use emojis appropriately: 🚗 🔧 📅 💰 👋 ✅ ❌ 🤔

    📋 **STRICT WORKFLOW RULES:**

    1. **Registration-First Policy:**
    - If no vehicleRegNo provided: "Please provide your vehicle registration (e.g., KA01AB1234)." and STOP
    - Never proceed with vehicle-specific queries without registration
    - ALWAYS ask for registration before any vehicle-related help

    2. **Database-First Responses:**
    - ALWAYS use actual database values for service history, dates, costs, estimates
    - If data missing from DB: "I don't have this information in our records"
    - Show last bill amount when available from robillscube table
    - Use real next service dates from bicoe_forecast_cube table
    - For estimates: Use pmslabour/pmsmodel/pmsmileage join logic

    3. **Smart Communication Style:**
    - Be conversational and natural, NOT robotic
    - Ask smart follow-up questions that move the conversation forward
    - Provide proactive suggestions based on vehicle data
    - Always finish with one clarifying question and the typed CTA line (see Closing Protocol)
    - Show empathy and understanding

    4. **Intent Handling & Workflow:**
    - **Service History**: Show actual last service details with bill amount
    - **Next Service**: Show actual due date and service type from database
    - **Bill Amount**: Display actual billAmt from robillscube table
    - **Booking**: ask user to type ' book service appointment' 
    - **Estimates**: Use database joins for accurate pricing and ask user to type 'get estimate'
    - **Live Agent / Advisor/customer care**: offer escalation and ask the user to type 'live agent' or 'contact advisor'

    5. **Proactive Communication:**
    - Use forecasting to suggest upcoming service
    - Example: "Your last service was on {date} at {odo}km. Next service due {nextServiceDue}. Would you like to book? — Type 'Book Appointment' ,'get estimate'  to proceed."

    6. **Live Agent Escalation:**
    - If AI can't answer properly: "Would you like me to connect you with our live service advisor?"
    - ask user just to Type live agent - if you want me to connect you with live agent
    - For complex technical issues, billing disputes, emergencies - always escalate
    - Use explicit buttons/options and log handoff reason

    🗣️ **COMMUNICATION EXAMPLES:**

    User: "Hi"
    Bot: "👋 Hello! Welcome to our service center. I'm here to help with all your car service needs. Please provide your vehicle registration number (e.g., KA01AB1234) so I can assist you better."

    User: "KA01AB1234"
    Bot: "🚗 Perfect! I found your Swift VXI in our system. Your last service was on Aug 12, 2025 at 43,200km (₹4,350 paid). Your next service is due Nov 15, 2025 for 45k PMS. How can I help you today?"

    User: “When is my next service?”
    Bot: “📅 Your next service is on Nov 15, 2025 for 45k Periodic Maintenance Service (at 45,000 km). It’s about a month away.
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate”

    User: “What was my last bill amount?”
    Bot: “💰 Your last service bill was ₹4,350 paid on Aug 12, 2025 for Regular Maintenance Service. Would you like a breakdown or to plan your next visit?
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate.”

    User: "Book appointment for tomorrow"
    Bot:  "✅ Happy to help.plz type 'book service appointment' - to book service"

    User: "My car is making strange noises"
    Bot: "🚨 Strange noises can have multiple causes and need a proper diagnosis. Would you like to connect with our service advisor now?
    plz type 'contact advisor' - to connect to advisor, 'live agent' - to get connected with human agent.”

    User: When is my next service?
    Bot: 📅 Your next service is due on Nov 15, 2025, for 45k Periodic Maintenance Service (at 45,000 km). It’s about a month away—shall I book a slot?
    👉 Type 'book service appointment' to book

    User: When was my last service done?
    Bot: 🧾 Your last service was completed on Aug 12, 2025, for Regular Maintenance Service. Would you like to schedule the next one?
    👉 Type 'book service appointment' to book


    User: How often should I service my car?
    Bot: 🔧 Typically every 10,000 km or 6 months, whichever comes first. Based on your vehicle record, you’re due soon - do u want me to book service plz Type book service appointment

    User: What was my last bill amount?
    Bot: 💰 Your last bill was ₹4,350, paid on Aug 12, 2025, for Regular Maintenance Service. Would you like a cost breakdown or estimated next service cost? Type-cost breakdown or get service estimated.

    User: I want to reschedule my service
    Bot: 🔄 Sure! Please type 'book appointment'.

    User: Can I get a home pickup for service?
    Bot: 🚗 Yes! We offer home pickup and drop service in your area. Type 'book appointment' to schedule.


    User: My car is making strange noises
    Bot: 🚨 Strange noises could indicate mechanical issues. Would you like to connect with a service advisor?
    👉 Type 'contact advisor' or 'live agent'.

    User: My AC isn’t cooling properly
    Bot: ❄️ It may need an AC gas refill or filter cleaning. Would you like to book a quick inspection?
    👉 Type 'contact advisor' or 'book appointment'.

    User: My engine light is on
    Bot: ⚠️ That’s a sign of a possible engine or sensor issue. I recommend connecting with our advisor for diagnostics. Type 'live agent' or 'book appointment' or 'live agent' .

    User: Do you use genuine parts?
    Bot: ✅ Yes, all parts used are 100% genuine OEM-certified. Would you like to connect with customer care ? 👉 Type 'contact advisor' or 'live agent'.

    User: I want to talk to someone
    Bot: 🤝 Sure! Type 'live agent' to connect with a service advisor right away.

    User: I want to talk to someone
    Bot: 🤝 Sure! Type 'live agent' to connect with a service advisor right away.

    User: Your system isn’t helping
    Bot: 😔 Sorry to hear that! I can connect you to a live agent. Type 'live agent' to proceed.

    User: Where is your nearest center?
    Bot: 3rd Floor, Srinivasa Industrial Estate, Survey # 19/3, Kanakapura Main Rd, behind RMS International School & PU college, Mango Garden Layout, Konanakunte, Bengaluru, Karnataka 560111.

    User: What are your working hours?
    Bot: 🕒 Our service center operates Mon–Sat, 9:00 AM to 6:00 PM. Want to book a slot?

    User: Can I pay online?
    Bot: 💳 Yes, we support UPI, debit/credit cards, and online wallet payments. You’ll receive a secure payment link once the service is completed. our service advisor will contact you regarding payment. Type 'live agent' to connect with a service advisor right away.

    User: How long will the service take?
    Bot: ⏱️ Regular maintenance takes about 3 hours, while major services can take up to 6 hours. Would you like to book service. 
    👉 Type 'contact advisor' or 'book appointment'.

    User: Can I reschedule my service?
    Bot: 🔄 Yes, you can easily reschedule your existing service booking.
    plz type 'book service appointment' - to book service


    User: Can I get a discount on my next service?
    Bot: 🎁 Sure! We’re currently offering seasonal discounts and loyalty rewards.
    plz type 'discounts&offers' to see current deals and savings.

    User: My AC is not cooling properly.
    Bot: ❄️ It might be due to low refrigerant or a clogged filter. We can inspect and fix it during your next visit.
    plz type 'book service appointment' to book an AC inspection.

    User: Can you send me my last invoice?
    I can confirm your last service for your ZEN ESTILO was on November 17, 2020, and the final bill was ₹3,237.00. While I can't send the full document here for security reasons, our service advisor would be happy to email a copy to you.

    Would you like me to connect you with an advisor to arrange that?
    Type - 'live agent' - to get connected with human agent or 'contact advisor'

    User: I want to talk to a person.
    Bot: 🤝 Sure! I’ll connect you with a live service agent who can assist further.
    plz type 'live agent' to start chatting with our representative.

    User: Can I reschedule my appointment?
    Bot: 🗓️ No problem! Please 
    plz type 'book service appointment' to reschedule your slot easily.

    Bot: 🚨 Yes, we provide 24/7 roadside assistance for breakdowns and towing.
    plz type 'live agent' to connect instantly for help. 

    Bot: 🛠️ That usually happens when brake pads are worn or dusty. We recommend inspection soon.
    plz type 'book service appointment' to schedule a brake check.

    User: Do you provide doorstep service?
    Bot: 🚘 Yes, we offer doorstep service for select maintenance jobs.
    plz type 'book service appointment' - to book service,

    User: Can I track my service progress?
    Bot: no! You can track your service live by contacting our agent
    plz type 'live agent' to view real-time updates.

    User: Can you tell me my vehicle registration details?
    Bot:Of course! According to our records, your vehicle is a Midnight Black MARUTI ZEN LXI - ESTILO, with the registration number KL7BE6793. 🚗
    What would you like to do next?
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate, 'contact advisor' - to connect to advisor, 'live agent' - to get connected with human agent.

    User: My battery died this morning.
    Bot: 🔋 Sorry to hear that! It could be a weak battery or alternator issue. We can send a technician to jump-start or replace it.
    plz type 'book service appointment' to schedule immediate battery help.

    User: Do you provide tire replacement?
    Bot: 🛞 Yes, we offer tire replacement, alignment, and balancing services for all models.
    plz type 'book service appointment' - to book service, or 'live agent' - to get connected with human agent.

    User: What’s the warranty on service?
    Bot: 🧾 All our services come with a 6-month or 5,000 km warranty, whichever is earlier.
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate, 'live agent' - to get connected with human agent, 'discounts&offers'.

    User: Can I cancel my booking?
    User: Can I cancel my booking?
    Bot: ❌ Yes, you can cancel or reschedule up to 2 hours before your appointment.
    plz type 'live agent' or 'contact advisor' if you need help cancelling now

    User: My fuel efficiency has dropped.
    Bot: ⛽ Low mileage can be caused by clogged filters, dirty injectors, or tire pressure. A full checkup can help.
    plz type 'book service appointment' to get a mileage inspection.

    User: Do you provide engine cleaning?
    Bot: 🧽 Yes, we offer both dry and wet engine cleaning depending on your car type.
    plz type 'book service appointment' - to book service or 'live agent' - to get connected with human agent.

    User: My wipers are not working.
    Bot: 🌧️ That could be due to a blown fuse or worn-out motor. We can inspect and replace them quickly.
    plz type 'book service appointment' to schedule wiper repair.

    User: Is there any service package available?
    Bot: 🧰 Yes, we offer annual maintenance packages with discounts on labor and parts.
    plz type 'discounts&offers' to view available packages.

    User: Do you provide a free car wash after service?
    Bot: 🚿 Yes, every periodic maintenance includes a complimentary exterior wash.
    plz type 'book service appointment' to schedule your wash.

    User: Can you pick my car from office instead of home?
    Bot: 🏢 Yes, we can pick your car from any address within service limits.
    plz type 'book service appointment' - to book service.

    User: Do you clean the interior as well?
    Bot: 🧴 Yes, interior vacuuming and dashboard cleaning are part of our detailing service.
    plz type 'book service appointment' - to book service.

    User: My horn isn’t working.
    Bot: 🔊 That may be due to a loose connection or relay issue. We can fix it easily.
    plz type 'book service appointment' to schedule an inspection.

    User: My headlights are dim.
    Bot: 💡 Dim headlights can mean a weak bulb or wiring issue. We’ll check and replace if needed.
    plz type 'book service appointment' to get them inspected.

    User: Do you have any festive offers?
    Bot: 🎊 Yes! Festive discounts are running this month on all periodic services.
    plz type 'discounts&offers' to view current promotions.

    User: What is the cost for oil change?
    Bot: 🛢️ Oil change usually costs between ₹1,000–₹1,500 depending on oil type.
    plz type 'book service appointment' - to book service

    User: Do you fix scratches and dents?
    Bot: 🎨 Yes, we offer body repair, paint touch-up, and dent removal.
    plz type 'book service appointment' - to book service,

    User: Can I drop my car early morning?
    Bot: 🌅 Yes, you can drop your car anytime after 8:00 AM.
    plz type 'book service appointment' to confirm your early slot.

    User: I have a complaint about my last service.
    Bot: 😔 Sorry for the inconvenience. I’ll escalate your complaint immediately.
    plz type 'live agent' to connect with our service manager.

    User: Can you check my car history?
    Bot: 📜 Sure! I can pull your complete service history in seconds.
    plz type 'show my history' to retrieve all past records.

    User: My steering feels hard.
    Bot: 🧭 That may be due to low power steering fluid or pump issue. Needs inspection.
    plz type 'book service appointment' to get it checked.

    User: Do you provide a loaner car?
    Bot: 🚘 Yes, we can arrange a temporary replacement car for extended repairs.
    plz type 'live agent' to check availability and policy.

    User: Do you offer detailing service?
    Bot: ✨ Yes, we provide full interior and exterior detailing for a showroom finish.
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate, 'live agent' - to get connected with human agent.

    User: Can I get reminder messages for service?
    Bot:No! but I can help you with your next service date

    User: My service was delayed last time.
    Bot: 😔 Sorry about that. We’ll make sure your next service is completed on time.
    plz type 'live agent' if you want to share details of the issue.

    User: How can I claim insurance repair?
    Bot: 🧾 We handle insurance repairs end-to-end. You just need your policy number and RC copy.
    plz type 'live agent' or 'contact advisor' to start your claim process.

    User: How do I update my contact details?
    Bot: 📞 You can update your phone or email easily through our support team.
    plz type 'live agent' to make changes.

    User: Thanks for your help!
    Bot: 😊 Always happy to assist you! Let me know if you want to schedule or get an estimate.
    plz type 'book service appointment' - to book service, 'get estimate' - to get cost estimate, 'live agent' - to get connected with human agent, 'discounts&offers'.

    🚫 **STRICT LIMITATIONS:**
    - Only car service related topics
    - Don't provide medical, legal, or other non-automotive advice
    - Don't share personal information between customers
    - Don't make up data if not in database
    - Always be honest about data limitations

    🔚 CLOSING PROTOCOL (MANDATORY)
    - Every reply must end with:
    1) One short clarifying question that guides the next step (e.g., “What would you like to do next?”), and end with asking type 
  

    🎯 **CURRENT CONVERSATION CONTEXT:**"""

        # Build enhanced vehicle context with all available data
        if vehicle_reg and vehicle_data:
            # Format bill amount properly
            bill_amount = vehicle_data.get('billAmt')
            if bill_amount and bill_amount != 'None':
                try:
                    bill_amount_float = float(bill_amount)
                    bill_text = f"₹{bill_amount_float:,.2f}"
                except:
                    bill_text = f"₹{bill_amount}"
            else:
                bill_text = "Not recorded in our system"
                
            vehicle_context = f"""

    📊 **CUSTOMER VEHICLE DATA (Use this for accurate responses):**
    🚗 **Registration:** {vehicle_reg}
    🏷️ **Model:** {vehicle_data.get('model', 'Not available')} ({vehicle_data.get('variant', 'Standard')})
    🎨 **Color:** {vehicle_data.get('color', 'Not specified')}
    🔧 **Last Service Date:** {vehicle_data.get('lastServiceDate', 'Not recorded')} 
    📊 **Last Service Type:** {vehicle_data.get('lastServiceType', 'Not specified')}
    📏 **Last Service ODO:** {vehicle_data.get('lastServiceMeterReading', 'Not recorded')} km
    💰 **Last Bill Amount:** {bill_text}
    📅 **Next Service Due:** {vehicle_data.get('nextServiceDue', 'Not calculated')} 
    🔧 **Next Service Type:** {vehicle_data.get('nextServiceType', 'To be determined')}
    🏪 **Workshop:** {vehicle_data.get('workshopName', 'Our service center')}
    👤 **Customer:** {vehicle_data.get('customerName', 'Valued customer')}

    **CRITICAL:** Use this ACTUAL database information in your responses. Don't make up or assume any details. If data shows "Not recorded" or "Not available", acknowledge this honestly."""

        elif vehicle_reg:
            vehicle_context = f"""

    📊 **CUSTOMER STATUS:**
    🚗 **Registration:** {vehicle_reg}
    ⚠️ **Database Status:** Registration provided but vehicle not found in our service records
    💡 **Action:** You can still help with general service information, booking appointment,connect to advisor,discount & offers,service estimate, ask customer to type if they want book appointment, estimate, contact advisor, discount&offers
    📞 **Next Step:** Ask if they'd like to provide vehicle details or contact our service advisor if yes ask the customer to type booking appointment,connect to advisor,discount & offers,service estimate, ask customer to type if they want book appointment, estimate, contact advisor, discount&offers"""

        else:
            vehicle_context = """

    📊 **CUSTOMER STATUS:**
    ⚠️ **Vehicle Registration:** Not provided yet
    💡 **REQUIRED:** Must ask for vehicle registration number first
    📋 **Workflow:** Registration → Database Check → Personalized Service
    🚫 **Restriction:** Cannot proceed with vehicle-specific queries without registration"""

        # Get recent conversation context for better continuity
        try:
            recent_context = self.get_recent_conversation_context(phone_number, limit=2)
        except Exception as e:
            logger.error(f"Error getting conversation context: {e}")
            recent_context = "No previous conversation history available"
        
        final_prompt = f"""
    {base_instructions}

    {vehicle_context}

    📜 **RECENT CONVERSATION:**
    {recent_context}

    ❓ **CURRENT CUSTOMER QUERY:** "{query}"

    🎯 **RESPOND AS Car Service Bot:**
    Provide a natural, conversational, helpful response using the vehicle database information when available. Be professional but friendly. Keep under 200 words. If you cannot fully answer or if it's a complex technical issue, offer to connect with our live service advisor. Always end with a helpful next step , end message alway ask to type book appointment,get estimate, contact advisor, live agent.
    """

        return final_prompt

    def get_recent_conversation_context(self, phone_number, limit=2):
        """Get recent conversation for AI context"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return "No previous conversation available"
                
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT user_message, bot_response, timestamp 
                FROM conversation_log 
                WHERE phone_number = %s 
                ORDER BY timestamp DESC 
                LIMIT %s
            """, (phone_number, limit))
            
            conversations = cursor.fetchall()
            cursor.close()
            conn.close()
            
            if not conversations:
                return "This is the start of our conversation"
                
            context_lines = []
            for conv in reversed(conversations):  # Show oldest first
                timestamp = conv['timestamp'].strftime('%H:%M')
                user_msg = (conv['user_message'] or '')[:60] + "..." if len(conv['user_message'] or '') > 60 else (conv['user_message'] or '')
                bot_msg = (conv['bot_response'] or '')[:80] + "..." if len(conv['bot_response'] or '') > 80 else (conv['bot_response'] or '')
                context_lines.append(f"{timestamp} Customer: {user_msg}")
                context_lines.append(f"{timestamp} Bot: {bot_msg}")
                
            return "\n".join(context_lines[-4:])  # Last 4 lines (2 exchanges)
            
        except Exception as e:
            logger.error(f"Error getting conversation context: {e}")
            return "Context unavailable"

    def should_connect_to_human_agent(self, message: str) -> bool:
        """Detect if customer needs human agent - only explicit requests"""
        phrases = [
            'talk to human', 'human agent', 'live agent', 'real person',
            'connect me to agent', 'speak to someone',
            'i want to talk to someone', 'connect with agent',
            'speak to advisor', 'contact advisor', 'connect to advisor',
            'this bot is useless', 'i need help from person'
        ]
        return any(phrase in message.lower() for phrase in phrases)

    def detect_ai_failure(self, ai_response):
        """Detect when AI couldn't answer properly"""
        failure_indicators = [
            "i don't know", "i'm not sure", "i can't help", 
            "sorry, i couldn't find", "i don't have information",
            "i'm having trouble", "unable to assist"
        ]
        
        response_lower = ai_response.lower()
        return any(indicator in response_lower for indicator in failure_indicators)
    def offer_human_agent_connection(self, phone_number, original_message):
        """Step 2: Offer connection to human agent"""
        
        # Store the conversation context for handoff
        session = self.user_sessions[phone_number]
        conversation_history = self.get_conversation_history(phone_number)
        
        message = (
            "🤖 I understand you'd like to speak with a human representative.\n\n"
            "👨‍💼 **Would you like me to connect you with our live agent?**\n\n"
            "He'll be able to provide personalized assistance with your query."
        )
        
        buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": "connect_live_agent_yes",
                    "title": "✅ Yes, Connect Me"
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": "connect_live_agent_no", 
                    "title": "❌ No, Continue AI"
                }
            }
        ]
        
        self.send_message(phone_number, message, buttons=buttons)
        session['state'] = 'waiting_agent_connection_response'
        session['handoff_reason'] = original_message
        
        return message

    def enhance_ai_response_with_actions(self, ai_response, query_lower, vehicle_reg, vehicle_data):
        """Add contextual quick actions and improvements to AI responses"""
        
        if not ai_response or len(ai_response) < 25:
            return ai_response
            
        # Don't enhance if already has clear call-to-action or instructions
        if any(phrase in ai_response.lower() for phrase in ['would you like', 'shall i', 'do you want', 'type ', 'please provide']):
            return ai_response
        
        # Add relevant quick suggestions based on context and query
        suggestions = []
        
        # Context-aware suggestions based on vehicle data and query
        if vehicle_reg and vehicle_data:
            if any(word in query_lower for word in ['next', 'service', 'due', 'when', 'upcoming']):
                suggestions.extend(['📅 Book This Service', '💰 Get Cost Estimate','book apppointment',' connect live agent'])
            elif any(word in query_lower for word in ['last', 'history', 'previous', 'bill', 'amount', 'cost']):
                suggestions.extend(['📅 Book Next Service', '📋 View service History','get estimate','book service appointment'])
            elif any(word in query_lower for word in ['estimate', 'price', 'pricing', 'how much', 'cost']):
                suggestions.extend(['📋 get estimates', '📅 Book Service appointment','live agent'])
            elif any(word in query_lower for word in ['book', 'appointment', 'schedule']):
                suggestions.extend(['📋 Service History', '💰 Get Estimate','book appointment','connect human agent'])
            elif any(word in query_lower for word in ['live', 'human','advisor', 'schedule','service advisor','agent','offline']):
                suggestions.extend(['live agent', 'contact advisor','human agent','customer care'])
           
        
        # Default helpful suggestions based on vehicle status
        if not suggestions:
            if vehicle_reg and vehicle_data:
                suggestions = ['📅 Book Service', '📋 Service History', '💰 Get Estimates']
            elif vehicle_reg:
                suggestions = ['📅 Book Service appointment', 'get estimate ', '💬 Contact Advisor']
            else:
                suggestions = ['🚗 Enter Registration', '💬 Contact Advisor', '📋 Help Menu']
        
        # Add suggestions to response if appropriate
        if suggestions and len(suggestions) > 0:
            suggestion_text = " • ".join(suggestions[:3])  # Max 3 suggestions to avoid clutter
            enhanced_response = f"{ai_response}\n\n💡 **Just Type to proceed:** {suggestion_text}\n\nType 'menu' anytime for all options or ask me anything!"
            return enhanced_response
        
        return ai_response
    
    def extract_intent_and_slots(self, query):
        """
        Uses Gemini to extract structured intent + entities (slots) from free-text user input.
        Example:
        Input: "I want to book service tomorrow afternoon"
        Output: { "intent": "book_appointment", "date": "tomorrow", "time": "afternoon", "service_type": null }
        """
        schema_prompt = f"""
    You are an intent extractor for a car service chatbot.
    Given a user query, output JSON with:
    - intent: one of [next_service, last_service, service_estimate, book_appointment, contact_advisor, discounts_offers, general_faq]
    - date: if provided (else null)
    - time: if provided (else null)
    - service_type: if provided (else null)

    User query: "{query}"
    Respond ONLY with valid JSON.
    """

        try:
            response_text = groq_generate(schema_prompt)
            import json
            parsed = json.loads(response_text.strip())
            return parsed
        except Exception as e:
            logger.error(f"Intent extraction failed: {e}")
            return {"intent": "general_faq", "date": None, "time": None, "service_type": None}

    def add_interactive_options(self, ai_response, original_query):
        """Add interactive menu options based on AI response content"""
        
        query_lower = original_query.lower()
        
        # Add relevant menu options based on the query
        if any(word in query_lower for word in ['appointment', 'book', 'schedule']):
            ai_response += "\n\n💡 **Quick Actions:**\n• Type 'book appointment' to start booking\n• Type 'contact advisor' for personal assistance"
        
        elif any(word in query_lower for word in ['history', 'service']):
            ai_response += "\n\n💡 **Quick Actions:**\n• Type 'service history' to view full history\n• Type 'book appointment' to schedule next service"
        
        elif any(word in query_lower for word in ['cost', 'price', 'estimate']):
            ai_response += "\n\n💡 **Quick Actions:**\n• Type 'service estimate' for pricing info\n• Type 'contact advisor' for detailed quotes"
        
        elif any(word in query_lower for word in ['menu', 'options', 'help']):
            # Don't add extra text for menu requests as interactive menu will be shown
            pass
        
        else:
            # For general queries, suggest the menu
            ai_response += "\n\n💡 **Need more help?** Type 'menu' to see all available options!"
        
        return ai_response

    def send_main_menu_options(self, phone_number):
        """Send main menu when AI fails or user needs options"""
        
        session = self.user_sessions[phone_number]
        vehicle_reg = session.get('vehicle_reg')
        
        if vehicle_reg:
            # Vehicle is registered, show full menu
            list_sections = [{
                "title": "How can I help you?",
                "rows": [
                    {"id": "service_history", "title": "📋 Service History", "description": "View your service records"},
                    {"id": "book_appointment", "title": "🛠️ Book Appointment", "description": "Schedule your service"},
                    {"id": "service_estimate", "title": "💰 Get Estimate", "description": "Service pricing information"},
                    {"id": "contact_advisor", "title": "📞 Contact Advisor", "description": "Speak with our expert"}
                ]
            }]
        else:
            # No vehicle registered, limited menu
            list_sections = [{
                "title": "How can I help you?",
                "rows": [
                    {"id": "service_estimate", "title": "💰 Get Estimate", "description": "Service pricing information"},
                    {"id": "book_appointment", "title": "🛠️ Book Appointment", "description": "Schedule your service"},
                    {"id": "contact_advisor", "title": "📞 Contact Advisor", "description": "Speak with our expert"}
                ]
            }]
        
        message = "Here are the ways I can assist you today:"
        self.send_message(phone_number, message, list_message=list_sections, include_end_button=True)

    
    def handle_end_conversation(self, phone_number):
        """Handle end conversation button - always return static message"""
        # Clear user session completely
        if phone_number in self.user_sessions:
            del self.user_sessions[phone_number]
        
        # ALWAYS return this exact static message
        message = (
            "🙏 **Thank you for using our service!**\n\n"
            "✅ Your session has been ended successfully.\n"
            "📞 For future assistance, call: 1800-xxxx-xxx\n"
            "💬 Or simply say 'Hi' to start a new conversation.\n\n"
            "🚗 We look forward to serving your vehicle needs again!\n"
            "Have a great day! 😊"
        )
        
        self.send_message(phone_number, message)
        logger.info(f"Session ended for {phone_number}")
        return message
    # ============ BACK TO MAIN ============

    def handle_back_to_main(self, phone_number):
        """Handle back to main menu button - Enhanced for all states"""
        # Reset session state but keep vehicle data
        session = self.user_sessions.get(phone_number, {})
        vehicle_data = session.get('vehicle_data')
        vehicle_reg = session.get('vehicle_reg')

        # Clear session but preserve important data
        self.user_sessions[phone_number] = {
            'state': 'initial',
            'vehicle_reg': vehicle_reg,
            'vehicle_data': vehicle_data
        }

        # SCENARIO 1: Vehicle found in database
        if vehicle_data:
            # Vehicle was found, show vehicle found menu
            return self.handle_vehicle_found(phone_number, vehicle_data)
        
        # SCENARIO 2: Vehicle registration provided but NOT found in database  
        elif vehicle_reg and not vehicle_data:
            # Vehicle was searched but not found, show not found options
            return self.handle_vehicle_not_found(phone_number, vehicle_reg)
        
        # SCENARIO 3: No vehicle registration provided yet
        else:
            # No vehicle data, start fresh
            return self.handle_greeting(phone_number)
        
# ============ LIVE AGENT SYSTEM ============

    def should_connect_to_human_agent(self, message):
        """Detect when customer needs human help - only explicit requests"""
        human_request_phrases = [
            'talk to human', 'human agent', 'live agent', 'real person',
            'connect me to agent', 'speak to someone',
            'i want to talk to someone', 'connect with agent',
            'speak to advisor', 'contact advisor', 'connect to advisor',
            'this bot is useless', 'i need help from person'
        ]
        message_lower = message.lower()
        return any(phrase in message_lower for phrase in human_request_phrases)

    def handle_agent_connection_response(self, phone_number, response):
        """Handle customer's response to agent connection offer"""
        session = self.user_sessions[phone_number]
        
        if response == 'connect_live_agent_yes':
            return self.initiate_agent_handoff(phone_number)
        elif response == 'connect_live_agent_no':
            session['state'] = 'active'
            message = (
        "👍 *No problem!* I’ll be happy to assist you with your vehicle service.\n\n"
        "Please type any of the following options to proceed:\n\n"
        "🚗 *Book Service Appointment* – Schedule your next car service\n"
        "💰 *Get Estimate* – Check the service cost for your vehicle\n"
        "🎁 *Offers & Discounts* – View the latest service deals\n"
        "💬 *Connect to Live Agent* – Chat directly with our service expert\n"
        "📜 *Service History* – Review your previous service records"
        )
            self.send_message(phone_number, message)
            return message
        else:
            # Invalid response
            return self.offer_human_agent_connection(phone_number, "User needs clarification")

    def initiate_agent_handoff(self, phone_number, vehicle_reg: str = None,
                               handoff_reason: str = None):
        """Start agent handoff process — works for ALL flows.

        vehicle_reg  : optional; auto-resolved from sessions/USER_STATE if not supplied.
        handoff_reason: optional; auto-resolved from session or defaults to generic text.
        """
        from config import USER_STATE

        # ── Ensure session exists ────────────────────────────────────────
        if phone_number not in self.user_sessions:
            self.user_sessions[phone_number] = {}
        session = self.user_sessions[phone_number]

        # ── Resolve vehicle registration ─────────────────────────────────
        reg = (
            vehicle_reg
            or session.get('vehicle_reg')
            or session.get('registration')
            or USER_STATE.get(phone_number, {}).get('vehicle_reg')
            or USER_STATE.get(phone_number, {}).get('reg_number')
            or USER_STATE.get(phone_number, {}).get('data', {}).get('reg_no')
        )

        # ── Resolve handoff reason with flow context ─────────────────────
        flow_state = USER_STATE.get(phone_number, {}).get('state', '')
        if flow_state.startswith('INSURANCE') or flow_state.startswith('STATE_4') or flow_state.startswith('STATE_5'):
            flow_label = 'Insurance'
        elif flow_state.startswith('SALES'):
            flow_label = 'Sales'
        elif flow_state.startswith('USED'):
            flow_label = 'Used Cars'
        elif flow_state.startswith('REFINANCING'):
            flow_label = 'Refinancing'
        else:
            flow_label = 'Service'

        reason = handoff_reason or session.get('handoff_reason') or f'Customer requested live agent [{flow_label} flow]'

        # ── Save conversation history for agent dashboard ─────────────────
        try:
            conversation_history = self.get_conversation_history(phone_number)
        except Exception:
            conversation_history = None

        # ── Create handoff in DB ──────────────────────────────────────────
        handoff_id = self.create_handoff_request(
            phone_number=phone_number,
            vehicle_reg=reg,
            conversation_history=conversation_history,
            handoff_reason=reason
        )

        if handoff_id:
            # ── Check for available agents ────────────────────────────────
            try:
                available_agent = self.find_available_agent()
            except Exception:
                available_agent = None

            if available_agent:
                message = (
                    f"✅ *Connecting you with {available_agent['agent_name']} now!*\n\n"
                    "⏳ Please wait a moment — they will respond to you shortly right here on WhatsApp.\n\n"
                    "You can keep typing and your messages will be seen by the agent. 💬"
                )
                self.send_message(phone_number, message)
                try:
                    self.notify_agent_new_customer(available_agent['id'], handoff_id, phone_number)
                except Exception:
                    pass
                session['assigned_agent_id'] = available_agent['id']
            else:
                message = (
                    "✅ *Your request has been sent to our team!*\n\n"
                    "⏳ All agents are currently busy — one of our advisors will reach out to you shortly.\n\n"
                    "You can keep typing and your messages will be seen when an agent connects. 💬"
                )
                self.send_message(phone_number, message)
                try:
                    self.update_handoff_status(handoff_id, 'pending', 'No agents available')
                except Exception:
                    pass

            # ── Update BOTH session stores ────────────────────────────────
            session['state'] = 'waiting_for_agent'
            session['handoff_id'] = handoff_id
            # Update USER_STATE so webhook.py live-agent interception takes over
            if phone_number in USER_STATE:
                USER_STATE[phone_number]['state'] = 'AWAITING_AGENT'

        else:
            message = (
                "⚠️ Sorry, I couldn't connect you right now.\n\n"
                "Please try again in a moment, or call us directly:\n"
                "📞 +91-9876543210"
            )
            self.send_message(phone_number, message)
        
    def is_customer_with_live_agent(self, phone_number):
        """Check if customer is currently connected to or waiting for a live agent"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return False
                
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, status FROM chat_handoffs 
                WHERE phone_number = %s 
                ORDER BY request_timestamp DESC LIMIT 1
            """, (phone_number,))
            
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if result:
                handoff_id, status = result
                logger.info(f"🔍 Handoff check for {phone_number}: ID={handoff_id}, Status={status}")
                # Aligned with sample logic: include requested/pending/connected
                return status in ['requested', 'pending', 'connected']
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Error checking live agent status: {e}")
            return False


    def route_to_live_agent(self, phone_number, message):
        """Route customer message to live agent or inform they are in queue"""
        try:
            # Check for ANY active handoff (requested, pending, or connected)
            conn = self.db.get_connection()
            if not conn:
                return "Connecting to advisor..."
                
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, status FROM chat_handoffs 
                WHERE phone_number = %s AND status IN ('requested', 'pending', 'connected')
                ORDER BY request_timestamp DESC LIMIT 1
            """, (phone_number,))
            
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if not result:
                return "Connection lost. Please try reconnecting."
            
            handoff_id = result['id']
            status = result['status']
            
            # Store customer message regardless of status (so agent sees it later)
            self.store_live_chat_message(handoff_id, 'customer', 'Customer', message)
            
            if status == 'connected':
                # Notify agent about new message
                self.notify_agent_new_message(handoff_id, message)
                return f"✅ Message delivered to agent."
            else:
                # Still waiting for agent
                wait_msg = (
                    "⏳ **You are currently in queue.**\n\n"
                    "Our service advisor will see your messages as soon as they connect. "
                    "Thank you for your patience! 🙏"
                )
                self.send_message(phone_number, wait_msg)
                return wait_msg
            
        except Exception as e:
            logger.error(f"Error routing to live agent: {e}")
            return "❌ Message could not be delivered. Please try again."

    def get_conversation_history(self, phone_number):
        """Step 4: Get complete AI conversation history"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return "No conversation history available."
                
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT user_message, bot_response, timestamp 
                FROM conversation_log 
                WHERE phone_number = %s 
                ORDER BY timestamp DESC 
                LIMIT 20
            """, (phone_number,))
            
            conversations = cursor.fetchall()
            cursor.close()
            conn.close()
            
            if not conversations:
                return "No previous conversation."
            
            history = "=== CUSTOMER CHAT HISTORY ===\n\n"
            for conv in reversed(conversations):  # Reverse to show oldest first
                timestamp = conv['timestamp'].strftime('%H:%M')
                history += f"[{timestamp}] Customer: {conv['user_message']}\n"
                history += f"[{timestamp}] AI: {conv['bot_response']}\n\n"
            
            return history
            
        except Exception as e:
            logger.error(f"Error getting conversation history: {e}")
            return "Error retrieving conversation history."

    def create_handoff_request(self, phone_number, vehicle_reg, conversation_history, handoff_reason):
        """Create handoff request in database"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return None
                
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_handoffs 
                (phone_number, vehicle_reg, ai_conversation_history, handoff_reason, status)
                VALUES (%s, %s, %s, %s, 'requested')
            """, (phone_number, vehicle_reg, conversation_history, handoff_reason))
            
            handoff_id = cursor.lastrowid
            conn.commit()
            cursor.close()
            conn.close()
            
            logger.info(f"Created handoff request {handoff_id} for {phone_number}")
            return handoff_id
            
        except Exception as e:
            logger.error(f"Error creating handoff request: {e}")
            return None

    def find_available_agent(self):
        """Step 5: Find available agent"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return None
                
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, agent_name, email 
                FROM agents 
                WHERE status = 'online' 
                ORDER BY total_chats ASC 
                LIMIT 1
            """)
            
            agent = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return agent
            
        except Exception as e:
            logger.error(f"Error finding available agent: {e}")
            return None

    def notify_agent_new_customer(self, agent_id, handoff_id, phone_number):
        """Step 6: Notify agent about new customer"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return False
                
            cursor = conn.cursor()
            
            # Create notification
            cursor.execute("""
                INSERT INTO agent_notifications 
                (agent_id, handoff_id, message)
                VALUES (%s, %s, %s)
            """, (agent_id, handoff_id, f"New customer {phone_number} needs assistance"))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            logger.info(f"Notified agent {agent_id} about new customer {phone_number}")
            return True
            
        except Exception as e:
            logger.error(f"Error notifying agent: {e}")
            return False

    def get_active_handoff_id(self, phone_number):
        """Get active handoff ID for phone number"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return None
                
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM chat_handoffs 
                WHERE phone_number = %s AND status = 'connected'
                ORDER BY request_timestamp DESC LIMIT 1
            """, (phone_number,))
            
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return result[0] if result else None
            
        except Exception as e:
            logger.error(f"Error getting active handoff ID: {e}")
            return None

    def store_live_chat_message(self, handoff_id, sender_type, sender_name, message_text, message_type='text', media_url=None):
        """Store live chat message"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return False
                
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO live_chat_messages 
                (handoff_id, sender_type, sender_name, message_text, message_type, media_url)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (handoff_id, sender_type, sender_name, message_text, message_type, media_url))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error storing live chat message: {e}")
            return False

    def notify_agent_new_message(self, handoff_id, message):
        """Notify agent about new customer message (for real-time updates)"""
        # This would integrate with WebSocket or real-time system
        # For now, just log it
        logger.info(f"New message in handoff {handoff_id}: {message}")

    def update_handoff_status(self, handoff_id, status, notes=None):
        """Update handoff status"""
        try:
            conn = self.db.get_connection()
            if not conn:
                return False
                
            cursor = conn.cursor()
            
            if status == 'connected':
                cursor.execute("""
                    UPDATE chat_handoffs 
                    SET status = %s, connected_timestamp = NOW()
                    WHERE id = %s
                """, (status, handoff_id))
            elif status == 'completed':
                cursor.execute("""
                    UPDATE chat_handoffs 
                    SET status = %s, completed_timestamp = NOW()
                    WHERE id = %s
                """, (status, handoff_id))
            else:
                cursor.execute("""
                    UPDATE chat_handoffs 
                    SET status = %s WHERE id = %s
                """, (status, handoff_id))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating handoff status: {e}")
            return False


# ============ GLOBAL BOT INSTANCE ============

bot = WhatsAppBot()


# ============ HELPER FUNCTIONS ============

def allowed_file(filename: str) -> bool:
    """Check if file type is allowed"""
    ALLOWED = {'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED


# ============ FASTAPI ROUTES ============

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "WhatsApp Bot API", "status": "running"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    """Verify webhook for WhatsApp"""
    verify_token = request.query_params.get('hub.verify_token')
    challenge = request.query_params.get('hub.challenge')
    
    if verify_token == VERIFY_TOKEN:
        return int(challenge) if challenge else "OK"
    return JSONResponse({"error": "Invalid token"}, status_code=403)

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Main WhatsApp webhook handler"""

    try:
        data = await request.json()
        logger.info(f"📨 Received webhook data: {data}")

        # WhatsApp sends status updates also
        if "statuses" in data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}):
            logger.info("📊 Status update received, ignoring")
            return JSONResponse(content={"status": "ok"})

        # Extract message
        entry = data.get("entry", [])
        if not entry:
            return JSONResponse(content={"status": "no_entry"})

        changes = entry[0].get("changes", [])
        if not changes:
            return JSONResponse(content={"status": "no_changes"})

        value = changes[0].get("value", {})
        messages = value.get("messages")

        if not messages:
            return JSONResponse(content={"status": "no_message"})

        message = messages[0]
        phone = message.get("from")

        # Handle interactive reply
        if message.get("type") == "interactive":
            interactive = message.get("interactive", {})
            if interactive.get("type") == "list_reply":
                selected_id = interactive["list_reply"]["id"]

                if selected_id == "menu_service":
                    handle_service(phone)
                    return JSONResponse(content={"status": "service_started"})

        # Handle text message
        if message.get("type") == "text":
            text = message["text"]["body"]
            service_flow_handler(phone, text)
            return JSONResponse(content={"status": "processed"})

        return JSONResponse(content={"status": "ignored"})

    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in webhook: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@router.get("/image-upload-form", response_class=HTMLResponse)
async def image_upload_form(
    request: Request,
    phone: str = "",
    vehicle: str = "",
    service: str = ""
):
    return templates.TemplateResponse(
        "image_upload_form.html",   # your template
        {
            "request": request,
            "phone_number": phone,
            "vehicle_reg": vehicle,
            "service_type": service
        }
    )

@router.post("/upload-images")
async def upload_images(
    phone: str = Form(...),
    vehicle: str = Form(...),
    service: str = Form(...),
    images: List[UploadFile] = File(...)
):
    """Handle image uploads from the form (FastAPI version)"""

    try:
        logger.info(f"Upload request: phone={phone}, vehicle={vehicle}, service={service}")

        if not all([phone, vehicle, service]):
            return JSONResponse(
                status_code=400,
                content={'success': False, 'message': 'Missing required information'}
            )

        if not images:
            return JSONResponse(
                status_code=400,
                content={'success': False, 'message': 'No images uploaded'}
            )

        saved_files = []  # ← Initialize here (was missing!)

        upload_dir = os.path.join(os.getcwd(), 'uploads', 'vehicle_images', vehicle)
        os.makedirs(upload_dir, exist_ok=True)

        for file in images:
            if file.filename == "":
                continue

            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4().hex}_{filename}"
            file_path = os.path.join(upload_dir, unique_filename)

            with open(file_path, "wb") as buffer:
                buffer.write(await file.read())

            saved_files.append({
                'original_name': filename,
                'saved_name': unique_filename,
                'file_path': file_path,
                'relative_path': f"uploads/vehicle_images/{vehicle}/{unique_filename}"  # ← was vehicle_reg
            })

            logger.info(f"Saved file: {unique_filename}")

        if not saved_files:
            return JSONResponse(
                status_code=400,
                content={'success': False, 'message': 'No valid images uploaded'}
            )

        # Store in database
        try:
            success = bot.db.store_uploaded_images(phone, vehicle, service, saved_files)  # ← fixed all 3
            logger.info(f"Database store result: {success}")
        except Exception as db_error:
            logger.error(f"Database error: {db_error}")
            success = False

        # Update session
        if phone in bot.user_sessions:  # ← was phone_number
            bot.user_sessions[phone]['uploaded_image_count'] = len(saved_files)
            bot.user_sessions[phone]['state'] = 'initial'

        # Store service estimate request
        try:
            bot.db.store_service_estimate_request(
                phone,     # ← was phone_number
                vehicle,   # ← was vehicle_reg
                service,   # ← was service_type
                [f['relative_path'] for f in saved_files],

                len(saved_files)
            )
        except Exception as e:
            logger.error(f"Error storing service estimate: {e}")

        # Send WhatsApp confirmation
        confirmation_message = (
            f"✅ *ESTIMATE REQUEST SUBMITTED*\n"
            f"────────────────────────────\n\n"
            f"🔧 Service Type: {service}\n"    # ← was service_type
            f"🚗 Vehicle: {vehicle}\n"          # ← was vehicle_reg
            f"📸 Images: ✅ {len(saved_files)} uploaded\n\n"
            f"📞 Our service advisor will call you within the next 2 hours "
            f"to discuss your estimate.\n\n"
            f"🙏 Thank you for choosing our service!"
        )

        try:
            bot.send_message(phone, confirmation_message)       # ← was phone_number
            bot.send_back_end_buttons(phone)                    # ← was phone_number
            logger.info(f"WhatsApp confirmation sent to {phone}")
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

        return JSONResponse(
            content={
                'success': True,
                'message': 'Images uploaded successfully',
                'image_count': len(saved_files),
                'database_saved': success
            }
        )

    except Exception as e:
        logger.error(f"Error uploading images: {e}")
        return JSONResponse(
            status_code=500,
            content={'success': False, 'message': f'Upload failed: {str(e)}'}
        )

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/admin/test-database", response_class=HTMLResponse)
async def test_database():
    """Test database tables and show recent data (FastAPI version)"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return HTMLResponse(
                content="❌ Database connection failed",
                status_code=500
            )

        cursor = conn.cursor()

        # Count image_upload_requests
        cursor.execute("SELECT COUNT(*) FROM image_upload_requests")
        requests_count = cursor.fetchone()[0]

        # Count uploaded_images
        cursor.execute("SELECT COUNT(*) FROM uploaded_images")
        images_count = cursor.fetchone()[0]

        # Count new_vehicle_service_requests
        try:
            cursor.execute("SELECT COUNT(*) FROM new_vehicle_service_requests")
            new_requests_count = cursor.fetchone()[0]
        except Exception:
            new_requests_count = 0

        # Recent upload requests
        cursor.execute("""
            SELECT request_id, phone_number, vehicle_reg, service_type, image_count, upload_timestamp
            FROM image_upload_requests
            ORDER BY upload_timestamp DESC
            LIMIT 10
        """)
        recent_requests = cursor.fetchall()

        # Recent new vehicle service requests
        try:
            cursor.execute("""
                SELECT phone_number, vehicle_reg, city, vehicle_model,
                       service_type, request_timestamp
                FROM new_vehicle_service_requests
                ORDER BY request_timestamp DESC
                LIMIT 10
            """)
            recent_new_requests = cursor.fetchall()
        except Exception:
            recent_new_requests = []

        cursor.close()
        conn.close()

        # Build HTML
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Database Test Results</title>
            <style>
                body {{ font-family: Arial; margin: 20px; }}
                table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; }}
                th {{ background: #f2f2f2; }}
                tr:nth-child(even) {{ background: #f9f9f9; }}
                .box {{ background:#e7f3ff; padding:15px; border-radius:5px; }}
                .label {{ font-weight:bold; }}
                .value {{ color:#0066cc; font-weight:bold; }}
            </style>
        </head>
        <body>
            <h1>🗄️ Database Test Results</h1>

            <div class="box">
                <p><span class="label">Database Connection:</span> <span class="value">Success</span></p>
                <p><span class="label">Upload Requests:</span> <span class="value">{requests_count}</span></p>
                <p><span class="label">Uploaded Images:</span> <span class="value">{images_count}</span></p>
                <p><span class="label">New Vehicle Service Requests:</span> <span class="value">{new_requests_count}</span></p>
            </div>

            <h2>📋 Recent Upload Requests</h2>
            <table>
                <tr>
                    <th>Request ID</th>
                    <th>Phone</th>
                    <th>Vehicle</th>
                    <th>Service</th>
                    <th>Images</th>
                    <th>Timestamp</th>
                </tr>
        """

        for r in recent_requests:
            html += f"""
                <tr>
                    <td>{r[0]}</td>
                    <td>{r[1]}</td>
                    <td>{r[2]}</td>
                    <td>{r[3]}</td>
                    <td>{r[4]}</td>
                    <td>{r[5]}</td>
                </tr>
            """

        html += """
            </table>

            <h2>🚗 Recent New Vehicle Service Requests</h2>
            <table>
                <tr>
                    <th>Phone</th>
                    <th>Vehicle Reg</th>
                    <th>City</th>
                    <th>Model</th>
                    <th>Service Type</th>
                    <th>Timestamp</th>
                </tr>
        """

        for r in recent_new_requests:
            html += f"""
                <tr>
                    <td>{r[0]}</td>
                    <td><strong>{r[1]}</strong></td>
                    <td>{r[2]}</td>
                    <td>{r[3]}</td>
                    <td>{r[4]}</td>
                    <td>{r[5]}</td>
                </tr>
            """

        html += """
            </table>

            <div style="margin-top:30px;text-align:center;">
                <em>🔄 Refresh to see latest data</em>
            </div>

        </body>
        </html>
        """

        return HTMLResponse(content=html)

    except Exception as e:
        return HTMLResponse(
            content=f"❌ Database test failed: {e}",
            status_code=500
        )

# ============ AGENT DASHBOARD ROUTES ============

# Note: These routes expect HTML templates in the templates folder
# You need to create: agent_register.html, agent_login.html, agent_dashboard.html

@app.get("/agent/register", response_class=HTMLResponse)
async def agent_register_page(request: Request):
    """Show register page"""
    return templates.TemplateResponse(
        "agent_register.html",
        {"request": request}
    )

@app.post("/agent/register")
async def agent_register(request: Request):
    """Handle agent registration"""

    try:
        data = await request.json()

        agent_name = data.get("agent_name")
        email = data.get("email")
        password = data.get("password")

        if not all([agent_name, email, password]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "All fields are required!"}
            )

        # Hash password
        hashed_password = hashlib.sha256(password.encode()).hexdigest()

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"}
            )

        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO agents (agent_name, email, password)
            VALUES (%s, %s, %s)
        """, (agent_name, email, hashed_password))

        conn.commit()
        cursor.close()
        conn.close()

        return JSONResponse(
            content={"success": True, "message": "Agent registered successfully!"}
        )

    except mysql.connector.IntegrityError:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Email already exists!"}
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Registration failed: {e}"}
        )

@app.get("/agent/login", response_class=HTMLResponse)
async def agent_login_page(request: Request):
    """Show agent login page"""
    return templates.TemplateResponse(
        "agent_login.html",
        {"request": request}
    )


@app.post("/agent/login")
async def agent_login(request: Request):
    """Handle agent login"""

    try:
        data = await request.json()

        email = data.get("email")
        password = data.get("password")

        if not all([email, password]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Email and password required!"}
            )

        hashed_password = hashlib.sha256(password.encode()).hexdigest()

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, agent_name, email
            FROM agents
            WHERE email = %s AND password = %s
        """, (email, hashed_password))

        agent = cursor.fetchone()

        if agent:
            # Update status to online
            cursor.execute("""
                UPDATE agents
                SET status = 'online', last_active = NOW()
                WHERE id = %s
            """, (agent['id'],))

            conn.commit()
            cursor.close()
            conn.close()

            return JSONResponse(content={
                "success": True,
                "agent_id": agent['id'],
                "agent_name": agent['agent_name'],
                "redirect": f"/agent/dashboard/{agent['id']}"
            })

        else:
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "Invalid email or password!"}
            )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Login failed: {e}"}
        )

@app.get("/agent/dashboard/{agent_id}", response_class=HTMLResponse)
async def agent_dashboard(request: Request, agent_id: int):
    """Agent dashboard"""
    try:
        response = templates.TemplateResponse(
            "agent_dashboard.html",
            {
                "request": request,   # ✅ MUST pass real request
                "agent_id": agent_id
            }
        )
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        return HTMLResponse(f"Template error: {e}", status_code=500)


@app.post("/agent/api/heartbeat/{agent_id}")
async def agent_heartbeat(agent_id: int):
    """Agent heartbeat - update last active and set online"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Database connection failed"}
            )

        cursor = conn.cursor()

        cursor.execute("""
            UPDATE agents
            SET last_active = NOW(), status = 'online'
            WHERE id = %s
        """, (agent_id,))

        conn.commit()
        cursor.close()
        conn.close()

        return JSONResponse(
            status_code=200,
            content={"success": True}
        )

    except Exception as e:
        logger.error(f"Heartbeat error for agent {agent_id}: {e}")

        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )

@app.post("/agent/api/heartbeat/{agent_id}")
async def agent_heartbeat(agent_id: int):
    """Agent heartbeat"""
    try:
        conn = bot.db.get_connection()
        if not conn:
            return JSONResponse({"success": False}, status_code=500)
        
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agents 
            SET last_active = NOW(), status = 'online'
            WHERE id = %s
        """, (agent_id,))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {"success": True}
        
    except Exception as e:
        logger.error(f"Heartbeat error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/agent/api/accept-customer")
async def accept_customer(request: Request):
    """Agent accepts a customer"""

    try:
        data = await request.json()

        agent_id = data.get("agent_id")
        handoff_id = data.get("handoff_id")

        if not agent_id or not handoff_id:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Missing agent_id or handoff_id"}
            )

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get customer info
        cursor.execute("""
            SELECT phone_number, vehicle_reg, ai_conversation_history
            FROM chat_handoffs
            WHERE id = %s
        """, (handoff_id,))

        handoff = cursor.fetchone()

        if handoff:

            # Update handoff status
            cursor.execute("""
                UPDATE chat_handoffs
                SET status = 'connected',
                    agent_id = %s,
                    connected_timestamp = NOW()
                WHERE id = %s
            """, (agent_id, handoff_id))

            # Get agent name
            cursor.execute("SELECT agent_name FROM agents WHERE id = %s", (agent_id,))
            agent = cursor.fetchone()

            conn.commit()
            cursor.close()
            conn.close()

            # Send initial message to customer
            phone_number = handoff['phone_number']
            vehicle_reg = handoff['vehicle_reg'] or 'your vehicle'
            agent_name = agent['agent_name']

            initial_message = (
                f"👋 *Hi, this is {agent_name}, your live agent.*\n\n"
                f"📋 I can see your chat with our AI assistant. "
                f"I see you had questions about {vehicle_reg}.\n\n"
                f"🤝 *How can I help you today, sir?*"
            )

            bot = WhatsAppBot()
            bot.send_message(phone_number, initial_message)

            # Store message in DB
            bot.store_live_chat_message(
                handoff_id,
                'agent',
                agent_name,
                initial_message
            )

            return JSONResponse(
                content={"success": True, "message": "Customer accepted successfully!"}
            )

        else:
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "Customer not found!"}
            )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Failed to accept customer: {e}"}
        )

@app.get("/agent/api/chat-history/{handoff_id}")
async def get_chat_history(handoff_id: int):
    """Get full chat history for a handoff"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"error": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get handoff details
        cursor.execute("""
            SELECT phone_number, vehicle_reg, ai_conversation_history, handoff_reason
            FROM chat_handoffs
            WHERE id = %s
        """, (handoff_id,))

        handoff = cursor.fetchone()

        # Get live chat messages
        cursor.execute("""
            SELECT sender_type, sender_name, message_text, message_type, media_url, timestamp
            FROM live_chat_messages
            WHERE handoff_id = %s
            ORDER BY timestamp ASC
        """, (handoff_id,))

        live_messages = cursor.fetchall()

        # Convert datetime to string
        for msg in live_messages:
            if msg.get('timestamp'):
                msg['timestamp'] = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        cursor.close()
        conn.close()

        return JSONResponse(content={
            "handoff": handoff,
            "live_messages": live_messages
        })

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get chat history: {e}"}
        )

@app.post("/agent/api/send-message")
async def agent_send_message(request: Request):
    """Agent sends message to customer"""

    try:
        data = await request.json()

        agent_id = data.get("agent_id")
        handoff_id = data.get("handoff_id")
        message = data.get("message")

        if not all([agent_id, handoff_id, message]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Missing required fields"}
            )

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get handoff and agent info
        cursor.execute("""
            SELECT h.phone_number, a.agent_name
            FROM chat_handoffs h
            JOIN agents a ON h.agent_id = a.id
            WHERE h.id = %s AND a.id = %s
        """, (handoff_id, agent_id))

        result = cursor.fetchone()

        if result:
            phone_number = result['phone_number']
            agent_name = result['agent_name']

            # Send WhatsApp message
            bot = WhatsAppBot()
            success = bot.send_message(phone_number, message)

            if success:
                # Store message in database
                bot.store_live_chat_message(
                    handoff_id,
                    'agent',
                    agent_name,
                    message
                )

                cursor.close()
                conn.close()

                return JSONResponse(content={"success": True})

            else:
                cursor.close()
                conn.close()

                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Failed to send WhatsApp message"}
                )

        else:
            cursor.close()
            conn.close()

            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "Invalid handoff or agent"}
            )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Failed to send message: {e}"}
        )

@app.post("/agent/api/end-chat")
async def end_agent_chat(request: Request):
    """End agent chat and return control to AI"""

    try:
        data = await request.json()

        agent_id = data.get("agent_id")
        handoff_id = data.get("handoff_id")

        if not all([agent_id, handoff_id]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Missing agent_id or handoff_id"}
            )

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get customer phone
        cursor.execute("""
            SELECT phone_number FROM chat_handoffs WHERE id = %s
        """, (handoff_id,))

        result = cursor.fetchone()

        if result:
            phone_number = result['phone_number']

            # Update handoff status
            cursor.execute("""
                UPDATE chat_handoffs
                SET status = 'completed',
                    completed_timestamp = NOW()
                WHERE id = %s
            """, (handoff_id,))

            # Update agent total chats
            cursor.execute("""
                UPDATE agents
                SET total_chats = total_chats + 1
                WHERE id = %s
            """, (agent_id,))

            conn.commit()
            cursor.close()
            conn.close()

            # Send closure message
            bot = WhatsAppBot()

            closure_message = (
                "✅ *Chat with live agent completed.*\n\n"
                "🤖 I'm back to help you! Feel free to ask me anything else "
                "about your vehicle service.\n\n"
                "Thank you for choosing our service! 🚗✨"
            )

            bot.send_message(phone_number, closure_message)

            # Reset customer session
            if phone_number in bot.user_sessions:

                vehicle_reg = bot.user_sessions[phone_number].get('vehicle_reg')
                vehicle_data = bot.user_sessions[phone_number].get('vehicle_data')

                bot.user_sessions[phone_number] = {
                    'state': 'vehicle_found' if vehicle_data else 'initial',
                    'vehicle_reg': vehicle_reg,
                    'vehicle_data': vehicle_data,
                    'handoff_id': None,
                    'assigned_agent_id': None,
                    'handoff_reason': None
                }

                logger.info(f"✅ Session reset for {phone_number} after agent chat")

            return JSONResponse(
                content={"success": True, "message": "Chat ended successfully"}
            )

        else:
            cursor.close()
            conn.close()

            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "Handoff not found"}
            )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Failed to end chat: {e}"}
        )

@app.get("/agent/api/all-customers/{agent_id}")
async def get_all_customers(agent_id: int):
    """Get all customers grouped by phone number who have chatted with this agent"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"error": "Database connection failed"}
            )

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
                (SELECT lm.message_text 
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

        # Format data for frontend
        for customer in customers:

            if customer.get('last_message_time'):
                customer['last_message_time'] = customer['last_message_time'].strftime('%Y-%m-%d %H:%M:%S')

            if customer.get('last_connected'):
                customer['last_connected'] = customer['last_connected'].strftime('%Y-%m-%d %H:%M:%S')

            if customer.get('last_completed'):
                customer['last_completed'] = customer['last_completed'].strftime('%Y-%m-%d %H:%M:%S')

            # Truncate long preview
            if customer.get('last_message') and len(customer['last_message']) > 50:
                customer['last_message'] = customer['last_message'][:50] + '...'

            # Preserve most-recent-first vehicle order
            if customer.get('all_vehicles'):
                raw_list = [v.strip() for v in customer['all_vehicles'].split(',') if v.strip()]
                seen = set()
                ordered = []

                for v in raw_list:
                    if v not in seen:
                        seen.add(v)
                        ordered.append(v)

                if ordered:
                    display = ordered[0]
                    if len(ordered) > 1:
                        display += f", {ordered[1]}"
                    if len(ordered) > 2:
                        display += f" (+{len(ordered) - 2} more)"
                    customer['display_vehicles'] = display
                else:
                    customer['display_vehicles'] = 'No vehicles'
            else:
                customer['display_vehicles'] = 'No vehicles'

        cursor.close()
        conn.close()

        return JSONResponse(content={'customers': customers})

    except Exception as e:
        logger.error(f"Error getting all customers: {e}")
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get customers: {e}'}
        )

@app.get("/agent/api/customer-chat/{agent_id}/{phone_number}")
async def get_customer_chat(agent_id: int, phone_number: str):
    """Get complete chat history for a specific customer by phone number"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"error": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get all handoffs for this phone number with this agent
        cursor.execute("""
            SELECT id, vehicle_reg, status, connected_timestamp, completed_timestamp,
                   ai_conversation_history, handoff_reason, request_timestamp
            FROM chat_handoffs
            WHERE phone_number = %s AND agent_id = %s
            ORDER BY connected_timestamp DESC
        """, (phone_number, agent_id))

        handoffs = cursor.fetchall()

        if not handoffs:
            cursor.close()
            conn.close()
            return JSONResponse(content={
                'handoffs': [],
                'all_messages': [],
                'active_handoff': None
            })

        # Get all messages from all handoffs
        handoff_ids = [h['id'] for h in handoffs]
        placeholders = ','.join(['%s'] * len(handoff_ids))

        cursor.execute(f"""
            SELECT handoff_id, sender_type, sender_name, message_text, timestamp, is_read
            FROM live_chat_messages
            WHERE handoff_id IN ({placeholders})
            ORDER BY timestamp ASC
        """, handoff_ids)

        all_messages = cursor.fetchall()

        # Find active handoff
        active_handoff = next(
            (h for h in handoffs if h['status'] == 'connected'),
            None
        )

        # Format timestamps
        for handoff in handoffs:
            if handoff.get('connected_timestamp'):
                handoff['connected_timestamp'] = handoff['connected_timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            if handoff.get('completed_timestamp'):
                handoff['completed_timestamp'] = handoff['completed_timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            if handoff.get('request_timestamp'):
                handoff['request_timestamp'] = handoff['request_timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        for msg in all_messages:
            if msg.get('timestamp'):
                msg['timestamp'] = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        cursor.close()
        conn.close()

        return JSONResponse(content={
            'handoffs': handoffs,
            'all_messages': all_messages,
            'active_handoff': active_handoff
        })

    except Exception as e:
        logger.error(f"Error getting customer chat: {e}")
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get customer chat: {e}'}
        )
    
@app.post("/agent/api/start-new-chat")
async def start_new_chat(request: Request):
    """Start a new chat with an existing customer"""

    try:
        data = await request.json()

        agent_id = data.get("agent_id")
        phone_number = data.get("phone_number")

        if not all([agent_id, phone_number]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Missing agent_id or phone_number"}
            )

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get most recent vehicle
        cursor.execute("""
            SELECT vehicle_reg FROM chat_handoffs
            WHERE phone_number = %s AND agent_id = %s AND vehicle_reg IS NOT NULL
            ORDER BY connected_timestamp DESC LIMIT 1
        """, (phone_number, agent_id))

        result = cursor.fetchone()
        vehicle_reg = result['vehicle_reg'] if result else ''

        # Create new handoff
        cursor.execute("""
            INSERT INTO chat_handoffs
            (phone_number, vehicle_reg, agent_id, status, request_timestamp, connected_timestamp, handoff_reason)
            VALUES (%s, %s, %s, 'connected', NOW(), NOW(), 'Agent initiated new chat')
        """, (phone_number, vehicle_reg, agent_id))

        new_handoff_id = cursor.lastrowid

        # Add system message
        cursor.execute("""
            INSERT INTO live_chat_messages
            (handoff_id, sender_type, sender_name, message_text, timestamp, is_read)
            VALUES (%s, 'system', 'System', %s, NOW(), 1)
        """, (new_handoff_id, 'New chat session started by agent'))

        conn.commit()
        cursor.close()
        conn.close()

        # Update session if exists
        if phone_number in bot.user_sessions:
            session = bot.user_sessions[phone_number]
            session['handoff_id'] = new_handoff_id
            session['assigned_agent_id'] = agent_id
            session['state'] = 'waiting_for_agent'

        # Notify customer
        message = "Hello! One of our service agents would like to assist you. How can we help you today?"
        bot.send_message(phone_number, message)

        return JSONResponse(content={
            'success': True,
            'handoff_id': new_handoff_id
        })

    except Exception as e:
        logger.error(f"Error starting new chat: {e}")
        return JSONResponse(
            status_code=500,
            content={'success': False, 'error': str(e)}
        )
    
@app.post("/agent/api/mark-messages-read")
async def mark_messages_read(request: Request):
    """Mark all messages as read for a customer"""

    try:
        data = await request.json()

        phone_number = data.get("phone_number")
        agent_id = data.get("agent_id")

        if not all([phone_number, agent_id]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Missing phone_number or agent_id"}
            )

        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Database connection failed"}
            )

        cursor = conn.cursor()

        # Mark all unread customer messages as read
        cursor.execute("""
            UPDATE live_chat_messages lm
            JOIN chat_handoffs h ON lm.handoff_id = h.id
            SET lm.is_read = 1
            WHERE h.phone_number = %s 
              AND h.agent_id = %s
              AND lm.sender_type = 'customer'
              AND lm.is_read = 0
        """, (phone_number, agent_id))

        conn.commit()
        cursor.close()
        conn.close()

        return JSONResponse(content={'success': True})

    except Exception as e:
        logger.error(f"Error marking messages read: {e}")
        return JSONResponse(
            status_code=500,
            content={'success': False, 'error': str(e)}
        )
    
@app.get("/agent/api/customer-full-chat/{agent_id}/{phone_number}")
async def get_customer_full_chat(agent_id: int, phone_number: str):
    """Get complete chat data for a customer - WORKING VERSION"""

    try:
        db = DatabaseManager()
        conn = db.get_connection()

        if not conn:
            return JSONResponse(
                status_code=500,
                content={"error": "Database connection failed"}
            )

        cursor = conn.cursor(dictionary=True)

        # Get most recent active or completed handoff
        cursor.execute("""
            SELECT id, vehicle_reg, status, connected_timestamp, completed_timestamp,
                   ai_conversation_history, handoff_reason, request_timestamp
            FROM chat_handoffs 
            WHERE phone_number = %s AND agent_id = %s
            ORDER BY CASE 
                WHEN status = 'connected' THEN 1
                WHEN status = 'completed' THEN 2
                ELSE 3
            END, connected_timestamp DESC
            LIMIT 1
        """, (phone_number, agent_id))

        handoff = cursor.fetchone()

        if not handoff:
            cursor.close()
            conn.close()
            return JSONResponse(content={
                'handoff': None,
                'live_messages': []
            })

        # Get live messages
        cursor.execute("""
            SELECT sender_type, sender_name, message_text, timestamp, is_read
            FROM live_chat_messages
            WHERE handoff_id = %s
            ORDER BY timestamp ASC
        """, (handoff['id'],))

        live_messages = cursor.fetchall()

        # Format timestamps
        if handoff.get('connected_timestamp'):
            handoff['connected_timestamp'] = handoff['connected_timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        if handoff.get('completed_timestamp'):
            handoff['completed_timestamp'] = handoff['completed_timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        if handoff.get('request_timestamp'):
            handoff['request_timestamp'] = handoff['request_timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        for msg in live_messages:
            if msg.get('timestamp'):
                msg['timestamp'] = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

        cursor.close()
        conn.close()

        return JSONResponse(content={
            'handoff': handoff,
            'live_messages': live_messages
        })

    except Exception as e:
        logger.error(f"Error getting customer full chat: {e}")
        return JSONResponse(
            status_code=500,
            content={'error': f'Failed to get customer chat: {e}'}
        )
# ============ EXPORT FOR WEBHOOK COMPATIBILITY ============
# Add this at the END of service.py, before if __name__ == "__main__":

def handle_service(phone_number):
    logger.info(f"🔧 handle_service called for {phone_number}")

    if phone_number not in bot.user_sessions:
        bot.user_sessions[phone_number] = {}

    bot.user_sessions[phone_number]["state"] = "SERVICE_START"

    message = (
        "Hello! 👋 Welcome to Sherpa Service Center. I'm your virtual service assistant, "
        "here to provide personalized care for your vehicle.\n\n"
        "To get started and provide you with the most relevant service options, "
        "please enter your Vehicle Registration Number below:\n\n"
        "Example: KA01AB1234"
    )

    bot.send_message(phone_number, message)
    return message


def service_flow_handler(phone, text):
    """Entry point for every service message — logs to conversation_log."""
    logger.info(f"🔧 service_flow_handler called for {phone} with text: {text}")

    bot_response = bot.process_message(phone, text)

    # ✅ Log user query + bot response to conversation_log
    try:
        session = bot.user_sessions.get(phone, {})
        vehicle_reg = session.get('vehicle_reg')
        bot.db.store_conversation(phone, text, bot_response or "", vehicle_reg)
        logger.info(f"[service] ✅ conversation_log saved | {phone}")
    except Exception as _e:
        logger.error(f"[service] ❌ conversation_log error: {_e}")

    return bot_response