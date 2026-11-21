"""Pure parser for `mkvmerge -J` JSON metadata payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import AppConstants
from app.models.schemas import (
    AttachmentInfo,
    AudioTrackSummary,
    ParsedTrackInfo,
    TagKeyValueInfo,
)

_STATISTICS_TAG_KEYS: frozenset[str] = frozenset(
    {
        "tag_bps",
        "tag_duration",
        "tag_number_of_frames",
        "tag_number_of_bytes",
        "tag__statistics_writing_app",
        "tag__statistics_writing_date_utc",
        "tag__statistics_tags",
    }
)

_TRACK_SIZE_TAG_KEY = "tag_number_of_bytes"
_TRACK_SIZE_TAG_PREFIX = "tag_number_of_bytes-"

_IGNORED_FORMAT_TAG_KEYS: frozenset[str] = frozenset(
    {
        "encoder",
        "creation_time",
        "duration",
        "major_brand",
        "minor_version",
        "compatible_brands",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedMediaProbe:
    """Structured metadata extracted from `mkvmerge -J` output."""

    duration_us: int
    container_title: str
    has_global_tags: bool
    has_track_tags: bool
    attachments_count: int
    chapters_count: int
    has_aac_51: bool
    has_ac3_eac3: bool
    resolution: str | None
    video_codec: str | None
    video_dynamic_range: str | None
    audio_summary: list[AudioTrackSummary]
    audio_summary_json: str
    subtitle_count: int
    tracks: list[ParsedTrackInfo]
    global_tags_count: int = 0
    track_tags_count: int = 0
    attachments: tuple[AttachmentInfo, ...] = ()
    tag_entries: tuple[TagKeyValueInfo, ...] = ()


def _safe_int(value: Any) -> int | None:
    """Safely coerce a numeric JSON value to int, returning None if invalid."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_track_size_bytes(props: dict[str, Any]) -> int | None:
    """Extract track size in bytes from Matroska statistics tags if already present.

    Checks `tag_number_of_bytes` first, then language-suffixed variants such as
    `tag_number_of_bytes-eng`. Returns None when unavailable or non-positive.
    """
    direct = _safe_int(props.get(_TRACK_SIZE_TAG_KEY))
    if direct is not None and direct > 0:
        return direct
    for raw_k, raw_v in props.items():
        if isinstance(raw_k, str) and raw_k.lower().startswith(_TRACK_SIZE_TAG_PREFIX):
            parsed = _safe_int(raw_v)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _convert_duration_ns_to_us(duration_ns: Any) -> int:
    """Convert mkvmerge container duration from nanoseconds to microseconds."""
    parsed = _safe_int(duration_ns)
    if parsed is None or parsed <= 0:
        return 0
    return parsed // AppConstants.NS_PER_US


def _convert_min_timestamp_ns_to_ms(min_ts_ns: Any) -> int:
    """Convert track properties.minimum_timestamp from nanoseconds to integer ms."""
    parsed = _safe_int(min_ts_ns)
    if parsed is None or parsed == 0:
        return 0
    return round(parsed / AppConstants.NS_PER_MS)


def _normalize_video_codec(raw_codec: str, codec_id: str | None) -> str:
    """Normalize verbose mkvmerge video codec labels into concise badge names."""
    combined = f"{raw_codec} {codec_id or ''}".lower()
    if "hevc" in combined or "h.265" in combined or "h265" in combined:
        return "HEVC"
    if "avc" in combined or "h.264" in combined or "h264" in combined or "mpeg-4p10" in combined:
        return "H.264"
    if "av1" in combined or "v_av1" in combined:
        return "AV1"
    if "vp9" in combined:
        return "VP9"
    if "mpeg-2" in combined or "v_mpeg2" in combined:
        return "MPEG-2"
    if "vc-1" in combined or "vc1" in combined:
        return "VC-1"
    return raw_codec.strip() or "Video"


