"""FareBeep API - WhatsApp conversational layer (Meta Cloud + Twilio test).

Endpoints:
  GET  /webhook/meta    - Meta handshake (hub.challenge echo after
                          hub.verify_token check)
  POST /webhook/meta    - Meta message receiver. X-Hub-Signature-256 is
                          verified with HMAC-SHA256 over the RAW body using
                          META_APP_SECRET (the requirement: `use hmac to
                          verify X-Hub-Signature-256`).
  POST /webhook/twilio  - Twilio WhatsApp Sandbox receiver (test channel).
                          X-Twilio-Signature is verified with the account
                          auth token; the reply goes out via the REST API
                          in a background task.
  POST /webhook/paystack- Paystack transaction events (settles the
                          10-minute booking loop).
  GET  /health          - liveness.

Run:  uvicorn FareBeep.main:app --port 8000
"""
import hashlib
import hmac
import logging
import os
import re
import uuid

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import (HTMLResponse, PlainTextResponse,
                               RedirectResponse)

from FareBeep import brain, chatstate
from FareBeep.config import (APP_BASE_URL, CONSENT_VERSION, MESSAGING_PROVIDER,
                             META_APP_SECRET, META_VERIFY_TOKEN,
                             REQUOTE_TOLERANCE_NGN)
from FareBeep.database import SessionLocal, init_db
from FareBeep.iata import city_name
from FareBeep.models import BookingSession, User, utcnow
from FareBeep.notifier import get_notifier
from FareBeep.payments import verify_paystack_signature
from FareBeep.search import LedgerSearch
from FareBeep.transactions import BookingService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("farebeep.main")

app = FastAPI(title="FareBeep - Transactional Utility")

notifier = get_notifier()

# Per-chat conversational memory lives in the `chat_state` table (see
# FareBeep/chatstate.py) - NOT in RAM, so it survives deploys/restarts and
# works from any replica. Last quoted fare, last ranked list, and any
# pending follow-up question are read/written there on every turn.


@app.on_event("startup")
def _startup():
    """Create the schema on first run (safe in both SQLite + Supabase modes)
    then verify the connection - prints the mission banner:
    '✅ Connected to Supabase Shared Ledger'."""
    from FareBeep.database import verify_connection
    from FareBeep.models import Base
    init_db(Base)
    verify_connection()


