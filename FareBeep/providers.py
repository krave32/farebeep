"""THE DEFENSIVE INTEGRATION LAYER - resilient HTTP + churn-proof contracts.

Everything that talks to a third-party API (SerpApi today, Tiqwa next) sits
behind this module. Its job is to make vendor churn LOUD instead of silent:

  1. RetryClient      - timeouts, exponential backoff + jitter, retry on
                        429/5xx/connect errors, request-id logging.
  2. Contract/Parser  - typed extraction that never raises: a missing or
                        RENAMED field falls back to a default and is REPORTED
                        as drift, so a field rename surfaces in /health long
                        before users see a crash.
  3. parse_fare       - normalize any vendor's fare JSON into the single
                        ledger dict shape the rest of FareBeep consumes.
  4. probe_contract   - run a live request + validate against a pinned field
                        map; the report is what /health should check.
  5. get_live_engine  - FARE_PROVIDER switch (serpapi now, tiqwa later) with
                        a FailoverEngine so one vendor never takes us down.
"""
import logging
import random
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import httpx

from FareBeep.config import (HTTP_MAX_RETRIES, HTTP_TIMEOUT,
                             FARE_PROVIDER, TIQWA_API_KEY, TIQWA_BASE_URL,
                             TIQWA_ENV)

logger = logging.getLogger("farebeep.providers")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ProviderError(Exception):
    """A vendor call failed after retries, or the payload was unusable."""


# ---------------------------------------------------------------------------
# 1. RETRYCLIENT - one resilient HTTP client for every vendor
# ---------------------------------------------------------------------------
class RetryClient:
    """httpx wrapper with retries + backoff + jitter + request-id logging.

    Retries on connect errors, timeouts, 429 (honouring Retry-After) and
    5xx. Never retries 4xx except 408/429. Owns a single httpx.Client that
    callers should `.close()` (or pass their own via `http_client`).
    """

    def __init__(self, timeout: float = None, max_retries: int = None,
                 http_client: httpx.Client = None):
        self.timeout = timeout or HTTP_TIMEOUT
        self.max_retries = max_retries or HTTP_MAX_RETRIES
        self._http = http_client or httpx.Client(timeout=self.timeout,
                                                 headers={"User-Agent": "FareBeep/1.0"})
        self._owns = http_client is None

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def get_json(self, url: str, **kw) -> dict:
        return self._request("GET", url, **kw)

    def post_json(self, url: str, json: dict = None, **kw) -> dict:
        return self._request("POST", url, json=json, **kw)

    def _request(self, method: str, url: str, **kw) -> dict:
        request_id = uuid.uuid4().hex[:8]
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, url, **kw)
                if resp.status_code == 200:
                    logger.debug("%s %s ok (%s)", method, url, request_id)
                    return resp.json()
                if resp.status_code not in RETRYABLE_STATUS or attempt > self.max_retries:
                    raise ProviderError(
                        f"{method} {url} -> HTTP {resp.status_code}: "
                        f"{resp.text[:200]} (rid={request_id})")
                delay = self._backoff(resp, attempt)
            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError) as e:
                if attempt > self.max_retries:
                    raise ProviderError(
                        f"{method} {url} failed after {attempt} attempts: "
                        f"{e} (rid={request_id})") from e
                delay = min(0.5 * (2 ** (attempt - 1)), 8.0) + random.uniform(0, 0.25)
            except ProviderError:
                raise
            logger.warning("%s %s retry %d/%d after %.2fs (rid=%s)",
                           method, url, attempt, self.max_retries, delay, request_id)
            time.sleep(delay)

    @staticmethod
    def _backoff(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(float(retry_after), 10.0)
            except ValueError:
                pass
        return min(0.5 * (2 ** (attempt - 1)), 8.0) + random.uniform(0, 0.25)


# ---------------------------------------------------------------------------
# 2. CONTRACT / PARSER - typed extraction that reports drift instead of dying
# ---------------------------------------------------------------------------
Coerce = Callable[[Any], Any]


def _as_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("NGN", "").strip())
    except ValueError:
        return None


def _as_int(v: Any) -> Optional[int]:
    f = _as_float(v)
    return int(f) if f is not None else None


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "y")
    return bool(v) if v is not None else None


class Parser:
    """Extract typed values from a vendor dict via dotted paths.

    A path like "offers.0.price" navigates nested dicts and lists. Missing
    or un-coercible values NEVER raise: they fall back to the default and
    are recorded in `.missing` / `.coerced` for the drift report.
    """

    def __init__(self):
        self.missing: List[str] = []
        self.coerced: List[str] = []

    @staticmethod
    def _walk(data: Any, path: str) -> Any:
        node = data
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit():
                i = int(part)
                node = node[i] if i < len(node) else None
            else:
                return None
            if node is None:
                return None
        return node

    def take(self, data: Any, path: str, coerce: Coerce = _as_str,
             default: Any = None) -> Any:
        raw = self._walk(data, path)
        if raw is None or raw == "":
            self.missing.append(path)
            return default
        value = coerce(raw)
        if value is None:
            self.coerced.append(f"{path}({raw!r})")
            return default
        return value

    def report(self) -> dict:
        return {"missing": self.missing, "coerced": self.coerced}


