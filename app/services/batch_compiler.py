"""Pure declarative rule-to-JobConfig compiler for Batch Processing and Templates."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.config import AppConstants
from app.db.repositories import decode_audio_summary_json
from app.models.schemas import (
    AudioConversionConfig,
    BatchAudioRule,
    BatchContainerRule,
    BatchFileChip,
    BatchFileOverride,
    BatchFilePlan,
    BatchInventorySummary,
    BatchPreviewResponse,
    BatchRuleSpec,
    BatchTrackFilterRule,
    ContainerMetadataConfig,
    JobChangeItem,
    JobConfig,
    ParsedTrackInfo,
    TrackEditConfig,
)
from app.services.ffmpeg_runner import (
    canonicalize_audio_codec,
    compute_target_channels,
    is_lossless_source_codec,
    resolve_effective_bitrate,
)
from app.services.job_summary import summarize_job_changes
from app.services.probe_parser import parse_mkvmerge_json
from app.utils.paths import PathSafetyError, safe_resolve_path

_COMMENTARY_OR_AD_RE = re.compile(
    r"\b("
    r"commentary|comments|audio\s*description|visually\s*impaired|"
    r"descriptive|director(?:'s)?|cast|crew|isolated\s*score|"
    r"narration|ad|vi|sdh"
    r")\b",
    re.IGNORECASE,
)

_NATURAL_SORT_CHUNK_RE = re.compile(r"(\d+)")
_SEASON_EPISODE_RE = re.compile(
    r"(?:^|[\s._\-\[\(])s(\d{1,2})[\s._\-]*e(\d{1,4})(?:$|[\s._\-\]\)])",
    re.IGNORECASE,
)
_X_SEASON_EPISODE_RE = re.compile(
    r"(?:^|[\s._\-\[\(])(\d{1,2})x(\d{1,4})(?:$|[\s._\-\]\)])",
    re.IGNORECASE,
)
_EPISODE_ONLY_RE = re.compile(
    r"(?:^|[\s._\-\[\(])(?:episode|ep\.?|e)[\s._\-]*(\d{1,4})(?:$|[\s._\-\]\)])",
    re.IGNORECASE,
)
_BARE_EPISODE_NUMBER_RE = re.compile(
    r"(?:[\s._\-]\-\s*|[\[\(])(\d{1,3})(?:[\]\)]|[\s._\-]|$)"
)
_RENAME_TOKEN_RE = re.compile(r"\{(ep|season|n|orig)\}", re.IGNORECASE)
_FORBIDDEN_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NON_EPISODE_NUMBERS: frozenset[int] = frozenset({264, 265, 480, 576, 720, 1080, 2160})


def natural_sort_key(text: str) -> list[tuple[int, int | str]]:
    """Return a natural-sort key so `Ep2.mkv` sorts strictly before `Ep10.mkv` (Guardrail #1)."""
    parts = _NATURAL_SORT_CHUNK_RE.split((text or "").lower())
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part))
    return key


def extract_season_episode(filename: str) -> tuple[int | None, int | None]:
    """Smartly extract `(season, episode)` integers from a media filename."""
    stem = Path(filename or "").stem
    if not stem:
        return None, None

    m = _SEASON_EPISODE_RE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = _X_SEASON_EPISODE_RE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = _EPISODE_ONLY_RE.search(stem)
    if m:
        return None, int(m.group(1))

    for m in _BARE_EPISODE_NUMBER_RE.finditer(stem):
        num = int(m.group(1))
        if num in _NON_EPISODE_NUMBERS or 1900 <= num <= 2099:
            continue
        return None, num

    return None, None


def sanitize_batch_output_filename(raw_name: str | None) -> str:
    """Sanitize a user-supplied or pattern-rendered output filename and enforce `.mkv`."""
    if not raw_name:
        return ""
    cleaned = _FORBIDDEN_FILENAME_CHARS_RE.sub("", str(raw_name).strip())
    cleaned = cleaned.replace("..", "").strip(" .")
    if not cleaned:
        return ""
    if not cleaned.lower().endswith(AppConstants.EXT_MKV):
        # Strip an existing video extension like .mp4/.m4v before appending .mkv
        stem = Path(cleaned).stem if Path(cleaned).suffix.lower() in (".mp4", ".m4v", ".mov", ".avi") else cleaned
        cleaned = f"{stem.strip(' .')}{AppConstants.EXT_MKV}"
    if cleaned.lower() == AppConstants.EXT_MKV:
        return ""
    return cleaned


def _normalize_destination_folder(raw_folder: str | None, fallback_folder: str) -> str:
    """Normalize a virtual folder path (e.g. `'/Movies/S1'` or `'/'`)."""
    candidate = (raw_folder or "").strip()
    if not candidate:
        candidate = (fallback_folder or "/").strip()
    stripped = candidate.strip("/")
    return f"/{stripped}" if stripped else "/"


def _join_rel_storage_path(folder: str, filename: str) -> str:
    """Join a normalized virtual folder (`'/'` or `'/Movies/S1'`) and basename into a relative storage path."""
    clean_folder = folder.strip("/")
    return f"{clean_folder}/{filename}" if clean_folder else filename


def render_batch_output_filename(
    source_filename: str,
    *,
    rule: BatchRuleSpec,
    seq_index: int,
    pad_digits: int,
    override_filename: str | None = None,
) -> tuple[str | None, bool, bool]:
    """Compute target output basename from inline override or batch rename pattern.

    Returns `(resolved_filename_or_None, is_inline_override, used_episode_fallback)`.
    """
    if override_filename is not None and override_filename.strip():
        sanitized = sanitize_batch_output_filename(override_filename)
        if sanitized:
            return sanitized, True, False

    if not rule.rename.enabled:
        return None, False, False

    raw_pattern = (rule.rename.pattern or "").strip()
    if not raw_pattern:
        return None, False, False

    # Zero-syntax prefix convenience: typing `SOME_PREFIX_S1_` without tokens auto-appends `{ep}`
    effective_pattern = (
        raw_pattern
        if _RENAME_TOKEN_RE.search(raw_pattern)
        else f"{raw_pattern}{{ep}}"
    )
    season_num, ep_num = extract_season_episode(source_filename)
    used_ep_fallback = False
    if ep_num is None:
        ep_num = seq_index
        used_ep_fallback = "{ep}" in effective_pattern.lower()
    effective_season = season_num if season_num is not None else 1
    orig_stem = Path(source_filename).stem

    def _replace_token(match: re.Match[str]) -> str:
        tok = match.group(1).lower()
        if tok == "ep":
            return str(ep_num).zfill(pad_digits)
        if tok == "season":
            return str(effective_season).zfill(AppConstants.BATCH_RENAME_PAD_DIGITS_2)
        if tok == "n":
            return str(seq_index).zfill(pad_digits)
        if tok == "orig":
            return orig_stem
        return match.group(0)

    rendered = _RENAME_TOKEN_RE.sub(_replace_token, effective_pattern)
    sanitized = sanitize_batch_output_filename(rendered)
    return (sanitized or None), False, used_ep_fallback


def normalize_lang_code(raw_lang: str | None) -> str:
    """Normalize an ISO-639 language code (`en` -> `eng`, empty/None -> `und`)."""
    if not raw_lang:
        return AppConstants.DEFAULT_LANGUAGE
    cleaned = raw_lang.strip().lower()
    if cleaned in ("", "und", "null", "unknown", "none"):
        return AppConstants.DEFAULT_LANGUAGE
    return AppConstants.ISO_LANG_ALIAS_MAP.get(cleaned, cleaned)


def is_commentary_or_description_track(track_name: str | None) -> bool:
    """Return True if a track's title indicates commentary, director notes, or audio description (AD)."""
    if not track_name or not track_name.strip():
        return False
    return bool(_COMMENTARY_OR_AD_RE.search(track_name.strip()))


def format_channel_label(channels: int | None) -> str:
    """Format an integer channel count into a canonical badge label (`5.1`, `7.1`, `2.0`, `1.0`)."""
    ch = int(channels or 0)
    mapping = {
        1: "1.0",
        2: "2.0",
        3: "2.1",
        4: "4.0",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
    }
    if ch in mapping:
        return mapping[ch]
    if ch > 0:
        return f"{ch}ch"
    return "5.1"


def _language_display_name(raw_lang: str | None) -> str:
    """Return human-readable language name (e.g. 'English') or uppercase code when unknown."""
    code = (raw_lang or "").strip().lower()
    if not code or code == AppConstants.DEFAULT_LANGUAGE:
        return ""
    label = AppConstants.LANGUAGE_DISPLAY_LABELS.get(code)
    if label:
        return label.split(" (", 1)[0].strip()
    return code.upper()


