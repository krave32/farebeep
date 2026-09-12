"""Template registry and alert helpers."""
import logging
from dataclasses import dataclass
from . import sender

logger = logging.getLogger(__name__)

@dataclass
class TemplateDef:
    name: str
    language: str
    category: str
    approved: bool

TEMPLATES = {
    "farebeep_price_drop": TemplateDef("farebeep_price_drop", "en", "UTILITY", False),
    "farebeep_flight_delay": TemplateDef("farebeep_flight_delay", "en", "UTILITY", False),
    "farebeep_payment_received": TemplateDef("farebeep_payment_received", "en", "UTILITY", False),
    "farebeep_ticket_confirmed": TemplateDef("farebeep_ticket_confirmed", "en", "UTILITY", False),
    "farebeep_refund_update": TemplateDef("farebeep_refund_update", "en", "UTILITY", False),
}

async def send_price_drop(to: str, origin: str, dest: str, date: str, airline: str, old_price: int, new_price: int, checked_at: str, offer_ref: str) -> bool:
    t = TEMPLATES["farebeep_price_drop"]
    savings = old_price - new_price
    pct = round((savings / old_price) * 100) if old_price else 0
    if t.approved:
        try:
            await sender.send_template(to, t.name, t.language, [
                {"type": "body", "parameters": [
                    {"type": "text", "text": f"{origin} → {dest}"}, {"type": "text", "text": date},
                    {"type": "text", "text": airline}, {"type": "text", "text": f"₦{old_price:,}"},
                    {"type": "text", "text": f"₦{new_price:,}"}, {"type": "text", "text": f"₦{savings:,} ({pct}%)"},
                    {"type": "text", "text": checked_at},
                ]},
            ])
            return True
        except Exception as e:
            logger.error("Template failed, fallback: %s", e)
    await sender.send_buttons(to,
        f"🔔 *Fare drop: {origin} → {dest}*\n📅 {date} · ✈️ {airline}\nWas: ₦{old_price:,}\n*Now: ₦{new_price:,}*\n*Save ₦{savings:,} ({pct}%)*\n_Checked {checked_at}. Fare may change._",
        [{"id": f"book_{offer_ref}", "title": "Book now"}, {"id": f"beep_{offer_ref}", "title": "Keep watching"}])
    return True

async def send_delay_alert(to: str, flight: str, origin: str, dest: str, scheduled: str, estimated: str, delay_min: int, checked_at: str) -> bool:
    await sender.send_text(to,
        f"⚠️ *Flight update: {flight}*\n📍 {origin} → {dest}\nScheduled: {scheduled}\n*New estimate: {estimated}*\nDelay: ~{delay_min} min\n_Checked {checked_at}. Follow airline check-in instructions._")
    return True

async def send_ticket_confirmed(to: str, pnr: str, airline: str, origin: str, dest: str, date: str, dep: str) -> bool:
    await sender.send_text(to,
        f"✅ *Ticket confirmed!*\nRef: *{pnr}*\n✈️ {airline}\n📍 {origin} → {dest}\n📅 {date} · 🕐 {dep}\nReply *my bookings* for details.")
    return True
