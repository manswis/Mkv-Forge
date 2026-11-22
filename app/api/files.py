"""Library files and single-file inspection/rescan API endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import AppConstants
from app.db.repositories import MediaFileRepository, decode_audio_summary_json
from app.models.schemas import (
    AudioTrackSummary,
    AudioTypeFacet,
    FolderTreeEntry,
    LibraryFacets,
    MediaFileDetailResponse,
    MediaFileInfoResponse,
    MediaFileListItem,
    MediaFileListResponse,
    OtherFileEntry,
)
from app.services.folder_service import parse_visible_extensions_set
from app.services.folder_sync import reconcile_folder_snapshot
from app.services.media_info_extractor import extract_media_file_info
from app.services.playback_profile import (
    PlaybackProfileSpec,
    evaluate_file_needs_conversion,
)
from app.services.probe_parser import parse_mkvmerge_json
from app.services.scanner import (
    LibraryScanner,
    ProbeError,
    probe_container_tags_with_ffprobe,
)
from app.services.watcher import LibraryWatcher
from app.utils.paths import PathSafetyError, safe_resolve_path

router = APIRouter(prefix="/api/files", tags=["files"])
logger = logging.getLogger(__name__)


def _row_to_list_item(
    row: dict[str, Any],
    *,
    playback_profile: PlaybackProfileSpec | None = None,
) -> MediaFileListItem:
    """Convert a SQLite media_files row into a MediaFileListItem schema."""
    summary_dicts = decode_audio_summary_json(row.get("audio_summary_json"))
    audio_summary = [AudioTrackSummary(**entry) for entry in summary_dicts]
    container_title = str(row.get("container_title") or "")
    has_global_tags = bool(row.get("has_global_tags"))
    scan_status = str(row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
    needs_conversion = (
        evaluate_file_needs_conversion(
            summary_dicts, playback_profile, scan_status=scan_status
        )
        if playback_profile is not None
        else False
    )

    return MediaFileListItem(
        id=int(row["id"]),
        filepath=str(row["filepath"]),
        folder=str(row["folder"]),
        filename=str(row["filename"]),
        size_bytes=int(row["size_bytes"]),
        mtime=float(row["mtime"]),
        duration_us=int(row["duration_us"]),
        resolution=row.get("resolution"),
        video_codec=row.get("video_codec"),
        video_dynamic_range=row.get("video_dynamic_range"),
        audio_summary=audio_summary,
        subtitle_count=int(row.get("subtitle_count") or 0),
        container_title=container_title,
        has_global_tags=has_global_tags,
        has_metadata=bool(container_title) or has_global_tags,
        has_aac_51=bool(row.get("has_aac_51")),
        has_ac3_eac3=bool(row.get("has_ac3_eac3")),
        needs_conversion=needs_conversion,
        scan_status=scan_status,  # type: ignore[arg-type]
        scanned_at=str(row["scanned_at"]),
    )


def _row_to_detail(
    row: dict[str, Any],
    raw_payload_override: dict[str, Any] | None = None,
) -> MediaFileDetailResponse:
    """Convert a full media_files row (with raw_json) into MediaFileDetailResponse."""
    base = _row_to_list_item(row)
    if raw_payload_override is not None:
        raw_payload = raw_payload_override
    else:
        raw_json_str = str(row.get("raw_json") or "{}")
        try:
            raw_payload = json.loads(raw_json_str)
        except json.JSONDecodeError:
            raw_payload = {}

    probe = parse_mkvmerge_json(raw_payload if isinstance(raw_payload, dict) else {})
    return MediaFileDetailResponse(
        **base.model_dump(),
        has_track_tags=probe.has_track_tags,
        global_tags_count=probe.global_tags_count,
        track_tags_count=probe.track_tags_count,
        attachments_count=probe.attachments_count,
        chapters_count=probe.chapters_count,
        attachments=list(probe.attachments),
        tag_entries=list(probe.tag_entries),
        tracks=probe.tracks,
    )


async def _enrich_raw_payload_tags(
    request: Request,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Parse `row['raw_json']` and enrich `container_tags` via ffprobe if global_tags exist."""
    raw_json_str = str(row.get("raw_json") or "{}")
    try:
        parsed = json.loads(raw_json_str)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    global_tags = parsed.get("global_tags")
    if (
        isinstance(global_tags, list)
        and len(global_tags) > 0
        and "container_tags" not in parsed
    ):
        try:
            storage_dir = request.app.state.settings.storage_dir
            abs_path = safe_resolve_path(str(row["filepath"]), storage_dir)
            if abs_path.is_file():
                tags = await probe_container_tags_with_ffprobe(abs_path)
                if tags:
                    parsed["container_tags"] = tags
        except Exception:
            pass
    return parsed


