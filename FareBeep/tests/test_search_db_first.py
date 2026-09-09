"""VERIFICATION - the search flow must ALWAYS check the database before the API.

These tests pin the exact 6-step contract from the reconstruction brief:
  1 request -> 2 ledger check -> 3 hit (<500ms) | 4 miss -> SerpApi ->
  5 normalization (local dict) -> 6 ledger UPSERT (community benefit)
"""
from datetime import datetime, timedelta

import pytest

from FareBeep import search as search_module
from FareBeep.iata import resolve_iata
from FareBeep.models import FareLedger, Subscription, User, utcnow
from FareBeep.search import (LedgerSearch, SearchError,
                             SerpApiGoogleFlights, get_adaptive_ttl,
                             get_ledger_ttl)


class FakeLiveApi:
    """Records every call so tests can prove order + count."""

    def __init__(self, fare=None, fares=None):
        self.fare = fare or {"price": 98000.0, "currency": "NGN",
                             "airline": "Air Peace",
                             "verify_link": "https://google.com/travel/flights?q=x"}
        self.fares = fares
        self.calls = []

    def fetch(self, origin, destination, flight_date):
        self.calls.append((origin, destination, flight_date))
        return self.fare

    def fetch_list(self, origin, destination, flight_date, limit=3):
        self.calls.append((origin, destination, flight_date))
        return (self.fares or [self.fare])[:max(1, limit)]


class ScriptedLive(FakeLiveApi):
    """Returns one price per fetch call, in order - for the low-price
    anomaly re-verification scenarios."""

    def __init__(self, prices):
        super().__init__()
        self.prices = list(prices)

    def fetch(self, origin, destination, flight_date):
        self.calls.append((origin, destination, flight_date))
        p = self.prices.pop(0)
        return {"price": p, "currency": "NGN", "airline": "Air Peace",
                "verify_link": "https://flights.example.com/x"}


@pytest.fixture
def search(db):
    live = FakeLiveApi()
    return LedgerSearch(db, live=live, ledger_ttl_minutes=20), live


def test_ledger_is_checked_before_the_api(search):
    """Order guarantee: 'ledger' must precede any 'api' activity."""
    service, _ = search
    service.search("Lagos", "Abuja", "2026-08-20")
    assert service.call_order[0] == "ledger"


def test_miss_then_upsert_means_second_call_is_a_hit(search):
    """Step 4 miss -> step 6 UPSERT -> next request = step 3 hit (no API)."""
    service, live = search
    first = service.search("Abuja", "Port Harcourt", "2026-08-21")
    assert first["source"] == "serpapi"
    assert len(live.calls) == 1

    second = service.search("Abuja", "Port Harcourt", "2026-08-21")
    assert second["source"] == "ledger"
    assert second["price"] == 98000.0
    assert len(live.calls) == 1          # the API was NOT called again


def test_fresh_hit_never_calls_the_api(search):
    """A previously cached route+date returns instantly, zero API spend."""
    service, live = search
    service.search("Lagos", "Kano", "2026-08-22")
    live.calls.clear()
    hit = service.search("Lagos", "Kano", "2026-08-22")
    assert hit["source"] == "ledger"
    assert live.calls == []


def test_stale_ledger_forces_a_miss(search, db):
    """last_updated older than the 20-min TTL -> the engine is called again."""
    service, live = search
    service.search("Lagos", "Kano", "2026-08-23")
    live.calls.clear()
    row = db.query(FareLedger).first()
    row.last_updated = utcnow() - timedelta(minutes=21)
    db.commit()

    service.clock = lambda: utcnow() + timedelta(minutes=16)
    result = service.search("Lagos", "Kano", "2026-08-23")
    assert result["source"] == "serpapi"
    assert live.calls == [("LOS", "KAN", "2026-08-23")]


# ---------------------------------------------------------------------------
# Dynamic ledger TTL - get_ledger_ttl (8 min trunk routes Mon/Fri, else 15)
# ---------------------------------------------------------------------------
def _monday():
    # Pinned to Monday NOON: deriving from the wall clock lets +9/+12min
    # arithmetic roll into Tuesday when the suite runs late at night,
    # flipping peak-weekday expectations. Noon never rolls over.
    today = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    return monday.replace(hour=12, minute=0, second=0, microsecond=0)


