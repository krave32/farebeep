"""Route inbound messages to deterministic handlers or the LLM agent."""
import logging
from .router import InboundMessage, MessageType, detect_command
from . import sender

logger = logging.getLogger(__name__)

async def handle_inbound(msg: InboundMessage) -> None:
    await sender.mark_as_read(msg.message_id)
    if msg.message_type == MessageType.BUTTON_REPLY:
        await _handle_button(msg)
    elif msg.message_type == MessageType.LIST_REPLY:
        await _handle_list(msg)
    elif msg.message_type == MessageType.FLOW_RESPONSE:
        await _handle_flow(msg)
    elif msg.message_type == MessageType.TEXT:
        await _handle_text(msg)
    elif msg.message_type == MessageType.UNSUPPORTED:
        await sender.send_text(msg.from_number, "I can't process files yet. Type your request or reply *menu*.")
    else:
        logger.warning("Unhandled type %s from %s", msg.message_type, msg.from_number)

async def _handle_text(msg: InboundMessage) -> None:
    if not msg.text: return
    cmd = detect_command(msg.text)
    if cmd:
        await _handle_command(msg.from_number, cmd)
        return
    # TODO: Connect to existing Groq agent pipeline
    await sender.send_buttons(msg.from_number,
        "I'd love to help! What would you like to do?",
        [{"id": "find_flights", "title": "Find flights"}, {"id": "set_beep", "title": "Set a beep"}])

async def _handle_command(phone: str, cmd: str) -> None:
    if cmd == "menu":
        await sender.send_buttons(phone,
            "Welcome to *FareBeep* ✈️\nWatch Nigerian flight prices and get alerts when fares drop.\n\nWhat would you like to do?",
            [{"id": "find_flights", "title": "Find flights"}, {"id": "set_beep", "title": "Set a beep"}, {"id": "track_flight", "title": "Track a flight"}],
            footer="Reply STOP to pause alerts")
    elif cmd == "help":
        await sender.send_text(phone,
            "*FareBeep help:*\n🔍 Find flights\n🔔 Set a beep — price-drop alerts\n✈️ Track a flight\n📋 My beeps\n\nType naturally: _\"Lagos to Abuja next Friday\"_\nReply *menu* anytime.")
    elif cmd == "stop":
        # TODO: pause_all_watches(phone)
        await sender.send_text(phone, "Alerts paused. Reply *menu* to resume.\nReply *delete my data* for full deletion.")
    elif cmd == "my_watches":
        # TODO: fetch from DB
        await sender.send_text(phone, "Your active beeps will appear here.\n_Connecting to your watch database._")
    elif cmd == "my_account":
        await sender.send_buttons(phone, "*Your Account*",
            [{"id": "my_watches", "title": "My beeps"}, {"id": "my_bookings", "title": "My bookings"}])
    elif cmd == "my_bookings":
        await sender.send_text(phone, "Your bookings will appear here.")

async def _handle_button(msg: InboundMessage) -> None:
    bid = msg.button_id or ""
    phone = msg.from_number
    if bid == "find_flights":
        await sender.send_text(phone, "Where are you flying from?")
        # TODO: set conversation state AWAITING_ORIGIN
    elif bid == "set_beep":
        token = f"beep_{phone}_{msg.timestamp}"
        await sender.send_flow(phone,
            "Set a price-drop alert. We'll notify you when fares drop — even small savings.",
            flow_id="YOUR_BEEP_FLOW_ID", flow_cta="Set my beep", flow_token=token)
    elif bid == "track_flight":
        await sender.send_text(phone, "Which airline? e.g. *Air Peace*")
    elif bid.startswith("book_"):
        await _handle_book(phone, bid.replace("book_", ""))
    elif bid.startswith("beep_"):
        await _handle_beep_from_offer(phone, bid.replace("beep_", ""))

async def _handle_list(msg: InboundMessage) -> None:
    logger.info("List: %s from %s", msg.list_id, msg.from_number)

async def _handle_flow(msg: InboundMessage) -> None:
    await sender.send_text(msg.from_number,
        "✅ *Your beep is on!*\nWe'll watch fares and message you when we find a lower price.\nReply *my beeps* to manage watches.")

async def _handle_book(phone: str, offer_ref: str) -> None:
    # TODO: revalidate offer, launch passenger Flow, create Paystack checkout
    await sender.send_text(phone, "Let's get you booked. I need your passenger details.")

async def _handle_beep_from_offer(phone: str, offer_ref: str) -> None:
    # TODO: prefill beep from offer details
    await sender.send_text(phone, "Setting up a beep for this route. We'll alert you on any drop.")
