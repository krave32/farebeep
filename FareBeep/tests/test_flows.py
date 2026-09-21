"""WHATSAPP FLOWS - the structured "Set a beep" screens.

Answers, with tests:
  Q1 the /flow endpoint answers ping, serves screen data, and validates.
  Q2 COMPLETE creates a watch through the same idempotent TRACK service
     (a second completion refreshes, never duplicates).
  Q3 invalid completions (past date, same airports) bounce with an error.
  Q4 Meta's encryption scheme round-trips (AES-256-GCM + RSA-OAEP).
  Q5 the chat-side fallback: an nfm_reply webhook turn creates the watch
     and confirms in chat.
  Q6 the "Set a beep" card tap offers the Flow, and degrades to the
     guided TRACK path without BEEP_FLOW_ID.
"""
import base64
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep import main
from FareBeep.models import Base, Subscription, User

FUTURE_DATE = "2099-06-01"


def _sign(body: bytes) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(b"test-app-secret", body,
                                hashlib.sha256).hexdigest()


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
    monkeypatch.setattr(main, "META_APP_SECRET", "test-app-secret")
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main.brain, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", None)
    # hermetic default: plaintext /flow mode (dev), even though the local
    # .env may carry a real FLOW_PRIVATE_KEY - encrypted tests opt in
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", None)
    # hermetic typing bubble: the dispatch path builds MetaWhatsapp()
    # directly, which would hit the real Graph API (token in .env)
    monkeypatch.setattr(main, "MetaWhatsapp", type("W", (), {
        "send_typing_indicator": staticmethod(lambda to: True)}))
    return TestClient(main.app)


def _post_flow(client, payload):
    return client.post("/flow", json=payload)


# -- Q1: ping + screen data -------------------------------------------------
def test_ping_answers_active(client):
    r = _post_flow(client, {"version": "3.0", "action": "ping"})
    assert r.status_code == 200
    assert r.json() == {"data": {"status": "active"}}


def test_init_picks_first_screen(client):
    # Meta sends INIT with an empty screen - the endpoint must answer
    # with the first screen (never empty: Meta validates against the
    # routing model).
    r = _post_flow(client, {"version": "3.0", "action": "INIT", "screen": ""})
    body = r.json()
    assert body["screen"] == "SET_BEEP_TRIP"
    assert body["data"]["min_date"] and body["data"]["max_date"]


def test_init_date_bounds_span_the_booking_window(client):
    from FareBeep.dates import lagos_today
    body = _post_flow(client, {"version": "3.0", "action": "INIT",
                               "screen": ""}).json()
    assert body["data"]["min_date"] == lagos_today().isoformat()
    assert body["data"]["max_date"] > body["data"]["min_date"]


def test_back_walks_up_the_stack(client):
    r = _post_flow(client, {"version": "3.0", "action": "BACK",
                            "screen": "SET_BEEP_REVIEW"})
    body = r.json()
    assert body["screen"] == "SET_BEEP_DATES"
    assert body["data"]["min_date"]          # bounds re-served for the picker
    r = _post_flow(client, {"version": "3.0", "action": "BACK", "screen": ""})
    assert r.json()["screen"] == "SET_BEEP_TRIP"


# -- Q1b: the dates -> review data_exchange step ---------------------------
def test_data_exchange_serves_ledger_fare(client, session_factory):
    from FareBeep.models import FareLedger
    db = session_factory()
    db.add(FareLedger(origin="LOS", destination="ABV",
                      flight_date=FUTURE_DATE, price=98000.0,
                      airline="Air Peace"))
    db.commit()
    db.close()
    r = _post_flow(client, {"version": "3.0", "action": "data_exchange",
                            "screen": "SET_BEEP_DATES",
                            "origin": "LOS", "destination": "ABV",
                            "departure_date": FUTURE_DATE})
    body = r.json()
    assert body["screen"] == "SET_BEEP_REVIEW"
    assert "98,000" in body["data"]["fare_label"]
    assert "Air Peace" in body["data"]["fare_label"]


def test_data_exchange_without_ledger_says_any_drop(client):
    r = _post_flow(client, {"version": "3.0", "action": "data_exchange",
                            "screen": "SET_BEEP_DATES",
                            "origin": "LOS", "destination": "KAN",
                            "departure_date": FUTURE_DATE})
    body = r.json()
    assert body["screen"] == "SET_BEEP_REVIEW"
    assert "ANY drop" in body["data"]["fare_label"]


