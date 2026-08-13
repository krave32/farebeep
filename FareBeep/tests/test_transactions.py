"""THE TRANSACTIONAL LOOP - 10-minute booking_session state machine."""
from datetime import timedelta

import pytest

from FareBeep import transactions
from FareBeep.models import (BookingSession, SessionStatus, StatusWatch,
                             User, utcnow)
from FareBeep.transactions import BookingService


@pytest.fixture(autouse=True)
def fake_paystack_link(monkeypatch):
    """The Settlement Engine calls payments.initialize_paystack_payment -
    tests substitute it so no real Paystack request is made."""
    calls = []

    def _fake(ref, final_price, email):
        calls.append((ref, final_price, email))
        return {"access_code": f"AC_{ref}",
                "authorization_url": f"https://paystack.com/pay/{ref}"}

    monkeypatch.setattr(transactions, "initialize_paystack_payment", _fake)
    return calls


@pytest.fixture
def user(db):
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_create_booking_mission_schema(db, user, fake_paystack_link):
    """BOOK -> session row with the mission columns: payment_ref (unique),
    total_price, flight_details JSON, status pending, expires_at 10 minutes."""
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])

    result = svc.create_booking(
        user.user_id, "ABV", "PHC", "2026-08-20", 85000.0,
        airline="Air Peace", source="serpapi")

    session = db.query(BookingSession).first()
    assert session.status == SessionStatus.PENDING.value
    assert session.expires_at - clock[0] <= timedelta(minutes=10)
    assert session.payment_ref.startswith("FB-")
    assert session.payment_ref == fake_paystack_link[0][0]
    assert fake_paystack_link[0][1] == session.total_price
    # flight_details captures the LIVE handshake snapshot
    assert session.flight_details["airline"] == "Air Peace"
    assert session.flight_details["route"] == {
        "origin": "ABV", "destination": "PHC", "flight_date": "2026-08-20"}
    assert session.flight_details["net_price"] == 85000.0
    assert session.flight_details["source"] == "serpapi"
    # total_price = (85000 + 5000 + 100) / 0.985
    assert session.total_price == pytest.approx(91472.08)
    assert session.markup == 5000.0
    # Get returns the link + lock window
    assert "paystack.com/pay" in result["payment_link"]
    assert result["expires_at"] == session.expires_at


def test_webhook_after_expiry_triggers_refund_required(db, user):
    """THE CORE RULE: paid after expires_at -> EXPIRED + REFUND REQUIRED.
    The airline API must NOT be called (no ticket issued)."""
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(user.user_id, "LOS", "ABV", "2026-08-21",
                                 90000.0)

    # 11 minutes later the Paystack webhook arrives
    clock[0] = clock[0] + timedelta(minutes=11)
    outcome = svc.settle_payment(created["session"].payment_ref, "success")

    assert outcome["outcome"] == "refund_required"
    assert created["session"].status == SessionStatus.EXPIRED.value
    assert created["session"].paid_at is None


def test_webhook_before_expiry_marks_paid_with_pnr(db, user):
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(user.user_id, "LOS", "ABV", "2026-08-21",
                                 90000.0)

    clock[0] = clock[0] + timedelta(minutes=5)
    outcome = svc.settle_payment(created["session"].payment_ref, "success")

    assert outcome["outcome"] == "paid"
    assert created["session"].status == SessionStatus.PAID.value
    assert created["session"].paid_at is not None
    # the mock ticket PNR: FB-<last4>
    assert outcome["pnr"] == f"FB-{created['session'].payment_ref[-4:].upper()}"
    assert outcome["ticket"] == f"TICKET:{outcome['pnr']}"


def test_webhook_is_idempotent(db, user):
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(user.user_id, "LOS", "ABV", "2026-08-21",
                                 90000.0)
    svc.settle_payment(created["session"].payment_ref, "success")
    again = svc.settle_payment(created["session"].payment_ref, "success")
    assert again["outcome"] == "already_paid"


def test_expire_stale_sessions_sweep(db, user):
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    svc.create_booking(user.user_id, "LOS", "ABV", "2026-08-22", 80000.0)

    clock[0] = clock[0] + timedelta(minutes=15)
    n = svc.expire_stale_sessions()
    assert n == 1
    assert db.query(BookingSession).first().status == SessionStatus.EXPIRED.value


# ---------------------------------------------------------------------------
# paid booking -> auto status watch (3h pre-departure)
# ---------------------------------------------------------------------------
def test_paid_booking_with_flight_creates_status_watch(db, user):
    """The moment a flight-specific booking is paid, a StatusWatch exists
    with watch_starts_at = departure - 3h (worker picks it up on its cycle)."""
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(
        user.user_id, "LOS", "ABV", "2026-08-21", 90000.0,
        flight_iata="P47123", scheduled_departure=utcnow() + timedelta(hours=6))

    clock[0] = clock[0] + timedelta(minutes=5)
    outcome = svc.settle_payment(created["session"].payment_ref, "success")

    assert outcome["outcome"] == "paid"
    watch = db.query(StatusWatch).first()
    assert watch is not None
    assert watch.booking_id == created["session"].id
    assert watch.flight_iata == "P47123"
    assert watch.watch_starts_at == created["session"].scheduled_departure \
        - timedelta(hours=3)


def test_paid_booking_without_flight_skips_watch(db, user):
    """No flight number -> no watch (a watch needs a flight to poll)."""
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(user.user_id, "LOS", "ABV", "2026-08-21",
                                 90000.0)

    clock[0] = clock[0] + timedelta(minutes=5)
    svc.settle_payment(created["session"].payment_ref, "success")

    assert db.query(StatusWatch).count() == 0


def test_refund_required_booking_never_creates_watch(db, user):
    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0])
    created = svc.create_booking(
        user.user_id, "LOS", "ABV", "2026-08-21", 90000.0,
        flight_iata="P47123")

    clock[0] = clock[0] + timedelta(minutes=11)
    outcome = svc.settle_payment(created["session"].payment_ref, "success")

    assert outcome["outcome"] == "refund_required"
    assert db.query(StatusWatch).count() == 0


def test_watch_creation_failure_never_blocks_payment(db, user):
    """A broken watch hook must not roll back a successful payment."""
    clock = [utcnow()]

    def broken_factory(session):
        raise RuntimeError("watch service down")

    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0],
                         watch_factory=broken_factory)
    created = svc.create_booking(
        user.user_id, "LOS", "ABV", "2026-08-21", 90000.0,
        flight_iata="P47123")

    clock[0] = clock[0] + timedelta(minutes=5)
    outcome = svc.settle_payment(created["session"].payment_ref, "success")

    assert outcome["outcome"] == "paid"
    assert created["session"].status == SessionStatus.PAID.value
    assert outcome["watch"] is None


def test_custom_watch_factory_is_used(db, user):
    """The watch hook is injectable: the real default creates via
    StatusService, tests can substitute their own factory."""
    calls = []

    def spy_factory(session):
        calls.append(session.payment_ref)
        return "WATCH-OK"

    clock = [utcnow()]
    svc = BookingService(db, ttl_minutes=10, clock=lambda: clock[0],
                         watch_factory=spy_factory)
    created = svc.create_booking(
        user.user_id, "LOS", "ABV", "2026-08-21", 90000.0,
        flight_iata="P47123")
    svc.settle_payment(created["session"].payment_ref, "success")

    assert calls == [created["session"].payment_ref]