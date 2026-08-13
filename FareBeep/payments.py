"""THE SETTLEMENT ENGINE - Paystack money math + secure webhook verification.

The brief's formula, implemented exactly:

    final_price = (Net_Fare + ARHA_MARKUP_NGN + PAYSTACK_FLAT_FEE_NAIRA)
                  / (1 - PROCESSING_FEE_RATE)

The customer funds the gateway fee (gross-up): after Paystack's cut the
utility nets exactly (net_fare + markup) on every booking. The flat fee
keeps the user from paying less than Paystack's fixed charge on small
tickets.

Known-fee safety (kept from the pricing review): if Paystack's applicable
fee (1.5% of base + flat) would exceed PAYSTACK_FEE_CAP_NAIRA, the cap
applies and the customer is NOT overcharged.

Security: Paystack signs every webhook with HMAC-SHA512 over the RAW body
using the secret key (X-Paystack-Signature) - see verify_paystack_signature.
"""
import hashlib
import hmac
import logging
import os
from typing import Optional

import httpx

from FareBeep.config import (ARHA_MARKUP_NGN, PAYSTACK_CALLBACK_URL,
                             PAYSTACK_FEE_CAP_NAIRA, PAYSTACK_FLAT_FEE_NAIRA,
                             PAYSTACK_SECRET_KEY, PROCESSING_FEE_RATE)

logger = logging.getLogger("farebeep.payments")

PAYSTACK_API_BASE = "https://api.paystack.co"      # test keys -> test gateway


# ---------------------------------------------------------------------------
# Pricing - the money math
# ---------------------------------------------------------------------------
def calculate_final_price(net_fare: float, markup: float = None,
                          flat_fee: float = None,
                          fee_rate: float = None) -> dict:
    """Final price per the settlement brief.

    Returns:
      {net_fare, markup, base_amount, processing_fee, total_amount}
      where base_amount = net_fare + markup and
            total_amount = (net_fare + markup + flat) / (1 - fee_rate).
    """
    markup = ARHA_MARKUP_NGN if markup is None else markup
    flat_fee = PAYSTACK_FLAT_FEE_NAIRA if flat_fee is None else flat_fee
    fee_rate = PROCESSING_FEE_RATE if fee_rate is None else fee_rate
    net_fare = round(float(net_fare), 2)
    base = round(net_fare + markup, 2)

    applicable = fee_rate * base + flat_fee
    if applicable >= PAYSTACK_FEE_CAP_NAIRA:
        # cap branch: Paystack would take more than the cap, so charge
        # base + cap and Paystack keeps exactly the cap.
        total = round(base + PAYSTACK_FEE_CAP_NAIRA, 2)
    else:
        total = round((net_fare + markup + flat_fee) / (1.0 - fee_rate), 2)
    return {
        "net_fare": net_fare,
        "markup": markup,
        "base_amount": base,
        "processing_fee": round(total - base, 2),
        "total_amount": total,
    }


# ---------------------------------------------------------------------------
# Paystack Test API - link generation
# ---------------------------------------------------------------------------
def initialize_paystack_payment(payment_ref: str, final_price: float,
                                email: str, secret_key: str = None,
                                callback_url: str = None,
                                http_client: httpx.Client = None) -> dict:
    """Create a Paystack Test Link for `final_price` NGN.

    Returns {access_code, authorization_url} or raises RuntimeError when
    the secret key is missing / Paystack rejects the request.
    """
    secret_key = secret_key or PAYSTACK_SECRET_KEY
    if not secret_key:
        raise RuntimeError("PAYSTACK_SECRET_KEY not set - paste the TEST key "
                           "into FareBeep/.env to issue payment links.")
    client = http_client or httpx.Client(timeout=15.0)
    resp = client.post(
        f"{PAYSTACK_API_BASE}/transaction/initialize",
        headers={"Authorization": f"Bearer {secret_key}"},
        json={
            "reference": payment_ref,
            "amount": int(round(final_price, 2) * 100),   # Paystack takes kobo
            "currency": "NGN",
            "email": email,
            "callback_url": callback_url or PAYSTACK_CALLBACK_URL or None,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError(f"Paystack initialize failed: {data}")
    return {
        "access_code": data["data"]["access_code"],
        "authorization_url": data["data"]["authorization_url"],
    }


# ---------------------------------------------------------------------------
# Webhook security - X-Paystack-Signature
# ---------------------------------------------------------------------------
def verify_paystack_signature(raw_body: bytes, signature: str,
                              secret_key: str = None) -> bool:
    """HMAC-SHA512 over the RAW body, keyed by the secret key.

    Paystack docs: X-Paystack-Signature = hmac_sha512(secret_key, raw_body),
    hex-encoded. The signature is computed over the body exactly as received
    (no reformatting, no charset tricks) - hence the bytes interface.
    """
    secret_key = secret_key or PAYSTACK_SECRET_KEY or os.getenv(
        "PAYSTACK_SECRET_KEY")
    if not secret_key:
        logger.warning("PAYSTACK_SECRET_KEY not set - webhook signature "
                       "verification impossible")
        return False
    expected = hmac.new(secret_key.encode("utf-8"), raw_body,
                        hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature or "")


__all__ = [
    "calculate_final_price", "initialize_paystack_payment",
    "verify_paystack_signature",
]
