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
    StorageSlice,
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

_VIDEO_CODEC_LABELS: tuple[tuple[str, str], ...] = (
    ("HEVC", "HEVC (H.265)"),
    ("H.264", "H.264 (AVC)"),
    ("AV1", "AV1"),
    ("VP9", "VP9"),
    ("MPEG-2", "MPEG-2"),
    ("VC-1", "VC-1"),
    ("Other", "Other Video"),
)

_RESOLUTION_ORDER: tuple[str, ...] = ("2160p", "1080p", "720p", "480p", "SD")
_RESOLUTION_LABELS: tuple[tuple[str, str], ...] = (
    ("2160p", "4K (2160p)"),
    ("1080p", "1080p Full HD"),
    ("720p", "720p HD"),
    ("480p", "480p SD"),
    ("SD", "SD"),
    ("Other", "Other Resolution"),
)
_DYNAMIC_RANGE_ORDER: tuple[str, ...] = ("DV HDR", "DV", "HDR10", "HLG", "HDR", "SDR")


def _build_storage_slices(
    raw_storage: dict[str, tuple[int, int]],
    order_pairs: tuple[tuple[str, str], ...],
) -> list[StorageSlice]:
    """Build ordered StorageSlice items with byte totals, file counts, and percentages."""
    known_keys = {k for k, _ in order_pairs if k != "Other"}
    bucket_bytes: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    for raw_key, (b_val, c_val) in raw_storage.items():
        b_safe = max(0, int(b_val))
        c_safe = max(0, int(c_val))
        if b_safe <= 0 and c_safe <= 0:
            continue
        target_key = raw_key if raw_key in known_keys else "Other"
        bucket_bytes[target_key] = bucket_bytes.get(target_key, 0) + b_safe
        bucket_counts[target_key] = bucket_counts.get(target_key, 0) + c_safe

    total_bytes = sum(bucket_bytes.values())
    if total_bytes <= 0:
        return []

    slices: list[StorageSlice] = []
    for key, label in order_pairs:
        sz = bucket_bytes.get(key, 0)
        fc = bucket_counts.get(key, 0)
        if sz <= 0:
            continue
        pct = round((sz / total_bytes) * 100.0, 1)
        slices.append(
            StorageSlice(
                key=key,
                label=label,
                size_bytes=sz,
                file_count=fc,
                percent=pct,
            )
        )
    return slices


