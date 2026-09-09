"""FAREBEEP AGENT - Groq + LangChain conversational brain.

Replaces the Gemini intent tree when GROQ_API_KEY is set (see
main._handle_incoming_message). Design notes:

  Tools   - LangChain StructuredTools over the existing sync engines:
            search_fares (Shared Ledger first, Travels247 live on miss),
            reserve_fare (live re-verify + 10-minute lock + Paystack link)
            and subscribe_alerts (free price-drop watch with target).
  Loop    - an explicit tool loop (max 4 steps), not framework magic:
            same behaviour, fully testable, no version roulette.
  Memory  - per-phone rolling history in chat_state.agent_history
            (DB-backed, survives restarts/replicas - RAM never holds it).
  Async   - the worker threads running this module are sync, so the
            async Travels247 client runs via asyncio.run() (no live event
            loop here - _run_async raises loudly otherwise).

Quoting also writes chat_state.last_fare, so a bare "BOOK" afterwards
still locks exactly what the agent quoted (the pick/requote gates in
main.py keep working unchanged).
"""
import asyncio
import json
import logging
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from FareBeep import chatstate
from FareBeep.config import GROQ_API_KEY, GROQ_MODEL
from FareBeep.iata import resolve_iata
from FareBeep.models import User, utcnow
from FareBeep.search import LedgerOnlyEngine, LedgerSearch
from FareBeep.travels247 import Travels247Client, pick_cheapest
from FareBeep.transactions import BookingService, PaystackError

logger = logging.getLogger("farebeep.agent")

MAX_STEPS = 4
FALLBACK = ("Sorry, that took too long on my side - please send your "
            "route again (e.g. Lagos to Abuja tomorrow).")
TROUBLE = ("Sorry - I'm having trouble reaching live fares right now. "
           "Please try again in a bit.")

