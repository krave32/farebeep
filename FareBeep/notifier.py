"""THE CONVERSATIONAL LAYER (outbound) - provider-agnostic WhatsApp sending.

Two channels share one interface (send_text / send_template) so the brain
pipeline in main.py never cares which one is wired:

  meta     - Meta WhatsApp Cloud API direct (the production path; 360dialog /
             Infobip are just resellers of the same API - we need no BSP).
  twilio   - Twilio WhatsApp Sandbox (a test path: one shared sandbox number
             users join with a code; no templates in the sandbox, so
             send_template degrades to a plain text message).
  telegram - Telegram Bot API (the FASTEST test path: no approval, no
             sandbox, no 24-hour window, no templates. Identity = chat_id).

Switch with MESSAGING_PROVIDER in .env, or call get_notifier().
"""
import logging

import httpx

from FareBeep.config import (MESSAGING_PROVIDER, META_ACCESS_TOKEN,
                             META_API_VERSION, META_PHONE_NUMBER_ID,
                             TELEGRAM_BOT_TOKEN, TWILIO_ACCOUNT_SID,
                             TWILIO_AUTH_TOKEN, TWILIO_FROM_WHATSAPP)

logger = logging.getLogger("farebeep.notifier")

GRAPH_BASE = "https://graph.facebook.com"


def get_notifier():
    """Factory - return the transport configured by MESSAGING_PROVIDER."""
    provider = (MESSAGING_PROVIDER or "meta").lower()
    if provider == "twilio":
        return TwilioWhatsapp()
    if provider == "telegram":
        return TelegramBot()
    return MetaWhatsapp()


class MetaWhatsapp:
    """Minimal Meta Cloud API outbound client."""

    def __init__(self, access_token: str = None, phone_number_id: str = None,
                 api_version: str = None, http_client: httpx.Client = None):
        self.access_token = access_token or META_ACCESS_TOKEN
        self.phone_number_id = phone_number_id or META_PHONE_NUMBER_ID
        self.api_version = api_version or META_API_VERSION
        self._http = http_client or httpx.Client(timeout=10.0)

    @property
    def _messages_url(self) -> str:
        return (f"{GRAPH_BASE}/{self.api_version}"
                f"/{self.phone_number_id}/messages")

    def _send(self, to: str, payload: dict, message_type: str) -> bool:
        if not self.access_token or not self.phone_number_id:
            logger.warning(
                "META_ACCESS_TOKEN / META_PHONE_NUMBER_ID not set - "
                "message NOT sent (%s to %s)", message_type, to)
            return False
        payload["messaging_product"] = "whatsapp"
        payload["to"] = to
        try:
            resp = self._http.post(
                self._messages_url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=payload)
            resp.raise_for_status()
            logger.info("WhatsApp %s sent to %s", message_type, to)
            return True
        except Exception as e:
            logger.error("WhatsApp %s failed to %s: %s", message_type, to, e)
            return False

    def send_text(self, to: str, body: str) -> bool:
        """Instant message (24-hr window / user-initiated replies)."""
        return self._send(to, {"type": "text",
                               "text": {"body": body}}, "text")

    def send_template(self, to: str, template_name: str,
                      body_parameters: list = None,
                      language: str = "en_US",
                      policy: str = "deterministic") -> bool:
        """Proactive WhatsApp Template message (outside the 24h window).

        Used by status.py for status-change pushes (e.g. "Delayed"). The
        template must already be approved in the Meta app, with the exact
        same name (see META_TEMPLATE_FLIGHT_STATUS in config).
        """
        components = []
        if body_parameters:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p}
                               for p in body_parameters],
            })
        template = {
            "name": template_name,
            "language": {"code": language, "policy": policy},
        }
        if components:
            template["components"] = components
        return self._send(to, {"type": "template",
                               "template": template}, "template")


class TwilioWhatsapp:
    """Twilio WhatsApp Sandbox transport - THE TEST-VERSION CHANNEL.

    Sandbox limits accepted for the test build:
      - one shared sandbox number (user must send `join <code>` first)
      - NO approved templates: send_template degrades to send_text
      - outbound only inside the 24-hour window after a user message
    """

    def __init__(self, account_sid: str = None, auth_token: str = None,
                 from_whatsapp: str = None, client=None):
        self.account_sid = account_sid or TWILIO_ACCOUNT_SID
        self.auth_token = auth_token or TWILIO_AUTH_TOKEN
        self.from_whatsapp = from_whatsapp or TWILIO_FROM_WHATSAPP
        self._client = client
        if client is None and self.account_sid and self.auth_token:
            from twilio.rest import Client
            self._client = Client(self.account_sid, self.auth_token)

    @property
    def _ready(self) -> bool:
        return bool(self._client and self.from_whatsapp)

    def send_text(self, to: str, body: str) -> bool:
        """Outbound text via the Twilio REST API (sandbox number)."""
        if not self._ready:
            logger.warning(
                "Twilio not configured (ACCOUNT_SID/AUTH_TOKEN/FROM_WHATSAPP) "
                "- message NOT sent to %s", to)
            return False
        try:
            self._client.messages.create(
                from_=self.from_whatsapp, to=f"whatsapp:{to}", body=body)
            logger.info("Twilio text sent to %s", to)
            return True
        except Exception as e:
            logger.error("Twilio text failed to %s: %s", to, e)
            return False

    def send_template(self, to: str, template_name: str,
                      body_parameters: list = None,
                      language: str = "en_US",
                      policy: str = "deterministic") -> bool:
        """Sandbox has no templates - degrade to a plain-text push."""
        parts = [p if isinstance(p, str) else str(p)
                 for p in (body_parameters or [])]
        body = f"{template_name}: {' '.join(parts)}"
        return self.send_text(to, body)


class TelegramBot:
    """Telegram Bot API transport - THE FASTEST TEST CHANNEL.

    No approval, no sandbox join codes, no 24-hour window, no templates:
    the bot can message anyone who has opened the chat. Identity in the
    FareBeep User model is the chat_id (str), stored where a phone number
    would live on the WhatsApp channels.
    """

    def __init__(self, token: str = None, http_client: httpx.Client = None):
        self.token = token or TELEGRAM_BOT_TOKEN
        self._http = http_client or httpx.Client(timeout=10.0)

    @property
    def _ready(self) -> bool:
        return bool(self.token)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send_text(self, to: str, body: str) -> bool:
        """sendMessage to the chat_id (`to`). Plain mode: no markdown
        escaping needed for fare text with // and currencies."""
        if not self._ready:
            logger.warning("TELEGRAM_BOT_TOKEN not set - message NOT sent to %s",
                           to)
            return False
        try:
            resp = self._http.post(
                self._api_url("sendMessage"),
                json={"chat_id": to, "text": body})
            resp.raise_for_status()
            data = resp.json()
            ok = bool(data.get("ok"))
            logger.info("Telegram message sent to %s (ok=%s)", to, ok)
            return ok
        except Exception as e:
            logger.error("Telegram message failed to %s: %s", to, e)
            return False

    def send_template(self, to: str, template_name: str,
                      body_parameters: list = None,
                      language: str = "en_US",
                      policy: str = "deterministic") -> bool:
        """No templates outside WhatsApp - degrade to a plain-text push."""
        parts = [p if isinstance(p, str) else str(p)
                 for p in (body_parameters or [])]
        body = f"{template_name}: {' '.join(parts)}"
        return self.send_text(to, body)