# ---------------------------------------------------------------------------
# Meta Cloud API Webhook - HANDSHAKE
# ---------------------------------------------------------------------------
@app.get("/webhook/meta")
async def meta_verify(request: Request):
    """GET verification: echo hub.challenge when hub.verify_token matches."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Meta webhook subscribed (handshake OK)")
        return PlainTextResponse(challenge, media_type="text/plain")
    logger.warning("Meta handshake rejected (mode=%r token=%r)", mode, token)
    return Response(status_code=403)


# ---------------------------------------------------------------------------
# Meta Cloud API Webhook - RECEIVER
# ---------------------------------------------------------------------------
def _verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    """X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(META_APP_SECRET, raw_body).

    The HMAC is computed over the RAW request body - never over a parsed /
    re-serialized version.
    """
    if not signature_header or not META_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook/meta")
async def meta_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    if not _verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Meta webhook REJECTED: bad X-Hub-Signature-256")
        return Response(status_code=403)

    payload = await request.json()
    entry = (payload.get("entry") or [{}])[0]
    changes = (entry.get("changes") or [{}])[0]
    value = changes.get("value") or {}
    message = (value.get("messages") or [{}])[0]
    text = (message.get("text") or {}).get("body", "")
    phone = message.get("from", "")

    # Ack Meta immediately (20s deadline); handle the message off-thread.
    if text and phone:
        background.add_task(_handle_incoming_message, phone, text)
    return Response(content="200 OK", media_type="text/plain")


def _handle_incoming_message(phone: str, text: str) -> None:
    """PASS 2 - CONCIERGE LOGIC: intent -> ask / search / act -> reply."""
    db = SessionLocal()
    try:
        try:
            user = _get_or_create_user(db, phone)

            # PICK GATE (before the brain): a "1", "2" or "3" reply right
            # after a ranked fare list selects and locks THAT fare. Narrow:
            # only when an active list exists AND the message is the number
            # (or "number 2" / "the 2nd one"). Without a list, a bare number
            # falls through to the brain as a DATE - never intercepted.
            pick = _try_pick(db, text, phone)
            if pick is not None:
                if pick == "out_of_range":
                    n = len((chatstate.get_last_fares(db, phone) or {})
                            .get("fares", []))
                    _say(phone, f"I only showed {n} option"
                         + ("s" if n != 1 else "")
                         + ". Reply 1-" + str(n) + ", or ask me for a new search.",
                         user.name)
                elif pick == "unclear":
                    ctx = chatstate.get_last_fares(db, phone) or {}
                    msg = brain.compose_unclear_pick_reply(
                        text, ctx.get("fares") or [], user_name=user.name)
                    _say(user.phone, msg, user.name, humanized=True)
                else:
                    _reply_booking(db, user, brain.Intent(intent="book"),
                                   picked_fare=pick)
                return

            # REQUOTE GATE: after "the price moved to ₦X - still lock it?",
            # a bare yes/book answer proceeds with the already-live-quoted
            # fare; a no/cancel politely aborts. Anything naming a city or
            # date is a fresh request - it falls through to the brain.
            if _try_requote_answer(db, user, text):
                return

            # CONTINUATION: the user answered our follow-up question with a
            # bare city ("abuja" after "where will you be flying from?") -
            # fill the missing piece and search, like a person would.
            if _fill_pending_fare(db, user, text):
                return

            intent = brain.parse_intent(text)
            logger.info("Intent=%s payload=%s phone=%s",
                        intent.intent, intent.as_dict(), phone)

            if intent.name and not user.name:
                user.name = intent.name
                db.commit()
                logger.info("Captured name %s for %s", intent.name, phone)

            if intent.intent == "fare":
                if intent.has_route:
                    chatstate.clear_pending_fare(db, user.phone)  # superseded by a full route
                    _reply_fare(db, user, intent)
                elif intent.destination_iata and intent.date and not intent.origin_iata:
                    # "Find me a flight to Abj for next tuesday" - destination
                    # + date, no origin: search from the default hub (Lagos).
                    # BUT when the destination IS the hub (Lagos), the hub
                    # cannot also be the origin - that would be Lagos->Lagos.
                    # Ask where they're flying from instead.
                    if intent.destination_iata == "LOS":
                        _ask_missing_info(db, user, intent)
                    else:
                        _reply_fare(db, user, intent)
                elif intent.is_partial:
                    _ask_missing_info(db, user, intent)
                else:
                    _say(user.phone, _help_text(), user.name)
            elif intent.intent == "book":
                # BOOK after a fare quote: the route/date the user discussed
                # live in the per-chat context (chat_state), so a bare "BOOK"
                # books exactly what was quoted - never silently today.
                if intent.has_route or chatstate.get_last_fare(db, user.phone) is not None:
                    _reply_booking(db, user, intent)
                elif intent.is_partial:
                    _ask_missing_info(db, user, intent)
                else:
                    _say(user.phone,
                         "To book: send your route, e.g. 'BOOK Lagos to "
                         "Abuja tomorrow' - or just say BOOK right after a "
                         "fare quote I sent you.",
                         user.name)
            elif intent.intent == "subscribe":
                _reply_subscribe(db, user, intent)
            elif intent.intent == "unsubscribe":
                _reply_unsubscribe(db, user)
            elif intent.intent in ("status", "track"):
                _reply_status_ack(user, intent)
            else:
                _say(user.phone, _help_text(), user.name)
        except Exception as e:
            # never let a single turn crash the webhook thread
            logger.error("Message handling failed (%s): %s", phone, e)
    finally:
        db.close()


def _say(phone: str, msg: str, user_name: str = None,
         humanized: bool = False) -> None:
    """Send a reply. By default Gemini runs a personality pass first so the
    bot sounds human; on AI failure the template goes out verbatim. When the
    message is ALREADY AI-narrated (the ranked fare list) pass humanized=True
    so it is sent verbatim - no second personality pass to garble the prices."""
    if not msg:
        notifier.send_text(phone, "")
        return
    if humanized:
        notifier.send_text(phone, msg)
        return
    notifier.send_text(phone, brain.compose_reply(msg, user_name=user_name))


_YES_WORDS = {"yes", "yeah", "yep", "y", "sure", "ok", "okay", "alright",
              "book", "lock", "proceed", "confirm", "continue", "go", "ahead"}
_NO_WORDS = {"no", "nope", "nah", "cancel", "forget", "never", "stop"}


def _try_requote_answer(db, user: User, text: str) -> bool:
    """Answer to the price-move question ("the price moved to ₦X - still
    lock it?"). YES/BOOK -> book the already re-quoted fare; NO/CANCEL ->
    politely abort. Returns True when handled. A message naming a city or
    a date is a fresh request, not an answer - it falls through."""
    pending = chatstate.get_pending_requote(db, user.phone)
    if not pending:
        return False
    if brain.single_city(text) is not None or brain.has_date(text):
        return False
    words = re.findall(r"[a-z]+", text.lower())
    if not words:
        return False
    if all(w in _YES_WORDS for w in words):
        chatstate.clear_pending_requote(db, user.phone)
        _reply_requoted_booking(db, user, pending)
        return True
    if all(w in _NO_WORDS for w in words):
        chatstate.clear_pending_requote(db, user.phone)
        _say(user.phone,
             "No problem - that quote is gone anyway. Ask me for a fresh "
             "search and I'll check the latest price.",
             user.name)
        return True
    return False


def _reply_requoted_booking(db, user: User, pending: dict) -> None:
    """Book the fare that was LIVE re-quoted before the price-move question
    (no re-fetch - it is seconds old). Surge is still refused."""
    fare = pending.get("fare") or {}
    origin_iata = pending.get("origin_iata")
    destination_iata = pending.get("destination_iata")
    flight_date = pending.get("flight_date")
    if not (origin_iata and destination_iata and flight_date and fare):
        _say(user.phone,
             "That quote has expired - ask me for a fresh search and I'll "
             "check the latest price.",
             user.name)
        return
    if fare.get("above_guardrail"):
        _say(user.phone,
             "That fare is at a surge price right now - I can't lock it "
             "safely. Try again in a bit, or TRACK it and I'll Beep you "
             "when it normalises.",
             user.name)
        return
    _create_and_send_booking(db, user, origin_iata, destination_iata,
                             flight_date, fare,
                             flight_iata=fare.get("flight_number"))


def _fill_pending_fare(db, user: User, text: str) -> bool:
    """Continue a partial fare conversation: we asked a follow-up (e.g.
    "where will you be flying from?"), and the user answered with a bare
    city. Fill the missing slot and run the search. Returns True when
    handled. Only fires for a clean single-city answer with no new date -
    anything richer is a fresh request for the brain."""
    pending = chatstate.get_pending_fare(db, user.phone)
    if not pending:
        return False
    iata = brain.single_city(text)
    if iata is None or brain.has_date(text):
        return False
    origin_iata = pending.get("origin_iata")
    destination_iata = pending.get("destination_iata")
    date_ = pending.get("date")
    if not origin_iata and destination_iata:
        origin_iata = iata
    elif not destination_iata and origin_iata:
        destination_iata = iata
    else:
        return False
    if not (origin_iata and destination_iata and date_):
        return False
    chatstate.clear_pending_fare(db, user.phone)
    intent = brain.Intent(intent="fare",
                          origin=city_name(origin_iata),
                          destination=city_name(destination_iata),
                          date=date_)
    logger.info("Filled pending fare: %s -> %s on %s (from '%s')",
                origin_iata, destination_iata, date_, text)
    _reply_fare(db, user, intent)
    return True


def _ask_missing_info(db, user: User, intent: brain.Intent) -> None:
    """Pass 2 follow-up: the extraction was partial - ask for the missing
    pieces like a human agent would, never show a blank error. For fare
    partials we remember what we already know (chat_state) so the user's
    next bare-city answer completes the search (_fill_pending_fare)."""
    if intent.intent == "fare":
        chatstate.set_pending_fare(db, user.phone, {
            "origin_iata": intent.origin_iata,
            "destination_iata": intent.destination_iata,
            "date": intent.date,
        })
    dest = city_name(intent.destination_iata) if intent.destination_iata else None
    origin = city_name(intent.origin_iata) if intent.origin_iata else None
    if dest and not origin and not intent.date:
        msg = (f"I'd love to help you get to {dest}! ✈️ Where will you be "
               f"flying from, and what date are you looking at?")
    elif dest and not origin:
        msg = f"I'd love to help you get to {dest}! ✈️ Where will you be flying from?"
    elif origin and not dest:
        msg = f"Great - from {origin}! 🎉 Where are you headed?"
    else:
        msg = ("I'd love to help! ✈️ Tell me where you're going, where you're "
               "flying from, and what date you have in mind.")
    _say(user.phone, msg, user.name)


def _get_or_create_user(db, phone: str) -> User:
    user = db.query(User).filter(User.phone == phone).first()
    if user is None:
        user = User(phone=phone, first_seen_at=utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _try_pick(db, text: str, phone: str):
    """Interpret a reply as a ranked-list pick. Returns the picked fare dict,
    "out_of_range" when the number exceeds the list, "unclear" when the reply
    looks like a pick but no option resolves, or None to fall through to the
    brain (no active list, or not a pick-shaped message)."""
    ctx = chatstate.get_last_fares(db, phone)
    if not ctx or not ctx.get("fares"):
        return None
    m = _PICK_RE.search(text.strip())
    if m:
        n = int(m.group(1))
        if n > len(ctx["fares"]):
            return "out_of_range"
        return ctx["fares"][n - 1]
    # Natural-language pick ("the second one", "Air Peace", "the 7am flight",
    # "the cheapest") resolved by the brain. Gated on pick signals so an
    # unrelated message never costs an AI call.
    if _looks_like_pick(text, ctx["fares"]):
        idx = brain.resolve_pick(text, ctx["fares"])
        if idx is None:
            # A QUESTION about the list ("is that the cheapest?", "are they
            # the same airline?") is NOT a pick - let the brain answer it
            # instead of forcing a 1-2-3 redirect.
            if _QUESTION_RE.search(text):
                return None
            return "unclear"
        return ctx["fares"][idx - 1]
    return None


# A bare "2", "number 2", "option 2", "pick 2", "the 2nd one", "#2".
# Only reached when an active ranked list exists (see _try_pick).
_PICK_RE = re.compile(
    r"^(?:the\s+)?(?:number|option|pick|choose|select)?\s*#?\s*"
    r"([1-9])\s*(?:st|nd|rd|th)?\s*(?:one)?\s*$",
    re.IGNORECASE)

# Cheap gate before the AI pick resolver: only messages that could plausibly
# be picking (ordinals, "one", "cheapest", an am/pm time, ...) spend an AI
# call. Everything else falls through to the brain untouched.
_PICK_SIGNAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|last|"
    r"one|option|cheapest|cheaper|cheap|best|deal|pick|choose|select)\b"
    r"|\b\d(?:st|nd|rd|th)\b|\b\d{1,2}\s*(?:am|pm)\b", re.IGNORECASE)

_QUESTION_RE = re.compile(
    r"\?|\b(is|are|was|were|do|does|did|can|could|what|which|how|when|"
    r"where)\b", re.IGNORECASE)


def _looks_like_pick(text: str, fares: list) -> bool:
    """True when the message could be choosing a listed fare - so we try the
    pick resolver; False means it is unrelated and should go to the brain."""
    if _PICK_SIGNAL_RE.search(text):
        return True
    flat = text.lower()
    upper = text.upper()
    for f in fares:
        airline = (f.get("airline") or "").lower()
        if airline and any(t in flat for t in airline.split() if len(t) >= 3):
            return True
        fnum = (f.get("flight_number") or "").upper()
        if fnum and fnum in upper:
            return True
        dep = f.get("departs_at")
        if dep and re.match(r"\d{1,2}:\d{2}", str(dep)) and str(dep)[:5] in flat:
            return True
    return False


def _reply_fare(db, user: User, intent: brain.Intent) -> None:
    # A fresh search supersedes any open price-move question.
    chatstate.clear_pending_requote(db, user.phone)
    # Pass 2: destination-only with a date -> search from the user's default
    # hub (Lagos - Nigeria's main base). The fare reply names both cities,
    # so any wrong assumption is visible and correctable.
    origin_iata = intent.origin_iata or "LOS"
    search = LedgerSearch(db)
    fares, surge_fares = search.search_list(origin_iata,
                                            intent.destination_iata,
                                            intent.date, limit=3)
    if not fares:
        if surge_fares:
            # Everything is above the guardrail - flag it, don't fake "no fares".
            surge_price = min(f["price"] for f in surge_fares)
            _say(user.phone,
                 f"Flights are at a surge rate on "
                 f"{city_name(origin_iata)} -> "
                 f"{city_name(intent.destination_iata)} right now. 🚨 Prices "
                 f"are unusually high (from ₦{surge_price:,.0f}) - I won't "
                 f"sell you that. Try again in a bit, or TRACK and I'll Beep "
                 f"you when it normalises.",
                 user.name)
            return
        _say(user.phone,
             f"No fare found {city_name(origin_iata)} -> "
             f"{city_name(intent.destination_iata)} for "
             f"{intent.date or 'that date'}.",
             user.name)
        return

    if len(fares) == 1:
        # Thin route: one result - keep the classic single-fare reply.
        fare = fares[0]
        chatstate.set_last_fare(db, user.phone, {
            "origin_iata": origin_iata,
            "destination_iata": intent.destination_iata,
            "flight_date": fare["flight_date"],
            "price": fare["price"],
            "airline": fare["airline"],
        })
        _say(user.phone,
             f"Fare {city_name(origin_iata)} -> "
             f"{city_name(intent.destination_iata)} {fare['flight_date']}:\n"
             f"₦{fare['price']:,.0f} via {fare['airline']} (live)\n"
             f"Verify: {fare['verify_link']}\n"
             f"Reply BOOK to buy at ₦{_booking_total(fare['price']):,.0f} "
             f"(Paystack), or TRACK to get a Beep when it drops.",
             user.name)
        return

    # Ranked list - one option per airline, NARRATED by the brain so it reads
    # like a person presenting choices, not a menu. The numbered options stay
    # pickable (reply 1, 2 or 3 - or just say the airline).
    chatstate.set_last_fares(db, user.phone, {
        "origin_iata": origin_iata,
        "destination_iata": intent.destination_iata,
        "flight_date": fares[0]["flight_date"],
        "fares": fares,
    })
    origin = city_name(origin_iata)
    destination = city_name(intent.destination_iata)
    msg = brain.compose_ranked_reply(
        fares, origin, destination, fares[0]["flight_date"],
        user_name=user.name)
    _say(user.phone, msg, user.name, humanized=True)


def _booking_total(airline_price: float) -> float:
    from FareBeep.payments import calculate_final_price
    return calculate_final_price(airline_price)["total_amount"]


def _reply_booking(db, user: User, intent: brain.Intent,
                   picked_fare: dict = None) -> None:
    """THE LIVE HANDSHAKE - what happens when a user replies BOOK.

    1. FORCE REFRESH: the Shared Ledger is ignored; SerpApi is queried
       LIVE so the seat exists at the quoted price right now.
    2. Session: a booking_session row is saved with expires_at = now + 10m.
    3. The WhatsApp/TG call: the Paystack TEST link + the "Price Locked"
       message, with the 10-minute expiry stated up front.

    picked_fare: when set, the user picked "1, 2 or 3" from a ranked list -
    the route/date come from the list context and the SELECTED flight is
    re-verified live (by flight number) so the price is fresh, never the
    stale list price.

    Date resolution: the intent's own date wins; otherwise the date of the
    LAST fare quote in this chat (so "BOOK" right after a quote books that
    flight). With neither, the bot ASKS - it never silently books today.
    """
    ctx = chatstate.get_last_fare(db, user.phone) or {}
    list_ctx = chatstate.get_last_fares(db, user.phone) or {}
    if picked_fare is not None:
        origin_iata = list_ctx.get("origin_iata")
        destination_iata = list_ctx.get("destination_iata")
        flight_date = list_ctx.get("flight_date")
        if not (origin_iata and destination_iata and flight_date):
            _say(user.phone,
                 "I've lost that fare list - ask me for the fares again "
                 "and I'll re-run the search.",
                 user.name)
            return
    else:
        origin_iata = intent.origin_iata or ctx.get("origin_iata")
        destination_iata = intent.destination_iata or ctx.get("destination_iata")
        flight_date = intent.date or ctx.get("flight_date")
        if not (origin_iata and destination_iata):
            _say(user.phone,
                 "To book: send your route, e.g. BOOK Lagos to Abuja tomorrow "
                 "(or BOOK right after a fare quote I just sent you).",
                 user.name)
            return
        if not flight_date:
            _say(user.phone,
                 "Which date? E.g. BOOK Lagos to Abuja on the 31st - I won't "
                 "book a flight for today without you saying so.",
                 user.name)
            return

    search = LedgerSearch(db)
    expected_price = None
    if picked_fare is not None:
        # Force-refresh the ranked list live and re-locate the picked flight
        # by its number (prices move - never book the stale list price).
        fares, _ = search.search_list(origin_iata, destination_iata,
                                      flight_date, limit=6)
        picked_no = picked_fare.get("flight_number")
        if picked_no:
            fare = next((f for f in fares
                         if f.get("flight_number") == picked_no), None)
        else:
            fare = next((f for f in fares
                         if f.get("airline") == picked_fare.get("airline")
                         and f.get("departs_at") == picked_fare.get("departs_at")),
                        None)
        if fare is None:
            _say(user.phone,
                 "That option is no longer available at that price - the "
                 "seat or fare may have moved. Reply 1, 2 or 3 again, or "
                 "ask me for a fresh search.",
                 user.name)
            return
        expected_price = picked_fare.get("price")
        if fare.get("above_guardrail"):
            _say(user.phone,
                 "That fare is at a surge price right now - I can't lock it "
                 "safely. Try again in a bit, or TRACK it and I'll Beep you "
                 "when it normalises.",
                 user.name)
            return
    else:
        fare = search.search(origin_iata, destination_iata, flight_date,
                             force_refresh=True)
        if fare is None:
            _say(user.phone,
                 "That seat/route is no longer available right now - ask me for "
                 "the latest fare again and I'll re-check live.",
                 user.name)
            return
        if fare.get("above_guardrail"):
            _say(user.phone,
                 "That fare is at a surge price right now - I can't lock it "
                 "safely. Try again in a bit, or TRACK it and I'll Beep you "
                 "when it normalises.",
                 user.name)
            return
        expected_price = ctx.get("price") if ctx else None

    # THE PRICE-MOVE HANDCHECK: the live re-quote may differ from what the
    # user was shown. Within the tolerance (the natural volatility buffer)
    # book silently; beyond it, ASK first - never take a silent price jump.
    if (expected_price is not None
            and abs(fare["price"] - expected_price) > REQUOTE_TOLERANCE_NGN):
        chatstate.clear_last_fares(db, user.phone)   # superseded by the question
        chatstate.set_pending_requote(db, user.phone, {
            "origin_iata": origin_iata,
            "destination_iata": destination_iata,
            "flight_date": fare["flight_date"],
            "fare": fare,
        })
        _say(user.phone,
             f"The fare moved from ₦{expected_price:,.0f} to "
             f"₦{fare['price']:,.0f} while you were deciding - the airline "
             f"repriced it. Still lock it at the new price? Reply YES or "
             f"BOOK - or NO to cancel.",
             user.name)
        return

    _create_and_send_booking(db, user, origin_iata, destination_iata,
                             fare["flight_date"], fare,
                             flight_iata=fare.get("flight_number")
                             or intent.flight)

    if picked_fare is not None:
        # A pick is consumed by booking: a stray "2" later must not rebook.
        chatstate.clear_last_fares(db, user.phone)


def _create_and_send_booking(db, user: User, origin_iata: str,
                             destination_iata: str, flight_date: str,
                             fare: dict, flight_iata: str = None) -> None:
    """Create the 10-minute booking session and send the price-locked
    message with the confirmation-page link. Shared by the direct BOOK flow
    and the re-quoted "yes" flow."""
    bookings = BookingService(db)
    try:
        result = bookings.create_booking(
            user.user_id, origin_iata, destination_iata,
            flight_date, fare["price"],
            flight_iata=flight_iata,
            email=user.email or f"{user.phone.replace('+', '')}@farebeep.ng",
            airline=fare.get("airline"),
            source="serpapi")
    except Exception as e:
        logger.error("Booking creation failed: %s", e)
        _say(user.phone, "Payment link could not be created. Try again in a minute.", user.name)
        return

    session = result["session"]
    expires = result["expires_at"].strftime("%H:%M")
    origin = city_name(origin_iata)
    destination = city_name(destination_iata)
    # The user goes to the booking confirmation page (NOT the raw Paystack
    # URL): it reconfirms the price breakdown + captures NDPA consent before
    # redirecting to payment. Every booking flows through this page.
    book_url = f"{APP_BASE_URL}/book/{session.id}"
    _say(user.phone,
         f"🔒 PRICE LOCKED for 10 minutes.\n"
         f"{origin} -> {destination} on {flight_date}\n"
         f"Airline price: ₦{fare['price']:,.0f}\n"
         f"Markup + fees: ₦{session.markup + session.processing_fee:,.0f}\n"
         f"TOTAL:         ₦{result['total_amount']:,.0f}\n\n"
         f"Confirm & pay here (valid until {expires} today):\n"
         f"{book_url}\n\n"
         f"⚠️ PAYSTACK TEST MODE - use the test card, no real money leaves "
         f"your account.\n"
         f"Miss the window? Your payment is auto-refunded - no questions.",
         user.name)


def _reply_status_ack(user: User, intent: brain.Intent) -> None:
    flight = intent.flight or "your flight"
    _say(user.phone,
         f"Status watch for {flight} is attached to a paid booking. "
         f"We'll text you via template 3h before departure if anything changes.",
         user.name)


def _reply_subscribe(db, user: User, intent: brain.Intent) -> None:
    """Set a fare-drop subscription (target price optional).

    Like BOOK: a bare TRACK right after a fare quote arms the alert on the
    ROUTE + DATE the user was just shown (never a made-up route). The route
    lives in the single-fare context OR the active ranked list - both count
    as "the route we were just discussing"."""
    from FareBeep.alerts import SubscriptionMonitor
    ctx = chatstate.get_last_fare(db, user.phone) or {}
    list_ctx = chatstate.get_last_fares(db, user.phone) or {}
    origin_iata = (intent.origin_iata or ctx.get("origin_iata")
                   or list_ctx.get("origin_iata"))
    destination_iata = (intent.destination_iata or ctx.get("destination_iata")
                        or list_ctx.get("destination_iata"))
    if not (origin_iata and destination_iata):
        _say(user.phone,
             "To set a price alert, send your route, e.g. 'TRACK Lagos to "
             "Abuja below 80k' - or just say TRACK right after a fare quote.",
             user.name)
        return
    target_date = (intent.date or ctx.get("flight_date")
                   or list_ctx.get("flight_date"))

    monitor = SubscriptionMonitor(db)
    monitor.subscribe(user.user_id, origin_iata, destination_iata,
                      target_price=intent.target_price, target_date=target_date)

    origin = city_name(origin_iata)
    destination = city_name(destination_iata)
    if intent.target_price is not None:
        msg = (f"✅ Beep armed: {origin} -> {destination}\n"
               f"We'll text you the moment it hits ₦{intent.target_price:,.0f} or lower.")
    else:
        msg = (f"✅ Beep armed: {origin} -> {destination}\n"
               f"We'll text you when the fare drops by 10% or more.")
    _say(user.phone, msg, user.name)


def _reply_unsubscribe(db, user: User) -> None:
    """Remove all subscriptions for the user (NDPA-style data removal)."""
    from FareBeep.alerts import SubscriptionMonitor
    removed = SubscriptionMonitor(db).unsubscribe(user.user_id)
    if removed:
        _say(user.phone,
             f"🔕 {removed} price alert(s) removed. Your route data is deleted.",
             user.name)
    else:
        _say(user.phone, "You have no active price alerts.", user.name)


def _help_text() -> str:
    return ("Beep! 🎫 FareBeep is your fast Nigerian flight concierge. "
            "Just tell me where and when, e.g. 'Lagos to Abuja tomorrow'\n"
            "      'BOOK Lagos to Abuja' (reserve in 10 mins)\n"
            "      'TRACK Lagos to Abuja below 80k' (price alert)\n"
            "      'Track P47123' (flight status)")


# ---------------------------------------------------------------------------
# Twilio WhatsApp Sandbox Webhook - TEST CHANNEL
# ---------------------------------------------------------------------------
def _verify_twilio_signature(raw_url: str, form: dict, signature: str) -> bool:
    """Compare X-Twilio-Signature (base64 HMAC-SHA1 over the URL + POST
    params, keyed by the account auth token) the way Twilio ships it."""
    if not signature or not MESSAGING_PROVIDER.lower() == "twilio":
        return False
    from twilio.request_validator import RequestValidator
    try:
        validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN") or "")
        return validator.validate(raw_url, form, signature)
    except Exception as e:
        logger.warning("Twilio signature validation error: %s", e)
        return False


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request, background: BackgroundTasks):
    """Twilio sandbox receiver (form-encoded, x-twilio-signature verified).

    Replies are sent by the background task THROUGH the REST API, so this
    handler always answers Twilio with an empty TwiML ack instantly.
    """
    form = dict(await request.form())
    sig = request.headers.get("X-Twilio-Signature", "")
    if not _verify_twilio_signature(str(request.url), form, sig):
        logger.warning("Twilio webhook REJECTED: bad X-Twilio-Signature")
        return Response(status_code=403)

    phone = str(form.get("From", "")).removeprefix("whatsapp:")
    text = str(form.get("Body", ""))
    if text and phone:
        background.add_task(_handle_incoming_message, phone, text)
    return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>",
                    media_type="application/xml")


@app.get("/webhook/twilio")
async def twilio_verify(request: Request):
    """Twilio sandbox webhook settings may fire a GET before the POST."""
    return PlainTextResponse("FareBeep Twilio webhook is live")


# ---------------------------------------------------------------------------
# Telegram Bot API Webhook - FASTEST TEST CHANNEL
# ---------------------------------------------------------------------------
def _verify_telegram_secret(secret: str) -> bool:
    """X-Telegram-Bot-Api-Secret-Token set via setWebhook(secret_token)."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
    return bool(expected) and hmac.compare_digest(expected, secret)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    """Telegram Bot API receiver (JSON, secret-token header verified).

    The chat_id IS the user identity (stored where a phone number would
    live on the WhatsApp channels), so the whole conversational pipeline
    is reused unchanged.
    """
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _verify_telegram_secret(secret):
        logger.warning("Telegram webhook REJECTED: bad secret token")
        return Response(status_code=403)

    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(message.get("text") or "")
    if text and chat_id:
        background.add_task(_handle_incoming_message, chat_id, text)
    return {"ok": True}


@app.get("/webhook/telegram")
async def telegram_verify(request: Request):
    return PlainTextResponse("FareBeep Telegram webhook is live")


# ---------------------------------------------------------------------------
# Paystack webhook - the other half of the settlement loop
# ---------------------------------------------------------------------------
def _notify_admin(text: str) -> None:
    """Best-effort admin alert (Refund Required etc.). Goes to
    ADMIN_ALERT_PHONE if configured, else the log (a real ops channel can
    be wired without touching the loop)."""
    from FareBeep.config import ADMIN_ALERT_PHONE
    if ADMIN_ALERT_PHONE:
        try:
            notifier.send_text(ADMIN_ALERT_PHONE, text)
            logger.info("Admin alert sent to %s", ADMIN_ALERT_PHONE)
            return
        except Exception as e:
            logger.error("Admin alert send failed: %s", e)
    logger.warning("ADMIN ALERT (no recipient configured): %s", text)


def _notify_session_user(session, text: str) -> None:
    """Send an outbound message to the session's owner (WhatsApp number or
    Telegram chat_id - whatever the user's phone column holds)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == session.user_id).first()
    finally:
        db.close()
    if user is None:
        logger.warning("Session %s has no user row - message not sent: %s",
                       session.payment_ref, text)
        return
    notifier.send_text(user.phone, text)


@app.post("/webhook/paystack")
async def paystack_webhook(request: Request):
    """Paystack event receiver - X-Paystack-Signature (HMAC-SHA512) verified
    over the RAW body. On charge.success the booking is gated by expires_at:

      now() <= expires_at  -> status = paid, mock Ticket Issued (PNR FB-XXXX)
      now() >  expires_at  -> status = expired, Refund Required admin alert
                              + the user is told a refund/price-match is coming
    """
    raw = await request.body()
    if not verify_paystack_signature(
            raw, request.headers.get("x-paystack-signature", "")):
        logger.warning("Paystack webhook REJECTED: bad signature")
        return Response(status_code=403)

    payload = await request.json()
    event = payload.get("event", "")
    data = payload.get("data") or {}
    reference = str(data.get("reference", ""))
    status = str(data.get("status", ""))

    db = SessionLocal()
    try:
        bookings = BookingService(db)
        outcome = bookings.settle_payment(reference, status)
        logger.info("Paystack %s -> %s", reference, outcome["outcome"])

        session = outcome.get("session")
        if outcome["outcome"] == "paid":
            pnr = outcome.get("pnr") or "FB-????"
            _notify_session_user(
                session,
                f"✅ BOOKING CONFIRMED - payment received!\n"
                f"PNR: {pnr} (provisional)\n"
                f"{city_name(session.origin)} -> {city_name(session.destination)} "
                f"{session.flight_date}\n"
                f"Paid: ₦{session.total_price:,.0f}\n"
                f"Your e-ticket is issued once the airline confirms "
                f"availability. Safe travels!")
        elif outcome["outcome"] == "refund_required":
            _notify_session_user(
                session,
                "Payment was successful, but the 10-minute window closed. "
                "Your refund is being processed automatically - no "
                "questions.")
            # THE REFUND PROMISE: the airline was never ticketed, so the
            # money goes back via the Paystack API - not just an alert.
            refunded = False
            try:
                from FareBeep.payments import refund_paystack_transaction
                refund_paystack_transaction(reference)
                refunded = True
            except Exception as e:
                logger.error("Auto-refund failed for %s: %s", reference, e)
            if refunded:
                tail = "No action needed - Paystack refund issued."
            else:
                tail = ("MANUAL REFUND REQUIRED - the Paystack refund call "
                        "failed.")
            _notify_admin(
                f"{'AUTO-REFUNDED' if refunded else 'REFUND FAILED'}: "
                f"{reference} was paid after its 10-minute lock expired "
                f"({session.expires_at}); the airline API was NOT called. "
                f"{tail}")
        return {"ok": True, "outcome": outcome["outcome"]}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    # Vendor contract drift probe: when the fare source's payload changes a
    # pinned field name, `providers.probe_contract` reports it here so churn
    # shows up as a health signal before users see a broken quote.
    from FareBeep import providers
    probe = providers.tiqwa_probe()
    if probe is None:
        return {"status": "ok", "service": "FareBeep"}
    return {"status": "ok" if probe["ok"] else "degraded",
            "service": "FareBeep", "fare_provider_probe": probe}


# ---------------------------------------------------------------------------
# Booking confirmation page - the ONLY place a user proceeds to payment.
# The WhatsApp bot sends a link here instead of a raw Paystack URL. The page
# reconfirms the quoted price (breakdown) and captures NDPA consent (version
# + timestamp + phone) BEFORE the Paystack redirect. All bookings flow
# through this page, so consent is always captured - no text-parsing needed.
# ---------------------------------------------------------------------------
_CONSENT_TEXT = (
    "By proceeding, you agree that FareBeep may collect and process your "
    "details (and, for ticket purchases, passenger/travel document data) "
    "for the purpose of booking, pricing and confirming your flight, "
    "sending you fare alerts, and contacting you about your booking. "
    "We do not share your data with third parties for their own marketing. "
    "You can stop alerts anytime by replying STOP. "
    "This page confirms your INTENT to purchase the flight shown at the "
    "quoted total - the price is locked for 10 minutes, and your ticket is "
    "issued after payment is confirmed by our payment partner. "
    "This is our current data notice (version {version}).")


def _as_naive(dt):
    """Normalise a datetime for safe comparison.

    Postgres returns timestamptz columns as offset-NAIVE datetimes, while
    models.utcnow() is offset-aware - comparing the two raises TypeError.
    Strip tzinfo from both sides before comparing (all values are UTC).
    """
    return dt.replace(tzinfo=None) if dt is not None else None


def _is_expired(session: BookingSession) -> bool:
    return _as_naive(session.expires_at) < _as_naive(utcnow())


def _booking_not_found() -> HTMLResponse:
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Booking</title></head>
<body style="font-family:system-ui,sans-serif;background:#0b1220;color:#e8eef7;
display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#131c2e;border:1px solid #26324a;border-radius:16px;
padding:2.5rem;max-width:26rem;text-align:center">
<h1>Booking not found</h1>
<p style="color:#9fb0c9">This link is invalid or already used. Ask the bot
for a fresh fare and booking in WhatsApp.</p>
</div></body></html>""")


