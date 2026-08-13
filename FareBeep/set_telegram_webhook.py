"""Point the Telegram bot at your tunnel URL (one-time per tunnel restart).

Usage:
    python FareBeep/set_telegram_webhook.py https://xxx.trycloudflare.com

Registers https://<url>/webhook/telegram with the secret token from .env.
"""
import os
import sys

import httpx

from FareBeep.config import TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python FareBeep/set_telegram_webhook.py <tunnel-url>")
        return 2
    base = sys.argv[1].rstrip("/")
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is empty - paste it into FareBeep/.env first")
        return 2
    if not TELEGRAM_WEBHOOK_SECRET:
        print("TELEGRAM_WEBHOOK_SECRET is empty - set it in FareBeep/.env")
        return 2

    url = f"{base}/webhook/telegram"
    resp = httpx.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        params={"url": url, "secret_token": TELEGRAM_WEBHOOK_SECRET},
        timeout=15.0)
    data = resp.json()
    print(data)
    if data.get("ok"):
        print(f"Webhook set -> {url}")
        return 0
    print(f"FAILED ({data.get('error_code')}): {data.get('description')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
