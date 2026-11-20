"""Dashboard summary API endpoint with storage, audio codec, and video analytics."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from app.config import AppConstants
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
)
from app.models.schemas import (
    AudioCodecSlice,
    DashboardAnalytics,
    DashboardResponse,
    DashboardStats,
    RecentJobItem,
    ResolutionCountItem,
    StorageBreakdown,
)
from app.services.scanner import LibraryScanner

router = APIRouter(prefix="/api", tags=["dashboard"])

_AUDIO_CODEC_ORDER: tuple[tuple[str, str], ...] = (
    ("EAC3", "DD+ (EAC3)"),
    ("AC3", "DD (AC3)"),
    ("AAC", "AAC"),
    ("DTS", "DTS / DTS-HD"),
    ("TrueHD", "TrueHD"),
    ("Other", "Other"),
)

_RESOLUTION_ORDER: tuple[str, ...] = ("2160p", "1080p", "720p", "480p", "SD")
_DYNAMIC_RANGE_ORDER: tuple[str, ...] = ("DV HDR", "DV", "HDR10", "HLG", "HDR", "SDR")


def _build_dashboard_analytics(
    raw: dict[str, Any], storage_dir: Path
) -> DashboardAnalytics:
    """Assemble clamped storage breakdown, track-summed audio codec donut, and video pills."""
    disk_total = 0
    disk_used = 0
    disk_free = 0
    try:
        if storage_dir.is_dir():
            usage = shutil.disk_usage(storage_dir)
            disk_total = max(0, int(usage.total))
            disk_used = max(0, int(usage.used))
            disk_free = max(0, int(usage.free))
    except OSError:
        pass

    mkv_bytes = max(0, int(raw.get("mkv_bytes") or 0))
    mkv_count = max(0, int(raw.get("mkv_count") or 0))
    mp4_bytes = max(0, int(raw.get("mp4_bytes") or 0))
    mp4_count = max(0, int(raw.get("mp4_count") or 0))
    media_bytes = max(0, int(raw.get("media_bytes") or 0))
    other_used_bytes = max(0, disk_used - media_bytes)

    codec_counts: dict[str, int] = raw.get("codec_counts") or {}
    total_audio_tracks = sum(max(0, int(v)) for v in codec_counts.values())
    audio_codecs: list[AudioCodecSlice] = []
    if total_audio_tracks > 0:
        for key, label in _AUDIO_CODEC_ORDER:
            cnt = max(0, int(codec_counts.get(key, 0)))
            if cnt <= 0:
                continue
            pct = round((cnt / total_audio_tracks) * 100.0, 1)
            audio_codecs.append(
                AudioCodecSlice(key=key, label=label, count=cnt, percent=pct)
            )

    res_counts: dict[str, int] = raw.get("res_counts") or {}
    resolutions: list[ResolutionCountItem] = []
    seen_res: set[str] = set()
    for label in _RESOLUTION_ORDER:
        cnt = max(0, int(res_counts.get(label, 0)))
        if cnt > 0:
            resolutions.append(ResolutionCountItem(label=label, count=cnt))
            seen_res.add(label)
    for label, cnt in sorted(res_counts.items()):
        if label not in seen_res and int(cnt) > 0:
            resolutions.append(ResolutionCountItem(label=label, count=int(cnt)))

    dr_counts: dict[str, int] = raw.get("dr_counts") or {}
    dynamic_ranges: list[ResolutionCountItem] = []
    seen_dr: set[str] = set()
    for label in _DYNAMIC_RANGE_ORDER:
        cnt = max(0, int(dr_counts.get(label, 0)))
        if cnt > 0:
            dynamic_ranges.append(ResolutionCountItem(label=label, count=cnt))
            seen_dr.add(label)
    for label, cnt in sorted(dr_counts.items()):
        if label not in seen_dr and int(cnt) > 0:
            dynamic_ranges.append(ResolutionCountItem(label=label, count=int(cnt)))

    return DashboardAnalytics(
        storage=StorageBreakdown(
            disk_total_bytes=disk_total,
            disk_used_bytes=disk_used,
            disk_free_bytes=disk_free,
            mkv_bytes=mkv_bytes,
            mkv_count=mkv_count,
            mp4_bytes=mp4_bytes,
            mp4_count=mp4_count,
            media_bytes=media_bytes,
            other_used_bytes=other_used_bytes,
        ),
        total_audio_tracks=total_audio_tracks,
        audio_codecs=audio_codecs,
        resolutions=resolutions,
        dynamic_ranges=dynamic_ranges,
    )


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(request: Request) -> DashboardResponse:
    """Return cached library stats, storage/codec analytics, and recent job activity."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    state_repo: AppStateRepository = request.app.state.state_repo
    job_repo: JobRepository = request.app.state.job_repo
    scanner: LibraryScanner = request.app.state.scanner
    watcher = getattr(request.app.state, "library_watcher", None)
    user_settings_service = getattr(request.app.state, "user_settings_service", None)
    storage_dir: Path = request.app.state.settings.storage_dir

    counts = await asyncio.to_thread(media_repo.get_dashboard_counts)
    raw_analytics = await asyncio.to_thread(media_repo.get_dashboard_analytics)
    analytics = await asyncio.to_thread(
        _build_dashboard_analytics, raw_analytics, storage_dir
    )
    last_scan_at = await asyncio.to_thread(
        state_repo.get, AppConstants.STATE_KEY_LAST_SCAN_AT
    )
    last_scan_status = await asyncio.to_thread(
        state_repo.get, AppConstants.STATE_KEY_LAST_SCAN_STATUS
    )
    recent_rows = await asyncio.to_thread(
        job_repo.list_recent, AppConstants.DEFAULT_RECENT_JOBS_LIMIT
    )
    current_user = getattr(request.state, "current_user", None)
    effective_uid = (
        int(current_user.id)
        if current_user is not None and not getattr(current_user, "is_anonymous", True)
        else None
    )
    user_settings = (
        await asyncio.to_thread(user_settings_service.get, user_id=effective_uid)
        if user_settings_service is not None
        else None
    )

    return DashboardResponse(
        app_name=AppConstants.APP_NAME,
        app_version=AppConstants.APP_VERSION,
        **(
            {"user_settings": user_settings}
            if user_settings is not None
            else {}
        ),
        stats=DashboardStats(
            total_files=counts["total_files"],
            aac51_only=counts["aac51_only"],
            has_metadata=counts["has_metadata"],
            last_scan_at=last_scan_at,
            last_scan_status=last_scan_status,
        ),
        analytics=analytics,
        scan_running=scanner.is_running,
        watcher=watcher.get_status() if watcher is not None else None,
        recent_activity=[RecentJobItem(**row) for row in recent_rows],
    )
