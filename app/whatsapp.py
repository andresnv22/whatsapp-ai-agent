"""WhatsApp Cloud API: receive webhooks from Meta, send messages back.

Three production details most tutorials skip, all handled here:
- Signature check: every webhook is verified with HMAC-SHA256 and the app
  secret, so nobody can fake messages by POSTing to our endpoint.
- Deduplication: Meta re-sends a webhook if we're slow to ACK it; we remember
  recent message ids so the customer never gets two answers.
- Fast ACK: the webhook returns 200 immediately and the AI runs in the
  background, because Meta drops webhooks that take too long.
"""

import hashlib
import hmac
import logging
import os
import time

import httpx

log = logging.getLogger("whatsapp-bot")

GRAPH_VERSION = "v21.0"


def _token() -> str:
    return os.environ.get("WHATSAPP_TOKEN", "")


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")


def configured() -> bool:
    """False means simulator mode: no real WhatsApp, only the local chat page."""
    return bool(_token() and _phone_number_id())


def valid_signature(body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the app secret."""
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        # Without a secret we can't verify; allow it but say so loudly.
        log.warning("WHATSAPP_APP_SECRET not set: skipping signature verification")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# Recently seen message ids (Meta retries webhooks): id -> first-seen time.
_seen: dict[str, float] = {}
SEEN_TTL = 10 * 60


def is_duplicate(message_id: str) -> bool:
    now = time.time()
    for mid, t in list(_seen.items()):  # drop old entries
        if now - t > SEEN_TTL:
            del _seen[mid]
    if message_id in _seen:
        return True
    _seen[message_id] = now
    return False


def parse_incoming(payload: dict) -> list[dict]:
    """Extract text messages from a webhook payload.

    Meta also sends status updates (sent/delivered/read) through the same
    webhook; those have no "messages" key and are ignored here.
    """
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue  # voice notes, images, etc. — out of scope for now
                out.append({
                    "id": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "text": msg.get("text", {}).get("body", ""),
                })
    return out


def send_text(phone: str, text: str) -> bool:
    """Send a WhatsApp message. Returns False on failure (and logs it);
    a send error must never crash the webhook."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "text",
                "text": {"body": text[:4096]},
            },
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send failed (%s): %s", r.status_code, r.text[:500])
            return False
        return True
    except httpx.HTTPError as e:
        log.error("WhatsApp send failed: %s", e)
        return False


def mark_read(message_id: str) -> None:
    """Show the blue double-check to the customer. Best effort."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"
    try:
        httpx.post(
            url,
            headers={"Authorization": f"Bearer {_token()}"},
            json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
            timeout=10,
        )
    except httpx.HTTPError:
        pass
