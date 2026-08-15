"""BOOKING CONFIRMATION PAGE - price reconfirmation + NDPA consent capture.

Every booking flows through GET /book/{id} (reconfirm price) then
POST /book/{id}/confirm (record consent -> redirect to Paystack). This is
the ONLY path to payment, so consent is always captured - no text parsing.
"""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, BookingSession, User, utcnow


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch, session_factory):
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    return TestClient(main.app)


def _make_session(Session, *, expires_delta=timedelta(minutes=10),
                  airline_price=42000.0):
    db = Session()
    user = User(phone="+2348012345678")
    db.add(user)
    db.commit()
    session = BookingSession(
        user_id=user.user_id,
        origin="LOS", destination="ABV", flight_date="2026-08-20",
        airline_price=airline_price, markup=5000.0, processing_fee=730.0,
        total_price=48000.0,
        flight_details={"airline": "Air Peace"},
        expires_at=utcnow() + expires_delta,
        payment_ref=f"FB-{uuid.uuid4().hex[:12].upper()}",
        callback_url="https://checkout.paystack.com/test-access-code",
    )
    db.add(session)
    db.commit()
    return db, user, session


def test_page_reconfirms_price_breakdown(client, session_factory):
    db, user, session = _make_session(session_factory)
    r = client.get(f"/book/{session.id}")
    assert r.status_code == 200
    body = r.text
    assert "Confirm your booking" in body
    assert "Lagos" in body and "Abuja" in body          # city names resolved
    assert "Air Peace" in body                          # airline from details
    assert "42,000" in body                             # airline price
    assert "48,000" in body                             # total
    assert "Proceed to Payment" in body
    assert "consent" in body.lower() or "agree" in body.lower()
    db.close()


def test_confirm_records_consent_and_redirects(client, session_factory):
    db, user, session = _make_session(session_factory)
    assert user.consent_at is None
    r = client.post(f"/book/{session.id}/confirm", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.paystack.com/test-access-code"
    db.refresh(user)
    assert user.consent_at is not None
    assert user.consent_text_version == main.CONSENT_VERSION
    db.close()


def test_expired_session_blocks_page(client, session_factory):
    db, user, session = _make_session(session_factory,
                                      expires_delta=timedelta(minutes=-1))
    r = client.get(f"/book/{session.id}")
    assert r.status_code == 200
    assert "window" in r.text.lower() or "closed" in r.text.lower()
    assert "Proceed to Payment" not in r.text
    db.close()


def test_expired_session_blocks_confirm_no_consent(client, session_factory):
    db, user, session = _make_session(session_factory,
                                      expires_delta=timedelta(minutes=-1))
    r = client.post(f"/book/{session.id}/confirm", follow_redirects=False)
    assert "checkout.paystack.com" not in r.headers.get("location", "")
    db.refresh(user)
    assert user.consent_at is None
    db.close()


def test_unknown_session_returns_not_found(client):
    r = client.get(f"/book/{uuid.uuid4()}")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


def test_naive_expires_at_does_not_crash(client, session_factory):
    """Regression: Postgres returns expires_at as an offset-NAIVE datetime
    while utcnow() is aware - comparing them used to 500 the page."""
    db, user, session = _make_session(session_factory)
    session.expires_at = session.expires_at.replace(tzinfo=None)  # simulate PG
    db.commit()
    r = client.get(f"/book/{session.id}")
    assert r.status_code == 200
    assert "Confirm your booking" in r.text
    db.close()