def test_route_screen_exchange_serves_date_bounds(client):
    """The trip screen's footer is an endpoint step too (Meta forbids a
    static navigate into a screen with a non-empty data model) - it must
    answer the DATE screen with live bounds, not the review screen."""
    from FareBeep.dates import lagos_today
    r = _post_flow(client, {"version": "3.0", "action": "data_exchange",
                            "screen": "SET_BEEP_TRIP",
                            "origin": "LOS", "destination": "ABV"})
    body = r.json()
    assert body["screen"] == "SET_BEEP_DATES"
    assert body["data"]["min_date"] == lagos_today().isoformat()
    assert body["data"]["max_date"] > body["data"]["min_date"]
    assert "fare_label" not in body["data"]   # review data not served yet


# -- Q2: completion creates the watch (idempotent) --------------------------
def _complete_payload(token="beep:+2348010000001:123",
                      origin="LOS", destination="ABV", **kw):
    return {"version": "3.0", "action": "COMPLETE",
            "screen": "SET_BEEP_REVIEW", "flow_token": token,
            "origin": origin, "destination": destination,
            "departure_date": FUTURE_DATE, **kw}


def test_complete_creates_watch(client, session_factory):
    r = _post_flow(client, _complete_payload(target_price="85000"))
    assert r.status_code == 200
    assert r.json()["screen"] == "SUCCESS"
    db = session_factory()
    user = db.query(User).filter_by(phone="+2348010000001").first()
    assert user is not None
    subs = db.query(Subscription).filter_by(user_id=user.user_id).all()
    assert len(subs) == 1
    sub = subs[0]
    assert sub.origin == "LOS" and sub.destination == "ABV"
    assert sub.target_price == 85000.0
    assert str(sub.target_date)[:10] == FUTURE_DATE
    db.close()


def test_complete_is_idempotent_refresh_not_duplicate(client,
                                                      session_factory):
    _post_flow(client, _complete_payload(target_price="85000"))
    _post_flow(client, _complete_payload(target_price="70000"))
    db = session_factory()
    subs = db.query(Subscription).all()
    assert len(subs) == 1
    assert subs[0].target_price == 70000.0
    db.close()


def test_complete_without_minted_token_still_succeeds(client,
                                                      session_factory):
    # foreign token (not minted by us): endpoint acks, creates nothing -
    # the webhook turn owns creation for those
    r = _post_flow(client, _complete_payload(token="foreign-token"))
    assert r.json()["screen"] == "SUCCESS"
    db = session_factory()
    assert db.query(Subscription).count() == 0
    db.close()


# -- Q3: invalid completions bounce to the screen owning the field --------
def test_complete_past_date_bounces(client, session_factory):
    r = _post_flow(client, _complete_payload(departure_date="2001-01-01"))
    body = r.json()
    assert body["screen"] == "SET_BEEP_DATES"   # date error -> date screen
    assert "past" in body["error"].lower()
    db = session_factory()
    assert db.query(Subscription).count() == 0
    db.close()


def test_complete_same_airports_bounce(client):
    r = _post_flow(client, _complete_payload(destination="LOS"))
    body = r.json()
    assert body["screen"] == "SET_BEEP_TRIP"    # route error -> route screen
    assert "differ" in body["error"].lower()


def test_screens_json_dropdowns_match_iata_map():
    """The airport dropdowns are a build artifact of iata._AIRPORT_MAP
    (build_flow_screens.py) - drift between them is a regression."""
    import json
    from pathlib import Path
    from FareBeep.iata import _AIRPORT_MAP
    screens = json.loads((Path(__file__).resolve().parents[1]
                          / "whatsapp" / "flow_screens.json").read_text())
    trip = screens["screens"][0]["layout"]["children"][0]["children"]
    ids = [c["data-source"] for c in trip if c["type"] == "Dropdown"]
    assert len(ids) == 2
    expected = []
    for _names, code in _AIRPORT_MAP:
        if code not in expected:
            expected.append(code)
    for source in ids:
        assert [opt["id"] for opt in source] == expected


def _walk(node, found: list):
    """Collect every dict in a layout subtree."""
    if isinstance(node, dict):
        found.append(node)
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def _screens_json():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[1]
                       / "whatsapp" / "flow_screens.json").read_text())