def _build_dashboard_analytics(
    raw: dict[str, Any], storage_dir: Path
) -> DashboardAnalytics:
    """Assemble clamped storage breakdown, multi-lens audio donuts, and video pills."""
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

    storage_by_codec = _build_storage_slices(
        raw.get("vcodec_storage") or {}, _VIDEO_CODEC_LABELS
    )
    storage_by_resolution = _build_storage_slices(
        raw.get("res_storage") or {}, _RESOLUTION_LABELS
    )

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

    channel_counts: dict[str, int] = raw.get("channel_counts") or {}
    total_channel_tracks = sum(max(0, int(v)) for v in channel_counts.values())
    audio_channels: list[AudioCodecSlice] = []
    if total_channel_tracks > 0:
        for key, label in AppConstants.AUDIO_CHANNEL_TIER_ORDER:
            cnt = max(0, int(channel_counts.get(key, 0)))
            if cnt <= 0:
                continue
            pct = round((cnt / total_channel_tracks) * 100.0, 1)
            audio_channels.append(
                AudioCodecSlice(key=key, label=label, count=cnt, percent=pct)
            )

    lang_counts: dict[str, int] = raw.get("lang_counts") or {}
    total_lang_tracks = sum(max(0, int(v)) for v in lang_counts.values())
    audio_languages: list[AudioCodecSlice] = []
    if total_lang_tracks > 0:
        und_cnt = max(0, int(lang_counts.get("und", 0)))
        tagged_pairs = sorted(
            [
                (str(k), max(0, int(v)))
                for k, v in lang_counts.items()
                if str(k) != "und" and int(v) > 0
            ],
            key=lambda kv: (-kv[1], kv[0]),
        )
        top_limit = int(AppConstants.DASHBOARD_TOP_AUDIO_LANGUAGES_LIMIT)
        top_tagged = tagged_pairs[:top_limit]
        rest_tagged = tagged_pairs[top_limit:]

        primary_slices: list[tuple[str, int]] = list(top_tagged)
        if und_cnt > 0:
            primary_slices.append(("und", und_cnt))
        primary_slices.sort(key=lambda kv: (-kv[1], kv[0]))

        for code, cnt in primary_slices:
            label = AppConstants.LANGUAGE_DISPLAY_LABELS.get(
                code, f"{code.upper()} ({code})"
            )
            pct = round((cnt / total_lang_tracks) * 100.0, 1)
            audio_languages.append(
                AudioCodecSlice(key=code, label=label, count=cnt, percent=pct)
            )

        other_cnt = sum(cnt for _, cnt in rest_tagged)
        if other_cnt > 0:
            pct = round((other_cnt / total_lang_tracks) * 100.0, 1)
            audio_languages.append(
                AudioCodecSlice(
                    key="other",
                    label=AppConstants.LANGUAGE_DISPLAY_LABELS.get(
                        "other", "Other Languages"
                    ),
                    count=other_cnt,
                    percent=pct,
                )
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

    vcodec_counts: dict[str, int] = raw.get("vcodec_counts") or {}
    video_codecs: list[ResolutionCountItem] = []
    seen_vc: set[str] = set()
    for label in AppConstants.KNOWN_VIDEO_CODECS:
        cnt = max(0, int(vcodec_counts.get(label, 0)))
        if cnt > 0:
            video_codecs.append(ResolutionCountItem(label=label, count=cnt))
            seen_vc.add(label)
    for label, cnt in sorted(vcodec_counts.items()):
        if label not in seen_vc and int(cnt) > 0:
            video_codecs.append(ResolutionCountItem(label=label, count=int(cnt)))

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

    top_codec_label = audio_codecs[0].label if audio_codecs else ""
    top_codec_percent = audio_codecs[0].percent if audio_codecs else 0.0

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
        storage_by_codec=storage_by_codec,
        storage_by_resolution=storage_by_resolution,
        total_audio_tracks=total_audio_tracks,
        audio_codecs=audio_codecs,
        audio_channels=audio_channels,
        audio_languages=audio_languages,
        top_codec_label=top_codec_label,
        top_codec_percent=top_codec_percent,
        resolutions=resolutions,
        video_codecs=video_codecs,
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

    playback_profile = (
        await asyncio.to_thread(user_settings_service.get_playback_profile)
        if user_settings_service is not None
        else None
    )
    counts = await asyncio.to_thread(
        media_repo.get_dashboard_counts,
        playback_profile=playback_profile,
    )
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
            has_metadata_bytes=counts.get("has_metadata_bytes", 0),
            needs_conversion=counts.get("needs_conversion", 0),
            needs_conversion_bytes=counts.get("needs_conversion_bytes", 0),
            untagged_audio=counts.get("untagged_audio", 0),
            untagged_audio_bytes=counts.get("untagged_audio_bytes", 0),
            no_subtitles=counts.get("no_subtitles", 0),
            with_subtitles=counts.get("with_subtitles", 0),
            playback_profile_configured=bool(
                playback_profile is not None and playback_profile.configured
            ),
            playback_profile_preset=(
                playback_profile.preset
                if playback_profile is not None
                else AppConstants.DEFAULT_PLAYBACK_PROFILE_PRESET
            ),
            playback_profile_label=(
                playback_profile.label
                if playback_profile is not None
                else "Not Configured"
            ),
            last_scan_at=last_scan_at,
            last_scan_status=last_scan_status,
        ),
        analytics=analytics,
        scan_running=scanner.is_running,
        watcher=watcher.get_status() if watcher is not None else None,
        recent_activity=[RecentJobItem(**row) for row in recent_rows],
    )
