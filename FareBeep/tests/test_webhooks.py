"""WEBHOOK SURFACE - Meta handshake + hmac receiver + Paystack verification."""
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


@pytest.fixture
def client(monkeypatch, session_factory):
    """FastAPI TestClient with the webhook secrets + a sqlite session patched in.

    (Real Supabase/psycopg2 is not required for the webhook layer.)
    """
    monkeypatch.setattr(main, "META_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    # keep tests hermetic: no live Gemini calls from the background handler
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    return TestClient(main.app)


def _meta_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def test_handshake_echoes_challenge(client):
    r = client.get("/webhook/meta",
                   params={"hub.mode": "subscribe",
                           "hub.verify_token": "test-verify-token",
                           "hub.challenge": "CHALLENGE_123"})
    assert r.status_code == 200
    assert r.text == "CHALLENGE_123"


def test_handshake_rejects_bad_token(client):
    r = client.get("/webhook/meta",
                   params={"hub.mode": "subscribe",
                           "hub.verify_token": "wrong",
                           "hub.challenge": "CHALLENGE_123"})
    assert r.status_code == 403


def test_receiver_rejects_bad_signature(client):
    body = b'{"entry": []}'
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


def test_receiver_accepts_valid_signature(client):
    body = (b'{"entry":[{"changes":[{"value":{'
            b'"messages":[{"from":"+2348012345678",'
            b'"text":{"body":"hello"}}]}}]}]}')
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert r.status_code == 200
    assert r.text == "200 OK"


def test_paystack_webhook_verifies_hmac_sha512(client, monkeypatch):
    # the REAL key now lives in .env - pin the module-level one so the test
    # is hermetic (setenv alone is ignored while the module key is set)
    monkeypatch.setattr("FareBeep.payments.PAYSTACK_SECRET_KEY",
                        "test-paystack-key")
    body = (b'{"event":"charge.success","data":'
            b'{"reference":"FB-UNKNOWN","status":"success"}}')
    sig = hmac.new(b"test-paystack-key", body, hashlib.sha512).hexdigest()
    r = client.post("/webhook/paystack", content=body,
                    headers={"x-paystack-signature": sig})
    assert r.status_code == 200
    assert r.json()["outcome"] == "unknown_reference"


def test_paystack_webhook_rejects_bad_signature(client):
    r = client.post("/webhook/paystack", content=b"{}",
                    headers={"x-paystack-signature": "nope"})
    assert r.status_code == 403


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_payment_status_page(client):
    r = client.get("/payment/status", params={"reference": "FB-ABC123"})
    assert r.status_code == 200
    assert "Payment received" in r.text
    assert "FB-ABC123" in r.text