def _load_library_queries(
    media_repo: MediaFileRepository,
    *,
    q: str | None,
    folder: str | None,
    audio_type: str | None,
    audio_codec: str | None,
    video_codec: str | None,
    resolution: str | None,
    dynamic_range: str | None,
    audio_channels: str | None,
    audio_language: str | None,
    no_subtitles: bool,
    aac51_only: bool,
    aac51: bool,
    has_metadata: bool,
    no_dolby: bool,
    needs_conversion: bool,
    playback_profile: PlaybackProfileSpec | None,
    sort: str,
    order: str,
    page: int,
    limit: int,
) -> tuple[
    list[dict[str, Any]],
    int,
    list[str],
    list[dict[str, Any]],
    dict[str, int],
    dict[str, int],
]:
    """Execute all Library SQLite read queries in a single worker-thread call."""
    rows, total = media_repo.list_files(
        q=q,
        folder=folder,
        audio_type=audio_type,
        audio_codec=audio_codec,
        video_codec=video_codec,
        resolution=resolution,
        dynamic_range=dynamic_range,
        audio_channels=audio_channels,
        audio_language=audio_language,
        no_subtitles=no_subtitles,
        aac51_only=aac51_only,
        aac51=aac51,
        has_metadata=has_metadata,
        no_dolby=no_dolby,
        needs_conversion=needs_conversion,
        playback_profile=playback_profile,
        sort=sort,
        order=order,
        page=page,
        limit=limit,
    )
    folders = media_repo.list_folders(
        q=q,
        audio_type=audio_type,
        audio_codec=audio_codec,
        video_codec=video_codec,
        resolution=resolution,
        dynamic_range=dynamic_range,
        audio_channels=audio_channels,
        audio_language=audio_language,
        no_subtitles=no_subtitles,
        aac51_only=aac51_only,
        aac51=aac51,
        has_metadata=has_metadata,
        no_dolby=no_dolby,
        needs_conversion=needs_conversion,
        playback_profile=playback_profile,
    )
    audio_facets_raw = media_repo.list_audio_types(
        q=q,
        folder=folder,
        audio_codec=audio_codec,
        video_codec=video_codec,
        resolution=resolution,
        dynamic_range=dynamic_range,
        audio_channels=audio_channels,
        audio_language=audio_language,
        no_subtitles=no_subtitles,
        aac51_only=aac51_only,
        aac51=aac51,
        has_metadata=has_metadata,
        no_dolby=no_dolby,
        needs_conversion=needs_conversion,
        playback_profile=playback_profile,
    )
    workflow_counts = media_repo.get_workflow_facet_counts(
        q=q,
        folder=folder,
        audio_type=audio_type,
        audio_codec=audio_codec,
        video_codec=video_codec,
        resolution=resolution,
        dynamic_range=dynamic_range,
        audio_channels=audio_channels,
        audio_language=audio_language,
        no_subtitles=no_subtitles,
        playback_profile=playback_profile,
    )
    folder_counts = media_repo.get_folder_file_counts()
    return rows, total, folders, audio_facets_raw, workflow_counts, folder_counts


