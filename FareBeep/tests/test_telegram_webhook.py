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
    monkeypatch.setattr(main, "GROQ_API_KEY", None)  # deterministic path
    # hermetic typing bubble: TELEGRAM_BOT_TOKEN is set in FareBeep/.env,
    # so the webhook's best-effort typing call would hit the real API
    monkeypatch.setattr(
        "FareBeep.notifier.TelegramBot",
        lambda *a, **k: type("T", (), {"send_action": staticmethod(
            lambda to, action="typing": True)})())

    class FakeLedger:
        def __init__(self, db):
            pass

        def search(self, origin, destination, date_):
            return {"source": "ledger", "flight_date": date_ or "2026-08-14",
                    "price": 118500.0, "airline": "Air Peace",
                    "verify_link": "https://example.com/fare"}

        def search_list(self, origin, destination, date_, limit=3):
            f = {"source": "ledger", "flight_date": date_ or "2026-08-14",
                 "price": 118500.0, "airline": "Air Peace",
                 "verify_link": "https://example.com/fare"}
            return [f], []

    monkeypatch.setattr(main, "LedgerSearch", FakeLedger)

    class FakeNotifier:
        def __init__(self):
            self.sent = []
            self.acks = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

        def answer_callback(self, callback_id, text=None):
            self.acks.append((callback_id, text))
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
    """callback_query updates are ACKed but (with no message.chat and no
    data) dispatch nothing - the ack still goes out via answer_callback."""
    test_client, fake = client
    r = _tg_post(test_client, "/webhook/telegram",
                 {"update_id": 1, "callback_query": {"id": "x"}})
    assert r.status_code == 200
    assert fake.sent == []
    assert fake.acks == [("x", None)]


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


def test_telegram_send_action_posts_typing(monkeypatch):
    class FakeHTTP:
        def post(self, url, json):
            self.url = url
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
    assert bot.send_action("987654321") is True
    assert http.url.endswith("/sendChatAction")
    assert http.payload == {"chat_id": "987654321", "action": "typing"}


def test_telegram_send_action_without_token_is_safe(monkeypatch):
    bot = TelegramBot(token=None)
    assert bot.send_action("987654321") is False


def test_telegram_send_action_failure_never_raises(monkeypatch):
    class BoomHTTP:
        def post(self, url, json):
            raise ConnectionError("down")

    bot = TelegramBot(token="123:abc", http_client=BoomHTTP())
    assert bot.send_action("987654321") is False


def test_webhook_sends_typing_on_receipt(client, monkeypatch):
    """Typing indicator fires before the background reply is scheduled."""
    from FareBeep import notifier as notifier_mod

    actions = []

    class TypingBot(TelegramBot):
        def send_action(self, to, action="typing"):
            actions.append((to, action))
            return True

        def send_text(self, to, body):
            return True

    monkeypatch.setattr(notifier_mod, "TelegramBot", TypingBot)
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: None)
    test_client, _ = client
    r = test_client.post("/webhook/telegram",
                         json={"update_id": 42,
                               "message": {"chat": {"id": 555},
                                           "text": "hi"}},
                         headers={"X-Telegram-Bot-Api-Secret-Token":
                                  "farebeep-test-secret"})
    assert r.status_code == 200
    assert actions == [("555", "typing")]

