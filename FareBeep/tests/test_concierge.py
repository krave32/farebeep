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

    def __init__(self, db, fare=None, fares=None):
        self.calls = []
        self.fare = fare or {
            "source": "serpapi", "flight_date": "2026-08-14",
            "price": 118500.0, "airline": "Air Peace",
            "verify_link": "https://example.com/fare",
            "above_guardrail": False,
        }
        self.fares = fares

    def search(self, origin, destination, date_, **kwargs):
        self.calls.append((origin, destination, date_, kwargs))
        f = dict(self.fare)
        f["flight_date"] = date_ or f["flight_date"]
        return f

    def search_list(self, origin, destination, date_, limit=3):
        self.calls.append((origin, destination, date_, {"limit": limit}))
        if self.fares is not None:
            fares = [dict(f) for f in self.fares]
            for f in fares:
                f["flight_date"] = date_ or f.get("flight_date")
            surge = [f for f in fares if f.get("above_guardrail")]
            sane = [f for f in fares if not f.get("above_guardrail")]
            return sane, surge
        f = dict(self.fare)
        f["flight_date"] = date_ or f["flight_date"]
        if f.get("above_guardrail"):
            return [], [f]      # surge: nothing sane to sell
        return [f], []


def _install_fake_bookings(monkeypatch, calls):
    """Scripted BookingService that records what would be booked."""
    def _create(user_id, origin, destination, flight_date, airline_price,
                **kwargs):
        calls.update(origin=origin, destination=destination,
                     flight_date=flight_date, airline_price=airline_price,
                     flight_iata=kwargs.get("flight_iata"),
                     airline=kwargs.get("airline"))
        from FareBeep.models import BookingSession, utcnow
        from datetime import timedelta

        session = BookingSession(
            user_id=user_id, origin=origin, destination=destination,
            flight_date=flight_date, airline_price=airline_price,
            markup=5000.0, processing_fee=0.0, total_price=airline_price,
            status="pending", expires_at=utcnow() + timedelta(minutes=10),
            payment_ref="FB-PICK1")
        db = main.SessionLocal()
        db.add(session)
        db.commit()
        return {"session": session, "total_amount": airline_price,
                "expires_at": session.expires_at,
                "payment_link": "https://checkout.paystack.com/fb-pick1"}

    class _FakeBookings:
        def __init__(self, db):
            pass

        def create_booking(self, user_id, origin, destination, flight_date,
                           airline_price, **kwargs):
            return _create(user_id, origin, destination, flight_date,
                           airline_price, **kwargs)

    monkeypatch.setattr(main, "BookingService", _FakeBookings)


def _ranked_fares():
    """Three sane fares for the ranked-list flow."""
    return [
        {"source": "serpapi", "flight_date": "2026-08-14", "price": 98000.0,
         "airline": "Dana Air", "departs_at": "06:00",
         "flight_number": "9J 333", "verify_link": "https://example.com/1",
         "above_guardrail": False},
        {"source": "serpapi", "flight_date": "2026-08-14", "price": 118500.0,
         "airline": "Air Peace", "departs_at": "07:10",
         "flight_number": "P4 111", "verify_link": "https://example.com/2",
         "above_guardrail": False},
        {"source": "serpapi", "flight_date": "2026-08-14", "price": 154000.0,
         "airline": "Green Africa", "departs_at": "08:00",
         "flight_number": "9J 222", "verify_link": "https://example.com/3",
         "above_guardrail": False},
    ]


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


# ---------------------------------------------------------------------------
# The ranked-list pick flow ("reply 1, 2 or 3 to lock")
# ---------------------------------------------------------------------------
def test_ranked_list_reply_when_multiple_fares(client):
    """2+ sane fares -> numbered ranked list + the "reply 1, 2 or 3" prompt."""
    test_client, fake, ledger = client
    main._last_fares.clear()
    ledger["inst"] = RecordingLedger(None, fares=_ranked_fares())

    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "Here's what I found Lagos -> Abuja on" in body
    assert "Which one would you like? Reply 1, 2 or 3." in body
    assert "1. Dana Air, leaves 06:00 - ₦98,000" in body
    assert "2. Air Peace, leaves 07:10 - ₦118,500" in body
    assert "3. Green Africa, leaves 08:00 - ₦154,000" in body

    ctx = main._last_fares.get("987654321")
    assert ctx is not None
    assert ctx["origin_iata"] == "LOS"
    assert ctx["destination_iata"] == "ABV"
    assert len(ctx["fares"]) == 3


