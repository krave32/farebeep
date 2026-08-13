"""THE BEEP - subscription fare-drop alerts.

Per the FareBeep use-case spec:

  "The system monitors the Ledger. When the price for a tracked route
   drops by > 10%, a 'Utility' message is sent via Meta Cloud API. This
   high-intent alert minimizes messaging costs while maximizing
   conversion."

Two trigger rules (never both at once):

  1. Target price  - subscription carries `target_price`: alert the FIRST
                     time the fare is at/below it. A new, lower price after
                     recovery re-alerts (dedupe is by price, not forever).
  2. Rolling drop  - no target price: alert whenever the fare falls >= 10%
                     below the last observed baseline (`last_price`).

Dedupe state lives on the subscription row itself:
  - `last_price`          the most recent fare observed (baseline)
  - `last_alerted_price`  the fare we last beeped at - a higher-or-equal
                          price is never beeped again
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from FareBeep.models import Subscription, User, utcnow

logger = logging.getLogger("farebeep.alerts")

DEFAULT_DROP_RATIO = 0.10     # 10% drop from the last observed baseline


class SubscriptionMonitor:
    """Owns subscription lifecycle + the fare-drop detection cycle."""

    def __init__(self, db: Session, fare_provider: Callable = None,
                 notifier=None, drop_ratio: float = None,
                 clock: Callable = None):
        """
        Args:
            db: SQLAlchemy session.
            fare_provider: callable matching LedgerSearch.search(origin,
                destination, flight_date) -> {price, airline, verify_link} or
                None. Defaults to LedgerSearch over the same db.
            notifier: outbound WhatsApp client with .send_text(to, body).
            drop_ratio: rolling-alert threshold (default 0.10 = 10%).
            clock: time provider (default models.utcnow).
        """
        self.db = db
        if fare_provider is None:
            from FareBeep.search import LedgerSearch
            fare_provider = LedgerSearch(db).search
        self.fare_provider = fare_provider
        if notifier is None:
            from FareBeep.notifier import get_notifier
            notifier = get_notifier()
        self.notifier = notifier
        self.drop_ratio = drop_ratio if drop_ratio is not None else DEFAULT_DROP_RATIO
        self.clock = clock or utcnow

    # -----------------------------------------------------------------
    # lifecycle
    # -----------------------------------------------------------------
    def subscribe(self, user_id, origin: str, destination: str,
                  target_price: float = None,
                  target_date: str = None) -> Subscription:
        """Create or refresh one (user, route) subscription. Power idempotency:
        a second SUBSCRIBE for the same route updates the target, never
        duplicates the row (enforced by the unique constraint).
        """
        sub = (self.db.query(Subscription)
               .filter(Subscription.user_id == user_id,
                       Subscription.origin == origin.upper(),
                       Subscription.destination == destination.upper())
               .first())
        if sub is None:
            sub = Subscription(user_id=user_id, origin=origin.upper(),
                               destination=destination.upper())
            self.db.add(sub)
        sub.target_price = target_price
        sub.target_date = _as_date_value(target_date)
        # a changed target re-arms the alert (fresh dedupe baseline)
        sub.last_price = None
        sub.last_alerted_price = None
        self.db.commit()
        self.db.refresh(sub)
        logger.info("Subscription set: %s->%s target=%s date=%s (user %s)",
                    sub.origin, sub.destination, sub.target_price,
                    sub.target_date, user_id)
        return sub

    def unsubscribe(self, user_id) -> int:
        """Remove every subscription for the user; returns count removed."""
        n = (self.db.query(Subscription)
             .filter(Subscription.user_id == user_id)
             .delete(synchronize_session=False))
        self.db.commit()
        return n

    # -----------------------------------------------------------------
    # the cycle
    # -----------------------------------------------------------------
    def run_cycle(self) -> int:
        """Check every active subscription against the ledger; beep on a drop.

        Returns the number of Beep messages sent this cycle.
        """
        subs = self.db.query(Subscription).all()
        beeps = 0
        for sub in subs:
            try:
                fare = self._latest_fare(sub)
            except Exception as e:      # one bad route never breaks the cycle
                logger.warning("Alert cycle fare lookup failed for %s->%s: %s",
                               sub.origin, sub.destination, e)
                continue
            if fare is None:
                continue
            price = fare["price"]
            if self._should_beep(sub, price):
                if self._send_beep(sub, fare):
                    beeps += 1
            else:
                self._observe(sub, price)
        if beeps:
            logger.info("Beep cycle: %d fare-drop alert(s) sent", beeps)
        return beeps

    def _observe(self, sub: Subscription, price: float) -> None:
        """Non-beep observation: float the baseline, re-arm a recovered
        target, and persist - so the next cycle judges against reality."""
        sub.last_price = price
        if (sub.target_price is not None and price > sub.target_price
                and sub.last_alerted_price is not None):
            sub.last_alerted_price = None   # recovered above target: re-arm
        self.db.commit()

    def _latest_fare(self, sub: Subscription) -> Optional[dict]:
        """Probe date: the subscription's target_date, else 'tomorrow'."""
        date = _as_date_str(sub.target_date) if sub.target_date else (
            (self.clock() + timedelta(days=1)).strftime("%Y-%m-%d"))
        fare = self.fare_provider(sub.origin, sub.destination, date)
        if fare is None:
            return None
        return {"price": float(fare["price"]),
                "airline": fare.get("airline"),
                "verify_link": fare.get("verify_link")}

    def _should_beep(self, sub: Subscription, price: float) -> bool:
        """Decide whether `price` earns a Beep, WITHOUT mutating yet.

        Target rule: price <= target AND (never alerted OR a strictly lower
        price than the last alert). Rolling rule: >= drop_ratio below the
        last observed baseline. First observation only sets the baseline.
        """
        if sub.last_price is None:
            return False   # baseline only - the next cycle can beep

        if sub.target_price is not None:
            return (price <= sub.target_price
                    and (sub.last_alerted_price is None
                         or price < sub.last_alerted_price))

        return price <= sub.last_price * (1.0 - self.drop_ratio)

    def _send_beep(self, sub: Subscription, fare: dict) -> bool:
        """Push the Beep and persist the dedupe state."""
        user = (self.db.query(User).filter(User.user_id == sub.user_id)
                .first())
        if user is None:
            logger.warning("Beep skipped: user %s missing", sub.user_id)
            return False

        price = fare["price"]
        target = sub.target_price
        if target is not None:
            line = (f"{sub.origin} to {sub.destination}: now "
                    f"NGN {price:,.0f} - your target was {target:,.0f}!")
        else:
            line = (f"{sub.origin} to {sub.destination}: price dropped to "
                    f"NGN {price:,.0f} "
                    f"({_pct(sub.last_price, price):.0f}% off)")
        airline = f" via {fare['airline']}" if fare.get("airline") else ""
        link = f"\nVerify: {fare['verify_link']}" if fare.get("verify_link") else ""
        body = (f"📉 FARE BEEP\n{line}{airline}\n"
                f"Reply BOOK to buy at this price.{link}")

        sent = self.notifier.send_text(user.phone, body)
        if sent:
            sub.last_alerted_price = price
        sub.last_price = price
        self.db.commit()
        logger.info("Beep %s -> %s at NGN %s (sent=%s)",
                    sub.origin, sub.destination, price, sent)
        return sent


def _pct(baseline: float, price: float) -> float:
    return round((1.0 - price / baseline) * 100.0)


def _as_date_str(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _as_date_value(value) -> Optional[datetime]:
    """'YYYY-MM-DD' string -> naive-UTC datetime (models.utcnow convention)."""
    if value in (None, ""):
        return None
    return datetime.strptime(str(value)[:10], "%Y-%m-%d")


__all__ = ["SubscriptionMonitor", "DEFAULT_DROP_RATIO"]