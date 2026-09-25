"""THE DETERMINISTIC BOOKING GATE - plain-text name collection.

Regression coverage for the live E2E finding (25 Sep 2026): a user's
reply to "what's the passenger's full name?" was swallowed by the Groq
agent loop (the 'Gideon sha' timeout). The booking gate makes the
name -> 10-minute hold path deterministic: no LLM between the price
hold and money moving.
"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import chatstate, main
from FareBeep.models import Base, User, utcnow

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


class _FakeLive:
    """Live engine stand-in: force_refresh finds the seat at the shown price."""

    price = 118500.0
    calls = 0

    def fetch(self, origin, destination, flight_date):
        type(self).calls += 1
        return {"price": type(self).price, "currency": "NGN",
                "airline": "Air Peace", "verify_link": None,
                "flight_number": "P4 111", "booking_token": "tok-1"}


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)  # the deterministic path

    class FakeLedger:
        def __init__(self, db, live=None):
            self.live = live or _FakeLive()

        def search(self, origin, destination, date_, force_refresh=False,
                   verify=True):
            if force_refresh:
                # Mirror LedgerSearch.search: the live dict is stamped
                # with flight_date/source/checked_at on a miss.
                result = self.live.fetch(origin, destination, date_)
                if result is None:
                    return None
                return {**result, "flight_date": date_, "source": "live",
                        "checked_at": utcnow().isoformat()}
            return {"source": "ledger", "flight_date": date_,
                    "price": 118500.0, "airline": "Air Peace",
                    "verify_link": "https://example.com/fare"}

        def search_list(self, origin, destination, date_, limit=3):
            f = {"source": "ledger", "flight_date": date_,
                 "price": 118500.0, "airline": "Air Peace",
                 "verify_link": "https://example.com/fare"}
            return [f], []

    monkeypatch.setattr(main, "LedgerSearch", FakeLedger)

    class FakePaystack:
        def __init__(self):
            self.calls = []

        def __call__(self, ref, amount, email):
            self.calls.append((ref, amount, email))
            return {"access_code": "AC", "authorization_url": "https://pay"}

    fake_ps = FakePaystack()
    import FareBeep.transactions as tx
    monkeypatch.setattr(tx, "initialize_paystack_payment", fake_ps)

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send_text(self, to, body):
            self.sent.append((to, body))
            return True

        def send_action(self, to, action="typing"):
            return True

        def send_interactive_card(self, *a, **k):
            return True

        def answer_callback(self, *a, **k):
            return True

    fake = FakeNotifier()
    monkeypatch.setattr(main, "notifier", fake)
    return TestClient(main.app), fake, fake_ps


def _post(client, text):
    return client.post(
        "/webhook/telegram",
        json={"message": {"chat": {"id": 555001}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


def _seed_user(session_factory, name=None):
    db = session_factory()
    u = User(phone="555001", name=name)
    db.add(u)
    db.commit()
    db.close()


def _hold_via_quote_and_book(client):
    """Search, then BOOK - the quote path sets last_fare, BOOK force-refreshes."""
    _post(client, "Lagos to Abuja tomorrow")
    _post(client, "BOOK")


def test_nameless_user_gets_name_question_not_a_hold(client, session_factory):
    """A user with no name on file: BOOK holds nothing yet - it asks for
    the passenger's full name and parks the live fare in pending_booking."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    _hold_via_quote_and_book(test_client)
    body = fake.sent[-1][1]
    assert "full name" in body.lower()
    assert fake_ps.calls == []                       # nothing held yet
    db = session_factory()
    pending = chatstate.get_pending_booking(db, "555001")
    db.close()
    assert pending["price"] == 118500.0
    assert pending["stage"] == "name"
    assert pending["booking_token"] == "tok-1"


def test_reply_with_name_creates_hold_without_agent(client, session_factory):
    """THE REGRESSION: 'Gideon sha' after the name question goes straight
    to a 10-minute hold - travellers stored, Paystack link minted, no LLM."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    _hold_via_quote_and_book(test_client)
    n_links = len(fake_ps.calls)
    r = _post(test_client, "Gideon sha")
    assert r.status_code == 200
    assert len(fake_ps.calls) == n_links + 1         # the hold was created
    body = fake.sent[-1][1]
    assert "held for 10 minutes" in body
    db = session_factory()
    assert chatstate.get_pending_booking(db, "555001") is None
    from FareBeep.models import BookingSession
    session = db.query(BookingSession).order_by(
        BookingSession.created_at.desc()).first()
    details = session.flight_details
    db.close()
    assert details["travellers"]["first_name"] == "Gideon"
    assert details["travellers"]["last_name"] == "sha"
    assert details["booking_token"] == "tok-1"       # webhook can ticket


def test_named_user_skips_the_question_and_books_directly(client,
                                                          session_factory):
    """A user whose name we already know books straight through - the
    name question never fires for them."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name="Ada Obi")
    _hold_via_quote_and_book(test_client)
    bodies = [b for _, b in fake.sent]
    assert not any("full name" in b.lower() for b in bodies)
    assert len(fake_ps.calls) == 1                   # held directly


