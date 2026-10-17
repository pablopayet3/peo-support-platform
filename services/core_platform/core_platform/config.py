"""Settings for the core platform. Every value comes from the environment, never from code."""
import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./core.db"
    # JSON: {"<key>": {"client": "integration-service", "scopes": ["identity:read", ...]}}
    api_keys: str = Field(default="{}")
    # Extra scopes per client name, kept apart from the secret keys so permissions can change without
    # touching a key. JSON: {"integration-service": ["billing:read"]}
    client_scopes: str = Field(default="{}")
    # Keys shorter than this are ignored, so an empty or unresolved secret can never match.
    min_key_length: int = 32
    admin_user: str = "admin"
    admin_password: str = ""
    # Public read-only demo: the admin screens open without a password. Only for fictional data.
    admin_public_demo: bool = False
    seed_on_start: bool = True
    # Demo only: give sample people a real inbox (JSON {"P-1001": "you+maria@example.com"}), so a live demo
    # can email in as Maria and receive the agent's replies. Kept out of the repository.
    demo_email_overrides: str = "{}"
    # For demos and tests only: pretend today is this date (YYYY-MM-DD).
    frozen_today: str | None = None
    log_level: str = "INFO"

    def keys(self) -> dict[str, dict]:
        keys = json.loads(self.api_keys or "{}")
        return {k: v for k, v in keys.items() if len(k) >= self.min_key_length and "${{" not in k}

    def extra_scopes(self, client: str) -> list[str]:
        return list(json.loads(self.client_scopes or "{}").get(client, []))

    @property
    def sqlalchemy_url(self) -> str:
        """Hosted Postgres gives postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
