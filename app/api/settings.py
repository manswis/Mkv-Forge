"""User settings REST API endpoints (GET / PATCH /api/settings)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.config import AppConstants, Settings
from app.models.schemas import (
    ProcessingRuntimeInfo,
    ScratchLocationInfo,
    UserSettings,
    UserSettingsUpdate,
)
from app.services.processing_policy import (
    check_directory_writable,
    detect_cpu_count,
    get_tmp_scratch_dir,
    inspect_scratch_location,
    is_ionice_available,
    normalize_storage_scratch_folder,
    resolve_ffmpeg_threads,
)
from app.services.user_settings import UserSettingsService

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _effective_user_id(request: Request) -> int | None:
    current = getattr(request.state, "current_user", None)
    if current is None or getattr(current, "is_anonymous", True):
        return None
    return int(current.id)


def _build_processing_runtime_info_sync(
    app_settings: Settings,
    service: UserSettingsService,
) -> ProcessingRuntimeInfo:
    """Collect host capabilities and live mount telemetry for all Stage 1 scratch options."""
    cpus = detect_cpu_count()
    current_settings = service.get(user_id=None)

    storage_folder_path: Path | None = None
    if current_settings.stage1_scratch_storage_folder:
        try:
            _, storage_folder_path = normalize_storage_scratch_folder(
                current_settings.stage1_scratch_storage_folder,
                storage_root=app_settings.storage_dir,
            )
        except ValueError:
            storage_folder_path = None

    tmp_dir = get_tmp_scratch_dir()
    data_dir = app_settings.data_scratch_dir
    custom_dir = app_settings.scratch_dir

    locations: dict[str, ScratchLocationInfo] = {
        AppConstants.STAGE1_SCRATCH_SOURCE: inspect_scratch_location(
            "source",
            app_settings.storage_dir,
            storage_root=app_settings.storage_dir,
        ),
        AppConstants.STAGE1_SCRATCH_TMP: inspect_scratch_location(
            "tmp",
            tmp_dir,
            storage_root=app_settings.storage_dir,
        ),
        AppConstants.STAGE1_SCRATCH_DATA: inspect_scratch_location(
            "data",
            data_dir,
            storage_root=app_settings.storage_dir,
        ),
        AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER: inspect_scratch_location(
            "storage_folder",
            storage_folder_path,
            storage_root=app_settings.storage_dir,
        ),
        AppConstants.STAGE1_SCRATCH_CUSTOM: inspect_scratch_location(
            "custom",
            custom_dir,
            storage_root=app_settings.storage_dir,
        ),
    }

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
        storage_dir_path=str(app_settings.storage_dir),
        tmp_scratch_path=str(tmp_dir),
        data_scratch_path=str(data_dir),
        storage_folder_scratch_path=(
            str(storage_folder_path) if storage_folder_path is not None else None
        ),
        custom_scratch_path=(
            str(custom_dir) if custom_dir is not None else None
        ),
        scratch_locations=locations,
    )


def _validate_and_normalize_scratch_update_sync(
    payload: UserSettingsUpdate,
    *,
    app_settings: Settings,
    service: UserSettingsService,
) -> UserSettingsUpdate:
    """Validate scratch mode and folder writability/safety before persisting settings."""
    normalized_payload = payload
    current_settings = service.get(user_id=None)

    effective_storage_folder = current_settings.stage1_scratch_storage_folder
    if payload.stage1_scratch_storage_folder is not None:
        raw_folder = payload.stage1_scratch_storage_folder.strip()
        if not raw_folder:
            effective_storage_folder = None
            normalized_payload = payload.model_copy(
                update={"stage1_scratch_storage_folder": ""}
            )
        else:
            try:
                rel_folder, abs_folder = normalize_storage_scratch_folder(
                    raw_folder,
                    storage_root=app_settings.storage_dir,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=AppConstants.HTTP_BAD_REQUEST,
                    detail=str(exc),
                ) from exc

            exists_as_dir, writable = check_directory_writable(abs_folder)
            if not exists_as_dir:
                raise HTTPException(
                    status_code=AppConstants.HTTP_BAD_REQUEST,
                    detail=(
                        f"Selected temp folder '/{rel_folder}' does not exist inside /storage."
                    ),
                )
            if not writable:
                raise HTTPException(
                    status_code=AppConstants.HTTP_BAD_REQUEST,
                    detail=(
                        f"Selected temp folder '/{rel_folder}' is read-only or not writable."
                    ),
                )
            effective_storage_folder = rel_folder
            normalized_payload = payload.model_copy(
                update={"stage1_scratch_storage_folder": rel_folder}
            )

    target_mode = payload.stage1_scratch_mode
    if target_mode == AppConstants.STAGE1_SCRATCH_CUSTOM:
        if app_settings.scratch_dir is None:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=(
                    "Custom scratch directory is not configured. "
                    f"Set {AppConstants.ENV_SCRATCH_DIR} in your container environment."
                ),
            )
        _, writable = check_directory_writable(app_settings.scratch_dir)
        if not writable:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=(
                    f"Custom scratch directory '{app_settings.scratch_dir}' is read-only or not writable."
                ),
            )
    elif target_mode == AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER:
        if not effective_storage_folder:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail="Choose a subfolder inside /storage before enabling Storage Temp Folder mode.",
            )
        try:
            rel_folder, abs_folder = normalize_storage_scratch_folder(
                effective_storage_folder,
                storage_root=app_settings.storage_dir,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        exists_as_dir, writable = check_directory_writable(abs_folder)
        if not exists_as_dir:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=(
                    f"Selected temp folder '/{rel_folder}' does not exist inside /storage."
                ),
            )
        if not writable:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=(
                    f"Selected temp folder '/{rel_folder}' is read-only or not writable."
                ),
            )
    elif target_mode == AppConstants.STAGE1_SCRATCH_TMP:
        _, writable = check_directory_writable(get_tmp_scratch_dir())
        if not writable:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail="Container /tmp scratch directory is read-only or not writable.",
            )
    elif target_mode == AppConstants.STAGE1_SCRATCH_DATA:
        _, writable = check_directory_writable(app_settings.data_scratch_dir)
        if not writable:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=(
                    f"App Data scratch directory '{app_settings.data_scratch_dir}' is read-only or not writable."
                ),
            )

    return normalized_payload


@router.get("/runtime", response_model=ProcessingRuntimeInfo)
async def get_processing_runtime_info(request: Request) -> ProcessingRuntimeInfo:
    """Return host CPU count, ionice capability, and live Stage 1 scratch mount diagnostics."""
    app_settings: Settings = request.app.state.settings
    service: UserSettingsService = request.app.state.user_settings_service
    return await asyncio.to_thread(
        _build_processing_runtime_info_sync, app_settings, service
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
    service: UserSettingsService = request.app.state.user_settings_service
    validated_payload = await asyncio.to_thread(
        _validate_and_normalize_scratch_update_sync,
        payload,
        app_settings=app_settings,
        service=service,
    )
    uid = _effective_user_id(request)
    return await asyncio.to_thread(service.update, validated_payload, user_id=uid)
