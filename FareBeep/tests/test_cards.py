"""WHATSAPP FLIGHT CARDS - logo map, card copy, tap routing, alert taps.

Cards are Meta interactive messages (no catalog): airline logo picture,
live price body, Book + Set-alert buttons. Taps return as button_reply
ids (pick:N / alert:N / book).
"""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import cards, chatstate, main
from FareBeep.models import Base, Subscription, User
from FareBeep.notifier import MetaWhatsapp


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
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)  # local parser
    return TestClient(main.app)


def _meta_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _tap_update(button_id: str, phone: str = "+2348012345678") -> bytes:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "interactive": {
                            "button_reply": {"id": button_id},
                        },
                    }],
                },
            }],
        }],
    }
    return json.dumps(payload).encode()


FARE = {"price": 98000.0, "currency": "NGN", "airline": "Air Peace",
        "flight_number": "P4 111", "departs_at": "07:10",
        "flight_date": "2026-08-14",
        "verify_link": "https://example.com/2"}


# ---- logo map ----

def test_logo_for_known_airlines():
    assert cards.logo_for("Air Peace") == \
        "https://images.kiwi.com/airlines/64x64/P4.png"
    assert cards.logo_for("  GREEN AFRICA airways ") == \
        "https://images.kiwi.com/airlines/64x64/Q9.png"
    assert cards.logo_for("Ibom Air").endswith("/QI.png")
    assert cards.logo_for("Xejet").endswith("/XJ.png")


def test_logo_for_unknown_airline_is_none():
    # Dana is defunct (AOC suspended 2024) and Max Air has no verified
    # mark: neither may ever render a logo.
    assert cards.logo_for("Dana Air") is None
    assert cards.logo_for("Max Air") is None
    assert cards.logo_for("Oceanic Airlines") is None
    assert cards.logo_for("") is None
    assert cards.logo_for(None) is None


def test_logo_for_new_carriers():
    assert cards.logo_for("Enugu Air").endswith("/EE.png")
    assert cards.logo_for("Binani Air").endswith("/NA.png")


# ---- card copy ----

def test_card_body_has_picture_facts_and_price():
    body = cards.card_body(FARE, "LOS", "ABV")
    assert "Air Peace" in body and "P4 111" in body
    assert "LOS" in body and "ABV" in body
    assert "\u20a698,000" in body
    assert "14 Aug" in body
    assert len(body) <= 1024


def test_card_body_renders_rich_extras_when_present():
    rich = dict(FARE, arrival_time="08:15", duration="1h 15m",
                baggage="20 kg", seats_left=9)
    body = cards.card_body(rich, "LOS", "ABV")
    assert "08:15" in body
    assert "1h 15m" in body
    assert "20 kg included" in body
    assert "Only 9 left!" in body


def test_card_body_omits_missing_extras():
    body = cards.card_body(FARE, "LOS", "ABV")
    assert "included" not in body
    assert "left!" not in body


def test_card_buttons_ranked_and_single():
    book, alert = cards.card_buttons(FARE, 2)
    assert book[0] == "pick:2" and "98,000" in book[1]
    assert alert == ("alert:2", "Set alert")
    book0, alert0 = cards.card_buttons(FARE, 0)
    assert book0[0] == "book"
    assert alert0 == ("alert:0", "Set alert")
    for _, title in cards.card_buttons(FARE, 1):
        assert len(title) <= 20  # WhatsApp button cap


def test_translate_tap():
    assert cards.translate_tap("pick:2") == ("pick", 2)
    assert cards.translate_tap("alert:0") == ("alert", 0)
    assert cards.translate_tap("book") == ("book",)
    assert cards.translate_tap("pick:99") is None
    assert cards.translate_tap("buy-now") is None
    assert cards.translate_tap("") is None
    assert cards.translate_tap(None) is None


# ---- sender payload ----

class _FakeHttp:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "json": json})

        class _Resp:
            def raise_for_status(self):
                pass

        return _Resp()


