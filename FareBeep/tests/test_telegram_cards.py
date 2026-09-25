"""TELEGRAM FARE CARDS - inline keyboards + callback taps.

The Meta interactive-card UX on the Telegram channel: same tap ids
(pick:N / alert:N / book), same translate_tap parser, same tested
gates - only the transport differs (sendPhoto/sendMessage +
answerCallbackQuery instead of Meta's interactive payload).
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import chatstate, main
from FareBeep.cards import card_body, card_buttons
from FareBeep.models import Base, User, utcnow
from FareBeep.notifier import TelegramBot

SECRET = "farebeep-test-secret"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class _CardSpy:
    """Records send_interactive_card / answer_callback / send_text."""

    def __init__(self):
        self.cards, self.texts, self.acks = [], [], []

    def send_text(self, to, body):
        self.texts.append((to, body))
        return True

    def send_action(self, to, action="typing"):
        return True

    def send_interactive_card(self, to, body, buttons, image_url=None,
                              footer=None):
        self.cards.append({"to": to, "body": body, "buttons": buttons,
                           "image_url": image_url, "footer": footer})
        return True

    def answer_callback(self, callback_id, text=None):
        self.acks.append((callback_id, text))
        return True


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)

    class FakeLedger:
        def __init__(self, db):
            pass

        def search(self, origin, destination, date_):
            return {"source": "ledger", "flight_date": date_ or "2026-08-14",
                    "price": 118500.0, "airline": "Air Peace",
                    "verify_link": "https://example.com/fare"}

        def search_list(self, origin, destination, date_, limit=3):
            fares = [{"source": "ledger", "flight_date": date_ or "2026-08-14",
                      "price": p, "airline": a, "departs_at": t,
                      "flight_number": fn,
                      "verify_link": "https://example.com/fare"}
                     for p, a, t, fn in
                     [(118500.0, "Air Peace", "07:10", "P4 111"),
                      (98000.0, "Rano Air", "06:00", "RN 303")]]
            return fares[:limit], []

    monkeypatch.setattr(main, "LedgerSearch", FakeLedger)

    fake = _CardSpy()
    monkeypatch.setattr(main, "notifier", fake)
    return TestClient(main.app), fake


def _post(client, text):
    return client.post(
        "/webhook/telegram",
        json={"message": {"chat": {"id": 987654321}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


def _callback(client, data, cb_id="CB1"):
    return client.post(
        "/webhook/telegram",
        json={"callback_query": {"id": cb_id, "data": data,
                                 "message": {"chat": {"id": 987654321}}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


# ---- transport ---------------------------------------------------------

def test_send_interactive_card_uses_sendphoto_with_inline_keyboard():
    posts = []

    class _Http:
        def post(self, url, json=None):
            posts.append((url, json))

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"ok": True}

            return _R()

    bot = TelegramBot(token="T", http_client=_Http())
    ok = bot.send_interactive_card(
        "123", body="Air Peace P4 111 - \u20a6118,500",
        buttons=[("pick:1", "Book \u20a6118,500"), ("alert:1", "Set alert")],
        image_url="https://images.kiwi.com/airlines/64x64/P4.png")
    assert ok
    url, payload = posts[0]
    assert url.endswith("/sendPhoto")
    kb = payload["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "pick:1"
    assert kb[0][0]["text"].startswith("Book")
    assert kb[1][0]["callback_data"] == "alert:1"
    assert payload["photo"].endswith("P4.png")


def test_send_interactive_card_without_image_uses_sendmessage():
    posts = []

    class _Http:
        def post(self, url, json=None):
            posts.append((url, json))

            class _R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"ok": True}

            return _R()

    bot = TelegramBot(token="T", http_client=_Http())
    ok = bot.send_interactive_card("123", body="hello",
                                   buttons=[("book", "Book")])
    assert ok and posts[0][0].endswith("/sendMessage")
    assert posts[0][1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"] == "book"


# ---- webhook routing ---------------------------------------------------

def test_callback_query_routes_pick_into_pick_gate(client):
    """A Telegram '1' tap must behave EXACTLY like typing '1': the pick
    gate consumes the ranked list and confirms the booking handshake."""
    test_client, fake = client
    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    assert len(fake.cards) >= 1            # cards went out on Telegram
    r = _callback(test_client, "pick:1")
    assert r.status_code == 200
    assert fake.acks == [("CB1", None)]
    assert any("live price" in body.lower() or "book" in body.lower()
               for _, body in fake.texts)


def test_callback_query_alert_tap_arms_watch(client, session_factory):
    test_client, fake = client
    _post(test_client, "Lagos to Abuja tomorrow")
    db = session_factory()
    user = db.query(User).filter_by(phone="987654321").first()
    assert user is not None
    db.close()
    r = _callback(test_client, "alert:1")
    assert r.status_code == 200
    assert any("Beep armed" in body for _, body in fake.texts)


def test_callback_query_unknown_id_is_ignored_silently(client):
    test_client, fake = client
    r = _callback(test_client, "mystery:9", cb_id="CB9")
    assert r.status_code == 200
    assert fake.acks == [("CB9", None)]
    assert fake.texts == []


def test_fare_cards_go_out_on_telegram_search(client):
    """_send_fare_cards no longer skips Telegram: the search reply is
    followed by tappable cards with the Meta-identical button ids."""
    test_client, fake = client
    _post(test_client, "Lagos to Abuja tomorrow")
    assert fake.cards, "cards must render on the Telegram channel"
    for card in fake.cards:
        ids = [bid for bid, _ in card["buttons"]]
        assert ids[0].startswith("pick:") or ids[0] == "book"
        assert ids[1] == "alert:1" or ids[1].startswith("alert:")
        assert card_body({"airline": "X", "price": 1}, "LOS", "ABV")
