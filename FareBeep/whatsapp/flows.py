"""WhatsApp Flow data-exchange endpoint (the "Set a beep" screens).

Three contract points:
  1. The Flow JSON (whatsapp/flow_screens.json) must use screen ids
     SET_BEEP_TRIP / SET_BEEP_DATES / SET_BEEP_REVIEW - the ids served here.
  2. COMPLETE (flow finished) -> validates + creates the watch through
     SubscriptionMonitor.subscribe - the SAME idempotent service the
     TRACK chat path uses (unique (user, route) row; a second completion
     refreshes the target, never duplicates). The nfm_reply webhook turn
     calls the same service as a crash-safe fallback (endpoint calls are
     not durable; webhook delivery is save-before-ack'd).
  3. Encryption follows Meta's Flows endpoint scheme: request = JSON
     envelope {encrypted_flow_data, encrypted_aes_key, initial_vector}
     (AES-256-GCM with 16B nonce; RSA-OAEP(SHA-256)-wrapped 32B AES key);
     response = base64(AES-256-GCM(flipped_iv, payload)) as text/plain
     with the SAME AES key. Without FLOW_PRIVATE_KEY configured the
     endpoint speaks plaintext - dev only.
"""
import base64
import json
import logging
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

AIRPORTS = [
    {"id": "LOS", "title": "Lagos (LOS)"}, {"id": "ABV", "title": "Abuja (ABV)"},
    {"id": "PHC", "title": "Port Harcourt (PHC)"}, {"id": "KAN", "title": "Kano (KAN)"},
    {"id": "ENU", "title": "Enugu (ENU)"}, {"id": "BNI", "title": "Benin (BNI)"},
    {"id": "CBQ", "title": "Calabar (CBQ)"}, {"id": "ILR", "title": "Ilorin (ILR)"},
    {"id": "KAD", "title": "Kaduna (KAD)"}, {"id": "MDI", "title": "Makurdi (MDI)"},
    {"id": "MIU", "title": "Maiduguri (MIU)"}, {"id": "MXJ", "title": "Minna (MXJ)"},
    {"id": "QOW", "title": "Owerri (QOW)"}, {"id": "SKO", "title": "Sokoto (SKO)"},
    {"id": "YOL", "title": "Yola (YOL)"}, {"id": "ABB", "title": "Asaba (ABB)"},
    {"id": "AKR", "title": "Akure (AKR)"}, {"id": "IBA", "title": "Ibadan (IBA)"},
    {"id": "JOS", "title": "Jos (JOS)"}, {"id": "QRW", "title": "Warri (QRW)"},
]

AIRLINES = [
    {"id": "P4", "title": "Air Peace"}, {"id": "Q9", "title": "Arik Air"},
    {"id": "I7", "title": "Ibom Air"}, {"id": "5N", "title": "Aero Contractors"},
    {"id": "VK", "title": "Green Africa"}, {"id": "UJ", "title": "United Nigeria"},
    {"id": "N2", "title": "Overland Airways"}, {"id": "VL", "title": "ValueJet"},
    {"id": "W3", "title": "Max Air"}, {"id": "R4", "title": "Rano Air"},
]


# ---------------------------------------------------------------------------
# Meta Flows endpoint encryption (AES-256-GCM + RSA-OAEP hybrid)
#
# Wire format (per Meta's official endpoint example):
#   request  = JSON {encrypted_flow_data, encrypted_aes_key, initial_vector}
#              - AES key: 32B, RSA-OAEP(SHA-256)-wrapped, base64
#              - flow data: AES-256-GCM with 16B nonce, tag appended, base64
#   response = base64( AES-256-GCM(flipped_iv, json(payload)) )  text/plain
#              - SAME AES key; the request IV with every bit flipped
# ---------------------------------------------------------------------------
def _decrypt_request(body: bytes) -> tuple[dict, tuple[bytes, bytes] | None]:
    """Returns (payload, (aes_key, iv)). Without FLOW_PRIVATE_KEY the body
    is plaintext JSON and the crypto context is None (dev mode)."""
    from FareBeep.config import FLOW_PRIVATE_KEY
    if not FLOW_PRIVATE_KEY:
        return json.loads(body.decode("utf-8")), None
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = serialization.load_pem_private_key(
        FLOW_PRIVATE_KEY.encode(), password=None)
    envelope = json.loads(body.decode("utf-8"))
    aes_key = key.decrypt(
        base64.b64decode(envelope["encrypted_aes_key"]),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None))
    iv = base64.b64decode(envelope.get("initial_vector")
                          or envelope.get("iv") or "")
    blob = base64.b64decode(envelope["encrypted_flow_data"])
    payload = json.loads(AESGCM(aes_key).decrypt(iv, blob, None))
    return payload, (aes_key, iv)


