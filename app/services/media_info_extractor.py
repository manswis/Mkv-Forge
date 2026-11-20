"""Pure extractor building comprehensive read-only MediaInfo views from cached mkvmerge -J JSON."""

from __future__ import annotations

from typing import Any

from app.config import AppConstants
from app.models.schemas import (
    AttachmentInfo,
    ContainerInfoSummary,
    DetailedTrackInfo,
    MediaFileInfoResponse,
)
from app.services.probe_parser import (
    _classify_video_dynamic_range,
    _convert_duration_ns_to_us,
    _convert_min_timestamp_ns_to_ms,
    _format_channel_layout,
    _safe_int,
    parse_mkvmerge_json,
)

_NS_PER_SEC: float = 1_000_000_000.0
_US_PER_SEC: float = 1_000_000.0
_BITS_PER_BYTE: int = 8

_COLOR_PRIMARIES_MAP: dict[int, str] = {
    1: "BT.709",
    4: "BT.470M",
    5: "BT.470BG",
    6: "SMPTE 170M",
    7: "SMPTE 240M",
    8: "Film",
    9: "BT.2020",
    11: "DCI-P3",
    12: "Display P3",
}

_COLOR_TRANSFER_MAP: dict[int, str] = {
    1: "BT.709",
    4: "Gamma 2.2",
    5: "Gamma 2.8",
    6: "SMPTE 170M",
    8: "Linear",
    13: "sRGB",
    14: "BT.2020 (10-bit)",
    15: "BT.2020 (12-bit)",
    16: "SMPTE ST 2084 (PQ)",
    18: "ARIB STD-B67 (HLG)",
}

_COLOR_MATRIX_MAP: dict[int, str] = {
    0: "RGB",
    1: "BT.709",
    5: "BT.470BG",
    6: "SMPTE 170M",
    9: "BT.2020 NCL",
    10: "BT.2020 CL",
}


