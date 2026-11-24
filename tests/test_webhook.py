"""Webhook tests: verification handshake, signatures, deduplication."""

import hashlib
import hmac
import json

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent, db, main, whatsapp


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    agent.conversations.clear()
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "my-verify-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    whatsapp._seen.clear()
    return TestClient(main.app)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()


def incoming(msg_id="wamid.1", text="hello", phone="573001112233") -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [
        {"id": msg_id, "from": phone, "type": "text", "text": {"body": text}}
    ]}}]}]}


def test_meta_verification_handshake(client):
    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "my-verify-token", "hub.challenge": "42",
    })
    assert r.status_code == 200 and r.text == "42"


def test_wrong_verify_token_is_rejected(client):
    r = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42",
    })
    assert r.status_code == 403


def test_forged_signature_is_rejected(client):
    body = json.dumps(incoming()).encode()
    r = client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json",
    })
    assert r.status_code == 403


def test_webhook_is_rejected_when_secret_is_missing(client, monkeypatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    body = json.dumps(incoming()).encode()
    r = client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": sign(body), "Content-Type": "application/json",
    })
    assert r.status_code == 403


def test_valid_message_is_processed_once(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_process", lambda phone, text, mid: calls.append((phone, text)))

    body = json.dumps(incoming(text="book me a haircut")).encode()
    headers = {"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}

    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    # Meta retries the same webhook: the duplicate must be ignored
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    assert calls == [("573001112233", "book me a haircut")]


def test_status_updates_are_ignored(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_process", lambda *a: calls.append(a))
    body = json.dumps({"entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.1", "status": "delivered"}]
    }}]}]}).encode()
    r = client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": sign(body), "Content-Type": "application/json",
    })
    assert r.status_code == 200 and calls == []


def test_parse_ignores_non_text_messages():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "a", "from": "1", "type": "image", "image": {}},
        {"id": "b", "from": "2", "type": "text", "text": {"body": "hi"}},
    ]}}]}]}
    msgs = whatsapp.parse_incoming(payload)
    assert len(msgs) == 1 and msgs[0]["text"] == "hi"


def test_parse_extracts_interactive_replies():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {
            "id": "wamid.int1",
            "from": "573001112233",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "svc_1", "title": "Classic haircut"}},
        }
    ]}}]}]}
    msgs = whatsapp.parse_incoming(payload)
    assert msgs == [{"id": "wamid.int1", "phone": "573001112233", "text": "Classic haircut"}]


def test_unsupported_media_gets_polite_reply(client, monkeypatch):
    replies = []
    monkeypatch.setattr(main, "_reply_unsupported", lambda phone, mid, mtype: replies.append((phone, mtype)))
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.aud1", "from": "573001112233", "type": "audio", "audio": {}}
    ]}}]}]}).encode()
    r = client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": sign(body), "Content-Type": "application/json",
    })
    assert r.status_code == 200
    assert replies == [("573001112233", "audio")]


def test_send_text_chunks_long_messages(monkeypatch):
    sent = []

    def fake_post(url, headers, json, timeout):
        sent.append(json["text"]["body"])
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")

    long_msg = ("Hello world\n" * 500).strip()
    assert whatsapp.send_text("573001112233", long_msg) is True
    assert len(sent) >= 2
    assert all(len(part) <= whatsapp.MAX_WHATSAPP_CHARS for part in sent)


def test_simulator_and_appointments_endpoints(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    monkeypatch.setenv("PROVIDER", "claude")
    agent._client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Hello from *Fade Factory*")],
        )
    ))

    r = client.post("/sim/chat", json={"phone": "sim-12345", "text": "hi"})
    assert r.status_code == 200 and r.json()["reply"] == "Hello from *Fade Factory*"

    assert client.get("/sim/appointments").status_code == 200
    assert client.post("/sim/reset/sim-12345").status_code == 200
    assert client.get("/appointments").status_code == 503

    monkeypatch.setenv("ADMIN_TOKEN", "secret-admin")
    assert client.get("/appointments").status_code == 401
    assert client.get("/appointments", headers={"Authorization": "Bearer secret-admin"}).status_code == 200
    assert client.get("/health").json() == {"ok": True, "whatsapp_configured": False}
    agent._client = None
