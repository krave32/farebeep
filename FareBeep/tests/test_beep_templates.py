"""PROACTIVE BEEP TEMPLATES - price-drop pushes as approved Meta templates.

Outside the 24h user window plain text fails, so Beeps go out as the
farebeep_price_drop UTILITY template (params + Book-now tap payload),
falling back to text when the template send fails. "Book now" taps
(beep:<sub_id>) re-present that subscription's route fresh.
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
from FareBeep.alerts import SubscriptionMonitor
from FareBeep.models import Base, Subscription, User, utcnow
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
    monkeypatch.setattr(main, "GROQ_API_KEY", None)  # deterministic path
    return TestClient(main.app)


def _meta_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _tap_update(button_id: str, phone: str = "+2348012345678") -> bytes:
    """Interactive-shape tap (flight cards). Template-shape taps
    (beep buttons) use _template_tap_update - Meta delivers those as
    messages[].button.payload, a different shape."""
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


def _template_tap_update(payload_text: str,
                         phone: str = "+2348012345678") -> bytes:
    """Real shape of a template quick-reply tap ("Book now"/"Not now")."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": phone,
                        "type": "button",
                        "button": {"payload": payload_text,
                                   "text": "Book now"},
                    }],
                },
            }],
        }],
    }
    return json.dumps(payload).encode()


class _FakeHttp:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def post(self, url, headers=None, json=None):
        if self.fail:
            raise ConnectionError("Meta down")
        self.posts.append({"url": url, "json": json})

        class _Resp:
            def raise_for_status(self):
                pass

        return _Resp()


def _meta_notifier(fail=False):
    return MetaWhatsapp(access_token="tok", phone_number_id="123",
                        http_client=_FakeHttp(fail=fail))


def _user_with_sub(db, phone="+2348077777777", target_price=80000.0):
    user = User(phone=phone)
    db.add(user)
    db.commit()
    db.refresh(user)
    sub = Subscription(user_id=user.user_id, origin="LOS",
                       destination="ABV", target_price=target_price,
                       last_price=90000.0)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return user, sub


# ---- template payload ----

def test_send_template_carries_button_payloads():
    wa = _meta_notifier()
    assert wa.send_template(
        "+2348012345678", "farebeep_price_drop",
        ["Ada", "Lagos", "Abuja", "2026-08-14", "75,000", "90,000"],
        buttons=[{"payload": "beep:12"}, {"payload": "dismiss"}]) is True
    tpl = wa._http.posts[0]["json"]["template"]
    assert tpl["name"] == "farebeep_price_drop"
    assert tpl["components"][0]["parameters"][0] == {
        "type": "text", "text": "Ada"}
    btns = [c for c in tpl["components"] if c["type"] == "button"]
    assert [(b["index"], b["parameters"][0]["payload"]) for b in btns] == [
        ("0", "beep:12"), ("1", "dismiss")]


def test_translate_beep_tap():
    assert cards.translate_tap("beep:42") == ("beep", 42)
    assert cards.translate_tap("beep:abc") is None
    assert cards.translate_tap("dismiss") is None


# ---- beep delivery: template first, text fallback ----

def test_send_beep_uses_template_on_meta(session_factory, monkeypatch):
    monkeypatch.setattr("FareBeep.config.META_TEMPLATE_PRICE_DROP",
                        "test_beep_tpl")
    db = session_factory()
    user, sub = _user_with_sub(db)
    wa = _meta_notifier()
    monitor = SubscriptionMonitor(db, notifier=wa)

    assert monitor._send_beep(
        sub, {"price": 75000.0, "airline": "Air Peace"}) is True
    tpl = wa._http.posts[0]["json"]["template"]
    assert tpl["name"] == "test_beep_tpl"
    params = tpl["components"][0]["parameters"]
    assert [p["text"] for p in params] == [
        "there", "Lagos", "Abuja", "your dates", "75,000", "80,000"]
    btns = [c for c in tpl["components"] if c["type"] == "button"]
    assert btns[0]["parameters"][0]["payload"] == f"beep:{sub.id}"
    db.close()


