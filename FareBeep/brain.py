"""THE CONVERSATIONAL LAYER - Gemini intent parsing (flash-class model).

Efficiency rule (per the reconstruction brief): Gemini is told to be CONCISE.
No conversational filler. Just the data. We post back over Meta directly via
main.py - the brain only extracts {intent, origin, destination, date}.

IATA correctness rule: Gemini's raw city extraction is re-mapped through the
local dictionary in `iata.py`. The LLM never decides the final IATA code.

NOTE: the brief named "Gemini 1.5 Flash" but that model line is retired
(confirmed live: 404 on generateContent, and via the models list API). A
current flash model is used instead - same cheap/fast/concise role, set in
.env as GEMINI_MODEL.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from FareBeep.config import GEMINI_API_KEY, GEMINI_MODEL
from FareBeep.iata import CITY_TO_IATA, resolve_iata

logger = logging.getLogger("farebeep.brain")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = """You are the intent router for a Nigerian flight utility.
The user writes a WhatsApp message. Extract ONE JSON object.

Fields (ALL required; use null when unknown):
- "intent": one of "fare", "book", "status", "track", "subscribe", "unsubscribe", "help"
- "origin": the ORIGIN city/airport exactly as the user wrote it, or null
- "destination": the DESTINATION city/airport exactly as written, or null
- "date": departure date as "YYYY-MM-DD" (resolve "tomorrow", "next week",
  "next tuesday", "in August" against today), or null
- "target_price": for "subscribe" only - the numeric NGN price threshold
  the user wants to be alerted under (e.g. "below 80000" -> 80000), or null
- "flight": flight number like "P47123" when the user names a specific flight
- "name": the user's name ONLY if they introduce themselves ("my name is
  Damilola", "I'm Tunde", "call me Bola"), as written, else null