def format_smart_track_badge(
    track: ParsedTrackInfo,
    *,
    effective_language: str | None = None,
    track_format: str = AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT,
) -> str:
    """Build the clean 'Smart fill' track name matching the selected user preset."""
    fmt = (
        track_format
        if track_format in AppConstants.ALLOWED_SMART_FILL_TRACK_FORMATS
        else AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT
    )
    codec = track.codec or "Unknown"
    lang = (effective_language if effective_language is not None else track.language) or ""
    lang_clean = lang.strip()
    has_lang = bool(lang_clean and lang_clean.lower() != AppConstants.DEFAULT_LANGUAGE)
    lang_name = _language_display_name(lang_clean) if has_lang else ""

    if fmt == AppConstants.SMART_FILL_FORMAT_COMPACT:
        if track.type == "audio":
            return (
                f"{codec} {format_channel_label(track.audio_channels)}"
                if track.audio_channels
                else codec
            )
        if track.type == "video":
            return f"{codec} {track.pixel_dimensions}" if track.pixel_dimensions else codec
        return f"{lang_name} {codec}".strip() if lang_name else codec

    if fmt == AppConstants.SMART_FILL_FORMAT_LANG_CODEC:
        if track.type == "audio":
            spec = (
                f"{codec} {format_channel_label(track.audio_channels)}"
                if track.audio_channels
                else codec
            )
            return f"{lang_name} · {spec}" if lang_name else spec
        if track.type == "video":
            return f"{codec} · {track.pixel_dimensions}" if track.pixel_dimensions else codec
        return f"{lang_name} · {codec}" if lang_name else codec

    # Default ('detailed'): exact original format (`Audio • EAC3 • 5.1 • eng`)
    type_label = (
        "Video"
        if track.type == "video"
        else "Audio"
        if track.type == "audio"
        else "Sub"
    )
    pieces: list[str] = [type_label, codec]
    if track.type == "audio" and track.audio_channels:
        pieces.append(format_channel_label(track.audio_channels))
    elif track.type == "video" and track.pixel_dimensions:
        pieces.append(track.pixel_dimensions)
    if has_lang:
        pieces.append(lang_clean)
    return " • ".join(pieces)


def build_converted_audio_title(
    *,
    target_codec: str,
    target_channels: int,
    bitrate: str | None,
    language: str,
    track_format: str = AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT,
) -> str:
    """Build the default track title for a newly converted audio track per preset."""
    fmt = (
        track_format
        if track_format in AppConstants.ALLOWED_SMART_FILL_TRACK_FORMATS
        else AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT
    )
    codec_labels = {
        "eac3": "EAC3",
        "ac3": "AC3",
        "aac": "AAC",
        "opus": "Opus",
        "flac": "FLAC",
    }
    label = codec_labels.get(target_codec, target_codec.upper())
    ch_label = format_channel_label(target_channels)
    spec = f"{label} {ch_label}"
    norm_lang = (language or "").strip().lower()
    has_lang = bool(norm_lang and norm_lang != AppConstants.DEFAULT_LANGUAGE)

    if fmt == AppConstants.SMART_FILL_FORMAT_COMPACT:
        return spec

    if fmt == AppConstants.SMART_FILL_FORMAT_LANG_CODEC:
        lang_name = _language_display_name(norm_lang) if has_lang else ""
        return f"{lang_name} · {spec}" if lang_name else spec

    pieces = [spec]
    if target_codec not in AppConstants.LOSSLESS_TARGET_CODECS and bitrate:
        pieces.append(bitrate)
    if has_lang:
        pieces.append(norm_lang)
    pieces.append("muxed")
    return " • ".join(pieces)


def resolve_default_bitrate_for_codec(target_codec: str, channels: int) -> str | None:
    """Return a valid default bitrate for `target_codec` and `channels`."""
    if target_codec in AppConstants.LOSSLESS_TARGET_CODECS:
        return None
    defaults_by_codec: dict[str, dict[int, str]] = {
        "eac3": {1: "128k", 2: "256k", 6: "640k"},
        "ac3": {1: "128k", 2: "256k", 6: "448k"},
        "aac": {1: "128k", 2: "256k", 6: "448k", 8: "640k"},
        "opus": {1: "96k", 2: "192k", 6: "320k", 8: "448k"},
    }
    by_ch = defaults_by_codec.get(target_codec, {})
    if channels in by_ch:
        return by_ch[channels]
    if channels >= 6 and 6 in by_ch:
        return by_ch[6]
    if channels >= 2 and 2 in by_ch:
        return by_ch[2]
    return AppConstants.DEFAULT_BITRATE


def select_primary_audio_track(
    audio_tracks: list[ParsedTrackInfo],
    *,
    rule: BatchAudioRule,
    user_preferred_language: str = AppConstants.DEFAULT_PREFERRED_LANGUAGE,
) -> ParsedTrackInfo | None:
    """Deterministically select the primary source audio track for a file (Guardrail #1).

    Ranking order:
    1. Non-commentary / non-AD tracks (`is_commentary_or_description_track == False`) ALWAYS
       rank strictly ahead of commentary/description/AD tracks (so an English commentary track
       never beats a non-English main audio track).
    2. Language tier:
       - If `source_language_mode == 'first_audio'`: all tracks share language tier 0.
       - Otherwise: Tier 0 = matches preferred/custom language, Tier 1 = untagged (`und`),
         Tier 2 = other language fallback.
    3. Highest channel count (`audio_channels`, descending).
    4. Default track flag (`default_track == True` before `False`).
    5. Lowest track index in the container (`order_idx`, ascending).
    """
    if not audio_tracks:
        return None

    target_lang = normalize_lang_code(
        user_preferred_language
        if rule.source_language_mode == AppConstants.BATCH_AUDIO_LANG_MODE_PREFERRED
        else rule.custom_source_language
    )

    def _rank_key(item: tuple[int, ParsedTrackInfo]) -> tuple[int, int, int, int, int]:
        order_idx, track = item
        track_lang = normalize_lang_code(track.language)
        if rule.source_language_mode == AppConstants.BATCH_AUDIO_LANG_MODE_FIRST:
            lang_tier = 0
        elif track_lang == target_lang:
            lang_tier = 0
        elif track_lang == AppConstants.DEFAULT_LANGUAGE:
            lang_tier = 1
        else:
            lang_tier = 2

        commentary_penalty = 1 if is_commentary_or_description_track(track.track_name) else 0
        channels = int(track.audio_channels or 0)
        default_rank = 0 if track.default_track else 1
        return (commentary_penalty, lang_tier, -channels, default_rank, order_idx)

    indexed = list(enumerate(audio_tracks))
    _, best_track = min(indexed, key=_rank_key)
    return best_track


def _track_matches_exact_audio_target(
    track: ParsedTrackInfo,
    rule: BatchAudioRule,
    reference_channels: int,
) -> bool:
    """Return True if `track` already matches the exact target codec and channel tier."""
    canonical = canonicalize_audio_codec(track.codec)
    if not canonical:
        return False
    track_ch = int(track.audio_channels or 0) or AppConstants.MAX_DOLBY_CHANNELS
    expected_out_ch = compute_target_channels(
        reference_channels,
        target_codec=rule.target_codec,
        requested_channels=rule.target_channels,
    )
    is_explicit_downmix = (
        rule.target_channels is not None and expected_out_ch < track_ch
    )
    if is_explicit_downmix:
        return False

    return canonical == rule.target_codec and track_ch >= expected_out_ch


def _track_satisfies_audio_target(
    track: ParsedTrackInfo,
    rule: BatchAudioRule,
    reference_channels: int,
) -> bool:
    """Return True if `track` already satisfies the desired target audio codec & channel tier."""
    if _track_matches_exact_audio_target(track, rule, reference_channels):
        return True

    # When user explicitly chooses 'any_non_target' ("All files not already in <Target>"),
    # only an exact codec+channel match satisfies the target (e.g. AC-3 can upgrade to E-AC-3).
    if rule.match_condition == AppConstants.BATCH_MATCH_ANY_NON_TARGET:
        return False

    canonical = canonicalize_audio_codec(track.codec)
    if not canonical:
        return False
    track_ch = int(track.audio_channels or 0) or AppConstants.MAX_DOLBY_CHANNELS
    expected_out_ch = compute_target_channels(
        reference_channels,
        target_codec=rule.target_codec,
        requested_channels=rule.target_channels,
    )
    if rule.target_channels is not None and expected_out_ch < track_ch:
        return False

    if (
        rule.accept_existing_dolby
        and rule.target_codec in (AppConstants.TARGET_CODEC_EAC3, AppConstants.TARGET_CODEC_AC3)
        and canonical in (AppConstants.TARGET_CODEC_EAC3, AppConstants.TARGET_CODEC_AC3)
        and track_ch >= min(reference_channels, AppConstants.MAX_DOLBY_CHANNELS)
    ):
        return True

    return False


