from google import genai
from google.genai import types
import os
from datetime import datetime
import re

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.services.databases import Databases
from appwrite.input_file import InputFile



def generate_image_bytes(description: str) -> bytes:
    """Generate a single square JPEG image from a textual description using Google GenAI.

    Returns raw JPEG bytes. Raises RuntimeError on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY env var")

    client = genai.Client(api_key=api_key, vertexai=True)

    # Strongly discourage text in image and steer style
    prompt = f"""You are an expert food photographer and stylist. Your task is to generate a photorealistic, highly appetizing image of a plated cafeteria/cantina meal based on the menu provided below.  Only depict food and dishware, no surroundings

Before generating the image, you must strictly follow these rules to avoid safety filters and ensure high quality:
1. TRANSLATE TO VISUALS: Read the provided menu (which may be in French or contain abbreviations) and internally translate it into purely visual descriptions of generic cooked food.
2. NO BRANDS OR LOGOS: Completely ignore any brand names, trademarks, or geographical abbreviations (e.g., Ebly, CH, Fr). Do not draw any packaging. Render only the generic food equivalent (e.g., render \"Ebly\" simply as \"cooked wheat grains\").
3. IGNORE SYMBOLS: Ignore all special characters, bullet points, asterisks (***), or symbols (∆). 
4. ZERO TEXT: Do strictly NOT generate any words, letters, typography, floating text, or labels anywhere in the image. Dishware, cups, and trays must be completely blank and unbranded.
5. COMPOSITION: Arrange the food sensibly using multiple small plates and bowls if needed but try to keep main meal together on one plate and split when it is needed. No Cutlery. No try. Photostudio shooting on plane light grey background. Use bright, natural, appetizing food-photography lighting.

Generate the image based on the food elements found in this menu:

{description.strip()}
    """
    generate_content_config = types.GenerateContentConfig(
        temperature = 1,
        top_p = 0.95,
        max_output_tokens = 32768,
        response_modalities = ["IMAGE"],
        safety_settings = [types.SafetySetting(
        category="HARM_CATEGORY_HATE_SPEECH",
        threshold="OFF"
        ),types.SafetySetting(
        category="HARM_CATEGORY_DANGEROUS_CONTENT",
        threshold="OFF"
        ),types.SafetySetting(
        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
        threshold="OFF"
        ),types.SafetySetting(
        category="HARM_CATEGORY_HARASSMENT",
        threshold="OFF"
        )],
        image_config=types.ImageConfig(
            aspect_ratio="1:1",
            image_size="1K",
            output_mime_type="image/jpeg",
        ),
        thinking_config=types.ThinkingConfig(
        thinking_level="HIGH",
        ),
    )

    result = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=prompt,
        config=generate_content_config
    )

    if not result.candidates or not result.candidates[0].content.parts:
        raise RuntimeError("No images generated")

    part = result.candidates[0].content.parts[0]
    if part.inline_data and part.inline_data.data:
        # inline_data.data contains the bytes
        return part.inline_data.data

    raise RuntimeError("Image payload missing in response")

def _parse_event_payload(body) -> dict:
    """Assumes body is an already-parsed dict with 'description' and 'date' or 'id'."""
    if not isinstance(body, dict):
        return {"description": None, "date": None, "id": None}
    return {
        "description": body.get("description"), 
        "date": body.get("date"),
        "id": body.get("$id", body.get("id"))
    }


def _date_to_file_id(date_str: str) -> str:
    """Convert ISO datetime string to yyyy-mm-dd safe file ID."""
    if not date_str:
        raise RuntimeError("Missing 'date' in event payload")
    # Normalize possible Z suffix
    ds = date_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ds)
    except Exception:
        # Fallback: try first 10 chars
        try:
            dt = datetime.fromisoformat(ds[:10])
        except Exception as e:
            raise RuntimeError(f"Unrecognized date format: {date_str}") from e
    return dt.strftime("%Y-%m-%d")


def _strip_afternoon_snack(text: str) -> str:
    """Remove the '4 heures' section and everything after it from the text.

    - Case-insensitive match on '4 heures'.
    - Tolerates extra spaces and non-breaking spaces.
    - Returns the text up to (but not including) the match, right-stripped.
    """
    if not isinstance(text, str):
        return text
    s = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00A0", " ")
    m = re.search(r"(?i)\b4\s*heures\b", s)
    if m:
        return s[: m.start()].rstrip()
    return s


def main(context):
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS, PUT, DELETE",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, x-appwrite-project, x-appwrite-key"
    }

    if getattr(context.req, "method", "") == "OPTIONS":
        return context.res.send("", 200, cors_headers)

    # Init Appwrite client from env + header key
    client = Client()

    if not os.environ.get('APPWRITE_FUNCTION_API_ENDPOINT') or not context.req.headers.get("x-appwrite-key"):
        raise Exception('Environment variables are not set. Function cannot use Appwrite SDK.')

    (client
        .set_endpoint(os.environ.get('APPWRITE_FUNCTION_API_ENDPOINT', None))
        .set_project(os.environ.get('APPWRITE_FUNCTION_PROJECT_ID', None))
        .set_key(context.req.headers.get("x-appwrite-key"))
    )

    storage = Storage(client)
    databases = Databases(client)

    # Parse event body for description + date (assumes dict)
    payload = _parse_event_payload(getattr(context.req, "body", {}))
    description = payload.get("description")
    date_str = payload.get("date")
    menu_id = payload.get("id")

    if menu_id and (not description or not date_str):
        # Fetch from database
        try:
            doc = databases.get_document(
                database_id="cver",
                collection_id="menu",
                document_id=menu_id
            )
            description = doc.get("description")
            date_str = doc.get("date")
        except Exception as e:
            context.error(f"Failed to fetch document {menu_id}: {e}")
            return context.res.send("", 404, cors_headers)

    if not description:
        context.error("Missing 'description' or 'id' in event payload; skipping image generation")
        return context.res.send("", 204, cors_headers)

    # Remove '4 heures' and everything after to keep only the lunch menu
    description_for_image = _strip_afternoon_snack(description)
    if not description_for_image.strip():
        context.error("Description empty after removing '4 heures'; skipping image generation")
        return context.res.send("", 204, cors_headers)

    try:
        file_id = _date_to_file_id(date_str)
    except Exception as e:
        context.error(str(e))
        return context.res.send("", 400, cors_headers)

    # Generate image
    try:
        img_bytes = generate_image_bytes(description_for_image)
    except Exception as e:
        context.error(f"Image generation failed: {e}")
        return context.res.send("", 500, cors_headers)

    # Upload to Storage bucket 'cver' with fileId yyyy-mm-dd
    try:
        input_file = InputFile.from_bytes(img_bytes, filename=f"{file_id}.jpg")
        storage.create_file(
            bucket_id="cver",
            file_id=file_id,
            file=input_file,
        )
        context.log(f"Saved image to bucket 'cver' with id {file_id}")
    except Exception as e:
        context.error(f"Failed to save image to Storage: {e}")
        return context.res.send("", 500, cors_headers)

    return context.res.send("", 201, cors_headers)


if __name__ == "__main__":
    # Local smoke test (requires GEMINI_API_KEY); writes local file for manual testing
    sample = (
        "Filet de cabillaud\nSauce chien\nBoulgour\nSalade de carottes\nMoelleux pistache et framboises"
    )
    try:
        data = generate_image_bytes(sample)
        with open("generated_image.jpg", "wb") as f:
            f.write(data)
        print("generated_image.jpg written")
    except Exception as e:
        print(f"Local generation failed: {e}")
