"""Speaker & Audio System Test Media Generator (zero external fonts or sample assets)."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConstants
from app.models.schemas import TestMediaGenerateRequest, TestMediaGenerateResponse
from app.services.ffmpeg_runner import resolve_effective_bitrate
from app.services.library_events import LibraryEventBroker
from app.services.mkv_runner import _move_without_overwrite, _safe_unlink
from app.services.scanner import LibraryScanner
from app.utils.paths import (
    PathSafetyError,
    extract_folder,
    safe_resolve_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")


class TestMediaValidationError(ValueError):
    """Raised when a requested test-media combination or path is invalid."""

    __test__ = False


class TestMediaCollisionError(FileExistsError):
    """Raised when the requested destination file already exists on disk."""

    __test__ = False


class TestMediaInsufficientSpaceError(RuntimeError):
    """Raised when the target directory has insufficient free disk space."""

    __test__ = False


class TestMediaTimeoutError(TimeoutError):
    """Raised when FFmpeg exceeds the synchronous generation timeout."""

    __test__ = False


class TestMediaGenerationError(RuntimeError):
    """Raised when FFmpeg fails during test media generation."""

    __test__ = False


@dataclass(frozen=True)
class SpeakerChannelDef:
    """Static speaker channel specification for test tones and visual room diagram."""

    code: str
    label: str
    freq_hz: int
    is_lfe: bool
    box_x: int
    box_y: int


@dataclass(frozen=True)
class SpeakerSlot:
    """One timed speaker event inside a generated speaker test clip."""

    index: int
    channel_index: int | None  # None for final all-channel check
    code: str
    label: str
    start_sec: float
    end_sec: float
    freq_hz: int
    is_lfe: bool
    box_x: int | None
    box_y: int | None


# Canonical speaker order matching FFmpeg channel layouts (mono, stereo, 2.1, 5.1, 7.1)
_SPEAKER_DEFS: dict[str, SpeakerChannelDef] = {
    "FC": SpeakerChannelDef("FC", "Center (FC)", 523, False, 550, 90),
    "FL": SpeakerChannelDef("FL", "Front Left (FL)", 440, False, 190, 120),
    "FR": SpeakerChannelDef("FR", "Front Right (FR)", 494, False, 910, 120),
    "LFE": SpeakerChannelDef(
        "LFE",
        f"Subwoofer (LFE · {AppConstants.TEST_MEDIA_LFE_FREQ_HZ} Hz)",
        AppConstants.TEST_MEDIA_LFE_FREQ_HZ,
        True,
        550,
        250,
    ),
    "BL": SpeakerChannelDef("BL", "Back Left (BL)", 349, False, 240, 520),
    "BR": SpeakerChannelDef("BR", "Back Right (BR)", 392, False, 860, 520),
    "SL": SpeakerChannelDef("SL", "Surround Left (SL)", 330, False, 120, 340),
    "SR": SpeakerChannelDef("SR", "Surround Right (SR)", 370, False, 980, 340),
}

# Exact channel order per FFmpeg layout (`join` channel names)
_LAYOUT_SPEAKER_ORDER: dict[str, tuple[str, ...]] = {
    "1.0": ("FC",),
    "2.0": ("FL", "FR"),
    "2.1": ("FL", "FR", "LFE"),
    "5.1": ("FL", "FR", "FC", "LFE", "BL", "BR"),
    "7.1": ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"),
}

_DEFAULT_BITRATE_BY_CODEC_AND_CHANNELS: dict[str, dict[int, str]] = {
    "eac3": {1: "128k", 2: "256k", 3: "384k", 6: "640k"},
    "ac3": {1: "128k", 2: "256k", 3: "384k", 6: "448k"},
    "aac": {1: "128k", 2: "256k", 3: "320k", 6: "448k", 8: "640k"},
    "opus": {1: "96k", 2: "192k", 6: "320k", 8: "448k"},
}


@dataclass(frozen=True)
class ValidatedTestMediaSpec:
    """Fully validated, normalized specification ready for FFmpeg/mkvmerge execution."""

    output_mode: str
    codec: str
    layout: str
    channels: int
    ffmpeg_layout: str
    container: str
    bitrate: str | None
    signal_type: str
    tone_duration_sec: float
    gap_duration_sec: float
    include_subtitles: bool
    destination_dir: Path
    final_path: Path
    warnings: tuple[str, ...]


def resolve_test_ffmpeg_layout(codec: str, layout: str) -> str:
    """Resolve the canonical FFmpeg channel layout string per encoder."""
    if layout == "5.1":
        return AppConstants.TEST_MEDIA_51_ENCODER_LAYOUT_MAP.get(codec, "5.1")
    if layout not in AppConstants.TEST_MEDIA_FFMPEG_LAYOUT_MAP:
        raise TestMediaValidationError(f"Unsupported speaker layout: {layout!r}")
    return AppConstants.TEST_MEDIA_FFMPEG_LAYOUT_MAP[layout]


def resolve_default_test_bitrate(codec: str, channels: int) -> str | None:
    """Return the sensible default bitrate for lossy codecs, or None for lossless."""
    if codec in AppConstants.TEST_MEDIA_LOSSLESS_CODECS:
        return None
    by_ch = _DEFAULT_BITRATE_BY_CODEC_AND_CHANNELS.get(codec, {})
    if channels in by_ch:
        return by_ch[channels]
    return AppConstants.DEFAULT_BITRATE


def build_default_test_filename(
    *,
    codec: str,
    layout: str,
    container: str,
    signal_type: str,
) -> str:
    """Build a deterministic, collision-friendly default filename."""
    codec_tag = codec.upper()
    signal_suffix = "_PinkNoise" if signal_type == AppConstants.TEST_MEDIA_SIGNAL_PINK else ""
    return f"MKVForge_Test_{codec_tag}_{layout}{signal_suffix}.{container}"


def _sanitize_filename(raw_filename: str, expected_ext: str) -> str:
    """Validate user-supplied filename and enforce the resolved container extension."""
    cleaned = raw_filename.strip()
    if not cleaned:
        raise TestMediaValidationError("Filename must not be empty.")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned or ".." in cleaned:
        raise TestMediaValidationError(
            "Filename must not contain path separators or traversal sequences."
        )
    if len(cleaned) > AppConstants.TEST_MEDIA_MAX_FILENAME_LEN:
        raise TestMediaValidationError(
            f"Filename exceeds {AppConstants.TEST_MEDIA_MAX_FILENAME_LEN} characters."
        )
    if not _SAFE_FILENAME_RE.match(cleaned):
        raise TestMediaValidationError(
            "Filename may only contain letters, numbers, spaces, hyphens, underscores, and dots."
        )

    suffix = f".{expected_ext.lower()}"
    stem = cleaned[: -len(suffix)] if cleaned.lower().endswith(suffix) else Path(cleaned).stem
    stem = stem.strip(" .")
    if not stem:
        raise TestMediaValidationError("Filename must include a valid base name.")
    if AppConstants.TEMP_MARKER in f"{stem}{suffix}".lower():
        raise TestMediaValidationError("Filename must not contain reserved '.mkvforge.' marker.")
    return f"{stem}{suffix}"


def validate_test_media_spec(
    req: TestMediaGenerateRequest,
    *,
    storage_root: Path,
) -> ValidatedTestMediaSpec:
    """Validate every combination of mode, codec, layout, container, bitrate, and path."""
    output_mode = req.output_mode
    codec = req.codec
    layout = req.layout
    signal_type = req.signal_type

    if output_mode not in AppConstants.TEST_MEDIA_OUTPUT_MODES:
        raise TestMediaValidationError(f"Unsupported output mode: {output_mode!r}")
    if codec not in AppConstants.TEST_MEDIA_ALLOWED_CODECS:
        raise TestMediaValidationError(f"Unsupported audio codec: {codec!r}")
    if layout not in AppConstants.TEST_MEDIA_ALLOWED_LAYOUTS:
        raise TestMediaValidationError(f"Unsupported speaker layout: {layout!r}")
    if signal_type not in AppConstants.TEST_MEDIA_SIGNAL_TYPES:
        raise TestMediaValidationError(f"Unsupported signal type: {signal_type!r}")

    tone_duration_sec = round(float(req.tone_duration_sec), 3)
    gap_duration_sec = round(float(req.gap_duration_sec), 3)
    if (
        tone_duration_sec < AppConstants.TEST_MEDIA_MIN_SLOT_DURATION_SEC
        or tone_duration_sec > AppConstants.TEST_MEDIA_MAX_SLOT_DURATION_SEC
    ):
        raise TestMediaValidationError(
            f"Tone duration must be between {AppConstants.TEST_MEDIA_MIN_SLOT_DURATION_SEC}s "
            f"and {AppConstants.TEST_MEDIA_MAX_SLOT_DURATION_SEC}s."
        )
    if (
        gap_duration_sec < AppConstants.TEST_MEDIA_MIN_GAP_DURATION_SEC
        or gap_duration_sec > AppConstants.TEST_MEDIA_MAX_GAP_DURATION_SEC
    ):
        raise TestMediaValidationError(
            f"Gap duration must be between {AppConstants.TEST_MEDIA_MIN_GAP_DURATION_SEC}s "
            f"and {AppConstants.TEST_MEDIA_MAX_GAP_DURATION_SEC}s."
        )

    channels = AppConstants.TEST_MEDIA_LAYOUT_CHANNELS[layout]
    max_channels = AppConstants.TEST_MEDIA_CODEC_MAX_CHANNELS[codec]
    if channels > max_channels:
        raise TestMediaValidationError(
            f"{AppConstants.TEST_MEDIA_CODEC_LABELS[codec]} supports up to 5.1 (6 channels); "
            f"choose 5.1 or switch to FLAC, PCM, Opus, or AAC for {layout}."
        )

    # Empirical FFmpeg/libopus RFC 7845 constraint: 3ch Opus mapping family 1 is 3.0, not 2.1
    if codec == AppConstants.TARGET_CODEC_OPUS and layout == "2.1":
        raise TestMediaValidationError(
            "Opus (RFC 7845) defines 3-channel surround as 3.0, not 2.1 (FL+FR+LFE). "
            "Choose 2.0 or 5.1, or switch codec to E-AC-3, AC-3, AAC, FLAC, or PCM for 2.1."
        )

    # Resolve & validate container
    if output_mode == AppConstants.TEST_MEDIA_OUTPUT_VIDEO:
        container = AppConstants.TEST_MEDIA_VIDEO_CONTAINER
        if req.container and req.container.strip().lower().lstrip(".") != container:
            raise TestMediaValidationError(
                "Video + Audio test output always uses the Matroska (.mkv) container."
            )
    else:
        allowed_containers = AppConstants.TEST_MEDIA_AUDIO_CONTAINERS_BY_CODEC[codec]
        requested_container = (
            req.container.strip().lower().lstrip(".") if req.container else allowed_containers[0]
        )
        if requested_container not in allowed_containers:
            allowed_display = ", ".join(f".{ext}" for ext in allowed_containers)
            raise TestMediaValidationError(
                f"Container '.{requested_container}' is not compatible with "
                f"{AppConstants.TEST_MEDIA_CODEC_LABELS[codec]} (supported: {allowed_display})."
            )
        container = requested_container

    # Resolve & validate bitrate
    effective_bitrate: str | None
    if codec in AppConstants.TEST_MEDIA_LOSSLESS_CODECS:
        effective_bitrate = None
    else:
        raw_bitrate = (
            req.bitrate.strip().lower()
            if req.bitrate and req.bitrate.strip()
            else resolve_default_test_bitrate(codec, channels)
        )
        allowed_rates = AppConstants.AUDIO_CODEC_ALLOWED_BITRATES.get(codec, frozenset())
        if not raw_bitrate or raw_bitrate not in allowed_rates:
            raise TestMediaValidationError(
                f"Unsupported bitrate {raw_bitrate!r} for {AppConstants.TEST_MEDIA_CODEC_LABELS[codec]}."
            )
        effective_bitrate = resolve_effective_bitrate(codec, raw_bitrate, channels)

    # Validate destination folder strictly inside storage_root (never auto-creates unexpected folders)
    raw_dest = (req.destination_dir or AppConstants.TEST_MEDIA_DEFAULT_DESTINATION_DIR).strip()
    if raw_dest in ("", "/", "."):
        dest_dir = storage_root.resolve(strict=False)
    else:
        rel_dest = raw_dest.lstrip("/")
        try:
            dest_dir = safe_resolve_path(rel_dest, storage_root, allow_root=True)
        except PathSafetyError as exc:
            raise TestMediaValidationError(str(exc)) from exc

    if not dest_dir.is_dir():
        raise TestMediaValidationError(
            f"Destination folder does not exist: {raw_dest or '/'}"
        )

    filename = (
        _sanitize_filename(req.filename, container)
        if req.filename and req.filename.strip()
        else build_default_test_filename(
            codec=codec,
            layout=layout,
            container=container,
            signal_type=signal_type,
        )
    )
    final_path = dest_dir / filename
    try:
        safe_resolve_path(final_path, storage_root)
    except PathSafetyError as exc:
        raise TestMediaValidationError(str(exc)) from exc

    warnings: list[str] = []
    if codec in (
        AppConstants.TARGET_CODEC_OPUS,
        AppConstants.TARGET_CODEC_FLAC,
        AppConstants.TEST_MEDIA_CODEC_PCM,
    ) and channels > AppConstants.CHANNELS_STEREO:
        warnings.append(
            f"Multichannel {AppConstants.TEST_MEDIA_CODEC_LABELS[codec]} ({layout}) "
            "requires HDMI/eARC LPCM decoding — not supported over Optical SPDIF."
        )
    if codec == AppConstants.TARGET_CODEC_AAC and channels > AppConstants.CHANNELS_STEREO:
        warnings.append(
            f"Many TVs and soundbars downmix AAC {layout} to 2.0 stereo over ARC/Optical; "
            "use E-AC-3 or AC-3 to test native Dolby surround."
        )
    if output_mode == AppConstants.TEST_MEDIA_OUTPUT_AUDIO:
        warnings.append(
            "Audio-only files work great on music receivers/DACs, while Smart TV apps "
            "typically show richer speaker labels with Video + Audio (.mkv)."
        )

    include_subtitles = (
        bool(req.include_subtitles)
        if output_mode == AppConstants.TEST_MEDIA_OUTPUT_VIDEO
        else False
    )

    return ValidatedTestMediaSpec(
        output_mode=output_mode,
        codec=codec,
        layout=layout,
        channels=channels,
        ffmpeg_layout=resolve_test_ffmpeg_layout(codec, layout),
        container=container,
        bitrate=effective_bitrate,
        signal_type=signal_type,
        tone_duration_sec=tone_duration_sec,
        gap_duration_sec=gap_duration_sec,
        include_subtitles=include_subtitles,
        destination_dir=dest_dir,
        final_path=final_path,
        warnings=tuple(warnings),
    )


def build_speaker_timeline(
    layout: str,
    *,
    tone_duration_sec: float = AppConstants.TEST_MEDIA_SLOT_DURATION_SEC,
    gap_duration_sec: float = AppConstants.TEST_MEDIA_GAP_DURATION_SEC,
) -> tuple[list[SpeakerSlot], float]:
    """Build deterministic per-speaker cue timeline + final all-channel balance slot."""
    speaker_codes = _LAYOUT_SPEAKER_ORDER[layout]
    slot_dur = round(float(tone_duration_sec), 3)
    gap_dur = round(float(gap_duration_sec), 3)
    all_dur = slot_dur

    slots: list[SpeakerSlot] = []
    cursor = 0.0
    for idx, code in enumerate(speaker_codes):
        sdef = _SPEAKER_DEFS[code]
        start = round(cursor, 3)
        end = round(cursor + slot_dur, 3)
        slots.append(
            SpeakerSlot(
                index=idx,
                channel_index=idx,
                code=sdef.code,
                label=sdef.label,
                start_sec=start,
                end_sec=end,
                freq_hz=sdef.freq_hz,
                is_lfe=sdef.is_lfe,
                box_x=sdef.box_x,
                box_y=sdef.box_y,
            )
        )
        cursor = end + gap_dur

    # Final all-channel balance check (skip duplicate ALL slot for 1.0 mono)
    if len(speaker_codes) > 1:
        all_start = round(cursor, 3)
        all_end = round(cursor + all_dur, 3)
        slots.append(
            SpeakerSlot(
                index=len(slots),
                channel_index=None,
                code="ALL",
                label="All Speakers Balance Check",
                start_sec=all_start,
                end_sec=all_end,
                freq_hz=AppConstants.TEST_MEDIA_ALL_CHANNEL_FREQ_HZ,
                is_lfe=False,
                box_x=None,
                box_y=None,
            )
        )
        total_sec = round(all_end + gap_dur, 3)
    else:
        total_sec = round(cursor, 3)

    return slots, total_sec


def _format_srt_timestamp(seconds: float) -> str:
    """Format fractional seconds as `HH:MM:SS,mmm`."""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def build_test_srt_content(
    slots: list[SpeakerSlot],
    *,
    codec: str,
    layout: str,
    signal_type: str,
) -> str:
    """Generate synced forced `.srt` subtitles announcing every active speaker."""
    codec_label = AppConstants.TEST_MEDIA_CODEC_LABELS[codec]
    signal_label = (
        "Pink Noise"
        if signal_type == AppConstants.TEST_MEDIA_SIGNAL_PINK
        else "Test Tone"
    )
    blocks: list[str] = []
    for idx, slot in enumerate(slots, start=1):
        start_ts = _format_srt_timestamp(slot.start_sec)
        end_ts = _format_srt_timestamp(slot.end_sec)
        if slot.code == "ALL":
            headline = f"ALL CHANNELS ({layout}) — Balance Check"
        elif signal_type == AppConstants.TEST_MEDIA_SIGNAL_PINK:
            headline = f"{slot.label} — {signal_label}"
        else:
            headline = f"{slot.label} — {slot.freq_hz} Hz"
        blocks.append(
            f"{idx}\n{start_ts} --> {end_ts}\n{headline}\n{codec_label} {layout} Speaker Test\n"
        )
    return "\n".join(blocks)


def build_audio_filter_complex(
    slots: list[SpeakerSlot],
    total_sec: float,
    *,
    spec: ValidatedTestMediaSpec,
) -> str:
    """Build sample-accurate, pop-free FFmpeg `filter_complex` via per-channel `concat` + `join`.

    Using explicit `anullsrc` silence segments + `concat` instead of `adelay`/`apad`/`amix`
    guarantees exact clip duration across all FFmpeg versions (Debian 5.x through macOS 9.x)
    with zero PTS drift and zero channel bleed.
    """
    sr = AppConstants.TEST_MEDIA_SAMPLE_RATE
    fade = AppConstants.TEST_MEDIA_FADE_DURATION_SEC
    speaker_codes = _LAYOUT_SPEAKER_ORDER[spec.layout]
    all_slot = next((s for s in slots if s.channel_index is None), None)
    chains: list[str] = []

    for ch_idx, _code in enumerate(speaker_codes):
        slot = next(s for s in slots if s.channel_index == ch_idx)
        dur = round(slot.end_sec - slot.start_sec, 3)
        fade_out_st = round(max(0.0, dur - fade), 3)

        if spec.signal_type == AppConstants.TEST_MEDIA_SIGNAL_PINK:
            src_filter = (
                f"anoisesrc=color=pink:sample_rate={sr}:duration={dur}:seed={100 + ch_idx}"
            )
            if slot.is_lfe:
                src_filter += f",lowpass=f={AppConstants.TEST_MEDIA_LFE_LOWPASS_HZ}"
            vol_db = AppConstants.TEST_MEDIA_PINK_VOLUME_DB
        else:
            src_filter = f"sine=frequency={slot.freq_hz}:sample_rate={sr}:duration={dur}"
            vol_db = (
                AppConstants.TEST_MEDIA_LFE_VOLUME_DB
                if slot.is_lfe
                else AppConstants.TEST_MEDIA_TONE_VOLUME_DB
            )

        solo_filter = (
            f"{src_filter},"
            f"volume={vol_db}dB,"
            f"afade=t=in:st=0:d={fade},"
            f"afade=t=out:st={fade_out_st}:d={fade},"
            "aformat=channel_layouts=mono"
        )

        seg_labels: list[str] = []
        seg_idx = 0

        def _append_silence(silence_dur: float) -> None:
            nonlocal seg_idx
            rounded = round(silence_dur, 3)
            if rounded <= 0:
                return
            lbl = f"c{ch_idx}_s{seg_idx}"
            seg_idx += 1
            chains.append(f"anullsrc=r={sr}:cl=mono:d={rounded}[{lbl}]")
            seg_labels.append(f"[{lbl}]")

        # 1. Leading silence before this speaker's slot
        _append_silence(slot.start_sec)

        # 2. Isolated solo speaker tone / pink noise
        solo_lbl = f"c{ch_idx}_solo"
        chains.append(f"{solo_filter}[{solo_lbl}]")
        seg_labels.append(f"[{solo_lbl}]")

        # 3. Optional middle silence + final all-channel balance check
        if all_slot is not None:
            _append_silence(all_slot.start_sec - slot.end_sec)

            all_dur = round(all_slot.end_sec - all_slot.start_sec, 3)
            all_fade_out_st = round(max(0.0, all_dur - fade), 3)
            if spec.signal_type == AppConstants.TEST_MEDIA_SIGNAL_PINK:
                all_src = (
                    f"anoisesrc=color=pink:sample_rate={sr}:duration={all_dur}:seed={200 + ch_idx}"
                )
                if slot.is_lfe:
                    all_src += f",lowpass=f={AppConstants.TEST_MEDIA_LFE_LOWPASS_HZ}"
            else:
                all_freq = (
                    AppConstants.TEST_MEDIA_LFE_FREQ_HZ
                    if slot.is_lfe
                    else AppConstants.TEST_MEDIA_ALL_CHANNEL_FREQ_HZ
                )
                all_src = f"sine=frequency={all_freq}:sample_rate={sr}:duration={all_dur}"

            all_lbl = f"c{ch_idx}_all"
            chains.append(
                f"{all_src},"
                f"volume={AppConstants.TEST_MEDIA_ALL_CHANNEL_VOLUME_DB}dB,"
                f"afade=t=in:st=0:d={fade},"
                f"afade=t=out:st={all_fade_out_st}:d={fade},"
                f"aformat=channel_layouts=mono[{all_lbl}]"
            )
            seg_labels.append(f"[{all_lbl}]")
            _append_silence(total_sec - all_slot.end_sec)
        else:
            _append_silence(total_sec - slot.end_sec)

        if len(seg_labels) == 1:
            chains.append(f"{seg_labels[0]}acopy[ch{ch_idx}]")
        else:
            chains.append(
                f"{''.join(seg_labels)}concat=n={len(seg_labels)}:v=0:a=1[ch{ch_idx}]"
            )

    frame_size = AppConstants.TEST_MEDIA_ENCODER_FRAME_SIZE_MAP[spec.codec]
    reframe_tail = f"asetnsamples=n={frame_size}:p=1,asetpts=N/SR/TB"

    if len(speaker_codes) == 1:
        chains.append(f"[ch0]aformat=channel_layouts=mono,{reframe_tail}[aout]")
    else:
        inputs_tag = "".join(f"[ch{i}]" for i in range(len(speaker_codes)))
        map_spec = "|".join(f"{i}.0-{code}" for i, code in enumerate(speaker_codes))
        chains.append(
            f"{inputs_tag}join=inputs={len(speaker_codes)}:"
            f"channel_layout={spec.ffmpeg_layout}:map={map_spec},{reframe_tail}[aout]"
        )

    return ";".join(chains)


def build_video_filter_graph(
    slots: list[SpeakerSlot],
    total_sec: float,
    *,
    layout: str,
) -> str:
    """Build zero-font dark 720p room diagram with synced `drawbox` speaker highlights."""
    w = AppConstants.TEST_MEDIA_VIDEO_WIDTH
    h = AppConstants.TEST_MEDIA_VIDEO_HEIGHT
    fps = AppConstants.TEST_MEDIA_VIDEO_FPS
    bw = AppConstants.TEST_MEDIA_BOX_W
    bh = AppConstants.TEST_MEDIA_BOX_H
    bg = AppConstants.TEST_MEDIA_VIDEO_BG_COLOR
    dim = AppConstants.TEST_MEDIA_BOX_DIM_COLOR
    active = AppConstants.TEST_MEDIA_BOX_ACTIVE_COLOR
    lfe_active = AppConstants.TEST_MEDIA_BOX_LFE_ACTIVE_COLOR

    speaker_codes = _LAYOUT_SPEAKER_ORDER[layout]
    all_slot = next((s for s in slots if s.channel_index is None), None)

    filters: list[str] = [f"color=c={bg}:s={w}x{h}:r={fps}:d={total_sec}"]

    # Subtle screen-top "TV / Front Wall" bar and dim resting speaker boxes
    filters.append(f"drawbox=x=380:y=28:w=520:h=10:color={dim}:t=fill")
    for code in speaker_codes:
        sdef = _SPEAKER_DEFS[code]
        filters.append(
            f"drawbox=x={sdef.box_x}:y={sdef.box_y}:w={bw}:h={bh}:color={dim}:t=fill"
        )

    # Active speaker highlight boxes synchronized to exact time slots
    for slot in slots:
        if slot.channel_index is not None and slot.box_x is not None and slot.box_y is not None:
            color = lfe_active if slot.is_lfe else active
            filters.append(
                f"drawbox=x={slot.box_x}:y={slot.box_y}:w={bw}:h={bh}:"
                f"color={color}:t=fill:enable='between(t,{slot.start_sec},{slot.end_sec})'"
            )

    # Light all speaker boxes simultaneously during final balance check
    if all_slot is not None:
        for code in speaker_codes:
            sdef = _SPEAKER_DEFS[code]
            color = lfe_active if sdef.is_lfe else active
            filters.append(
                f"drawbox=x={sdef.box_x}:y={sdef.box_y}:w={bw}:h={bh}:"
                f"color={color}:t=fill:enable='between(t,{all_slot.start_sec},{all_slot.end_sec})'"
            )

    return ",".join(filters) + "[vout]"


def build_test_ffmpeg_command(
    output_path: Path,
    *,
    spec: ValidatedTestMediaSpec,
    slots: list[SpeakerSlot],
    total_sec: float,
    srt_path: Path | None = None,
) -> list[str]:
    """Build deterministic single-pass FFmpeg argv for audio-only or video+audio+forced-SRT MKV."""
    audio_fc = build_audio_filter_complex(slots, total_sec, spec=spec)
    encoder = AppConstants.TEST_MEDIA_ENCODER_MAP[spec.codec]
    codec_label = AppConstants.TEST_MEDIA_CODEC_LABELS[spec.codec]

    argv: list[str] = [AppConstants.TOOL_FFMPEG, "-y"]

    if spec.output_mode == AppConstants.TEST_MEDIA_OUTPUT_VIDEO:
        if srt_path is not None:
            argv.extend(["-i", str(srt_path)])
        video_fc = build_video_filter_graph(slots, total_sec, layout=spec.layout)
        full_fc = f"{video_fc};{audio_fc}"
        argv.extend(
            [
                "-filter_complex",
                full_fc,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ]
        )
        if srt_path is not None:
            argv.extend(["-map", "0:s:0"])
        argv.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                AppConstants.TEST_MEDIA_VIDEO_PRESET,
                "-crf",
                AppConstants.TEST_MEDIA_VIDEO_CRF,
                "-pix_fmt",
                "yuv420p",
                "-t",
                str(total_sec),
            ]
        )
    else:
        argv.extend(["-filter_complex", audio_fc, "-map", "[aout]"])

    argv.extend(
        [
            "-c:a",
            encoder,
            "-ar",
            str(AppConstants.TEST_MEDIA_SAMPLE_RATE),
        ]
    )
    if spec.bitrate is not None:
        argv.extend(["-b:a", spec.bitrate])
    if spec.codec == AppConstants.TARGET_CODEC_OPUS and spec.channels > AppConstants.CHANNELS_STEREO:
        argv.extend(
            ["-mapping_family", str(AppConstants.OPUS_MULTICHANNEL_MAPPING_FAMILY)]
        )

    if spec.output_mode == AppConstants.TEST_MEDIA_OUTPUT_VIDEO:
        title = f"MKV Forge Speaker Test — {codec_label} {spec.layout}"
        audio_track_name = (
            f"{codec_label} {spec.layout} • {spec.bitrate}"
            if spec.bitrate
            else f"{codec_label} {spec.layout} • Lossless"
        )
        argv.extend(
            [
                "-metadata",
                f"title={title}",
                "-metadata:s:v:0",
                f"title=Speaker Layout Diagram ({spec.layout})",
                "-metadata:s:a:0",
                f"title={audio_track_name}",
                "-metadata:s:a:0",
                "language=eng",
                "-disposition:v:0",
                "default",
                "-disposition:a:0",
                "default",
            ]
        )
        if srt_path is not None:
            argv.extend(
                [
                    "-c:s",
                    "srt",
                    "-metadata:s:s:0",
                    "title=Active Speaker Labels",
                    "-metadata:s:s:0",
                    "language=eng",
                    "-disposition:s:0",
                    "default+forced",
                ]
            )
        argv.extend(["-f", "matroska"])

    argv.append(str(output_path))
    return argv


def build_test_mkvmerge_command(
    raw_video_audio_path: Path,
    srt_path: Path,
    final_tmp_path: Path,
    *,
    spec: ValidatedTestMediaSpec,
) -> list[str]:
    """Mux FFmpeg's video+audio clip with synced forced speaker-label `.srt` subtitles."""
    codec_label = AppConstants.TEST_MEDIA_CODEC_LABELS[spec.codec]
    title = f"MKV Forge Speaker Test — {codec_label} {spec.layout}"
    audio_track_name = (
        f"{codec_label} {spec.layout} • {spec.bitrate}"
        if spec.bitrate
        else f"{codec_label} {spec.layout} • Lossless"
    )
    return [
        AppConstants.TOOL_MKVMERGE,
        "-o",
        str(final_tmp_path),
        "--title",
        title,
        "--track-name",
        f"0:Speaker Layout Diagram ({spec.layout})",
        "--language",
        "0:und",
        "--default-track-flag",
        "0:yes",
        "--track-name",
        f"1:{audio_track_name}",
        "--language",
        "1:eng",
        "--default-track-flag",
        "1:yes",
        str(raw_video_audio_path),
        "--track-name",
        "0:Active Speaker Labels",
        "--language",
        "0:eng",
        "--default-track-flag",
        "0:yes",
        "--forced-display-flag",
        "0:yes",
        str(srt_path),
    ]


