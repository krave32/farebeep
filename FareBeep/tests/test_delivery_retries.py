"""DELIVERY SAFETY - bounded inbound retries, receipt tracking, no double-book.

Covers task #3:
  - a failed turn is reclaimed when Meta redelivers the same wamid
    (attempts bounded by MAX_INBOUND_ATTEMPTS, then permanently dropped)
  - outbound sent/delivered/read/failed callbacks are tracked per wamid
  - the same BOOK wamid retried by Meta creates exactly ONE booking;
    settle_payment double-call stays already_paid (see test_transactions)
"""
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main, chatstate
from FareBeep.models import (Base, BookingSession, DeliveryReceipt,
                             ProcessedMessage, utcnow)


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
def client(monkeypatch, session_factory):
    monkeypatch.setattr(main, "META_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)  # no live agent
    monkeypatch.setattr(main, "MetaWhatsapp",
                        lambda *a, **k: type("W", (), {
                            "send_typing_indicator": staticmethod(
                                lambda to: True)})())
    sent = []
    monkeypatch.setattr(main, "notifier", type("N", (), {
        "send_text": staticmethod(
            lambda to, body: sent.append((to, body)) or True)})())
    return TestClient(main.app)


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _post(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhook/meta", content=body,
                       headers={"X-Hub-Signature-256": _sig(body)})


def _text_payload(mid, phone, body, ts):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"id": mid, "from": phone, "timestamp": str(ts),
         "type": "text", "text": {"body": body}}]}}]}]}


def _row(session_factory, mid):
    db = session_factory()
    try:
        return db.query(ProcessedMessage).filter_by(
            message_id=mid).first()
    finally:
        db.close()


def test_failed_turn_reclaimed_on_redelivery(client, monkeypatch,
                                             session_factory):
    """Turn 1 crashes (failed, attempts=1); Meta redelivers the same wamid
    -> reclaimed and succeeds exactly once. Restart-durable: the row is
    the only state, so a fresh process would do the same."""
    calls = []
    state = {"fail": True}

    def _handle(phone, text):
        if state["fail"]:
            raise RuntimeError("booking provider blew up")
        calls.append((phone, text))

    monkeypatch.setattr(main, "_handle_incoming_message", _handle)
    assert _post(client, _text_payload("wamid-r1", "+234801", "hi", 1)
                 ).status_code == 200
    row = _row(session_factory, "wamid-r1")
    assert row.status == "failed" and row.attempts == 1
    assert "blew up" in (row.last_error or "")
    assert calls == []

    state["fail"] = False
    assert _post(client, _text_payload("wamid-r1", "+234801", "hi", 1)
                 ).status_code == 200
    row = _row(session_factory, "wamid-r1")
    assert row.status == "done" and row.attempts == 1
    assert calls == [("+234801", "hi")]


def test_exhausted_failures_dropped(client, monkeypatch, session_factory):
    """attempts == MAX means stop: further redeliveries ack 200 but never
    re-run the turn (no infinite retry loop, no duplicate side-effects)."""
    db = session_factory()
    db.add(ProcessedMessage(message_id="wamid-old", phone="+234801",
                            message_type="text", status="failed",
                            attempts=main.MAX_INBOUND_ATTEMPTS))
    db.commit()
    db.close()
    calls = []
    monkeypatch.setattr(main, "_handle_incoming_message",
                        lambda phone, text: calls.append((phone, text)))
    assert _post(client, _text_payload("wamid-old", "+234801", "hi", 5)
                 ).status_code == 200
    assert calls == []


def test_status_callbacks_tracked(client, session_factory):
    """Outbound lifecycle lands in delivery_receipts: sent -> delivered,
    plus a failed receipt stays queryable (and never auto-resends)."""
    def _status(mid, status):
        return {"entry": [{"changes": [{"value": {"statuses": [
            {"id": mid, "status": status, "timestamp": "7",
             "recipient_id": "+234801"}]}}]}]}

    assert _post(client, _status("out-1", "sent")).status_code == 200
    assert _post(client, _status("out-1", "delivered")).status_code == 200
    assert _post(client, _status("out-9", "failed")).status_code == 200

    db = session_factory()
    try:
        sent_row = db.query(DeliveryReceipt).filter_by(
            message_id="out-1").first()
        failed_row = db.query(DeliveryReceipt).filter_by(
            message_id="out-9").first()
    finally:
        db.close()
    assert sent_row.status == "delivered" and sent_row.phone == "+234801"
    assert failed_row.status == "failed"


