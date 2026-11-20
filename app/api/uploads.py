"""Staged external track upload endpoints (POST / DELETE /api/uploads)."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import AppConstants
from app.models.schemas import StagedUploadResponse
from app.services.upload_service import (
    UploadNotFoundError,
    UploadService,
    UploadTooLargeError,
    UploadValidationError,
)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post(
    "/track",
    response_model=StagedUploadResponse,
    status_code=AppConstants.HTTP_CREATED,
)
async def upload_external_track(
    request: Request,
    kind: Literal["audio", "subtitles"] = Query(...),
    filename: str = Query(..., min_length=1, max_length=255),
) -> StagedUploadResponse:
    """Stream an external audio or subtitle file into `<DATA_DIR>/uploads/`."""
    upload_service: UploadService = request.app.state.upload_service

    raw_len = request.headers.get("content-length")
    content_length: int | None = None
    if raw_len is not None and raw_len.strip():
        try:
            content_length = int(raw_len.strip())
        except ValueError:
            content_length = None

    try:
        staged = await upload_service.stage_upload_stream(
            kind=kind,
            filename=filename,
            chunks=request.stream(),
            content_length=content_length,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_PAYLOAD_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except UploadValidationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return StagedUploadResponse(
        upload_id=staged.upload_id,
        kind=staged.kind,  # type: ignore[arg-type]
        original_filename=staged.original_filename,
        size_bytes=staged.size_bytes,
    )


@router.delete("/{upload_id}")
async def delete_staged_upload(
    request: Request,
    upload_id: str,
) -> dict[str, object]:
    """Discard an unsubmitted staged upload file from `<DATA_DIR>/uploads/`."""
    upload_service: UploadService = request.app.state.upload_service
    try:
        await asyncio.to_thread(upload_service.delete_upload, upload_id)
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
    return {"deleted": True, "upload_id": upload_id}
