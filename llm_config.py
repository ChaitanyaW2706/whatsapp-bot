import os
import json
from groq import Groq
import groq
from dotenv import load_dotenv

import google.generativeai as genai

# Load environment variables if not already loaded
load_dotenv()

# Centralized Model Name
MODEL_NAME = os.getenv("GROQ_MODEL_NAME", "llama-3.3-70b-versatile")
SECONDARY_MODEL_NAME = "llama-3.1-8b-instant"

# Centralized Groq Client Instance
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

# Gemini Fallback Client Instance
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    
def _convert_messages_for_gemini(messages: list) -> str:
    """Convert OpenAI/Groq message list to a flat string for Gemini fallback."""
    flat = []
    for m in messages:
        role = m.get("role", "user").upper()
        content = m.get("content", "")
        flat.append(f"[{role}]: {content}")
    return "\n\n".join(flat)

def smart_llm_call(messages: list, temperature: float = 0.1, max_tokens: int = 800, response_format: dict = None, as_json: bool = False) -> str | dict:
    """
    Robust wrapper that tries Groq primary, Groq secondary, then Gemini.
    """
    # Attempt 1: Primary Groq Model
    try:
        response = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format
        )
        content = response.choices[0].message.content.strip()
        return json.loads(content) if as_json else content
    except Exception as e:
        print(f"[llm] Primary Groq error ({MODEL_NAME}): {e}")
        
    # Attempt 2: Secondary Groq Model
    try:
        print(f"[llm] Falling back to secondary Groq model: {SECONDARY_MODEL_NAME}")
        response = groq_client.chat.completions.create(
            model=SECONDARY_MODEL_NAME,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format
        )
        content = response.choices[0].message.content.strip()
        return json.loads(content) if as_json else content
    except Exception as e:
        print(f"[llm] Secondary Groq error ({SECONDARY_MODEL_NAME}): {e}")

    # Attempt 3: Ultimate Fallback to Gemini
    print(f"[llm] Ultimate fallback to Gemini model: gemini-1.5-flash-latest")
    if not GEMINI_API_KEY:
        raise Exception("All Groq models failed and GEMINI_API_KEY is not set.")
        
    gemini_model = genai.GenerativeModel("gemini-1.5-flash-latest")
    prompt = _convert_messages_for_gemini(messages)
    
    # Configure generation
    gen_config = genai.GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json" if as_json or (response_format and response_format.get("type") == "json_object") else "text/plain"
    )
    
    response = gemini_model.generate_content(prompt, generation_config=gen_config)
    content = response.text.strip()
    return json.loads(content) if as_json else content
