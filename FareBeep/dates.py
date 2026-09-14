"""Shared flight-date parsing and validation (Africa/Lagos home).

One home for every date the product touches, so the Groq agent tools
(agent.py) and the deterministic parser (brain._local_date) can never
disagree:

  lagos_today()            - "today" in Africa/Lagos (not server time).
  validate_flight_date()   - strict YYYY-MM-DD: real calendar day, not in
                             the past. Raises DateError; the agent tools
                             turn that into structured JSON errors.
  validate_adults()        - 1..9 clamp with an error outside it.
  parse_expression()       - relative/Nigerian-English expressions to ISO.
                             "next tomorrow" is the DAY AFTER tomorrow
                             (not tomorrow - the old prompt said otherwise).

parse_expression takes an explicit `today` so tests pin dates without
freezing clocks; callers pass lagos_today() in production.
"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LAGOS_TZ = ZoneInfo("Africa/Lagos")

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b((?:next|this|coming)\s+)?(monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_STRICT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class DateError(ValueError):
    """A flight date the product cannot use (bad shape, unreal, past)."""


def lagos_today() -> date:
    """Today's date in Africa/Lagos."""
    return datetime.now(LAGOS_TZ).date()


def validate_flight_date(value, today: date = None) -> str:
    """Strict tool-facing check: YYYY-MM-DD, a real calendar day, and not
    before `today` (Lagos by default). Returns the ISO string, raises
    DateError otherwise - never silently repairs."""
    s = (value or "")
    s = s.strip() if isinstance(s, str) else ""
    m = _STRICT_RE.match(s)
    if not m:
        raise DateError(f"use YYYY-MM-DD (got {value!r})")
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise DateError(f"{s} is not a real calendar date")
    ref = today or lagos_today()
    if day < ref:
        raise DateError(f"{s} is in the past (today is {ref.isoformat()})")
    return day.isoformat()


def validate_adults(value) -> int:
    """Passenger count: None means 1; otherwise an int in 1..9. Floats
    are rejected outright (2.5 passengers must never silently become 2
    on a paid booking) - the model must send a clean int."""
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            as_int = int(str(value).strip()) if isinstance(value, str) \
                else None
        except (ValueError, TypeError):
            as_int = None
        if as_int is None or str(value).strip() != str(as_int):
            raise DateError(f"passengers must be 1-9 (got {value!r})")
        value = as_int
    if not 1 <= value <= 9:
        raise DateError(f"passengers must be 1-9 (got {value!r})")
    return value


def parse_expression(text: str, today: date = None):
    """Relative / Nigerian-English date expressions to ISO, or None.

    Rules (in priority order): "next tomorrow" (+2) before "tomorrow"
    (+1); "day after tomorrow" (+2); today/now; "next week <day>" (that
    weekday of next calendar week); bare ISO; slash dates (day-first);
    weekday names ("next <day>" = next calendar week, bare = upcoming,
    always future); month names + ordinals ("31st", "on the 2nd"); bare
    day numbers (this month if ahead, else next).
    """
    today = today or lagos_today()
    text = (text or "").lower()
    if re.search(r"\bnext\s+tomorrow\b", text):
        return (today + timedelta(days=2)).isoformat()
    if "day after tomorrow" in text:
        return (today + timedelta(days=2)).isoformat()
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b|\bnow\b", text):
        return today.isoformat()
    # "next week thursday" = the Thursday of the NEXT calendar week
    # (Mon-Sun after the current one).
    m = re.search(
        r"\bnext\s+week\s+(monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday)\b", text)
    if m:
        target = _WEEKDAYS[m.group(1)]
        return (today + timedelta(days=(7 - today.weekday()) + target)
                ).isoformat()
    if "next week" in text:
        return (today + timedelta(days=7)).isoformat()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    # Slash/dash dates: "31/08", "31-08", "08/31" - day/month first,
    # swapped only when the first part can't be a day (>12).
    m = re.search(r"\b(\d{1,2})\s*[/-]\s*(\d{1,2})(?:[/-]\d{2,4})?\b", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        day, month = (b, a) if a <= 12 < b else (a, b)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        year = today.year
        try:
            if date(year, month, day) < today:
                year += 1   # already passed -> next year
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    wd = _WEEKDAY_RE.search(text)
    if wd:
        target = _WEEKDAYS[wd.group(2)]
        if wd.group(1) and "next" in wd.group(1):
            delta = (7 - today.weekday()) + target   # next calendar week
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
    # Bare ordinal day with no month: "31st", "on the 2nd".
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
    # A BARE NUMBER is the day of the CURRENT month. Times (10:30, 10am),
    # years (2026) and prices (80k, 15000) never match.
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


_SLASH_RE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:[/-]\d{2,4})?\b")
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _pretty(day: date) -> str:
    return f"{day.day} {day.strftime('%B')}"


def ambiguous_date_hint(text: str, today: date = None):
    """A slash date readable both ways ("05/06" = 5 June or 6 May):
    return a confirmation question, else None. The parser rule stays
    day-first; this fires only when the SWAPPED reading is also a real,
    non-past date - "08/31" or "13/06" read one way and never ask.
    An explicit ISO date in the same message wins outright (no ask)."""
    today = today or lagos_today()
    flat = (text or "").lower()
    if _ISO_RE.search(flat):
        return None
    m = _SLASH_RE.search(flat)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a > 12 or b > 12 or a == b:
        return None
    readings = []
    for day, month in ((a, b), (b, a)):
        year = today.year
        try:
            if date(year, month, day) < today:
                year += 1
            readings.append(date(year, month, day))
        except ValueError:
            return None
    if readings[0] == readings[1]:
        return None
    return (f"Just to confirm - did you mean {_pretty(readings[0])} or "
            f"{_pretty(readings[1])}?")


__all__ = [
    "LAGOS_TZ", "DateError", "lagos_today", "validate_flight_date",
    "validate_adults", "parse_expression", "ambiguous_date_hint",
]
