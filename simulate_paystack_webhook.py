"""simulate_paystack_webhook.py - local webhook driver for the Settlement
Engine demo.

Your cloudflared tunnel is unreliable on this ISP, and Paystack delivers
charge.success events over the public internet. This script plays Paystack's
part locally: it finds the newest PENDING booking_session, signs a
charge.success webhook with the REAL HMAC-SHA512 key and POSTs it to the
running server - so the full loop (PAID -> Ticket Issued / refund_required)
can be demonstrated without a public URL.

Usage:
    python simulate_paystack_webhook.py              # fire newest pending session
    python simulate_paystack_webhook.py fb-abc123    # fire a specific reference
"""
import hmac
import hashlib
import json
import sys

import httpx

from FareBeep.config import PAYSTACK_SECRET_KEY
from FareBeep.database import SessionLocal
from FareBeep.models import BookingSession

WEBHOOK_URL = "http://127.0.0.1:8000/webhook/paystack"


def newest_pending_ref():
    db = SessionLocal()
    try:
        s = db.query(BookingSession).filter(
            BookingSession.status == "pending") \
            .order_by(BookingSession.created_at.desc()).first()
        return s.payment_ref if s else None
    finally:
        db.close()


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else newest_pending_ref()
    if not ref:
        print("No pending booking found - BOOK a flight in Telegram first.")
        sys.exit(1)

    payload = {
        "event": "charge.success",
        "data": {
            "status": "success",
            "reference": ref,
            "amount": 16200000,          # kobo; unused by settle_payment
            "currency": "NGN",
            "customer": {"email": "passenger@farebeep.ng"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw,
                         hashlib.sha512).hexdigest()

    r = httpx.post(WEBHOOK_URL, content=raw, timeout=15.0,
                   headers={"X-Paystack-Signature": signature,
                            "Content-Type": "application/json"})
    print(f"POST {ref} -> {r.status_code}")
    if r.status_code != 200:
        print(r.text)


if __name__ == "__main__":
    main()
