"""File rename, batch move, and batch delete API endpoints."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.files import _row_to_list_item
from app.api.jobs import _row_to_job_item
from app.config import AppConstants
from app.db.repositories import JobRepository, MediaFileRepository
from app.models.schemas import (
    BatchCopyEnqueueResponse,
    BatchFileOpItemResult,
    BatchFileOpResponse,
    FileRenameRequest,
    FilesCopyRequest,
    FilesDeleteRequest,
    FilesMoveRequest,
    JobConfig,
    JobItemResponse,
    MediaFileListItem,
    OtherFileDeleteRequest,
    OtherFileMoveRequest,
    OtherFileOpResponse,
    OtherFileRenameRequest,
)
from app.services.file_copy_runner import (
    check_copy_disk_space,
    ensure_copy_destination_writable,
    resolve_copy_destination_dir,
)
from app.services.file_ops_service import FileOpError, FileOpsService
from app.services.folder_sync import FolderReconcileCache
from app.services.job_worker import DuplicateJobError, JobWorker
from app.services.preflight import InsufficientDiskSpaceError
from app.services.watcher import LibraryWatcher
from app.utils.paths import PathSafetyError, safe_resolve_path

router = APIRouter(prefix="/api/files", tags=["file-ops"])


def _invalidate_cache_for_paths(request: Request, *paths: str) -> None:
    cache: FolderReconcileCache | None = getattr(
        request.app.state, "folder_reconcile_cache", None
    )
    if cache is None:
        return
    for raw in paths:
        if raw:
            cache.invalidate_for_filepath(raw)


def _raise_if_total_batch_failure(result: BatchFileOpResponse) -> None:
    """Promote total 403 (:ro) or single-item failures to direct HTTP error responses."""
    if result.succeeded > 0 or not result.results:
        return
    codes = {item.status_code for item in result.results}
    if AppConstants.HTTP_FORBIDDEN in codes and len(codes) == 1:
        raise HTTPException(
            status_code=AppConstants.HTTP_FORBIDDEN,
            detail=result.results[0].error or "Permission denied",
        )
    if len(result.results) == 1 and not result.results[0].ok:
        raise HTTPException(
            status_code=result.results[0].status_code,
            detail=result.results[0].error or "File operation failed",
        )


@router.post("/move", response_model=BatchFileOpResponse)
async def move_files(
    request: Request,
    payload: FilesMoveRequest,
) -> BatchFileOpResponse:
    """Move one or more indexed media files to `destination_folder`."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        result = await asyncio.to_thread(
            file_ops.move_files,
            payload.file_ids,
            payload.destination_folder,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _raise_if_total_batch_failure(result)
    cache: FolderReconcileCache | None = getattr(
        request.app.state, "folder_reconcile_cache", None
    )
    if cache is not None and result.succeeded > 0:
        cache.invalidate_folder(payload.destination_folder, include_parent=True)
        for item in result.results:
            if item.ok:
                if item.filepath:
                    cache.invalidate_for_filepath(item.filepath)
                if item.new_filepath:
                    cache.invalidate_for_filepath(item.new_filepath)
    return result


@router.post(
    "/copy",
    response_model=BatchCopyEnqueueResponse,
    status_code=AppConstants.HTTP_ACCEPTED,
)
async def copy_files(
    request: Request,
    payload: FilesCopyRequest,
) -> JSONResponse:
    """Validate targets and enqueue background copy job(s) into `destination_folder`."""
    settings = request.app.state.settings
    media_repo: MediaFileRepository = request.app.state.media_repo
    job_repo: JobRepository = request.app.state.job_repo
    job_worker: JobWorker = request.app.state.job_worker

    try:
        dst_dir, normalized_dst = await asyncio.to_thread(
            resolve_copy_destination_dir,
            payload.destination_folder,
            settings.storage_dir,
        )
        await asyncio.to_thread(ensure_copy_destination_writable, dst_dir)
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    unique_ids = list(dict.fromkeys(payload.file_ids))
    results: list[BatchFileOpItemResult] = []
    valid_targets: list[tuple[int, str, int]] = []
    failed = 0

    for file_id in unique_ids:
        row = await asyncio.to_thread(media_repo.get_by_id, file_id)
        if row is None:
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    ok=False,
                    status_code=AppConstants.HTTP_NOT_FOUND,
                    error=f"Media file {file_id} not found",
                )
            )
            continue

        rel_filepath = str(row["filepath"])
        if (
            str(row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
            == AppConstants.FILE_SCAN_STATUS_SCANNING
        ):
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_CONFLICT,
                    error=f"Cannot copy {row['filename']} while metadata scan is in progress",
                )
            )
            continue

        active_job = await asyncio.to_thread(
            job_repo.get_active_job_for_filepath, rel_filepath
        )
        if active_job is not None:
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_CONFLICT,
                    error=f"Active or queued job exists for {row['filename']}",
                )
            )
            continue

        try:
            src_path = await asyncio.to_thread(
                safe_resolve_path, rel_filepath, settings.storage_dir
            )
        except PathSafetyError as exc:
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_BAD_REQUEST,
                    error=str(exc),
                )
            )
            continue

        if not src_path.is_file():
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_NOT_FOUND,
                    error=f"File missing on disk: {rel_filepath}",
                )
            )
            continue

        size_bytes = int(src_path.stat().st_size)
        valid_targets.append((file_id, rel_filepath, size_bytes))

    if valid_targets:
        total_required = sum(size for _, _, size in valid_targets)
        try:
            await asyncio.to_thread(check_copy_disk_space, total_required, dst_dir)
        except InsufficientDiskSpaceError as exc:
            raise HTTPException(
                status_code=AppConstants.HTTP_INSUFFICIENT_STORAGE,
                detail=str(exc),
            ) from exc
        except FileOpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

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

    batch_id = (
        f"{AppConstants.BATCH_ID_PREFIX}{uuid.uuid4().hex[:AppConstants.BATCH_ID_HEX_LEN]}"
        if len(valid_targets) > 1
        else None
    )

    enqueued_jobs: list[JobItemResponse] = []
    copy_cfg = JobConfig(
        operation=AppConstants.JOB_OP_COPY,
        copy_destination_folder=normalized_dst,
    )
    for file_id, rel_filepath, _size in valid_targets:
        try:
            job_row = await job_worker.enqueue_job(
                filepath=rel_filepath,
                config=copy_cfg,
                created_by_user_id=created_by_user_id,
                created_by_name=created_by_name,
                batch_id=batch_id,
            )
            enqueued_jobs.append(_row_to_job_item(job_row))
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=True,
                    status_code=AppConstants.HTTP_ACCEPTED,
                )
            )
        except DuplicateJobError as exc:
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_CONFLICT,
                    error=str(exc),
                )
            )
        except PathSafetyError as exc:
            failed += 1
            results.append(
                BatchFileOpItemResult(
                    file_id=file_id,
                    filepath=rel_filepath,
                    ok=False,
                    status_code=AppConstants.HTTP_BAD_REQUEST,
                    error=str(exc),
                )
            )

    if len(enqueued_jobs) == 0 and results:
        if len(results) == 1 and not results[0].ok:
            raise HTTPException(
                status_code=results[0].status_code,
                detail=results[0].error or "File copy failed",
            )
        codes = {item.status_code for item in results}
        if len(codes) == 1:
            raise HTTPException(
                status_code=results[0].status_code,
                detail=results[0].error or "File copy failed",
            )

    body = BatchCopyEnqueueResponse(
        enqueued=len(enqueued_jobs),
        failed=failed,
        batch_id=batch_id if len(enqueued_jobs) > 1 else None,
        jobs=enqueued_jobs,
        results=results,
    )
    return JSONResponse(
        status_code=AppConstants.HTTP_ACCEPTED,
        content=body.model_dump(),
    )


