"""Lazy storage directory tree browser and subfolder creation API endpoints."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import AppConstants
from app.models.schemas import (
    FolderCreateRequest,
    FolderDeleteRequest,
    FolderListResponse,
    FolderMoveRequest,
    FolderOpResponse,
    FolderRenameRequest,
    FolderTrackFileEntry,
    FolderTreeEntry,
)
from app.services.file_ops_service import FileOpError, FileOpsService
from app.services.folder_service import (
    FolderAlreadyExistsError,
    FolderNotFoundError,
    FolderPermissionError,
    create_storage_subfolder,
    list_storage_folder_and_tracks,
    list_storage_subfolders,
)
from app.utils.paths import PathSafetyError

router = APIRouter(prefix="/api/folders", tags=["folders"])


@router.get("", response_model=FolderListResponse)
async def list_folders_tree(
    request: Request,
    path: str = Query(default="/"),
    include_files: bool = Query(default=False),
    kind: Literal["audio", "subtitles"] | None = Query(default=None),
) -> FolderListResponse:
    """Return immediate subdirectories (and optional external track files) inside STORAGE_DIR."""
    storage_dir = request.app.state.settings.storage_dir
    try:
        if include_files:
            normalized_path, children, track_files = await asyncio.to_thread(
                list_storage_folder_and_tracks,
                path,
                storage_dir,
                kind=kind,
            )
        else:
            normalized_path, children = await asyncio.to_thread(
                list_storage_subfolders,
                path,
                storage_dir,
            )
            track_files = []
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FolderNotFoundError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return FolderListResponse(
        path=normalized_path,
        entries=[
            FolderTreeEntry(
                name=child.name,
                path=child.path,
                has_children=child.has_children,
            )
            for child in children
        ],
        files=[
            FolderTrackFileEntry(
                name=item.name,
                path=item.path,
                size_bytes=item.size_bytes,
                kind=item.kind,  # type: ignore[arg-type]
            )
            for item in track_files
        ],
    )


@router.post("", response_model=FolderTreeEntry, status_code=AppConstants.HTTP_CREATED)
async def create_folder(
    request: Request,
    payload: FolderCreateRequest,
) -> FolderTreeEntry:
    """Create a new subfolder inside `payload.parent` within STORAGE_DIR."""
    storage_dir = request.app.state.settings.storage_dir
    try:
        created = await asyncio.to_thread(
            create_storage_subfolder,
            payload.parent,
            payload.name,
            storage_dir,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FolderNotFoundError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FolderAlreadyExistsError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=str(exc),
        ) from exc
    except FolderPermissionError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_FORBIDDEN,
            detail=str(exc),
        ) from exc

    return FolderTreeEntry(
        name=created.name,
        path=created.path,
        has_children=created.has_children,
    )


@router.post("/rename", response_model=FolderOpResponse)
async def rename_folder(
    request: Request,
    payload: FolderRenameRequest,
) -> FolderOpResponse:
    """Rename a folder inside STORAGE_DIR and rewrite descendant DB rows."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        return await asyncio.to_thread(
            file_ops.rename_folder,
            payload.path,
            payload.new_name,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/move", response_model=FolderOpResponse)
async def move_folder(
    request: Request,
    payload: FolderMoveRequest,
) -> FolderOpResponse:
    """Move a folder into `destination_folder` and rewrite descendant DB rows."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        return await asyncio.to_thread(
            file_ops.move_folder,
            payload.path,
            payload.destination_folder,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/delete", response_model=FolderOpResponse)
async def delete_folder(
    request: Request,
    payload: FolderDeleteRequest,
) -> FolderOpResponse:
    """Delete a folder inside STORAGE_DIR (403 on :ro volumes, 409 if non-empty without recursive)."""
    file_ops: FileOpsService = request.app.state.file_ops
    try:
        return await asyncio.to_thread(
            file_ops.delete_folder,
            payload.path,
            recursive=payload.recursive,
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
