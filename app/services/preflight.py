"""Pre-flight disk space and target collision guards before media processing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConstants
from app.models.schemas import JobConfig
from app.services.ffmpeg_runner import canonicalize_audio_codec
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
) -> None:
    """Reject illogical same-codec re-encodes and lossy AC3 -> EAC3 upsampling."""
    normalized_target = target_codec.strip().lower()
    if normalized_target not in AppConstants.ALLOWED_TARGET_CODECS:
        raise InvalidAudioConversionError(
            f"Unsupported target audio codec: {target_codec!r}"
        )

    canonical_source = canonicalize_audio_codec(source_codec)
    if canonical_source is None:
        return

    if canonical_source == normalized_target:
        raise InvalidAudioConversionError(
            f"Source track is already {canonical_source.upper()}; "
            f"refusing pointless same-codec conversion to {normalized_target.upper()}"
        )

    if (
        canonical_source == AppConstants.TARGET_CODEC_AC3
        and normalized_target == AppConstants.TARGET_CODEC_EAC3
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


def estimate_audio_size_bytes(duration_us: int, bitrate: str) -> int:
    """Estimate encoded audio stream size in bytes from duration and bitrate."""
    bps = AppConstants.BITRATE_BPS_MAP.get(bitrate)
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
    bitrate: str,
) -> tuple[int, int]:
    """Return `(required_bytes, estimated_audio_bytes)` for safe same-dir processing.

    Formula: `1.1 * input_size + estimated_audio_bytes (when enabled) + 100 MiB`.
    """
    if input_size_bytes < 0:
        raise ValueError(f"input_size_bytes must be non-negative, got {input_size_bytes}")

    estimated_audio = (
        estimate_audio_size_bytes(duration_us, bitrate)
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
    bitrate: str,
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
        target_dir=target_dir,
    )