def test_get_ledger_ttl_trunk_routes_shrink_to_8_on_peak_days():
    monday = _monday()
    friday = monday + timedelta(days=4)
    tuesday = monday + timedelta(days=1)
    for route in [("LOS", "ABV"), ("ABV", "LOS"), ("LOS", "PHC"),
                  ("PHC", "LOS"), ("ABV", "PHC"), ("PHC", "ABV")]:
        assert get_ledger_ttl(route[0], route[1], clock=lambda: monday) == 8
        assert get_ledger_ttl(route[0], route[1], clock=lambda: friday) == 8
        assert get_ledger_ttl(route[0], route[1], clock=lambda: tuesday) == 15


def test_get_ledger_ttl_other_routes_stay_at_15_even_on_peak_days():
    monday = _monday()
    assert get_ledger_ttl("LOS", "KAN", clock=lambda: monday) == 15
    assert get_ledger_ttl("ABV", "KAN", clock=lambda: monday) == 15


def test_get_ledger_ttl_is_case_insensitive():
    monday = _monday()
    assert get_ledger_ttl("los", "abv", clock=lambda: monday) == 8
    assert get_ledger_ttl("Los", "Abv", clock=lambda: monday) == 8


# ---------------------------------------------------------------------------
# Adaptive TTL - freshness follows the flight (pure cache policy: no
# provider specifics, SerpApi-free)
# ---------------------------------------------------------------------------
FIXED_MONDAY = datetime(2026, 9, 7, 12, 0, 0)  # a Monday, off-peak season


def _fixed(dt=FIXED_MONDAY):
    return lambda: dt


def _future(days):
    return (FIXED_MONDAY + timedelta(days=days)).strftime("%Y-%m-%d")


def test_adaptive_imminent_flight_is_5_minutes(db):
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(1), _fixed()) == 5
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(2), _fixed()) == 5
    # non-trunk route tightens too: urgency beats the 15-min base
    assert get_adaptive_ttl(db, "LOS", "KAN", _future(0), _fixed()) == 5


def test_adaptive_normal_window_uses_base(db):
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(7), _fixed()) == 8
    assert get_adaptive_ttl(db, "LOS", "KAN", _future(7), _fixed()) == 15


def test_adaptive_far_horizon_relaxes(db):
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(38), _fixed()) == 30
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(147), _fixed()) == 45


def test_adaptive_no_date_and_past_dates_stay_base(db):
    assert get_adaptive_ttl(db, "LOS", "ABV", None, _fixed()) == 8
    assert get_adaptive_ttl(db, "LOS", "ABV", "", _fixed()) == 8
    assert get_adaptive_ttl(db, "LOS", "ABV", "2026-08-17", _fixed()) == 8
    assert get_adaptive_ttl(db, "LOS", "KAN", "2026-08-17", _fixed()) == 15


def test_adaptive_watcher_caps_far_date(db):
    user = User(phone="+2348000000001")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(Subscription(user_id=user.user_id, origin="LOS",
                        destination="ABV"))
    db.commit()
    assert get_adaptive_ttl(db, "LOS", "ABV", _future(147), _fixed()) == 10
    # unwatched route on the same horizon stays relaxed
    assert get_adaptive_ttl(db, "LOS", "KAN", _future(147), _fixed()) == 45


def test_adaptive_festive_season_tightens_all_routes(db):
    xmas = datetime(2026, 12, 20, 12, 0, 0)  # a Sunday
    assert get_adaptive_ttl(db, "LOS", "KAN", "2026-12-28",
                             _fixed(xmas)) == 8
    # far horizon stays relaxed even in season (stable horizon wins)
    assert get_adaptive_ttl(db, "LOS", "KAN", "2027-02-01",
                             _fixed(xmas)) == 30


def test_adaptive_public_holiday_tightens(db):
    oct1 = datetime(2026, 10, 1, 12, 0, 0)  # Independence Day, Thursday
    assert get_adaptive_ttl(db, "LOS", "KAN", "2026-10-10",
                             _fixed(oct1)) == 8


