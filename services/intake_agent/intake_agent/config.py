"""Settings for the email intake agent (the Forethought stand-in on the email channel)."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # The model only classifies and extracts. Every action is taken by code.
    gemini_api_key: str = ""
    model: str = "gemini-flash-lite-latest"
    llm_timeout_s: float = 15.0

    integration_base_url: str = "http://localhost:8712"
    integration_api_key: str = ""

    zendesk_subdomain: str = ""
    zendesk_email: str = ""
    zendesk_api_token: str = ""
    # The signing secret, or the webhook's ID so the agent reads the secret from Zendesk itself.
    zendesk_webhook_secret: str = ""
    zendesk_webhook_id: str = ""
    zendesk_field_ids: str = "{}"

    # Confidence thresholds from the case study, section 3
    high_confidence: float = 0.85
    medium_confidence: float = 0.50
    log_level: str = "INFO"

    def field_ids(self) -> dict[str, int]:
        return {k: int(v) for k, v in json.loads(self.zendesk_field_ids or "{}").items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
