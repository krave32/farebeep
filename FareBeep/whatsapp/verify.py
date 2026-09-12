"""Webhook signature verification and subscription challenge."""
import hashlib, hmac, logging
from fastapi import Query, HTTPException
from .config import get_config

logger = logging.getLogger(__name__)

def verify_signature(payload_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    config = get_config()
    expected = "sha256=" + hmac.new(
        config.app_secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

async def handle_verification(
    mode: str = Query(..., alias="hub.mode"),
    token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge"),
):
    config = get_config()
    if mode == "subscribe" and token == config.verify_token:
        logger.info("WhatsApp webhook verified.")
        return int(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")
