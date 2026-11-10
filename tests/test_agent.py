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