def test_ledger_hit_honors_adaptive_window(db):
    """Imminent flight: a 4-min-old row is live, a 6-min-old row is not."""
    service = LedgerSearch(db, live=FakeLiveApi())
    tomorrow = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date=tomorrow, price=90000.0, currency="NGN",
                      airline="Air Peace",
                      last_updated=utcnow() - timedelta(minutes=4)))
    db.commit()
    assert service._ledger_hit("LOS", "ABV", tomorrow) is not None
    row = db.query(FareLedger).first()
    row.last_updated = utcnow() - timedelta(minutes=6)
    db.commit()
    assert service._ledger_hit("LOS", "ABV", tomorrow) is None


def test_trunk_route_on_monday_stale_after_8_minutes(db):
    """LOS-ABV on a Monday: a 9-minute-old row is NOT served as live."""
    live = FakeLiveApi()
    service = LedgerSearch(db, live=live)
    monday = _monday()
    service.search("Lagos", "Abuja", "2026-08-17")
    live.calls.clear()
    row = db.query(FareLedger).first()
    service.clock = lambda: monday + timedelta(minutes=9)
    row.last_updated = monday + timedelta(minutes=1)
    db.commit()

    result = service.search("Lagos", "Abuja", "2026-08-17")
    assert result["source"] == "serpapi"
    assert live.calls == [("LOS", "ABV", "2026-08-17")]


def test_trunk_route_on_monday_fresh_within_8_minutes_is_hit(db):
    """An 8-minute-old row is still served - no unnecessary API spend."""
    live = FakeLiveApi()
    service = LedgerSearch(db, live=live)
    monday = _monday()
    service.search("Lagos", "Abuja", "2026-08-17")
    live.calls.clear()
    row = db.query(FareLedger).first()
    service.clock = lambda: monday + timedelta(minutes=7)
    row.last_updated = monday + timedelta(minutes=1)
    db.commit()

    result = service.search("Lagos", "Abuja", "2026-08-17")
    assert result["source"] == "ledger"
    assert live.calls == []


def test_non_trunk_route_on_monday_keeps_15_minute_window(db):
    """LOS-KAN on a Monday: an 11-minute-old row is still live."""
    live = FakeLiveApi()
    service = LedgerSearch(db, live=live)
    monday = _monday()
    service.search("Lagos", "Kano", "2026-08-17")
    live.calls.clear()
    row = db.query(FareLedger).first()
    service.clock = lambda: monday + timedelta(minutes=12)
    row.last_updated = monday + timedelta(minutes=1)
    db.commit()

    result = service.search("Lagos", "Kano", "2026-08-17")
    assert result["source"] == "ledger"
    assert live.calls == []


def test_api_receives_normalized_iata_not_city_names(search):
    """Step 5: the local dict (iata.py) must map cities BEFORE SerpApi.

    'Abuja' -> 'ABV', 'Port Harcourt' -> 'PHC' - the engine never sees
    a raw city name, so it can never throw a 'invalid airport' error.
    """
    service, live = search
    service.search("Abuja", "Port Harcourt", "2026-08-24")
    assert live.calls == [("ABV", "PHC", "2026-08-24")]


def test_ledger_row_is_upserted_not_duplicated(search, db):
    """The community ledger keeps ONE row per (route, date); re-search updates."""
    service, _ = search
    service.search("Lagos", "Abuja", "2026-08-25")
    service.search("Lagos", "Abuja", "2026-08-25")
    rows = db.query(FareLedger).all()
    assert len(rows) == 1
    assert rows[0].origin == "LOS" and rows[0].destination == "ABV"


def test_unresolvable_route_returns_none(search):
    """Garbage city names are rejected by the dict, never sent to an API."""
    service, live = search
    result = service.search("Xanadu", "Atlantis", "2026-08-26")
    assert result is None
    assert live.calls == []


def test_resolve_iata_ph_vs_phc():
    """The classic LLM mistake - 'PH' must resolve to 'PHC'."""
    assert resolve_iata("PH") == "PHC"
    assert resolve_iata("abuja") == "ABV"
    assert resolve_iata("Port Harcourt") == "PHC"
    assert resolve_iata("LOS") == "LOS"
    assert resolve_iata("Mars") is None


