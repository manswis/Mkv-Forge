"""Pre-flight disk space and target collision guards before media processing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConstants
from app.models.schemas import JobConfig
from app.services.ffmpeg_runner import (
    canonicalize_audio_codec,
    is_lossless_source_codec,
)
from app.utils.paths import (
    ensure_mp4_replace_target_available,
    ensure_save_as_target_available,
)


class PreflightError(RuntimeError):
    """Base error raised when a job fails pre-flight safety checks."""


class InsufficientDiskSpaceError(PreflightError):
    """Raised when the target filesystem lacks enough free space for safe output."""


class TargetFileCollisionError(PreflightError):
    """Raised when replacing an .mp4 would overwrite an existing <stem>.mkv."""


class InvalidAudioConversionError(PreflightError):
    """Raised when a requested source -> target audio conversion is illogical."""


def validate_audio_conversion_pair(
    source_codec: str | None,
    target_codec: str,
    *,
    source_channels: int | None = None,
    target_channels: int | None = None,
) -> None:
    """Validate audio conversion codec & channel constraints.

    - Rejects unsupported target codecs and channel counts exceeding codec/source caps.
    - Rejects lossy -> FLAC conversions (bloat with zero quality recovery).
    - Allows intentional same-codec downmixing (e.g. EAC3 5.1 -> EAC3 2.0 or AAC 7.1 -> AAC 2.0),
      while still rejecting pointless same-codec same-channel re-encodes and lossy AC3 -> EAC3 upsamples.
    """
    normalized_target = target_codec.strip().lower()
    if normalized_target not in AppConstants.ALLOWED_TARGET_CODECS:
        raise InvalidAudioConversionError(
            f"Unsupported target audio codec: {target_codec!r}"
        )

    max_for_codec = AppConstants.AUDIO_CODEC_MAX_CHANNELS.get(
        normalized_target, AppConstants.MAX_DOLBY_CHANNELS
    )
    if target_channels is not None:
        if target_channels < AppConstants.MIN_AUDIO_CHANNELS:
            raise InvalidAudioConversionError(
                f"Invalid target channel count: {target_channels}"
            )
        if target_channels > max_for_codec:
            raise InvalidAudioConversionError(
                f"Codec {normalized_target.upper()} supports at most {max_for_codec} channels "
                f"(requested {target_channels})"
            )
        if source_channels is not None and target_channels > source_channels:
            raise InvalidAudioConversionError(
                f"Cannot upmix audio from {source_channels} to {target_channels} channels"
            )

    canonical_source = canonicalize_audio_codec(source_codec)
    if canonical_source is None:
        return

    if (
        normalized_target in AppConstants.LOSSLESS_TARGET_CODECS
        and not is_lossless_source_codec(source_codec)
    ):
        raise InvalidAudioConversionError(
            f"Refusing lossy {canonical_source.upper()} to lossless "
            f"{normalized_target.upper()} conversion (inflates size without recovering quality)"
        )

    effective_target_channels: int | None = (
        target_channels
        if target_channels is not None
        else (min(source_channels, max_for_codec) if source_channels is not None else None)
    )
    is_explicit_downmix = (
        source_channels is not None
        and effective_target_channels is not None
        and effective_target_channels < source_channels
    )

    if canonical_source == normalized_target and not is_explicit_downmix:
        raise InvalidAudioConversionError(
            f"Source track is already {canonical_source.upper()}; "
            f"refusing pointless same-codec conversion to {normalized_target.upper()}"
        )

    if (
        canonical_source == AppConstants.TARGET_CODEC_AC3
        and normalized_target == AppConstants.TARGET_CODEC_EAC3
        and not is_explicit_downmix
    ):
        raise InvalidAudioConversionError(
            "Refusing lossy AC3 to EAC3 conversion (no quality benefit)"
        )


@dataclass(frozen=True, slots=True)
class DiskSpaceCheckResult:
    """Computed disk space requirement vs. actual available bytes."""

    free_bytes: int
    required_bytes: int
    input_size_bytes: int
    estimated_audio_bytes: int


def estimate_audio_size_bytes(
    duration_us: int,
    bitrate: str | None,
    *,
    target_codec: str = AppConstants.TARGET_CODEC_EAC3,
    channels: int | None = None,
) -> int:
    """Estimate encoded audio stream size in bytes from duration, codec, and bitrate/channels."""
    if target_codec in AppConstants.LOSSLESS_TARGET_CODECS:
        if duration_us <= 0:
            return AppConstants.UNKNOWN_DURATION_AUDIO_ESTIMATE_BYTES
        effective_channels = max(
            AppConstants.MIN_AUDIO_CHANNELS,
            min(
                channels or AppConstants.CHANNELS_71,
                AppConstants.MAX_AUDIO_CHANNELS,
            ),
        )
        duration_sec = duration_us / AppConstants.US_PER_SEC
        return int(
            duration_sec
            * effective_channels
            * AppConstants.FLAC_ESTIMATED_SAMPLE_RATE
            * AppConstants.FLAC_ESTIMATED_BYTES_PER_SAMPLE
        )

    bps = AppConstants.BITRATE_BPS_MAP.get(bitrate or "")
    if bps is None:
        raise ValueError(f"Unsupported audio bitrate: {bitrate!r}")

    if duration_us <= 0:
        return AppConstants.UNKNOWN_DURATION_AUDIO_ESTIMATE_BYTES

    duration_sec = duration_us / AppConstants.US_PER_SEC
    return int((duration_sec * bps) / AppConstants.BITS_PER_BYTE)


def calculate_required_disk_bytes(
    *,
    input_size_bytes: int,
    audio_conversion_enabled: bool,
    duration_us: int,
    bitrate: str | None,
    target_codec: str = AppConstants.TARGET_CODEC_EAC3,
    channels: int | None = None,
) -> tuple[int, int]:
    """Return `(required_bytes, estimated_audio_bytes)` for safe same-dir processing.

    Formula: `1.1 * input_size + estimated_audio_bytes (when enabled) + 100 MiB`.
    """
    if input_size_bytes < 0:
        raise ValueError(f"input_size_bytes must be non-negative, got {input_size_bytes}")

    estimated_audio = (
        estimate_audio_size_bytes(
            duration_us,
            bitrate,
            target_codec=target_codec,
            channels=channels,
        )
        if audio_conversion_enabled
        else 0
    )
    required = (
        int(input_size_bytes * AppConstants.DISK_SPACE_SIZE_MULTIPLIER)
        + estimated_audio
        + AppConstants.DISK_SPACE_BUFFER_BYTES
    )
    return required, estimated_audio


def _nearest_existing_dir(directory: Path) -> Path:
    """Walk upward until an existing directory is found for disk usage checks."""
    current = directory
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def check_disk_space(
    source_path: Path,
    *,
    audio_conversion_enabled: bool,
    duration_us: int,
    bitrate: str | None,
    target_codec: str = AppConstants.TARGET_CODEC_EAC3,
    channels: int | None = None,
    target_dir: Path | None = None,
) -> DiskSpaceCheckResult:
    """Verify free disk space in the target directory meets the safety threshold."""
    if not source_path.is_file():
        raise PreflightError(f"Source file does not exist: {source_path}")

    input_size = source_path.stat().st_size
    required_bytes, estimated_audio = calculate_required_disk_bytes(
        input_size_bytes=input_size,
        audio_conversion_enabled=audio_conversion_enabled,
        duration_us=duration_us,
        bitrate=bitrate,
        target_codec=target_codec,
        channels=channels,
    )
    probe_dir = _nearest_existing_dir(target_dir or source_path.parent)
    free_bytes = shutil.disk_usage(probe_dir).free

    if free_bytes < required_bytes:
        free_mib = free_bytes / AppConstants.BYTES_PER_MIB
        req_mib = required_bytes / AppConstants.BYTES_PER_MIB
        raise InsufficientDiskSpaceError(
            f"Insufficient disk space in {probe_dir}: "
            f"{free_mib:.1f} MiB free, {req_mib:.1f} MiB required"
        )

    return DiskSpaceCheckResult(
        free_bytes=free_bytes,
        required_bytes=required_bytes,
        input_size_bytes=input_size,
        estimated_audio_bytes=estimated_audio,
    )


def run_preflight_checks(
    source_path: Path,
    config: JobConfig,
    *,
    duration_us: int = 0,
    storage_root: Path | None = None,
) -> DiskSpaceCheckResult:
    """Execute all pre-flight guards before launching Stage 1 or Stage 2."""
    if not source_path.is_file():
        raise PreflightError(f"Source file does not exist: {source_path}")

    if config.audio_conversion.enabled:
        validate_audio_conversion_pair(
            config.audio_conversion.source_codec,
            config.audio_conversion.target_codec,
            source_channels=config.audio_conversion.source_channels,
            target_channels=config.audio_conversion.target_channels,
        )

    target_dir: Path | None = None
    if config.output_mode == AppConstants.OUTPUT_MODE_SAVE_AS:
        if storage_root is None:
            raise PreflightError("storage_root is required for save_as output mode")
        assert config.output_path is not None
        try:
            resolved_target = ensure_save_as_target_available(
                config.output_path,
                storage_root,
                source_path=source_path,
            )
        except FileExistsError as exc:
            raise TargetFileCollisionError(str(exc)) from exc
        target_dir = resolved_target.parent
    elif (
        source_path.suffix.lower() == AppConstants.EXT_MP4
        and config.output_mode == AppConstants.OUTPUT_MODE_REPLACE
    ):
        try:
            ensure_mp4_replace_target_available(source_path)
        except FileExistsError as exc:
            raise TargetFileCollisionError(str(exc)) from exc

    return check_disk_space(
        source_path,
        audio_conversion_enabled=config.audio_conversion.enabled,
        duration_us=duration_us,
        bitrate=config.audio_conversion.bitrate,
        target_codec=config.audio_conversion.target_codec,
        channels=(
            config.audio_conversion.target_channels
            or config.audio_conversion.source_channels
        ),
        target_dir=target_dir,
    )
