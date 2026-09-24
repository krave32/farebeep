"""ROUTE WARMER - top routes stay fresh without humans asking.

247travels ONLY (single-source decision): the fake client below stands in
for it. No network in these tests.
"""
from datetime import datetime, timedelta

from FareBeep import warmer
from FareBeep.models import FareLedger, Subscription, User


class _Fake247:
    """Scripted async 247travels: route/date -> offers (or boom)."""

    def __init__(self, offers=None, fail_login=False, fail_search=False):
        self.offers = offers if offers is not None else []
        self.fail_login = fail_login
        self.fail_search = fail_search
        self.closed = False
        self.searched = []

    async def login(self):
        if self.fail_login:
            raise RuntimeError("bad creds")
        return "TOK"

    async def search_offers(self, origin, destination, flight_date):
        self.searched.append((origin, destination, flight_date))
        if self.fail_search:
            raise RuntimeError("supplier down")
        return [dict(o) for o in self.offers]

    async def close(self):
        self.closed = True


def _offer(price=98000.0, airline="Air Peace", token="btk_warm"):
    return {"flight_no": "P47123", "airline": "P4",
            "airline_name": airline, "departure_code": "LOS",
            "arrival_code": "ABV", "departure_time": "08:30",
            "arrival_time": "09:25", "price": price, "currency": "NGN",
            "booking_token": token}


def _dates(db, origin="LOS", destination="ABV"):
    return sorted(r.flight_date for r in db.query(FareLedger).filter_by(
        origin=origin, destination=destination).all())


def test_warmer_upserts_cheapest_per_route_date(db):
    fake = _Fake247(offers=[_offer(120000.0), _offer(75000.0)])
    stats = warmer.warm_ledger(
        db=db, routes=[("LOS", "ABV")], days_ahead=(1, 3), client=fake)

    assert stats["upserted"] == 2
    assert stats["misses"] == 0 and stats["errors"] == 0
    assert fake.closed is True
    rows = db.query(FareLedger).filter_by(origin="LOS",
                                          destination="ABV").all()
    assert sorted(r.price for r in rows) == [75000.0, 75000.0]
    assert len(_dates(db)) == 2  # one row per date (upsert target)


def test_warmer_skips_surge_never_caches_it(db):
    fake = _Fake247(offers=[_offer(999999.0)])
    stats = warmer.warm_ledger(
        db=db, routes=[("LOS", "ABV")], days_ahead=(1,), client=fake)

    assert stats["skipped_surge"] == 1
    assert stats["upserted"] == 0
    assert db.query(FareLedger).count() == 0


def test_warmer_miss_and_error_are_counted_not_raised(db):
    stats = warmer.warm_ledger(
        db=db, routes=[("LOS", "ABV")], days_ahead=(1,),
        client=_Fake247(offers=[]))
    assert stats["misses"] == 1
    assert stats["upserted"] == 0
    assert stats["errors"] == 0

    stats = warmer.warm_ledger(
        db=db, routes=[("LOS", "ABV")], days_ahead=(1,),
        client=_Fake247(offers=[], fail_search=True))
    assert stats["errors"] == 1
    assert stats["upserted"] == 0


def test_warmer_login_failure_aborts_quietly(db):
    fake = _Fake247(fail_login=True)
    stats = warmer.warm_ledger(
        db=db, routes=[("LOS", "ABV")] * 12, days_ahead=(1, 3, 7),
        client=fake)

    assert stats["errors"] == 36  # every job counted, one log line
    assert stats["upserted"] == 0
    assert fake.searched == []    # no searches attempted


def test_warmer_includes_subscriber_routes(db):
    user = User(phone="+2348000000001")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(Subscription(user_id=user.user_id, origin="LOS",
                        destination="ENU"))
    db.commit()

    fake = _Fake247(offers=[_offer()])
    warmer.warm_ledger(db=db, routes=[("LOS", "ABV")], days_ahead=(1,),
                       client=fake)

    pairs = {(o, d) for o, d, _ in fake.searched}
    assert ("LOS", "ABV") in pairs
    assert ("LOS", "ENU") in pairs  # the watched route joins in


