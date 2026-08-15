"""SQLAlchemy models for FareBeep - a Transactional Utility (NOT a SaaS).

Migrated FROM naijafly/app/models/models.py:
  - `SeenUser`          -> `User`          (phone-number identity)
  - `UserSubscription`  -> `Subscription`  (flattened: IATA codes, not FK'd
                           to a routes table; a utility stores the route
                           directly on the row)

Dropped (per the reconstruction brief):
  - Twilio-only model columns (Twilio SDK usage removed)
  - Rule-based parser models (n/a - brain.py uses Gemini)
  - SaaS-style Route/Fare satellite tables (the fare_ledger *is* the cargo)

Added for FareBeep core flows:
  - FareLedger      : the shared, community search cache (15-min TTL)
  - BookingSession  : the 10-minute transactional loop state machine
  - StatusWatch     : per-booking 3-hour pre-departure watch window
  - StatusEvent     : status-change dedupe log (template message sent once)
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        JSON, String, Text, Uuid, UniqueConstraint)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    """Naive-UTC timestamp for storage (matches Postgres timestamptz when
    the server session timezone is UTC, and survives SQLite round-trips)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uuid():
    return uuid.uuid4()


# -------------------------------------------------------------------------
# users - migrated from naijafly SeenUser (Meta/WhatsApp phone identity)
# -------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    user_id = Column(Uuid(as_uuid=True), primary_key=True, default=new_uuid)
    phone = Column(String, unique=True, index=True)   # "+2348012345678"
    name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    preferred_currency = Column(String, default="NGN")
    first_seen_at = Column(DateTime(timezone=True), default=utcnow)
    # NDPA consent record: timestamp + version of the consent text accepted
    # on the booking confirmation page. NULL until the user agrees.
    consent_at = Column(DateTime(timezone=True), nullable=True)
    consent_text_version = Column(String, nullable=True)

    subscriptions = relationship("Subscription", back_populates="user",
                                 cascade="all, delete-orphan")
    booking_sessions = relationship("BookingSession", back_populates="user",
                                    cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# subscriptions (migrated from naijafly UserSubscription)
# ---------------------------------------------------------------------------
class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "origin", "destination",
                         name="uq_subscription_user_route"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.user_id"), index=True)
    origin = Column(String)          # IATA code, e.g. "LOS"
    destination = Column(String)     # IATA code, e.g. "ABV"
    target_price = Column(Float)     # None = alert on any >10% drop
    target_date = Column(DateTime(timezone=True), nullable=True)  # NULL = rolling window
    last_price = Column(Float, nullable=True)          # last observed fare (baseline)
    last_alerted_price = Column(Float, nullable=True)  # dedupe: never re-alert same price
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User", back_populates="subscriptions")


# ---------------------------------------------------------------------------
# fare_ledger - THE SHARED LEDGER (community cache, 15-min TTL)
# One row per (origin, destination, flight_date). Upsert target for search.py.
# ---------------------------------------------------------------------------
class FareLedger(Base):
    __tablename__ = "fare_ledger"
    __table_args__ = (
        UniqueConstraint("origin", "destination", "flight_date",
                         name="uq_fare_ledger_route_date"),
    )

    id = Column(Integer, primary_key=True)
    origin = Column(String, index=True)
    destination = Column(String, index=True)
    flight_date = Column(String, index=True)       # "YYYY-MM-DD"
    price = Column(Float)
    currency = Column(String, default="NGN")
    airline = Column(String, nullable=True)
    verify_link = Column(Text, nullable=True)
    last_updated = Column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# fx_rates - USD->NGN snapshots (price tracking, one row per daily fetch)
# The worker records a snapshot every FX_RATE_TTL_HOURS so the founder can
# see the naira trend; ngn_per_usd() converts with the latest live rate.
# ---------------------------------------------------------------------------
class FxRate(Base):
    __tablename__ = "fx_rates"

    id = Column(Integer, primary_key=True)
    usd_ngn = Column(Float)                    # NGN per 1 USD
    source = Column(String, nullable=True)     # e.g. "open.er-api.com"
    fetched_at = Column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# booking_sessions - THE SETTLEMENT ENGINE'S 10-MINUTE TRANSACTIONAL LOOP
# status: pending -> paid | expired | failed
# ---------------------------------------------------------------------------
class SessionStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class BookingSession(Base):
    """One row per BOOK -> Paystack settlement attempt.

    Mission-aligned columns (see schema.sql):
      payment_ref    - unique Paystack reference (FB-<hex>)
      flight_details - JSONB snapshot of {airline, route, net_price, source}
      total_price    - what the user paid (fare + ARHA markup + fee)
      status         - pending | paid | expired | failed
      expires_at     - created_at + 10 minutes (the price-lock window)
    """

    __tablename__ = "booking_sessions"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=new_uuid)
    user_id = Column(ForeignKey("users.user_id"), index=True)
    origin = Column(String)
    destination = Column(String)
    flight_date = Column(String)             # "YYYY-MM-DD"
    flight_iata = Column(String, nullable=True)           # e.g. "P47123"
    scheduled_departure = Column(DateTime(timezone=True), nullable=True)
    airline_price = Column(Float)            # net fare from the LIVE SerpApi hit
    markup = Column(Float, default=5000.0)   # ARHA_MARKUP_NGN flat margin
    processing_fee = Column(Float)           # Paystack fee (user-funded)
    total_price = Column(Float)              # airline_price + markup + fee
    flight_details = Column(JSON, nullable=True)  # {airline, route, net_price, source}
    currency = Column(String, default="NGN")
    status = Column(String, default=SessionStatus.PENDING.value)
    expires_at = Column(DateTime(timezone=True))  # created_at + 10 minutes
    payment_ref = Column(String, unique=True, index=True)
    paystack_access_code = Column(String, nullable=True)
    callback_url = Column(String, nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User", back_populates="booking_sessions")


# ---------------------------------------------------------------------------
# status_watches - 3-hour pre-departure watch window (status.py)
# ---------------------------------------------------------------------------
class StatusWatch(Base):
    __tablename__ = "status_watches"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=new_uuid)
    booking_id = Column(ForeignKey("booking_sessions.id"))
    user_id = Column(ForeignKey("users.user_id"), index=True)
    flight_iata = Column(String)             # e.g. "P47123"
    flight_date = Column(String)             # "YYYY-MM-DD"
    scheduled_departure = Column(DateTime(timezone=True))
    watch_starts_at = Column(DateTime(timezone=True))  # departure - 3h
    last_status = Column(String, nullable=True)
    initiated = Column(Boolean, default=False)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# status_events - status-change log + template-message dedupe (status.py)
# ---------------------------------------------------------------------------
class StatusEvent(Base):
    __tablename__ = "status_events"

    id = Column(Integer, primary_key=True)
    watch_id = Column(ForeignKey("status_watches.id"), index=True)
    status = Column(String)          # "delayed" | "cancelled" | ...
    previous = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    template_sent = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "Base", "utcnow",
    "User", "Subscription", "FareLedger", "FxRate",
    "BookingSession", "SessionStatus", "StatusWatch", "StatusEvent",
]
