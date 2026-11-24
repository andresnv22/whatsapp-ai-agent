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

import httpx

from . import db

log = logging.getLogger("whatsapp-bot")

GRAPH_VERSION = "v21.0"
MAX_WHATSAPP_CHARS = 4096


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
        log.error("WHATSAPP_APP_SECRET not set: rejecting webhook")
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# Kept for backwards compatibility in tests; persistence lives in SQLite.
_seen: dict[str, float] = {}
SEEN_TTL = 10 * 60


def is_duplicate(message_id: str) -> bool:
    return db.claim_message(message_id, SEEN_TTL)


def _extract_text(msg: dict) -> str:
    msg_type = msg.get("type")
    if msg_type == "text":
        return msg.get("text", {}).get("body", "")
    if msg_type == "interactive":
        inter = msg.get("interactive", {})
        itype = inter.get("type")
        if itype == "button_reply":
            return inter.get("button_reply", {}).get("title", "")
        if itype == "list_reply":
            return inter.get("list_reply", {}).get("title", "")
    if msg_type == "button":
        return msg.get("button", {}).get("text", "")
    return ""


def parse_incoming(payload: dict) -> list[dict]:
    """Extract text and interactive reply messages from a webhook payload.

    Meta also sends status updates (sent/delivered/read) through the same
    webhook; those have no "messages" key and are ignored here.
    """
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                text = _extract_text(msg)
                if not text:
                    continue
                out.append({
                    "id": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "text": text,
                })
    return out


def parse_unsupported(payload: dict) -> list[dict]:
    """Extract non-text messages (voice notes, images, etc.) so the bot can reply politely."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if _extract_text(msg):
                    continue
                msg_type = msg.get("type", "unknown")
                if msg_type in {"text", "interactive", "button"}:
                    continue
                out.append({
                    "id": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "type": msg_type,
                })
    return out


def chunk_text(text: str, limit: int = MAX_WHATSAPP_CHARS) -> list[str]:
    """Split long replies on paragraph/line/space boundaries under WhatsApp's 4096-char cap."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [c for c in chunks if c]


def send_text(phone: str, text: str) -> bool:
    """Send a WhatsApp message (chunked if >4096 chars). Returns False on failure."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"
    chunks = chunk_text(text)
    if not chunks:
        return True
    for part in chunks:
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {_token()}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": part},
                },
                timeout=15,
            )
            if r.status_code >= 400:
                log.error("WhatsApp send failed (%s): %s", r.status_code, r.text[:500])
                return False
        except httpx.HTTPError as e:
            log.error("WhatsApp send failed: %s", e)
            return False
    return True


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