SYSTEM_PROMPT = """ROLE
You are FareBeep, a flight-booking assistant for NIGERIAN DOMESTIC
travel on WhatsApp and Telegram. You chat like a sharp, warm travel
agent friend: small Naija flavor is fine, never robotic, never a
lecture. Every message does at most two things: acknowledge what the
user just said (one short line), then move things forward with at
most ONE question. You never invent, guess, or round fares. Every
price you quote comes from search_fares, word for word.

MEMORY
The conversation so far is included above - you remember it. Never
ask for anything the user already told you in this chat (route,
date, or name). If they correct you ("I am in Abuja" after you said
Lagos), take the correction gracefully and carry on - never argue,
never re-ask what they just fixed.

GREETING
When the message is just a greeting (hi, hello, hey, good morning)
with no request attached: greet them BACK first. If you know their
name, use it. One warm line saying who you are and what you do
("Hey! I'm FareBeep - I find cheap Nigerian flights and lock fares
for you."), then one open question ("Where are you flying to?").
Never answer a greeting with a bare question and nothing else.
If the history shows an UNFINISHED thread (a fare you offered that
they never answered, a question they never replied to): greet
briefly, then resume it in the SAME message ("Hey! Still on Abuja
to Port Harcourt tomorrow - want me to lock that fare?"). Never
re-ask what the thread already holds, and never pitch a NEW search
unprompted.

WHAT YOU CAN DO
1. Search one-way domestic flights (search_fares).
2. Lock a fare for 10 minutes and take payment via Paystack
   (reserve_fare -> payment link).
3. Watch a route for price drops, free (subscribe_alerts -> beep).
That's it. You cannot do round trips, international flights, or
flight status. If asked, say so plainly in one line and offer what
you CAN do.

AIRPORTS YOU COVER (IATA in brackets)
Lagos LOS (Eko), Abuja ABV (Abj), Port Harcourt PHC (PH), Kano KAN,
Enugu ENU, Calabar CBQ, Uyo QUO, Owerri QOW, Asaba ABB, Benin BNI,
Warri QRW, Ilorin ILR, Ibadan IBA, Akure AKR, Jos JOS, Kaduna KAD,
Sokoto SKO, Maiduguri MIU, Yola YOL.
Slang map: Abj=Abuja, Eko=Lagos, PH=Port Harcourt. If a user names a
city outside this list or abroad, say you only fly domestic Nigeria
and ask for a domestic route. NEVER assume the origin city - not even
Lagos. "Flight to Abuja" with no origin means you ASK where from,
offering Lagos only as a guess to confirm ("Leaving from Lagos too,
or somewhere else?") - never stating it as fact.

DATES
Convert relative dates yourself using the Today line in your header
and CONFIRM the result - never re-ask a date the user already gave.
"Tomorrow" (or "next tomorrow") means tomorrow's date: say it back
("Got it - Lagos to Enugu tomorrow, Sunday 7 September?") instead of
asking "which exact date?". Same for "next Friday", "on the 21st".
Only ask about the date when the user gave none at all, or two
readings genuinely clash. Never assume today.

TOOL 1: search_fares
Call it the moment you have origin + destination - the date is
OPTIONAL, never a blocker. No date given? Call it WITHOUT
flight_date: you instantly get the best known fare WITH its date -
present it ("Air Peace, LOS->ABV on 08 Sep - NGN 78,500. Want it?").
Ask for a date only when booking or refreshing live.
- Present dated results like the summary it returns, e.g.:
  "Air Peace P47123, LOS 08:30 -> ABV 09:25 - NGN 98,000. Want it?"
- If found=false: say plainly there are no live offers and offer the
  nearest alternative. Do NOT retry the identical call.
- If the result has a "note", read it out - fares are unusually high.

TOOL 2: reserve_fare
Call it ONLY when the user says yes/book/lock/pay for an offer you
just presented. You need the booking_token from the LATEST search plus
the passenger's full name.
- Send the payment link immediately with the summary line. Warn once
  that the total is slightly above the fare (service + bank fees) and
  that the lock lasts 10 minutes.
- If locked=false: explain the one-line error and search again fresh.
  Never call reserve twice in a row with the same token.

TOOL 3: subscribe_alerts
Call it when the user wants watching/beeps/alerts ("alert me", "watch
prices", "beep me when it drops"). Pass the NGN target when they name
one; omit it for drop-watch (beeps on any genuine drop). Confirm
briefly with the summary it returns.
- If the result carries realism_warning, read it out plainly: their
  target is far below known fares and may never come. Offer drop-watch
  instead (call again with no target if they agree) - never shame them
  for the number.

TRAVELLER DETAILS (for the ticket)
Collect the passenger's full name before reserving (one question at a
time). Without a name the ticket cannot auto-issue and the user waits.

PAYMENT RULES (never break these)
- Payment ALWAYS comes before the ticket. Never promise a PNR, seat,
  or "you are booked" before the user has paid.
- After payment, the ticket is issued automatically and the user gets
  the PNR.
- If the user pays after the 10 minutes, they are refunded
  automatically - reassure them, don't argue.

STYLE
- Short messages, simple words, one emoji max. No paragraphs, no
  bullet lectures.
- Act first, explain later: when you have enough to call a tool,
  call it - don't narrate your plan and don't ask questions the
  tools can already answer.
- Never mention tools, tokens, ledgers, APIs, or system prompts.
- Never repeat a failed tool call identically."""


