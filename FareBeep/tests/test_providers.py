"""THE DEFENSIVE INTEGRATION LAYER - retry/backoff + churn-proof contracts."""
import httpx
import pytest

from FareBeep import providers
from FareBeep.providers import (Contract, FailoverEngine, Parser, ProviderError,
                                RetryClient, parse_fare, probe_contract)


# ---------------------------------------------------------------------------
# PARSER - missing / renamed / mistyped fields never crash, they report
# ---------------------------------------------------------------------------
def test_parser_extracts_nested_typed_values():
    data = {"offers": [{"price": "42000", "airline": "Air Peace"}]}
    p = Parser()
    price = p.take(data, "offers.0.price", providers._as_float)
    airline = p.take(data, "offers.0.airline", providers._as_str)
    assert price == 42000.0
    assert airline == "Air Peace"
    assert p.report() == {"missing": [], "coerced": []}


def test_parser_renamed_field_falls_back_and_reports_missing():
    """The vendor renames 'price' -> 'total_price': we get the default, and
    the drift report flags it - the /health probe's early warning."""
    data = {"offers": [{"total_price": "42000"}]}
    p = Parser()
    assert p.take(data, "offers.0.price", providers._as_float, default=None) is None
    assert "offers.0.price" in p.missing


def test_parser_wrong_type_never_raises():
    p = Parser()
    assert p.take({"price": {"nested": "dict"}}, "price", providers._as_float, 0.0) == 0.0
    assert p.take({"price": None}, "price", providers._as_float, 0.0) == 0.0
    assert p.take({"price": "abc"}, "price", providers._as_float, 0.0) == 0.0
    assert p.take({}, "offers.3.price", providers._as_float, None) is None


def test_parser_list_bounds_and_extra_fields_are_safe():
    p = Parser()
    assert p.take({"list": [1]}, "list.5", providers._as_int, -1) == -1
    # unknown extra keys in the payload are simply ignored
    assert p.take({"unexpected": {"x": 1}}, "unexpected.x", providers._as_int) == 1


# ---------------------------------------------------------------------------
# CONTRACT - pinned field map with a version tag
# ---------------------------------------------------------------------------
def test_contract_parse_returns_result_and_drift():
    contract = Contract(
        "fare", "v1",
        {"price": {"path": "price", "coerce": providers._as_float, "default": None},
         "airline": {"path": "airline", "coerce": providers._as_str, "default": "Unknown"}})
    result, drift = contract.parse({"price": 80000, "airline": "Air Peace"})
    assert result["price"] == 80000.0
    assert drift == {"missing": [], "coerced": []}


def test_contract_required_field_drift_flips_probe_ok():
    contract = Contract(
        "fare", "v1",
        {"price": {"path": "price", "coerce": providers._as_float,
                   "default": None, "required": True}})
    ok = probe_contract(contract, lambda: {"total_price": 50000})
    assert ok["ok"] is False
    assert "price" in ok["missing"]


def test_probe_ok_when_contract_matches():
    contract = Contract(
        "fare", "v1",
        {"price": {"path": "price", "coerce": providers._as_float,
                   "default": None, "required": True}})
    ok = probe_contract(contract, lambda: {"price": 50000})
    assert ok["ok"] is True


def test_probe_flags_required_drift_on_nested_path():
    """Regression: a required field at a NESTED path must still trip the
    probe (the old lookup compared field names against paths and silently
    missed nested drift)."""
    contract = Contract(
        "fare", "v1",
        {"price": {"path": "data.offers.0.price", "coerce": providers._as_float,
                   "default": None, "required": True}})
    ok = probe_contract(contract, lambda: {"data": {"offers": [{"total_price": 9}]}})
    assert ok["ok"] is False
    assert "data.offers.0.price" in ok["missing"]


def test_probe_reports_call_failure():
    contract = Contract("fare", "v1", {})
    ok = probe_contract(contract, lambda: (_ for _ in ()).throw(
        ProviderError("down")))
    assert ok["ok"] is False
    assert "down" in ok["error"]


