"""WhatsApp Cloud API configuration from environment variables."""
import os
from dataclasses import dataclass, field

@dataclass(frozen=True)
class WhatsAppConfig:
    verify_token: str = field(default_factory=lambda: os.environ["META_VERIFY_TOKEN"])
    app_secret: str = field(default_factory=lambda: os.environ["META_APP_SECRET"])
    access_token: str = field(default_factory=lambda: os.environ["META_ACCESS_TOKEN"])
    phone_number_id: str = field(default_factory=lambda: os.environ["META_PHONE_NUMBER_ID"])
    api_version: str = os.environ.get("META_API_VERSION", "v21.0")

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}"

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/{self.phone_number_id}/messages"

def get_config() -> WhatsAppConfig:
    return WhatsAppConfig()