# ---------------------------------------------------------------------------
# Low-price anomaly verification - _verify_and_upsert
# ---------------------------------------------------------------------------
def test_first_ever_search_skips_low_price_check(db):
    """No previous ledger entry -> no re-verify: one API call, direct upsert."""
    live = FakeLiveApi()   # default fare 98000
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)

    result = service.search("Lagos", "Abuja", "2026-08-20")

    assert result["source"] == "serpapi"
    assert live.calls == [("LOS", "ABV", "2026-08-20")]
    assert db.query(FareLedger).first().price == 98000.0


def test_suspicious_drop_confirmed_by_second_call_is_cached(db, monkeypatch):
    """>20% below the previous row: after the wait, a confirming second
    call makes the low price real - cached and quoted as-is."""
    monkeypatch.setattr(search_module, "ANOMALY_RECHECK_WAIT_SECONDS", 0)
    live = ScriptedLive([100000.0, 70000.0, 70000.0])
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)
    service.search("Lagos", "Abuja", "2026-08-20")      # baseline 100k

    result = service.search("Lagos", "Abuja", "2026-08-20",
                            force_refresh=True)

    assert live.calls == [("LOS", "ABV", "2026-08-20")] * 3
    assert result["price"] == 70000.0
    assert db.query(FareLedger).first().price == 70000.0


def test_suspicious_drop_bounced_back_caches_the_higher_price(db, monkeypatch):
    """The glitch pulled back: the second (higher) result is cached and
    quoted - the low price never enters the ledger."""
    monkeypatch.setattr(search_module, "ANOMALY_RECHECK_WAIT_SECONDS", 0)
    live = ScriptedLive([100000.0, 70000.0, 95000.0])
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)
    service.search("Lagos", "Abuja", "2026-08-20")      # baseline 100k

    result = service.search("Lagos", "Abuja", "2026-08-20",
                            force_refresh=True)

    assert live.calls == [("LOS", "ABV", "2026-08-20")] * 3
    assert result["price"] == 95000.0
    assert db.query(FareLedger).first().price == 95000.0


def test_normal_price_movement_never_triggers_recheck(db):
    """A <20% drop does not wait and does not call the engine twice."""
    live = ScriptedLive([100000.0, 85000.0])
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)
    service.search("Lagos", "Abuja", "2026-08-20")

    result = service.search("Lagos", "Abuja", "2026-08-20",
                            force_refresh=True)

    assert live.calls == [("LOS", "ABV", "2026-08-20")] * 2
    assert result["price"] == 85000.0
    assert db.query(FareLedger).first().price == 85000.0


def test_search_verify_false_skips_recheck_wait(db, monkeypatch):
    """Browse paths (chat/voice) skip the 90s anomaly hold: the fare is
    quoted and cached immediately; BOOK re-verifies before money moves."""
    sleeps = []
    monkeypatch.setattr(search_module.time, "sleep",
                        lambda s: sleeps.append(s))
    live = ScriptedLive([100000.0, 70000.0])
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)
    service.search("Lagos", "Abuja", "2026-08-20")      # baseline 100k

    result = service.search("Lagos", "Abuja", "2026-08-20",
                            force_refresh=True, verify=False)

    assert result["price"] == 70000.0
    assert sleeps == []                                 # no 90s hold
    assert len(live.calls) == 2                         # no re-check call
    assert db.query(FareLedger).first().price == 70000.0


def test_verify_false_still_skips_surge_upsert(db):
    """No hold does not mean no filter: absurd prices are quoted with a
    flag but never poison the ledger."""
    live = FakeLiveApi()
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20,
                           price_guardrail=50000.0)
    result = service.search("Lagos", "Abuja", "2026-08-20", verify=False)

    assert result["price"] == 98000.0
    assert result["above_guardrail"] is True
    assert db.query(FareLedger).count() == 0


