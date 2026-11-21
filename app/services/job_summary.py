"""Pure human-readable change summary extractor for persisted job configs."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from app.config import AppConstants
from app.models.schemas import JobChangeItem


_CODEC_DISPLAY_MAP: dict[str, str] = {
    AppConstants.TARGET_CODEC_EAC3: "EAC3",
    AppConstants.TARGET_CODEC_AC3: "AC3",
    AppConstants.TARGET_CODEC_AAC: "AAC",
    AppConstants.TARGET_CODEC_OPUS: "Opus",
    AppConstants.TARGET_CODEC_FLAC: "FLAC",
}

_TRACK_TYPE_LABEL_MAP: dict[str, str] = {
    "video": "video",
    "audio": "audio",
    "subtitles": "subtitle",
}


def _format_channels_short(
    channels: Any,
    *,
    target_codec: str = AppConstants.TARGET_CODEC_EAC3,
) -> str:
    """Format effective output channel count into a concise audiophile badge label."""
    try:
        count = int(channels)
    except (TypeError, ValueError):
        return "5.1"
    if count <= 0:
        return "5.1"
    max_for_codec = AppConstants.AUDIO_CODEC_MAX_CHANNELS.get(
        target_codec, AppConstants.MAX_DOLBY_CHANNELS
    )
    effective = min(count, max_for_codec)
    if effective == AppConstants.CHANNELS_71:
        return "7.1"
    if effective == AppConstants.CHANNELS_61:
        return "6.1"
    if effective == AppConstants.CHANNELS_51:
        return "5.1"
    if effective == AppConstants.CHANNELS_50:
        return "5.0"
    if effective == AppConstants.CHANNELS_40:
        return "4.0"
    if effective == AppConstants.CHANNELS_30:
        return "3.0"
    if effective == AppConstants.CHANNELS_STEREO:
        return "2.0"
    if effective == AppConstants.CHANNELS_MONO:
        return "1.0"
    return f"{effective}ch"


def _basename(path_str: str | None) -> str:
    if not path_str:
        return ""
    return PurePosixPath(path_str).name


def summarize_job_changes(
    config_json: str | None,
    *,
    filepath: str,
    output_filepath: str | None = None,
) -> tuple[str | None, list[JobChangeItem]]:
    """Derive `(output_mode, changes)` from a job's persisted `config_json`.

    Degrades gracefully to `(None, [])` on legacy or malformed JSON so history
    endpoints never fail.
    """
    if not config_json:
        return None, []
    try:
        raw = json.loads(config_json)
    except (TypeError, json.JSONDecodeError):
        return None, []
    if not isinstance(raw, dict):
        return None, []

    changes: list[JobChangeItem] = []

    # 1. Stage 1 Added Audio & optional dropped source audio
    audio_cfg = raw.get("audio_conversion")
    if isinstance(audio_cfg, dict) and bool(audio_cfg.get("enabled")):
        target_codec = str(audio_cfg.get("target_codec") or "eac3").lower()
        codec_label = _CODEC_DISPLAY_MAP.get(target_codec, target_codec.upper())
        raw_channels = (
            audio_cfg.get("target_channels")
            if audio_cfg.get("target_channels") is not None
            else audio_cfg.get("source_channels")
        )
        layout = _format_channels_short(raw_channels, target_codec=target_codec)
        is_lossless = target_codec in AppConstants.LOSSLESS_TARGET_CODECS
        bitrate = (
            "Lossless"
            if is_lossless
            else str(audio_cfg.get("bitrate") or "").strip()
        )
        lang = str(audio_cfg.get("language") or "").strip()
        meta_bits = [bit for bit in (bitrate, lang) if bit and bit != AppConstants.DEFAULT_LANGUAGE]
        detail = f" ({' • '.join(meta_bits)})" if meta_bits else ""
        default_suffix = " • Default" if bool(audio_cfg.get("set_as_default", True)) else ""
        changes.append(
            JobChangeItem(
                kind="added",
                label=f"+ {codec_label} {layout}{detail}{default_suffix}",
            )
        )
        if bool(audio_cfg.get("drop_source_track")):
            src_id = audio_cfg.get("source_track_id")
            suffix = f" (#{src_id})" if src_id is not None else ""
            changes.append(
                JobChangeItem(
                    kind="removed",
                    label=f"Dropped base audio{suffix}",
                )
            )

    # 1b. Imported External Audio & Subtitle Tracks
    external_tracks = raw.get("external_tracks")
    if isinstance(external_tracks, list):
        for ext in external_tracks:
            if not isinstance(ext, dict):
                continue
            ext_kind = str(ext.get("kind") or "").lower()
            kind_label = (
                "Imported Audio"
                if ext_kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO
                else "Imported Sub"
            )
            name_hint = (
                str(ext.get("track_name") or "").strip()
                or _basename(str(ext.get("original_filename") or ext.get("path") or ""))
            )
            lang = str(ext.get("language") or "").strip()
            bits = [
                b
                for b in (
                    name_hint[:24] + ("…" if len(name_hint) > 24 else "") if name_hint else "",
                    lang if lang and lang != AppConstants.DEFAULT_LANGUAGE else "",
                )
                if b
            ]
            detail = f" ({' • '.join(bits)})" if bits else ""
            changes.append(
                JobChangeItem(kind="added", label=f"+ {kind_label}{detail}")
            )

    # 2. Container title & metadata stripping
    container = raw.get("container")
    if isinstance(container, dict):
        title = container.get("title")
        if title == "":
            changes.append(JobChangeItem(kind="removed", label="Cleared title"))
        elif isinstance(title, str) and title.strip():
            trimmed = title.strip()
            short_title = trimmed if len(trimmed) <= 32 else f"{trimmed[:31]}…"
            changes.append(JobChangeItem(kind="changed", label=f'Title → "{short_title}"'))

        stripped: list[str] = []
        if bool(container.get("strip_global_tags")):
            stripped.append("global tags")
        if bool(container.get("strip_track_tags")):
            stripped.append("track tags")
        keep_att_ids = container.get("keep_attachment_ids")
        if bool(container.get("strip_attachments")) or (
            isinstance(keep_att_ids, list) and len(keep_att_ids) == 0
        ):
            stripped.append("attachments")
        elif isinstance(keep_att_ids, list) and len(keep_att_ids) > 0:
            changes.append(
                JobChangeItem(
                    kind="changed",
                    label=f"Kept {len(keep_att_ids)} {'attachment' if len(keep_att_ids) == 1 else 'attachments'}",
                )
            )
        if bool(container.get("strip_chapters")):
            stripped.append("chapters")
        if stripped:
            changes.append(
                JobChangeItem(
                    kind="removed",
                    label=f"Stripped {', '.join(stripped)}",
                )
            )

    # 3. Removed / dropped tracks grouped by type
    tracks = raw.get("tracks")
    if isinstance(tracks, list):
        removed_counts: dict[str, int] = {}
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if track.get("enabled") is False:
                t_type = str(track.get("type") or "track").lower()
                label = _TRACK_TYPE_LABEL_MAP.get(t_type, t_type)
                removed_counts[label] = removed_counts.get(label, 0) + 1

        for label, count in removed_counts.items():
            noun = "track" if count == 1 else "tracks"
            changes.append(
                JobChangeItem(
                    kind="removed",
                    label=f"Removed {count} {label} {noun}",
                )
            )

    # 4. Output behavior summary
    output_mode_raw = raw.get("output_mode")
    output_mode = str(output_mode_raw) if isinstance(output_mode_raw, str) else None
    source_is_mp4 = filepath.lower().endswith(AppConstants.EXT_MP4)

    if output_mode == AppConstants.OUTPUT_MODE_SAVE_AS:
        dest = output_filepath or str(raw.get("output_path") or "").strip()
        dest_display = f"/{dest.lstrip('/')}" if dest else "custom path"
        changes.append(JobChangeItem(kind="output", label=f"Saved as {dest_display}"))
    elif output_mode == AppConstants.OUTPUT_MODE_KEEP_BOTH:
        dest_name = _basename(output_filepath) or ".remux.mkv"
        changes.append(JobChangeItem(kind="output", label=f"Keep Both → {dest_name}"))
    elif output_mode == AppConstants.OUTPUT_MODE_REPLACE and source_is_mp4:
        dest_name = _basename(output_filepath) or f"{PurePosixPath(filepath).stem}.mkv"
        changes.append(JobChangeItem(kind="output", label=f".mp4 → {dest_name}"))

    # Fallback when user only edited track names/languages/order without flags above
    if not changes and isinstance(tracks, list) and len(tracks) > 0:
        changes.append(
            JobChangeItem(
                kind="changed",
                label="Updated track metadata / order",
            )
        )

    return output_mode, changes
