"""CONCIERGE TEST CASES (FareBeep v2.1 mission) - driven through the real
Telegram webhook so the whole Pass 2 pipeline is exercised:

  Incomplete: "I'm going to Abuja."          -> asks for origin/date
  Natural:    "Find me a flight to Abj for next tuesday." -> performs search
  Surge:      "Lagos to Akure tomorrow."    -> detects the anomaly, offers TRACK
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, User


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


class RecordingLedger:
    """Fake search service: records calls, returns a scripted fare."""

    def __init__(self, db, fare=None):
        self.calls = []
        self.fare = fare or {
            "source": "serpapi", "flight_date": "2026-08-14",
            "price": 118500.0, "airline": "Air Peace",
            "verify_link": "https://example.com/fare",
            "above_guardrail": False,
        }

    def search(self, origin, destination, date_, **kwargs):
        self.calls.append((origin, destination, date_, kwargs))
        f = dict(self.fare)
        f["flight_date"] = date_ or f["flight_date"]
        return f


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "farebeep-test-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)  # local parser
    ledger = {}
    monkeypatch.setattr(main, "LedgerSearch",
                        lambda db: ledger.setdefault("inst", RecordingLedger(db)))

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

    fake = FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    test_client = TestClient(main.app)
    return test_client, fake, ledger


def _post(client, text):
    return client.post(
        "/webhook/telegram",
        json={"message": {"chat": {"id": 987654321}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "farebeep-test-secret"})


def test_incomplete_destination_only_asks_followup(client):
    test_client, fake, _ = client
    r = _post(test_client, "I'm going to Abuja")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "Where will you be flying from" in body
    assert "Abuja" in body


def test_incomplete_no_route_at_all_asks_gently(client):
    test_client, fake, _ = client
    r = _post(test_client, "I want to fly somewhere")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "FareBeep" in body   # help menu - the gentle exit path


def test_natural_shortcut_searches_with_default_origin(client):
    test_client, fake, ledger = client
    r = _post(test_client, "Find me a flight to Abj for next tuesday")
    assert r.status_code == 200
    inst = ledger["inst"]
    assert len(inst.calls) == 1
    origin, destination, flight_date, _ = inst.calls[0]
    assert origin == "LOS"          # Pass 2: Lagos is the default hub
    assert destination == "ABV"
    target = (1 - date.today().weekday()) % 7
    assert flight_date == (date.today() + timedelta(days=7 + target)).isoformat()
    body = fake.sent[-1][1]
    assert "Air Peace" in body
    assert "118,500" in body
    assert "BOOK" in body and "TRACK" in body
    # (the curr=NGN link rewrite is covered by test_price_guardrail - the
    # fake ledger here bypasses fetch(), where the rewrite runs)


def test_surge_price_warns_and_offers_track(client):
    test_client, fake, ledger = client
    ledger["inst"] = RecordingLedger(
        None, fare={"source": "serpapi", "flight_date": "2026-08-14",
                    "price": 660000.0, "airline": "Green Africa Airways",
                    "verify_link": "https://example.com/fare",
                    "above_guardrail": True})
    r = _post(test_client, "Lagos to Akure tomorrow")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "surge" in body.lower()
    assert "660,000" in body
    assert "TRACK" in body
    assert "Beep" in body


def test_user_name_is_captured(client):
    test_client, fake, _ = client
    r = _post(test_client, "my name is Damilola, Lagos to Abuja tomorrow")
    assert r.status_code == 200
    db = main.SessionLocal()
    try:
        user = db.query(User).filter(User.phone == "987654321").first()
        assert user is not None
        assert user.name == "Damilola"
    finally:
        db.close()


def test_bare_book_after_fare_uses_quoted_date(client, monkeypatch):
    """THE DATE BUG: replying just 'BOOK' after a fare quote must book the
    quoted date (e.g. 31 Aug), NOT silently default to today."""
    test_client, fake, ledger = client
    main._last_fare.clear()

    fake_calls = {}

    def _fake_create(user_id, origin, destination, flight_date, airline_price,
                     **kwargs):
        fake_calls.update(origin=origin, destination=destination,
                          flight_date=flight_date, airline_price=airline_price)
        from FareBeep.models import BookingSession, utcnow
        from datetime import timedelta

        session = BookingSession(
            user_id=user_id, origin=origin, destination=destination,
            flight_date=flight_date, airline_price=airline_price,
            markup=5000.0, processing_fee=0.0, total_price=airline_price,
            status="pending", expires_at=utcnow() + timedelta(minutes=10),
            payment_ref="FB-TEST1")
        db = main.SessionLocal()
        db.add(session)
        db.commit()
        return {"session": session, "total_amount": airline_price,
                "expires_at": session.expires_at,
                "payment_link": "https://checkout.paystack.com/fb-test1"}

    class _FakeBookings:
        def __init__(self, db):
            pass

        def create_booking(self, user_id, origin, destination, flight_date,
                           airline_price, **kwargs):
            return _fake_create(user_id, origin, destination, flight_date,
                                airline_price, **kwargs)

    monkeypatch.setattr(main, "BookingService", _FakeBookings)

    # step 1: user asks for a fare on a SPECIFIC date
    r = _post(test_client, "Lagos to Abuja on the 31st of August")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "BOOK" in body

    # step 2: bare BOOK - no route, no date in the message
    r = _post(test_client, "BOOK")
    assert r.status_code == 200
    assert fake_calls["flight_date"] == "2026-08-31", fake_calls
    assert fake_calls["origin"] == "LOS"
    assert fake_calls["destination"] == "ABV"
    # the user sees the quoted date, not today
    assert "2026-08-31" in fake.sent[-1][1]
    assert "TEST MODE" in fake.sent[-1][1]


def test_bare_book_without_context_asks_for_route(client):
    test_client, fake, _ = client
    main._last_fare.clear()
    r = _post(test_client, "BOOK")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "BOOK Lagos to Abuja" in body


def test_bare_track_after_fare_uses_quoted_route(client):
    """THE TRACK BUG: bare 'TRACK' after a fare quote must arm the alert on
    the quoted route + date - the same context fix as BOOK."""
    test_client, fake, _ = client
    main._last_fare.clear()

    r = _post(test_client, "fare lagos to abuja 31")
    assert r.status_code == 200
    assert "BOOK" in fake.sent[-1][1]

    r = _post(test_client, "TRACK")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "Beep armed" in body
    assert "Lagos" in body and "Abuja" in body

    from FareBeep.models import Subscription
    db = main.SessionLocal()
    try:
        subs = db.query(Subscription).all()
        assert len(subs) == 1
        assert subs[0].origin == "LOS"
        assert subs[0].destination == "ABV"
        assert subs[0].target_date is not None   # the quoted date, not NULL
    finally:
        db.close()


def test_bare_track_without_context_asks_for_route(client):
    test_client, fake, _ = client
    main._last_fare.clear()
    r = _post(test_client, "TRACK")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "TRACK Lagos" in body