def test_same_book_wamid_creates_one_booking(client, monkeypatch,
                                             session_factory):
    """Meta retries the SAME book wamid -> the concierge turn (and its
    create_booking) runs exactly once. No blind repeat of the booking."""
    phone = "+2348012345678"
    created = []

    class _FakeSearch:
        def __init__(self, db):
            pass

        def search(self, *a, **k):
            return {"source": "serpapi", "flight_date": "2026-09-20",
                    "price": 85000.0, "airline": "Rano Air",
                    "flight_number": "RN 303",
                    "verify_link": "https://example.com/x"}

    class _FakeBookings:
        def __init__(self, db):
            pass

        def create_booking(self, user_id, origin, destination, flight_date,
                           airline_price, **kwargs):
            ref = f"FB-T{len(created) + 1}"
            session = BookingSession(
                user_id=user_id, origin=origin, destination=destination,
                flight_date=flight_date, airline_price=airline_price,
                markup=5000.0, processing_fee=0.0,
                total_price=airline_price, status="pending",
                expires_at=utcnow() + timedelta(minutes=10),
                payment_ref=ref)
            db = main.SessionLocal()
            db.add(session)
            db.commit()
            created.append(ref)
            return {"session": session, "total_amount": airline_price,
                    "expires_at": session.expires_at,
                    "payment_link": "https://checkout.paystack.com/x"}

    monkeypatch.setattr(main, "LedgerSearch", _FakeSearch)
    monkeypatch.setattr(main, "BookingService", _FakeBookings)
    db = session_factory()
    chatstate.set_last_fare(db, phone, {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-09-20", "price": 85000.0,
        "airline": "Rano Air"})
    db.commit()
    db.close()

    payload = _text_payload("wamid-book1", phone, "BOOK", 11)
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200  # Meta retry
    assert created == ["FB-T1"]


def test_booking_crash_mid_turn_documents_duplicates(client, monkeypatch,
                                                     session_factory):
    """Mid-turn crash honesty. Evidence first: _handle_incoming_message
    swallows concierge errors itself (a failed create_booking becomes a
    'try again' reply, turn marked done) - so the crash that reaches the
    bridge is one that kills the turn AFTER the booking row exists but
    BEFORE done (process death in that window). Simulated here by running
    the real BOOK turn once, then raising post-write.

    Result: recovery re-runs the whole turn -> a SECOND pending session
    with a NEW ref. So at-least-once redelivery CAN duplicate the booking
    ROW. What is evidenced against double CHARGING: unique payment_ref
    per session, settle_payment idempotency (already_paid - see
    test_transactions), settle reachable only via the Paystack webhook,
    and the worker expiry sweep clearing stale pendings. Exactly-once
    booking is NOT claimed."""
    phone = "+2348012345678"
    created = []
    real_handle = main._handle_incoming_message
    mode = {"crash": True}

    class _FakeSearch:
        def __init__(self, db):
            pass

        def search(self, *a, **k):
            return {"source": "serpapi", "flight_date": "2026-09-20",
                    "price": 85000.0, "airline": "Rano Air",
                    "flight_number": "RN 303",
                    "verify_link": "https://example.com/x"}

    class _CountingBookings:
        def __init__(self, db):
            pass

        def create_booking(self, user_id, origin, destination, flight_date,
                           airline_price, **kwargs):
            ref = f"FB-C{len(created) + 1}"
            session = BookingSession(
                user_id=user_id, origin=origin, destination=destination,
                flight_date=flight_date, airline_price=airline_price,
                markup=5000.0, processing_fee=0.0,
                total_price=airline_price, status="pending",
                expires_at=utcnow() + timedelta(minutes=10),
                payment_ref=ref)
            db = main.SessionLocal()
            db.add(session)
            db.commit()
            created.append(ref)
            return {"session": session, "total_amount": airline_price,
                    "expires_at": session.expires_at,
                    "payment_link": "https://checkout.paystack.com/x"}

    def _handle_then_die(p, text):
        real_handle(p, text)  # real BOOK turn: session row written...
        if mode["crash"]:
            raise RuntimeError("process died before done-marking")

    monkeypatch.setattr(main, "LedgerSearch", _FakeSearch)
    monkeypatch.setattr(main, "BookingService", _CountingBookings)
    monkeypatch.setattr(main, "_handle_incoming_message", _handle_then_die)
    db = session_factory()
    chatstate.set_last_fare(db, phone, {
        "origin_iata": "LOS", "destination_iata": "ABV",
        "flight_date": "2026-09-20", "price": 85000.0,
        "airline": "Rano Air"})
    db.commit()
    db.close()

    payload = _text_payload("wamid-bookcrash", phone, "BOOK", 12)
    assert _post(client, payload).status_code == 200  # turn crashes
    db = session_factory()
    pendings = db.query(BookingSession).filter_by(status="pending").all()
    db.close()
    assert [s.payment_ref for s in pendings] == ["FB-C1"]

    mode["crash"] = False
    assert _post(client, payload).status_code == 200  # Meta redelivers
    db = session_factory()
    try:
        pendings = db.query(BookingSession).filter_by(
            status="pending").order_by(BookingSession.created_at).all()
        refs = [s.payment_ref for s in pendings]
        paid = db.query(BookingSession).filter(
            BookingSession.status != "pending").count()
    finally:
        db.close()
    # Documented, not hidden: TWO pending rows, unique refs, NOTHING paid
    # (settle happens only via the Paystack webhook per ref).
    assert refs == ["FB-C1", "FB-C2"]
    assert paid == 0
