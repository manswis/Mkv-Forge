"""Stage 1 audio conversion service: ffprobe verification and async FFmpeg runner."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import AppConstants

logger = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    """Raised when ffprobe or Stage 1 FFmpeg audio conversion fails."""


class StreamVerificationError(FFmpegError):
    """Raised when the ffprobe audio stream does not match the expected track."""


@dataclass(frozen=True, slots=True)
class FFprobeAudioStreamInfo:
    """Verified FFprobe metadata for a selected relative audio stream."""

    audio_index: int
    stream_index: int
    codec_name: str
    channels: int
    format_duration_us: int
    sample_rate: int | None = None
    channel_layout: str | None = None


@dataclass(frozen=True, slots=True)
class FFmpegTranscodeResult:
    """Output summary from a completed Stage 1 FFmpeg audio transcode."""

    temp_audio_path: Path
    channels: int
    effective_duration_us: int
    log_lines: list[str]


ProgressCallback = Callable[[int, str | None], None]
LogCallback = Callable[[str], None]
ProcessCallback = Callable[[asyncio.subprocess.Process], None]


def canonicalize_audio_codec(codec_name: str | None) -> str | None:
    """Normalize mkvmerge and ffprobe audio codec names to a shared canonical key."""
    if not codec_name or not codec_name.strip():
        return None
    lower = codec_name.strip().lower()
    if "e-ac-3" in lower or "eac3" in lower or "eac-3" in lower:
        return "eac3"
    if "ac-3" in lower or "ac3" in lower:
        return "ac3"
    if "aac" in lower:
        return "aac"
    if "truehd" in lower:
        return "truehd"
    if "dts" in lower or "dca" in lower:
        return "dts"
    if "opus" in lower:
        return "opus"
    if "flac" in lower:
        return "flac"
    if "mp3" in lower or "layer 3" in lower:
        return "mp3"
    if "pcm" in lower:
        return "pcm"
    if "alac" in lower:
        return "alac"
    return lower


def is_lossless_source_codec(raw_codec: str | None) -> bool:
    """Return True if a source audio track codec string represents lossless audio."""
    if not raw_codec or not raw_codec.strip():
        return False
    lower = raw_codec.strip().lower()
    if "dts-hd" in lower or "dts_hd" in lower or "dtshd" in lower:
        # DTS-HD High Resolution is lossy; DTS-HD Master Audio (MA) is lossless
        return "high resolution" not in lower and "hra" not in lower
    canonical = canonicalize_audio_codec(raw_codec)
    return canonical in AppConstants.LOSSLESS_SOURCE_CODECS


def compute_target_channels(
    source_channels: int,
    target_codec: str = AppConstants.TARGET_CODEC_EAC3,
    requested_channels: int | None = None,
) -> int:
    """Resolve output channel count bounded by source and codec capability — never upmix."""
    if source_channels < AppConstants.MIN_AUDIO_CHANNELS:
        raise ValueError(f"Invalid source audio channel count: {source_channels}")
    if target_codec not in AppConstants.AUDIO_CODEC_MAX_CHANNELS:
        raise ValueError(f"Unsupported target codec: {target_codec!r}")
    max_for_codec = AppConstants.AUDIO_CODEC_MAX_CHANNELS[target_codec]
    ceiling = min(source_channels, max_for_codec)
    if requested_channels is None:
        return ceiling
    if requested_channels < AppConstants.MIN_AUDIO_CHANNELS:
        raise ValueError(f"Invalid target audio channel count: {requested_channels}")
    if requested_channels > source_channels:
        raise ValueError(
            f"Cannot upmix from {source_channels} to {requested_channels} channels"
        )
    if requested_channels > max_for_codec:
        raise ValueError(
            f"Codec {target_codec!r} supports at most {max_for_codec} channels "
            f"(requested {requested_channels})"
        )
    return requested_channels


def resolve_target_sample_rate(
    source_sample_rate: int | None,
    target_codec: str,
) -> int | None:
    """Return an explicit `-ar` override only when the target encoder requires resampling.

    - `eac3` / `ac3`: supports `{48000, 44100, 32000}`; resamples outside that set to `48000`.
    - `opus`: native `48000 Hz`; resamples when source rate differs from `48000`.
    - `aac` / `flac`: preserves native sample rate (`None`).
    """
    if target_codec == AppConstants.TARGET_CODEC_OPUS:
        if source_sample_rate is None or source_sample_rate <= 0:
            return AppConstants.OPUS_SAMPLE_RATE
        if source_sample_rate != AppConstants.OPUS_SAMPLE_RATE:
            return AppConstants.OPUS_SAMPLE_RATE
        return None
    if source_sample_rate is None or source_sample_rate <= 0:
        return None
    if target_codec in (
        AppConstants.TARGET_CODEC_EAC3,
        AppConstants.TARGET_CODEC_AC3,
    ):
        if source_sample_rate not in AppConstants.DOLBY_SUPPORTED_SAMPLE_RATES:
            return AppConstants.DOLBY_FALLBACK_SAMPLE_RATE
    return None


def resolve_effective_bitrate(
    target_codec: str,
    bitrate: str | None,
    target_channels: int,
) -> str | None:
    """Return encoder-safe bitrate, clamping Opus mono to RFC 6716 256k ceiling."""
    if target_codec in AppConstants.LOSSLESS_TARGET_CODECS:
        return None
    if (
        target_codec == AppConstants.TARGET_CODEC_OPUS
        and target_channels == AppConstants.CHANNELS_MONO
        and bitrate is not None
    ):
        bps = AppConstants.BITRATE_BPS_MAP.get(bitrate, 0)
        if bps > AppConstants.OPUS_MAX_MONO_BPS:
            return AppConstants.OPUS_MAX_MONO_BITRATE
    return bitrate


def parse_ffmpeg_progress_kv(
    line: str,
    duration_us: int,
) -> tuple[str, Any] | None:
    """Parse one `key=value` line from `ffmpeg -progress pipe:1`.

    Safely ignores `N/A` and negative timestamps. Returns `("percent", int)`,
    `("speed", str)`, or `None`.
    """
    if "=" not in line:
        return None
    key, raw_value = line.split("=", maxsplit=1)
    key = key.strip()
    value = raw_value.strip()

    if key == "out_time_us":
        if not value or value.upper() == "N/A":
            return None
        try:
            out_us = int(value)
        except ValueError:
            return None
        if out_us < 0 or duration_us <= 0:
            return None
        percent = int((out_us / duration_us) * 100)
        bounded = max(0, min(AppConstants.FFMPEG_MAX_PROGRESS_PERCENT, percent))
        return ("percent", bounded)

    if key == "speed":
        if not value or value.upper() == "N/A":
            return None
        return ("speed", value)

    return None


def build_ffmpeg_command(
    source_path: Path,
    temp_audio_path: Path,
    *,
    audio_index: int,
    target_codec: str,
    bitrate: str | None = None,
    channels: int,
    sample_rate: int | None = None,
    requested_channels: int | None = None,
    source_codec: str | None = None,
) -> list[str]:
    """Build deterministic Stage 1 FFmpeg argv list (zero shell interpolation)."""
    if audio_index < 0:
        raise ValueError(f"audio_index must be >= 0, got {audio_index}")
    if target_codec not in AppConstants.ALLOWED_TARGET_CODECS:
        raise ValueError(f"Unsupported target codec: {target_codec!r}")
    is_lossless_target = target_codec in AppConstants.LOSSLESS_TARGET_CODECS
    if not is_lossless_target:
        allowed_rates = AppConstants.AUDIO_CODEC_ALLOWED_BITRATES.get(
            target_codec, frozenset()
        )
        if not bitrate or bitrate not in allowed_rates:
            raise ValueError(
                f"Unsupported bitrate {bitrate!r} for target codec {target_codec!r}"
            )
    target_channels = compute_target_channels(
        channels,
        target_codec=target_codec,
        requested_channels=requested_channels,
    )
    target_sample_rate = resolve_target_sample_rate(sample_rate, target_codec)
    encoder_name = AppConstants.AUDIO_CODEC_FFMPEG_ENCODER_MAP[target_codec]

    argv: list[str] = [
        AppConstants.TOOL_FFMPEG,
        "-y",
    ]
    # Disable Dolby Dynamic Range Compression on decode for AC-3 / E-AC-3 sources
    canonical_src = canonicalize_audio_codec(source_codec)
    if canonical_src in (
        AppConstants.TARGET_CODEC_AC3,
        AppConstants.TARGET_CODEC_EAC3,
    ):
        argv.extend(["-drc_scale", AppConstants.DOLBY_DECODE_DRC_SCALE_OFF])

    argv.extend(
        [
            "-i",
            str(source_path),
            "-map",
            f"0:a:{audio_index}",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:a",
            encoder_name,
        ]
    )
    if not is_lossless_target and bitrate:
        argv.extend(["-b:a", bitrate])

    argv.extend(["-ac", str(target_channels)])

    # Normalize canonical multichannel layout (prevents libopus 5.1(side)/7.1(wide) failures)
    if target_channels > AppConstants.CHANNELS_STEREO:
        layout_name = AppConstants.CHANNEL_LAYOUT_MAP.get(target_channels)
        if layout_name:
            argv.extend(["-channel_layout", layout_name])
        if target_codec == AppConstants.TARGET_CODEC_OPUS:
            argv.extend(
                [
                    "-mapping_family",
                    str(AppConstants.OPUS_MULTICHANNEL_MAPPING_FAMILY),
                ]
            )

    if target_sample_rate is not None:
        argv.extend(["-ar", str(target_sample_rate)])

    argv.extend(
        [
            "-progress",
            "pipe:1",
            "-nostats",
            str(temp_audio_path),
        ]
    )
    return argv


async def verify_audio_stream_with_ffprobe(
    source_path: Path,
    audio_index: int,
    *,
    expected_codec: str | None = None,
    expected_channels: int | None = None,
    timeout_sec: float = AppConstants.FFPROBE_TIMEOUT_SEC,
) -> FFprobeAudioStreamInfo:
    """Inspect audio streams and container duration via ffprobe before Stage 1.

    Confirms `-map 0:a:<audio_index>` exists and matches expected codec/channels,
    and returns `format_duration_us` as a reliable fallback for `.mp4` inputs.
    """
    if audio_index < 0:
        raise StreamVerificationError(f"Invalid audio_index: {audio_index}")

    try:
        proc = await asyncio.create_subprocess_exec(
            AppConstants.TOOL_FFPROBE,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:stream=index,codec_name,channels,sample_rate,channel_layout",
            "-select_streams",
            "a",
            str(source_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise FFmpegError("ffprobe executable not found in PATH") from exc
    except OSError as exc:
        raise FFmpegError(f"Failed to launch ffprobe: {exc}") from exc

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
        raise FFmpegError(f"ffprobe timed out after {timeout_sec}s") from exc

    if proc.returncode != AppConstants.EXIT_CODE_SUCCESS:
        detail = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
        raise FFmpegError(
            f"ffprobe failed with exit code {proc.returncode}: "
            f"{detail[: AppConstants.MAX_ERROR_EXCERPT_LEN]}"
        )

    try:
        payload = json.loads(
            stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace")
        )
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Invalid JSON returned by ffprobe: {exc}") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list) or audio_index >= len(streams):
        count = len(streams) if isinstance(streams, list) else 0
        raise StreamVerificationError(
            f"Audio stream index 0:a:{audio_index} not found "
            f"(ffprobe found {count} audio stream(s))"
        )

    selected = streams[audio_index]
    codec_name = str(selected.get("codec_name") or "").strip()
    channels = int(selected.get("channels") or 0)
    stream_index = int(selected.get("index", audio_index))
    raw_sample_rate = selected.get("sample_rate")
    sample_rate: int | None = None
    if raw_sample_rate not in (None, "", "N/A"):
        try:
            parsed_sr = int(raw_sample_rate)
            if parsed_sr > 0:
                sample_rate = parsed_sr
        except (TypeError, ValueError):
            sample_rate = None
    raw_layout = selected.get("channel_layout")
    channel_layout = (
        str(raw_layout).strip()
        if raw_layout not in (None, "", "N/A", "unknown")
        else None
    )

    if channels < AppConstants.MIN_AUDIO_CHANNELS:
        raise StreamVerificationError(
            f"ffprobe reported invalid channel count ({channels}) for 0:a:{audio_index}"
        )

    expected_canonical = canonicalize_audio_codec(expected_codec)
    actual_canonical = canonicalize_audio_codec(codec_name)
    if (
        expected_canonical is not None
        and actual_canonical is not None
        and expected_canonical != actual_canonical
    ):
        raise StreamVerificationError(
            f"Stream 0:a:{audio_index} codec mismatch: expected {expected_canonical!r}, "
            f"ffprobe found {actual_canonical!r}"
        )

    if expected_channels is not None and expected_channels > 0 and channels != expected_channels:
        raise StreamVerificationError(
            f"Stream 0:a:{audio_index} channel mismatch: expected {expected_channels}, "
            f"ffprobe found {channels}"
        )

    format_duration_us = 0
    fmt = payload.get("format")
    if isinstance(fmt, dict) and fmt.get("duration") not in (None, "", "N/A"):
        try:
            dur_sec = float(fmt["duration"])
            if dur_sec > 0:
                format_duration_us = int(dur_sec * AppConstants.US_PER_SEC)
        except (TypeError, ValueError):
            format_duration_us = 0

    return FFprobeAudioStreamInfo(
        audio_index=audio_index,
        stream_index=stream_index,
        codec_name=codec_name,
        channels=channels,
        format_duration_us=format_duration_us,
        sample_rate=sample_rate,
        channel_layout=channel_layout,
    )


async def run_ffmpeg_transcode(
    source_path: Path,
    temp_audio_path: Path,
    *,
    audio_index: int,
    target_codec: str,
    bitrate: str | None = None,
    target_channels: int | None = None,
    duration_us: int = 0,
    expected_codec: str | None = None,
    expected_channels: int | None = None,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    on_process_started: ProcessCallback | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> FFmpegTranscodeResult:
    """Verify stream via ffprobe and run Stage 1 FFmpeg audio transcode asynchronously."""
    if check_cancelled is not None:
        check_cancelled()

    probe_info = await verify_audio_stream_with_ffprobe(
        source_path,
        audio_index,
        expected_codec=expected_codec,
        expected_channels=expected_channels,
    )

    if check_cancelled is not None:
        check_cancelled()

    effective_duration_us = (
        duration_us if duration_us > 0 else probe_info.format_duration_us
    )
    resolved_channels = compute_target_channels(
        probe_info.channels,
        target_codec=target_codec,
        requested_channels=target_channels,
    )
    effective_bitrate = resolve_effective_bitrate(
        target_codec, bitrate, resolved_channels
    )
    if effective_bitrate != bitrate and on_log is not None:
        on_log(
            f"Mono {target_codec.upper()} caps at {effective_bitrate}; "
            f"clamping requested bitrate {bitrate} -> {effective_bitrate}."
        )
    target_sample_rate = resolve_target_sample_rate(
        probe_info.sample_rate, target_codec
    )
    if (
        target_sample_rate is not None
        and probe_info.sample_rate is not None
        and on_log is not None
    ):
        on_log(
            f"Source sample rate {probe_info.sample_rate} Hz requires resampling for "
            f"{target_codec.upper()}; resampling to {target_sample_rate} Hz."
        )

    cmd = build_ffmpeg_command(
        source_path,
        temp_audio_path,
        audio_index=audio_index,
        target_codec=target_codec,
        bitrate=effective_bitrate,
        channels=probe_info.channels,
        requested_channels=resolved_channels,
        sample_rate=probe_info.sample_rate,
        source_codec=probe_info.codec_name,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise FFmpegError("ffmpeg executable not found in PATH") from exc
    except OSError as exc:
        raise FFmpegError(f"Failed to launch ffmpeg: {exc}") from exc

    if on_process_started is not None:
        on_process_started(proc)

    last_percent = 0
    last_speed: str | None = None
    log_lines: list[str] = []

    async def _read_stdout() -> None:
        nonlocal last_percent, last_speed
        assert proc.stdout is not None
        while True:
            raw_line = await proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
            parsed = parse_ffmpeg_progress_kv(line, effective_duration_us)
            if parsed is None:
                continue
            metric, val = parsed
            if metric == "percent":
                last_percent = max(last_percent, int(val))
                if on_progress is not None:
                    on_progress(last_percent, last_speed)
            elif metric == "speed":
                last_speed = str(val)
                if on_progress is not None:
                    on_progress(last_percent, last_speed)

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            raw_line = await proc.stderr.readline()
            if not raw_line:
                break
            line = raw_line.decode(AppConstants.UTF8_ENCODING, errors="replace").rstrip()
            if line:
                log_lines.append(line)
                if on_log is not None:
                    on_log(line)

    await asyncio.gather(_read_stdout(), _read_stderr())
    returncode = await proc.wait()

    if returncode != AppConstants.EXIT_CODE_SUCCESS:
        tail = " | ".join(log_lines[-3:])[: AppConstants.MAX_ERROR_EXCERPT_LEN]
        raise FFmpegError(
            f"ffmpeg failed with exit code {returncode}"
            + (f": {tail}" if tail else "")
        )

    return FFmpegTranscodeResult(
        temp_audio_path=temp_audio_path,
        channels=resolved_channels,
        effective_duration_us=effective_duration_us,
        log_lines=log_lines,
    )
