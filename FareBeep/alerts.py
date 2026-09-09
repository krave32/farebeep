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

Honesty gate: before a Beep goes out, the price is re-checked LIVE
(force_refresh - the same call the BOOK handshake uses, which also
UPSERTs the result back into the ledger). If the price bounced back up
in the minutes between the periodic check and the send, the alert is
held - the user must never be beeped into a price that no longer exists.
"""
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Callable, Optional

from sqlalchemy.orm import Session

from FareBeep.models import Subscription, User, utcnow

logger = logging.getLogger("farebeep.alerts")

DEFAULT_DROP_RATIO = 0.10     # 10% drop from the last observed baseline

# Fairytale-target guard: a target more than this far below the cheapest
# KNOWN fare is almost certainly unachievable. We still arm it (the
# user's explicit wish) but warn honestly instead of letting them chase
# a price that never comes.
TARGET_REALISM_RATIO = 0.70


def target_realism_note(db, origin: str, destination: str,
                        target_price) -> str | None:
    """Warn when a target price is unrealistically low, else None.

    Compares against the cheapest LEDGER fare (no live calls - subscribing
    must stay free and instant). No known fares, no target, or a sane
    target -> None (nothing to say).
    """
    if target_price is None:
        return None
    try:
        target = float(target_price)
    except (TypeError, ValueError):
        return None
    try:
        from FareBeep.search import LedgerOnlyEngine, LedgerSearch
        best = LedgerSearch(db, live=LedgerOnlyEngine()).window_cheapest(
            origin, destination)
    except Exception:
        return None  # ledger unreadable: subscribe quietly, warn never
    if not best:
        return None
    cheapest = best["price"]
    if target >= cheapest * TARGET_REALISM_RATIO:
        return None
    pct = round((1.0 - target / cheapest) * 100)
    return (f"Heads up: \u20a6{target:,.0f} is {pct}% below the cheapest "
            f"known fare (\u20a6{cheapest:,.0f}) - that price may never "
            f"come. Armed anyway; say the word and I'll watch for any "
            f"genuine drop instead.")


class SubscriptionMonitor:
    """Owns subscription lifecycle + the fare-drop detection cycle."""

    def __init__(self, db: Session, fare_provider: Callable = None,
                 fresh_provider: Callable = None,
                 notifier=None, drop_ratio: float = None,
                 clock: Callable = None):
        """
        Args:
            db: SQLAlchemy session.
            fare_provider: callable matching LedgerSearch.search(origin,
                destination, flight_date) -> {price, airline, verify_link} or
                None. Defaults to LedgerSearch over the same db.
            fresh_provider: the LIVE re-check used before a Beep is sent
                (defaults to LedgerSearch.search with force_refresh=True,
                exactly like the BOOK handshake - ledger bypassed, result
                UPSERTed back).
            notifier: outbound WhatsApp client with .send_text(to, body).
            drop_ratio: rolling-alert threshold (default 0.10 = 10%).
            clock: time provider (default models.utcnow).
        """
        self.db = db
        if fare_provider is None:
            from FareBeep.search import LedgerSearch
            fare_provider = LedgerSearch(db).search
        self.fare_provider = fare_provider
        if fresh_provider is None:
            from FareBeep.search import LedgerSearch
            fresh_provider = partial(LedgerSearch(db).search,
                                     force_refresh=True)
        self.fresh_provider = fresh_provider
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

        Before any Beep is sent the price is re-checked LIVE - the alert
        only goes out while the fresh price is still below the threshold,
        and it carries the FRESH price (never the periodic one).

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
            try:
                price = float(fare["price"])
            except (TypeError, ValueError, KeyError) as e:
                logger.warning("Alert cycle unusable fare for %s->%s: %s",
                               sub.origin, sub.destination, e)
                continue
            try:
                if self._should_beep(sub, price):
                    if self._beep_if_still_true(sub, price):
                        beeps += 1
                else:
                    self._observe(sub, price)
            except Exception as e:
                # Live re-check or send failed mid-subscription: hold this
                # alert (baseline floats to the last seen price) and keep
                # checking everyone else - never abort the whole cycle.
                logger.warning("Alert cycle error on %s->%s - holding: %s",
                               sub.origin, sub.destination, e)
                try:
                    self._observe(sub, price)
                except Exception:
                    logger.exception("Alert baseline update failed for %s->%s",
                                     sub.origin, sub.destination)
                continue
        if beeps:
            logger.info("Beep cycle: %d fare-drop alert(s) sent", beeps)
        return beeps

    def _beep_if_still_true(self, sub: Subscription, price: float) -> bool:
        """The honesty gate: confirm `price` is real BEFORE notifying.

        One live force-refresh (ledger bypassed, result UPSERTed back)
        for the subscription's route/date. Still at/below the threshold ->
        Beep with the FRESH price. Bounced back up, or the live check
        failed -> the alert is held and the fresh number becomes the new
        baseline. Returns True when a Beep was actually sent.
        """
        fresh = self._fresh_fare(sub)
        if fresh is None:
            logger.warning("Beep held for %s->%s: live re-check "
                           "unavailable - not alerting on a stale price",
                           sub.origin, sub.destination)
            self._observe(sub, price)
            return False
        if self._should_beep(sub, fresh["price"]):
            return self._send_beep(sub, fresh)
        logger.info("Beep held for %s->%s: fresh NGN %s bounced back above "
                    "the threshold", sub.origin, sub.destination,
                    fresh["price"])
        self._observe(sub, fresh["price"])
        return False

    def _observe(self, sub: Subscription, price: float) -> None:
        """Non-beep observation: float the baseline, re-arm a recovered
        target, and persist - so the next cycle judges against reality."""
        sub.last_price = price
        if (sub.target_price is not None and price > sub.target_price
                and sub.last_alerted_price is not None):
            sub.last_alerted_price = None   # recovered above target: re-arm
        self.db.commit()

    def _probe_date(self, sub: Subscription) -> str:
        """Probe date: the subscription's target_date, else 'tomorrow'."""
        if sub.target_date:
            return _as_date_str(sub.target_date)
        return (self.clock() + timedelta(days=1)).strftime("%Y-%m-%d")

    def _latest_fare(self, sub: Subscription) -> Optional[dict]:
        fare = self.fare_provider(sub.origin, sub.destination,
                                  self._probe_date(sub))
        if fare is None:
            return None
        return {"price": float(fare["price"]),
                "airline": fare.get("airline"),
                "verify_link": fare.get("verify_link")}

    def _fresh_fare(self, sub: Subscription) -> Optional[dict]:
        """ONE live price check for the subscription's route/date.

        force_refresh: the ledger is BYPASSED (never trust a cached number
        at alert time) and the result is UPSERTed back into it - the same
        contract as the BOOK handshake in main.py.
        """
        fare = self.fresh_provider(sub.origin, sub.destination,
                                   self._probe_date(sub))
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

        sent = self._deliver_beep(user, sub, body, price)
        if sent:
            sub.last_alerted_price = price
        sub.last_price = price
        self.db.commit()
        logger.info("Beep %s -> %s at NGN %s (sent=%s)",
                    sub.origin, sub.destination, price, sent)
        return sent

    def _deliver_beep(self, user, sub: Subscription, body: str,
                      price: float) -> bool:
        """Send the Beep on the right channel shape.

        Meta WhatsApp: approved UTILITY template with live params + tap
        buttons (works outside the 24h window, where plain text fails).
        Anything else (Twilio sandbox, Telegram, test doubles): the
        plain-text body, unchanged. If the template send fails (e.g. not
        approved yet), fall back to text so the Beep still goes out
        inside an open window.
        """
        from FareBeep.notifier import MetaWhatsapp
        if not isinstance(self.notifier, MetaWhatsapp):
            return self.notifier.send_text(user.phone, body)
        from FareBeep.config import META_TEMPLATE_PRICE_DROP
        from FareBeep.iata import city_name
        baseline = (sub.target_price if sub.target_price is not None
                    else sub.last_price or price)
        date_label = _as_date_str(sub.target_date) or "your dates"
        params = [user.name or "there",
                  city_name(sub.origin), city_name(sub.destination),
                  date_label, f"{price:,.0f}", f"{baseline:,.0f}"]
        sent = self.notifier.send_template(
            user.phone, META_TEMPLATE_PRICE_DROP, params,
            buttons=[{"payload": f"beep:{sub.id}"},
                     {"payload": "dismiss"}])
        if not sent:
            logger.warning("Beep template failed - falling back to text")
            sent = self.notifier.send_text(user.phone, body)
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