def test_screens_json_satisfies_meta_validator_rules():
    """Meta's draft-flow validator rejected four separate shapes while this
    flow was tuned. Encoding its rules here means build_flow_screens.py can't
    regress them (the upload only fails in Business Manager, silently, later).

    Rules learned from the validator, in order it complained:
      1. DatePicker bounds are `min-date`/`max-date`, never `min`/`max`.
      2. Dynamic props bind as "${data.key}" STRINGS (not {"path": ...}
         objects) outside of an on-click-action payload.
      3. Every key a screen interpolates must be declared in that screen's
         own `data` model, with `type` AND `__example__`.
      4. routing_model is FORWARD-ONLY: exactly one entry screen (no inbound
         edge). Error re-routes happen at runtime via the endpoint response.
      5. A `navigate` action may not target a screen with a non-empty data
         model - that hop needs an endpoint step carrying a payload.
    """
    import re
    doc = _screens_json()
    screens = {s["id"]: s for s in doc["screens"]}

    for sid, s in screens.items():
        nodes: list = []
        _walk(s["layout"], nodes)
        model = s.get("data", {})
        for n in nodes:
            # (1) DatePicker prop names
            if n.get("type") == "DatePicker":
                assert "min" not in n and "max" not in n, \
                    f"{sid}: DatePicker must use min-date/max-date"
            # (5) no static navigate into a screen with a data model
            action = n.get("on-click-action") or {}
            if action.get("name") == "navigate":
                nxt = (action.get("next") or {}).get("name")
                assert not screens[nxt].get("data"), \
                    f"{sid}: navigate into {nxt} needs an endpoint payload"
            # (2)+(3) every interpolated key is declared with an example
            for key, val in n.items():
                if key in ("on-click-action", "payload"):
                    continue
                if not isinstance(val, str):
                    continue
                for ref in re.findall(r"\$\{data\.([A-Za-z0-9_]+)\}", val):
                    assert ref in model, \
                        f"{sid}: '${{data.{ref}}}' missing from data model"
                    assert model[ref].get("type"), f"{sid}.{ref}: no type"
                    assert model[ref].get("__example__"), \
                        f"{sid}.{ref}: no __example__"

    # (4) forward-only routing with a single entry screen
    routing = doc["routing_model"]
    assert set(routing) <= set(screens), "routing_model names an unknown screen"
    targets = [t for edges in routing.values() for t in edges]
    entries = [sid for sid in screens if sid not in targets]
    assert len(entries) == 1, f"expected one entry screen, got {entries}"
    assert entries[0] == doc["screens"][0]["id"]
    for src, edges in routing.items():
        for tgt in edges:
            assert tgt not in routing or src not in routing[tgt], \
                f"backward edge {src} <-> {tgt} is rejected by Meta"


# -- Q4: encryption round-trip ----------------------------------------------
@pytest.fixture
def rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


