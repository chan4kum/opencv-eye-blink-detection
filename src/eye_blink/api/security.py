"""API-key authentication. Only SHA-256 digests of keys are ever configured or compared."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from eye_blink.config import Settings
from eye_blink.errors import UnauthorizedError

_bearer = HTTPBearer(auto_error=False, description="API key, sent as `Authorization: Bearer <key>`")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. ``id`` scopes async-job ownership."""

    id: str


ANONYMOUS = Principal(id="anonymous")


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    settings: Settings = request.app.state.settings
    if not settings.api_key_hashes:
        return ANONYMOUS  # auth explicitly disabled (dev/test, or enforced upstream)
    if credentials is None:
        raise UnauthorizedError("missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    matched: str | None = None
    for known in settings.api_key_hashes:  # no early exit: constant work per known key
        if hmac.compare_digest(digest, known):
            matched = known
    if matched is None:
        raise UnauthorizedError("invalid API key", headers={"WWW-Authenticate": "Bearer"})
    return Principal(id=matched[:16])


CurrentPrincipal = Annotated[Principal, Depends(authenticate)]


WS_SUBPROTOCOL = "bearer"


def authenticate_websocket(websocket: WebSocket) -> tuple[Principal, str | None]:
    """Authenticate a WebSocket handshake. Returns ``(principal, subprotocol_to_accept)``.

    Accepts ``Authorization: Bearer <key>`` (non-browser clients) or, for browsers that cannot set
    headers, ``Sec-WebSocket-Protocol: bearer, <key>``. Keys are never accepted in the URL, where they
    would end up in access logs.
    """
    settings: Settings = websocket.app.state.settings
    if not settings.api_key_hashes:
        return ANONYMOUS, None
    key: str | None = None
    subprotocol: str | None = None
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        key = header[7:].strip()
    else:
        protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        if len(protocols) == 2 and protocols[0] == WS_SUBPROTOCOL:
            key, subprotocol = protocols[1], WS_SUBPROTOCOL
    if not key:
        raise UnauthorizedError("missing bearer token")
    digest = hashlib.sha256(key.encode()).hexdigest()
    matched: str | None = None
    for known in settings.api_key_hashes:
        if hmac.compare_digest(digest, known):
            matched = known
    if matched is None:
        raise UnauthorizedError("invalid API key")
    return Principal(id=matched[:16]), subprotocol
