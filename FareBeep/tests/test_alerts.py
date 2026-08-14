"""THE BEEP - subscription lifecycle + >10% / target-price drop alerts."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep.alerts import SubscriptionMonitor
from FareBeep.brain import Intent, _build_intent
from FareBeep.models import Base, Subscription, User, utcnow

from datetime import timedelta

# The monitor probes "tomorrow" (clock + 1d) for dateless subscriptions,
# so the board keys must derive from the run date - not a hardcoded day
# (a hardcoded date silently misses after midnight).
PROBE_DATE = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def user(db):
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class FareBoard:
    """Scripted live-fare provider: route/date -> price or None."""

    def __init__(self, prices):
        self.prices = dict(prices)      # {"LOS-ABV-2026-08-20": 90000.0}
        self.calls = []

    def __call__(self, origin, destination, flight_date):
        key = f"{origin}-{destination}-{flight_date}"
        self.calls.append(key)
        price = self.prices.get(key)
        if price is None:
            return None
        return {"price": price, "airline": "Air Peace",
                "verify_link": "https://flights.example.com/LOS-ABV"}


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return True


def make_monitor(db, board, notifier=None, clock=None):
    return SubscriptionMonitor(db, fare_provider=board,
                               notifier=notifier or FakeNotifier(),
                               clock=clock or utcnow)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_subscribe_creates_row(db, user):
    m = SubscriptionMonitor(db)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=80000.0)

    sub = db.query(Subscription).one()
    assert sub.origin == "LOS"
    assert sub.destination == "ABV"
    assert sub.target_price == 80000.0


def test_subscribe_same_route_updates_not_duplicates(db, user):
    m = SubscriptionMonitor(db)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=80000.0)
    m.subscribe(user.user_id, "los", "abv", target_price=65000.0)

    rows = db.query(Subscription).all()
    assert len(rows) == 1
    assert rows[0].target_price == 65000.0


def test_unsubscribe_removes_all_for_user(db, user):
    m = SubscriptionMonitor(db)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.subscribe(user.user_id, "LOS", "PHC")
    assert m.unsubscribe(user.user_id) == 2
    assert db.query(Subscription).count() == 0


# ---------------------------------------------------------------------------
# rolling rule: >10% drop from baseline
# ---------------------------------------------------------------------------
def test_first_observation_sets_baseline_no_beep(db, user):
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 90000.0})
    notifier = FakeNotifier()
    m = make_monitor(db, board, notifier)

    m.subscribe(user.user_id, "LOS", "ABV")
    assert m.run_cycle() == 0
    assert notifier.sent == []
    assert db.query(Subscription).one().last_price == 90000.0


def test_ten_percent_drop_beeps_once(db, user):
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 90000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.run_cycle()                       # baseline 90,000

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 80000.0   # -11%: beep
    assert m.run_cycle() == 1
    assert len(m.notifier.sent) == 1
    body = m.notifier.sent[0][1]
    assert "FARE BEEP" in body
    assert "80,000" in body

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 80000.0   # same price: never again
    assert m.run_cycle() == 0
    assert len(m.notifier.sent) == 1


def test_under_ten_percent_never_beeps(db, user):
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 90000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.run_cycle()

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 83000.0   # -8%: below threshold
    assert m.run_cycle() == 0
    assert m.notifier.sent == []


def test_baseline_floats_up_without_alerts(db, user):
    """A price hike raises the baseline; a later drop past it beeps."""
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 90000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.run_cycle()

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 95000.0   # hike: no beep
    assert m.run_cycle() == 0

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 84000.0   # -12% from 95k: beep
    assert m.run_cycle() == 1


# ---------------------------------------------------------------------------
# target-price rule
# ---------------------------------------------------------------------------
def test_target_price_hit_beeps(db, user):
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 78000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=80000.0)
    m.run_cycle()                       # baseline 78k

    notifier = m.notifier
    # price returns to 79k: still at/below target -> beep (target rule is a hit, not a drop)
    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 79000.0
    assert m.run_cycle() == 1
    assert "target" in notifier.sent[0][1]


def test_target_above_target_no_beep(db, user):
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 85000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=80000.0)
    m.run_cycle()
    assert m.notifier.sent == []


def test_target_dedupe_rearms_after_recovery(db, user):
    """Beep at 79k; recover above target; another dip below beeps again."""
    board = FareBoard({f"LOS-ABV-{PROBE_DATE}": 78000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=80000.0)
    m.run_cycle()
    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 79000.0
    assert m.run_cycle() == 1

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 82000.0   # recovered above target
    assert m.run_cycle() == 0

    board.prices[f"LOS-ABV-{PROBE_DATE}"] = 79000.0   # dipped again: beep again
    assert m.run_cycle() == 1


# ---------------------------------------------------------------------------
# probing + robustness
# ---------------------------------------------------------------------------
def test_date_scoped_subscription_probes_its_date(db, user):
    board = FareBoard({"LOS-ABV-2026-08-20": 90000.0})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV", target_price=95000.0,
                target_date="2026-08-20")
    m.run_cycle()
    assert "LOS-ABV-2026-08-20" in board.calls


def test_rolling_subscription_probes_tomorrow(db, user):
    board = FareBoard({})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.run_cycle()
    from datetime import timedelta
    tomorrow = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert board.calls == [f"LOS-ABV-{tomorrow}"]


def test_missing_fare_skips_quietly(db, user):
    board = FareBoard({})
    m = make_monitor(db, board)
    m.subscribe(user.user_id, "LOS", "ABV")
    assert m.run_cycle() == 0
    assert db.query(Subscription).one().last_price is None


def test_one_broken_route_does_not_kill_cycle(db, user):
    def broken(origin, destination, flight_date):
        raise RuntimeError("engine down")

    m = make_monitor(db, broken)
    m.subscribe(user.user_id, "LOS", "ABV")
    m.subscribe(user.user_id, "LOS", "PHC")
    assert m.run_cycle() == 0           # no exception escapes the cycle


# ---------------------------------------------------------------------------
# brain: subscribe/unsubscribe understanding (pure, no HTTP)
# ---------------------------------------------------------------------------
def test_build_intent_subscribe_with_target():
    intent = _build_intent(
        '{"intent":"subscribe","origin":"Lagos","destination":"Abuja",'
        '"date":null,"target_price":80000,"flight":null}',
        "SUBSCRIBE LOS ABV 80000")
    assert intent.intent == "subscribe"
    assert intent.target_price == 80000.0
    assert intent.origin_iata == "LOS"
    assert intent.destination_iata == "ABV"


def test_build_intent_unsubscribe():
    intent = _build_intent(
        '{"intent":"unsubscribe","origin":null,"destination":null,'
        '"date":null,"target_price":null,"flight":null}',
        "unsubscribe")
    assert intent.intent == "unsubscribe"


def test_build_intent_rejects_bad_target_price():
    intent = _build_intent(
        '{"intent":"subscribe","origin":"Lagos","destination":"Abuja",'
        '"date":null,"target_price":"cheap","flight":null}',
        "alert me when it gets cheap")
    assert intent.intent == "subscribe"
    assert intent.target_price is None