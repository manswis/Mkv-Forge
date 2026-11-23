"""File rename, batch move, and batch delete API endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.api.files import _row_to_list_item
from app.config import AppConstants
from app.models.schemas import (
    BatchFileOpResponse,
    FileRenameRequest,
    FilesDeleteRequest,
    FilesMoveRequest,
    MediaFileListItem,
    OtherFileDeleteRequest,
    OtherFileMoveRequest,
    OtherFileOpResponse,
    OtherFileRenameRequest,
)
from app.services.file_ops_service import FileOpError, FileOpsService
from app.services.folder_sync import FolderReconcileCache
from app.services.watcher import LibraryWatcher
from app.utils.paths import PathSafetyError

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
