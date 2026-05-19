import os
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["whatsapp_bot"]
knowledge_collection = db["knowledge_base"]

def store_knowledge_in_mongo(module, filename, content, metadata=None):
    """
    Store knowledge document in MongoDB.
    module: 'insurance', 'usedcars', 'sales', etc.
    """
    document = {
        "module": module,
        "filename": filename,
        "content": content,
        "upload_timestamp": datetime.utcnow(),
        "metadata": metadata or {}
    }
    
    # Optional: Clear previous knowledge for this module and filename to avoid duplicates
    # Or keep it as a history. The user said "store in mongodb", let's keep it simple.
    result = knowledge_collection.insert_one(document)
    print(f"✅ Knowledge stored in MongoDB for {module}: {filename} (ID: {result.inserted_id})")
    return result.inserted_id

def get_knowledge_by_module(module):
    return list(knowledge_collection.find({"module": module}))

def check_knowledge_exists(module, filename):
    """
    Check if a document with the same filename exists in the given module.
    """
    return knowledge_collection.find_one({"module": module, "filename": filename}) is not None

def delete_knowledge_by_file(module, filename):
    """
    Delete a specific document from MongoDB.
    """
    result = knowledge_collection.delete_many({"module": module, "filename": filename})
    print(f"🗑️ Deleted {result.deleted_count} documents from MongoDB for {module}: {filename}")
    return result.deleted_count
