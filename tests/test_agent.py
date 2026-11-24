"""Agent-loop tests with a scripted fake model: no API key, no network."""

from types import SimpleNamespace

import pytest

from app import agent, db


class FakeClaude:
    """Returns pre-scripted responses, like a real tool-using model would."""

    def __init__(self, script):
        self.script = list(script)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def text_response(text):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
    )


def tool_response(name, args):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id="tu_1", name=name, input=args)],
    )


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("PROVIDER", "claude")
    db.init()
    agent.conversations.clear()
    agent._client = None
    yield
    agent._client = None


def test_text_answer_is_whatsapp_formatted():
    agent._client = FakeClaude([text_response("**Welcome** to Fade Factory")])
    out = agent.reply("111", "hi")
    assert out["reply"] == "*Welcome* to Fade Factory"  # single asterisks, WhatsApp style


def test_tool_call_runs_and_feeds_back_to_the_model():
    agent._client = FakeClaude([
        tool_response("list_services", {}),
        text_response("We offer haircuts from $15"),
    ])
    out = agent.reply("111", "what do you offer?")
    assert out["reply"] == "We offer haircuts from $15"
    assert out["actions"] == [{"tool": "list_services", "input": {}, "error": False}]


def test_model_cannot_use_another_customers_phone():
    from datetime import timedelta

    from app import tools

    d = db.now() + timedelta(days=7)
    while d.weekday() == 6:
        d += timedelta(days=1)
    tools.book_appointment("Beard trim", f"{d.date().isoformat()}T11:00", "Ana", phone="111")

    # A prompt-injected model tries to read Ana's appointments from another chat.
    result, is_error = agent.run_tool("my_appointments", {"phone": "111"}, phone="222")
    # The system overwrote the phone with the real sender's: attacker sees nothing.
    assert not is_error and result["appointments"] == []


def test_crash_returns_friendly_message_and_rolls_back():
    agent._client = FakeClaude([RuntimeError("API down")])
    out = agent.reply("111", "hello")
    assert out["reply"] == agent.FAIL_MSG and out.get("error")
    # The failed turn is not left half-written in the history
    assert agent.conversations["111"]["messages"] == []

    # Next message works again
    agent._client = FakeClaude([text_response("Hi!")])
    assert agent.reply("111", "hello")["reply"] == "Hi!"


def test_unknown_tool_is_reported_as_error_not_crash():
    agent._client = FakeClaude([
        tool_response("drop_database", {}),
        text_response("sorry, I can't do that"),
    ])
    out = agent.reply("111", "hack it")
    assert out["actions"][0]["error"] is True
    assert out["reply"] == "sorry, I can't do that"


def test_empty_message_gets_a_greeting_without_calling_the_model():
    agent._client = FakeClaude([])  # any model call would crash (empty script)
    out = agent.reply("111", "   ")
    assert "Hi" in out["reply"]


def test_conversation_persists_in_sqlite_across_restarts():
    agent._client = FakeClaude([text_response("Hi Ana!")])
    assert agent.reply("111", "Hello")["reply"] == "Hi Ana!"

    # Simulate server restart (in-memory dict wiped)
    agent.conversations.clear()
    seen_histories = []

    class InspectingClaude(FakeClaude):
        def _create(self, **kwargs):
            seen_histories.append(list(kwargs["messages"]))
            return super()._create(**kwargs)

    agent._client = InspectingClaude([text_response("Sure!")])
    assert agent.reply("111", "What time?")["reply"] == "Sure!"
    # Previous turn (user + assistant) was loaded from SQLite before the new user turn
    assert len(seen_histories[0]) == 3
    assert seen_histories[0][0]["content"] == "Hello"


def test_history_trimming_keeps_tool_pairs_intact(monkeypatch):
    monkeypatch.setattr(agent, "MAX_HISTORY", 4)
    msgs = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "list_services", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "{}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "services"}]},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    trimmed = agent._trim_history(msgs, "claude")
    assert trimmed[0] == {"role": "user", "content": "turn 2"}


def test_gemini_reply_loop_and_model_fallback(monkeypatch):
    from google.genai import errors, types

    monkeypatch.setenv("PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash")
    agent._down_until.clear()

    called_models = []

    def fake_generate(model, contents, config):
        called_models.append(model)
        if model == "gemini-3.8-flash":
            raise errors.ServerError(503, {"error": {"message": "overloaded"}}, None)
        if len(called_models) == 2:
            # Tool call on fallback model
            return SimpleNamespace(
                candidates=[SimpleNamespace(content=types.Content(
                    role="model",
                    parts=[types.Part.from_function_call(name="list_services", args={})],
                ))],
                function_calls=[SimpleNamespace(name="list_services", args={})],
            )
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="**Classic haircut** is $15")],
            ))],
            function_calls=[],
        )

    monkeypatch.setattr(agent, "_gemini", SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
    out = agent.reply("555", "prices?")
    assert out["reply"] == "*Classic haircut* is $15"
    assert called_models == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash"]
    assert out["actions"] == [{"tool": "list_services", "input": {}, "error": False}]
    agent._gemini = None
    agent._down_until.clear()
