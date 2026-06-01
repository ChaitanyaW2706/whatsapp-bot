import os
from groq import Groq
from dotenv import load_dotenv

# Load environment variables if not already loaded
load_dotenv()

# Centralized Model Name
# Pull from environment or fallback to the versatile model
MODEL_NAME = os.getenv("GROQ_MODEL_NAME", "llama-3.3-70b-versatile")

# Centralized Groq Client Instance
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)
