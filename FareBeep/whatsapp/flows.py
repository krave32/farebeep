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
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


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
    try:
        payload = json.loads(AESGCM(aes_key).decrypt(iv, blob, None))
    except Exception as exc:
        # Documented behavior: undecryptable requests answer HTTP 421 so
        # Meta re-keys instead of retrying into the void.
        raise HTTPException(421, "Flow request decryption failed") from exc
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


def _validate_beep(d: dict) -> tuple[str, str] | None:
    """Returns (screen, message) for the screen that owns the bad field,
    so an error routes the user back one step - not to screen 1."""
    if not d.get("origin"): return ("SET_BEEP_TRIP", "Select a departure city.")
    if not d.get("destination"): return ("SET_BEEP_TRIP", "Select a destination.")
    if d["origin"] == d["destination"]: return ("SET_BEEP_TRIP", "Cities must differ.")
    if not d.get("departure_date"): return ("SET_BEEP_DATES", "Select a travel date.")
    try:
        from FareBeep.dates import lagos_today
        if datetime.strptime(str(d["departure_date"])[:10],
                             "%Y-%m-%d").date() < lagos_today():
            return ("SET_BEEP_DATES", "Date cannot be in the past.")
    except (ValueError, TypeError):
        return ("SET_BEEP_DATES", "Invalid date.")
    # NOTE: no passenger count here on purpose - a price alert is per
    # seat; pax only matters at booking time (token BOOK path asks for
    # adults/children where it is actually used).
    return None


def _date_bounds() -> dict:
    """DatePicker bounds served as dynamic props (min_date/max_date):
    today in Lagos .. +330 days (the airline booking window)."""
    from FareBeep.dates import lagos_today
    today = lagos_today()
    return {"min_date": today.isoformat(),
            "max_date": (today + timedelta(days=330)).isoformat()}


def _fare_label(origin: str, destination: str, flight_date: str) -> str:
    """Current-fare line for the review screen, read from the Shared
    Ledger ONLY (cache hit or nothing - the flow data endpoint must
    answer inside Meta's ~5s budget, so no live pricing here)."""
    from FareBeep.main import SessionLocal
    from FareBeep.models import FareLedger
    db = SessionLocal()
    try:
        row = db.query(FareLedger).filter_by(
            origin=str(origin).upper(),
            destination=str(destination).upper(),
            flight_date=str(flight_date)[:10]).first()
    finally:
        db.close()
    if row is None or row.price is None:
        return ("No cached fare for this route yet - leave the target "
                "empty and we'll beep you on ANY drop.")
    airline = f" {row.airline}" if row.airline else ""
    return (f"Right now:{airline} ₦{row.price:,.0f} in the Shared Ledger "
            f"for this date.")


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
        # Meta's current docs expect "active" (the legacy value was "art").
        return _encrypt_response(aes_key, {"data": {"status": "active"}})

    if action == "INIT":
        # Flow opened. Meta sends screen="" - the endpoint picks the
        # first screen and serves the DatePicker bounds the screens
        # reference as dynamic props (min_date/max_date).
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": "SET_BEEP_TRIP",
                                           "data": _date_bounds()})

    if action == "BACK":
        previous = {"SET_BEEP_DATES": "SET_BEEP_TRIP",
                    "SET_BEEP_REVIEW": "SET_BEEP_DATES"}.get(screen,
                                                             "SET_BEEP_TRIP")
        data_out = _date_bounds() if previous == "SET_BEEP_DATES" else {}
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": previous,
                                           "data": data_out})

    if action == "data_exchange":
        # Two endpoint steps now (Meta requires a payload whenever the
        # next screen declares a data model, so neither hop can be a
        # static navigate):
        #   SET_BEEP_TRIP  -> SET_BEEP_DATES  (live DatePicker bounds)
        #   SET_BEEP_DATES -> SET_BEEP_REVIEW (Shared Ledger fare label)
        fields = data.get("data") if isinstance(data.get("data"), dict) \
            else data
        origin = str(fields.get("origin") or "")
        dest = str(fields.get("destination") or "")
        when = str(fields.get("departure_date") or "")
        if screen == "SET_BEEP_TRIP" or not when:
            # No date picked yet - serve the date screen's bounds.
            return _encrypt_response(aes_key, {"version": "3.0",
                                               "screen": "SET_BEEP_DATES",
                                               "data": _date_bounds()})
        label = _fare_label(origin, dest, when) if origin and dest else ""
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": "SET_BEEP_REVIEW",
                                           "data": {"fare_label": label,
                                                    **_date_bounds()}})

    if action == "COMPLETE":
        beep = data.get("beep_data") or data   # nested or flattened form
        if screen == "SET_BEEP_REVIEW" or beep.get("origin"):
            err = _validate_beep(beep)
            if err:
                err_screen, err_msg = err
                return _encrypt_response(aes_key, {"version": "3.0",
                                                   "screen": err_screen,
                                                   "error": err_msg,
                                                   "data": _date_bounds()})
            phone = _flow_token_phone(token)
            if phone:
                _create_beep(phone, beep)
        # SUCCESS is a reserved screen name - the documented flow-completion
        # response; it is not part of the routing model.
        return _encrypt_response(aes_key, {"version": "3.0",
                                           "screen": "SUCCESS",
                                           "data": {"extension_message_response": {"params": {"flow_token": token}}}})

    # Unknown action: never return an empty screen - Meta validates every
    # response screen against the routing model. Screens with a declared
    # data model still get their bounds so the DatePicker isn't unbounded.
    fallback = screen if screen in ("SET_BEEP_TRIP", "SET_BEEP_DATES",
                                    "SET_BEEP_REVIEW") else "SET_BEEP_TRIP"
    return _encrypt_response(aes_key, {
        "version": "3.0", "screen": fallback,
        "data": {} if fallback == "SET_BEEP_TRIP" else _date_bounds()})
