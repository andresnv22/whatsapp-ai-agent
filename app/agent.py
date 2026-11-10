"""The AI agent: an agentic loop over Claude with the barbershop tools.

1. Send the conversation + tools to the model.
2. If it answers with text, we're done.
3. If it asks for a tool, run it, append the result, go back to 1.

Golden rule: the customer never sees a technical error. If anything breaks,
they get a friendly message, the conversation rolls back to the previous
turn, and the details go to the log.
"""

import json
import logging
import os
import re
import threading
import time

from . import db
from .tools import FUNCTIONS, NEED_PHONE, TOOLS

log = logging.getLogger("whatsapp-bot")

PROVIDER = os.environ.get("PROVIDER", "claude").lower()
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# If a Gemini model is overloaded, the next one in the list is tried
GEMINI_MODELS = list(dict.fromkeys([GEMINI_MODEL, "gemini-3.5-flash"]))
MAX_STEPS = 10          # safety cap so the loop can't run forever
IDLE_RESET = 6 * 60 * 60  # seconds of silence before a fresh conversation
MAX_MESSAGE_LEN = 2000

FAIL_MSG = "Sorry, I hit a technical problem 🙏 Could you send that again in a minute?"
REFUSED_MSG = "Sorry, I can't help with that. Shall I book you an appointment?"
CONFUSED_MSG = "Sorry, I got confused. Could you say that another way?"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

SYSTEM = """You are Max, the virtual assistant of Fade Factory, a barbershop. You chat on WhatsApp.

Your job: book, check and cancel appointments, and answer questions about services and prices.

How you work:
- Write short and friendly, WhatsApp style. No long lists.
- WhatsApp formatting: bold with single asterisks (*like this*), never ** or # headings.
- Never invent prices or time slots: call check_availability or list_services first.
- Before booking you need: service, date, time and the customer's full name.
- The system already knows the customer's phone number: never ask for it.
- Before cancelling, confirm which appointment.
- If a tool returns an error, explain it in plain words and offer an alternative. Never show technical messages.
- If asked about anything unrelated to the barbershop, redirect politely.
- Reply in the customer's language (Spanish or English).

Hours: Monday to Saturday, 9:00 to 19:00."""


class Refused(Exception):
    """The model declined to answer (provider safety filter)."""


# In-memory conversations: phone -> {"messages": [...], "last": timestamp}.
# For production this would live in the database.
conversations: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_client = None  # tests inject a fake client here


def _lock(phone: str) -> threading.Lock:
    """One lock per customer: two quick messages are handled in order."""
    with _locks_guard:
        return _locks.setdefault(phone, threading.Lock())


def missing_key() -> str | None:
    variable = "GEMINI_API_KEY" if PROVIDER == "gemini" else "ANTHROPIC_API_KEY"
    value = os.environ.get(variable, "")
    return variable if not value or value.startswith("paste-your") else None


def _system() -> str:
    t = db.now()
    return f"{SYSTEM}\n\nToday is {DAYS[t.weekday()]} {t.date().isoformat()}, the time is {t:%H:%M}."


def run_tool(name: str, args: dict, phone: str) -> tuple[dict, bool]:
    fn = FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"No tool named '{name}'."}, True
    args = {k: v for k, v in args.items() if k != "phone"}  # the system sets the phone
    if name in NEED_PHONE:
        args["phone"] = phone
    try:
        result = fn(**args)
    except TypeError as e:
        return {"error": f"Invalid parameters: {e}"}, True
    except Exception:
        log.exception("Tool %s failed with %s", name, args)
        return {"error": "Internal tool error. Try again."}, True
    return result, "error" in result