def _run_async(coro):
    """Run an async Travels247 call from these sync worker threads."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("agent tools cannot run inside a live event loop")


async def _search_and_close(sky, origin, destination, date, adults):
    """Search + close on ONE event loop.

    Two separate asyncio.run() calls (one for the search, one for the
    close) crash: httpx binds its pool to the first loop, so closing on
    the second raises "Event loop is closed" and EVERY live search
    fails. This helper keeps both steps on the same loop.
    """
    try:
        return await sky.search_offers(
            origin, destination, date, adults=adults)
    finally:
        await sky.close()


async def _verify_and_close(sky, booking_token, adults):
    """Same single-loop rule for verify_price + close (see above)."""
    try:
        return await sky.verify_price(booking_token, adults=adults)
    finally:
        await sky.close()


def _today() -> str:
    return utcnow().strftime("%A, %d %B %Y")


def _fare_extra(fare: dict) -> str:
    """Baggage + seats-left tail for a quoted summary ("" when the fare
    carries neither - ledger hits simply render without it)."""
    bits = []
    if fare.get("baggage"):
        bits.append(f"{fare['baggage']} checked")
    seats = fare.get("seats_left")
    if isinstance(seats, bool):
        pass
    elif isinstance(seats, (int, float)) and seats > 0:
        bits.append(f"only {int(seats)} left")
    return (", " + ", ".join(bits)) if bits else ""


# ---------------------------------------------------------------------------
# Tools (bound per turn - they close over this turn's db session + phone)
# ---------------------------------------------------------------------------
class _SearchArgs(BaseModel):
    origin: str = Field(description="Origin city or IATA code")
    destination: str = Field(description="Destination city or IATA code")
    flight_date: str = Field(
        default="",
        description=("Travel date as YYYY-MM-DD. Omit when the user gave "
                     "no date - the tool answers from the best known fare."))
    adults: int = Field(default=1, description="Adult passengers")


class _SubscribeArgs(BaseModel):
    origin: str = Field(description="Origin city or IATA code")
    destination: str = Field(description="Destination city or IATA code")
    target_price: Optional[float] = Field(
        default=None,
        description=("NGN target: beep when the fare hits this or lower. "
                     "Omit for drop-watch (beep on any genuine drop)."))
    flight_date: str = Field(
        default="",
        description=("Travel date as YYYY-MM-DD. Omit for a rolling "
                     "window watch."))


class _ReserveArgs(BaseModel):
    booking_token: str = Field(description="booking_token from the latest search")
    origin: str = Field(description="Origin IATA code from that search")
    destination: str = Field(description="Destination IATA code from that search")
    flight_date: str = Field(description="Travel date as YYYY-MM-DD")
    passenger_name: str = Field(description="Full name for the ticket")
    adults: int = Field(default=1, description="Adult passengers")
    payment_method: Optional[str] = Field(
        default=None, description="'card' or 'bank_transfer' if known")


def build_tools(db: Session, phone: str) -> list:
    """search_fares + reserve_fare + subscribe_alerts for one turn."""

    def search_fares(origin: str, destination: str, flight_date: str = "",
                     adults: int = 1) -> str:
        o = resolve_iata(origin or "")
        d = resolve_iata(destination or "")
        date = (flight_date or "")[:10]
        if not o or not d:
            return json.dumps({"found": False, "error":
                               "need origin and destination"})
        ledger = LedgerSearch(db, live=LedgerOnlyEngine())
        if not date:
            # No date: answer INSTANTLY from the best known fare - no live
            # calls, no interrogation. The date comes later, only for a
            # live refresh or a booking.
            best = ledger.window_cheapest(o, d)
            if best is None:
                return json.dumps({
                    "found": False, "source": "ledger",
                    "error": (f"no known fares for {o}->{d} yet - ask for "
                              f"a travel date to check live")})
            chatstate.set_last_fare(db, phone, {
                "origin_iata": o, "destination_iata": d,
                "flight_date": best["flight_date"], "price": best["price"],
                "airline": best.get("airline")})
            return json.dumps({
                "found": True, "source": "ledger",
                "summary": (f"{best.get('airline') or 'Airline'}, "
                            f"{o}->{d} on {best['flight_date']} - "
                            f"NGN {best['price']:,.0f}"
                            f"{_fare_extra(best)}. Want it?"),
                "price_ngn": best["price"],
                "flight_date": best["flight_date"],
                "note": ("Prices are unusually high right now."
                         if best.get("above_guardrail") else None)})
        hit = ledger.search(o, d, date)
        if hit is not None:
            chatstate.set_last_fare(db, phone, {
                "origin_iata": o, "destination_iata": d,
                "flight_date": date, "price": hit["price"],
                "airline": hit.get("airline")})
            return json.dumps({
                "found": True, "source": "ledger",
                "summary": (f"{hit.get('airline') or 'Airline'}, "
                            f"{o}->{d} on {date} - "
                            f"NGN {hit['price']:,.0f}"
                            f"{_fare_extra(hit)}. Want it?"),
                "price_ngn": hit["price"],
                "note": ("Prices are unusually high right now."
                         if hit.get("above_guardrail") else None)})
        sky = Travels247Client()
        offers = _run_async(_search_and_close(
            sky, o, d, date, max(adults, 1)))
        best = pick_cheapest(offers)
        if best is None:
            return json.dumps({"found": False, "source": "247travels",
                               "error": f"no live offers for {o}->{d} "
                                        f"on {date}"})
        # Cache sane prices only: surge skipped, no 90s hold on chat -
        # the BOOK handshake re-verifies before money moves.
        ledger._verify_and_upsert(o, d, date, {
            "price": best["price"], "currency": best["currency"],
            "airline": best["airline_name"], "verify_link": None},
            verify=False)
        chatstate.set_last_fare(db, phone, {
            "origin_iata": o, "destination_iata": d,
            "flight_date": date, "price": best["price"],
            "airline": best["airline_name"]})
        return json.dumps({
            "found": True, "source": "247travels",
            "summary": (f"{best['airline_name']} {best.get('flight_no') or ''}, "
                        f"{best.get('departure_code') or o} "
                        f"{best.get('departure_time') or ''}->"
                        f"{best.get('arrival_code') or d} "
                        f"{best.get('arrival_time') or ''} - "
                        f"NGN {best['price']:,.0f}"
                        f"{_fare_extra(best)}. Want it?"),
            "price_ngn": best["price"],
            "booking_token": best["booking_token"]})

    def reserve_fare(booking_token: str, origin: str, destination: str,
                     flight_date: str, passenger_name: str, adults: int = 1,
                     payment_method: str = None) -> str:
        o = resolve_iata(origin or "")
        d = resolve_iata(destination or "")
        date = (flight_date or "")[:10]
        name = (passenger_name or "").strip()
        if not booking_token or not o or not d or not date:
            return json.dumps({"locked": False, "error":
                               "need booking_token, origin, destination, "
                               "flight_date"})
        if not name:
            return json.dumps({"locked": False, "error":
                               "need the passenger's full name for "
                               "the ticket"})
        user = db.query(User).filter(User.phone == phone).first()
        if user is None:
            return json.dumps({"locked": False,
                               "error": "unknown user - say hello first"})
        sky = Travels247Client()
        try:
            priced = _run_async(_verify_and_close(
                sky, booking_token, max(adults, 1)))
        except Exception as e:
            return json.dumps({"locked": False,
                               "error": f"live price check failed: {e}"})
        parts = name.split()
        travellers = {"primary_guest": {
            "first_name": parts[0],
            "last_name": " ".join(parts[1:]) or parts[0],
            "phone": phone}}
        try:
            result = BookingService(db).create_booking(
                user.user_id, o, d, date, priced["verified_price"],
                airline=priced.get("airline"), source="247travels",
                payment_method=payment_method)
        except PaystackError as e:
            return json.dumps({"locked": False,
                               "error": f"payment link failed: {e}"})
        session = result["session"]
        details = dict(session.flight_details or {})
        details.update({
            "booking_token": priced["booking_token"],
            "passengers": {"adults": max(adults, 1), "children": 0,
                           "infants": 0},
            "travellers": travellers})
        session.flight_details = details
        db.commit()
        total = result["total_amount"]
        return json.dumps({
            "locked": True, "payment_link": result["payment_link"],
            "total_amount": total, "payment_ref": session.payment_ref,
            "summary": (f"Locked {o}->{d} {date} at NGN {total:,.0f}. "
                        f"Pay within 10 minutes: {result['payment_link']}")})

    def subscribe_alerts(origin: str, destination: str,
                         target_price: float = None,
                         flight_date: str = "") -> str:
        from FareBeep.alerts import SubscriptionMonitor, target_realism_note
        o = resolve_iata(origin or "")
        d = resolve_iata(destination or "")
        date = (flight_date or "")[:10] or None
        if not o or not d:
            return json.dumps({"subscribed": False, "error":
                               "need origin and destination"})
        user = db.query(User).filter(User.phone == phone).first()
        if user is None:
            return json.dumps({"subscribed": False,
                               "error": "unknown user - say hello first"})
        tp = None
        if target_price is not None:
            try:
                tp = float(target_price)
            except (ValueError, TypeError):
                tp = None
        SubscriptionMonitor(db).subscribe(
            user.user_id, o, d, target_price=tp, target_date=date)
        date_label = date or "the coming weeks"
        if tp is not None:
            summary = (f"Watching {o}->{d} ({date_label}) - beep at "
                       f"NGN {tp:,.0f} or lower.")
        else:
            summary = (f"Watching {o}->{d} ({date_label}) - beep on any "
                       f"genuine drop.")
        return json.dumps({
            "subscribed": True, "origin": o, "destination": d,
            "target_price": tp, "date_label": date_label,
            "summary": summary,
            "realism_warning": target_realism_note(db, o, d, tp)})

    return [
        StructuredTool.from_function(
            func=search_fares, name="search_fares",
            description=("Search live Nigerian domestic fares. Call the "
                         "moment you have origin + destination + date. "
                         "Returns a summary to read out plus booking_token."),
            args_schema=_SearchArgs),
        StructuredTool.from_function(
            func=reserve_fare, name="reserve_fare",
            description=("Lock a presented offer for 10 minutes and get the "
                         "Paystack link. Call ONLY on yes/book/pay, with the "
                         "LATEST booking_token and the passenger's name."),
            args_schema=_ReserveArgs),
        StructuredTool.from_function(
            func=subscribe_alerts, name="subscribe_alerts",
            description=("Set a free price-drop alert. Call when the user "
                         "wants watching/beeps/alerts for a route, with an "
                         "NGN target when given, without one for drop-watch. "
                         "If the result carries realism_warning, read it "
                         "out plainly."),
            args_schema=_SubscribeArgs),
    ]


# ---------------------------------------------------------------------------
# One conversational turn
# ---------------------------------------------------------------------------
def agent_reply(db: Session, user, text: str, llm=None,
                max_steps: int = MAX_STEPS) -> str:
    """Run one Groq + LangChain turn: history + prompt -> tools -> reply.

    The reply is already human-ready (the caller sends it verbatim - no
    second personality pass). The turn is recorded to chat_state.
    """
    if llm is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not set - add it to "
                               "FareBeep/.env to switch the agent on")
        llm = ChatGroq(model=GROQ_MODEL, api_key=GROQ_API_KEY,
                       temperature=0.3)
    tools = build_tools(db, user.phone)
    bound = llm.bind_tools(tools)
    header = (f"Today is {_today()}. The user's name is "
              f"{user.name or 'unknown'} and their number is {user.phone}.\n\n")
    messages = [SystemMessage(content=header + SYSTEM_PROMPT)]
    for h in chatstate.get_agent_history(db, user.phone)[-12:]:
        if h.get("role") == "assistant":
            messages.append(AIMessage(content=h.get("content", "")))
        else:
            messages.append(HumanMessage(content=h.get("content", "")))
    messages.append(HumanMessage(content=text))

    by_name = {t.name: t for t in tools}
    reply = FALLBACK
    saw_error = False
    for _ in range(max_steps):
        ai = bound.invoke(messages)
        messages.append(ai)
        calls = ai.tool_calls or []
        if not calls:
            reply = ai.content or FALLBACK
            break
        for tc in calls:
            tool = by_name.get(tc["name"])
            if tool is None:
                result = f"ERROR: unknown tool {tc['name']}"
            else:
                try:
                    result = tool.invoke(tc["args"])
                except Exception as e:
                    logger.warning("agent tool %s failed: %s", tc["name"], e)
                    result = f"ERROR: {e}"
                    saw_error = True
            messages.append(ToolMessage(content=str(result),
                                        tool_call_id=tc["id"]))
    if reply is FALLBACK and saw_error:
        # Every round died in a backend error (never the user's fault):
        # say that, don't blame their message.
        reply = TROUBLE
    chatstate.append_agent_history(db, user.phone, text, reply)
    return reply


__all__ = ["agent_reply", "build_tools", "SYSTEM_PROMPT", "FALLBACK"]