def _first_present(props: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-None property value matching any key in `keys`."""
    for key in keys:
        if props.get(key) is not None:
            return props.get(key)
    return None


def _has_dolby_vision_signal(
    raw_codec: str, codec_id: str | None, props: dict[str, Any]
) -> bool:
    """Return True if track properties indicate Dolby Vision (dvcC/dvvC/dvh1/dvhe)."""
    combined = f"{raw_codec} {codec_id or ''}".lower()
    if "dolby vision" in combined or "dvh1" in combined or "dvhe" in combined:
        return True
    if props.get("dv_profile") is not None:
        return True
    track_name = str(props.get("track_name") or "").lower()
    if "dolby vision" in track_name or re.search(r"\bdovi\b", track_name):
        return True
    mappings = props.get("block_addition_mappings")
    if isinstance(mappings, list):
        for entry in mappings:
            if not isinstance(entry, dict):
                continue
            mapping_type = _safe_int(
                entry.get("type")
                if entry.get("type") is not None
                else entry.get("id_type")
            )
            if mapping_type in AppConstants.DV_BLOCK_ADDITION_TYPES:
                return True
            name = str(entry.get("name") or "").lower()
            if "dolby vision" in name or "dvcc" in name or "dvvc" in name:
                return True
    return False


def _classify_video_dynamic_range(
    raw_codec: str, codec_id: str | None, props: dict[str, Any]
) -> str:
    """Classify a video track as DV, DV HDR, HDR10, HLG, HDR, or SDR."""
    raw_transfer = _first_present(props, AppConstants.COLOR_TRANSFER_KEYS)
    transfer = _safe_int(raw_transfer)
    transfer_str = str(raw_transfer or "").lower()

    raw_primaries = _first_present(props, AppConstants.COLOR_PRIMARIES_KEYS)
    primaries = _safe_int(raw_primaries)
    primaries_str = str(raw_primaries or "").lower()

    raw_matrix = _first_present(props, AppConstants.COLOR_MATRIX_KEYS)
    matrix = _safe_int(raw_matrix)
    matrix_str = str(raw_matrix or "").lower()

    has_mastering = any(
        props.get(key) is not None for key in AppConstants.HDR_MASTERING_KEYS
    )
    has_pq = (
        transfer == AppConstants.COLOR_TRANSFER_PQ
        or "smpte2084" in transfer_str
        or "pq" == transfer_str
    )
    has_hlg = (
        transfer == AppConstants.COLOR_TRANSFER_HLG
        or "arib-std-b67" in transfer_str
        or "hlg" in transfer_str
    )
    has_bt2020 = (
        primaries == AppConstants.COLOR_PRIMARIES_BT2020
        or matrix in AppConstants.COLOR_MATRIX_BT2020_TYPES
        or "bt2020" in primaries_str
        or "bt2020" in matrix_str
    )
    has_dv = _has_dolby_vision_signal(raw_codec, codec_id, props)

    track_name = str(props.get("track_name") or "").lower()
    name_has_hdr10 = bool(re.search(r"\bhdr10\+?\b", track_name))
    name_has_hlg = bool(re.search(r"\bhlg\b", track_name))
    name_has_hdr = bool(re.search(r"\bhdr\b", track_name))

    if has_dv:
        if has_pq or has_mastering or name_has_hdr10 or name_has_hdr:
            return AppConstants.DYNAMIC_RANGE_DV_HDR
        return AppConstants.DYNAMIC_RANGE_DV
    if has_pq or name_has_hdr10:
        return AppConstants.DYNAMIC_RANGE_HDR10
    if has_hlg or name_has_hlg:
        return AppConstants.DYNAMIC_RANGE_HLG
    if has_mastering or has_bt2020 or name_has_hdr:
        return AppConstants.DYNAMIC_RANGE_HDR
    return AppConstants.DYNAMIC_RANGE_SDR


def _normalize_audio_codec(raw_codec: str, codec_id: str | None) -> str:
    """Normalize verbose mkvmerge audio codec labels into concise badge names."""
    lower = raw_codec.lower()
    cid = (codec_id or "").upper()
    if "e-ac-3" in lower or "eac3" in lower or "eac-3" in lower or cid == "A_EAC3":
        return "EAC3"
    if "ac-3" in lower or "ac3" in lower or cid == "A_AC3":
        return "AC3"
    if "aac" in lower or cid.startswith("A_AAC"):
        return "AAC"
    if "truehd" in lower or cid == "A_TRUEHD":
        return "TrueHD"
    if "dts-hd" in lower:
        return "DTS-HD"
    if "dts" in lower or cid.startswith("A_DTS"):
        return "DTS"
    if "opus" in lower or cid == "A_OPUS":
        return "Opus"
    if "flac" in lower or cid == "A_FLAC":
        return "FLAC"
    if "pcm" in lower or cid.startswith("A_PCM"):
        return "PCM"
    if "mp3" in lower or "layer 3" in lower or cid == "A_MPEG/L3":
        return "MP3"
    return raw_codec.strip() or "Audio"


def _format_channel_layout(channels: int | None) -> str | None:
    """Format channel count into a human-friendly badge label."""
    if channels is None or channels <= 0:
        return None
    if channels == AppConstants.CHANNELS_MONO:
        return "1.0"
    if channels == AppConstants.CHANNELS_STEREO:
        return "2.0"
    if channels == AppConstants.CHANNELS_51:
        return "5.1"
    if channels == AppConstants.CHANNELS_71:
        return "7.1"
    return f"{channels}ch"


def _classify_resolution(pixel_dimensions: str | None) -> str | None:
    """Classify pixel dimensions (e.g. '1920x800') into standard resolution tiers.

    Uses both width and height thresholds so CinemaScope/anamorphic encodes
    like 1920x800 correctly classify as 1080p rather than 720p.
    """
    if not pixel_dimensions or "x" not in pixel_dimensions.lower():
        return None
    parts = pixel_dimensions.lower().split("x", maxsplit=1)
    width = _safe_int(parts[0].strip())
    height = _safe_int(parts[1].strip())
    if width is None or height is None or width <= 0 or height <= 0:
        return None

    if (
        height >= AppConstants.RES_2160P_MIN_HEIGHT
        or width >= AppConstants.RES_2160P_MIN_WIDTH
    ):
        return "2160p"
    if (
        height >= AppConstants.RES_1080P_MIN_HEIGHT
        or width >= AppConstants.RES_1080P_MIN_WIDTH
    ):
        return "1080p"
    if (
        height >= AppConstants.RES_720P_MIN_HEIGHT
        or width >= AppConstants.RES_720P_MIN_WIDTH
    ):
        return "720p"
    return "SD"


def _is_aac_51(raw_codec: str, codec_id: str | None, channels: int | None) -> bool:
    """Return True if track is a 6-channel AAC audio track."""
    if channels != AppConstants.CHANNELS_51:
        return False
    lower = raw_codec.lower()
    cid = (codec_id or "").upper()
    return "aac" in lower or cid.startswith("A_AAC")


def _is_ac3_or_eac3(raw_codec: str, codec_id: str | None) -> bool:
    """Return True if track is AC-3 (Dolby Digital) or E-AC-3 (Dolby Digital Plus)."""
    lower = raw_codec.lower()
    cid = (codec_id or "").upper()
    return (
        "ac-3" in lower
        or "ac3" in lower
        or "e-ac-3" in lower
        or "eac3" in lower
        or cid in ("A_AC3", "A_EAC3")
    )


def count_tag_entries_from_probe(raw_tags: Any) -> int:
    """Sum `num_entries` across `global_tags` or `track_tags` arrays."""
    if not isinstance(raw_tags, list):
        return 0
    total = 0
    for item in raw_tags:
        if isinstance(item, dict):
            num = _safe_int(item.get("num_entries"))
            total += num if num is not None and num > 0 else 1
    return total


def extract_attachments_from_probe(payload: dict[str, Any]) -> list[AttachmentInfo]:
    """Extract embedded attachment entries (fonts, cover art, logos, XML, etc.)."""
    raw_attachments = payload.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    result: list[AttachmentInfo] = []
    for idx, item in enumerate(raw_attachments):
        if not isinstance(item, dict):
            continue
        att_id = _safe_int(item.get("id"))
        props = item.get("properties")
        uid_val = (
            props.get("uid")
            if isinstance(props, dict) and props.get("uid") is not None
            else item.get("uid")
        )
        uid_str = (
            str(uid_val).strip()
            if uid_val is not None and not isinstance(uid_val, bool)
            else None
        )
        result.append(
            AttachmentInfo(
                id=att_id if att_id is not None else idx + 1,
                file_name=str(item.get("file_name") or f"attachment_{idx + 1}").strip(),
                content_type=str(
                    item.get("content_type") or "application/octet-stream"
                ).strip(),
                size_bytes=max(0, _safe_int(item.get("size")) or 0),
                description=str(item.get("description") or "").strip(),
                uid=uid_str or None,
            )
        )
    return result


def extract_tag_entries_from_probe(payload: dict[str, Any]) -> list[TagKeyValueInfo]:
    """Extract human-readable custom tag key-value entries from container & track metadata."""
    entries: list[TagKeyValueInfo] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(scope: str, key: str, value: Any) -> None:
        if value is None or isinstance(value, (bool, dict, list)):
            return
        k = key.strip().upper()
        v = str(value).strip()
        if not k or not v:
            return
        sig = (scope, k, v)
        if sig not in seen:
            seen.add(sig)
            entries.append(TagKeyValueInfo(scope=scope, key=k, value=v))

    container_tags = payload.get("container_tags")
    if isinstance(container_tags, dict):
        for raw_k, raw_v in container_tags.items():
            k_lower = str(raw_k or "").strip().lower()
            if not k_lower or k_lower in _IGNORED_FORMAT_TAG_KEYS:
                continue
            _add("Global", str(raw_k), raw_v)

    container = payload.get("container")
    container_props = (
        container.get("properties", {}) if isinstance(container, dict) else {}
    )
    if isinstance(container_props, dict):
        for raw_k, raw_v in container_props.items():
            k_str = str(raw_k or "").strip()
            if k_str.lower().startswith("tag_"):
                _add("Global", k_str[4:], raw_v)

    raw_tracks = payload.get("tracks")
    if isinstance(raw_tracks, list):
        for item in raw_tracks:
            if not isinstance(item, dict):
                continue
            track_id = _safe_int(item.get("id"))
            props = item.get("properties")
            if track_id is None or not isinstance(props, dict):
                continue
            for raw_k, raw_v in props.items():
                k_str = str(raw_k or "").strip()
                k_lower = k_str.lower()
                if not k_lower.startswith("tag_") or k_lower in _STATISTICS_TAG_KEYS:
                    continue
                _add(f"Track #{track_id}", k_str[4:], raw_v)

    return entries


def parse_mkvmerge_json(payload: dict[str, Any]) -> ParsedMediaProbe:
    """Parse an `mkvmerge -J` dictionary into structured container and track data."""
    container = payload.get("container")
    container_props = (
        container.get("properties", {}) if isinstance(container, dict) else {}
    )
    if not isinstance(container_props, dict):
        container_props = {}

    duration_us = _convert_duration_ns_to_us(container_props.get("duration"))
    raw_title = container_props.get("title")
    container_title = str(raw_title).strip() if raw_title is not None else ""

    # Explicitly inspect global_tags only — track_tags contains _STATISTICS_* on almost all MKVs
    global_tags = payload.get("global_tags")
    has_global_tags = isinstance(global_tags, list) and len(global_tags) > 0
    global_tags_count = count_tag_entries_from_probe(global_tags)

    track_tags = payload.get("track_tags")
    has_track_tags = isinstance(track_tags, list) and len(track_tags) > 0
    track_tags_count = count_tag_entries_from_probe(track_tags)

    parsed_attachments = extract_attachments_from_probe(payload)
    attachments_count = len(parsed_attachments)
    parsed_tag_entries = extract_tag_entries_from_probe(payload)

    chapters = payload.get("chapters")
    chapters_count = 0
    if isinstance(chapters, list):
        for chapter_entry in chapters:
            if isinstance(chapter_entry, dict):
                num_entries = _safe_int(chapter_entry.get("num_entries"))
                chapters_count += num_entries if num_entries and num_entries > 0 else 1

    raw_tracks = payload.get("tracks")
    if not isinstance(raw_tracks, list):
        raw_tracks = []

    parsed_tracks: list[ParsedTrackInfo] = []
    audio_summaries: list[AudioTrackSummary] = []
    has_aac_51 = False
    has_ac3_eac3 = False
    resolution: str | None = None
    video_codec: str | None = None
    video_dynamic_range: str | None = None
    subtitle_count = 0
    audio_counter = 0

    for item in raw_tracks:
        if not isinstance(item, dict):
            continue

        track_id = _safe_int(item.get("id"))
        if track_id is None:
            continue

        track_type = str(item.get("type") or "").strip().lower()
        raw_codec = str(item.get("codec") or "").strip()
        props = item.get("properties")
        if not isinstance(props, dict):
            props = {}

        codec_id = str(props["codec_id"]) if props.get("codec_id") else None
        language = str(props.get("language") or AppConstants.DEFAULT_LANGUAGE).strip()
        if not language:
            language = AppConstants.DEFAULT_LANGUAGE
        language_ietf = (
            str(props["language_ietf"]).strip() if props.get("language_ietf") else None
        )
        track_name = str(props.get("track_name") or "").strip()
        default_track = bool(props.get("default_track", False))
        forced_track = bool(props.get("forced_track", False))
        raw_min_ts_ns = _safe_int(props.get("minimum_timestamp"))
        minimum_timestamp_ns = raw_min_ts_ns if raw_min_ts_ns is not None else 0
        sync_delay_ms = _convert_min_timestamp_ns_to_ms(minimum_timestamp_ns)

        audio_channels: int | None = None
        audio_sampling_freq: int | None = None
        audio_index: int | None = None
        pixel_dimensions: str | None = None

        if track_type == "video":
            raw_dims = props.get("pixel_dimensions")
            pixel_dimensions = str(raw_dims).strip() if raw_dims else None
            if resolution is None:
                resolution = _classify_resolution(pixel_dimensions)
            if video_codec is None:
                video_codec = _normalize_video_codec(raw_codec, codec_id)
            if video_dynamic_range is None:
                video_dynamic_range = _classify_video_dynamic_range(
                    raw_codec, codec_id, props
                )

        elif track_type == "audio":
            audio_channels = _safe_int(props.get("audio_channels"))
            audio_sampling_freq = _safe_int(props.get("audio_sampling_frequency"))
            audio_index = audio_counter
            audio_counter += 1

            if _is_aac_51(raw_codec, codec_id, audio_channels):
                has_aac_51 = True
            if _is_ac3_or_eac3(raw_codec, codec_id):
                has_ac3_eac3 = True

            audio_summaries.append(
                AudioTrackSummary(
                    codec=_normalize_audio_codec(raw_codec, codec_id),
                    channels=audio_channels,
                    channel_layout=_format_channel_layout(audio_channels),
                    language=language,
                )
            )

        elif track_type == "subtitles":
            subtitle_count += 1

        parsed_tracks.append(
            ParsedTrackInfo(
                id=track_id,
                type=track_type,
                codec=raw_codec,
                codec_id=codec_id,
                language=language,
                language_ietf=language_ietf,
                track_name=track_name,
                default_track=default_track,
                forced_track=forced_track,
                audio_channels=audio_channels,
                audio_sampling_frequency=audio_sampling_freq,
                audio_index=audio_index,
                pixel_dimensions=pixel_dimensions,
                minimum_timestamp_ns=minimum_timestamp_ns,
                sync_delay_ms=sync_delay_ms,
                size_bytes=extract_track_size_bytes(props),
            )
        )

    audio_summary_json = json.dumps(
        [entry.model_dump() for entry in audio_summaries],
        separators=(",", ":"),
    )

    return ParsedMediaProbe(
        duration_us=duration_us,
        container_title=container_title,
        has_global_tags=has_global_tags,
        has_track_tags=has_track_tags,
        attachments_count=attachments_count,
        chapters_count=chapters_count,
        has_aac_51=has_aac_51,
        has_ac3_eac3=has_ac3_eac3,
        resolution=resolution,
        video_codec=video_codec,
        video_dynamic_range=video_dynamic_range,
        audio_summary=audio_summaries,
        audio_summary_json=audio_summary_json,
        subtitle_count=subtitle_count,
        tracks=parsed_tracks,
        global_tags_count=global_tags_count,
        track_tags_count=track_tags_count,
        attachments=tuple(parsed_attachments),
        tag_entries=tuple(parsed_tag_entries),
    )
