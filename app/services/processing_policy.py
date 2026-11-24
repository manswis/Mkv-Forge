"""Centralized runtime policy for FFmpeg/mkvmerge CPU/I/O priority and Stage 1 scratch paths."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import AppConstants, Settings
from app.models.schemas import UserSettings


@dataclass(frozen=True, slots=True)
class ProcessingRuntimePolicy:
    """Resolved per-job execution limits for Stage 1 FFmpeg and Stage 2 mkvmerge."""

    priority_mode: str = AppConstants.DEFAULT_PROCESSING_PRIORITY_MODE
    ffmpeg_threads: int | None = None
    nice_increment: int = 0
    use_ionice_idle: bool = False
    scratch_mode: str = AppConstants.DEFAULT_STAGE1_SCRATCH_MODE
    scratch_dir: Path | None = None


def detect_cpu_count(override: int | None = None) -> int:
    """Return usable logical CPU count clamped to at least 1."""
    raw = override if override is not None else (os.cpu_count() or AppConstants.MIN_FFMPEG_THREADS)
    return max(AppConstants.MIN_FFMPEG_THREADS, int(raw))


def resolve_ffmpeg_threads(
    priority_mode: str,
    *,
    cpu_count: int | None = None,
) -> int | None:
    """Return FFmpeg `-threads` cap for `priority_mode`, or `None` for Auto (full speed)."""
    cpus = detect_cpu_count(cpu_count)
    if priority_mode == AppConstants.PROCESSING_PRIORITY_HALF_CORES:
        return max(AppConstants.MIN_FFMPEG_THREADS, cpus // 2)
    if priority_mode == AppConstants.PROCESSING_PRIORITY_LOW:
        half = max(AppConstants.MIN_FFMPEG_THREADS, cpus // 2)
        return max(
            AppConstants.MIN_FFMPEG_THREADS,
            min(AppConstants.LOW_PRIORITY_MAX_THREADS, half),
        )
    return None


def resolve_nice_increment(priority_mode: str) -> int:
    """Return POSIX `os.nice()` increment for `priority_mode`."""
    if priority_mode == AppConstants.PROCESSING_PRIORITY_HALF_CORES:
        return AppConstants.HALF_CORES_NICE_INCREMENT
    if priority_mode == AppConstants.PROCESSING_PRIORITY_LOW:
        return AppConstants.LOW_PRIORITY_NICE_INCREMENT
    return 0


def is_ionice_available() -> bool:
    """Return True when the `ionice` binary is present on PATH (Linux containers)."""
    return shutil.which(AppConstants.IONICE_TOOL) is not None


def get_tmp_scratch_dir() -> Path:
    """Return the container temporary scratch directory path."""
    return Path(tempfile.gettempdir()) / AppConstants.TMP_SCRATCH_DIR_NAME


def resolve_stage1_scratch_dir(
    scratch_mode: str,
    *,
    settings: Settings,
) -> Path | None:
    """Resolve Stage 1 FFmpeg scratch directory, or `None` for source-folder mode."""
    if scratch_mode == AppConstants.STAGE1_SCRATCH_TMP:
        return get_tmp_scratch_dir()
    if scratch_mode == AppConstants.STAGE1_SCRATCH_DATA:
        return settings.data_scratch_dir
    if scratch_mode == AppConstants.STAGE1_SCRATCH_CUSTOM and settings.scratch_dir is not None:
        return settings.scratch_dir
    return None


def list_managed_scratch_dirs(settings: Settings) -> list[Path]:
    """Return all unique scratch directories MKV Forge may have written temp files into."""
    candidates: list[Path] = [
        get_tmp_scratch_dir(),
        settings.data_scratch_dir,
    ]
    if settings.scratch_dir is not None:
        candidates.append(settings.scratch_dir)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def resolve_processing_policy(
    user_settings: UserSettings,
    *,
    settings: Settings,
    cpu_count: int | None = None,
    ionice_available: bool | None = None,
) -> ProcessingRuntimePolicy:
    """Build a validated `ProcessingRuntimePolicy` from persisted system settings."""
    priority_mode = (
        user_settings.processing_priority_mode
        if user_settings.processing_priority_mode in AppConstants.ALLOWED_PROCESSING_PRIORITY_MODES
        else AppConstants.DEFAULT_PROCESSING_PRIORITY_MODE
    )
    scratch_mode = (
        user_settings.stage1_scratch_mode
        if user_settings.stage1_scratch_mode in AppConstants.ALLOWED_STAGE1_SCRATCH_MODES
        else AppConstants.DEFAULT_STAGE1_SCRATCH_MODE
    )
    has_ionice = is_ionice_available() if ionice_available is None else bool(ionice_available)
    scratch_dir = resolve_stage1_scratch_dir(scratch_mode, settings=settings)
    effective_scratch_mode = (
        AppConstants.STAGE1_SCRATCH_SOURCE if scratch_dir is None else scratch_mode
    )
    return ProcessingRuntimePolicy(
        priority_mode=priority_mode,
        ffmpeg_threads=resolve_ffmpeg_threads(priority_mode, cpu_count=cpu_count),
        nice_increment=resolve_nice_increment(priority_mode),
        use_ionice_idle=(
            priority_mode == AppConstants.PROCESSING_PRIORITY_LOW and has_ionice
        ),
        scratch_mode=effective_scratch_mode,
        scratch_dir=scratch_dir,
    )


def wrap_command_with_io_priority(
    argv: list[str],
    *,
    use_ionice_idle: bool,
) -> list[str]:
    """Prefix argv with `ionice -t -c 3` when idle I/O priority is enabled.

    Passes `-t` (`--ignore`) so restricted containers without `CAP_SYS_NICE`
    still execute `argv` cleanly if `ioprio_set` returns `EPERM`.
    """
    if not use_ionice_idle:
        return list(argv)
    return [AppConstants.IONICE_TOOL, *AppConstants.IONICE_IDLE_CLASS_ARGS, *argv]


def build_nice_preexec_fn(nice_increment: int) -> Callable[[], None] | None:
    """Return a child-process `preexec_fn` applying `os.nice(nice_increment)` safely."""
    if nice_increment <= 0 or not hasattr(os, "nice"):
        return None

    def _apply_nice() -> None:
        try:
            os.nice(nice_increment)
        except OSError:
            pass

    return _apply_nice
