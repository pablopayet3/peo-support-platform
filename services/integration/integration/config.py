"""Settings for the integration service. Secrets come from the environment (Railway variables)."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INT_", env_file=".env", extra="ignore")

    # Callers of this service. JSON: {"<key>": {"client": "intake-agent", "scopes": ["checks:run"]}}
    api_keys: str = "{}"

    # The core platform (Engineering-owned)
    core_base_url: str = "http://localhost:8711"
    core_api_key: str = ""
    core_timeout_s: float = 4.0

    # Zendesk
    zendesk_subdomain: str = ""
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

    # A switch that turns the checks off without touching Zendesk or the AI tools.
    checks_enabled: bool = True
    log_level: str = "INFO"

    def keys(self) -> dict[str, dict]:
        return json.loads(self.api_keys or "{}")

    def field_ids(self) -> dict[str, int]:
        return {k: int(v) for k, v in json.loads(self.zendesk_field_ids or "{}").items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