@router.post("/delete", response_model=BatchFileOpResponse)
async def delete_files(
    request: Request,
    payload: FilesDeleteRequest,
) -> BatchFileOpResponse:
    """Delete one or more media files from disk and SQLite (403 on :ro volumes)."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        result = await asyncio.to_thread(
            file_ops.delete_files,
            payload.file_ids,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _raise_if_total_batch_failure(result)
    if result.succeeded > 0:
        _invalidate_cache_for_paths(
            request, *(item.filepath or "" for item in result.results if item.ok)
        )
    return result


@router.post("/other/rename", response_model=OtherFileOpResponse)
async def rename_other_file(
    request: Request,
    payload: OtherFileRenameRequest,
) -> OtherFileOpResponse:
    """Rename a non-indexed file on disk; triggers background probe if renamed to .mkv/.mp4."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        result = await asyncio.to_thread(
            file_ops.rename_other_file,
            payload.filepath,
            payload.new_name,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _invalidate_cache_for_paths(request, payload.filepath, result.new_filepath or "")
    if result.promoted_to_media and result.new_filepath:
        watcher: LibraryWatcher | None = getattr(
            request.app.state, "library_watcher", None
        )
        if watcher is not None:
            watcher.schedule_background_probe(result.new_filepath)
    return result


@router.post("/other/move", response_model=OtherFileOpResponse)
async def move_other_file(
    request: Request,
    payload: OtherFileMoveRequest,
) -> OtherFileOpResponse:
    """Move a non-indexed file into `destination_folder`."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        result = await asyncio.to_thread(
            file_ops.move_other_file,
            payload.filepath,
            payload.destination_folder,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    _invalidate_cache_for_paths(request, payload.filepath, result.new_filepath or "")
    cache: FolderReconcileCache | None = getattr(
        request.app.state, "folder_reconcile_cache", None
    )
    if cache is not None:
        cache.invalidate_folder(payload.destination_folder, include_parent=True)
    return result


@router.post("/other/delete", response_model=OtherFileOpResponse)
async def delete_other_file(
    request: Request,
    payload: OtherFileDeleteRequest,
) -> OtherFileOpResponse:
    """Delete a non-indexed file from disk."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        result = await asyncio.to_thread(
            file_ops.delete_other_file,
            payload.filepath,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    _invalidate_cache_for_paths(request, payload.filepath)
    return result


@router.post("/{file_id}/rename", response_model=MediaFileListItem)
async def rename_file(
    request: Request,
    file_id: int,
    payload: FileRenameRequest,
) -> MediaFileListItem:
    """Rename a single media file on disk and update SQLite immediately."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        updated = await asyncio.to_thread(
            file_ops.rename_file,
            file_id,
            payload.new_name,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _invalidate_cache_for_paths(request, str(updated.get("filepath") or ""))
    return _row_to_list_item(updated)


@router.post("/{file_id}/duplicate", response_model=MediaFileListItem)
async def duplicate_file(
    request: Request,
    file_id: int,
) -> MediaFileListItem:
    """Duplicate a single media file in its current folder (`<name> (1).ext`) and index it."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        created = await asyncio.to_thread(
            file_ops.duplicate_file,
            file_id,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _invalidate_cache_for_paths(request, str(created.get("filepath") or ""))
    return _row_to_list_item(created)