# ---------------------------------------------------------------------------
# The ranked-list flow (the "reply 1, 2 or 3 to lock" demo)
# ---------------------------------------------------------------------------
def test_fetch_list_parses_sorts_limits_and_converts_to_ngn(monkeypatch):
    """best_flights + one-way other_flights -> ranked NGN list, limit applied."""
    engine = SerpApiGoogleFlights(api_key="test-key", fx_rate=1500.0)
    data = {
        "best_flights": [
            {"price": 150, "flights": [
                {"airline": "Arik Air", "departure_time": "07:10",
                 "flight_number": "W3 101"}],
             "link": "https://www.google.com/travel/flights?q=a"},
            {"price": 120, "flights": [
                {"airline": "Air Peace", "departure_time": "08:30",
                 "flight_number": "P4 202"}],
             "link": "https://www.google.com/travel/flights?q=b"},
        ],
        "other_flights": [
            {"type": "One way", "price": 200, "flights": [
                {"airline": "Ibom Air", "departure_time": "10:45",
                 "flight_number": "QI 303"}]},
        ],
        "search_metadata": {"google_flights_url":
                            "https://www.google.com/travel/flights?q=all"},
    }
    monkeypatch.setattr(engine, "_request_data",
                        lambda o, d, f: data)
    fares = engine.fetch_list("LOS", "ABV", "2026-08-20", limit=3)

    assert [f["price"] for f in fares] == [180000.0, 225000.0, 300000.0]
    assert fares[0]["airline"] == "Air Peace"
    assert fares[0]["departs_at"] == "08:30"
    assert fares[0]["flight_number"] == "P4 202"
    assert fares[2]["airline"] == "Ibom Air"        # one-way other_flights kept
    assert fares[2]["departs_at"] == "10:45"
    assert all(f["currency"] == "NGN" for f in fares)
    assert "curr=NGN" in fares[0]["verify_link"]
    assert all(f["verify_link"].startswith("https://") for f in fares)

    assert len(engine.fetch_list("LOS", "ABV", "2026-08-20", limit=2)) == 2


def test_fetch_list_carries_arrival_and_duration(monkeypatch):
    """Extra inventory facts ride along when the engine provides them -
    cards render them, lean fares render without."""
    engine = SerpApiGoogleFlights(api_key="test-key", fx_rate=1500.0)
    data = {
        "best_flights": [
            {"price": 120, "duration": "1h 15m", "flights": [
                {"airline": "Air Peace", "departure_time": "08:30",
                 "arrival_time": "09:45", "flight_number": "P4 202"}],
             "link": "https://www.google.com/travel/flights?q=b"},
        ],
    }
    monkeypatch.setattr(engine, "_request_data",
                        lambda o, d, f: data)
    (fare,) = engine.fetch_list("LOS", "ABV", "2026-08-20")
    assert fare["arrival_time"] == "09:45"
    assert fare["duration"] == "1h 15m"


def test_fetch_list_dedupes_to_one_fare_per_airline(monkeypatch):
    """Three Air Peace departures + Rano + Arik -> ONE per airline (cheapest),
    ranked - the reply must read like a person, not three flights on the
    same airline."""
    engine = SerpApiGoogleFlights(api_key="test-key", fx_rate=1500.0)
    data = {
        "best_flights": [
            {"price": 150, "flights": [
                {"airline": "Air Peace", "departure_time": "07:10",
                 "flight_number": "P4 101"}], "link": "https://g.com/1"},
            {"price": 120, "flights": [
                {"airline": "Air Peace", "departure_time": "08:30",
                 "flight_number": "P4 202"}], "link": "https://g.com/2"},
            {"price": 100, "flights": [
                {"airline": "Rano Air", "departure_time": "06:00",
                 "flight_number": "RN 303"}], "link": "https://g.com/3"},
            {"price": 90, "flights": [
                {"airline": "Arik Air", "departure_time": "09:00",
                 "flight_number": "W3 404"}], "link": "https://g.com/4"},
        ]
    }
    monkeypatch.setattr(engine, "_request_data", lambda o, d, f: data)
    fares = engine.fetch_list("LOS", "ABV", "2026-08-29")
    assert [f["airline"] for f in fares] == ["Arik Air", "Rano Air", "Air Peace"]
    assert [f["price"] for f in fares] == [135000.0, 150000.0, 180000.0]
    assert fares[2]["flight_number"] == "P4 202"   # cheapest Air Peace kept


