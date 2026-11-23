"""Storage, cache, and SQLite maintenance API endpoints."""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.auth import _clear_session_cookie
from app.config import AppConstants
from app.models.schemas import (
    FactoryResetRequest,
    FactoryResetResponse,
    MaintenanceActionResponse,
    MaintenanceRunAllResponse,
    MaintenanceStatusResponse,
)
from app.services.auth_service import AuthCredentialsError, AuthService
from app.services.folder_sync import FolderReconcileCache
from app.services.maintenance_service import (
    MaintenanceBusyError,
    MaintenanceService,
    StorageUnmountedError,
)

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])

_T = TypeVar("_T")


def _invalidate_all_folder_cache(request: Request) -> None:
    cache: FolderReconcileCache | None = getattr(
        request.app.state, "folder_reconcile_cache", None
    )
    if cache is not None:
        cache.invalidate_all()


async def _run_serialized(
    service: MaintenanceService, fn: Callable[[], _T]
) -> _T:
    """Serialize maintenance actions and map busy/unmounted errors to HTTP 409."""
    if service.op_lock.locked():
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail="Another maintenance operation is already in progress.",
        )
    async with service.op_lock:
        try:
            return await asyncio.to_thread(fn)
        except (MaintenanceBusyError, StorageUnmountedError) as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_CONFLICT,
                detail=str(exc),
            ) from exc


@router.get("/status", response_model=MaintenanceStatusResponse)
async def get_maintenance_status(request: Request) -> MaintenanceStatusResponse:
    """Return read-only DB freelist, temp/upload orphan, and schedule diagnostics."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await asyncio.to_thread(service.get_status)


@router.post("/optimize-db", response_model=MaintenanceActionResponse)
async def optimize_database(request: Request) -> MaintenanceActionResponse:
    """Checkpoint WAL, VACUUM freelist pages, and run PRAGMA optimize."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await _run_serialized(service, service.optimize_database)


@router.post("/clean-temp", response_model=MaintenanceActionResponse)
async def clean_temp_files(request: Request) -> MaintenanceActionResponse:
    """Remove stale orphaned staged uploads and stray `.mkvforge.*` temp files."""
    service: MaintenanceService = request.app.state.maintenance_service
    result = await _run_serialized(service, service.clean_temp_and_uploads)
    _invalidate_all_folder_cache(request)
    return result


@router.post("/prune-missing", response_model=MaintenanceActionResponse)
async def prune_missing_files(request: Request) -> MaintenanceActionResponse:
    """Prune SQLite rows for files no longer on disk (USB-unmount protected)."""
    service: MaintenanceService = request.app.state.maintenance_service
    result = await _run_serialized(service, service.prune_missing_files)
    _invalidate_all_folder_cache(request)
    return result


@router.post("/prune-jobs", response_model=MaintenanceActionResponse)
async def prune_old_jobs(request: Request) -> MaintenanceActionResponse:
    """Prune terminal job history rows older than configured retention days."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await _run_serialized(service, service.prune_old_jobs)


@router.post("/run-all", response_model=MaintenanceRunAllResponse)
async def run_all_maintenance(request: Request) -> MaintenanceRunAllResponse:
    """Execute clean-temp, prune-missing, prune-jobs, and optimize-db sequentially."""
    service: MaintenanceService = request.app.state.maintenance_service
    result = await _run_serialized(service, service.run_all)
    _invalidate_all_folder_cache(request)
    return result


@router.post("/factory-reset", response_model=FactoryResetResponse)
async def factory_reset_app(
    request: Request,
    response: Response,
    payload: FactoryResetRequest,
) -> FactoryResetResponse:
    """Physically rebuild SQLite and wipe app data without touching media files.

    - In `local` auth mode: requires signed-in admin user + valid current password.
    - In `anonymous` / `unconfigured` mode: requires `confirm_text == 'RESET'`.
    """
    auth_service: AuthService = request.app.state.auth_service
    auth_mode = await asyncio.to_thread(auth_service.get_auth_mode)

    if auth_mode == AppConstants.AUTH_MODE_LOCAL:
        current_user = getattr(request.state, "current_user", None)
        if current_user is None or getattr(current_user, "is_anonymous", True):
            raise HTTPException(
                status_code=AppConstants.HTTP_UNAUTHORIZED,
                detail="Sign in as an administrator to perform a factory reset.",
            )
        if getattr(current_user, "role", None) != AppConstants.AUTH_ROLE_ADMIN:
            raise HTTPException(
                status_code=AppConstants.HTTP_FORBIDDEN,
                detail="Administrator privileges are required to perform a factory reset.",
            )
        try:
            await asyncio.to_thread(
                auth_service.verify_user_password,
                user_id=int(current_user.id),
                password=payload.password,
            )
        except AuthCredentialsError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    else:
        if (payload.confirm_text or "").strip() != AppConstants.FACTORY_RESET_CONFIRM_TOKEN:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=f"Type {AppConstants.FACTORY_RESET_CONFIRM_TOKEN} to confirm factory reset.",
            )

    service: MaintenanceService = request.app.state.maintenance_service
    try:
        result = await service.factory_reset(
            job_worker=getattr(request.app.state, "job_worker", None),
            library_watcher=getattr(request.app.state, "library_watcher", None),
            job_broker=getattr(request.app.state, "job_broker", None),
        )
    except MaintenanceBusyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=str(exc),
        ) from exc

    _invalidate_all_folder_cache(request)
    _clear_session_cookie(response, request)
    return result
