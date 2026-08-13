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

Rules:
- PASS 1 IS EXTRACTION ONLY: origin and destination are independent. NEVER
  invent one from the other. "I'm going to Lagos" -> destination "Lagos",
  origin null, date null. Record city names as WRITTEN, no IATA codes.
- Today's date is {{today}}. Resolve "tomorrow", "next week", "next tuesday",
  "in August" against it.
- "track"/"status" = user gave a flight number or wants flight status/the
  departure time of their booking. Include the flight number in "flight"
  (e.g. "P47123") if present.
- "book" = user wants to proceed/buy after seeing a fare. Include the
  flight number in "flight" if one is given (e.g. "book P47123").
- "subscribe" = user wants price-drop alerts for a route ("subscribe LOS ABV",
  "alert me when Lagos to Abuja drops below 80000", "watch Abuja to Enugu",
  "track lagos to abuja").
- "unsubscribe" = user wants to stop alerts ("unsubscribe", "stop alerts",
  "remove my alerts", "unsubscribe LOS ABV").
- "help" = greeting, "help", or unrelated chat.
- CONCISE: output ONLY the JSON object. No preamble, no prose, no markdown
  fences. Nothing else."""


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
_PRICE_RE = re.compile(
    r"(?:below|under|less than|max)\s*(?:ngn|n)?\s*([\d,]+(?:\.\d+)?)k?"
    r"|([\d,]+)(?:\.\d+)?\s*k\b"
    r"|(?:^|\s)([\d,]+(?:\.\d+)?)(?:\s|$)", re.IGNORECASE)

_NAME_RE = re.compile(
    r"\b(?:my name is|i\x27?m called|i am called|call me)\s+([a-z]{2,})\b",
    re.IGNORECASE)

_WEEKDAY_RE = re.compile(
    r"\b((?:next|this|coming)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")


def _local_name(text: str) -> Optional[str]:
    """"My name is Damilola" / "call me Bola" -> "Damilola"."""
    m = _NAME_RE.search(text)
    return m.group(1).capitalize() if m else None


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
    raw = m.group(1) or m.group(2) or m.group(3)
    try:
        price = float(raw.replace(",", ""))
    except ValueError:
        return None
    if "k" in m.group(0).lower():
        price *= 1000.0  # "below 80k" -> 80000
    return price


def _local_parse(text: str) -> Optional[Intent]:
    """Deterministic intent extraction for offline/fallback operation."""
    flat = " ".join(text.strip().lower().split())
    if not flat:
        return None

    if re.search(r"\b(unsubscribe|stop alerts|remove my alerts|off alerts)\b", flat):
        return Intent(intent="unsubscribe", raw_text=text)

    fm = _FLIGHT_RE.search(flat)
    flight = fm.group(0).upper() if fm else None
    if flight and re.search(r"\b(track|status)\b", flat):
        return Intent(intent="status", flight=flight, raw_text=text)

    route = _local_route(flat)
    day = _local_date(flat)
    name = _local_name(flat)

    if re.search(r"\b(book|buy|reserve|pay|proceed)\b", flat):
        # "BOOK" alone, "book lagos to abuja tomorrow" - the route and date
        # are optional here: Pass 2 (main.py) fills them from the user's
        # last fare search when the user just says BOOK after a quote.
        return Intent(intent="book",
                      origin=route[0] if len(route or []) > 1 else None,
                      destination=route[1] if len(route or []) > 1 else None,
                      date=day, flight=flight, name=name, raw_text=text)

    target = _local_target_price(flat)
    if re.search(r"\b(subscribe|alert|watch|beep|track)\b", flat) \
            and (len(route or []) >= 2 or target is not None
                 or re.fullmatch(r"(subscribe|alert|watch|beep|track)( it)?", flat)):
        # bare "TRACK" / "beep" after a fare quote -> subscribe intent;
        # Pass 2 (main.py) fills the route from the user's last fare search.
        return Intent(intent="subscribe",
                      origin=route[0] if route else None,
                      destination=route[1] if len(route or []) > 1 else None,
                      date=day, target_price=target, name=name, raw_text=text)

    if route and re.search(r"\b(to|from|fly|flight)\b", flat):
        # PASS 1 EXTRACTION: partial routes stay partial ("I'm going to
        # Abuja" = destination only). Pass 2 (main.py) asks for what's
        # missing or defaults the origin - never invent data here.
        return Intent(intent="fare",
                      origin=route[0] if len(route) > 1 else None,
                      destination=route[1] if len(route) > 1 else route[0],
                      date=day, name=name, raw_text=text)

    if re.search(r"\b(hello|hi|hey|help|menu|options|what can you do|start)\b", flat):
        return Intent(intent="help", name=name, raw_text=text)
    if name:
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