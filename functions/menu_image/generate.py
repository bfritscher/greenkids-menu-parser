from google import genai
import os
from datetime import datetime

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.input_file import InputFile

def generate_image_bytes(description: str) -> bytes:
    """Generate a single square JPEG image from a textual description using Google GenAI.

    Returns raw JPEG bytes. Raises RuntimeError on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY env var")

    client = genai.Client(api_key=api_key)

    # Strongly discourage text in image and steer style
    prompt = (
        "DO NOT DRAW ANY TEXTS!\n"
        "Generate a high-quality photo of a cafeteria/cantina meal (plated, appetizing).\n"
        "Only depict food and dishware, no logos or text.\n\n"
        "Use the following items (combine sensibly, multiple small plates ok for sides):\n\n"
        f"{description.strip()}\n"
    )

    result = client.models.generate_images(
        model="models/imagen-4.0-generate-001",
        prompt=prompt,
        config=dict(
            number_of_images=1,
            output_mime_type="image/jpeg",
            person_generation="DONT_ALLOW",
            aspect_ratio="1:1",
            image_size="1K",
        ),
    )

    if not getattr(result, "generated_images", None):
        raise RuntimeError("No images generated")

    generated_image = result.generated_images[0]
    img = getattr(generated_image, "image", None)
    if img is None:
        raise RuntimeError("Image payload missing in response")

    # Prefer direct bytes provided by the SDK
    data = getattr(img, "image_bytes", None)
    if data:
        return data

    # Fallback: some backends may only support saving to a file
    try:
        import tempfile
        mime = getattr(img, "mime_type", None) or "image/jpeg"
        ext = ".jpg" if mime == "image/jpeg" else ".png" if mime == "image/png" else ".webp" if mime == "image/webp" else ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
        try:
            img.save(tmp_path)
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    except Exception as e:
        raise RuntimeError(f"Could not extract image bytes: {e}")

def _parse_event_payload(body) -> dict:
    """Assumes body is an already-parsed dict with 'description' and 'date'."""
    if not isinstance(body, dict):
        return {"description": None, "date": None}
    return {"description": body.get("description"), "date": body.get("date")}


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


def main(context):
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

    # Parse event body for description + date (assumes dict)
    payload = _parse_event_payload(getattr(context.req, "body", {}))
    description = payload.get("description")
    date_str = payload.get("date")

    if not description:
        context.error("Missing 'description' in event payload; skipping image generation")
        return context.res.send("", 204)

    try:
        file_id = _date_to_file_id(date_str)
    except Exception as e:
        context.error(str(e))
        return context.res.send("", 400)

    # Generate image
    try:
        img_bytes = generate_image_bytes(description)
    except Exception as e:
        context.error(f"Image generation failed: {e}")
        return context.res.send("", 500)

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
        return context.res.send("", 500)

    return context.res.send("", 201)


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
