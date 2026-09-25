"""E2E SMOKE - the whole WhatsApp beep pipeline against MOCKED Meta APIs.

One user journey, wired end-to-end through the real FastAPI app (no layer
skipped), with Meta's Cloud API as the only boundary replaced:

  1. "Set a beep" card tap  -> /webhook/meta   (interactive button_reply)
  2. Flow offered           -> MetaWhatsapp().send_flow      [MOCKED]
  3. Meta runs the screens  -> /flow COMPLETE  (the real endpoint:
                               validation + watch creation)
  4. Chat confirmation turn -> /webhook/meta nfm_reply (the crash-safe
                               fallback; idempotent with step 3)
  5. Fare drops in the ledger -> worker.run_fare_cycle (the REAL worker
                               loop, session swapped to the test DB)
  6. Beep delivered         -> notifier.send_text            [MOCKED]

Why: the unit suites pin each stage in isolation (test_flows, test_alerts,
test_fare_freshness), and Meta's validator rejects shapes only Business
Manager can see - the failures this file catches are the WIRING ones: a
tap id renamed, a token format changed, the worker reading the wrong
session factory, the freshness gate silently holding every alert.

Hermeticity notes:
  - MetaWhatsapp is monkeypatched everywhere the dispatch path builds it.
  - worker.run_fare_cycle resolves FareBeep.worker.SessionLocal at call
    time, so patching that name keeps the REAL Supabase engine untouched.
  - The live re-check behind force_refresh is reached through
    FareBeep.search.SupplierLiveEngine; patching that class swaps the
    network client for a scripted fake (LedgerSearch instantiates it as
    its default `live` engine at construction time).
"""
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main, worker
from FareBeep.models import (Base, FareLedger, Subscription, User,
                             utcnow)

PHONE = "+2348010001234"
FUTURE_DATE = "2099-06-01"
TOKEN = f"beep:{PHONE}:{int(time.time())}"


class _MetaSpy:
    """Mocked Meta Cloud API: records every outbound WhatsApp send."""

    flows, texts = [], []

    @classmethod
    def reset(cls):
        cls.flows, cls.texts = [], []

    def send_flow(self, to, flow_cta, flow_token, **kw):
        type(self).flows.append({"to": to, "cta": flow_cta,
                                 "token": flow_token, **kw})
        return True

    def send_typing_indicator(self, to):
        return True

    def send_text(self, to, body):
        type(self).texts.append({"to": to, "body": body})
        return True


class _FakeLive:
    """Scripted live-supplier stand-in: what the LIVE re-check
    (force_refresh) returns for the route. `price=None` = provider has
    nothing."""

    def __init__(self):
        self.price = None
        self.calls = []

    def fetch(self, origin, destination, flight_date):
        self.calls.append((origin, destination, flight_date))
        if self.price is None:
            return None
        return {"price": self.price, "currency": "NGN",
                "airline": "Air Peace", "flight_date": flight_date,
                "verify_link": "https://example.com/fare",
                "source": "live", "above_guardrail": False}


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch, session_factory):
    _MetaSpy.reset()
    monkeypatch.setattr(main, "MESSAGING_PROVIDER", "meta")  # Meta-channel E2E
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "MetaWhatsapp", _MetaSpy)
    monkeypatch.setattr(main, "notifier", _MetaSpy())
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", None)
    return TestClient(main.app)


@pytest.fixture
def fake_live(monkeypatch):
    live = _FakeLive()
    monkeypatch.setattr("FareBeep.search.SupplierLiveEngine",
                        lambda: live)
    return live


def _sign(body: bytes) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


def _post_webhook(client, payload: dict):
    body = json.dumps(payload).encode()
    return client.post("/webhook/meta", content=body,
                       headers={"X-Hub-Signature-256": _sign(body)})


def _tap_set_beep(mid):
    """The third button on a fare card (cards.card_buttons -> 'set_beep')."""
    return {"entry": [{"changes": [{"value": {"messages": [{
        "id": mid, "from": PHONE, "timestamp": "1", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {
            "id": "set_beep", "title": "Set a beep"}}}]}}]}]}


def _nfm_reply(token):
    """Meta's flow-completion webhook turn (the chat-side fallback)."""
    return {"entry": [{"changes": [{"value": {"messages": [{
        "id": f"nfm-{uuid.uuid4()}", "from": PHONE, "timestamp": "1",
        "type": "interactive",
        "interactive": {"type": "nfm_reply", "nfm_reply": {
            "flow_token": token,
            "response_json": json.dumps(
                {"origin": "LOS", "destination": "ABV",
                 "departure_date": FUTURE_DATE,
                 "target_price": "85000"})}}}]}}]}]}


def _seed_ledger(session_factory, price):
    db = session_factory()
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date=FUTURE_DATE, price=price,
                      currency="NGN", airline="Air Peace",
                      verify_link="https://example.com/fare",
                      last_updated=utcnow()))
    db.commit()
    db.close()


def _the_subscription(session_factory) -> Subscription:
    db = session_factory()
    try:
        return db.query(Subscription).one()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------
