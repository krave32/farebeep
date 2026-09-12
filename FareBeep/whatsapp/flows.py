"""WhatsApp Flow data-exchange endpoint."""
import logging
from typing import Any
from datetime import date, datetime
from fastapi import APIRouter, Request, HTTPException

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

@router.post("/flow")
async def flow_data_exchange(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    # TODO: Add Meta Flow endpoint decryption for production
    action = data.get("action", "")
    screen = data.get("screen", "")
    token = data.get("flow_token", "")
    logger.info("Flow: action=%s screen=%s", action, screen)
    if action in ("INIT", "data_exchange"):
        if screen == "SET_BEEP_TRIP":
            return {"version": "3.0", "screen": screen, "data": {"airports": AIRPORTS, "airlines": AIRLINES}}
        if screen == "SET_BEEP_DATES":
            o, d = data.get("origin", ""), data.get("destination", "")
            if o and d and o == d:
                return {"version": "3.0", "screen": screen, "error": "Origin and destination cannot be the same.", "data": {}}
            return {"version": "3.0", "screen": screen, "data": {"origin": o, "destination": d}}
        return {"version": "3.0", "screen": screen, "data": {}}
    if action == "COMPLETE":
        if screen == "SET_BEEP_REVIEW":
            err = _validate_beep(data.get("beep_data", {}))
            if err:
                return {"version": "3.0", "screen": "SET_BEEP_TRIP", "error": err, "data": {}}
            # TODO: Call existing watch creation service here
            return {"version": "3.0", "screen": "SUCCESS", "data": {"extension_message_response": {"params": {"flow_token": token}}}}
        return {"version": "3.0", "screen": "SUCCESS", "data": {}}
    return {"version": "3.0", "screen": screen, "data": {}}

def _validate_beep(d: dict) -> str | None:
    if not d.get("origin"): return "Select a departure city."
    if not d.get("destination"): return "Select a destination."
    if d["origin"] == d["destination"]: return "Cities must differ."
    if not d.get("departure_date"): return "Select a travel date."
    try:
        if datetime.strptime(d["departure_date"], "%Y-%m-%d").date() < date.today():
            return "Date cannot be in the past."
    except (ValueError, TypeError):
        return "Invalid date."
    p = d.get("passengers", 1)
    if not isinstance(p, int) or p < 1 or p > 9: return "Passengers: 1–9."
    return None
