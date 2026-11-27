"""Chapters read/parse and stateless on-demand scene frame preview REST endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import AppConstants, Settings
from app.db.repositories import JobRepository, MediaFileRepository
from app.models.schemas import (
    ChaptersParseRequest,
    ChaptersParseResponse,
    ChaptersReadResponse,
)
from app.services.chapter_service import (
    ChapterError,
    extract_chapters_from_file,
    parse_chapters_text,
)
from app.services.probe_parser import parse_mkvmerge_json
from app.services.scene_frame_service import (
    SceneFrameCancelledError,
    SceneFrameError,
    clamp_frame_timestamp_ms,
    extract_scene_frame,
)
from app.utils.paths import PathSafetyError, safe_resolve_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["chapters"])


def _is_in_place_active_job_for_file(
    rel_filepath: str, active_job: dict[str, Any] | None
) -> bool:
    """Return True iff `active_job` is modifying `rel_filepath` in place (`replace` or `delete_original`)."""
    if active_job is None:
        return False
    active_fp = str(active_job.get("filepath") or "").strip().lstrip("/")
    target_fp = str(rel_filepath or "").strip().lstrip("/")
    if not active_fp or active_fp != target_fp:
        return False

    raw_cfg = active_job.get("config_json")
    if not isinstance(raw_cfg, str) or not raw_cfg:
        return True
    try:
        parsed = json.loads(raw_cfg)
    except json.JSONDecodeError:
        return True
    if not isinstance(parsed, dict):
        return True

    output_mode = str(parsed.get("output_mode") or AppConstants.OUTPUT_MODE_KEEP_BOTH)
    if output_mode == AppConstants.OUTPUT_MODE_REPLACE:
        return True
    if output_mode == AppConstants.OUTPUT_MODE_SAVE_AS and bool(
        parsed.get("delete_original")
    ):
        return True
    return False


def _cached_chapters_count(row: dict[str, Any]) -> int:
    """Extract cached chapters count from `row['raw_json']` without spawning a subprocess."""
    raw_json = row.get("raw_json")
    if not isinstance(raw_json, str) or not raw_json.strip():
        return 0
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    return parse_mkvmerge_json(payload).chapters_count


async def _resolve_ready_file_and_guard_job(
    request: Request, file_id: int
) -> tuple[dict[str, Any], Path, bool]:
    """Validate file existence, scan readiness, active-job safety, and path confinement.

    Returns `(row, abs_path, has_unrelated_active_job)`.
    """
    media_repo: MediaFileRepository = request.app.state.media_repo
    job_repo: JobRepository = request.app.state.job_repo
    settings: Settings = request.app.state.settings

    row = await asyncio.to_thread(media_repo.get_by_id, file_id)
    if row is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} not found",
        )

    scan_status = str(row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
    if scan_status != AppConstants.FILE_SCAN_STATUS_READY:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail="File is currently scanning or failed scanning. Wait until it is ready.",
        )

    rel_filepath = str(row["filepath"])
    active_job = await asyncio.to_thread(job_repo.get_active)
    if _is_in_place_active_job_for_file(rel_filepath, active_job):
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=(
                "A Replace Original job is currently modifying this file. "
                "Wait for it to finish before previewing scenes or editing chapters."
            ),
        )

    try:
        abs_path = await asyncio.to_thread(
            safe_resolve_path, rel_filepath, settings.storage_dir
        )
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if not await asyncio.to_thread(abs_path.is_file):
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} no longer exists on disk",
        )

    return row, abs_path, active_job is not None


@router.get("/{file_id}/chapters", response_model=ChaptersReadResponse)
async def get_file_chapters(request: Request, file_id: int) -> ChaptersReadResponse:
    """Lazily read existing chapters from an `.mkv` (`mkvextract`) or `.mp4` (`ffprobe`)."""
    row, abs_path, _has_active_job = await _resolve_ready_file_and_guard_job(
        request, file_id
    )
    duration_us = int(row.get("duration_us") or 0)
    if (
        abs_path.suffix.lower() == AppConstants.EXT_MKV
        and _cached_chapters_count(row) == 0
    ):
        return ChaptersReadResponse(
            file_id=file_id,
            duration_us=duration_us,
            chapters=[],
            has_complex_chapters=False,
        )

    try:
        chapters, has_complex = await extract_chapters_from_file(abs_path)
    except ChapterError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return ChaptersReadResponse(
        file_id=file_id,
        duration_us=duration_us,
        chapters=chapters,
        has_complex_chapters=has_complex,
    )


@router.post("/{file_id}/chapters/parse", response_model=ChaptersParseResponse)
async def parse_file_chapters(
    request: Request,
    file_id: int,
    body: ChaptersParseRequest,
) -> ChaptersParseResponse:
    """Auto-detect and parse pasted/uploaded chapters (XML, OGG, YouTube/plain text, CSV)."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    row = await asyncio.to_thread(media_repo.get_by_id, file_id)
    if row is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} not found",
        )

    duration_us = int(row.get("duration_us") or 0)
    duration_ms = duration_us // AppConstants.US_PER_MS if duration_us > 0 else None

    try:
        detected_format, chapters, warnings = parse_chapters_text(
            body.raw_text,
            duration_ms=duration_ms,
        )
    except ChapterError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ChaptersParseResponse(
        detected_format=detected_format,
        chapters=chapters,
        warnings=warnings,
    )


@router.get("/{file_id}/frame")
async def get_file_scene_frame(
    request: Request,
    file_id: int,
    ts_ms: int = Query(..., ge=0),
    mode: Literal["fast", "accurate"] = Query(default=AppConstants.SCENE_FRAME_MODE_FAST),  # type: ignore[assignment]
    v: str | None = Query(default=None),
) -> Response:
    """Extract and stream one stateless JPEG preview frame (`pipe:1`) with browser HTTP caching."""
    del v  # Video-fingerprint query param is used strictly for browser HTTP cache busting
    row, abs_path, has_unrelated_active_job = await _resolve_ready_file_and_guard_job(
        request, file_id
    )

    duration_us = int(row.get("duration_us") or 0)
    has_video = bool(row.get("video_codec") or row.get("resolution"))
    if duration_us <= 0 or not has_video:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail="Scene frame previews are only supported for video files with known duration",
        )

    clamped_ms = clamp_frame_timestamp_ms(ts_ms, duration_us)
    try:
        jpeg_bytes = await extract_scene_frame(
            abs_path,
            ts_ms=clamped_ms,
            mode=mode,
            low_io_priority=has_unrelated_active_job,
            is_disconnected=request.is_disconnected,
        )
    except SceneFrameCancelledError:
        logger.info(
            "Scene frame cancelled by client disconnect (file_id=%s ts_ms=%s mode=%s)",
            file_id,
            clamped_ms,
            mode,
        )
        return Response(status_code=AppConstants.HTTP_NO_CONTENT)
    except SceneFrameError as exc:
        logger.warning(
            "Scene frame request failed (file_id=%s ts_ms=%s mode=%s): %s",
            file_id,
            clamped_ms,
            mode,
            exc,
        )
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={
            AppConstants.CACHE_CONTROL_HEADER: (
                f"private, max-age={AppConstants.SCENE_FRAME_HTTP_CACHE_MAX_AGE_SEC}"
            )
        },
    )