def test_send_beep_falls_back_to_text(session_factory):
    db = session_factory()
    user, sub = _user_with_sub(db)

    class _FailTemplate(MetaWhatsapp):
        def send_template(self, *a, **k):
            return False

    wa = _FailTemplate(access_token="tok", phone_number_id="123",
                       http_client=_FakeHttp())
    monitor = SubscriptionMonitor(db, notifier=wa)
    assert monitor._send_beep(
        sub, {"price": 75000.0, "airline": "Air Peace"}) is True
    text = wa._http.posts[0]["json"]["text"]["body"]
    assert "FARE BEEP" in text
    db.close()


def test_send_beep_unchanged_on_other_channels(session_factory):
    """Twilio/Telegram doubles only speak send_text - untouched path."""

    class _TextOnly:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append(body)
            return True

    db = session_factory()
    user, sub = _user_with_sub(db)
    fake = _TextOnly()
    monitor = SubscriptionMonitor(db, notifier=fake)
    assert monitor._send_beep(
        sub, {"price": 75000.0, "airline": "Air Peace"}) is True
    assert "FARE BEEP" in fake.sent[0]
    db.close()


# ---- beep tap -> fresh fares ----

class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return True


class _ListLedger:
    """LedgerSearch stand-in returning one scripted fare list."""

    def __init__(self, fares):
        self.fares = fares

    def __call__(self, db):
        return self

    def search_list(self, origin, destination, flight_date, limit=3):
        return [dict(f) for f in self.fares], []


def test_meta_beep_tap_routes_to_beep_handler(client, monkeypatch):
    tapped = []
    monkeypatch.setattr(main, "_tap_beep_by_phone",
                        lambda phone, sid: tapped.append((phone, sid)))
    handled = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: handled.append((phone, text)))
    body = _template_tap_update("beep:7")
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert r.status_code == 200
    assert tapped == [("+2348012345678", 7)]
    assert handled == []


def test_meta_dismiss_tap_is_silent(client, monkeypatch):
    tapped = []
    monkeypatch.setattr(main, "_tap_beep_by_phone",
                        lambda phone, sid: tapped.append((phone, sid)))
    handled = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: handled.append((phone, text)))
    body = _template_tap_update("dismiss")
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _meta_sig(body)})
    assert r.status_code == 200
    assert tapped == []
    assert handled == []


def test_beep_tap_represents_fares_fresh(client, monkeypatch,
                                         session_factory):
    phone = "+2348066666666"
    db = session_factory()
    user = User(phone=phone)
    db.add(user)
    db.commit()
    db.refresh(user)
    sub = Subscription(user_id=user.user_id, origin="LOS",
                       destination="ABV", target_price=80000.0)
    db.add(sub)
    db.commit()
    sub_id = sub.id
    db.close()

    fares = [{"price": 75000.0, "currency": "NGN", "airline": "Air Peace",
              "flight_number": "P4 111", "departs_at": "07:10",
              "flight_date": "2026-08-14",
              "verify_link": "https://example.com/2"}]
    monkeypatch.setattr(main, "LedgerSearch", _ListLedger(fares))
    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)

    main._tap_beep_by_phone(phone, sub_id)

    assert any("75,000" in body for _, body in fake.sent)


def test_beep_tap_for_deleted_sub_asks_for_search(client, monkeypatch,
                                                 session_factory):
    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    main._tap_beep_by_phone("+2348055555555", 424242)
    assert any("expired" in body for _, body in fake.sent)


def test_beep_tap_for_foreign_sub_stays_silent(client, monkeypatch,
                                              session_factory):
    db = session_factory()
    owner = User(phone="+2348044444444")
    intruder = User(phone="+2348033333333")
    db.add_all([owner, intruder])
    db.commit()
    db.refresh(owner)
    sub = Subscription(user_id=owner.user_id, origin="LOS",
                       destination="ABV")
    db.add(sub)
    db.commit()
    sub_id = sub.id
    db.close()

    fake = _FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    main._tap_beep_by_phone("+2348033333333", sub_id)
    assert fake.sent == []
