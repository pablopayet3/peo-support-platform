"""Settings for the email intake agent (the Forethought stand-in on the email channel)."""
import json
from functools import lru_cache

from pydantic import AliasChoices, Field
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
    # The voice AI's phone number. Zendesk Talk shows it as the caller when the AI transfers a call.
    voice_line: str = "+18604193002"

    # Chat demo: the Northwind employee portal page with the Zendesk messaging widget, logged in as a sample
    # employee. The portal signs a messaging JWT (Admin Center > Messaging > authentication signing key).
    portal_demo: bool = False
    widget_key: str = ""
    messaging_key_id: str = ""
    # Railway would not rename the variable once created, so the short spelling is accepted too.
    messaging_secret: str = Field(default="", validation_alias=AliasChoices(
        "AGENT_MESSAGING_SECRET", "AGENT_MESSAGING_SECRE", "messaging_secret"))

    # Chat: the agent as first responder in Zendesk messaging, through the Conversations API
    sunco_app_id: str = ""
    sunco_key_id: str = ""
    sunco_key_secret: str = ""
    sunco_first_responder: bool = False   # set up the webhook and the switchboard order at startup
    public_base_url: str = ""             # defaults to https://$RAILWAY_PUBLIC_DOMAIN

    # Confidence thresholds from the case study, section 3
    high_confidence: float = 0.85
    medium_confidence: float = 0.50
    log_level: str = "INFO"

    def field_ids(self) -> dict[str, int]:
        return {k: int(v) for k, v in json.loads(self.zendesk_field_ids or "{}").items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
