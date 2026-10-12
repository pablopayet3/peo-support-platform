"""Settings for the integration service. Secrets come from the environment (Railway variables)."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INT_", env_file=".env", extra="ignore")

    # Callers of this service. JSON: {"<key>": {"client": "intake-agent", "scopes": ["checks:run"]}}
    api_keys: str = "{}"
    # Keys shorter than this are ignored, so an empty or unresolved secret can never match.
    min_key_length: int = 32

    # The core platform (Engineering-owned)
    core_base_url: str = "http://localhost:8711"
    core_api_key: str = ""
    core_timeout_s: float = 4.0

    # Zendesk
    zendesk_subdomain: str = ""
    # Zendesk API access: OAuth client credentials (a confidential OAuth client). The API token pair below
    # is only for accounts that still have API tokens; Zendesk deactivates all of them on April 30, 2027.
    zendesk_oauth_client_id: str = ""
    zendesk_oauth_client_secret: str = ""
    zendesk_oauth_scope: str = "read write"
    zendesk_email: str = ""
    zendesk_api_token: str = ""
    # Either the webhook's signing secret, or the webhook's ID: with the ID, the service reads the secret
    # from Zendesk with its API token, so nobody copies it by hand and a rotated secret is picked up.
    zendesk_webhook_secret: str = ""
    zendesk_webhook_id: str = ""
    zendesk_timeout_s: float = 8.0
    # JSON: {"life_event_type": 123, "ai_intake_result": 456, "event_date": 789,
    #        "enrollment_outcome": 111, "enrollment_id": 222}
    zendesk_field_ids: str = "{}"
    benefits_ops_group_id: int = 0
    # The sidebar app installation this service keeps configured (its settings, including its key).
    zendesk_app_installation_id: str = ""
    # This service's public URL, as the sidebar app reaches it. Railway sets RAILWAY_PUBLIC_DOMAIN.
    public_base_url: str = ""
    # The Benefits Ops procedure (Confluence), linked from the sidebar's guidance.
    procedure_url: str = ""

    # Salesforce (External Client App, OAuth client credentials). Context only: never blocks a check or an approval.
    salesforce_instance_url: str = ""
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_timeout_s: float = 6.0
    salesforce_seed_demo: bool = False

    # A switch that turns the checks off without touching Zendesk or the AI tools.
    checks_enabled: bool = True
    log_level: str = "INFO"

    def keys(self) -> dict[str, dict]:
        keys = json.loads(self.api_keys or "{}")
        return {k: v for k, v in keys.items() if len(k) >= self.min_key_length and "${{" not in k}

    def field_ids(self) -> dict[str, int]:
        return {k: int(v) for k, v in json.loads(self.zendesk_field_ids or "{}").items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
