"""CACHED VS FRESH - every quoted fare says how fresh it is, bookings
re-check live, and no message promises a supplier fare lock.

Rules pinned here:
  - ledger fares render "cached ~N min ago" (+ re-check promise);
    live fares render "checked just now".
  - BOOK always prices from a force-refreshed live quote (never the
    shown/cached number) with the price-move handcheck intact.
  - the 10-minute hold is OURS (honored total), never an airline seat
    lock: "held", never "locked".
"""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import chatstate, main
from FareBeep.models import (Base, BookingSession, ChatState, FareLedger,
                             Subscription, User, utcnow)
from FareBeep.search import LedgerSearch, fare_freshness


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
def user(db):
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class _RecordingLedger:
    """Live-shaped fake: records calls, returns scripted fares."""

    def __init__(self, db, fares=None):
        self.calls = []
        self.fares = fares if fares is not None else [{
            "source": "live", "flight_date": "2026-08-14",
            "price": 118500.0, "airline": "Air Peace",
            "flight_number": "P4 111", "departs_at": "07:10",
            "verify_link": "https://example.com/fare",
            "above_guardrail": False,
        }]

    def search(self, origin, destination, date_, **kwargs):
        self.calls.append((origin, destination, date_, kwargs))
        f = dict(self.fares[0])
        f["flight_date"] = date_ or f["flight_date"]
        return f

    def search_list(self, origin, destination, date_, limit=3):
        self.calls.append((origin, destination, date_, {"limit": limit}))
        fares = [dict(f) for f in self.fares]
        for f in fares:
            f["flight_date"] = date_ or f.get("flight_date")
        return fares, []


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "farebeep-test-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)  # deterministic path
    db = session_factory()
    db.query(ChatState).delete()
    db.commit()
    db.close()
    ledger = {}
    monkeypatch.setattr(main, "LedgerSearch",
                        lambda db: ledger.setdefault("inst", _RecordingLedger(db)))

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

    fake = FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    return TestClient(main.app), fake, ledger


def _post(client, text):
    return client.post(
        "/webhook/telegram",
        json={"message": {"chat": {"id": 987654321}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "farebeep-test-secret"})


# ---- helper units ------------------------------------------------------

def test_freshness_labels():
    now = utcnow()
    assert fare_freshness({"source": "serpapi"}) == "checked just now"
    assert fare_freshness({"source": "247travels"}) == "checked just now"
    assert fare_freshness({"source": "live"}) == "checked just now"
    aged = (now - timedelta(minutes=12)).isoformat()
    assert fare_freshness({"source": "ledger",
                           "checked_at": aged}, now) == "cached ~12 min ago"
    assert fare_freshness({"source": "ledger",
                           "checked_at": now.isoformat()},
                          now) == "checked just now"
    assert fare_freshness({"source": "ledger"}) == "cached fare"
    assert fare_freshness({}) == ""
    assert fare_freshness(None) == ""


# ---- search() provenance -----------------------------------------------

def test_ledger_hit_carries_checked_at(session_factory):
    db = session_factory()
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date="2026-08-20", price=98000.0,
                      currency="NGN", airline="Air Peace", verify_link=None,
                      last_updated=utcnow() - timedelta(minutes=12)))
    db.commit()

    def _boom(*a, **k):
        raise AssertionError("ledger hit must not call live")

    svc = LedgerSearch(db, live=type("E", (), {"fetch": _boom})(),
                       ledger_ttl_minutes=30)
    out = svc.search("LOS", "ABV", "2026-08-20")
    assert out["source"] == "ledger"
    assert out["checked_at"] is not None
    assert "cached ~" in fare_freshness(out)
    db.close()


def test_live_result_stamped_just_now(session_factory):
    class _Live:
        def fetch(self, o, d, dt):
            return {"price": 99000.0, "currency": "NGN",
                    "airline": "Rano Air", "verify_link": None}

    db = session_factory()
    svc = LedgerSearch(db, live=_Live(), ledger_ttl_minutes=30)
    out = svc.search("LOS", "ABV", "2026-10-02")
    assert out["source"] == "serpapi" and out["checked_at"] is not None
    assert fare_freshness(out) == "checked just now"
    db.close()


# ---- concierge replies ---------------------------------------------------

def test_single_fare_labels_fresh_and_promises_recheck(client):
    test_client, fake, _ = client
    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "checked just now" in body
    assert "(live)" not in body
    assert "re-confirm" in body


def test_ranked_list_labels_fresh(client):
    test_client, fake, ledger = client
    ledger["inst"] = _RecordingLedger(None, fares=[
        {"source": "live", "flight_date": "2026-08-14", "price": 98000.0,
         "airline": "Rano Air", "departs_at": "06:00",
         "flight_number": "RN 303", "verify_link": "https://example.com/1",
         "above_guardrail": False},
        {"source": "live", "flight_date": "2026-08-14", "price": 118500.0,
         "airline": "Air Peace", "departs_at": "07:10",
         "flight_number": "P4 111", "verify_link": "https://example.com/2",
         "above_guardrail": False},
    ])
    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    body = fake.sent[-1][1]
    assert "checked just now" in body and "re-confirm" in body


