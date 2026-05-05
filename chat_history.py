# chat_history.py

from pymongo import MongoClient, DESCENDING, ASCENDING
from datetime import datetime
import os
from bson import ObjectId
from typing import Optional

# ===============================
# MongoDB Connection
# ===============================

MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI)
db = client["whatsapp_bot"]
collection = db["chat_sessions"]


# ===============================
# Create New Chat Session
# ===============================

def start_chat_session(user_phone, platform="whatsapp"):
    session = {
        "user_phone": user_phone,
        "platform": platform,
        "start_time": datetime.utcnow(),
        "end_time": None,
        "status": "active",
        "messages": [],
        "summary": {
            "searched_cars": [],
            "insurance_renewal": False,
            "lead_type": None
        }
    }

    result = collection.insert_one(session)
    return result.inserted_id


# ===============================
# Add Message To Session
# ===============================

def add_message(session_id, sender, text, intent=None, meta=None):
    message = {
        "sender": sender,         # "user" or "bot"
        "text": text,
        "intent": intent,
        "ts": datetime.utcnow()
    }

    if meta:
        message["meta"] = meta

    collection.update_one(
        {"_id": session_id},
        {"$push": {"messages": message}}
    )


# ===============================
# Update Summary
# ===============================

def update_summary(session_id, summary_updates: dict):
    update_query = {}

    for key, value in summary_updates.items():
        update_query[f"summary.{key}"] = value

    collection.update_one(
        {"_id": session_id},
        {"$set": update_query}
    )


# ===============================
# Close Chat Session
# ===============================

def close_chat_session(session_id):
    collection.update_one(
        {"_id": session_id},
        {
            "$set": {
                "status": "closed",
                "end_time": datetime.utcnow()
            }
        }
    )


# ================================================================
# NEW: Get All Sessions (for frontend listing)
# Supports filter by phone, status, lead_type, date range
# ================================================================

def get_all_sessions(
    phone=None,
    status=None,
    lead_type=None,
    date_from=None,
    date_to=None,
    limit=50,
    skip=0
):
    """
    Fetch all chat sessions with optional filters.
    Returns list of sessions sorted by start_time DESC (newest first).
    """
    query = {}

    if phone:
        query["user_phone"] = {"$regex": phone.strip(), "$options": "i"}

    if status:
        query["status"] = status

    if lead_type:
        query["summary.lead_type"] = lead_type

    date_filter = {}
    if date_from:
        date_filter["$gte"] = date_from
    if date_to:
        date_filter["$lte"] = date_to
    if date_filter:
        query["start_time"] = date_filter

    sessions = list(
        collection.find(query)
        .sort("start_time", DESCENDING)
        .skip(skip)
        .limit(limit)
    )

    for s in sessions:
        s["_id"] = str(s["_id"])
        s["message_count"] = len(s.get("messages", []))
        msgs = s.get("messages", [])
        s["last_message"] = msgs[-1] if msgs else None
        if s["last_message"] and "ts" in s["last_message"]:
            s["last_message"]["ts"] = s["last_message"]["ts"].isoformat()
        del s["messages"]
        if s.get("start_time"):
            s["start_time"] = s["start_time"].isoformat()
        if s.get("end_time"):
            s["end_time"] = s["end_time"].isoformat()

    total = collection.count_documents(query)

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "sessions": sessions
    }


# ================================================================
# NEW: Get Single Session Chat (WhatsApp-style messages)
# ================================================================

def get_session_chat(
    session_id,
    sender_filter=None,
    keyword=None,
    date_from=None,
    date_to=None
):
    """
    Fetch a single session with all its messages.
    Messages formatted like a WhatsApp chat - user on right, bot on left.
    Supports filter by sender, keyword, date range.
    """
    try:
        oid = ObjectId(session_id)
    except Exception:
        return {"error": "Invalid session_id"}

    session = collection.find_one({"_id": oid})
    if not session:
        return {"error": "Session not found"}

    messages = session.get("messages", [])

    filtered = []
    for msg in messages:
        if sender_filter and msg.get("sender") != sender_filter:
            continue

        if keyword:
            if keyword.lower() not in (msg.get("text") or "").lower():
                continue

        ts = msg.get("ts")
        if ts:
            if date_from and ts < date_from:
                continue
            if date_to and ts > date_to:
                continue

        msg_copy = dict(msg)
        if msg_copy.get("ts"):
            msg_copy["ts"] = msg_copy["ts"].isoformat()

        # WhatsApp-style side: user = right, bot = left
        msg_copy["side"] = "right" if msg_copy.get("sender") == "user" else "left"

        filtered.append(msg_copy)

    return {
        "session_id": session_id,
        "user_phone": session.get("user_phone"),
        "status": session.get("status"),
        "start_time": session["start_time"].isoformat() if session.get("start_time") else None,
        "end_time": session["end_time"].isoformat() if session.get("end_time") else None,
        "summary": session.get("summary", {}),
        "total_messages": len(messages),
        "filtered_count": len(filtered),
        "messages": filtered
    }


# ================================================================
# NEW: Get Session Stats (for dashboard)
# ================================================================

def get_session_stats():
    """Quick stats for dashboard - total, active, closed, by lead type."""
    total = collection.count_documents({})
    active = collection.count_documents({"status": "active"})
    closed = collection.count_documents({"status": "closed"})

    pipeline = [
        {"$group": {"_id": "$summary.lead_type", "count": {"$sum": 1}}}
    ]
    lead_breakdown = {
        doc["_id"] or "unknown": doc["count"]
        for doc in collection.aggregate(pipeline)
    }

    return {
        "total": total,
        "active": active,
        "closed": closed,
        "by_lead_type": lead_breakdown
    }