def test_send_interactive_card_payload():
    http = _FakeHttp()
    wa = MetaWhatsapp(access_token="tok", phone_number_id="123",
                      api_version="v26.0", http_client=http)
    assert wa.send_interactive_card(
        "+2348012345678", body="Air Peace\nLOS \u2192 ABV\n\u20a698,000",
        buttons=[("pick:1", "Book \u20a698,000"), ("alert:1", "Set alert")],
        image_url="https://images.kiwi.com/airlines/64x64/P4.png",
        footer="FareBeep \u2022 verified just now") is True
    payload = http.posts[0]["json"]
    assert payload["type"] == "interactive"
    assert payload["messaging_product"] == "whatsapp"
    assert payload["interactive"]["header"]["image"]["link"].endswith("P4.png")
    btns = payload["interactive"]["action"]["buttons"]
    assert [(b["reply"]["id"], b["reply"]["title"]) for b in btns] == [
        ("pick:1", "Book \u20a698,000"), ("alert:1", "Set alert")]


def test_send_interactive_card_without_image_omits_header():
    http = _FakeHttp()
    wa = MetaWhatsapp(access_token="tok", phone_number_id="123",
                      http_client=http)
    wa.send_interactive_card("+2348012345678", body="Max Air\n\u20a6150,000",
                             buttons=[("book", "Book")])
    assert "header" not in http.posts[0]["json"]["interactive"]


# ---- webhook tap routing ----

def test_meta_pick_tap_reuses_pick_gate(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    body = _tap_update("pick:2")
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert r.status_code == 200
    assert calls == [("+2348012345678", "2")]


def test_meta_book_tap_becomes_bare_book(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    body = _tap_update("book")
    client.post("/webhook/meta", content=body,
                headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert calls == [("+2348012345678", "BOOK")]


def test_meta_alert_tap_subscribes_from_shown_fare(client, monkeypatch,
                                                  session_factory):
    tapped = []
    monkeypatch.setattr(main, "_tap_alert_by_phone",
                        lambda phone, idx: tapped.append((phone, idx)))
    handled = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: handled.append((phone, text)))
    body = _tap_update("alert:1")
    client.post("/webhook/meta", content=body,
                headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert tapped == [("+2348012345678", 1)]
    assert handled == []  # alert taps never reach the brain as text


def test_meta_unknown_tap_is_ignored(client, monkeypatch):
    handled = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: handled.append((phone, text)))
    body = _tap_update("buy-now")
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert r.status_code == 200
    assert handled == []


# ---- alert tap end to end ----

class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return True


def test_alert_tap_creates_subscription_and_confirms(client, monkeypatch,
                                                    session_factory):
    phone = "+2348099999999"
    db = session_factory()
    chatstate.set_last_fares(db, phone, {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-08-14",
        "fares": [dict(FARE), dict(FARE, price=118500.0)],
    })
    db.commit()
    db.close()

    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)

    main._tap_alert_by_phone(phone, 1)

    db = session_factory()
    user_id = db.query(User).filter_by(phone=phone).first().user_id
    sub = db.query(Subscription).filter_by(user_id=user_id).one()
    assert (sub.origin, sub.destination) == ("LOS", "ABV")
    assert sub.target_price == 98000.0  # alert:1 = FIRST card, not second
    db.close()
    assert any("Beep armed" in body for _, body in fake.sent)


def test_alert_tap_second_card_subscribes_second_fare(
        client, monkeypatch, session_factory):
    phone = "+2348077777778"
    db = session_factory()
    chatstate.set_last_fares(db, phone, {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-08-14",
        "fares": [dict(FARE), dict(FARE, price=118500.0)],
    })
    db.commit()
    db.close()

    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)

    main._tap_alert_by_phone(phone, 2)

    db = session_factory()
    user_id = db.query(User).filter_by(phone=phone).first().user_id
    sub = db.query(Subscription).filter_by(user_id=user_id).one()
    assert sub.target_price == 118500.0
    db.close()


def test_alert_tap_with_expired_list_asks_for_search(client, monkeypatch,
                                                    session_factory):
    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    main._tap_alert_by_phone("+2348088888888", 0)
    assert any("expired" in body for _, body in fake.sent)
