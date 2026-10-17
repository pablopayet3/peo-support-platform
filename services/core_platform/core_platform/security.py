"""Who is calling, and what are they allowed to do.

Each internal caller has its own key with a fixed list of scopes. Keys live in the secrets store
(Railway variables here) and are compared in constant time. The next step in production would be
OAuth 2.0 client credentials with short-lived tokens; the scope checks stay the same.
"""
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import get_settings


@dataclass(frozen=True)
class Caller:
    client: str
    scopes: frozenset[str]


def _match_key(presented: str) -> Caller | None:
    for key, info in get_settings().keys().items():
        if hmac.compare_digest(presented.encode(), key.encode()):
            client = info.get("client", "unknown")
            scopes = set(info.get("scopes", [])) | set(get_settings().extra_scopes(client))
            return Caller(client=client, scopes=frozenset(scopes))
    return None


def require_scope(scope: str):
    def dependency(authorization: str | None = Header(default=None)) -> Caller:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer key")
        caller = _match_key(authorization.split(" ", 1)[1].strip())
        if caller is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid key")
        if scope not in caller.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope {scope}")
        return caller
    return dependency


_basic = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    s = get_settings()
    if s.admin_public_demo:
        return "public-demo"
    ok = bool(credentials and s.admin_password) and secrets.compare_digest(
        credentials.username, s.admin_user) and secrets.compare_digest(credentials.password, s.admin_password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def request_id(request: Request) -> str:
    return request.state.request_id
