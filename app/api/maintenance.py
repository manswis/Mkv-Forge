"""Storage, cache, and SQLite maintenance API endpoints."""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Request

from app.config import AppConstants
from app.models.schemas import (
    MaintenanceActionResponse,
    MaintenanceRunAllResponse,
    MaintenanceStatusResponse,
)
from app.services.maintenance_service import (
    MaintenanceBusyError,
    MaintenanceService,
    StorageUnmountedError,
)

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])

_T = TypeVar("_T")


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
    return await _run_serialized(service, service.clean_temp_and_uploads)


@router.post("/prune-missing", response_model=MaintenanceActionResponse)
async def prune_missing_files(request: Request) -> MaintenanceActionResponse:
    """Prune SQLite rows for files no longer on disk (USB-unmount protected)."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await _run_serialized(service, service.prune_missing_files)


@router.post("/prune-jobs", response_model=MaintenanceActionResponse)
async def prune_old_jobs(request: Request) -> MaintenanceActionResponse:
    """Prune terminal job history rows older than configured retention days."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await _run_serialized(service, service.prune_old_jobs)


@router.post("/run-all", response_model=MaintenanceRunAllResponse)
async def run_all_maintenance(request: Request) -> MaintenanceRunAllResponse:
    """Execute clean-temp, prune-missing, prune-jobs, and optimize-db sequentially."""
    service: MaintenanceService = request.app.state.maintenance_service
    return await _run_serialized(service, service.run_all)