class Contract:
    """A pinned field map for one vendor response + its version.

    version is the API version this contract was written against. When a
    field shows up in `.missing`, bump the probe and treat it as drift -
    the signal that the vendor changed the payload.
    """

    def __init__(self, name: str, version: str, spec: Dict[str, dict]):
        self.name = name
        self.version = version
        self.spec = spec  # {"field": {"path": ..., "coerce": ..., "default": ...}}

    def parse(self, data: Any) -> tuple:
        parser = Parser()
        result = {}
        for field, opts in self.spec.items():
            result[field] = parser.take(
                data, opts["path"], opts.get("coerce", _as_str),
                opts.get("default"))
        return result, parser.report()


# ---------------------------------------------------------------------------
# 3. PARSE_FARE - normalize any vendor fare into the ledger dict shape
# ---------------------------------------------------------------------------
def parse_fare(data: Any) -> tuple:
    """Map a vendor fare JSON onto {price, currency, airline, flight_id}.

    Returns (result, drift_report). price is NGN float; anything else falls
    back to None so the caller can treat it as "no usable fare" instead of
    poisoning the ledger.
    """
    contract = Contract(
        "fare", "v1",
        {
            "price": {"path": "price", "coerce": _as_float, "default": None},
            "currency": {"path": "currency", "coerce": _as_str, "default": "NGN"},
            "airline": {"path": "airline", "coerce": _as_str, "default": "Unknown"},
            "flight_id": {"path": "flight_id", "coerce": _as_str, "default": None},
            "flight_number": {"path": "flight_number", "coerce": _as_str, "default": None},
            "departs_at": {"path": "departs_at", "coerce": _as_str, "default": None},
        },
    )
    result, drift = contract.parse(data)
    if result["price"] is None:
        result["flight_id"] = None
    return result, drift


# ---------------------------------------------------------------------------
# 4. PROBE_CONTRACT - live drift check for /health
# ---------------------------------------------------------------------------
def probe_contract(contract: Contract, request: Callable[[], Any]) -> dict:
    """Run `request()` and validate the response against `contract`.

    Returns {"contract", "version", "ok", "missing", "coerced"}.
    `ok` is False when required fields vanish OR the call itself fails -
    this is the early-warning signal for vendor churn.
    """
    try:
        data = request()
    except ProviderError as e:
        return {"contract": contract.name, "version": contract.version,
                "ok": False, "missing": [], "coerced": [], "error": str(e)}
    _, drift = contract.parse(data)
    required_missing = [f for f in drift["missing"]
                        if contract.spec.get(f, {}).get("required")]
    return {"contract": contract.name, "version": contract.version,
            "ok": not required_missing,
            "missing": drift["missing"], "coerced": drift["coerced"]}


# ---------------------------------------------------------------------------
# 5. PROVIDER FACTORY + FAILOVER - one vendor must never take us down
# ---------------------------------------------------------------------------
class FailoverEngine:
    """Try `primary.fetch(...)`; on any failure fall back to `secondary`.

    A failed primary is logged loudly (that's the drift/outage signal), and
    the caller learns which source served the fare via `source`.
    """

    def __init__(self, primary: Any, secondary: Any):
        self.primary = primary
        self.secondary = secondary

    def fetch(self, origin: str, destination: str, flight_date: str) -> Optional[dict]:
        try:
            result = self.primary.fetch(origin, destination, flight_date)
            if result is not None:
                result.setdefault("source", "primary")
                return result
        except Exception as e:
            logger.warning("Primary engine failed (%s) - using fallback", e)
        result = self.secondary.fetch(origin, destination, flight_date)
        if result is not None:
            result.setdefault("source", "fallback")
        return result


def get_live_engine():
    """Build the live fare engine configured by FARE_PROVIDER.

    "serpapi" (default today) = SerpApiGoogleFlights (the pitch-deck demo
    source). "tiqwa" = the production consolidator engine - arrives in
    FareBeep/tiqwa.py once the API token + contract are available; it must
    implement the same fetch(origin, destination, flight_date) contract.
    """
    from FareBeep.search import SerpApiGoogleFlights

    provider = (FARE_PROVIDER or "serpapi").lower()
    if provider == "tiqwa":
        try:
            from FareBeep.tiqwa import TiqwaFlights
            return TiqwaFlights()
        except ImportError:  # pragma: no cover - Tiqwa client not shipped yet
            logger.warning("FARE_PROVIDER=tiqwa but FareBeep/tiqwa.py missing - "
                           "falling back to SerpApi")
            return SerpApiGoogleFlights()
    return SerpApiGoogleFlights()


def _tiqwa_ready() -> bool:
    """True when a Tiqwa token + base URL are configured (the client can run)."""
    return bool(TIQWA_API_KEY and TIQWA_BASE_URL)


def tiqwa_probe() -> Optional[dict]:
    """/health drift probe for the Tiqwa source. Returns None (skip) until
    credentials AND the client exist - the probe goes live with the real
    integration (FareBeep/tiqwa.py implements a search call to probe)."""
    if not _tiqwa_ready():
        return None
    from FareBeep.tiqwa import TiqwaFlights  # noqa: F401
    return {"contract": "tiqwa_flight", "version": "v1",
            "ok": False, "error": "tiqwa probe: implement a live search call"}


__all__ = [
    "ProviderError", "RetryClient", "Parser", "Contract",
    "parse_fare", "probe_contract", "FailoverEngine", "get_live_engine",
    "tiqwa_probe", "_as_float", "_as_int", "_as_str", "_as_bool",
]