def _short_subtitle_codec_label(codec: str | None) -> str:
    """Shorten verbose mkvmerge subtitle codec names into compact chip labels."""
    raw = (codec or "").strip()
    upper = raw.upper()
    if "SUBRIP" in upper or "SRT" in upper:
        return "SRT"
    if "ASS" in upper or "SSA" in upper:
        return "ASS"
    if "PGS" in upper or "HDMV" in upper:
        return "PGS"
    if "VOBSUB" in upper:
        return "VobSub"
    if "WEBVTT" in upper or "VTT" in upper:
        return "VTT"
    if "TEXT" in upper:
        return "Text"
    return raw or "Sub"


def _short_video_codec_label(codec: str | None) -> str:
    """Shorten verbose mkvmerge video codec names for compact per-file chip labels."""
    raw = (codec or "").strip()
    upper = raw.upper()
    if "HEVC" in upper or "H.265" in upper or "H265" in upper:
        return "HEVC"
    if "AVC" in upper or "H.264" in upper or "H264" in upper:
        return "H.264"
    if "AV1" in upper:
        return "AV1"
    if "VP9" in upper:
        return "VP9"
    if "MPEG-2" in upper or "MPEG2" in upper:
        return "MPEG-2"
    if "VC-1" in upper or "VC1" in upper:
        return "VC-1"
    return raw or "Video"


def _short_audio_codec_label(codec: str | None) -> str:
    """Shorten audio codec names for compact per-file chip labels."""
    canonical = canonicalize_audio_codec(codec)
    mapping = {
        "eac3": "EAC3",
        "ac3": "AC3",
        "aac": "AAC",
        "opus": "Opus",
        "flac": "FLAC",
    }
    if canonical in mapping:
        return mapping[canonical]
    return (codec or "Audio").strip() or "Audio"


def _resolve_retag_target_language(rule: BatchTrackFilterRule) -> str:
    """Return normalized retag target language (`und` means no retag)."""
    return normalize_lang_code(rule.retag_und_target_language)


