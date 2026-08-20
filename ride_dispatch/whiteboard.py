"""Generate whiteboard sign images via fal.ai GPT-Image-2 edit API.

NOTE: load_dotenv() is called here at module level so FAL_KEY is populated
even when this module is imported before the caller's own load_dotenv().
python-dotenv's load_dotenv() is idempotent and never overrides vars that
are already set in the real environment, so repeated calls are harmless.
"""

import base64
import hashlib
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
import httpx

load_dotenv()

logger = logging.getLogger("whiteboard")

FAL_KEY = os.environ.get("FAL_KEY", "")
BASE_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "whiteboard_base.png"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "whiteboard"

SUBMIT_URL = "https://queue.fal.run/fal-ai/gpt-image-2/edit"
POLL_INTERVAL = 3  # seconds
POLL_TIMEOUT = 180  # seconds


def is_configured() -> bool:
    return bool(FAL_KEY)


# Platforms append a VIP marker to the 乘客姓名 field, so it arrives as part of
# the stored name. Both paren widths and both character sets are matched because
# the marker's exact form is the platform's choice, not ours.
_VIP_MARKER_RE = re.compile(r"[(（]\s*(?:重要)?\s*[贵貴][宾賓]\s*[)）]")


def sanitize_name(name: str) -> str:
    """Strip the VIP marker from a passenger name for use on a whiteboard sign.

    The marker is platform metadata about the booking, not part of the person's
    name, and the sign is held up in an arrivals hall for that person to read.
    Only the 贵宾 family is removed — any other parenthetical may be part of the
    real name.
    """
    cleaned, hits = _VIP_MARKER_RE.subn("", name)
    if not hits:
        return name
    return re.sub(r"\s+", " ", cleaned).strip()


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
    # Sanitized here rather than at each call site so no path can reach the
    # image model with a name the passenger should not see on the board.
    name = sanitize_name(name)
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


# The cache holds a generated-but-not-yet-delivered image, so a dropped
# connection to Telegram does not burn the paid generation call. It is emptied
# on successful delivery: an order with no cached file regenerates, which is
# what makes re-running /board on a bad-looking board still work.


def cache_path(order_id: str, name: str, flight: str) -> Path:
    """Path of the cache slot for one order's board.

    The digest binds the file to the text written on the board, so editing the
    order's name or flight orphans the old file instead of resending a board
    that no longer matches the order.
    """
    digest = hashlib.sha1(f"{name}|{flight}".encode("utf-8")).hexdigest()[:8]
    return CACHE_DIR / f"{order_id}-{digest}.png"


def cache_store(order_id: str, name: str, flight: str, image_bytes: bytes) -> bool:
    """Cache a generated image. Returns False if it could not be written.

    Caching is an optimization for the retry path, never a precondition for
    delivery, so a failing disk must not abort the send.
    """
    path = cache_path(order_id, name, flight)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        return True
    except OSError:
        logger.warning("Whiteboard cache write failed: %s", path, exc_info=True)
        return False


def cache_load(order_id: str, name: str, flight: str) -> bytes | None:
    """Return the cached image for this order's current name/flight, or None."""
    path = cache_path(order_id, name, flight)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Whiteboard cache read failed: %s", path, exc_info=True)
        return None


def cache_discard(order_id: str, name: str, flight: str) -> None:
    """Drop the cache slot once its image has been delivered."""
    path = cache_path(order_id, name, flight)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Whiteboard cache delete failed: %s", path, exc_info=True)


def qualifies_for_prompt(order: dict) -> bool:
    """Check if an order should be offered a whiteboard sign prompt on landing.

    The "whiteboard" reminder tag records that the prompt was offered, not that
    an image exists — generation itself is behind a confirm button because every
    call costs credits.
    """
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
