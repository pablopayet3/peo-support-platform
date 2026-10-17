"""Settings for the email intake agent (the Forethought stand-in on the email channel)."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # The model only classifies and extracts. Every action is taken by code.
    gemini_api_key: str = ""
    model: str = "gemini-2.5-flash"  # the lite "latest" alias timed out (504) under structured output
    llm_timeout_s: float = 15.0

    integration_base_url: str = "http://localhost:8712"
    integration_api_key: str = ""

    zendesk_subdomain: str = ""
    # Zendesk API access: OAuth client credentials (a confidential OAuth client). The API token pair below
    # is only for accounts that still have API tokens; Zendesk deactivates all of them on April 30, 2027.
    zendesk_oauth_client_id: str = ""
    zendesk_oauth_client_secret: str = ""
    zendesk_oauth_scope: str = "read write"
    zendesk_email: str = ""
    zendesk_api_token: str = ""
    # The signing secret, or the webhook's ID so the agent reads the secret from Zendesk itself.
    zendesk_webhook_secret: str = ""
    zendesk_webhook_id: str = ""
    zendesk_field_ids: str = "{}"

    # Live phone calls: the shared secret the voice platform (Vapi) sends in X-Vapi-Secret
    vapi_secret: str = ""

    # Confidence thresholds from the case study, section 3
    high_confidence: float = 0.85
    medium_confidence: float = 0.50
    log_level: str = "INFO"

    def field_ids(self) -> dict[str, int]:
        return {k: int(v) for k, v in json.loads(self.zendesk_field_ids or "{}").items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
