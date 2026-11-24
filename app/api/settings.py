"""User settings REST API endpoints (GET / PATCH /api/settings)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.config import AppConstants, Settings
from app.models.schemas import (
    ProcessingRuntimeInfo,
    UserSettings,
    UserSettingsUpdate,
)
from app.services.processing_policy import (
    detect_cpu_count,
    get_tmp_scratch_dir,
    is_ionice_available,
    resolve_ffmpeg_threads,
)
from app.services.user_settings import UserSettingsService

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _effective_user_id(request: Request) -> int | None:
    current = getattr(request.state, "current_user", None)
    if current is None or getattr(current, "is_anonymous", True):
        return None
    return int(current.id)


@router.get("/runtime", response_model=ProcessingRuntimeInfo)
async def get_processing_runtime_info(request: Request) -> ProcessingRuntimeInfo:
    """Return host CPU count, ionice capability, and resolved scratch directory paths."""
    app_settings: Settings = request.app.state.settings
    cpus = detect_cpu_count()
    return ProcessingRuntimeInfo(
        cpu_count=cpus,
        half_cores_threads=resolve_ffmpeg_threads(
            AppConstants.PROCESSING_PRIORITY_HALF_CORES, cpu_count=cpus
        )
        or AppConstants.MIN_FFMPEG_THREADS,
        low_priority_threads=resolve_ffmpeg_threads(
            AppConstants.PROCESSING_PRIORITY_LOW, cpu_count=cpus
        )
        or AppConstants.MIN_FFMPEG_THREADS,
        ionice_available=is_ionice_available(),
        tmp_scratch_path=str(get_tmp_scratch_dir()),
        data_scratch_path=str(app_settings.data_scratch_dir),
        custom_scratch_path=(
            str(app_settings.scratch_dir)
            if app_settings.scratch_dir is not None
            else None
        ),
    )


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
    app_settings: Settings = request.app.state.settings
    if (
        payload.stage1_scratch_mode == AppConstants.STAGE1_SCRATCH_CUSTOM
        and app_settings.scratch_dir is None
    ):
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=(
                "Custom scratch directory is not configured. "
                f"Set {AppConstants.ENV_SCRATCH_DIR} in your container environment."
            ),
        )
    service: UserSettingsService = request.app.state.user_settings_service
    uid = _effective_user_id(request)
    return await asyncio.to_thread(service.update, payload, user_id=uid)
