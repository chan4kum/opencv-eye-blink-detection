"""Typed application state shared by routes and lifespan."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from eye_blink.config import Settings
    from eye_blink.jobs import JobBus
    from eye_blink.service import BlinkService
    from eye_blink.storage import ObjectStore

_STORAGE_CHECK_TTL_S = 5.0


@dataclass
class AppState:
    settings: Settings
    service: BlinkService
    bus: JobBus | None = None
    store: ObjectStore | None = None
    shutting_down: bool = False
    active_streams: int = 0
    _storage_ok_until: float = field(default=0.0, repr=False)

    async def storage_healthy(self) -> bool:
        """Cached bucket probe so readiness checks do not hammer the object store."""
        if self.store is None:
            return True
        now = time.monotonic()
        if now < self._storage_ok_until:
            return True
        try:
            await self.store.check()
        except Exception:
            return False
        self._storage_ok_until = now + _STORAGE_CHECK_TTL_S
        return True


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.eb
    return state
