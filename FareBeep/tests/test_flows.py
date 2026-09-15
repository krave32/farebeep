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
    # hermetic typing bubble: the dispatch path builds MetaWhatsapp()
    # directly, which would hit the real Graph API (token in .env)
    monkeypatch.setattr(main, "MetaWhatsapp", type("W", (), {
        "send_typing_indicator": staticmethod(lambda to: True)}))
    return TestClient(main.app)


def _post_flow(client, payload):
    return client.post("/flow", json=payload)


# -- Q1: ping + screen data -------------------------------------------------
def test_ping_answers_art(client):
    r = _post_flow(client, {"version": "3.0", "action": "ping"})
    assert r.status_code == 200
    assert r.json() == {"data": {"status": "art"}}


def test_init_serves_airports(client):
    r = _post_flow(client, {"version": "3.0", "action": "INIT",
                            "screen": "SET_BEEP_TRIP"})
    body = r.json()
    assert body["screen"] == "SET_BEEP_TRIP"
    assert len(body["data"]["airports"]) == 20


def test_dates_screen_rejects_same_airport(client):
    r = _post_flow(client, {"version": "3.0", "action": "data_exchange",
                            "screen": "SET_BEEP_DATES",
                            "origin": "LOS", "destination": "LOS"})
    assert "same" in r.json()["error"].lower()


# -- Q2: completion creates the watch (idempotent) --------------------------
def _complete_payload(token="beep:+2348010000001:123",
                      origin="LOS", destination="ABV", **kw):
    return {"version": "3.0", "action": "COMPLETE",
            "screen": "SET_BEEP_REVIEW", "flow_token": token,
            "origin": origin, "destination": destination,
            "departure_date": FUTURE_DATE, "passengers": "1", **kw}


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


# -- Q3: invalid completions bounce -----------------------------------------
def test_complete_past_date_bounces(client, session_factory):
    r = _post_flow(client, _complete_payload(departure_date="2001-01-01"))
    body = r.json()
    assert body["screen"] == "SET_BEEP_TRIP"
    assert "past" in body["error"].lower()
    db = session_factory()
    assert db.query(Subscription).count() == 0
    db.close()


def test_complete_same_airports_bounce(client):
    r = _post_flow(client, _complete_payload(destination="LOS"))
    assert "differ" in r.json()["error"].lower()


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


def _encrypt_for(pub_pem: bytes, payload: dict) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes_key = os.urandom(32)
    iv = os.urandom(12)
    blob = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
    pub = serialization.load_pem_public_key(pub_pem)
    wrapped = pub.encrypt(aes_key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(b"\x00\x01" + wrapped + iv + blob), aes_key


def _decrypt_response(aes_key: bytes, body: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(body)
    assert raw[:2] == b"\x00\x01"
    iv, blob = raw[2:14], raw[14:]
    return json.loads(AESGCM(aes_key).decrypt(iv, blob, None))


def test_encrypted_roundtrip(client, monkeypatch, rsa_keypair):
    priv, pub = rsa_keypair
    import FareBeep.config as config
    monkeypatch.setattr(config, "FLOW_PRIVATE_KEY", priv)
    body, aes_key = _encrypt_for(pub, _complete_payload())
    r = client.post("/flow", content=body,
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200
    payload = _decrypt_response(aes_key, r.content)
    assert payload["screen"] == "SUCCESS"


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
         "departure_date": FUTURE_DATE, "passengers": "2"})).encode()
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
         "departure_date": FUTURE_DATE, "passengers": "1"})).encode()
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
