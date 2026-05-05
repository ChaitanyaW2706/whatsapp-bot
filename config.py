# config.py
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ===============================
# WhatsApp / Meta Config
# ===============================
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

# ===============================
# Database Config
# ===============================
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME")
}

# ===============================
# Global User State (UNCHANGED)
# ===============================
USER_STATE = {}
BASE_URL = os.getenv("BASE_URL")