async def _run_timed_subprocess(
    argv: list[str],
    *,
    timeout_sec: float = AppConstants.TEST_MEDIA_TIMEOUT_SEC,
    allowed_exit_codes: tuple[int, ...] = (AppConstants.EXIT_CODE_SUCCESS,),
) -> None:
    """Execute a subprocess with strict timeout and clean SIGKILL teardown."""
    tool_name = argv[0]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TestMediaGenerationError(f"{tool_name} executable not found in PATH") from exc
    except OSError as exc:
        raise TestMediaGenerationError(f"Failed to launch {tool_name}: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise TestMediaTimeoutError(
            f"{tool_name} timed out after {timeout_sec:.0f}s while generating test media."
        ) from exc

    if proc.returncode not in allowed_exit_codes:
        stderr_text = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
        stdout_text = stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
        tail = (stderr_text or stdout_text).splitlines()[-3:]
        excerpt = " | ".join(tail)[: AppConstants.MAX_ERROR_EXCERPT_LEN]
        raise TestMediaGenerationError(
            f"{tool_name} failed (exit {proc.returncode}): {excerpt}"
        )


async def generate_test_media(
    req: TestMediaGenerateRequest,
    *,
    storage_root: Path,
    scanner: LibraryScanner | None = None,
    library_broker: LibraryEventBroker | None = None,
    timeout_sec: float = AppConstants.TEST_MEDIA_TIMEOUT_SEC,
) -> TestMediaGenerateResponse:
    """Validate, synthesize, atomically commit, and index a speaker test media file."""
    spec = validate_test_media_spec(req, storage_root=storage_root)

    if spec.final_path.exists():
        raise TestMediaCollisionError(
            f"File already exists: {spec.final_path.name}. Choose a different filename or folder."
        )

    usage = await asyncio.to_thread(shutil.disk_usage, spec.destination_dir)
    if usage.free < AppConstants.TEST_MEDIA_MIN_FREE_BYTES:
        raise TestMediaInsufficientSpaceError(
            "Insufficient free disk space in destination folder to generate test file."
        )

    slots, total_sec = build_speaker_timeline(
        spec.layout,
        tone_duration_sec=spec.tone_duration_sec,
        gap_duration_sec=spec.gap_duration_sec,
    )
    tmp_raw_path: Path | None = None
    tmp_srt_path: Path | None = None
    tmp_final_path: Path | None = None
    finalized = False

    try:
        if spec.output_mode == AppConstants.TEST_MEDIA_OUTPUT_VIDEO:
            tmp_final_path = spec.destination_dir / (
                f"{spec.final_path.name}{AppConstants.TEMP_REMUX_SUFFIX}"
            )
            if spec.include_subtitles:
                tmp_srt_path = spec.destination_dir / (
                    f"{spec.final_path.name}{AppConstants.TEMP_MARKER}labels.srt"
                )
                srt_text = build_test_srt_content(
                    slots,
                    codec=spec.codec,
                    layout=spec.layout,
                    signal_type=spec.signal_type,
                )
                await asyncio.to_thread(
                    tmp_srt_path.write_text, srt_text, AppConstants.UTF8_ENCODING
                )

            ffmpeg_cmd = build_test_ffmpeg_command(
                tmp_final_path,
                spec=spec,
                slots=slots,
                total_sec=total_sec,
                srt_path=tmp_srt_path,
            )
            await _run_timed_subprocess(ffmpeg_cmd, timeout_sec=timeout_sec)
        else:
            tmp_final_path = spec.destination_dir / (
                f"{spec.final_path.name}{AppConstants.TEMP_MARKER}{spec.container}"
            )
            ffmpeg_cmd = build_test_ffmpeg_command(
                tmp_final_path, spec=spec, slots=slots, total_sec=total_sec
            )
            await _run_timed_subprocess(ffmpeg_cmd, timeout_sec=timeout_sec)

        try:
            await asyncio.to_thread(_move_without_overwrite, tmp_final_path, spec.final_path)
        except FileExistsError as exc:
            raise TestMediaCollisionError(
                f"File already exists: {spec.final_path.name}."
            ) from exc
        finalized = True

        stat_info = await asyncio.to_thread(spec.final_path.stat)
        rel_filepath = to_relative_storage_path(spec.final_path, storage_root)
        folder = extract_folder(rel_filepath)

        file_id: int | None = None
        if scanner is not None and spec.final_path.suffix.lower() in AppConstants.SUPPORTED_MEDIA_EXTENSIONS:
            try:
                probed_row = await scanner.probe_single_file(rel_filepath)
                if probed_row is not None and probed_row.get("id") is not None:
                    file_id = int(probed_row["id"])
            except Exception as exc:
                logger.warning("Generated test file %s but probe failed: %s", rel_filepath, exc)

        if library_broker is not None:
            library_broker.record_change(
                action="added",
                filepath=rel_filepath,
                file_id=file_id,
            )

        return TestMediaGenerateResponse(
            filepath=rel_filepath,
            filename=spec.final_path.name,
            folder=folder,
            size_bytes=int(stat_info.st_size),
            duration_sec=total_sec,
            codec=spec.codec,
            layout=spec.layout,
            channels=spec.channels,
            output_mode=spec.output_mode,
            container=spec.container,
            file_id=file_id,
            warnings=list(spec.warnings),
        )
    finally:
        _safe_unlink(tmp_raw_path)
        _safe_unlink(tmp_srt_path)
        if not finalized:
            _safe_unlink(tmp_final_path)