def test_cancel_word_drops_the_pending_booking(client, session_factory):
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    _hold_via_quote_and_book(test_client)
    _post(test_client, "cancel")
    db = session_factory()
    assert chatstate.get_pending_booking(db, "555001") is None
    db.close()
    assert fake_ps.calls == []                       # never held


def test_non_name_reply_falls_through_to_normal_gates(client, session_factory):
    """A stray digit (old ranked-list habit) is NOT a name: the hold is
    dropped and the message re-enters the normal gates."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    _hold_via_quote_and_book(test_client)
    n_links = len(fake_ps.calls)
    r = _post(test_client, "2")
    assert r.status_code == 200
    assert len(fake_ps.calls) == n_links             # nothing booked
    db = session_factory()
    assert chatstate.get_pending_booking(db, "555001") is None
    db.close()


def test_yes_word_is_not_swallowed_as_a_name(client, session_factory):
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    _hold_via_quote_and_book(test_client)
    _post(test_client, "yes")
    db = session_factory()
    assert chatstate.get_pending_booking(db, "555001") is None
    db.close()
    assert fake_ps.calls == []


# ---- the agent bridge (arm_booking_if_asking_name) ------------------------

def test_agent_name_question_arms_the_gate(session_factory):
    """The agent asked 'what's the passenger's full name?' -> the gate is
    armed from the last search context, so the name reply books even if
    Groq is down by then (the live-smoke failure mode)."""
    from FareBeep import agent as fare_agent
    db = session_factory()
    chatstate.set_last_fare(db, "555001", {
        "origin_iata": "ABV", "destination_iata": "PHC",
        "flight_date": "2026-09-26", "price": 113442.0,
        "airline": "Ibom Air", "booking_token": "tok-9"})
    armed = fare_agent.arm_booking_if_asking_name(
        db, "555001",
        "Got it - Abuja to Port Harcourt tomorrow, Ibom Air, NGN113,442. "
        "What's the passenger's full name?")
    pending = chatstate.get_pending_booking(db, "555001")
    db.close()
    assert armed is True
    assert pending["booking_token"] == "tok-9"
    assert pending["stage"] == "name"
    assert pending["price"] == 113442.0


def test_agent_normal_reply_does_not_arm(session_factory):
    from FareBeep import agent as fare_agent
    db = session_factory()
    chatstate.set_last_fare(db, "555001", {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-09-26", "price": 118500.0,
        "airline": "Air Peace", "booking_token": "tok-1"})
    armed = fare_agent.arm_booking_if_asking_name(
        db, "555001", "Air Peace, LOS->ABV on 26 Sep - NGN118,500. Want it?")
    pending = chatstate.get_pending_booking(db, "555001")
    db.close()
    assert armed is False
    assert pending is None


def test_armed_gate_survives_groq_outage(client, session_factory):
    """END-TO-END regression for the live-smoke failure: agent asks for
    the name (gate armed), then Groq DIES on the name reply. The fallback
    must consume the name and book - not dump help text."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    # Simulate the agent's turn: quote presented, name question asked.
    import FareBeep.agent as fare_agent
    db = session_factory()
    chatstate.set_last_fare(db, "555001", {
        "origin_iata": "ABV", "destination_iata": "PHC",
        "flight_date": "2026-09-26", "price": 113442.0,
        "airline": "Ibom Air", "booking_token": "tok-9"})
    fare_agent.arm_booking_if_asking_name(
        db, "555001", "Ibom Air NGN113,442. What's the passenger's full name?")
    db.close()
    # The name arrives while Groq is down (guided fallback handles the turn).
    r = _post(test_client, "Gideon dogari")
    assert r.status_code == 200
    assert len(fake_ps.calls) == 1                 # the hold was created
    body = fake.sent[-1][1]
    assert "held for 10 minutes" in body
    assert "simple mode" not in body               # not the resting help text
    from FareBeep.models import BookingSession
    db = session_factory()
    session = db.query(BookingSession).order_by(
        BookingSession.created_at.desc()).first()
    details = session.flight_details
    db.close()
    assert details["travellers"]["first_name"] == "Gideon"
    assert details["booking_token"] == "tok-9"


def test_fresh_route_after_armed_gate_is_not_a_name(client, session_factory):
    """Agent armed the gate, user ignores it and asks a new route - the
    route must fall through to search, never become a passenger name."""
    test_client, fake, fake_ps = client
    _seed_user(session_factory, name=None)
    import FareBeep.agent as fare_agent
    db = session_factory()
    chatstate.set_last_fare(db, "555001", {
        "origin_iata": "ABV", "destination_iata": "PHC",
        "flight_date": "2026-09-26", "price": 113442.0,
        "airline": "Ibom Air", "booking_token": "tok-9"})
    fare_agent.arm_booking_if_asking_name(
        db, "555001", "Ibom Air NGN113,442. What's the passenger's full name?")
    db.close()
    n_links = len(fake_ps.calls)
    r = _post(test_client, "Lagos to Abuja tomorrow")
    assert r.status_code == 200
    assert len(fake_ps.calls) == n_links           # nothing booked
    body = fake.sent[-1][1].lower()
    assert "fare" in body or "abuja" in body       # a search reply, not a hold
    db = session_factory()
    assert chatstate.get_pending_booking(db, "555001") is None
    db.close()