def test_single_result_keeps_classic_reply_and_no_list(client):
    """One result -> the single-fare reply, never a ranked list."""
    test_client, fake, ledger = client
    main._last_fares.clear()
    ledger["inst"] = RecordingLedger(None)   # default = 1 sane fare

    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "Fare" in body and "BOOK" in body and "TRACK" in body
    assert "Reply 1, 2 or 3" not in body
    assert "987654321" not in main._last_fares


def test_pick_2_books_the_selected_fare_live(client, monkeypatch):
    """'2' after a ranked list re-verifies that exact flight live, then books
    the FRESH price - and the list is consumed so a stray '2' can't rebook."""
    test_client, fake, ledger = client
    main._last_fares.clear()
    ledger["inst"] = RecordingLedger(None, fares=_ranked_fares())
    calls = {}
    _install_fake_bookings(monkeypatch, calls)

    _post(test_client, "Lagos to Abuja tomorrow")

    r = _post(test_client, "2")
    assert r.status_code == 200
    assert calls["airline_price"] == 118500.0, calls    # Air Peace (option 2)
    assert calls["flight_iata"] == "P4 111", calls
    assert calls["airline"] == "Air Peace", calls
    assert calls["origin"] == "LOS" and calls["destination"] == "ABV"
    assert "TEST MODE" in fake.sent[-1][1]

    # two live list calls: the original (limit=3) + the booking re-check (limit=6)
    inst = ledger["inst"]
    assert [c[3]["limit"] for c in inst.calls] == [3, 6]
    assert "987654321" not in main._last_fares       # pick consumed


def test_pick_out_of_range_does_not_book(client):
    """A number beyond the list gets a gentle correction, not a booking."""
    test_client, fake, ledger = client
    main._last_fares.clear()
    ledger["inst"] = RecordingLedger(None, fares=_ranked_fares())
    _post(test_client, "Lagos to Abuja tomorrow")

    r = _post(test_client, "5")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "I only showed 3 options" in body
    assert "Reply 1-3" in body
    assert "987654321" in main._last_fares          # list still active


def test_picked_flight_no_longer_available_is_handled(client, monkeypatch):
    """If the picked flight vanished on the live re-check, no booking - the
    bot says so and keeps the flow honest."""
    test_client, fake, ledger = client
    main._last_fares.clear()
    ledger["inst"] = RecordingLedger(None, fares=_ranked_fares())
    calls = {}
    _install_fake_bookings(monkeypatch, calls)

    _post(test_client, "Lagos to Abuja tomorrow")
    ledger["inst"].fares = [_ranked_fares()[0], _ranked_fares()[2]]  # P4 111 gone

    r = _post(test_client, "2")
    assert r.status_code == 200
    assert calls == {}
    body = fake.sent[-1][1]
    assert "no longer available" in body
    assert "TEST MODE" not in body


def test_pick_gate_shapes(monkeypatch):
    """_try_pick: active list + a pick-shaped message -> the fare; anything
    else -> None (falls through to the brain as a date, never intercepted)."""
    monkeypatch.setattr(main, "_last_fares", {})
    phone = "987654321"
    fares = _ranked_fares()
    main._last_fares[phone] = {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-08-14", "fares": fares,
    }

    assert main._try_pick("2", phone) is fares[1]
    assert main._try_pick("number 2", phone) is fares[1]
    assert main._try_pick("option 2", phone) is fares[1]
    assert main._try_pick("pick 2", phone) is fares[1]
    assert main._try_pick("the 2nd one", phone) is fares[1]
    assert main._try_pick("2nd", phone) is fares[1]
    assert main._try_pick("#2", phone) is fares[1]
    assert main._try_pick("1", phone) is fares[0]
    assert main._try_pick("3", phone) is fares[2]
    assert main._try_pick("5", phone) == "out_of_range"
    assert main._try_pick("31", phone) is None          # a DATE, not a pick
    assert main._try_pick("book 2", phone) is None      # not a pick shape
    main._last_fares.clear()
    assert main._try_pick("2", phone) is None           # no active list