import json
import time
import re
from typing import Tuple, Optional
from pydantic import BaseModel, Field

# We use optional imports to avoid hard crashes if the client library isn't installed yet
try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

class PeriodicalMetadata(BaseModel):
    title: str = Field(
        description="The name of the magazine or publication, e.g., 'Wired' or 'The Economist'. If you cannot find a title, set to an empty string."
    )
    year: int = Field(
        description="The 4-digit year of publication, e.g., 2023. If you cannot find a year, set to 0."
    )

def get_image_mime_type(data: bytes) -> str:
    """Detect mime type based on magic numbers."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    elif data.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    elif data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/png'

def analyze_cover(image_bytes: bytes, api_key: str, model_name: str = "gemini-3.6-flash") -> Tuple[Optional[str], Optional[int]]:
    """
    Calls the Gemini Multimodal API to analyze the cover image.
    Automatically retries on 429 rate limits.
    
    Returns:
        Tuple[title, year]
    """
    if not HAS_GENAI:
        print("Warning: 'google-genai' library is not installed. Gemini cover analysis skipped.")
        return None, None
        
    client = genai.Client(api_key=api_key)
    mime_type = get_image_mime_type(image_bytes)
    
    max_retries = 3
    base_sleep = 15.0
    
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=mime_type,
                    ),
                    "Analyze this magazine cover image. Identify the name of the publication (magazine) and its publication year."
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PeriodicalMetadata,
                    temperature=0.1
                )
            )
            
            if not response.text:
                return None, None
                
            data = json.loads(response.text)
            title = data.get("title")
            year = data.get("year")
            
            # Normalize and clean output
            if title:
                title = title.strip()
                if title.lower() in ("", "unknown", "none", "untitled"):
                    title = None
                    
            if year and (1900 <= year <= 2099):
                pass
            else:
                year = None
                
            return title, year
            
        except Exception as e:
            err_msg = str(e)
            is_rate_limit = "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower()
            
            if is_rate_limit and attempt < max_retries:
                # Try parsing recommended retry wait time (e.g. "Please retry in 12.783938001s.")
                sleep_time = base_sleep
                match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_msg)
                if match:
                    sleep_time = float(match.group(1)) + 1.5  # Add 1.5s safety margin
                
                print(f"Rate limited (429). Retrying in {sleep_time:.2f} seconds... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(sleep_time)
            else:
                print(f"Error calling Gemini API: {e}")
                return None, None