def compile_batch_rule_for_file(
    row: dict[str, Any],
    rule: BatchRuleSpec,
    *,
    active_job_filepaths: set[str] | None = None,
    user_preferred_language: str = AppConstants.DEFAULT_PREFERRED_LANGUAGE,
    smart_fill_track_format: str = AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT,
    file_override: BatchFileOverride | None = None,
    seq_index: int = AppConstants.BATCH_RENAME_START_DEFAULT,
    pad_digits: int = AppConstants.BATCH_RENAME_PAD_DIGITS_2,
) -> BatchFilePlan:
    """Compile a declarative `BatchRuleSpec` + optional `BatchFileOverride` into a `BatchFilePlan`."""
    file_id = int(row["id"])
    filepath = str(row["filepath"])
    filename = str(row["filename"])
    folder = str(row.get("folder") or "/")
    size_bytes = int(row.get("size_bytes") or 0)
    scan_status = str(row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)

    # Guardrail #3: Active-job duplicate guard (never bypassed by per-file overrides)
    if active_job_filepaths and filepath in active_job_filepaths:
        return BatchFilePlan(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            folder=folder,
            size_bytes=size_bytes,
            action=AppConstants.BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB,  # type: ignore[arg-type]
            stage_plan=AppConstants.BATCH_STAGE_NONE,  # type: ignore[arg-type]
            reason="Skip (Already in Queue)",
        )

    if scan_status != AppConstants.FILE_SCAN_STATUS_READY:
        label = (
            "Skip (Metadata scan in progress)"
            if scan_status == AppConstants.FILE_SCAN_STATUS_SCANNING
            else "Skip (Metadata scan failed — rescan file first)"
        )
        return BatchFilePlan(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            folder=folder,
            size_bytes=size_bytes,
            action=AppConstants.BATCH_PLAN_ACTION_SKIP_NOT_READY,  # type: ignore[arg-type]
            stage_plan=AppConstants.BATCH_STAGE_NONE,  # type: ignore[arg-type]
            reason=label,
        )

    raw_json_str = str(row.get("raw_json") or "{}")
    try:
        raw_payload = json.loads(raw_json_str)
    except (TypeError, json.JSONDecodeError):
        raw_payload = {}
    probe = parse_mkvmerge_json(raw_payload if isinstance(raw_payload, dict) else {})

    all_tracks = [
        t for t in probe.tracks if t.type in ("video", "audio", "subtitles")
    ]
    if not all_tracks:
        return BatchFilePlan(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            folder=folder,
            size_bytes=size_bytes,
            action=AppConstants.BATCH_PLAN_ACTION_SKIP_WARNING,  # type: ignore[arg-type]
            stage_plan=AppConstants.BATCH_STAGE_NONE,  # type: ignore[arg-type]
            reason="Skip (No editable tracks found)",
            warnings=["File has no video, audio, or subtitle tracks in cached metadata."],
        )

    video_tracks = [t for t in all_tracks if t.type == "video"]
    audio_tracks = [t for t in all_tracks if t.type == "audio"]
    sub_tracks = [t for t in all_tracks if t.type == "subtitles"]
    warnings: list[str] = []
    override = file_override

    effective_pref_lang = normalize_lang_code(
        rule.track_filter.preferred_language or user_preferred_language
    )
    retag_target_lang = _resolve_retag_target_language(rule.track_filter)

    primary_audio = select_primary_audio_track(
        audio_tracks,
        rule=rule.audio,
        user_preferred_language=effective_pref_lang,
    )

    source_audio_summary: str | None = None
    if primary_audio is not None:
        ch_lbl = format_channel_label(primary_audio.audio_channels)
        lang_lbl = primary_audio.language or AppConstants.DEFAULT_LANGUAGE
        source_audio_summary = (
            f"Track #{primary_audio.id} ({primary_audio.codec} {ch_lbl} • {lang_lbl})"
        )

    primary_orig_lang = (
        (primary_audio.language or AppConstants.DEFAULT_LANGUAGE)
        if primary_audio is not None
        else AppConstants.DEFAULT_LANGUAGE
    )
    primary_norm_lang = normalize_lang_code(primary_orig_lang)
    can_retag_primary = (
        primary_audio is not None
        and retag_target_lang != AppConstants.DEFAULT_LANGUAGE
        and primary_norm_lang != retag_target_lang
    )
    default_retag_und = bool(
        rule.track_filter.retag_und_audio_to_preferred and can_retag_primary
    )
    if override is not None and override.retag_und_enabled is not None:
        effective_retag_und = bool(
            rule.track_filter.retag_und_audio_to_preferred
            and override.retag_und_enabled
            and can_retag_primary
        )
    else:
        effective_retag_und = default_retag_und

    # ------------------------------------------------------------------
    # 1. Evaluate Audio Conversion Rule (with mixed-codec intelligence + override)
    # ------------------------------------------------------------------
    default_convert_audio = False
    can_convert_audio = False
    already_has_exact_target = False
    audio_conversion_cfg = AudioConversionConfig(enabled=False)
    audio_skip_note: str | None = None
    planned_out_channels: int = AppConstants.MAX_DOLBY_CHANNELS
    planned_resolved_rate: str | None = None

    if primary_audio is not None:
        src_canonical = canonicalize_audio_codec(primary_audio.codec)
        src_channels = int(primary_audio.audio_channels or 0) or AppConstants.MAX_DOLBY_CHANNELS
        primary_lang = normalize_lang_code(primary_audio.language)

        planned_out_channels = compute_target_channels(
            src_channels,
            target_codec=rule.audio.target_codec,
            requested_channels=rule.audio.target_channels,
        )
        allowed_rates = AppConstants.AUDIO_CODEC_ALLOWED_BITRATES.get(
            rule.audio.target_codec, frozenset()
        )
        raw_rate = rule.audio.bitrate
        if rule.audio.target_codec in AppConstants.LOSSLESS_TARGET_CODECS:
            planned_resolved_rate = None
        else:
            if not raw_rate or raw_rate not in allowed_rates:
                raw_rate = resolve_default_bitrate_for_codec(
                    rule.audio.target_codec, planned_out_channels
                )
            planned_resolved_rate = resolve_effective_bitrate(
                rule.audio.target_codec, raw_rate, planned_out_channels
            )

        lossy_to_flac = (
            rule.audio.target_codec in AppConstants.LOSSLESS_TARGET_CODECS
            and not is_lossless_source_codec(primary_audio.codec)
        )
        ac3_to_eac3_upsample = (
            src_canonical == AppConstants.TARGET_CODEC_AC3
            and rule.audio.target_codec == AppConstants.TARGET_CODEC_EAC3
            and planned_out_channels >= src_channels
        )
        can_convert_audio = not lossy_to_flac and not ac3_to_eac3_upsample

        same_lang_non_comm = [
            t
            for t in audio_tracks
            if normalize_lang_code(t.language) == primary_lang
            and not is_commentary_or_description_track(t.track_name)
        ]
        already_has_exact_target = _track_matches_exact_audio_target(
            primary_audio, rule.audio, src_channels
        ) or any(
            _track_matches_exact_audio_target(t, rule.audio, src_channels)
            for t in same_lang_non_comm
        )

        if rule.audio.enabled:
            already_satisfied = already_has_exact_target or _track_satisfies_audio_target(
                primary_audio, rule.audio, src_channels
            ) or any(
                _track_satisfies_audio_target(t, rule.audio, src_channels)
                for t in same_lang_non_comm
            )

            if already_satisfied:
                audio_skip_note = f"Audio already {primary_audio.codec} {format_channel_label(primary_audio.audio_channels)}"
            elif (
                rule.audio.match_condition == AppConstants.BATCH_MATCH_AAC51_ONLY
                and not (src_canonical == AppConstants.TARGET_CODEC_AAC and src_channels >= AppConstants.CHANNELS_51)
            ):
                audio_skip_note = f"Audio is {primary_audio.codec} {format_channel_label(primary_audio.audio_channels)} (not AAC 5.1)"
            elif (
                rule.audio.match_condition == AppConstants.BATCH_MATCH_INCOMPATIBLE_OR_NON_DOLBY
                and src_canonical in (AppConstants.TARGET_CODEC_EAC3, AppConstants.TARGET_CODEC_AC3)
                and (rule.audio.target_channels is None or rule.audio.target_channels >= src_channels)
            ):
                audio_skip_note = f"Audio already Dolby ({primary_audio.codec})"
            elif lossy_to_flac:
                warnings.append(
                    f"Skipped FLAC conversion on {filename}: source track ({primary_audio.codec}) is lossy."
                )
                audio_skip_note = "Source audio is lossy (cannot convert to lossless FLAC)"
            elif ac3_to_eac3_upsample:
                audio_skip_note = (
                    f"Audio already {primary_audio.codec} (skipping lossy AC-3 → E-AC-3 upsample)"
                )
            else:
                default_convert_audio = True

    if override is not None and override.audio_convert_enabled is not None:
        should_convert_audio = bool(
            rule.audio.enabled
            and override.audio_convert_enabled
            and can_convert_audio
            and not already_has_exact_target
        )
        if override.audio_convert_enabled and not can_convert_audio and primary_audio is not None:
            if (
                canonicalize_audio_codec(primary_audio.codec) == AppConstants.TARGET_CODEC_AC3
                and rule.audio.target_codec == AppConstants.TARGET_CODEC_EAC3
            ):
                warnings.append(
                    f"Cannot upsample lossy AC-3 to E-AC-3 on {filename} without downmixing."
                )
            else:
                warnings.append(
                    f"Cannot convert lossy source ({primary_audio.codec}) to lossless FLAC on {filename}."
                )
    else:
        should_convert_audio = default_convert_audio

    # ------------------------------------------------------------------
    # 2. Evaluate Track Filtering, Language Retagging & Smart Track Names
    # ------------------------------------------------------------------
    allowed_audio_langs: set[str] = {effective_pref_lang}
    for code in rule.track_filter.keep_audio_languages:
        if code and code.strip():
            allowed_audio_langs.add(normalize_lang_code(code))

    allowed_sub_langs: set[str] = {effective_pref_lang}
    for code in rule.track_filter.keep_subtitle_languages:
        if code and code.strip():
            allowed_sub_langs.add(normalize_lang_code(code))

    track_overrides = override.track_enabled if override is not None else {}

    # Compute default and effective enabled states for video tracks (default kept; user can drop via chip!)
    default_video_enabled: dict[int, bool] = {}
    surviving_video_ids: set[int] = set()
    for t in video_tracks:
        default_video_enabled[t.id] = True
        eff_keep = bool(track_overrides[t.id]) if t.id in track_overrides else True
        if eff_keep:
            surviving_video_ids.add(t.id)

    # Compute default and effective enabled states for audio tracks.
    # When `should_convert_audio` and `rule.audio.drop_source_track` are both True,
    # the primary source track defaults to dropped (`False`), and an explicit per-file
    # chip override on `primary_audio.id` toggles keeping vs dropping that source track!
    default_audio_enabled: dict[int, bool] = {}
    surviving_audio_ids: set[int] = set()
    for t in audio_tracks:
        eff_lang = normalize_lang_code(t.language)
        if (
            effective_retag_und
            and (primary_audio is not None and t.id == primary_audio.id or len(audio_tracks) == 1)
        ):
            eff_lang = retag_target_lang
        if rule.track_filter.keep_audio_mode == AppConstants.BATCH_KEEP_TRACKS_ALL:
            default_keep = True
        else:
            default_keep = eff_lang in allowed_audio_langs
        if (
            should_convert_audio
            and rule.audio.drop_source_track
            and primary_audio is not None
            and t.id == primary_audio.id
        ):
            default_keep = False
        default_audio_enabled[t.id] = default_keep
        eff_keep = bool(track_overrides[t.id]) if t.id in track_overrides else default_keep
        if eff_keep:
            surviving_audio_ids.add(t.id)

    # Keep AudioConversionConfig.drop_source_track 100% synchronized with surviving_audio_ids
    # so `mkv_builder.py` never drops a source track the user explicitly clicked to keep!
    if should_convert_audio and primary_audio is not None:
        effective_drop_source_track = primary_audio.id not in surviving_audio_ids
        src_channels = int(primary_audio.audio_channels or 0) or AppConstants.MAX_DOLBY_CHANNELS
        out_lang = primary_audio.language or AppConstants.DEFAULT_LANGUAGE
        if effective_retag_und:
            out_lang = retag_target_lang

        track_title = build_converted_audio_title(
            target_codec=rule.audio.target_codec,
            target_channels=planned_out_channels,
            bitrate=planned_resolved_rate,
            language=out_lang,
            track_format=smart_fill_track_format,
        )
        audio_conversion_cfg = AudioConversionConfig(
            enabled=True,
            source_track_id=primary_audio.id,
            source_audio_index=int(primary_audio.audio_index or 0),
            source_channels=src_channels,
            source_codec=primary_audio.codec,
            source_minimum_timestamp_ns=int(primary_audio.minimum_timestamp_ns or 0),
            target_codec=rule.audio.target_codec,
            target_channels=(
                planned_out_channels if rule.audio.target_channels is not None else None
            ),
            bitrate=planned_resolved_rate,
            track_title=track_title,
            language=out_lang,
            set_as_default=bool(rule.audio.set_as_default),
            drop_source_track=effective_drop_source_track,
        )

    # Zero-Audio Safety Guardrail:
    # - Automatic global language filters never silently drop all audio tracks.
    # - Explicit user chip clicks CAN remove all audio on files that still retain video/subtitles,
    #   or remove all video on files that still retain audio/subtitles (--no-video / --no-audio).
    has_surviving_video = len(surviving_video_ids) > 0
    user_explicitly_dropped_audio = any(
        t.id in track_overrides and track_overrides[t.id] is False
        for t in audio_tracks
    )
    if audio_tracks and not surviving_audio_ids and not should_convert_audio:
        if user_explicitly_dropped_audio and has_surviving_video:
            warnings.append(
                "No audio tracks selected — output file will have no audio (video/subtitles only)."
            )
        elif not user_explicitly_dropped_audio:
            fallback_id = primary_audio.id if primary_audio is not None else audio_tracks[0].id
            surviving_audio_ids.add(fallback_id)
            warnings.append(
                f"Kept primary audio track #{fallback_id} because no audio tracks matched the language filter."
            )

    default_sub_enabled: dict[int, bool] = {}
    effective_sub_enabled: dict[int, bool] = {}
    for t in sub_tracks:
        orig_lang = t.language or AppConstants.DEFAULT_LANGUAGE
        if rule.track_filter.keep_subtitle_mode == AppConstants.BATCH_KEEP_SUBS_NONE:
            default_keep = False
        elif rule.track_filter.keep_subtitle_mode == AppConstants.BATCH_KEEP_TRACKS_PREFERRED:
            sub_norm = normalize_lang_code(orig_lang)
            default_keep = sub_norm in allowed_sub_langs or sub_norm == AppConstants.DEFAULT_LANGUAGE
        else:
            default_keep = True
        default_sub_enabled[t.id] = default_keep
        effective_sub_enabled[t.id] = (
            bool(track_overrides[t.id]) if t.id in track_overrides else default_keep
        )

    surviving_sub_ids: set[int] = {tid for tid, keep in effective_sub_enabled.items() if keep}

    # Global Non-Empty Container Safety Guardrail: mkvmerge cannot build an MKV with 0 kept tracks
    total_kept_tracks = (
        len(surviving_video_ids)
        + len(surviving_audio_ids)
        + len(surviving_sub_ids)
        + (1 if should_convert_audio else 0)
    )
    if total_kept_tracks == 0:
        if video_tracks:
            fallback_tid = video_tracks[0].id
            surviving_video_ids.add(fallback_tid)
        elif audio_tracks:
            fallback_tid = primary_audio.id if primary_audio is not None else audio_tracks[0].id
            surviving_audio_ids.add(fallback_tid)
        else:
            fallback_tid = sub_tracks[0].id
            effective_sub_enabled[fallback_tid] = True
            surviving_sub_ids.add(fallback_tid)
        total_kept_tracks = 1
        warnings.append(
            f"Kept Track #{fallback_tid} because an MKV output must retain at least one track."
        )
    elif video_tracks and not surviving_video_ids:
        warnings.append(
            "No video tracks selected — output file will be audio/subtitles only (--no-video)."
        )

    default_smart_fill = bool(rule.container.smart_fill_track_names)
    effective_smart_fill = (
        bool(override.smart_fill_track_names)
        if override is not None and override.smart_fill_track_names is not None
        else default_smart_fill
    )

    # Ensure surviving source audio retains a default track when the user drops the
    # original default audio track and no converted track claims default ownership.
    promoted_default_audio_id: int | None = None
    if surviving_audio_ids and not (should_convert_audio and rule.audio.set_as_default):
        surviving_audio_tracks = [t for t in audio_tracks if t.id in surviving_audio_ids]
        if surviving_audio_tracks and not any(t.default_track for t in surviving_audio_tracks):
            if primary_audio is not None and primary_audio.id in surviving_audio_ids:
                promoted_default_audio_id = primary_audio.id
            else:
                promoted_default_audio_id = surviving_audio_tracks[0].id

    track_configs: list[TrackEditConfig] = []
    any_track_modified = False
    effective_audio_lang_by_id: dict[int, str] = {}

    for t in all_tracks:
        orig_lang = t.language or AppConstants.DEFAULT_LANGUAGE
        new_lang = orig_lang
        enabled = True
        effective_default = bool(t.default_track)

        if t.type == "video":
            enabled = t.id in surviving_video_ids
        elif t.type == "audio":
            if (
                effective_retag_und
                and (primary_audio is not None and t.id == primary_audio.id or len(audio_tracks) == 1)
            ):
                new_lang = retag_target_lang
            effective_audio_lang_by_id[t.id] = new_lang
            enabled = t.id in surviving_audio_ids
            if enabled and promoted_default_audio_id is not None and t.id == promoted_default_audio_id:
                effective_default = True
        elif t.type == "subtitles":
            enabled = effective_sub_enabled.get(t.id, True)

        orig_name = t.track_name or ""
        if effective_smart_fill and enabled:
            new_name: str | None = format_smart_track_badge(
                t,
                effective_language=new_lang,
                track_format=smart_fill_track_format,
            )
        else:
            new_name = orig_name if orig_name else None

        effective_new_name_str = new_name or ""
        if (
            not enabled
            or effective_new_name_str != orig_name
            or new_lang != orig_lang
            or effective_default != bool(t.default_track)
        ):
            any_track_modified = True

        if (
            should_convert_audio
            and rule.audio.drop_source_track
            and primary_audio is not None
            and t.id == primary_audio.id
        ):
            any_track_modified = True

        track_configs.append(
            TrackEditConfig(
                id=t.id,
                type=t.type,
                enabled=enabled,
                track_name=new_name,
                language=new_lang,
                default_track=effective_default,
                forced_track=bool(t.forced_track),
            )
        )

    # ------------------------------------------------------------------
    # 3. Evaluate Container Metadata Rules (with per-file overrides)
    # ------------------------------------------------------------------
    existing_title = str(row.get("container_title") or probe.container_title or "")
    has_title = bool(existing_title.strip())
    has_global_tags = bool(row.get("has_global_tags")) or probe.global_tags_count > 0
    has_track_tags = probe.has_track_tags or probe.track_tags_count > 0
    has_attachments = probe.attachments_count > 0
    has_chapters = probe.chapters_count > 0
    is_mp4 = filename.lower().endswith(AppConstants.EXT_MP4)

    default_clear_title = (
        rule.container.title_action == AppConstants.BATCH_TITLE_ACTION_CLEAR and has_title
    )
    clear_title_needed = (
        bool(override.clear_title)
        if override is not None and override.clear_title is not None
        else (rule.container.title_action == AppConstants.BATCH_TITLE_ACTION_CLEAR)
    ) and has_title

    default_strip_global = bool(rule.container.strip_global_tags and has_global_tags)
    strip_global_needed = (
        bool(override.strip_global_tags)
        if override is not None and override.strip_global_tags is not None
        else bool(rule.container.strip_global_tags)
    ) and has_global_tags

    default_strip_track = bool(rule.container.strip_track_tags and has_track_tags)
    strip_track_needed = (
        bool(override.strip_track_tags)
        if override is not None and override.strip_track_tags is not None
        else bool(rule.container.strip_track_tags)
    ) and has_track_tags

    default_strip_att = bool(rule.container.strip_attachments and has_attachments)
    strip_att_needed = (
        bool(override.strip_attachments)
        if override is not None and override.strip_attachments is not None
        else bool(rule.container.strip_attachments)
    ) and has_attachments

    default_strip_chap = bool(rule.container.strip_chapters and has_chapters)
    strip_chap_needed = (
        bool(override.strip_chapters)
        if override is not None and override.strip_chapters is not None
        else bool(rule.container.strip_chapters)
    ) and has_chapters

    # ------------------------------------------------------------------
    # 3b. Evaluate Output Destination (Save As) & Batch / Inline Rename
    # ------------------------------------------------------------------
    override_filename = override.output_filename if override is not None else None
    rendered_name, is_inline_rename_override, used_ep_fallback = render_batch_output_filename(
        filename,
        rule=rule,
        seq_index=seq_index,
        pad_digits=pad_digits,
        override_filename=override_filename,
    )
    if used_ep_fallback:
        warnings.append(
            f"No episode number found in {filename}; used sequential index #{seq_index}."
        )

    is_save_as_mode = rule.output_mode == AppConstants.OUTPUT_MODE_SAVE_AS
    target_folder = (
        _normalize_destination_folder(rule.destination_folder, folder)
        if is_save_as_mode
        else _normalize_destination_folder(folder, "/")
    )
    default_mkv_name = f"{Path(filename).stem}{AppConstants.EXT_MKV}"
    target_filename = rendered_name or default_mkv_name
    target_rel_path = _join_rel_storage_path(target_folder, target_filename)

    is_name_changed = bool(rendered_name is not None and rendered_name != filename)
    is_folder_changed = is_save_as_mode and target_folder != _normalize_destination_folder(folder, "/")
    is_renamed = is_name_changed or is_folder_changed

    # Effective JobConfig output_mode / output_path / delete_original:
    # - Save As -> writes to target_rel_path with user's delete_original flag
    # - Replace + Rename -> save_as to same folder with new filename & delete_original=True (atomic!)
    # - Keep Both + Rename -> save_as to same folder with new filename & delete_original=False
    # - Otherwise -> standard keep_both / replace without explicit output_path
    use_explicit_save_as = is_save_as_mode or is_name_changed
    effective_output_mode = (
        AppConstants.OUTPUT_MODE_SAVE_AS if use_explicit_save_as else rule.output_mode
    )
    effective_output_path: str | None = target_rel_path if use_explicit_save_as else None
    if is_save_as_mode:
        effective_delete_original = bool(rule.delete_original)
    elif is_name_changed and rule.output_mode == AppConstants.OUTPUT_MODE_REPLACE:
        effective_delete_original = True
    else:
        effective_delete_original = False

    # Guardrail #2: Renaming or Save As on an otherwise compliant file makes it actionable!
    any_container_modified = (
        clear_title_needed
        or strip_global_needed
        or strip_track_needed
        or strip_att_needed
        or strip_chap_needed
        or is_mp4
        or is_renamed
        or is_save_as_mode
    )

    # ------------------------------------------------------------------
    # 4. Build Interactive Per-File Chips (ALWAYS populated, even when compliant!)
    # ------------------------------------------------------------------
    chips: list[BatchFileChip] = []

    # 4a-0. Existing Video track chips (starting at Track #0; user can uncheck to strip video via --no-video!)
    for t in video_tracks:
        is_kept = t.id in surviving_video_ids
        def_kept = default_video_enabled.get(t.id, True)
        v_codec_lbl = _short_video_codec_label(t.codec)
        dims_suffix = f" · {t.pixel_dimensions}" if t.pixel_dimensions else ""
        lang_norm = normalize_lang_code(t.language)
        lang_suffix = f" · {t.language}" if (t.language and lang_norm != AppConstants.DEFAULT_LANGUAGE) else ""
        is_last_track = is_kept and total_kept_tracks <= 1
        chips.append(
            BatchFileChip(
                chip_id=f"video_track:{t.id}",
                category=AppConstants.BATCH_CHIP_CAT_VIDEO,  # type: ignore[arg-type]
                label=f"#{t.id} {v_codec_lbl}{dims_suffix}{lang_suffix}",
                detail=t.track_name or f"Video Track #{t.id} ({t.codec}{dims_suffix})",
                selected=is_kept,
                default_selected=def_kept,
                overridden=(t.id in track_overrides and is_kept != def_kept),
                override_key=AppConstants.BATCH_OVERRIDE_TRACK_ENABLED,
                track_id=t.id,
                disabled=is_last_track,
                disabled_reason=(
                    "Cannot drop the last remaining track in the file"
                    if is_last_track
                    else None
                ),
            )
        )

    # 4a. Build optional Audio conversion chip (placed at Index 0 when active to match mkvmerge --track-order 0:0!)
    convert_chip: BatchFileChip | None = None
    if (
        rule.audio.enabled
        and primary_audio is not None
        and can_convert_audio
        and not already_has_exact_target
    ):
        target_lbl = _short_audio_codec_label(rule.audio.target_codec)
        out_ch_lbl = format_channel_label(planned_out_channels)
        br_suffix = f" · {planned_resolved_rate}" if planned_resolved_rate else ""
        conv_lang_changed = bool(effective_retag_und and primary_orig_lang != retag_target_lang)
        conv_lang_suffix = (
            "" if conv_lang_changed else f" · {primary_orig_lang}"
        )
        convert_chip = BatchFileChip(
            chip_id="audio_convert",
            category=AppConstants.BATCH_CHIP_CAT_AUDIO,  # type: ignore[arg-type]
            label=(
                f"#0 New: {target_lbl} {out_ch_lbl}{br_suffix}{conv_lang_suffix}"
                if should_convert_audio
                else f"Add {target_lbl} {out_ch_lbl}{br_suffix}"
            ),
            detail=(
                f"Inserted at Audio Index 0 from Track #{primary_audio.id} ({primary_audio.codec} {format_channel_label(primary_audio.audio_channels)}, lang={retag_target_lang if conv_lang_changed else primary_orig_lang})"
                if should_convert_audio
                else f"Click to convert Track #{primary_audio.id} ({primary_audio.codec} {format_channel_label(primary_audio.audio_channels)}) to {target_lbl} {out_ch_lbl}"
            ),
            diff_from=primary_orig_lang if (should_convert_audio and conv_lang_changed) else None,
            diff_to=retag_target_lang if (should_convert_audio and conv_lang_changed) else None,
            selected=should_convert_audio,
            default_selected=default_convert_audio,
            overridden=(
                override is not None
                and override.audio_convert_enabled is not None
                and should_convert_audio != default_convert_audio
            ),
            override_key=AppConstants.BATCH_OVERRIDE_AUDIO_CONVERT,
        )

    if convert_chip is not None and should_convert_audio:
        chips.append(convert_chip)

    # 4b. Existing Audio track chips
    for t in audio_tracks:
        is_kept = t.id in surviving_audio_ids
        def_kept = default_audio_enabled.get(t.id, True)
        orig_lang_lbl = t.language or AppConstants.DEFAULT_LANGUAGE
        eff_lang_lbl = effective_audio_lang_by_id.get(t.id, orig_lang_lbl)
        lang_changed = eff_lang_lbl != orig_lang_lbl
        ch_lbl = format_channel_label(t.audio_channels)
        codec_lbl = _short_audio_codec_label(t.codec)
        is_last_track_in_file = is_kept and total_kept_tracks <= 1
        chips.append(
            BatchFileChip(
                chip_id=f"audio_track:{t.id}",
                category=AppConstants.BATCH_CHIP_CAT_AUDIO,  # type: ignore[arg-type]
                label=(
                    f"#{t.id} {codec_lbl} {ch_lbl}"
                    if lang_changed
                    else f"#{t.id} {codec_lbl} {ch_lbl} · {orig_lang_lbl}"
                ),
                detail=t.track_name
                or (
                    f"Audio Track #{t.id} ({t.codec} {ch_lbl}, {orig_lang_lbl} → {eff_lang_lbl})"
                    if lang_changed
                    else f"Audio Track #{t.id} ({t.codec} {ch_lbl}, {orig_lang_lbl})"
                ),
                diff_from=orig_lang_lbl if lang_changed else None,
                diff_to=eff_lang_lbl if lang_changed else None,
                selected=is_kept,
                default_selected=def_kept,
                overridden=(t.id in track_overrides and is_kept != def_kept),
                override_key=AppConstants.BATCH_OVERRIDE_TRACK_ENABLED,
                track_id=t.id,
                disabled=is_last_track_in_file,
                disabled_reason=(
                    "Cannot drop the last remaining track in the file"
                    if is_last_track_in_file
                    else None
                ),
            )
        )

    # When conversion is currently OFF (opt-in available), show the '+ Add ...' chip after existing tracks
    if convert_chip is not None and not should_convert_audio:
        chips.append(convert_chip)

    # 4c. Retag primary audio chip (shown only when global retag checkbox is ON and language differs)
    if (
        rule.track_filter.retag_und_audio_to_preferred
        and can_retag_primary
        and primary_audio is not None
    ):
        chips.append(
            BatchFileChip(
                chip_id="retag_und",
                category=AppConstants.BATCH_CHIP_CAT_AUDIO,  # type: ignore[arg-type]
                label="Retag",
                detail=f"Change primary audio Track #{primary_audio.id} language tag from {primary_orig_lang} to {retag_target_lang}",
                diff_from=primary_orig_lang,
                diff_to=retag_target_lang,
                selected=effective_retag_und,
                default_selected=default_retag_und,
                overridden=(
                    override is not None
                    and override.retag_und_enabled is not None
                    and effective_retag_und != default_retag_und
                ),
                override_key=AppConstants.BATCH_OVERRIDE_RETAG_UND,
            )
        )

    # 4d. Subtitle track chips
    for t in sub_tracks:
        is_kept = effective_sub_enabled.get(t.id, True)
        def_kept = default_sub_enabled.get(t.id, True)
        lang_lbl = t.language or AppConstants.DEFAULT_LANGUAGE
        sub_codec = _short_subtitle_codec_label(t.codec)
        forced_suffix = " · Forced" if t.forced_track else ""
        is_last_sub_track = is_kept and total_kept_tracks <= 1
        chips.append(
            BatchFileChip(
                chip_id=f"sub_track:{t.id}",
                category=AppConstants.BATCH_CHIP_CAT_SUBTITLE,  # type: ignore[arg-type]
                label=f"#{t.id} {sub_codec} · {lang_lbl}{forced_suffix}",
                detail=t.track_name or f"Subtitle Track #{t.id} ({t.codec}, {lang_lbl})",
                selected=is_kept,
                default_selected=def_kept,
                overridden=(t.id in track_overrides and is_kept != def_kept),
                override_key=AppConstants.BATCH_OVERRIDE_TRACK_ENABLED,
                track_id=t.id,
                disabled=is_last_sub_track,
                disabled_reason=(
                    "Cannot drop the last remaining track in the file"
                    if is_last_sub_track
                    else None
                ),
            )
        )

    # 4e. Tag / Metadata chips (show artifacts present on this file + Smart Fill)
    if has_title:
        chips.append(
            BatchFileChip(
                chip_id="tag:title",
                category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
                label="Clear Title",
                detail=f"Container title: {existing_title}",
                selected=clear_title_needed,
                default_selected=default_clear_title,
                overridden=(
                    override is not None
                    and override.clear_title is not None
                    and clear_title_needed != default_clear_title
                ),
                override_key=AppConstants.BATCH_OVERRIDE_CLEAR_TITLE,
            )
        )
    if has_global_tags:
        chips.append(
            BatchFileChip(
                chip_id="tag:global_tags",
                category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
                label="Strip Global Tags",
                detail="Remove container-level global metadata tags",
                selected=strip_global_needed,
                default_selected=default_strip_global,
                overridden=(
                    override is not None
                    and override.strip_global_tags is not None
                    and strip_global_needed != default_strip_global
                ),
                override_key=AppConstants.BATCH_OVERRIDE_STRIP_GLOBAL_TAGS,
            )
        )
    if has_track_tags:
        chips.append(
            BatchFileChip(
                chip_id="tag:track_tags",
                category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
                label="Strip Track Tags",
                detail="Remove per-track metadata tags (e.g. BPS, DURATION stats)",
                selected=strip_track_needed,
                default_selected=default_strip_track,
                overridden=(
                    override is not None
                    and override.strip_track_tags is not None
                    and strip_track_needed != default_strip_track
                ),
                override_key=AppConstants.BATCH_OVERRIDE_STRIP_TRACK_TAGS,
            )
        )
    if has_attachments:
        chips.append(
            BatchFileChip(
                chip_id="tag:attachments",
                category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
                label=f"Strip Attachments ({probe.attachments_count})",
                detail=f"Remove {probe.attachments_count} embedded container attachment(s)",
                selected=strip_att_needed,
                default_selected=default_strip_att,
                overridden=(
                    override is not None
                    and override.strip_attachments is not None
                    and strip_att_needed != default_strip_att
                ),
                override_key=AppConstants.BATCH_OVERRIDE_STRIP_ATTACHMENTS,
            )
        )
    if has_chapters:
        chips.append(
            BatchFileChip(
                chip_id="tag:chapters",
                category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
                label=f"Strip Chapters ({probe.chapters_count})",
                detail=f"Remove {probe.chapters_count} embedded chapter marker(s)",
                selected=strip_chap_needed,
                default_selected=default_strip_chap,
                overridden=(
                    override is not None
                    and override.strip_chapters is not None
                    and strip_chap_needed != default_strip_chap
                ),
                override_key=AppConstants.BATCH_OVERRIDE_STRIP_CHAPTERS,
            )
        )
    chips.append(
        BatchFileChip(
            chip_id="tag:smart_fill",
            category=AppConstants.BATCH_CHIP_CAT_TAG,  # type: ignore[arg-type]
            label="Smart Fill Names",
            detail="Apply clean standardized track titles to all kept tracks",
            selected=effective_smart_fill,
            default_selected=default_smart_fill,
            overridden=(
                override is not None
                and override.smart_fill_track_names is not None
                and effective_smart_fill != default_smart_fill
            ),
            override_key=AppConstants.BATCH_OVERRIDE_SMART_FILL_NAMES,
        )
    )

    has_overrides = any(c.overridden for c in chips) or is_inline_rename_override

    has_any_effective_change = (
        should_convert_audio or any_track_modified or any_container_modified
    )

    if not has_any_effective_change:
        compliant_reason = (
            f"Skip ({audio_skip_note} & metadata already clean)"
            if audio_skip_note
            else "Skip (Already matches all rules)"
        )
        return BatchFilePlan(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            folder=folder,
            size_bytes=size_bytes,
            action=AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT,  # type: ignore[arg-type]
            stage_plan=AppConstants.BATCH_STAGE_NONE,  # type: ignore[arg-type]
            reason=compliant_reason,
            source_audio_summary=source_audio_summary,
            output_filename=target_filename,
            output_rel_path=target_rel_path if use_explicit_save_as else None,
            is_renamed=is_renamed,
            chips=chips,
            has_overrides=has_overrides,
            warnings=warnings,
        )

    job_config = JobConfig(
        container=ContainerMetadataConfig(
            title="" if clear_title_needed else None,
            strip_global_tags=strip_global_needed,
            strip_track_tags=strip_track_needed,
            strip_attachments=strip_att_needed,
            keep_attachment_ids=None,
            strip_chapters=strip_chap_needed,
        ),
        audio_conversion=audio_conversion_cfg,
        tracks=track_configs,
        external_tracks=[],
        output_mode=effective_output_mode,  # type: ignore[arg-type]
        output_path=effective_output_path,
        delete_original=effective_delete_original,
    )

    # Build human-readable change chips
    _, change_chips = summarize_job_changes(
        job_config.model_dump_json(),
        filepath=filepath,
        output_filepath=effective_output_path,
    )
    if effective_retag_und and primary_audio is not None:
        change_chips.append(
            JobChangeItem(
                kind="changed",
                label=f"Retag Audio {primary_orig_lang} → {retag_target_lang}",
            )
        )
    if effective_smart_fill and any(
        tc.enabled and tc.track_name for tc in track_configs
    ):
        change_chips.append(
            JobChangeItem(
                kind="changed",
                label="Smart Fill Track Names",
            )
        )
    dropped_subs = sum(
        1 for tc in track_configs if tc.type == "subtitles" and not tc.enabled
    )
    if dropped_subs > 0:
        change_chips.append(
            JobChangeItem(
                kind="removed",
                label=f"Drop {dropped_subs} Subtitle{'s' if dropped_subs != 1 else ''}",
            )
        )
    if is_renamed:
        change_chips.append(
            JobChangeItem(
                kind="changed",
                label=f"Rename → {target_filename}",
            )
        )

    stage_plan = (
        AppConstants.BATCH_STAGE_TRANSCODE_AND_REMUX
        if should_convert_audio
        else AppConstants.BATCH_STAGE_REMUX_ONLY
    )
    if should_convert_audio:
        reason = f"Convert Audio ({rule.audio.target_codec.upper()}) + Remux"
    elif is_renamed and not any_track_modified and not (clear_title_needed or strip_global_needed or strip_track_needed or strip_att_needed or strip_chap_needed):
        reason = f"Fast Remux Only (Save as {target_filename})"
    elif audio_skip_note:
        reason = f"Fast Remux Only ({audio_skip_note})"
    else:
        reason = "Fast Remux Only (Metadata / Tracks)"

    return BatchFilePlan(
        file_id=file_id,
        filepath=filepath,
        filename=filename,
        folder=folder,
        size_bytes=size_bytes,
        action=AppConstants.BATCH_PLAN_ACTION_ENQUEUE,  # type: ignore[arg-type]
        stage_plan=stage_plan,  # type: ignore[arg-type]
        reason=reason,
        source_audio_summary=source_audio_summary,
        output_filename=target_filename,
        output_rel_path=effective_output_path,
        is_renamed=is_renamed,
        changes=change_chips,
        chips=chips,
        has_overrides=has_overrides,
        warnings=warnings,
        job_config=job_config,
    )