def test_fetch_list_collapses_single_airline_to_one(monkeypatch):
    """Only one airline serves the route -> a single (cheapest) fare, so the
    bot gives the classic one-fare reply instead of a pointless 1-2-3 list."""
    engine = SerpApiGoogleFlights(api_key="test-key", fx_rate=1500.0)
    data = {
        "best_flights": [
            {"price": 150, "flights": [
                {"airline": "Air Peace", "departure_time": "07:10",
                 "flight_number": "P4 101"}], "link": "https://g.com/1"},
            {"price": 120, "flights": [
                {"airline": "Air Peace", "departure_time": "08:30",
                 "flight_number": "P4 202"}], "link": "https://g.com/2"},
        ]
    }
    monkeypatch.setattr(engine, "_request_data", lambda o, d, f: data)
    fares = engine.fetch_list("LOS", "ABV", "2026-08-29")
    assert len(fares) == 1
    assert fares[0]["price"] == 180000.0
    assert fares[0]["flight_number"] == "P4 202"


def test_search_list_splits_sane_and_surge(search, db):
    """Above-guardrail results are surfaced separately (with prices), sane
    ones keep ranked order + flight_date, cheapest sane reaches the ledger."""
    service, live = search
    live.fares = [
        {"price": 98000.0, "currency": "NGN", "airline": "Rano Air",
         "departs_at": "06:00", "flight_number": "RN 333",
         "verify_link": "https://g.com/3"},
        {"price": 120000.0, "currency": "NGN", "airline": "Air Peace",
         "departs_at": "07:10", "flight_number": "P4 111",
         "verify_link": "https://g.com/1"},
        {"price": 420000.0, "currency": "NGN", "airline": "Azman Air",
         "departs_at": "09:00", "flight_number": "ZJ 222",
         "verify_link": "https://g.com/2"},
    ]
    sane, surge = service.search_list("Lagos", "Abuja", "2026-08-20")

    assert [f["price"] for f in sane] == [98000.0, 120000.0]
    assert [f["price"] for f in surge] == [420000.0]
    assert all(f["flight_date"] == "2026-08-20" for f in sane)
    assert "flight_date" not in surge[0]
    assert live.calls == [("LOS", "ABV", "2026-08-20")]

    row = db.query(FareLedger).first()      # cheapest SANE fare upserted
    assert row.price == 98000.0
    assert row.origin == "LOS" and row.destination == "ABV"


def test_search_list_returns_empty_pair_on_api_failure(search, monkeypatch):
    """The bot must never crash on an engine error - it just shows nothing."""
    service, live = search

    def boom(*args, **kwargs):
        raise SearchError("engine down")

    monkeypatch.setattr(live, "fetch_list", boom)
    assert service.search_list("Lagos", "Abuja", "2026-08-20") == ([], [])


def test_search_list_unresolvable_route_returns_empty(search):
    """Garbage cities are rejected by the dict before any API call."""
    service, live = search
    assert service.search_list("Xanadu", "Atlantis", "2026-08-20") == ([], [])
    assert live.calls == []


def test_search_list_skips_recheck_wait(db, monkeypatch):
    """Ranked browse never stalls 90s on a glitch-low: quote + cache now,
    BOOK re-verifies before money moves."""
    sleeps = []
    monkeypatch.setattr(search_module.time, "sleep",
                        lambda s: sleeps.append(s))
    live = FakeLiveApi(fares=[
        {"price": 70000.0, "currency": "NGN", "airline": "Air Peace",
         "departs_at": "07:10", "flight_number": "P4 111",
         "verify_link": "https://g.com/1"}])
    service = LedgerSearch(db, live=live, ledger_ttl_minutes=20)
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date="2026-08-20", price=100000.0,
                      currency="NGN", airline="Air Peace",
                      last_updated=utcnow()))
    db.commit()

    sane, surge = service.search_list("Lagos", "Abuja", "2026-08-20")

    assert [f["price"] for f in sane] == [70000.0]
    assert surge == []
    assert sleeps == []
    assert db.query(FareLedger).first().price == 70000.0