def test_tap_to_watch_to_beep_end_to_end(client, session_factory,
                                         monkeypatch, fake_live):
    # -- 1. user taps "Set a beep" on a fare card ----------------------
    r = _post_webhook(client, _tap_set_beep(f"tap-{uuid.uuid4()}"))
    assert r.status_code == 200

    # -- 2. the bot offers the Flow with a minted, phone-bound token ---
    assert len(_MetaSpy.flows) == 1
    offer = _MetaSpy.flows[0]
    assert offer["to"] == PHONE
    assert offer["cta"] == "Set my beep"
    assert offer["token"].startswith(f"beep:{PHONE}:")

    # -- 3. Meta runs the screens; COMPLETE lands on the endpoint ------
    r = client.post("/flow", json={
        "version": "3.0", "action": "COMPLETE",
        "screen": "SET_BEEP_REVIEW", "flow_token": offer["token"],
        "origin": "LOS", "destination": "ABV",
        "departure_date": FUTURE_DATE, "target_price": "85000"})
    assert r.status_code == 200
    sub = _the_subscription(session_factory)
    assert (sub.origin, sub.destination) == ("LOS", "ABV")
    assert sub.target_price == 85000.0
    assert sub.last_price is None            # no baseline yet - cycle 1 sets it

    # -- 4. the chat-side confirmation ALSO fires (production does both;
    #       creation is idempotent per (user, route)) -------------------
    r = _post_webhook(client, _nfm_reply(offer["token"]))
    assert r.status_code == 200
    assert _MetaSpy.texts, "confirmation turn must answer in chat"
    assert any("Your beep is on" in t["body"] for t in _MetaSpy.texts)
    db = session_factory()
    try:
        assert db.query(Subscription).count() == 1
        assert db.query(User).filter_by(phone=PHONE).count() == 1
    finally:
        db.close()

    # -- 5. the fare drops: worker cycles against the ledger -----------
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    _seed_ledger(session_factory, price=100000.0)

    # cycle 1: first sighting only sets the baseline (never beeps)
    assert worker.run_fare_cycle(notifier=_MetaSpy()) == 0
    sub = _the_subscription(session_factory)
    assert sub.last_price == 100000.0

    # the ledger price falls below the user's target...
    db = session_factory()
    db.query(FareLedger).filter_by(origin="LOS", destination="ABV",
                                   flight_date=FUTURE_DATE) \
        .update({"price": 80000.0})
    db.commit()
    db.close()

    # cycle 2: the honesty gate re-checks LIVE (fake supplier) before
    # beeping - the alert carries the fresh price, not the cached one.
    fake_live.price = 80000.0
    assert worker.run_fare_cycle(notifier=_MetaSpy()) == 1
    beeps = [t for t in _MetaSpy.texts if "FARE BEEP" in t["body"]]
    assert len(beeps) == 1
    assert beeps[0]["to"] == PHONE
    assert "LOS to ABV" in beeps[0]["body"]
    assert "80,000" in beeps[0]["body"]       # the fresh price
    assert "85000" in beeps[0]["body"].replace(",", "")  # the target hit
    sub = _the_subscription(session_factory)
    assert sub.last_alerted_price == 80000.0
    assert fake_live.calls, "live re-check must have run pre-alert"

    # cycle 3: same price again -> dedupe holds, no second beep
    assert worker.run_fare_cycle(notifier=_MetaSpy()) == 0
    assert len([t for t in _MetaSpy.texts
                if "FARE BEEP" in t["body"]]) == 1


def test_beep_held_when_live_price_bounces_back(client, session_factory,
                                                monkeypatch, fake_live):
    """The freshness contract, end-to-end: a cached drop that the LIVE
    re-check disproves must NOT alert - and the fresh number becomes the
    new baseline so the next cycle judges against reality."""
    r = client.post("/flow", json={
        "version": "3.0", "action": "COMPLETE",
        "screen": "SET_BEEP_REVIEW", "flow_token": TOKEN,
        "origin": "LOS", "destination": "ABV",
        "departure_date": FUTURE_DATE, "target_price": "85000"})
    assert r.status_code == 200

    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    _seed_ledger(session_factory, price=100000.0)
    assert worker.run_fare_cycle(notifier=_MetaSpy()) == 0   # baseline

    # cached ledger says 80000 (a hit!) but the live provider disagrees:
    db = session_factory()
    db.query(FareLedger).filter_by(origin="LOS", destination="ABV",
                                   flight_date=FUTURE_DATE) \
        .update({"price": 80000.0})
    db.commit()
    db.close()
    fake_live.price = 95000.0                # bounced back up in reality

    assert worker.run_fare_cycle(notifier=_MetaSpy()) == 0
    assert not [t for t in _MetaSpy.texts if "FARE BEEP" in t["body"]], \
        "no beep on a price the live re-check disproved"
    sub = _the_subscription(session_factory)
    assert sub.last_price == 95000.0         # baseline floated to the truth
    assert sub.last_alerted_price is None
