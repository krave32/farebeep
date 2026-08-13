"""THE TRANSACTIONAL LOOP - FareBeep's core business engine.

Flow (per the settlement brief):

  1. State Management - "BOOK" -> a `booking_session` row is created with
     status = "pending" and expires_at = now() + 10 minutes
  2. The Price Lock   - the fare is refreshed LIVE (SerpApi, ledger ignored)
     so the quoted price is real at lock time
  3. The Payment Link - a Paystack Test Link is generated for
     (Net_Fare + ARHA_Markup + 100) / (1 - 0.015)
  4. The Logic        - when the Paystack webhook (charge.success) arrives:
                          * now() <= expires_at -> mark paid -> ticket
                            (PNR FB-XXXX) + status watch
                          * now()  >  expires_at -> mark EXPIRED -> REFUND
                            REQUIRED alert; the airline API is NEVER called

The fee is user-funded, not absorbed: the user pays `gross` such that
after Paystack's cut the utility nets exactly (net_fare + markup).
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from FareBeep.config import BOOKING_TTL_MINUTES
from FareBeep.models import BookingSession, SessionStatus, utcnow
from FareBeep.payments import (calculate_final_price,
                               initialize_paystack_payment)

logger = logging.getLogger("farebeep.transactions")


class PaystackError(Exception):
    pass


def pnr_from_ref(payment_ref: str) -> str:
    """Mock PNR shown on ticket issuance: FB-XXXX (last 4 of the ref)."""
    return f"FB-{str(payment_ref)[-4:].upper()}"


# ---------------------------------------------------------------------------
# The 10-minute loop
# ---------------------------------------------------------------------------
DEFAULT_DEPARTURE_HOUR = 8   # fallback departure time when only the date is known


def _departure_for(flight_date: str, scheduled_departure=None):
    """Departure ts for the status watch: the recorded time, else the
    flight date at 08:00 (the utility often only knows the date)."""
    if scheduled_departure is not None:
        return scheduled_departure
    d = datetime.strptime(flight_date[:10], "%Y-%m-%d")
    return d.replace(hour=DEFAULT_DEPARTURE_HOUR)


class BookingService:
    """Owns booking_session lifecycle: pending -> paid | expired | refund_flagged."""

    def __init__(self, db: Session, paystack=None,
                 ttl_minutes: int = None, clock=None,
                 watch_factory: Callable = None):
        self.db = db
        self.paystack = paystack  # kept for injection compat (unused - payments.py owns Paystack)
        self.ttl_minutes = ttl_minutes or BOOKING_TTL_MINUTES
        self.clock = clock or utcnow
        # paid bookings -> status watch hook (default: StatusService.create_watch)
        self.watch_factory = watch_factory

    # step 1+2+3: state, expiry, payment link ------------------------------
    def create_booking(self, user_id, origin: str, destination: str,
                       flight_date: str, airline_price: float,
                       flight_iata: str = None,
                       scheduled_departure=None,
                       email: str = None,
                       airline: str = None,
                       source: str = "serpapi") -> dict:
        """Create a `booking_session` row + Paystack Test Link.

        Pricing follows the settlement brief:
            total = (airline_price + ARHA_MARKUP + 100) / (1 - 0.015)

        Returns {session, payment_link, total_amount, expires_at} or raises
        PaystackError.
        """
        pricing = calculate_final_price(airline_price)
        reference = f"FB-{uuid.uuid4().hex[:12].upper()}"
        link = initialize_paystack_payment(
            reference, pricing["total_amount"], email)

        session = BookingSession(
            user_id=user_id,
            origin=origin,
            destination=destination,
            flight_date=flight_date,
            flight_iata=flight_iata,
            scheduled_departure=scheduled_departure,
            airline_price=pricing["net_fare"],
            markup=pricing["markup"],
            processing_fee=pricing["processing_fee"],
            total_price=pricing["total_amount"],
            flight_details={
                "airline": airline,
                "route": {"origin": origin, "destination": destination,
                          "flight_date": flight_date},
                "net_price": pricing["net_fare"],
                "source": source,
            },
            status=SessionStatus.PENDING.value,
            expires_at=self.clock() + timedelta(minutes=self.ttl_minutes),
            payment_ref=reference,
            paystack_access_code=link["access_code"],
            callback_url=link["authorization_url"],
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        logger.info("Booking session %s created for %s->%s, total=%s, "
                    "expires=%s", reference, origin, destination,
                    pricing["total_amount"], session.expires_at)
        return {
            "session": session,
            "payment_link": link["authorization_url"],
            "total_amount": pricing["total_amount"],
            "expires_at": session.expires_at,
        }

    # step 4: the webhook logic ---------------------------------------------
    def settle_payment(self, reference: str, paystack_status: str) -> dict:
        """Handle a Paystack webhook for `reference`.

        THE KEY RULE: if the webhook arrives after expires_at, the booking
        is EXPIRED and a REFUND REQUIRED alert fires - the airline API is
        never called. On-time payments are marked paid and ticketed.
        """
        session = (self.db.query(BookingSession)
                   .filter(BookingSession.payment_ref == reference)
                   .first())
        if session is None:
            logger.warning("Webhook for unknown reference %s", reference)
            return {"outcome": "unknown_reference", "session": None}

        now = self.clock()
        if session.status == SessionStatus.PAID.value:
            return {"outcome": "already_paid", "session": session}  # idempotent

        # expired? -> EXPIRED + REFUND REQUIRED (no airline API call)
        if now > session.expires_at:
            session.status = SessionStatus.EXPIRED.value
            self.db.commit()
            logger.warning(
                "BOOKING EXPIRED: %s paid %s AFTER expiry %s -> REFUND "
                "REQUIRED. Airline API NOT called.", reference, now,
                session.expires_at)
            return {"outcome": "refund_required", "session": session}

        if paystack_status == "success":
            session.status = SessionStatus.PAID.value
            session.paid_at = now
            self.db.commit()
            ticket = self._provision_ticket(session)
            watch = self._open_status_watch(session)
            return {"outcome": "paid", "session": session, "ticket": ticket,
                    "watch": watch, "pnr": pnr_from_ref(reference)}

        session.status = SessionStatus.EXPIRED.value
        self.db.commit()
        return {"outcome": "failed", "session": session}

    def _provision_ticket(self, session: BookingSession) -> str:
        """Mock ticket issuance - the airline/GDS API is NOT wired up.

        The deliverable is the loop's correctness (expiry gating + PNR);
        swapping this hook for a real GDS call is the go-live step.
        """
        pnr = pnr_from_ref(session.payment_ref)
        logger.info("TICKET OK (provision hook) for %s - PNR %s",
                    session.payment_ref, pnr)
        return f"TICKET:{pnr}"

    def _open_status_watch(self, session: BookingSession):
        """Auto-start the 3-hour pre-departure watch on a PAID booking.

        A watch is only meaningful when we know which flight was bought; a
        flight can always be named later (TRACK <flight>) if unknown now.
        Fails softly - a watch problem must never roll back a payment.
        """
        if not session.flight_iata:
            return None
        try:
            if self.watch_factory is not None:
                return self.watch_factory(session)
            from FareBeep.status import StatusService
            svc = StatusService(self.db)
            dep = _departure_for(session.flight_date,
                                 session.scheduled_departure)
            return svc.create_watch(
                session.id, session.user_id, session.flight_iata,
                session.flight_date, dep)
        except Exception as e:
            logger.error("Status watch creation failed for %s: %s",
                         session.payment_ref, e)
            return None

    # worker helper: sweep stale sessions -----------------------------------
    def expire_stale_sessions(self) -> int:
        """Mark pending sessions past expires_at as expired (worker cron)."""
        now = self.clock()
        stale = (self.db.query(BookingSession)
                 .filter(BookingSession.status == SessionStatus.PENDING.value,
                         BookingSession.expires_at < now)
                 .all())
        for s in stale:
            s.status = SessionStatus.EXPIRED.value
        self.db.commit()
        return len(stale)
