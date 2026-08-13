"""Telegram long-polling transport - the tunnel-free fallback.

Cloudflared quick tunnels are unreliable on some ISPs (measured: repeated
"control stream encountered a failure while serving"). Polling needs NO
public URL: the bot pulls updates over an outbound connection to
api.telegram.org and feeds them into the EXACT same conversational pipeline
(_handle_incoming_message) as the webhook.

Run:  python -m FareBeep.poller     (a separate process, alongside uvicorn)

Telegram only delivers to ONE endpoint, so the poller deletes the webhook on
start. When you're back on a stable tunnel, stop the poller and re-run:
    python FareBeep/set_telegram_webhook.py <tunnel-url>
"""
import logging
import time

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("farebeep.poller")

from FareBeep.config import TELEGRAM_BOT_TOKEN, TELEGRAM_POLL_TIMEOUT  # noqa: E402


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _handle(chat_id: str, text: str) -> None:
    from FareBeep.main import _handle_incoming_message
    try:
        _handle_incoming_message(chat_id, text)
    except Exception as e:
        logger.error("Polled message failed (%s): %s", chat_id, e)


def poll_once(client: httpx.Client, offset: int) -> int:
    """Fetch one batch of updates, route each message, return next offset."""
    resp = client.get(
        _api("getUpdates"),
        params={"timeout": TELEGRAM_POLL_TIMEOUT, "offset": offset,
                "allowed_updates": ["message"]})
    resp.raise_for_status()
    for update in resp.json().get("result") or []:
        offset = max(offset, int(update["update_id"]) + 1)
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        text = str(msg.get("text") or "")
        if text and chat_id:
            _handle(chat_id, text)
    return offset


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is empty - set it in FareBeep/.env")
        return
    client = httpx.Client(timeout=TELEGRAM_POLL_TIMEOUT + 15)
    client.get(_api("deleteWebhook")).raise_for_status()
    logger.info("Telegram polling started (no tunnel needed) - webhook deleted")
    offset = 0
    while True:
        try:
            offset = poll_once(client, offset)
        except Exception as e:
            logger.warning("Poll failed: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
