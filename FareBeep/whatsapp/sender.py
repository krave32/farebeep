"""Outbound WhatsApp message builder and sender."""
import logging
from typing import Any
import httpx
from .config import get_config

logger = logging.getLogger(__name__)

def _headers() -> dict[str, str]:
    c = get_config()
    return {"Authorization": f"Bearer {c.access_token}", "Content-Type": "application/json"}

async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    c = get_config()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(c.messages_url, json=payload, headers=_headers())
        r.raise_for_status()
        result = r.json()
        logger.info("Sent to %s: %s", payload.get("to"), result.get("messages", [{}])[0].get("id", "?"))
        return result

async def send_text(to: str, body: str) -> dict:
    return await _post({
        "messaging_product": "whatsapp", "to": to,
        "type": "text", "text": {"preview_url": False, "body": body},
    })

async def send_buttons(to: str, body: str, buttons: list[dict], footer: str | None = None) -> dict:
    buttons = buttons[:3]
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                for b in buttons
            ]},
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer[:60]}
    return await _post(payload)

async def send_list(to: str, body: str, button_text: str, sections: list[dict], header: str | None = None) -> dict:
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list", "body": {"text": body},
            "action": {"button": button_text[:20], "sections": sections},
        },
    }
    if header:
        payload["interactive"]["header"] = {"type": "text", "text": header[:60]}
    return await _post(payload)

async def send_flight_card(
    to: str, airline: str, origin: str, destination: str,
    date: str, dep_time: str, arr_time: str | None,
    price_ngn: int, passengers: int, baggage: str | None,
    checked_at: str, offer_ref: str, image_url: str | None = None,
) -> dict:
    lines = [
        f"*{origin} → {destination}*", f"📅 {date}", f"✈️ {airline}",
        f"🕐 {dep_time}" + (f" → {arr_time}" if arr_time else ""),
        f"👤 {passengers} adult{'s' if passengers > 1 else ''}",
        f"💰 *₦{price_ngn:,} total*",
    ]
    if baggage:
        lines.append(f"🧳 {baggage}")
    lines.append(f"_Checked {checked_at}_")
    body = "\n".join(lines)
    btns = [{"id": f"book_{offer_ref}", "title": "Book now"}, {"id": f"beep_{offer_ref}", "title": "Set a beep"}]
    if image_url:
        return await _post({
            "messaging_product": "whatsapp", "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header": {"type": "image", "image": {"link": image_url}},
                "body": {"text": body},
                "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in btns]},
            },
        })
    return await send_buttons(to, body, btns)

async def send_flow(to: str, body: str, flow_id: str, flow_cta: str, flow_token: str, flow_mode: str = "draft") -> dict:
    return await _post({
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "flow", "body": {"text": body},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3", "flow_token": flow_token,
                    "flow_id": flow_id, "flow_cta": flow_cta[:20], "mode": flow_mode,
                },
            },
        },
    })

async def send_template(to: str, name: str, lang: str, components: list[dict] | None = None) -> dict:
    t: dict[str, Any] = {"name": name, "language": {"code": lang}}
    if components:
        t["components"] = components
    return await _post({"messaging_product": "whatsapp", "to": to, "type": "template", "template": t})

async def mark_as_read(message_id: str) -> None:
    c = get_config()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(c.messages_url, json={
                "messaging_product": "whatsapp", "status": "read", "message_id": message_id,
            }, headers=_headers())
    except Exception as e:
        logger.warning("mark_as_read failed: %s", e)
