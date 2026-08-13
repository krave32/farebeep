"""FareBeep configuration - single source of truth for env values.

Loads .env from the FareBeep directory itself, so the utility is portable:
you can drop the folder anywhere and it reads its own .env.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get(key: str, default=None):
    return os.getenv(key, default)


def _get_float(key: str, default: float) -> float:
    raw = _get(key)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


# --- Supabase ---
SUPABASE_URL = _get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_DB_URL = _get("SUPABASE_DB_URL", _get("DATABASE_URL"))

# --- Meta WhatsApp Cloud API ---
META_VERIFY_TOKEN = _get("META_VERIFY_TOKEN")
META_ACCESS_TOKEN = _get("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = _get("META_PHONE_NUMBER_ID")
META_APP_SECRET = _get("META_APP_SECRET")
META_API_VERSION = _get("META_API_VERSION", "v20.0")
META_TEMPLATE_FLIGHT_STATUS = _get("META_TEMPLATE_FLIGHT_STATUS",
                                   "farebeep_flight_status")

# --- Messaging provider switch ---
# "meta" (production path: Meta Cloud API direct, no BSP needed) or
# "twilio" (test path: Twilio WhatsApp Sandbox - one shared number, no
# templates, outbound within the 24h session) or
# "telegram" (fastest test path: plain Bot API - no approval, no sandbox,
# no 24h window; identity is the chat_id). Factory: notifier.get_notifier()
MESSAGING_PROVIDER = _get("MESSAGING_PROVIDER", "meta")
TWILIO_ACCOUNT_SID = _get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_WHATSAPP = _get("TWILIO_FROM_WHATSAPP")    # "whatsapp:+14155238886"
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = _get("TELEGRAM_WEBHOOK_SECRET")
# Long-poll seconds for the tunnel-free poller (FareBeep/poller.py).
# Telegram itself is the transport: no public URL, no cloudflared.
TELEGRAM_POLL_TIMEOUT = int(_get("TELEGRAM_POLL_TIMEOUT", "25"))

# --- Gemini ---
GEMINI_API_KEY = _get("GEMINI_API_KEY")
# GEMINI_MODEL default: "gemini-1.5-flash" is retired (404, confirmed live), and
# "gemini-2.5-flash" is "no longer available to new users" on new keys.
# gemini-flash-latest is the current affordable flash model (verified 200).
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-flash-latest")

# --- SerpApi ---
SERPAPI_API_KEY = _get("SERPAPI_API_KEY")
SERPAPI_ENGINE = _get("SERPAPI_ENGINE", "google_flights")
# Google Flights via SerpApi does NOT support NGN (verified live: 400
# "Unsupported `NGN` for currency."); fares are fetched in USD and converted
# with the daily NGN rate.
SERPAPI_CURRENCY = _get("SERPAPI_CURRENCY", "USD")
# FX_RATE_NGN_PER_USD is the ABSOLUTE FLOOR + offline fallback: the live
# rate never goes below it (founder price-volatility protection). Set it to
# the current parallel-market rate (13 Aug 2026: NGN 1,416-1,425).
FX_RATE_NGN_PER_USD = _get_float("FX_RATE_NGN_PER_USD", 1425.0)
# FX_SAFETY_MARGIN: quotes use the OFFICIAL/Google-basis rate (open.er-api,
# ~CBN) plus this buffer - so prices track Google's naira display while a
# sudden naira move can't wipe the margin. 0.03 = official + 3%.
FX_SAFETY_MARGIN = _get_float("FX_SAFETY_MARGIN", 0.03)
# How often the USD->NGN rate is re-fetched (open.er-api.com, free, no key)
# and a snapshot row is recorded for the tracked history.
FX_RATE_TTL_HOURS = _get_int("FX_RATE_TTL_HOURS", 12)
# Region bias for Google Flights results (ng = Nigerian market). This is a
# supported param, distinct from currency - keep USD + this.
SERPAPI_GL_REGION = _get("SERPAPI_GL_REGION", "ng")

# --- Paystack ---
PAYSTACK_SECRET_KEY = _get("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = _get("PAYSTACK_PUBLIC_KEY")
PAYSTACK_CALLBACK_URL = _get("PAYSTACK_CALLBACK_URL")

# --- Aviationstack ---
AVIATIONSTACK_API_KEY = _get("AVIATIONSTACK_API_KEY")

# --- Business rules ---
MARKUP_NAIRA = _get_float("MARKUP_NAIRA", 3000.0)
PROCESSING_FEE_RATE = _get_float("PROCESSING_FEE_RATE", 0.015)
# The Settlement Engine's margin: the flat NGN markup charged on every
# booking, over and above the airline fare (Settlement brief: ARHA_MARKUP).
ARHA_MARKUP_NGN = _get_float("ARHA_MARKUP_NGN", 5000.0)
# Real Paystack Nigeria fee: 1.5% + NGN 100, known-fee cap NGN 2,000.
# (pricing rule from http://support.paystack.com/en/articles/2130306)
PAYSTACK_FLAT_FEE_NAIRA = _get_float("PAYSTACK_FLAT_FEE_NAIRA", 100.0)
PAYSTACK_FEE_CAP_NAIRA = _get_float("PAYSTACK_FEE_CAP_NAIRA", 2000.0)
# Who gets the "Refund Required" alert when a payment lands after the
# 10-minute window closed (a phone number or Telegram chat_id as string).
ADMIN_ALERT_PHONE = _get("ADMIN_ALERT_PHONE")
BOOKING_TTL_MINUTES = _get_int("BOOKING_TTL_MINUTES", 10)
LEDGER_TTL_MINUTES = _get_int("LEDGER_TTL_MINUTES", 20)
STATUS_WATCH_LEAD_HOURS = _get_int("STATUS_WATCH_LEAD_HOURS", 3)
STATUS_POLL_SECONDS = _get_int("STATUS_POLL_SECONDS", 300)
# APScheduler worker (--scheduled mode): how often the TRACKING checks
# (fare-drop Beeps) run; booking sweep + status watches stay on the fast loop.
TRACKING_POLL_HOURS = _get_int("TRACKING_POLL_HOURS", 4)
# Price guardrail: a one-way domestic fare above this (₦NGN) is treated as
# an anomaly - the bot says prices are unusually high instead of quoting it.
FARE_PRICE_GUARDRAIL_NGN = _get_float("FARE_PRICE_GUARDRAIL_NGN", 250000.0)
