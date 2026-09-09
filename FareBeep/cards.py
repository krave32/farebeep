"""WhatsApp flight cards: airline logo + live price + tap buttons.

Renders search results as Meta interactive messages WITHOUT a product
catalog (no uploads, no approvals, no 14-24h waits, real-time prices):
each card = airline logo picture, route/time/price body, Book +
Set-alert buttons. Taps return through /webhook/meta as button_reply
ids - see translate_tap() and main._tap_alert_by_phone.

Logo art: hotlinked Kiwi CDN marks, verified live (HTTP 200) per code.
Keyed by airline NAME (that is what the search engines return).
Unknown airlines get the same card with NO image (the header is
optional) - never a wrong logo.
"""
import logging
from datetime import datetime

logger = logging.getLogger("farebeep.cards")

LOGO_CDN = "https://images.kiwi.com/airlines/64x64/{code}.png"

# Airline name (lowercased, stripped) -> IATA code with a VERIFIED logo.
# Max Air deliberately absent: "VM" 404s on the CDN, so it gets an
# imageless card until a real mark URL is confirmed.
AIRLINE_CODES = {
    "air peace": "P4",
    "arik air": "W3",
    "arik": "W3",
    "ibom air": "QI",
    "ibom": "QI",
    "green africa": "Q9",
    "green africa airways": "Q9",
    "enugu air": "EE",
    "enugu": "EE",
    "binani air": "NA",
    "binani": "NA",
    "rano air": "RN",
    "rano": "RN",
    "overland": "OJ",
    "overland airways": "OJ",
    "united nigeria": "UN",
    "united nigeria airlines": "UN",
    "xejet": "XJ",
    "valuejet": "VK",
    "value jet": "VK",
}

# Button ids. pick/alert carry the 1-based rank from the fare list so a
# tap reuses the tested "reply 1, 2, 3" pick gate; book = bare "BOOK".
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20


def logo_for(airline_name) -> str | None:
    """Logo image URL for an airline name, or None (imageless card)."""
    if not airline_name or not isinstance(airline_name, str):
        return None
    code = AIRLINE_CODES.get(airline_name.strip().lower())
    if not code:
        return None
    return LOGO_CDN.format(code=code)


def _pretty_date(flight_date) -> str:
    try:
        return datetime.strptime(str(flight_date)[:10], "%Y-%m-%d").strftime(
            "%a %d %b")
    except (ValueError, TypeError):
        return str(flight_date or "")


def _seats_note(fare: dict) -> str | None:
    seats = fare.get("seats_left")
    if isinstance(seats, bool):
        return None
    if isinstance(seats, (int, float)) and seats > 0:
        return f"Only {int(seats)} left!"
    return None


def card_body(fare: dict, origin: str, destination: str) -> str:
    """Card text: airline + flight, route/times, big price, extras.

    Renders whatever the fare carries - arrival time, duration, baggage
    and seats-left appear only when present, so lean (old-shape) fares
    render exactly as before.
    """
    airline = fare.get("airline") or "Airline"
    flight_no = fare.get("flight_number") or ""
    title = f"\u2708\ufe0f {airline} {flight_no}".strip()
    departs = fare.get("departs_at") or fare.get("departure_time") or ""
    arrives = fare.get("arrival_time") or ""
    route = (f"\U0001f6eb {origin}{(' ' + departs) if departs else ''} "
             f"\u2192 \U0001f6ec {destination}"
             f"{(' ' + arrives) if arrives else ''}")
    meta = _pretty_date(fare.get("flight_date"))
    if fare.get("duration"):
        meta += f" \u2022 {fare['duration']}"
    price = f"\U0001f4b0 \u20a6{fare.get('price', 0):,.0f}"
    lines = [title, f"{route} \u2022 {meta}", price]
    extras = []
    if fare.get("baggage"):
        extras.append(f"\U0001f9f3 {fare['baggage']} included")
    seats = _seats_note(fare)
    if seats:
        extras.append(f"\U0001f525 {seats}")
    if extras:
        lines.append(" \u2022 ".join(extras))
    return "\n".join(lines)[:1024]


def card_buttons(fare: dict, idx: int) -> list:
    """(button_id, title) pairs. idx is 1-based rank; 0 = single-fare
    card backed by last_fare context instead of the ranked list."""
    price = f"\u20a6{fare.get('price', 0):,.0f}"
    book_title = f"Book {price}"[:MAX_BUTTON_TITLE]
    if idx <= 0:
        return [("book", book_title), ("alert:0", "Set alert")]
    return [(f"pick:{idx}", book_title), (f"alert:{idx}", "Set alert")]


def translate_tap(button_id: str):
    """Map a button_reply/list_reply id to an action.

    Returns ("pick", n) | ("alert", n) | ("book",) | ("beep", sub_id)
    | None (unknown - the webhook ignores it). "dismiss" taps from
    beep templates also map to None: deliberate silence.
    """
    if not button_id or not isinstance(button_id, str):
        return None
    if button_id == "book":
        return ("book",)
    if button_id.startswith("beep:"):
        try:
            return ("beep", int(button_id[5:]))
        except ValueError:
            return None
    for kind in ("pick", "alert"):
        prefix = kind + ":"
        if button_id.startswith(prefix):
            try:
                n = int(button_id[len(prefix):])
            except ValueError:
                return None
            if 0 <= n <= 10:
                return (kind, n)
            return None
    return None