def _booking_closed(origin: str = "", destination: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Window closed</title></head>
<body style="font-family:system-ui,sans-serif;background:#0b1220;color:#e8eef7;
display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#131c2e;border:1px solid #26324a;border-radius:16px;
padding:2.5rem;max-width:26rem;text-align:center">
<h1>Price window closed</h1>
<p style="color:#9fb0c9">The 10-minute price lock for
{origin} &rarr; {destination} has expired. Send a new message in WhatsApp
(e.g. &ldquo;BOOK Lagos to Abuja tomorrow&rdquo;) and I&rsquo;ll re-check
the latest fare for you.</p>
</div></body></html>""")


def _booking_page(session: BookingSession) -> HTMLResponse:
    origin = city_name(session.origin)
    destination = city_name(session.destination)
    airline = (session.flight_details or {}).get("airline") or "—"
    expires = session.expires_at.strftime("%H:%M")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Confirm booking</title>
<style>
 body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif;
        background:#0b1220; color:#e8eef7; display:grid; place-items:center;
        min-height:100vh; margin:0; }}
 .card {{ background:#131c2e; border:1px solid #26324a; border-radius:16px;
         padding:2rem; max-width:26rem; width:100%; box-sizing:border-box; }}
 h1 {{ font-size:1.25rem; margin:0 0 .25rem; }}
 .route {{ color:#9fb0c9; margin:0 0 1.25rem; }}
 .row {{ display:flex; justify-content:space-between; padding:.35rem 0;
         border-bottom:1px solid #1f2a3f; color:#9fb0c9; }}
 .row.total {{ border-bottom:none; color:#e8eef7; font-weight:700;
               font-size:1.1rem; }}
 .locked {{ font-size:.8rem; color:#6ee7a0; margin:1rem 0; }}
 .notice {{ background:#101a2c; border:1px solid #26324a; border-radius:10px;
           padding:.9rem; font-size:.8rem; color:#9fb0c9; line-height:1.5;
           margin:1rem 0; }}
 button {{ width:100%; background:#22c55e; color:#06210f; border:0;
          border-radius:10px; padding:.85rem; font-size:1rem; font-weight:700;
          cursor:pointer; }}
</style></head>
<body>
<div class="card">
  <h1>Confirm your booking</h1>
  <p class="route">{origin} &rarr; {destination} &middot; {session.flight_date} &middot; {airline}</p>

  <div class="row"><span>Airline price</span><span>&#8358;{session.airline_price:,.0f}</span></div>
  <div class="row"><span>Markup + fees</span><span>&#8358;{session.markup + session.processing_fee:,.0f}</span></div>
  <div class="row total"><span>Total to pay</span><span>&#8358;{session.total_price:,.0f}</span></div>

  <p class="locked">Price locked, valid until {expires} today. Payments after
  the window are auto-refunded.</p>

  <div class="notice">{_CONSENT_TEXT.format(version=CONSENT_VERSION)}</div>

  <form method="post" action="/book/{session.id}/confirm">
    <button type="submit">I agree &amp; Proceed to Payment</button>
  </form>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/book/{session_id}")
def booking_page(session_id: uuid.UUID):
    db = SessionLocal()
    try:
        session = db.get(BookingSession, session_id)
        if session is None:
            return _booking_not_found()
        if _is_expired(session):
            return _booking_closed(session.origin, session.destination)
        return _booking_page(session)
    finally:
        db.close()


@app.post("/book/{session_id}/confirm")
def booking_confirm(session_id: uuid.UUID):
    """Record NDPA consent for the session's user, then send them to Paystack."""
    db = SessionLocal()
    try:
        session = db.get(BookingSession, session_id)
        if session is None:
            return _booking_not_found()
        if _is_expired(session):
            return _booking_closed(session.origin, session.destination)
        user = db.get(User, session.user_id)
        if user is not None:
            user.consent_at = utcnow()
            user.consent_text_version = CONSENT_VERSION
            db.commit()
            logger.info("Consent recorded v%s for user %s (booking %s)",
                        CONSENT_VERSION, session.user_id, session.payment_ref)
        return RedirectResponse(session.callback_url or "/payment/status",
                                status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Payment status page - where Paystack sends the user after checkout
# (PAYSTACK_CALLBACK_URL). The actual settlement happens in the webhook;
# this page is just a friendly confirmation screen.
# ---------------------------------------------------------------------------
@app.get("/payment/status")
def payment_status(reference: str = ""):
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Payment</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif;
         background: #0b1220; color: #e8eef7; display: grid; place-items: center;
         min-height: 100vh; margin: 0; }}
  .card {{ background: #131c2e; border: 1px solid #26324a; border-radius: 16px;
          padding: 2.5rem; max-width: 26rem; text-align: center; }}
  .check {{ font-size: 3rem; }}
  h1 {{ font-size: 1.25rem; margin: 0.75rem 0 0.5rem; }}
  p {{ color: #9fb0c9; margin: 0.25rem 0; line-height: 1.5; }}
  .ref {{ color: #64748b; font-size: 0.85rem; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="check">✅</div>
  <h1>Payment received</h1>
  <p>Your booking is being confirmed.<br>We&#39;ll send your ticket and PNR to your WhatsApp shortly.</p>
  <p class="ref">Reference: {reference or "—"}</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)
