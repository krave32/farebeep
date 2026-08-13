"""TELEGRAM TEST CHANNEL - webhook (secret-token header) + Bot API transport."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base
from FareBeep.notifier import TelegramBot


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
    monkeypatch.setattr(main, "MESSAGING_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "farebeep-test-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)

    class FakeLedger:
        def __init__(self, db):
            pass

        def search(self, origin, destination, date_):
            return {"source": "ledger", "flight_date": date_ or "2026-08-14",
                    "price": 118500.0, "airline": "Air Peace",
                    "verify_link": "https://example.com/fare"}

    monkeypatch.setattr(main, "LedgerSearch", FakeLedger)

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

    fake = FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    return TestClient(main.app), fake


def _tg_post(client, path, payload, secret="farebeep-test-secret"):
    return client.post(
        path, json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_telegram_receiver_accepts_valid_secret(client):
    test_client, fake = client
    r = _tg_post(test_client, "/webhook/telegram", {
        "message": {
            "chat": {"id": 987654321},
            "text": "Lagos to Abuja tomorrow",
        }})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # chat_id (as str) IS the identity - same pipeline as a phone number
    assert fake.sent[0][0] == "987654321"


def test_telegram_receiver_rejects_bad_secret(client):
    test_client, fake = client
    r = _tg_post(test_client, "/webhook/telegram",
                 {"message": {"chat": {"id": 1}, "text": "hello"}},
                 secret="wrong-secret")
    assert r.status_code == 403
    assert fake.sent == []


def test_telegram_receiver_ignores_non_message_updates(client):
    test_client, fake = client
    r = _tg_post(test_client, "/webhook/telegram",
                 {"update_id": 1, "callback_query": {"id": "x"}})
    assert r.status_code == 200
    assert fake.sent == []


def test_telegram_send_text_calls_sendMessage(monkeypatch):
    class FakeHTTP:
        def __init__(self):
            self.calls = []

        def post(self, url, json):
            self.calls.append((url, json))
            return FakeResp({"ok": True})

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    http = FakeHTTP()
    bot = TelegramBot(token="123:abc", http_client=http)
    ok = bot.send_text("987654321", "Lagos to Abuja: NGN 118,500")
    assert ok is True
    url, payload = http.calls[0]
    assert url.endswith("/bot123:abc/sendMessage")
    assert payload == {"chat_id": "987654321",
                       "text": "Lagos to Abuja: NGN 118,500"}


def test_telegram_send_template_degrades_to_text(monkeypatch):
    class FakeHTTP:
        def post(self, url, json):
            self.payload = json
            return FakeResp({"ok": True})

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    http = FakeHTTP()
    bot = TelegramBot(token="123:abc", http_client=http)
    ok = bot.send_template("987654321", "farebeep_status",
                           body_parameters=["P47123", "DELAYED"])
    assert ok is True
    assert http.payload["text"] == "farebeep_status: P47123 DELAYED"


def test_telegram_send_text_without_token_is_safe(monkeypatch):
    bot = TelegramBot(token=None)
    assert bot.send_text("987654321", "hello") is False
