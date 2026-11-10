"""Webhook tests: verification handshake, signatures, deduplication."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import main, whatsapp


@pytest.fixture()
def client(monkeypatch):
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
