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
    admin_user: str = "admin"
    admin_password: str = "change-me"
    seed_on_start: bool = True
    # For demos and tests only: pretend today is this date (YYYY-MM-DD).
    frozen_today: str | None = None
    log_level: str = "INFO"

    def keys(self) -> dict[str, dict]:
        return json.loads(self.api_keys or "{}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