def test_booking_message_holds_never_locks(client, monkeypatch):
    from datetime import timedelta as _td

    test_client, fake, ledger = client
    created = []

    class _FakeBookings:
        def __init__(self, db):
            pass

        def create_booking(self, user_id, origin, destination, flight_date,
                           airline_price, **kwargs):
            session = BookingSession(
                user_id=user_id, origin=origin, destination=destination,
                flight_date=flight_date, airline_price=airline_price,
                markup=5000.0, processing_fee=100.0,
                total_price=airline_price + 5100.0, status="pending",
                expires_at=utcnow() + _td(minutes=10),
                payment_ref="FB-FRESH1")
            db = main.SessionLocal()
            db.add(session)
            db.commit()
            created.append(session.payment_ref)
            return {"session": session,
                    "total_amount": session.total_price,
                    "expires_at": session.expires_at,
                    "payment_link": "https://checkout.paystack.com/fb-fresh1"}

    monkeypatch.setattr(main, "BookingService", _FakeBookings)
    assert _post(test_client, "Lagos to Abuja tomorrow").status_code == 200
    assert _post(test_client, "BOOK").status_code == 200
    assert created == ["FB-FRESH1"]
    body = fake.sent[-1][1]
    assert "Total held" in body
    assert "PRICE LOCKED" not in body
    assert "re-checked live" in body


def test_book_reprices_live_never_cached(client, monkeypatch):
    """The BOOK handshake force-refreshes: a stale shown price is
    re-quoted live (handcheck question), never booked blind."""
    test_client, fake, ledger = client
    fake_search = _RecordingLedger(None)
    ledger["inst"] = fake_search
    assert _post(test_client, "Lagos to Abuja tomorrow").status_code == 200

    # The world moved: live now returns a different price.
    fake_search.fares[0]["price"] = 150000.0
    monkeypatch.setattr(main, "BookingService",
                        lambda db: (_ for _ in ()).throw(
                            AssertionError("must ask first, not book")))
    assert _post(test_client, "BOOK").status_code == 200
    body = fake.sent[-1][1]
    assert "moved from" in body and "150,000" in body


# ---- agent summaries -------------------------------------------------------

def test_agent_ledger_summary_labels_cached(db, user, monkeypatch):
    from FareBeep import agent as agent_mod
    from FareBeep.agent import build_tools
    monkeypatch.setattr(agent_mod, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr("FareBeep.dates.lagos_today",
                        lambda: date(2026, 8, 1))
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date="2026-08-20", price=98000.0,
                      currency="NGN", airline="Air Peace", verify_link=None,
                      last_updated=utcnow() - timedelta(minutes=4)))
    db.commit()

    def _boom(*a, **k):
        raise AssertionError("ledger hit must not call live")

    monkeypatch.setattr(agent_mod, "get_inventory_client", _boom)
    tools = {t.name: t for t in build_tools(db, user.phone)}
    out = json.loads(tools["search_fares"].invoke(
        {"origin": "LOS", "destination": "ABV",
         "flight_date": "2026-08-20"}))
    assert out["found"] is True and out["price_ngn"] == 98000.0
    assert "Cached ~" in out["summary"] and "min ago" in out["summary"]
    assert "re-check live" in out["summary"]


def test_agent_reserve_summary_holds(db, user, monkeypatch):
    from FareBeep import agent as agent_mod
    from FareBeep import transactions
    from FareBeep.agent import build_tools
    monkeypatch.setattr(agent_mod, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(
        transactions, "initialize_paystack_payment",
        lambda ref, total, email: {
            "access_code": f"AC_{ref}",
            "authorization_url": f"https://paystack.com/pay/{ref}"})
    monkeypatch.setattr("FareBeep.dates.lagos_today",
                        lambda: date(2026, 8, 1))

    class _Fake247:
        async def verify_price(self, booking_token, **k):
            return {"verified": True, "price_changed": False,
                    "original_price": 98000.0, "verified_price": 98000.0,
                    "currency": "NGN", "booking_token": "btk_bbb",
                    "expires_at": None}

        async def close(self):
            pass

    monkeypatch.setattr(agent_mod, "get_inventory_client",
                        lambda *a, **k: _Fake247())
    tools = {t.name: t for t in build_tools(db, user.phone)}
    out = json.loads(tools["reserve_fare"].invoke(
        {"booking_token": "btk_aaa", "origin": "LOS", "destination": "ABV",
         "flight_date": "2026-08-21", "passenger_name": "Ada Obi"}))
    assert out["locked"] is True
    assert "Held" in out["summary"] and "Locked" not in out["summary"]


def test_beep_body_promises_recheck_not_lock(session_factory):
    from FareBeep.alerts import SubscriptionMonitor
    db = session_factory()
    db.add(User(phone="+2348077777777"))
    db.commit()
    user = db.query(User).filter_by(phone="+2348077777777").one()
    sub = Subscription(user_id=user.user_id, origin="LOS",
                       destination="ABV", target_price=80000.0,
                       last_price=90000.0)
    db.add(sub)
    db.commit()

    class _TextOnly:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append(body)
            return True

    fake = _TextOnly()
    monitor = SubscriptionMonitor(db, notifier=fake)
    assert monitor._send_beep(
        sub, {"price": 75000.0, "airline": "Air Peace"}) is True
    assert "re-check it live" in fake.sent[0]
    db.close()