def detect_majority_audio_language(rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Detect the majority tagged primary audio language across selected rows.

    - Excludes commentary/description/AD tracks when selecting each file's primary tagged vote.
    - Returns `(majority_lang, sorted_detected_non_und_langs)`.
    - If no file in the batch has a tagged (non-`und`) audio language, returns `("und", [])`
      so the UI never assumes English (`eng`).
    """
    lang_votes: dict[str, int] = {}
    all_detected_langs: set[str] = set()

    for row in rows:
        raw_json_str = str(row.get("raw_json") or "{}")
        audio_tracks: list[ParsedTrackInfo] = []
        try:
            raw_payload = json.loads(raw_json_str)
            if isinstance(raw_payload, dict) and raw_payload.get("tracks"):
                probe = parse_mkvmerge_json(raw_payload)
                audio_tracks = [t for t in probe.tracks if t.type == "audio"]
        except (TypeError, json.JSONDecodeError):
            audio_tracks = []

        file_vote_lang: str | None = None
        if audio_tracks:
            non_comm_tagged: list[tuple[int, ParsedTrackInfo]] = []
            for idx, track in enumerate(audio_tracks):
                norm = normalize_lang_code(track.language)
                if norm != AppConstants.DEFAULT_LANGUAGE:
                    all_detected_langs.add(norm)
                    if not is_commentary_or_description_track(track.track_name):
                        non_comm_tagged.append((idx, track))
            if non_comm_tagged:
                # Highest channel count -> default flag -> lowest index
                _, best = min(
                    non_comm_tagged,
                    key=lambda item: (
                        -int(item[1].audio_channels or 0),
                        0 if item[1].default_track else 1,
                        item[0],
                    ),
                )
                file_vote_lang = normalize_lang_code(best.language)
        else:
            summaries = decode_audio_summary_json(row.get("audio_summary_json"))
            for entry in summaries:
                norm = normalize_lang_code(str(entry.get("language") or ""))
                if norm != AppConstants.DEFAULT_LANGUAGE:
                    all_detected_langs.add(norm)
                    if file_vote_lang is None:
                        file_vote_lang = norm

        if file_vote_lang and file_vote_lang != AppConstants.DEFAULT_LANGUAGE:
            lang_votes[file_vote_lang] = lang_votes.get(file_vote_lang, 0) + 1

    if not lang_votes:
        return AppConstants.DEFAULT_LANGUAGE, sorted(all_detected_langs)

    majority_lang = min(lang_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return majority_lang, sorted(all_detected_langs)


def summarize_batch_inventory(rows: list[dict[str, Any]]) -> BatchInventorySummary:
    """Compute the Selection X-Ray heterogeneity counts and majority audio language."""
    total_files = len(rows)
    total_bytes = 0
    aac_51_count = 0
    dolby_count = 0
    other_audio_count = 0
    has_metadata_count = 0
    untagged_audio_count = 0

    for row in rows:
        total_bytes += int(row.get("size_bytes") or 0)
        has_aac = bool(row.get("has_aac_51"))
        has_dolby = bool(row.get("has_ac3_eac3"))
        if has_aac:
            aac_51_count += 1
        if has_dolby:
            dolby_count += 1
        if not has_aac and not has_dolby:
            other_audio_count += 1

        if bool(row.get("container_title")) or bool(row.get("has_global_tags")):
            has_metadata_count += 1

        summaries = decode_audio_summary_json(row.get("audio_summary_json"))
        if any(
            normalize_lang_code(str(entry.get("language") or ""))
            == AppConstants.DEFAULT_LANGUAGE
            for entry in summaries
        ):
            untagged_audio_count += 1

    majority_lang, detected_langs = detect_majority_audio_language(rows)

    return BatchInventorySummary(
        total_files=total_files,
        total_bytes=total_bytes,
        aac_51_count=aac_51_count,
        dolby_count=dolby_count,
        other_audio_count=other_audio_count,
        has_metadata_count=has_metadata_count,
        untagged_audio_count=untagged_audio_count,
        detected_majority_audio_language=majority_lang,
        detected_audio_languages=detected_langs,
    )


def estimate_batch_disk_space(
    plans: list[BatchFilePlan],
    *,
    output_mode: str,
    storage_root: Path,
    delete_original: bool = False,
) -> tuple[int, int, bool, str | None]:
    """Check free disk space against the planned batch (Guardrail #2).

    - For `keep_both` or `save_as` (with `delete_original=False`): requires `sum(file_size * 1.1) + 100 MiB`.
    - For `replace` or `save_as` (with `delete_original=True`): requires `max(file_size * 1.1) + 100 MiB`.
    """
    enqueued = [
        p for p in plans if p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
    ]
    try:
        free_bytes = int(shutil.disk_usage(storage_root).free)
    except OSError:
        free_bytes = 0

    if not enqueued:
        return free_bytes, 0, True, None

    needs_full_sum = (
        output_mode == AppConstants.OUTPUT_MODE_KEEP_BOTH
        or (output_mode == AppConstants.OUTPUT_MODE_SAVE_AS and not delete_original)
    )
    if needs_full_sum:
        raw_sum = sum(max(0, int(p.size_bytes)) for p in enqueued)
        required_bytes = (
            int(raw_sum * AppConstants.DISK_SPACE_SIZE_MULTIPLIER)
            + AppConstants.DISK_SPACE_BUFFER_BYTES
        )
    else:
        largest_single = max(max(0, int(p.size_bytes)) for p in enqueued)
        required_bytes = (
            int(largest_single * AppConstants.DISK_SPACE_SIZE_MULTIPLIER)
            + AppConstants.DISK_SPACE_BUFFER_BYTES
        )

    if free_bytes <= 0:
        return 0, required_bytes, True, None

    disk_space_ok = free_bytes >= required_bytes
    warning: str | None = None
    if not disk_space_ok:
        req_gb = required_bytes / (1024**3)
        free_gb = free_bytes / (1024**3)
        mode_hint = (
            "Switch Output Mode to 'Replace Original' to need only single-file scratch space."
            if needs_full_sum
            else "Free up storage space before queuing this batch."
        )
        warning = (
            f"Low disk space: batch requires ~{req_gb:.2f} GB ({output_mode}), "
            f"but only {free_gb:.2f} GB is free on storage. {mode_hint}"
        )
    return free_bytes, required_bytes, disk_space_ok, warning


def apply_batch_collision_checks(
    plans: list[BatchFilePlan],
    *,
    storage_root: Path,
) -> list[BatchFilePlan]:
    """Validate intra-batch duplicate output paths and on-disk collisions (Guardrail #3)."""
    target_counts: dict[str, int] = {}
    for p in plans:
        if p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE and p.output_rel_path:
            key = p.output_rel_path.strip().lower()
            target_counts[key] = target_counts.get(key, 0) + 1

    checked: list[BatchFilePlan] = []
    for p in plans:
        if p.action != AppConstants.BATCH_PLAN_ACTION_ENQUEUE or not p.output_rel_path:
            checked.append(p)
            continue

        norm_key = p.output_rel_path.strip().lower()
        collision_msg: str | None = None

        if target_counts.get(norm_key, 0) > 1:
            collision_msg = (
                f"Duplicate output filename in batch: {p.output_filename or p.output_rel_path}"
            )
        else:
            try:
                target_disk = safe_resolve_path(p.output_rel_path, storage_root)
                source_disk = safe_resolve_path(p.filepath, storage_root)
                if target_disk.exists():
                    if target_disk.resolve() == source_disk.resolve():
                        collision_msg = (
                            "Destination matches original file — pick a different folder, "
                            "rename the file, or switch Output Mode to Replace Original."
                        )
                    else:
                        collision_msg = (
                            f"File already exists on disk: {p.output_filename or p.output_rel_path}"
                        )
            except PathSafetyError:
                collision_msg = (
                    f"Invalid output path outside storage: {p.output_rel_path}"
                )

        if collision_msg:
            checked.append(
                p.model_copy(
                    update={
                        "action": AppConstants.BATCH_PLAN_ACTION_SKIP_WARNING,
                        "stage_plan": AppConstants.BATCH_STAGE_NONE,
                        "reason": "Filename Collision",
                        "collision_error": collision_msg,
                        "job_config": None,
                    }
                )
            )
        else:
            checked.append(p)

    return checked


def compile_batch_plans(
    rows: list[dict[str, Any]],
    rule: BatchRuleSpec,
    *,
    active_job_filepaths: set[str],
    storage_root: Path,
    user_preferred_language: str = AppConstants.DEFAULT_PREFERRED_LANGUAGE,
    smart_fill_track_format: str = AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT,
    file_overrides: dict[int, BatchFileOverride] | None = None,
    excluded_file_ids: set[int] | None = None,
) -> list[BatchFilePlan]:
    """Natural-sort rows, compile per-file plans, and validate collisions (Guardrails #1 & #3).

    When `excluded_file_ids` is supplied (unchecked rows from Dry-Run Preview), sequential
    numbering (`seq_index`, `pad_digits`) is computed over the full sorted scope first so
    included files keep the exact filenames shown in Preview, while excluded rows are omitted
    before collision validation and enqueuing.
    """
    sorted_rows = sorted(
        rows,
        key=lambda r: natural_sort_key(str(r.get("filepath") or r.get("filename") or "")),
    )
    overrides_map = file_overrides or {}
    excluded_set = excluded_file_ids or set()
    max_seq = rule.rename.start_number + max(0, len(sorted_rows) - 1)
    pad_digits = (
        AppConstants.BATCH_RENAME_PAD_DIGITS_3
        if max_seq >= AppConstants.BATCH_RENAME_PAD_THRESHOLD_3
        else AppConstants.BATCH_RENAME_PAD_DIGITS_2
    )

    plans = [
        compile_batch_rule_for_file(
            row,
            rule,
            active_job_filepaths=active_job_filepaths,
            user_preferred_language=user_preferred_language,
            smart_fill_track_format=smart_fill_track_format,
            file_override=overrides_map.get(int(row["id"])),
            seq_index=rule.rename.start_number + idx,
            pad_digits=pad_digits,
        )
        for idx, row in enumerate(sorted_rows)
        if int(row["id"]) not in excluded_set
    ]
    return apply_batch_collision_checks(plans, storage_root=storage_root)


def build_batch_preview_response(
    rows: list[dict[str, Any]],
    rule: BatchRuleSpec,
    *,
    batch_scope_label: str,
    active_job_filepaths: set[str],
    storage_root: Path,
    user_preferred_language: str = AppConstants.DEFAULT_PREFERRED_LANGUAGE,
    smart_fill_track_format: str = AppConstants.DEFAULT_SMART_FILL_TRACK_FORMAT,
    file_overrides: dict[int, BatchFileOverride] | None = None,
) -> BatchPreviewResponse:
    """Compile all selected rows (natural-sorted, with overrides & collision checks) into `BatchPreviewResponse`."""
    inventory = summarize_batch_inventory(rows)
    plans = compile_batch_plans(
        rows,
        rule,
        active_job_filepaths=active_job_filepaths,
        storage_root=storage_root,
        user_preferred_language=user_preferred_language,
        smart_fill_track_format=smart_fill_track_format,
        file_overrides=file_overrides,
    )

    enqueue_count = sum(
        1 for p in plans if p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
    )
    transcode_count = sum(
        1
        for p in plans
        if p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        and p.stage_plan == AppConstants.BATCH_STAGE_TRANSCODE_AND_REMUX
    )
    remux_only_count = sum(
        1
        for p in plans
        if p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        and p.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
    )
    skip_compliant_count = sum(
        1 for p in plans if p.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
    )
    skip_active_job_count = sum(
        1 for p in plans if p.action == AppConstants.BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB
    )
    collision_count = sum(1 for p in plans if p.collision_error)
    skip_other_count = len(plans) - (
        enqueue_count + skip_compliant_count + skip_active_job_count
    )

    free_bytes, required_bytes, disk_ok, disk_warn = estimate_batch_disk_space(
        plans,
        output_mode=rule.output_mode,
        storage_root=storage_root,
        delete_original=bool(rule.delete_original),
    )

    return BatchPreviewResponse(
        batch_scope_label=batch_scope_label,
        inventory=inventory,
        items=plans,
        enqueue_count=enqueue_count,
        transcode_count=transcode_count,
        remux_only_count=remux_only_count,
        skip_compliant_count=skip_compliant_count,
        skip_active_job_count=skip_active_job_count,
        skip_other_count=max(0, skip_other_count),
        collision_count=collision_count,
        disk_free_bytes=free_bytes,
        disk_required_bytes=required_bytes,
        disk_space_ok=disk_ok,
        disk_space_warning=disk_warn,
    )