WHAT EACH INTENT MEANS - apply these definitions first:
- "fare" = asks the PRICE or availability of a route ("how much from Lagos to
  Abuja", "cheapest flight to Enugu", "Lagos Abuja tomorrow", "is there a
  flight to Abuja on Friday"). Route is optional - a bare destination with a
  date is still "fare".
- "book" = wants to BUY or reserve or pay ("book a flight", "BOOK", "book
  P47123", "I want to pay for my booking", "reserve it").
- "status" = asks about an EXISTING booking or flight: "my booking", "check
  my booking", "flight status", "where is my flight", "when is my flight",
  "is my flight on time", "track my booking", "track my flight", or a flight
  number with track/status ("track P47123", "status of P47123").
- "subscribe" = wants a price-drop ALERT on a route ("track Lagos to Abuja",
  "alert me when it drops below 80000", "notify me", "watch Abuja to Enugu",
  "beep me", "subscribe LOS ABV 60000"). Include "target_price" if they name
  one; extract the date too if given.
- "unsubscribe" = wants alerts STOPPED ("unsubscribe", "stop alerts", "I
  don't want alerts anymore", "no more alerts", "opt out", "cancel my
  alerts", "stop" alone).
- "help" = greeting, small talk, thanks, "ok", "good", or anything
  unrelated to flights.

RULES:
- PASS 1 IS EXTRACTION ONLY: origin and destination are independent. NEVER
  invent one from the other. "I'm going to Lagos" -> destination "Lagos",
  origin null, date null. Record city names as WRITTEN, no IATA codes.
- DECISION ORDER when several match: unsubscribe > status > subscribe >
  book > fare > help. "Check my booking" is status, NEVER book.
- "track" is ambiguous - resolve by CONTEXT: with a flight number -> status;
  with a route -> subscribe; with "booking"/"flight" and no route -> status.
- "booking" as a NOUN ("my booking") = existing booking -> status. "book"
  as a VERB = the buy action -> book.
- "how much" / price questions are fare UNLESS the user wants to be alerted
  ("notify me", "alert me", "let me know when") -> then subscribe.
- Today's date is {{today}}. Resolve "tomorrow", "next week", "next
  tuesday", "in August" against it.
- CONCISE: output ONLY the JSON object. No preamble, no prose, no markdown
  fences. Nothing else.

Examples - match the pattern, not the words:
User: "how much is Lagos to Abuja tomorrow"
  -> {"intent":"fare","origin":"Lagos","destination":"Abuja","date":"<resolved>","target_price":null,"flight":null,"name":null}
User: "check my booking"
  -> {"intent":"status","origin":null,"destination":null,"date":null,"target_price":null,"flight":null,"name":null}
User: "track P47123"
  -> {"intent":"status","origin":null,"destination":null,"date":null,"target_price":null,"flight":"P47123","name":null}
User: "alert me when Lagos to Abuja drops below 80000"
  -> {"intent":"subscribe","origin":"Lagos","destination":"Abuja","date":null,"target_price":80000,"flight":null,"name":null}
User: "I don't want alerts anymore"
  -> {"intent":"unsubscribe","origin":null,"destination":null,"date":null,"target_price":null,"flight":null,"name":null}
User: "BOOK"
  -> {"intent":"book","origin":null,"destination":null,"date":null,"target_price":null,"flight":null,"name":null}
User: "I'm Tunde"
  -> {"intent":"help","origin":null,"destination":null,"date":null,"target_price":null,"flight":null,"name":"Tunde"}
User: "thanks"
  -> {"intent":"help","origin":null,"destination":null,"date":null,"target_price":null,"flight":null,"name":null}"""


@dataclass
class Intent:
    intent: str = "help"
    origin: Optional[str] = None
    destination: Optional[str] = None
    date: Optional[str] = None          # "YYYY-MM-DD"
    target_price: Optional[float] = None
    flight: Optional[str] = None
    name: Optional[str] = None          # user name if they introduced themselves
    raw_text: str = ""

    @property
    def origin_iata(self) -> Optional[str]:
        return resolve_iata(self.origin)

    @property
    def destination_iata(self) -> Optional[str]:
        return resolve_iata(self.destination)

    @property
    def has_route(self) -> bool:
        return bool(self.origin_iata and self.destination_iata)

    @property
    def is_partial(self) -> bool:
        """Pass 2 signal: some route info but not enough to search - the
        concierge must ask for the missing pieces instead of erroring."""
        return bool(self.origin_iata or self.destination_iata) and not self.has_route

    def as_dict(self) -> dict:
        return {
            "intent": self.intent,
            "origin": self.origin_iata,
            "destination": self.destination_iata,
            "date": self.date,
            "target_price": self.target_price,
            "flight": self.flight,
            "name": self.name,
        }


VALID_INTENTS = {"fare", "book", "status", "track",
                 "subscribe", "unsubscribe", "help"}


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def parse_intent(text: str, api_key: str = None, model: str = None,
                 http_client: Optional[httpx.Client] = None) -> Intent:
    """TWO-PASS BRAIN.

    Pass 1 (Extraction): Gemini (or the local parser when offline) converts
    the message into structured data - origin/destination/date/name may be
    independently null. Pass 2 (Concierge Logic) lives in main.py: incomplete
    intents get a warm follow-up question; complete ones go to the engine.

    Never raises.
    """
    api_key = api_key or GEMINI_API_KEY
    model = model or GEMINI_MODEL

    if not api_key:
        logger.debug("GEMINI_API_KEY not set - brain uses the local parser")
        intent = _local_parse(text) or Intent(raw_text=text)
        intent.name = intent.name or _local_name(text)
        return intent

    prompt = SYSTEM_PROMPT.replace("{{today}}", _today())
    payload = {
        "contents": [
            {"parts": [{"text": prompt + "\n\nUser message: " + text}]}
        ],
        "generationConfig": {
            "temperature": 0.0,
            # This model line "thinks" internally (measured ~650 tokens) before
            # emitting the tiny JSON - the cap must leave room for both.
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    url = f"{GEMINI_BASE}/models/{model}:generateContent?key={api_key}"
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        for attempt in (0, 1, 2):
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["candidates"][0]["content"]["parts"][0]["text"]
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))   # rate-limit: back off, retry
                    continue
                logger.warning("Gemini intent parse failed (%s) - local parser", e)
                content = None
                break
            except Exception as e:
                logger.warning("Gemini intent parse failed (%s) - local parser", e)
                content = None
                break
    finally:
        if own_client:
            client.close()

    if content is None:
        intent = _local_parse(text) or Intent(raw_text=text)
        intent.name = intent.name or _local_name(text)
        return intent

    parsed = _build_intent(content, text) or _local_parse(text) or Intent(raw_text=text)
    parsed.name = parsed.name or _local_name(text)
    return parsed


def _build_intent(content: str, raw: str) -> Optional[Intent]:
    try:
        data = json.loads(content.strip())
    except json.JSONDecodeError as e:
        logger.warning("Gemini returned non-JSON: %s (%s)", e, content[:160])
        return None

    intent_name = str(data.get("intent", "help")).lower() if data.get("intent") else "help"
    if intent_name not in VALID_INTENTS:
        intent_name = "help"

    date = data.get("date")
    if date is not None and isinstance(date, str):
        date = date.strip() or None

    target_price = data.get("target_price")
    if target_price not in (None, ""):
        try:
            target_price = float(str(target_price).replace(",", "").replace("NGN", "").strip())
        except ValueError:
            target_price = None

    return Intent(
        intent=intent_name,
        origin=str(data["origin"]).strip() if data.get("origin") else None,
        destination=str(data["destination"]).strip() if data.get("destination") else None,
        date=date,
        target_price=target_price,
        flight=str(data["flight"]).upper() if data.get("flight") else None,
        name=str(data["name"]).strip().title() if data.get("name") else None,
        raw_text=raw,
    )


# ---------------------------------------------------------------------------
# OFFLINE FALLBACK PARSER (no network, no Gemini)
# ---------------------------------------------------------------------------
# The "never-degrade" rule: if the LLM is slow or down, the message still
# gets a proper intent from this deterministic parser instead of a help menu.
# It only fires for clear patterns the local dictionary can resolve.

_MONTHS = {name: i + 1 for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

_WEEKDAYS = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
     "sunday"])}

_FLIGHT_RE = re.compile(r"\b[a-z]{1,2}\d{3,5}\b")
# Price is unambiguous only after a threshold word (below/under/less/max),
# with a naira sign/word, as "80k", or as a LARGE bare number (>=1000) -
# small bare numbers are dates, never prices.
_PRICE_RE = re.compile(
    r"(?:below|under|less than|max)\s*(?:ngn|n)?\s*([\d,]+(?:\.\d+)?)k?"
    r"|(?:ngn|\u20a6)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+)(?:\.\d+)?\s*k\b"
    r"|([\d,]+(?:\.\d+)?)\s*naira\b"
    r"|(?:^|\s)([\d,]+)(?:\s|$)", re.IGNORECASE)

_NAME_RE = re.compile(
    r"\b(?:my name is|i\x27?m called|i am called|call me)\s+([a-z]{2,})\b"
    r"|\bi\x27?m\s+(?!going\b|from\b|to\b|in\b|at\b|on\b|here\b|back\b|"
    r"coming\b|flying\b|travell?ing\b|leaving\b|arriving\b|looking\b|"
    r"hoping\b|planning\b|trying\b|sorry\b|happy\b|new\b|not\b|just\b)"
    r"([a-z]{2,})\b"
    r"|\bi am\s+(?!going\b|from\b|to\b|in\b|at\b|on\b|here\b|back\b|"
    r"coming\b|flying\b|travell?ing\b|leaving\b|arriving\b|looking\b|"
    r"hoping\b|planning\b|trying\b|sorry\b|happy\b|new\b|not\b|just\b)"
    r"([a-z]{2,})\b")

_WEEKDAY_RE = re.compile(
    r"\b((?:next|this|coming)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")

# Phrases about an EXISTING booking/flight -> status (checked before book).
_STATUS_RE = re.compile(
    r"\b(my booking|booking status|bookings?\b.*\bstatus|flight status|"
    r"status of|status\b|track my|tracking my|where is my flight|"
    r"when is my flight|what time is my flight|is my flight|"
    r"my flight|my booking)\b", re.IGNORECASE)

# An explicit buy verb governing the booking ("pay for my booking",
# "book my booking") is the BUY action -> book, not status.
_PAY_BOOKING_RE = re.compile(
    r"\b(pay|book|buy|reserve|proceed)\b[^.]{0,20}\bmy booking\b",
    re.IGNORECASE)

# Asking for a PRICE (fare beats status/book on these words).
_PRICE_WORD_RE = re.compile(
    r"\b(price|prices|fare|fares|cost|how much|cheap|cheapest|ticket|"
    r"tickets|quote|rate|available|availability)\b", re.IGNORECASE)

_UNSUBSCRIBE_RE = re.compile(
    r"\b(unsubscribe|stop alerts?|stop notifications|no more alerts?|"
    r"cancel (my )?alerts?|remove (my )?alerts?|turn off alerts?|"
    r"off alerts?|opt out|i don\x27?t want alerts?|i do not want alerts?|"
    r"don\x27?t text me)\b", re.IGNORECASE)

_SUBSCRIBE_RE = re.compile(
    r"\b(subscribe|alert|watch|beep|track|notif\w*|let me know when)\b",
    re.IGNORECASE)

_BOOK_RE = re.compile(r"\b(book|buy|reserve|pay|proceed)\b", re.IGNORECASE)

_GREETING_RE = re.compile(
    r"\b(hello|hi|hey|good (morning|afternoon|evening)|help|menu|options|"
    r"what can you do|start|thanks|thank you|ok|okay|"
    r"i\x27?ll think|i will think|let me think|get back to you|maybe)\b",
    re.IGNORECASE)


def _local_name(text: str) -> Optional[str]:
    """"My name is Damilola" / "call me Bola" / "I'm Tunde" -> "Tunde"."""
    m = _NAME_RE.search(text)
    if not m:
        return None
    name = next((g for g in m.groups() if g), None)
    return name.capitalize() if name else None


def _local_route(text: str) -> Optional[list]:
    """First two distinct known cities, earliest first (multi-word aliases
    resolved by longest-alias-first matching)."""
    hits = []
    for alias in sorted(CITY_TO_IATA, key=len, reverse=True):
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", text):
            hits.append((m.start(), CITY_TO_IATA[alias]))
    seen, route = set(), []
    for _, iata in sorted(hits):
        if iata not in seen:
            seen.add(iata)
            route.append(iata)
        if len(route) == 2:
            break
    return route or None


def _local_date(text: str) -> Optional[str]:
    today = date.today()
    if "day after tomorrow" in text:
        return (today + timedelta(days=2)).isoformat()
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b|\bnow\b", text):
        return today.isoformat()
    if "next week" in text:
        return (today + timedelta(days=7)).isoformat()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    # Slash/dash dates: "31/08", "31-08", "08/31" - day/month first, swapped
    # only when the first part can't be a day (>12), so US order works too.
    m = re.search(r"\b(\d{1,2})\s*[/-]\s*(\d{1,2})(?:[/-]\d{2,4})?\b", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        day, month = (b, a) if a <= 12 < b else (a, b)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        year = today.year
        try:
            if date(year, month, day) < today:
                year += 1   # 31/08 already passed -> next year
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    wd = _WEEKDAY_RE.search(text)
    if wd:
        target = _WEEKDAYS[wd.group(2)]
        if wd.group(1) and "next" in wd.group(1):
            delta = 7 + ((target - today.weekday()) % 7)   # next week's weekday
        else:
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7                                   # always future
        return (today + timedelta(days=delta)).isoformat()
    mm = re.search(r"\b(" + "|".join(_MONTHS) + r")\b", text)
    if mm:
        month = _MONTHS[mm.group(1)]
        year = today.year
        day = 1
        dm = (re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+" + mm.group(1) + r"\b", text)
              or re.search(mm.group(1) + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", text))
        if dm:
            day = int(dm.group(1))
            if (today.month, today.day) > (month, day):
                year += 1  # named date already passed -> next occurrence
        elif (today.month, today.day) > (month, 1):
            year += 1  # "in August" after Aug 1 -> next August
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    # Bare ordinal day with no month: "31st", "on the 2nd" -> this month if
    # still ahead, otherwise the same day next month ("5th" on Aug 13 -> Sep 5).
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
    if m:
        day = int(m.group(1))
        month, year = today.month, today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is None or candidate < today:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    # A BARE NUMBER is the day of the CURRENT month: "31", "the 31", "on 5"
    # -> this month if still ahead, otherwise next month. Times (10:30,
    # 10am, 9pm), years (2026) and prices (80k, 15000) never match.
    m = re.search(
        r"(?:\b(?:on|the|for)\s+)?(?<![\d:])(\d{1,2})(?![:\d])(?![a-z])\b",
        text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            return None
        month, year = today.month, today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is None or candidate < today:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _local_target_price(text: str) -> Optional[float]:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5)
    if not raw:
        return None
    try:
        price = float(raw.replace(",", ""))
    except ValueError:
        return None
    if m.group(3) is not None or "k" in m.group(0).lower():
        price *= 1000.0  # "below 80k" / "80k" -> 80000
    if m.group(5) is not None and price < 1000.0:
        return None      # a bare small number is a date ("the 5th"), not a price
    return price


def _local_parse(text: str) -> Optional[Intent]:
    """Deterministic intent extraction for offline/fallback operation.

    Decision order: unsubscribe > status > subscribe > book > fare > help.
    Mirrors the Gemini prompt's rules so online/offline never disagree.
    """
    flat = " ".join(text.strip().lower().split())
    if not flat:
        return None

    if _UNSUBSCRIBE_RE.search(flat) \
            or re.fullmatch(r"stop( it| everything)?", flat):
        return Intent(intent="unsubscribe", raw_text=text)

    fm = _FLIGHT_RE.search(flat)
    flight = fm.group(0).upper() if fm else None

    route = _local_route(flat)
    day = _local_date(flat)
    name = _local_name(flat)

    # STATUS: an existing booking/flight - "check my booking", "where is my
    # flight", "track my booking", "track P47123", bare "status". Price
    # questions ("how much is my flight") are fare, not status.
    if (flight and re.search(r"\b(track|status)\b", flat)) or (
            _STATUS_RE.search(flat) and not _PRICE_WORD_RE.search(flat)) \
            and not _PAY_BOOKING_RE.search(flat):
        return Intent(intent="status", flight=flight, raw_text=text)

    target = _local_target_price(flat)
    if _SUBSCRIBE_RE.search(flat) and (len(route or []) >= 2
                                       or target is not None
                                       or re.fullmatch(
                                           r"(subscribe|alert|watch|beep|track|notify)( it)?",
                                           flat)):
        # bare "TRACK" / "beep" after a fare quote -> subscribe intent;
        # Pass 2 (main.py) fills the route from the user's last fare search.
        return Intent(intent="subscribe",
                      origin=route[0] if route else None,
                      destination=route[1] if len(route or []) > 1 else None,
                      date=day, target_price=target, name=name, raw_text=text)

    if _BOOK_RE.search(flat):
        # "BOOK" alone, "book lagos to abuja tomorrow" - the route and date
        # are optional here: Pass 2 (main.py) fills them from the user's
        # last fare search when the user just says BOOK after a quote.
        return Intent(intent="book",
                      origin=route[0] if len(route or []) > 1 else None,
                      destination=route[1] if len(route or []) > 1 else None,
                      date=day, flight=flight, name=name, raw_text=text)

    if route:
        # PASS 1 EXTRACTION: partial routes stay partial ("I'm going to
        # Abuja" = destination only). Pass 2 (main.py) asks for what's
        # missing or defaults the origin - never invent data here.
        # fare needs a route word (to/from/fly/flight) OR a price word
        # ("Lagos Abuja price", "how much to Abuja").
        if re.search(r"\b(to|from|fly|flight)\b", flat) \
                or _PRICE_WORD_RE.search(flat):
            return Intent(intent="fare",
                          origin=route[0] if len(route) > 1 else None,
                          destination=route[1] if len(route) > 1 else route[0],
                          date=day, name=name, raw_text=text)

    if _GREETING_RE.search(flat) or name:
        return Intent(intent="help", name=name, raw_text=text)
    return None


# ---------------------------------------------------------------------------
# PASS 2 B - PERSONALITY PASS - the replies feel human, not like a script
# ---------------------------------------------------------------------------
REPLY_PROMPT = """You are a friendly Nigerian Travel Concierge named FareBeep.
Below is a system-generated reply. Rewrite it as a warm, natural WhatsApp
message with these rules:

- ALWAYS open with the greeting {greeting} - nothing before it.
- When presenting flight data: mention the airline and the price clearly,
  and add a helpful tip (e.g. "This is the best deal for Friday!").
- When the reply warns about high/surge prices, keep the warning tone and
  the TRACK offer clear.
- MAX 3 lines. Keep EVERY number, NGN/₦ amount, link and date EXACTLY as
  given (do not round or change prices).
- No markdown. Keep any emoji already present; add none beyond the greeting.
- If it's a help menu, greet briefly, say what you do, and give 2-3 spoken
  examples (not bullet lists).

Output ONLY the final message text, nothing else."""


def _greeting(user_name: str = None) -> str:
    """The concierge opener - ALWAYS present, even on the no-AI fallback."""
    name = (user_name or "").strip().title()
    return f"Hi {name}! 😊" if name else "Beep! 🎫"


def _warm_fallback(template: str, greeting: str) -> str:
    """What the user gets when Gemini is down: greeting + the full template.
    The concierge wrapper is guaranteed - tone never degrades with the LLM."""
    if not template:
        return greeting
    if template.startswith(greeting):
        return template
    return f"{greeting} {template}"


def compose_reply(template: str, user_name: str = None, api_key: str = None,
                  model: str = None,
                  http_client: Optional[httpx.Client] = None) -> str:
    """Humanize a system reply via Gemini. On ANY failure returns a WARM
    FALLBACK (greeting + template) - replies never break because the AI was
    slow, and the concierge greeting never disappears. Retries once on 429
    (rate-limit) before falling back."""
    api_key = api_key or GEMINI_API_KEY
    model = model or GEMINI_MODEL
    greeting = _greeting(user_name)
    if not api_key or not template:
        return _warm_fallback(template, greeting)

    prompt = REPLY_PROMPT.format(greeting=greeting)

    payload = {
        "contents": [{"parts": [{"text": prompt + "\n\nSystem reply:\n" + template}]}],
        # gemini-flash-latest thinks internally and the thinking budget counts
        # against maxOutputTokens (measured: 300-cap truncated mid-sentence).
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096},
    }
    url = f"{GEMINI_BASE}/models/{model}:generateContent?key={api_key}"
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        for attempt in (0, 1, 2):
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text or _warm_fallback(template, greeting)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))   # rate-limit: back off, retry
                    continue
                logger.warning("Gemini reply compose failed: %s", e)
                return _warm_fallback(template, greeting)
            except Exception as e:
                logger.warning("Gemini reply compose failed: %s", e)
                return _warm_fallback(template, greeting)
        return _warm_fallback(template, greeting)
    finally:
        if own_client:
            client.close()


# ---------------------------------------------------------------------------
# PASS 2 C - RANKED FARE LIST - AI narration + natural-language picking
# ---------------------------------------------------------------------------
# The ranked list is where the "feel like talking to a person" promise is won
# or lost. Gemini NARRATES the options (context line + a recommendation, all
# numbers/prices exact) and UNDERSTANDS the user's pick ("the second one",
# "Air Peace", "the 7am flight", "the cheapest"). Deterministic templates and
# the local resolver are the never-degrade fallbacks so the flow works with
# the LLM down - the same contract as the intent parser.

RANKED_REPLY_PROMPT = """You are FareBeep, a friendly Nigerian Travel Concierge.
A ranked list of {origin} to {destination} flights on {date} was found. The
options are given as JSON, each with a number (1-based), airline, departs_at
and price in naira. Present them as a warm, natural WhatsApp message.

Rules:
- Open with the greeting {greeting} - nothing before it.
- You may add ONE short intro line (e.g. "Good options for that day!") and
  ONE short closing recommendation (e.g. "Option 2 - Air Peace at 118,500 is
  the best value for a morning flight."), but keep it tight.
- Keep EVERY option's number, airline, departure time and price EXACTLY as
  given. Never invent a fare, a price or a time.
- Make it easy to answer: keep the options clearly numbered (1., 2., 3.).
- MAX 4 body lines. No markdown. No bullet symbols.
- End by inviting them to reply with the number they want.
Output ONLY the final message text."""


PICK_PROMPT = """You resolve which numbered flight option a user is choosing.
A concierge showed the user a ranked list; each option has a number (1-based),
airline, departure time and price.

Options (JSON):
{options}

User reply:
{text}

Rules:
- Output ONLY a number if the reply is clearly CHOOSING one option: "the
  second one", "Air Peace", "the 7am flight", "the cheapest", "that one".
- Output 0 for anything that is NOT a clear choice: questions ("is that the
  cheapest?", "are they the same airline?"), small talk, or a different
  request.
- If the user names an airline, time or price, map it to the matching option
  number. If nothing matches, output 0.
Output ONLY the number, nothing else."""


_ORDINAL_TO_N = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
}

# SECOND BRAIN TOGGLE - OFF for now.
# The local resolver below is a hand-written second brain for picking. A real
# product (and this pre-seed) should NOT maintain a second implementation of
# what the LLM does: when Gemini is down/unclear we answer "which one?"
# gracefully instead. Flip to True to restore the offline guesser (useful
# only if you ever want to run the pick flow with no Gemini key).
_ENABLE_LOCAL_PICK_FALLBACK = False

_AIRLINE_STOP_TOKENS = {"air", "airline", "airlines", "airways", "flight",
                        "flights", "fly", "aviation"}


def _pick_time_variants(departs_at: str) -> list:
    """'07:10' -> ['07:10', '7:10', '0710', '07:10am', '7:10am', '7am']."""
    if not departs_at:
        return []
    m = re.match(r"(\d{1,2}):(\d{2})", str(departs_at).strip())
    if not m:
        return []
    hh, mm = int(m.group(1)), m.group(2)
    hour12 = hh % 12 or 12
    suffix = "pm" if hh >= 12 else "am"
    variants = [f"{hh:02d}:{mm}", f"{hh}:{mm}", f"{hh:02d}{mm}", f"{hh}{mm}"]
    variants += [f"{hh}:{mm}{suffix}", f"{hh:02d}:{mm}{suffix}",
                 f"{hour12}{suffix}"]
    return variants


def _local_resolve_pick(text: str, fares: list) -> Optional[int]:
    """Deterministic natural-language pick resolution (offline fallback).

    'the second one' / 'Air Peace' / 'the 7am flight' / 'the cheapest' ->
    the 1-based index. Returns None when no option is clearly chosen (a
    question, small talk, or conflicting signals). A QUESTION ("is that the
    cheapest?", "are they the same airline?") is never a pick unless it
    explicitly selects one ("Can I get the second one?")."""
    flat = " ".join(text.strip().lower().split())
    if not flat:
        return None
    is_question = "?" in flat or bool(re.match(
        r"^(is|are|was|were|do|does|did|can|could|will|would|should|what|"
        r"which|how|when|where|why|who)\b", flat))
    if is_question and not re.search(
            r"\b(get|want|take|have|book|pick|choose|select|grab|go with)\b",
            flat):
        return None
    n = len(fares)
    candidates = set()

    ordinal = re.search(
        r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b",
        flat)
    if ordinal:
        candidates.add(_ORDINAL_TO_N[ordinal.group(1)])
    if re.search(r"\blast\b", flat):
        candidates.add(n)
    if re.search(r"\b(cheapest|cheap(er)?|best deal|best value|best)\b", flat):
        candidates.add(1)

    for i, f in enumerate(fares, start=1):
        airline = (f.get("airline") or "").lower()
        tokens = [t for t in airline.split()
                  if t not in _AIRLINE_STOP_TOKENS and len(t) >= 4]
        if any(tok in flat for tok in tokens):
            candidates.add(i)
        for v in _pick_time_variants(f.get("departs_at")):
            if v in flat:
                candidates.add(i)
        fnum = (f.get("flight_number") or "").upper()
        if fnum and fnum in text.upper():
            candidates.add(i)

    if len(candidates) == 1:
        idx = candidates.pop()
        return idx if 1 <= idx <= n else None
    return None


def resolve_pick(text: str, fares: list, api_key: str = None,
                 model: str = None,
                 http_client: Optional[httpx.Client] = None) -> Optional[int]:
    """Which numbered option is the user choosing? Returns the 1-based index
    or None (nothing clearly chosen - a question, small talk, or a pick that
    doesn't match any option). Gemini decides; the deterministic local
    resolver is the offline fallback. Never raises."""
    api_key = api_key or GEMINI_API_KEY
    model = model or GEMINI_MODEL
    if not api_key:
        if not _ENABLE_LOCAL_PICK_FALLBACK:
            return None
        return _local_resolve_pick(text, fares)
    n = len(fares)
    options = json.dumps([
        {"number": i, "airline": f.get("airline"),
         "departs_at": f.get("departs_at"),
         "price": f"₦{f['price']:,.0f}" if f.get("price") is not None else None}
        for i, f in enumerate(fares, start=1)])
    prompt = PICK_PROMPT.replace("{options}", options).replace("{text}", text)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 512},
    }
    url = f"{GEMINI_BASE}/models/{model}:generateContent?key={api_key}"
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    content = None
    try:
        for attempt in (0, 1, 2):
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["candidates"][0]["content"]["parts"][0]["text"]
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))   # rate-limit: back off, retry
                    continue
                logger.warning("Gemini pick resolve failed (%s) - local", e)
                break
            except Exception as e:
                logger.warning("Gemini pick resolve failed (%s) - local", e)
                break
    finally:
        if own_client:
            client.close()
    if content is not None:
        m = re.search(r"\b([0-9]+)\b", content)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= n:
                return idx
            # Gemini answered but with 0 or an out-of-range number: it judged
            # this NOT a pick (a question, small talk, ...). Trust that - do
            # not second-guess it with the local resolver.
            return None
    if not _ENABLE_LOCAL_PICK_FALLBACK:
        return None
    return _local_resolve_pick(text, fares)


def compose_ranked_reply(fares: list, origin: str, destination: str,
                         flight_date: str, user_name: str = None,
                         api_key: str = None, model: str = None,
                         http_client: Optional[httpx.Client] = None) -> str:
    """Present the ranked fare list like a human agent.

    The deterministic numbered template is the guaranteed fallback; when
    Gemini is reachable it NARRATES the options (a context line, a
    recommendation, every number/price preserved) so the reply reads as a
    person talking - not a menu. Never raises."""
    api_key = api_key or GEMINI_API_KEY
    model = model or GEMINI_MODEL
    greeting = _greeting(user_name)
    lines = [f"{i}. {f['airline']}"
             + (f", leaves {f['departs_at']}" if f.get("departs_at") else "")
             + f" - ₦{f['price']:,.0f}"
             for i, f in enumerate(fares, start=1)]
    nums = list(range(1, len(fares) + 1))
    choices = (", ".join(str(n) for n in nums[:-1]) + " or " + str(nums[-1]))
    template = (f"Here's what I found {origin} -> {destination} on "
                f"{flight_date}:\n" + "\n".join(lines)
                + f"\n\nWhich one would you like? Reply {choices}.")
    if not api_key:
        return _warm_fallback(template, greeting)

    options = json.dumps([
        {"number": i, "airline": f.get("airline"),
         "departs_at": f.get("departs_at"),
         "price": f"₦{f['price']:,.0f}" if f.get("price") is not None else None}
        for i, f in enumerate(fares, start=1)])
    prompt = (RANKED_REPLY_PROMPT
              .replace("{greeting}", greeting)
              .replace("{origin}", origin)
              .replace("{destination}", destination)
              .replace("{date}", flight_date))
    payload = {
        "contents": [{"parts": [{"text": prompt + "\n\nOptions:\n" + options}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 4096},
    }
    url = f"{GEMINI_BASE}/models/{model}:generateContent?key={api_key}"
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        for attempt in (0, 1, 2):
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text or _warm_fallback(template, greeting)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))   # rate-limit: back off, retry
                    continue
                logger.warning("Gemini ranked reply failed: %s", e)
                return _warm_fallback(template, greeting)
            except Exception as e:
                logger.warning("Gemini ranked reply failed: %s", e)
                return _warm_fallback(template, greeting)
        return _warm_fallback(template, greeting)
    finally:
        if own_client:
            client.close()