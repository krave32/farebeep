"""SUPPLIERS - registry selection + QuickAir client contract.

The registry must select by INVENTORY_PROVIDER (default travels247,
unknown values fall back loudly). The QuickAir client must return the
same normalized offer contract as travels247 from whatever wire format
the VENDOR block describes - via httpx MockTransport, no network.
"""
import asyncio

import httpx

from FareBeep.quickair import QuickAirClient, _normalize
from FareBeep.suppliers import get_inventory_client, pick_cheapest


def _run(coro):
    return asyncio.run(coro)


def _client_with(routes, **kw):
    """QuickAirClient bound to a MockTransport; zero-arg constructible."""
    def _handler(request):
        q = routes.get((request.method, request.url.path))
        if not q:
            return httpx.Response(404, json={"error": "no route"})
        item = q[0]
        if len(q) > 1:
            q.pop(0)
        status, body = item
        return httpx.Response(status, json=body)
    transport = httpx.MockTransport(_handler)
    return QuickAirClient(base_url="http://qa.test/api",
                          email="a@b.c", password="pw",
                          http_client=httpx.AsyncClient(
                              transport=transport))


LOGIN = {"status": "success",
         "data": {"access_token": "QTOK", "expires_in": 900}}
RAW_OFFER = {  # deliberately different field names than travels247
    "flightNumber": "QK220", "airlineCode": "QK",
    "airlineName": "QuickAir", "from": "los", "to": "ABV",
    "departTime": "07:15", "arriveTime": "08:10",
    "fare": 87500, "currency": "NGN", "token": "qtk_1"}


# -- registry ----------------------------------------------------------------

def test_registry_default_is_travels247(monkeypatch):
    monkeypatch.setattr("FareBeep.config.INVENTORY_PROVIDER", "travels247")
    from FareBeep.travels247 import Travels247Client
    assert isinstance(get_inventory_client(), Travels247Client)


def test_registry_selects_quickair(monkeypatch):
    monkeypatch.setattr("FareBeep.config.INVENTORY_PROVIDER", "quickair")
    monkeypatch.setattr("FareBeep.quickair.QUICKAIR_EMAIL", "e@x.com")
    monkeypatch.setattr("FareBeep.quickair.QUICKAIR_PASSWORD", "pw")
    c = get_inventory_client()
    from FareBeep.quickair import QuickAirClient as Q
    assert isinstance(c, Q)
    _run(c.close())


def test_registry_unknown_provider_falls_back(monkeypatch):
    monkeypatch.setattr("FareBeep.config.INVENTORY_PROVIDER", "nope")
    from FareBeep.travels247 import Travels247Client
    assert isinstance(get_inventory_client(), Travels247Client)


def test_pick_cheapest_shared_by_contract():
    assert pick_cheapest([]) is None
    offers = [{"price": 5.0, "booking_token": None},
              {"price": 9.0, "booking_token": "t2"},
              {"price": 7.0, "booking_token": "t1"}]
    assert pick_cheapest(offers)["booking_token"] == "t1"


# -- QuickAir normalization ----------------------------------------------------

def test_normalize_maps_alternate_field_names():
    o = _normalize(RAW_OFFER)
    assert o["flight_no"] == "QK220"
    assert o["airline_name"] == "QuickAir"
    assert o["departure_code"] == "LOS"       # uppercased
    assert o["price"] == 87500.0
    assert o["booking_token"] == "qtk_1"
    assert o["currency"] == "NGN"


def test_normalize_rejects_unparseable_price():
    assert _normalize({**RAW_OFFER, "fare": "free"}) is None


def test_quickair_search_returns_normalized_contract():
    qa = _client_with({
        ("POST", "/api/login"): [(200, LOGIN)],
        ("POST", "/api/flights/search"): [(200, {"data": {"offers":
                                            [RAW_OFFER]}})],
    })
    offers = _run(qa.search_offers("LOS", "ABV", "2026-10-01"))
    assert offers[0]["price"] == 87500.0
    assert offers[0]["booking_token"] == "qtk_1"
    # contract parity: same keys as a travels247 offer
    assert set(offers[0]) == {"flight_no", "airline", "airline_name",
                              "departure_code", "arrival_code",
                              "departure_time", "arrival_time", "duration",
                              "baggage", "cabin", "price", "currency",
                              "booking_token"}
    _run(qa.close())


def test_quickair_verify_and_reserve_shapes():
    qa = _client_with({
        ("POST", "/api/login"): [(200, LOGIN)],
        ("POST", "/api/flights/pricing"): [(200, {"data": {
            "verified": True, "price_changed": False,
            "verified_price": 87500.0, "currency": "NGN",
            "booking_token": "qtk_1b"}})],
        ("POST", "/api/flights/reserve"): [(200, {"data": {
            "pnr": "QKX7TT", "status": "CONFIRMED"}})],
    })
    v = _run(qa.verify_price("qtk_1"))
    assert v["verified_price"] == 87500.0 and v["booking_token"] == "qtk_1b"
    r = _run(qa.reserve("qtk_1b", travellers={"pax": 1}))
    assert r["pnr"] == "QKX7TT" and r["booking_reference"] == "QKX7TT"
    _run(qa.close())


def test_quickair_search_unusable_shape_raises():
    qa = _client_with({
        ("POST", "/api/login"): [(200, LOGIN)],
        ("POST", "/api/flights/search"): [(200, {"unexpected": 1})],
    })
    from FareBeep.quickair import QuickAirError
    try:
        _run(qa.search_offers("LOS", "ABV", "2026-10-01"))
        assert False, "should have raised"
    except QuickAirError:
        pass
    _run(qa.close())
