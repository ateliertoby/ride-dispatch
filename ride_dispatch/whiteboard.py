"""Generate whiteboard sign images via fal.ai GPT-Image-2 edit API.

NOTE: load_dotenv() is called here at module level so FAL_KEY is populated
even when this module is imported before the caller's own load_dotenv().
python-dotenv's load_dotenv() is idempotent and never overrides vars that
are already set in the real environment, so repeated calls are harmless.
"""

import base64
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
import httpx

load_dotenv()

logger = logging.getLogger("whiteboard")

FAL_KEY = os.environ.get("FAL_KEY", "")
BASE_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "whiteboard_base.png"

SUBMIT_URL = "https://queue.fal.run/fal-ai/gpt-image-2/edit"
POLL_INTERVAL = 3  # seconds
POLL_TIMEOUT = 180  # seconds


def is_configured() -> bool:
    return bool(FAL_KEY)


def build_prompt(name: str, flight: str) -> str:
    return (
        "Change ONLY the handwritten text on the whiteboard. "
        "Do NOT alter anything outside the whiteboard — the airport, gate signs, "
        "people, lighting must be pixel-identical. Write two lines in messy thick "
        "black marker handwriting with natural imperfections — wobbly baselines, "
        "inconsistent letter sizes, visible pen pressure variation, slightly tilted "
        "characters as if written quickly by hand. First line: "
        f"'{name}' in large text. Second line: '{flight}' in medium text. "
        "IMPORTANT: write every line EXACTLY as given — do not translate, "
        "transliterate, or change any text."
    )


def _build_data_uri(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_payload(name: str, flight: str, image_data_uri: str) -> dict:
    return {
        "prompt": build_prompt(name, flight),
        "image_urls": [image_data_uri],
        "image_size": {"width": 1024, "height": 768},
        "quality": "low",
        "output_format": "png",
        "num_images": 1,
    }


def _headers() -> dict:
    return {"Authorization": f"Key {FAL_KEY}"}


class WhiteboardError(Exception):
    pass


async def generate(name: str, flight: str) -> bytes:
    """Generate a whiteboard sign image. Returns PNG bytes.

    Raises WhiteboardError on timeout, API failure, or missing image.
    """
    base_bytes = BASE_IMAGE_PATH.read_bytes()
    data_uri = _build_data_uri(base_bytes)
    payload = _build_payload(name, flight, data_uri)

    async with httpx.AsyncClient(timeout=30) as client:
        # Submit job
        resp = await client.post(SUBMIT_URL, json=payload, headers=_headers())
        if resp.status_code != 200:
            raise WhiteboardError(f"fal submit failed: {resp.status_code} {resp.text[:200]}")
        submit_data = resp.json()

        status_url = submit_data.get("status_url")
        response_url = submit_data.get("response_url")
        if not status_url or not response_url:
            raise WhiteboardError(f"fal submit missing URLs: {submit_data}")

        # Poll until completed
        import asyncio
        elapsed = 0
        while elapsed < POLL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            poll_resp = await client.get(status_url, headers=_headers())
            code = poll_resp.status_code

            if code in (200, 202):
                poll_data = poll_resp.json()
                status = poll_data.get("status")
                if status == "COMPLETED":
                    break
                if status in ("IN_QUEUE", "IN_PROGRESS"):
                    continue
                # 202 with no recognizable status — still pending
                if code == 202:
                    continue
                raise WhiteboardError(f"fal job terminal status: {status}")
            elif 400 <= code < 500:
                raise WhiteboardError(
                    f"fal poll client error: {code} {poll_resp.text[:200]}"
                )
            else:
                # 5xx — transient; timeout is the backstop
                logger.warning("fal poll returned %d", code)
                continue
        else:
            raise WhiteboardError(f"fal job timed out after {POLL_TIMEOUT}s")

        # Fetch result
        result_resp = await client.get(response_url, headers=_headers())
        if result_resp.status_code != 200:
            raise WhiteboardError(f"fal result fetch failed: {result_resp.status_code}")
        result_data = result_resp.json()

        images = result_data.get("images", [])
        if not images or not images[0].get("url"):
            raise WhiteboardError(f"fal result missing image: {result_data}")

        image_url = images[0]["url"]

        # Download image bytes
        img_resp = await client.get(image_url)
        if img_resp.status_code != 200:
            raise WhiteboardError(f"image download failed: {img_resp.status_code}")
        return img_resp.content


def qualifies_for_auto(order: dict) -> bool:
    """Check if an order qualifies for automatic whiteboard generation on landing."""
    if order.get("service_type") != "接机":
        return False
    if "举牌" not in (order.get("additional_services") or ""):
        return False
    reminders = order.get("reminders_sent") or ""
    if "whiteboard" in reminders.split(","):
        return False
    if not is_configured():
        return False
    return True
