"""Resend email delivery - PDFs only (vouchers, boarding passes).

Best-effort by contract: an email failure is logged and swallowed. It
must never block or mask the WhatsApp message that already went out.
Unset RESEND_API_KEY -> every call is a no-op (dev/test safe).
"""
import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def _api_key() -> Optional[str]:
    return (os.environ.get("RESEND_API_KEY") or "").strip() or None


def _from() -> str:
    return (os.environ.get("EMAIL_FROM") or "FareBeep <onboarding@resend.dev>").strip()


def is_configured() -> bool:
    return _api_key() is not None


def send_email(to: str, subject: str, body: str,
               attachment: Optional[dict] = None) -> bool:
    """Send one email via Resend. attachment: {"filename", "content_bytes",
    "mime"} (content base64'd here). Returns True only on a 2xx.

    to - the recipient address; empty/None -> silent no-op (no address,
    no email - e.g. users who never gave one).
    """
    key = _api_key()
    if not key:
        return False
    to = (to or "").strip()
    if not to or "@" not in to:
        logger.info("emailer: no valid address for %r - skipped", to)
        return False
    payload: dict = {
        "from": _from(),
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if attachment:
        payload["attachments"] = [{
            "filename": attachment.get("filename") or "document.pdf",
            "content": base64.b64encode(
                attachment.get("content_bytes") or b"").decode(),
            "content_type": attachment.get("mime") or "application/pdf",
        }]
    try:
        resp = httpx.post(
            RESEND_URL, json=payload, timeout=15,
            headers={"Authorization": f"Bearer {key}"})
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("emailer: Resend %s for %s: %s",
                       resp.status_code, to, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001 - best-effort by contract
        logger.warning("emailer: send failed for %s: %s", to, e)
        return False
