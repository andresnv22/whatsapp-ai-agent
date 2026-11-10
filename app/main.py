"""Web server: Meta's webhook on one side, the AI agent on the other.

Two ways to run it:
- Real mode: set the WHATSAPP_* variables in .env, expose /webhook with a
  public URL and configure it in Meta for Developers.
- Simulator mode: leave them empty and open http://localhost:8000 — a local
  chat page that talks to the same agent, no Meta account needed.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

load_dotenv()

from . import db, whatsapp  # noqa: E402
from .agent import conversations, missing_key, reply  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(ROOT / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("whatsapp-bot")

app = FastAPI(title="WhatsApp AI Agent")
db.init()


# ---------- Meta webhook ----------

@app.get("/webhook")
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Meta calls this once when you register the webhook URL."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge)
    raise HTTPException(403, "Verification failed")


def _process(phone: str, text: str, message_id: str) -> None:
    """Runs in the background, after the webhook already returned 200."""
    whatsapp.mark_read(message_id)
    answer = reply(phone, text)  # never raises
    whatsapp.send_text(phone, answer["reply"])


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "Bad signature")

    payload = await request.json()
    for msg in whatsapp.parse_incoming(payload):
        if not msg["phone"] or whatsapp.is_duplicate(msg["id"]):
            continue
        background.add_task(_process, msg["phone"], msg["text"], msg["id"])
    # Always 200, fast: Meta retries (and eventually disables) slow webhooks.
    return {"status": "ok"}


# ---------- Local simulator (demo without a Meta account) ----------

class SimMessage(BaseModel):
    phone: str = Field(min_length=5, max_length=30)
    text: str = Field(max_length=5000)


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.post("/sim/chat")
def sim_chat(msg: SimMessage):
    variable = missing_key()
    if variable:  # owner's configuration problem, not the customer's
        raise HTTPException(500, f"Missing {variable} in .env")
    return reply(msg.phone.strip(), msg.text)


@app.post("/sim/reset/{phone}")
def sim_reset(phone: str):
    conversations.pop(phone, None)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "whatsapp_configured": whatsapp.configured()}
