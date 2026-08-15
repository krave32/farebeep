"""VERIFICATION - the search flow must ALWAYS check the database before the API.

These tests pin the exact 6-step contract from the reconstruction brief:
  1 request -> 2 ledger check -> 3 hit (<500ms) | 4 miss -> SerpApi ->
  5 normalization (local dict) -> 6 ledger UPSERT (community benefit)
"""
from datetime import timedelta

import pytest

from FareBeep.iata import resolve_iata
from FareBeep.models import FareLedger, User, utcnow
from FareBeep.search import LedgerSearch, SearchError, SerpApiGoogleFlights


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


def test_fetch_list_dedupes_to_one_fare_per_airline(monkeypatch):
    """Three Air Peace departures + Dana + Arik -> ONE per airline (cheapest),
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
                {"airline": "Dana Air", "departure_time": "06:00",
                 "flight_number": "9J 303"}], "link": "https://g.com/3"},
            {"price": 90, "flights": [
                {"airline": "Arik Air", "departure_time": "09:00",
                 "flight_number": "W3 404"}], "link": "https://g.com/4"},
        ]
    }
    monkeypatch.setattr(engine, "_request_data", lambda o, d, f: data)
    fares = engine.fetch_list("LOS", "ABV", "2026-08-29")
    assert [f["airline"] for f in fares] == ["Arik Air", "Dana Air", "Air Peace"]
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
        {"price": 98000.0, "currency": "NGN", "airline": "Dana Air",
         "departs_at": "06:00", "flight_number": "9J 333",
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