@router.get("", response_model=MediaFileListResponse)
async def list_files(
    request: Request,
    q: str | None = Query(default=None),
    folder: str | None = Query(default=None),
    audio_type: str | None = Query(default=None),
    audio_codec: str | None = Query(default=None),
    video_codec: str | None = Query(default=None),
    resolution: str | None = Query(default=None),
    dynamic_range: str | None = Query(default=None),
    audio_channels: str | None = Query(default=None),
    audio_language: str | None = Query(default=None),
    no_subtitles: bool = Query(default=False),
    aac51_only: bool = Query(default=False),
    aac51: bool = Query(default=False),
    has_metadata: bool = Query(default=False),
    no_dolby: bool = Query(default=False),
    needs_conversion: bool = Query(default=False),
    sort: str = Query(default=AppConstants.DEFAULT_SORT_FIELD),
    order: str = Query(default=AppConstants.DEFAULT_SORT_ORDER),
    page: int = Query(default=AppConstants.DEFAULT_PAGE, ge=AppConstants.DEFAULT_PAGE),
    limit: int = Query(
        default=AppConstants.DEFAULT_PAGE_LIMIT,
        ge=AppConstants.MIN_PAGE_LIMIT,
        le=AppConstants.MAX_PAGE_LIMIT,
    ),
) -> MediaFileListResponse:
    """List cached media files with search, filter chips, sorting, and pagination."""
    media_repo: MediaFileRepository = request.app.state.media_repo

    subfolders: list[FolderTreeEntry] = []
    other_files: list[OtherFileEntry] = []
    folder_exists = True
    reconcile_ms = 0.0
    pruned_count = 0
    probe_candidate_count = 0
    has_folder_scope = bool(folder is not None and folder.strip())
    if has_folder_scope:
        assert folder is not None
        storage_dir = request.app.state.settings.storage_dir
        user_settings_service = getattr(request.app.state, "user_settings_service", None)
        current_user = getattr(request.state, "current_user", None)
        effective_uid = (
            int(current_user.id)
            if current_user is not None and not getattr(current_user, "is_anonymous", True)
            else None
        )
        show_media_only = AppConstants.DEFAULT_SHOW_MEDIA_FILES_ONLY
        visible_exts = AppConstants.SUPPORTED_MEDIA_EXTENSIONS
        if user_settings_service is not None:
            eff_settings = await asyncio.to_thread(
                user_settings_service.get, user_id=effective_uid
            )
            show_media_only = bool(eff_settings.show_media_files_only)
            visible_exts = parse_visible_extensions_set(
                eff_settings.visible_media_extensions
            )

        t_rec = time.perf_counter()
        reconcile = await asyncio.to_thread(
            reconcile_folder_snapshot,
            folder.strip(),
            storage_dir=storage_dir,
            media_repo=media_repo,
            check_grand_children=False,
            show_media_files_only=show_media_only,
            visible_extensions=visible_exts,
        )
        reconcile_ms = (time.perf_counter() - t_rec) * 1000.0
        folder_exists = reconcile.folder_exists
        pruned_count = reconcile.pruned_count
        probe_candidate_count = len(reconcile.probe_candidates)
        subfolders = [
            FolderTreeEntry(
                name=child.name,
                path=child.path,
                has_children=child.has_children,
            )
            for child in reconcile.subfolders
        ]
        other_files = [
            OtherFileEntry(
                name=entry.name,
                filepath=entry.rel_filepath,
                folder=entry.folder,
                size_bytes=entry.size_bytes,
                mtime=entry.mtime,
                ext=entry.ext,
            )
            for entry in reconcile.other_files
        ]
        watcher: LibraryWatcher | None = getattr(
            request.app.state, "library_watcher", None
        )
        if watcher is not None and reconcile.probe_candidates:
            for rel_path in reconcile.probe_candidates:
                watcher.schedule_background_probe(rel_path)

    user_settings_service = getattr(request.app.state, "user_settings_service", None)
    playback_profile = (
        await asyncio.to_thread(user_settings_service.get_playback_profile)
        if user_settings_service is not None
        else None
    )

    t_db = time.perf_counter()
    (
        rows,
        total,
        folders,
        audio_facets_raw,
        workflow_counts,
        folder_counts,
    ) = await asyncio.to_thread(
        _load_library_queries,
        media_repo,
        q=q,
        folder=folder,
        audio_type=audio_type,
        audio_codec=audio_codec,
        video_codec=video_codec,
        resolution=resolution,
        dynamic_range=dynamic_range,
        audio_channels=audio_channels,
        audio_language=audio_language,
        no_subtitles=no_subtitles,
        aac51_only=aac51_only,
        aac51=aac51,
        has_metadata=has_metadata,
        no_dolby=no_dolby,
        needs_conversion=needs_conversion,
        playback_profile=playback_profile,
        sort=sort,
        order=order,
        page=page,
        limit=limit,
    )
    db_ms = (time.perf_counter() - t_db) * 1000.0
    total_ms = reconcile_ms + db_ms
    log_fn = (
        logger.warning
        if total_ms >= AppConstants.LOG_SLOW_FILES_QUERY_MS
        else logger.debug
    )
    log_fn(
        "Library query scope=%s reconcile_ms=%.1f db_ms=%.1f items=%d total=%d subfolders=%d pruned=%d candidates=%d",
        "folder" if has_folder_scope else "all",
        reconcile_ms,
        db_ms,
        len(rows),
        total,
        len(subfolders),
        pruned_count,
        probe_candidate_count,
    )
    audio_facets = [AudioTypeFacet(**entry) for entry in audio_facets_raw]
    total_pages = math.ceil(total / limit) if total > 0 else 0

    return MediaFileListResponse(
        items=[
            _row_to_list_item(row, playback_profile=playback_profile)
            for row in rows
        ],
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
        folders=folders,
        audio_types=[entry.label for entry in audio_facets],
        facets=LibraryFacets(
            aac51_only=workflow_counts["aac51_only"],
            has_metadata=workflow_counts["has_metadata"],
            no_dolby=workflow_counts["no_dolby"],
            needs_conversion=workflow_counts.get("needs_conversion", 0),
            audio_types=audio_facets,
        ),
        subfolders=subfolders,
        other_files=other_files,
        folder_counts=folder_counts,
        folder_exists=folder_exists,
    )