def _encrypt_response(ctx: tuple[bytes, bytes] | None, payload: dict) -> Response:
    if ctx is None:
        return JSONResponse(payload)
    aes_key, iv = ctx
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    blob = AESGCM(aes_key).encrypt(flipped_iv, json.dumps(payload).encode(), None)
    return Response(content=base64.b64encode(blob),
                    media_type="text/plain")


# ---------------------------------------------------------------------------
# Flow completion -> watch creation (the TRACK service, idempotent)
# ---------------------------------------------------------------------------
def _flow_token_phone(token: str) -> str:
    """Phone from the token minted when the flow was offered
    (beep:{phone}:{ts}). Empty when the token shape is foreign."""
    try:
        if token and token.startswith("beep:"):
            return token.split(":", 2)[1]
    except Exception:
        pass
    return ""


def _validate_beep(d: dict) -> str | None:
    if not d.get("origin"): return "Select a departure city."
    if not d.get("destination"): return "Select a destination."
    if d["origin"] == d["destination"]: return "Cities must differ."
    if not d.get("departure_date"): return "Select a travel date."
    try:
        from FareBeep.dates import lagos_today
        if datetime.strptime(str(d["departure_date"])[:10],
                             "%Y-%m-%d").date() < lagos_today():
            return "Date cannot be in the past."
    except (ValueError, TypeError):
        return "Invalid date."
    try:
        p = int(d.get("passengers") or 1)   # Dropdown yields strings
    except (TypeError, ValueError):
        return "Passengers: 1–9."
    if not 1 <= p <= 9: return "Passengers: 1–9."
    return None


def _target_price(d: dict) -> float | None:
    raw = d.get("target_price")
    if raw in (None, ""):
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _create_beep(phone: str, d: dict) -> None:
    """Idempotent watch creation through the same service TRACK uses.
    Session factory resolved from main at call time (the app's single
    source) so tests and the swap-to-Supabase path stay consistent."""
    from FareBeep.alerts import SubscriptionMonitor
    from FareBeep.models import User, utcnow
    from FareBeep.main import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == phone).first()
        if user is None:
            user = User(phone=phone, first_seen_at=utcnow())
            db.add(user)
            db.commit()
            db.refresh(user)
        SubscriptionMonitor(db).subscribe(
            user.user_id, str(d["origin"]), str(d["destination"]),
            target_price=_target_price(d),
            target_date=str(d.get("departure_date") or "") or None)
    finally:
        db.close()


@router.post("/flow")
async def flow_data_exchange(request: Request):
    try:
        body = await request.body()
        data, aes_key = _decrypt_request(body)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid flow payload")

    action = data.get("action", "")
    screen = data.get("screen", "")
    token = data.get("flow_token", "")
    logger.info("Flow: action=%s screen=%s", action, screen)

    if action == "ping":
        return _encrypt_response(aes_key, {"data": {"status": "art"}})

    if action in ("INIT", "data_exchange"):
        if screen == "SET_BEEP_TRIP":
            return _encrypt_response(aes_key, {"version": "3.0",
                                               "screen": screen,
                                               "data": {"airports": AIRPORTS,
                                                        "airlines": AIRLINES}})
        if screen == "SET_BEEP_DATES":
            o, d = data.get("origin", ""), data.get("destination", "")
            if o and d and o == d:
                return _encrypt_response(aes_key, {"version": "3.0",
                                                   "screen": screen,
                                                   "error": "Origin and destination cannot be the same.",
                                                   "data": {}})
            return _encrypt_response(aes_key, {"version": "3.0",
                                               "screen": screen,
                                               "data": {"origin": o,
                                                        "destination": d}})
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": screen, "data": {}})

    if action == "COMPLETE":
        beep = data.get("beep_data") or data   # nested or flattened form
        if screen == "SET_BEEP_REVIEW" or beep.get("origin"):
            err = _validate_beep(beep)
            if err:
                return _encrypt_response(aes_key, {"version": "3.0",
                                                   "screen": "SET_BEEP_TRIP",
                                                   "error": err, "data": {}})
            phone = _flow_token_phone(token)
            if phone:
                _create_beep(phone, beep)
            return _encrypt_response(aes_key, {"version": "3.0",
                                               "screen": "SUCCESS",
                                               "data": {"extension_message_response": {"params": {"flow_token": token}}}})
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": "SUCCESS", "data": {}})

    return _encrypt_response(aes_key, {"version": "3.0",
                                       "screen": screen, "data": {}})