def test_test_mode_never_caches(monkeypatch, db):
    """TRAVELS247_MODE=test (demo credentials) must not write fares: the
    ledger's baselines arm real beeps, and a demo baseline is a fake
    alert waiting to fire. Only an explicit client (a caller that has
    deliberately constructed its own) bypasses the guard."""
    from FareBeep import config
    import FareBeep.warmer as w
    monkeypatch.setattr(w, "_last_warm", None)
    monkeypatch.setattr(config, "TRAVELS247_MODE", "test")

    # No client + no override: refused, nothing searched, nothing cached.
    fake = _Fake247(offers=[_offer()])
    monkeypatch.setattr("FareBeep.travels247.Travels247Client",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not construct a client")))
    stats = warmer.warm_ledger(db=db)
    assert stats.get("skipped_test_mode") is True
    assert stats["upserted"] == 0
    assert _dates(db) == []

    # Explicit client: the caller is deliberate (unit tests, one-off
    # tooling) - the warm runs.
    stats = warmer.warm_ledger(db=db, client=_Fake247(offers=[_offer()]),
                               routes=[("LOS", "ABV")], days_ahead=(1,))
    assert stats["upserted"] == 1
    assert stats.get("skipped_test_mode") is None


def test_thin_routes_get_lighter_window(db):
    """Subscriber-only corridors warm on WARM_DAYS_AHEAD_THIN (3 dates),
    core WARM_ROUTES on the full window - cheaper fresh baselines."""
    user = User(phone="+2348000000002")
    db.add(user); db.commit(); db.refresh(user)
    db.add(Subscription(user_id=user.user_id, origin="JOS",
                        destination="ABV"))
    db.commit()

    fake = _Fake247(offers=[_offer()])
    warmer.warm_ledger(db=db, days_ahead=(1, 2, 3, 5, 7), client=fake)

    pairs = {(o, d) for o, d, _ in fake.searched}
    assert ("LOS", "ABV") in pairs                      # core route
    assert ("JOS", "ABV") in pairs                      # thin route joined
    core_dates = {dt for o, d, dt in fake.searched
                  if (o, d) == ("LOS", "ABV")}
    thin_dates = {dt for o, d, dt in fake.searched
                  if (o, d) == ("JOS", "ABV")}
    assert len(core_dates) == 5
    assert len(thin_dates) == len(warmer.WARM_DAYS_AHEAD_THIN)


def test_thin_route_already_in_core_gets_full_window(db):
    """A subscriber route that's also a WARM_ROUTES corridor must NOT be
    double-searched or downgraded to the thin window."""
    user = User(phone="+2348000000003")
    db.add(user); db.commit(); db.refresh(user)
    db.add(Subscription(user_id=user.user_id, origin="ABV",
                        destination="LOS"))
    db.commit()

    fake = _Fake247(offers=[_offer()])
    warmer.warm_ledger(db=db, days_ahead=(1, 3), client=fake)

    abv_los = [(o, d, dt) for o, d, dt in fake.searched
               if (o, d) == ("ABV", "LOS")]
    assert len(abv_los) == 2      # once per date, not duplicated


def test_warmer_interval_gate(monkeypatch, db):
    import FareBeep.warmer as w
    monkeypatch.setattr(w, "_last_warm", None)
    monkeypatch.setattr(w, "WARM_ROUTES", [("LOS", "ABV")])
    monkeypatch.setattr(w, "WARM_DAYS_AHEAD", (1,))
    now = datetime(2026, 9, 7, 12, 0, 0)
    fake = _Fake247(offers=[_offer()])

    got = w.run_warmer_if_due(db=db, client=fake, clock=lambda: now)
    assert got is not None and got["upserted"] == 1
    assert w.run_warmer_if_due(
        db=db, client=fake, clock=lambda: now) is None
    later = w.run_warmer_if_due(
        db=db, client=fake,
        clock=lambda: now + timedelta(minutes=16))
    assert later is not None
