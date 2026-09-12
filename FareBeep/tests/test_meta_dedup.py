"""META WEBHOOK BRIDGE - save-before-ack dedup, full-payload fan-out, FIFO.

Covers the wiring decisions for classify_message -> main.meta_webhook:
  - duplicate wamids are processed exactly once (booking safety)
  - every message in a payload is handled, not just messages[0]
  - one customer's messages run in timestamp (FIFO) order
  - claims survive restarts (row in processed_messages = durable dedup)
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, ProcessedMessage


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setattr(main, "META_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    # No network from the background batch: stub the typing bubble.
    monkeypatch.setattr(main, "MetaWhatsapp",
                        lambda *a, **k: type("W", (), {
                            "send_typing_indicator": staticmethod(
                                lambda to: True)})())
    return TestClient(main.app)


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _payload(messages):
    return {"entry": [{"changes": [{"value": {"messages": messages}}]}]}


def _text_msg(mid, phone, body, ts):
    return {"id": mid, "from": phone, "timestamp": str(ts),
            "type": "text", "text": {"body": body}}


def _post(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhook/meta", content=body,
                       headers={"X-Hub-Signature-256": _sig(body)})


def test_duplicate_wamid_processed_once(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    payload = _payload([_text_msg("wamid-dup1", "+234801", "hello", 1)])
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200  # Meta retry
    assert calls == [("+234801", "hello")]


def test_all_messages_in_payload_processed(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    payload = _payload([
        _text_msg("wamid-m1", "+234801", "first", 1),
        _text_msg("wamid-m2", "+234801", "second", 2),
    ])
    r = _post(client, payload)
    assert r.status_code == 200
    assert [t for _, t in calls] == ["first", "second"]


def test_same_customer_fifo_order(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append(text))
    # Arrive out of order on the wire - batch must still run FIFO by ts.
    payload = _payload([
        _text_msg("wamid-o3", "+234801", "third", 3),
        _text_msg("wamid-o1", "+234801", "first", 1),
        _text_msg("wamid-o2", "+234801", "second", 2),
    ])
    assert _post(client, payload).status_code == 200
    assert calls == ["first", "second", "third"]


def test_claim_survives_restart(client, monkeypatch, session_factory):
    """A claimed wamid stays claimed across processes: pre-insert the row
    (as a crashed worker would have left it), then the retry is dropped."""
    db = session_factory()
    db.add(ProcessedMessage(message_id="wamid-old", phone="+234801",
                            message_type="text", status="done"))
    db.commit()
    db.close()
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    payload = _payload([_text_msg("wamid-old", "+234801", "hello again", 9)])
    assert _post(client, payload).status_code == 200
    assert calls == []


def test_messages_without_id_still_processed(client, monkeypatch):
    """Legacy payloads with no wamid (see test_webhooks) keep working -
    they bypass dedup but must reach the concierge."""
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    payload = _payload([{"from": "+234801",
                         "text": {"body": "hello"}}])
    r = _post(client, payload)
    assert r.status_code == 200
    assert r.text == "200 OK"
    assert calls == [("+234801", "hello")]