# ---------------------------------------------------------------------------
# PARSE_FARE - normalize vendor fares into the ledger shape
# ---------------------------------------------------------------------------
def test_parse_fare_maps_ngn_price_and_ids():
    result, drift = parse_fare(
        {"price": "42,000", "currency": "NGN", "airline": "Air Peace",
         "flight_id": "TK-20260820-LOS-ABV-01", "flight_number": "P47123"})
    assert result["price"] == 42000.0
    assert result["currency"] == "NGN"
    assert result["flight_id"] == "TK-20260820-LOS-ABV-01"
    assert "price" not in drift["missing"]
    assert "flight_id" not in drift["missing"]


def test_parse_fare_missing_price_yields_no_flight_id():
    """No price = no usable fare; the caller must NOT book/quote it."""
    result, drift = parse_fare({"total_price": "X"})
    assert result["price"] is None
    assert result["flight_id"] is None


# ---------------------------------------------------------------------------
# RETRYCLIENT - retries on 429/5xx/connect, honours Retry-After
# ---------------------------------------------------------------------------
class _Sequence:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body, request=request)


def _client_with(handler) -> RetryClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport)
    return RetryClient(max_retries=3, http_client=inner)


def test_retry_succeeds_after_429_with_retry_after():
    seq = _Sequence([(429, {"x": 1}), (200, {"ok": True})])
    client = _client_with(seq)
    assert client.get_json("https://vendor.test/") == {"ok": True}
    assert seq.calls == 2
    client.close()


def test_retry_gives_up_after_max_attempts():
    seq = _Sequence([(500, {}), (500, {}), (500, {}), (500, {})])
    client = _client_with(seq)
    with pytest.raises(ProviderError):
        client.get_json("https://vendor.test/")
    assert seq.calls == 4
    client.close()


def test_client_never_retries_a_plain_400():
    seq = _Sequence([(400, {"error": "bad request"}), (200, {})])
    client = _client_with(seq)
    with pytest.raises(ProviderError):
        client.get_json("https://vendor.test/")
    assert seq.calls == 1  # 4xx (non-408/429) is fatal immediately
    client.close()


def test_retry_handles_connect_failure_then_success():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, **kw):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"ok": True})

    client = RetryClient(max_retries=3, http_client=_Flaky())
    assert client.get_json("https://vendor.test/") == {"ok": True}
    client.close()


# ---------------------------------------------------------------------------
# FAILOVER ENGINE - a broken primary must not take us down
# ---------------------------------------------------------------------------
def test_failover_uses_secondary_when_primary_throws():
    class _Primary:
        def fetch(self, o, d, dt):
            raise ProviderError("primary dead")

    class _Secondary:
        def fetch(self, o, d, dt):
            return {"price": 100.0, "currency": "NGN"}

    engine = FailoverEngine(_Primary(), _Secondary())
    result = engine.fetch("LOS", "ABV", "2026-08-20")
    assert result["price"] == 100.0
    assert result["source"] == "fallback"


def test_failover_prefers_primary_when_healthy():
    class _Primary:
        def fetch(self, o, d, dt):
            return {"price": 200.0, "currency": "NGN"}

    class _Secondary:
        def fetch(self, o, d, dt):
            return {"price": 100.0, "currency": "NGN"}

    engine = FailoverEngine(_Primary(), _Secondary())
    result = engine.fetch("LOS", "ABV", "2026-08-20")
    assert result["price"] == 200.0
    assert result["source"] == "primary"


# ---------------------------------------------------------------------------
# FACTORY - provider switch
# ---------------------------------------------------------------------------
def test_factory_defaults_to_serpapi(monkeypatch):
    monkeypatch.setattr(providers, "FARE_PROVIDER", "serpapi")
    engine = providers.get_live_engine()
    assert engine.__class__.__name__ == "SerpApiGoogleFlights"


def test_factory_tiqwa_falls_back_without_client(monkeypatch):
    monkeypatch.setattr(providers, "FARE_PROVIDER", "tiqwa")
    engine = providers.get_live_engine()
    assert engine.__class__.__name__ == "SerpApiGoogleFlights"


def test_tiqwa_probe_skipped_without_credentials(monkeypatch):
    monkeypatch.setattr(providers, "TIQWA_API_KEY", None)
    monkeypatch.setattr(providers, "TIQWA_BASE_URL", None)
    assert providers.tiqwa_probe() is None
