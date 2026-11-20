"""User settings REST API endpoints (GET / PATCH /api/settings)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from app.models.schemas import UserSettings, UserSettingsUpdate
from app.services.user_settings import UserSettingsService

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _effective_user_id(request: Request) -> int | None:
    current = getattr(request.state, "current_user", None)
    if current is None or getattr(current, "is_anonymous", True):
        return None
    return int(current.id)


@router.get("", response_model=UserSettings)
async def get_user_settings(request: Request) -> UserSettings:
    """Return current user-configurable UI and workflow preferences."""
    service: UserSettingsService = request.app.state.user_settings_service
    uid = _effective_user_id(request)
    return await asyncio.to_thread(service.get, user_id=uid)


@router.patch("", response_model=UserSettings)
async def patch_user_settings(
    request: Request, payload: UserSettingsUpdate
) -> UserSettings:
    """Partially update and persist hybrid user/system preferences in SQLite."""
    service: UserSettingsService = request.app.state.user_settings_service
    uid = _effective_user_id(request)
    return await asyncio.to_thread(service.update, payload, user_id=uid)
