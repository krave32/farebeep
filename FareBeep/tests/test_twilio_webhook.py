"""TWILIO TEST CHANNEL - sandbox webhook (form + signature) + provider factory."""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from twilio.request_validator import RequestValidator

from FareBeep import main
from FareBeep.models import Base
from FareBeep.notifier import TwilioWhatsapp


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
    monkeypatch.setattr(main, "MESSAGING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-twilio-token")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

    fake = FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    return TestClient(main.app), fake


def _twilio_post(client, path, params, auth_token="test-twilio-token"):
    """POST form-encoded with a VALID Twilio signature header."""
    url = f"http://testserver{path}"
    signature = RequestValidator(auth_token).compute_signature(url, params)
    return client.post(path, data=params,
                       headers={"X-Twilio-Signature": signature})


def test_twilio_receiver_accepts_valid_signature(client):
    test_client, fake = client
    r = _twilio_post(test_client, "/webhook/twilio",
                     {"From": "whatsapp:+2348012345678", "Body": "hello",
                      "To": "whatsapp:+14155238886"})
    assert r.status_code == 200
    assert "Response" in r.text
    # the phone was normalized (whatsapp: prefix stripped) and handled
    assert fake.sent[0][0] == "+2348012345678"


def test_twilio_receiver_rejects_bad_signature(client):
    test_client, fake = client
    r = test_client.post(
        "/webhook/twilio",
        data={"From": "whatsapp:+2348012345678", "Body": "hello",
              "To": "whatsapp:+14155238886"},
        headers={"X-Twilio-Signature": "garbage"})
    assert r.status_code == 403
    assert fake.sent == []


def test_twilio_send_text_uses_whatsapp_recipient(monkeypatch):
    class FakeClient:
        def __init__(self, sid, token):
            self.messages = FakeMessages()

    class FakeMessages:
        def __init__(self):
            self.created = []

        def create(self, from_, to, body):
            self.created.append((from_, to, body))
            return {"sid": "SM123"}

    fake_client = FakeClient("sid", "tok")
    tw = TwilioWhatsapp(account_sid="sid", auth_token="tok",
                        from_whatsapp="whatsapp:+14155238886",
                        client=fake_client)
    ok = tw.send_text("+2348012345678", "hello")
    assert ok is True
    assert fake_client.messages.created[0] == (
        "whatsapp:+14155238886", "whatsapp:+2348012345678", "hello")


def test_twilio_send_template_degrades_to_text(monkeypatch):
    class FakeClient:
        def __init__(self, sid, token):
            self.messages = FakeMessages()

    class FakeMessages:
        def __init__(self):
            self.created = []

        def create(self, from_, to, body):
            self.created.append((from_, to, body))
            return {"sid": "SM123"}

    tw = TwilioWhatsapp(account_sid="sid", auth_token="tok",
                        from_whatsapp="whatsapp:+14155238886",
                        client=FakeClient("sid", "tok"))
    ok = tw.send_template("+2348012345678", "farebeep_status",
                          body_parameters=["P47123", "DELAYED"])
    assert ok is True
    assert "farebeep_status: P47123 DELAYED" in \
        tw._client.messages.created[0][2]