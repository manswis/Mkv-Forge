"""Job queue management and real-time SSE progress API endpoints."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.config import AppConstants
from app.db.repositories import (
    JobRepository,
    MediaFileRepository,
    decode_warnings_json,
)
from app.models.schemas import (
    JobCreateRequest,
    JobHistoryDeleteResponse,
    JobItemResponse,
    JobListResponse,
)
from app.services.job_summary import summarize_job_changes
from app.services.job_worker import (
    DuplicateJobError,
    JobNotCancellableError,
    JobWorker,
)
from app.services.preflight import (
    InvalidAudioConversionError,
    validate_audio_conversion_pair,
)
from app.services.sse_broker import JobSSEBroker
from app.services.upload_service import (
    UploadNotFoundError,
    UploadService,
    UploadValidationError,
)
from app.utils.paths import (
    PathSafetyError,
    ensure_save_as_target_available,
    safe_resolve_path,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _row_to_job_item(row: dict[str, Any]) -> JobItemResponse:
    """Convert a SQLite jobs row into a JobItemResponse schema with change summary."""
    filepath = str(row["filepath"])
    output_filepath = row.get("output_filepath")
    output_mode, changes = summarize_job_changes(
        row.get("config_json"),
        filepath=filepath,
        output_filepath=str(output_filepath) if output_filepath else None,
    )
    return JobItemResponse(
        id=str(row["id"]),
        filepath=filepath,
        output_filepath=output_filepath,
        output_mode=output_mode,
        status=str(row["status"]),
        stage=row.get("stage"),
        progress=int(row.get("progress") or 0),
        changes=changes,
        warnings=decode_warnings_json(row.get("warnings_json")),
        error_message=row.get("error_message"),
        created_at=str(row["created_at"]),
        completed_at=row.get("completed_at"),
        created_by_name=str(
            row.get("created_by_name") or AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME
        ),
    )


@router.post(
    "",
    response_model=JobItemResponse,
    status_code=AppConstants.HTTP_ACCEPTED,
)
async def create_job(
    request: Request,
    payload: JobCreateRequest,
) -> JSONResponse:
    """Validate target file, guard against duplicates (409), and enqueue a job."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    job_worker: JobWorker = request.app.state.job_worker

    target_filepath: str
    media_row: dict[str, Any] | None = None
    if payload.file_id is not None:
        media_row = await asyncio.to_thread(media_repo.get_by_id, payload.file_id)
        if media_row is None:
            raise HTTPException(
                status_code=AppConstants.HTTP_NOT_FOUND,
                detail=f"Media file {payload.file_id} not found",
            )
        target_filepath = str(media_row["filepath"])
    else:
        assert payload.filepath is not None
        target_filepath = payload.filepath.strip()
        media_row = await asyncio.to_thread(
            media_repo.get_by_filepath, target_filepath
        )

    if (
        media_row is not None
        and str(media_row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
        == AppConstants.FILE_SCAN_STATUS_SCANNING
    ):
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=f"Cannot process {media_row['filename']} while metadata scan is still in progress.",
        )

    if payload.config.audio_conversion.enabled:
        try:
            validate_audio_conversion_pair(
                payload.config.audio_conversion.source_codec,
                payload.config.audio_conversion.target_codec,
                source_channels=payload.config.audio_conversion.source_channels,
                target_channels=payload.config.audio_conversion.target_channels,
            )
        except InvalidAudioConversionError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    if payload.config.external_tracks:
        upload_service: UploadService = request.app.state.upload_service
        try:
            await asyncio.to_thread(
                upload_service.resolve_external_tracks,
                payload.config.external_tracks,
            )
        except UploadValidationError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except UploadNotFoundError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_NOT_FOUND,
                detail=str(exc),
            ) from exc

    if payload.config.output_mode == AppConstants.OUTPUT_MODE_SAVE_AS:
        settings = request.app.state.settings
        try:
            resolved_source = safe_resolve_path(target_filepath, settings.storage_dir)
            assert payload.config.output_path is not None
            ensure_save_as_target_available(
                payload.config.output_path,
                settings.storage_dir,
                source_path=resolved_source,
            )
        except PathSafetyError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except FileExistsError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_CONFLICT,
                detail=str(exc),
            ) from exc

    current_user = getattr(request.state, "current_user", None)
    created_by_user_id = (
        int(current_user.id)
        if current_user is not None and not getattr(current_user, "is_anonymous", True)
        else None
    )
    created_by_name = (
        str(current_user.display_name)
        if current_user is not None and getattr(current_user, "display_name", "")
        else AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME
    )

    try:
        job_row = await job_worker.enqueue_job(
            filepath=target_filepath,
            config=payload.config,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except DuplicateJobError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=str(exc),
        ) from exc

    return JSONResponse(
        status_code=AppConstants.HTTP_ACCEPTED,
        content=_row_to_job_item(job_row).model_dump(),
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(request: Request) -> JobListResponse:
    """Return active job, queued jobs, and recent job history."""
    job_repo: JobRepository = request.app.state.job_repo

    active_row = await asyncio.to_thread(job_repo.get_active)
    queued_rows = await asyncio.to_thread(job_repo.list_queued)
    history_rows = await asyncio.to_thread(
        job_repo.list_history, AppConstants.JOB_HISTORY_LIMIT
    )

    return JobListResponse(
        active=_row_to_job_item(active_row) if active_row is not None else None,
        queued=[_row_to_job_item(row) for row in queued_rows],
        history=[_row_to_job_item(row) for row in history_rows],
    )


@router.delete("/history", response_model=JobHistoryDeleteResponse)
async def clear_job_history(request: Request) -> JobHistoryDeleteResponse:
    """Remove all terminal (`completed`, `failed`, `cancelled`) jobs from history."""
    job_repo: JobRepository = request.app.state.job_repo
    deleted_count = await asyncio.to_thread(job_repo.clear_history)
    return JobHistoryDeleteResponse(deleted=deleted_count)


@router.delete("/history/{job_id}", response_model=JobHistoryDeleteResponse)
async def delete_job_history_item(
    request: Request, job_id: str
) -> JobHistoryDeleteResponse:
    """Remove one terminal job entry from history (refuses active/queued jobs)."""
    job_repo: JobRepository = request.app.state.job_repo
    existing = await asyncio.to_thread(job_repo.get_by_id, job_id)
    if existing is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )
    deleted = await asyncio.to_thread(job_repo.delete_history_item, job_id)
    if not deleted:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=f"Job {job_id} is active or queued; cancel it before removing from history",
        )
    return JobHistoryDeleteResponse(deleted=1, id=job_id)


@router.delete("/{job_id}", response_model=JobItemResponse)
async def cancel_job(request: Request, job_id: str) -> JobItemResponse:
    """Cancel a queued or running job and clean up subprocesses/temp files."""
    job_worker: JobWorker = request.app.state.job_worker

    try:
        updated = await job_worker.cancel_job(job_id)
    except JobNotCancellableError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=str(exc),
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    return _row_to_job_item(updated)


@router.get("/stream")
async def stream_job_progress(request: Request) -> EventSourceResponse:
    """Stream live job progress and CLI logs over keepalive-protected SSE."""
    broker: JobSSEBroker = request.app.state.job_broker

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        async for event in broker.subscribe():
            if await request.is_disconnected():
                break
            yield {
                "event": AppConstants.SSE_JOB_EVENT_NAME,
                "data": event.model_dump_json(),
            }

    return EventSourceResponse(
        _event_generator(),
        ping=AppConstants.SSE_PING_INTERVAL_SEC,
        headers={
            AppConstants.SSE_ACCEL_BUFFERING_HEADER: AppConstants.SSE_ACCEL_BUFFERING_VALUE,
        },
    )
