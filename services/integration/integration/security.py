"""Callers of the integration service: the AI intake agents and the Zendesk sidebar app. One key each."""
import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from .config import get_settings


@dataclass(frozen=True)
class Caller:
    client: str
    scopes: frozenset[str]


def require_scope(scope: str):
    def dependency(authorization: str | None = Header(default=None)) -> Caller:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer key")
        presented = authorization.split(" ", 1)[1].strip().encode()
        for key, info in get_settings().keys().items():
            if hmac.compare_digest(presented, key.encode()):
                caller = Caller(info.get("client", "unknown"), frozenset(info.get("scopes", [])))
                if scope not in caller.scopes:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope {scope}")
                return caller
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid key")
    return dependency