def whatsapp_format(text: str) -> str:
    """WhatsApp bold is *single*, not **double**; headings don't exist."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def _final_text(text: str, actions: list) -> str:
    if text.strip():
        return whatsapp_format(text)
    did_something = any(not a["error"] for a in actions)
    return "Done 👍 Anything else?" if did_something else CONFUSED_MSG


def reply(phone: str, text: str) -> dict:
    """Takes a customer message, returns the bot's answer. Never raises."""
    text = text.strip()[:MAX_MESSAGE_LEN]
    if not text:
        return {"reply": "Hi! How can I help you? 😊", "actions": []}

    with _lock(phone):
        conv = conversations.get(phone)
        if conv is None or time.time() - conv["last"] > IDLE_RESET:
            conv = conversations[phone] = {"messages": [], "last": time.time()}
        messages = conv["messages"]
        before = len(messages)  # to undo this turn if something fails
        actions: list = []

        try:
            reply_with = _gemini_reply if PROVIDER == "gemini" else _claude_reply
            answer = reply_with(text, messages, actions, phone)
        except Refused:
            del messages[before:]
            return {"reply": REFUSED_MSG, "actions": actions}
        except Exception:
            del messages[before:]
            log.exception("Reply failed for %s", phone)
            return {"reply": FAIL_MSG, "actions": actions, "error": True}

        conv["last"] = time.time()
        if answer is None:  # ran out of steps
            del messages[before:]
            return {"reply": CONFUSED_MSG, "actions": actions}
        return {"reply": _final_text(answer, actions), "actions": actions}


def _claude_reply(text: str, messages: list, actions: list, phone: str) -> str | None:
    import anthropic

    global _client
    _client = _client or anthropic.Anthropic(timeout=60.0)  # a chat can't wait 10 minutes

    messages.append({"role": "user", "content": text})
    for _ in range(MAX_STEPS):
        response = _client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=_system(),
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise Refused()
        # Keep the full response (not just the text) so the context stays valid
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            output, is_error = run_tool(block.name, block.input, phone)
            actions.append({"tool": block.name, "input": block.input, "error": is_error})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output, ensure_ascii=False),
                "is_error": is_error,
            })
        messages.append({"role": "user", "content": results})
    return None


# ---------- Gemini ----------

_gemini = None
RETRY_AFTER = 5 * 60  # seconds to skip a model after it fails
_down_until: dict[str, float] = {}  # model -> skip it until this time


def _gemini_generate(messages, config):
    """Try the models in order; if one is overloaded or slow, move on."""
    import httpx
    from google.genai import errors, types

    def with_time(seconds: int, attempts: int):
        return config.model_copy(update={"http_options": types.HttpOptions(
            timeout=seconds * 1000, retry_options=types.HttpRetryOptions(attempts=attempts),
        )})

    # The first models get little time and no retries: if they're overloaded,
    # moving on quickly beats keeping the customer waiting.
    impatient, patient = with_time(15, 1), with_time(40, 2)
    available = [m for m in GEMINI_MODELS if time.time() >= _down_until.get(m, 0)]
    available = available or GEMINI_MODELS[-1:]  # all down: still try the last one

    for i, model in enumerate(available):
        last = i == len(available) - 1
        try:
            return _gemini.models.generate_content(
                model=model, contents=messages, config=patient if last else impatient
            )
        except (errors.ServerError, errors.ClientError, httpx.TimeoutException) as e:
            # 5xx or timeout = overloaded; 429 = that model's quota ran out
            if isinstance(e, errors.ClientError) and e.code != 429:
                raise
            log.warning("Gemini %s unavailable (%s), trying the next one", model, e)
            _down_until[model] = time.time() + RETRY_AFTER
            if last:
                raise


def _gemini_reply(text: str, messages: list, actions: list, phone: str) -> str | None:
    from google import genai
    from google.genai import types

    global _gemini
    _gemini = _gemini or genai.Client()  # reads GEMINI_API_KEY

    config = types.GenerateContentConfig(
        system_instruction=_system(),
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters_json_schema=t["input_schema"],
            )
            for t in TOOLS
        ])],
        # We run the loop ourselves, same as with Claude
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level="low"),  # simple chat: fast
    )

    messages.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
    for _ in range(MAX_STEPS):
        response = _gemini_generate(messages, config)
        if not response.candidates or response.candidates[0].content is None:
            raise Refused()  # blocked by the safety filter

        content = response.candidates[0].content
        messages.append(content)

        if not response.function_calls:
            return "".join(p.text for p in content.parts or [] if p.text and not p.thought)

        parts = []
        for call in response.function_calls:
            args = dict(call.args or {})
            output, is_error = run_tool(call.name, args, phone)
            actions.append({"tool": call.name, "input": args, "error": is_error})
            parts.append(types.Part.from_function_response(name=call.name, response=output))
        messages.append(types.Content(role="user", parts=parts))
    return None