@router.get("/{file_id}", response_model=MediaFileDetailResponse)
async def get_file_detail(request: Request, file_id: int) -> MediaFileDetailResponse:
    """Return full parsed tracks and container metadata for the file editor."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    row = await asyncio.to_thread(media_repo.get_by_id, file_id)
    if row is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} not found",
        )
    raw_payload = await _enrich_raw_payload_tags(request, row)
    return _row_to_detail(row, raw_payload)


@router.get("/{file_id}/info", response_model=MediaFileInfoResponse)
async def get_file_info(request: Request, file_id: int) -> MediaFileInfoResponse:
    """Return comprehensive MediaInfo-style container, track, and attachment metadata."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    row = await asyncio.to_thread(media_repo.get_by_id, file_id)
    if row is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} not found",
        )
    raw_payload = await _enrich_raw_payload_tags(request, row)
    return extract_media_file_info(row, raw_payload)


@router.post("/{file_id}/rescan", response_model=MediaFileDetailResponse)
async def rescan_single_file(request: Request, file_id: int) -> MediaFileDetailResponse:
    """Re-probe one media file on disk and return its updated metadata."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    scanner: LibraryScanner = request.app.state.scanner

    existing = await asyncio.to_thread(media_repo.get_by_id, file_id)
    if existing is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Media file {file_id} not found",
        )

    try:
        refreshed = await scanner.probe_single_file(str(existing["filepath"]))
    except PathSafetyError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ProbeError as exc:
        await asyncio.to_thread(
            media_repo.mark_scan_error,
            filepath=str(existing["filepath"]),
            folder=str(existing["folder"]),
            filename=str(existing["filename"]),
            size_bytes=int(existing["size_bytes"]),
            mtime=float(existing["mtime"]),
        )
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    if refreshed is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail="File no longer exists on disk and was removed from library",
        )

    return _row_to_detail(refreshed)