def _clean_str(value: Any) -> str | None:
    """Return stripped non-empty string or None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text else None


def _format_frame_rate(default_duration_ns: Any) -> str | None:
    """Convert mkvmerge track `default_duration` (ns per frame) to readable fps."""
    dur_ns = _safe_int(default_duration_ns)
    if dur_ns is None or dur_ns <= 0:
        return None
    fps = _NS_PER_SEC / float(dur_ns)
    if abs(fps - round(fps)) < 0.005:
        return f"{round(fps)} fps"
    return f"{fps:.3f} fps"


def _compute_overall_bitrate_bps(size_bytes: int, duration_us: int) -> int | None:
    """Compute overall container bitrate in bits/sec when both size and duration are positive."""
    if size_bytes <= 0 or duration_us <= 0:
        return None
    duration_sec = duration_us / _US_PER_SEC
    if duration_sec <= 0:
        return None
    return int(round((size_bytes * _BITS_PER_BYTE) / duration_sec))


def _lookup_color_code(props: dict[str, Any], keys: tuple[str, ...], mapping: dict[int, str]) -> str | None:
    """Resolve an ITU-T H.273 integer color code into a readable label."""
    for key in keys:
        val = _safe_int(props.get(key))
        if val is not None:
            return mapping.get(val, f"Code {val}")
    return None


def _count_tag_entries(raw_tags: Any) -> int:
    """Sum `num_entries` across global_tags or track_tags arrays."""
    if not isinstance(raw_tags, list):
        return 0
    total = 0
    for item in raw_tags:
        if isinstance(item, dict):
            num = _safe_int(item.get("num_entries"))
            total += num if num is not None and num > 0 else 1
    return total


def _extract_attachments(payload: dict[str, Any]) -> list[AttachmentInfo]:
    """Extract embedded attachment entries (fonts, cover art, XML, etc.)."""
    raw_attachments = payload.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    result: list[AttachmentInfo] = []
    for idx, item in enumerate(raw_attachments):
        if not isinstance(item, dict):
            continue
        att_id = _safe_int(item.get("id"))
        result.append(
            AttachmentInfo(
                id=att_id if att_id is not None else idx + 1,
                file_name=str(item.get("file_name") or f"attachment_{idx + 1}").strip(),
                content_type=str(item.get("content_type") or "application/octet-stream").strip(),
                size_bytes=max(0, _safe_int(item.get("size")) or 0),
                description=str(item.get("description") or "").strip(),
            )
        )
    return result


def extract_media_file_info(
    row: dict[str, Any],
    raw_payload: dict[str, Any],
) -> MediaFileInfoResponse:
    """Build a complete MediaInfo-style payload from a SQLite row and parsed `mkvmerge -J` dict."""
    probe = parse_mkvmerge_json(raw_payload)

    container_dict = raw_payload.get("container")
    if not isinstance(container_dict, dict):
        container_dict = {}
    container_props = container_dict.get("properties")
    if not isinstance(container_props, dict):
        container_props = {}

    size_bytes = int(row.get("size_bytes") or 0)
    duration_us = int(row.get("duration_us") or probe.duration_us or _convert_duration_ns_to_us(container_props.get("duration")))
    attachments = _extract_attachments(raw_payload)

    container_summary = ContainerInfoSummary(
        container_type=str(container_dict.get("type") or "Unknown").strip(),
        title=probe.container_title,
        duration_us=duration_us,
        size_bytes=size_bytes,
        overall_bitrate_bps=_compute_overall_bitrate_bps(size_bytes, duration_us),
        muxing_application=_clean_str(container_props.get("muxing_application")),
        writing_application=_clean_str(container_props.get("writing_application")),
        date_utc=_clean_str(container_props.get("date_utc")),
        segment_uid=_clean_str(container_props.get("segment_uid")),
        chapters_count=probe.chapters_count,
        attachments_count=len(attachments),
        global_tags_count=_count_tag_entries(raw_payload.get("global_tags")),
        track_tags_count=_count_tag_entries(raw_payload.get("track_tags")),
    )

    video_tracks: list[DetailedTrackInfo] = []
    audio_tracks: list[DetailedTrackInfo] = []
    subtitle_tracks: list[DetailedTrackInfo] = []
    other_tracks: list[DetailedTrackInfo] = []

    raw_tracks = raw_payload.get("tracks")
    if not isinstance(raw_tracks, list):
        raw_tracks = []

    audio_counter = 0
    for item in raw_tracks:
        if not isinstance(item, dict):
            continue
        track_id = _safe_int(item.get("id"))
        if track_id is None:
            continue
        track_type = str(item.get("type") or "").strip().lower()
        raw_codec = str(item.get("codec") or "").strip() or "Unknown"
        props = item.get("properties")
        if not isinstance(props, dict):
            props = {}

        codec_id = _clean_str(props.get("codec_id"))
        language = _clean_str(props.get("language")) or AppConstants.DEFAULT_LANGUAGE
        language_ietf = _clean_str(props.get("language_ietf"))
        track_name = str(props.get("track_name") or "").strip()
        uid = _clean_str(props.get("uid"))
        bitrate_bps = _safe_int(props.get("tag_bps"))
        frame_count = _safe_int(props.get("tag_number_of_frames"))
        sync_delay_ms = _convert_min_timestamp_ns_to_ms(props.get("minimum_timestamp"))

        common_kwargs: dict[str, Any] = {
            "id": track_id,
            "type": track_type or "other",
            "codec": raw_codec,
            "codec_id": codec_id,
            "language": language,
            "language_ietf": language_ietf,
            "track_name": track_name,
            "uid": uid,
            "default_track": bool(props.get("default_track", False)),
            "forced_track": bool(props.get("forced_track", False)),
            "enabled_track": bool(props.get("enabled_track", True)),
            "flag_original": bool(props.get("flag_original", False)),
            "flag_commentary": bool(props.get("flag_commentary", False)),
            "flag_hearing_impaired": bool(props.get("flag_hearing_impaired", False)),
            "flag_visual_impaired": bool(props.get("flag_visual_impaired", False)),
            "bitrate_bps": bitrate_bps if bitrate_bps and bitrate_bps > 0 else None,
            "frame_count": frame_count if frame_count and frame_count > 0 else None,
            "sync_delay_ms": sync_delay_ms,
        }

        if track_type == "video":
            video_tracks.append(
                DetailedTrackInfo(
                    **common_kwargs,
                    pixel_dimensions=_clean_str(props.get("pixel_dimensions")),
                    display_dimensions=_clean_str(props.get("display_dimensions")),
                    frame_rate=_format_frame_rate(props.get("default_duration")),
                    dynamic_range=_classify_video_dynamic_range(raw_codec, codec_id, props),
                    color_primaries=_lookup_color_code(
                        props, ("color_primaries", "colour_primaries"), _COLOR_PRIMARIES_MAP
                    ),
                    color_transfer=_lookup_color_code(
                        props,
                        ("color_transfer_characteristics", "colour_transfer_characteristics"),
                        _COLOR_TRANSFER_MAP,
                    ),
                    color_matrix=_lookup_color_code(
                        props,
                        ("color_matrix_coefficients", "colour_matrix_coefficients"),
                        _COLOR_MATRIX_MAP,
                    ),
                    bit_depth=_safe_int(
                        props.get("color_bits_per_channel")
                        if props.get("color_bits_per_channel") is not None
                        else props.get("colour_bits_per_channel")
                    ),
                )
            )
        elif track_type == "audio":
            channels = _safe_int(props.get("audio_channels"))
            audio_tracks.append(
                DetailedTrackInfo(
                    **common_kwargs,
                    audio_index=audio_counter,
                    audio_channels=channels,
                    channel_layout=_format_channel_layout(channels),
                    audio_sampling_frequency=_safe_int(props.get("audio_sampling_frequency")),
                    audio_bits_per_sample=_safe_int(props.get("audio_bits_per_sample")),
                )
            )
            audio_counter += 1
        elif track_type == "subtitles":
            raw_text_subs = props.get("text_subtitles")
            subtitle_tracks.append(
                DetailedTrackInfo(
                    **common_kwargs,
                    text_subtitles=bool(raw_text_subs) if isinstance(raw_text_subs, bool) else None,
                    encoding=_clean_str(props.get("encoding")),
                )
            )
        else:
            other_tracks.append(DetailedTrackInfo(**common_kwargs))

    raw_warnings = raw_payload.get("warnings")
    warnings = [str(w).strip() for w in raw_warnings if str(w).strip()] if isinstance(raw_warnings, list) else []

    return MediaFileInfoResponse(
        id=int(row["id"]),
        filepath=str(row["filepath"]),
        folder=str(row["folder"]),
        filename=str(row["filename"]),
        scanned_at=str(row["scanned_at"]),
        resolution=row.get("resolution") or probe.resolution,
        video_codec=row.get("video_codec") or probe.video_codec,
        video_dynamic_range=row.get("video_dynamic_range") or probe.video_dynamic_range,
        container=container_summary,
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        other_tracks=other_tracks,
        attachments=list(probe.attachments),
        tag_entries=list(probe.tag_entries),
        warnings=warnings,
    )