def _encrypt_for(pub_pem: bytes, payload: dict) -> tuple[bytes, bytes, bytes]:
    """Meta's real wire format: JSON envelope of three base64 fields,
    AES-256-GCM with a 16-byte nonce."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes_key = os.urandom(32)
    iv = os.urandom(16)
    blob = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
    pub = serialization.load_pem_public_key(pub_pem)
    wrapped = pub.encrypt(aes_key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))
    envelope = {
        "encrypted_flow_data": base64.b64encode(blob).decode(),
        "encrypted_aes_key": base64.b64encode(wrapped).decode(),
        "initial_vector": base64.b64encode(iv).decode(),
    }
    return json.dumps(envelope).encode(), aes_key, iv


def _decrypt_response(aes_key: bytes, iv: bytes, body: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    return json.loads(AESGCM(aes_key).decrypt(
        flipped_iv, base64.b64decode(body), None))


def test_encrypted_roundtrip(client, monkeypatch, rsa_keypair):
    priv, pub = rsa_keypair
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", priv)
    body, aes_key, iv = _encrypt_for(pub, _complete_payload())
    r = client.post("/flow", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    payload = _decrypt_response(aes_key, iv, r.content)
    assert payload["screen"] == "SUCCESS"


def test_encrypted_ping_roundtrip(client, monkeypatch, rsa_keypair):
    # Meta's health check: encrypted {"version":"3.0","action":"ping"}
    # must answer the encrypted {"data": {"status": "active"}}
    priv, pub = rsa_keypair
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", priv)
    body, aes_key, iv = _encrypt_for(pub, {"version": "3.0", "action": "ping"})
    r = client.post("/flow", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    payload = _decrypt_response(aes_key, iv, r.content)
    assert payload == {"data": {"status": "active"}}


def test_plaintext_rejected_when_key_set(client, monkeypatch, rsa_keypair):
    priv, _ = rsa_keypair
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", priv)
    r = client.post("/flow", json={"version": "3.0", "action": "ping"})
    assert r.status_code == 400


# -- Q5: the chat-side webhook turn -----------------------------------------
def _nfm_payload(mid, token, data):
    return {"entry": [{"changes": [{"value": {"messages": [{
        "id": mid, "from": "2348010000009", "timestamp": "1",
        "type": "interactive",
        "interactive": {"type": "nfm_reply", "nfm_reply": {
            "flow_token": token,
            "response_json": json.dumps(data)}}}]}}]}]}


def test_webhook_flow_response_creates_watch(client, monkeypatch,
                                             session_factory):
    sent = []
    monkeypatch.setattr(main, "notifier", type("N", (), {
        "send_text": staticmethod(lambda to, body: sent.append(body) or True),
        "send_typing_indicator": staticmethod(lambda to: True)}))
    body = json.dumps(_nfm_payload(
        f"nfm-{uuid.uuid4()}", "beep:2348010000009:77",
        {"origin": "los", "destination": "abv",
         "departure_date": FUTURE_DATE})).encode()
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    db = session_factory()
    user = db.query(User).filter_by(phone="2348010000009").first()
    subs = db.query(Subscription).filter_by(user_id=user.user_id).all()
    assert len(subs) == 1 and subs[0].origin == "LOS"
    db.close()
    assert any("Your beep is on" in s for s in sent)


def test_webhook_flow_response_idempotent_with_endpoint(client,
                                                        monkeypatch,
                                                        session_factory):
    # the /flow endpoint created it first - the chat turn must refresh,
    # not duplicate (both paths fire in production). Note the phone: Meta
    # "from" numbers carry no "+", so the minted token must match.
    _post_flow(client, _complete_payload(token="beep:2348010000009:77"))
    monkeypatch.setattr(main, "notifier", type("N", (), {
        "send_text": staticmethod(lambda to, body: True),
        "send_typing_indicator": staticmethod(lambda to: True)}))
    body = json.dumps(_nfm_payload(
        f"nfm-{uuid.uuid4()}", "beep:2348010000009:77",
        {"origin": "LOS", "destination": "ABV",
         "departure_date": FUTURE_DATE})).encode()
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    db = session_factory()
    assert db.query(Subscription).count() == 1
    db.close()


# -- Q6: the entry tap ------------------------------------------------------
class _FlowSpy:
    calls = []

    @classmethod
    def reset(cls):
        cls.calls = []

    def send_flow(self, to, flow_cta, flow_token, **kw):
        type(self).calls.append((to, flow_cta, flow_token))
        return True


def test_set_beep_tap_offers_flow(client, monkeypatch):
    _FlowSpy.reset()
    monkeypatch.setattr(main, "MetaWhatsapp", _FlowSpy)
    main._send_beep_flow("+2348010000001")
    to, cta, token = _FlowSpy.calls[-1]
    assert to == "+2348010000001"
    assert token.startswith("beep:+2348010000001:")


def test_set_beep_degrades_to_track_without_flow_id(client, monkeypatch,
                                                    session_factory):
    import FareBeep.config as config
    monkeypatch.setattr(config, "BEEP_FLOW_ID", None)
    sent = []
    monkeypatch.setattr(main, "notifier", type("N", (), {
        "send_text": staticmethod(lambda to, body: sent.append(body) or True),
        "send_typing_indicator": staticmethod(lambda to: True)}))
    main._send_beep_flow("+2348010000002")
    assert sent, "TRACK fallback produced no reply"


def test_card_buttons_carry_set_beep():
    from FareBeep.cards import card_buttons, translate_tap
    btns = card_buttons({"price": 98000}, 1)
    assert btns[-1] == ("set_beep", "Set a beep")
    assert translate_tap("set_beep") == ("set_beep",)
