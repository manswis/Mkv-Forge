"""Centralized runtime policy for FFmpeg/mkvmerge CPU/I/O priority and Stage 1 scratch paths."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from app.config import AppConstants, Settings
from app.models.schemas import ScratchLocationInfo, UserSettings
from app.utils.paths import PathSafetyError, safe_resolve_path, to_relative_storage_path

ScratchLocationId = Literal["source", "tmp", "data", "storage_folder", "custom"]


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


def normalize_storage_scratch_folder(
    raw_folder: str | None,
    *,
    storage_root: Path,
) -> tuple[str, Path]:
    """Validate and normalize a `/storage` subfolder chosen for Stage 1 scratch.

    Rejects empty strings, the library root (`/` or `.`), null bytes, path
    traversal (`..`), and symlinks escaping `storage_root`.
    Returns `(relative_posix_folder, resolved_abs_path)`.
    """
    if raw_folder is None:
        raise ValueError("Choose a subfolder inside /storage for temporary audio files.")

    cleaned = raw_folder.strip().replace("\\", "/")
    if "\x00" in cleaned:
        raise ValueError("Invalid folder path.")

    stripped = cleaned.strip("/")
    if not stripped or stripped == ".":
        raise ValueError(
            "Select a dedicated subfolder inside /storage (not the library root)."
        )

    resolved_root = storage_root.resolve(strict=False)
    try:
        resolved = safe_resolve_path(stripped, resolved_root)
    except PathSafetyError as exc:
        raise ValueError(
            "Selected temp folder must stay inside /storage."
        ) from exc

    if resolved == resolved_root:
        raise ValueError(
            "Select a dedicated subfolder inside /storage (not the library root)."
        )

    try:
        rel_posix = to_relative_storage_path(resolved, resolved_root)
    except PathSafetyError as exc:
        raise ValueError(
            "Selected temp folder must stay inside /storage."
        ) from exc

    if not rel_posix or rel_posix == ".":
        raise ValueError(
            "Select a dedicated subfolder inside /storage (not the library root)."
        )
    return rel_posix, resolved


def _unescape_proc_mount_field(raw: str) -> str:
    """Decode Linux `/proc/mounts` octal escape sequences (spaces, tabs, newlines, backslashes)."""
    return (
        raw.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def is_path_on_tmpfs(
    path: Path,
    *,
    proc_mounts_path: Path | None = None,
) -> bool:
    """Return True when `path` resolves onto a Linux RAM-backed (`tmpfs`/`ramfs`) mount."""
    mounts_file = (
        proc_mounts_path
        if proc_mounts_path is not None
        else Path(AppConstants.PROC_MOUNTS_PATH)
    )
    if not mounts_file.is_file():
        return False

    try:
        target = str(path.resolve(strict=False))
        lines = mounts_file.read_text(
            encoding=AppConstants.UTF8_ENCODING, errors="replace"
        ).splitlines()
    except OSError:
        return False

    best_mount_len = -1
    best_fs_type = ""
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = _unescape_proc_mount_field(parts[1])
        fs_type = parts[2].lower()
        if mount_point == "/":
            matched = target.startswith("/")
        else:
            normalized_mount = mount_point.rstrip("/")
            matched = target == normalized_mount or target.startswith(
                f"{normalized_mount}/"
            )
        if matched and len(mount_point) >= best_mount_len:
            best_mount_len = len(mount_point)
            best_fs_type = fs_type

    return best_fs_type in AppConstants.TMPFS_FS_TYPES


def _nearest_existing_directory(target: Path) -> Path | None:
    """Walk up from `target` to find the nearest existing directory, or `None` if blocked by a file."""
    current = target
    while True:
        if current.exists():
            return current if current.is_dir() else None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def check_directory_writable(target: Path) -> tuple[bool, bool]:
    """Return `(exists_as_dir, writable)` using a safe non-destructive probe file."""
    try:
        exists_as_dir = target.is_dir()
        if not exists_as_dir and target.exists():
            return False, False
    except OSError:
        return False, False

    probe_dir = target if exists_as_dir else _nearest_existing_directory(target)
    if probe_dir is None:
        return exists_as_dir, False

    try:
        with tempfile.NamedTemporaryFile(
            dir=probe_dir,
            prefix=AppConstants.SCRATCH_WRITE_PROBE_PREFIX,
            delete=True,
        ):
            pass
        return exists_as_dir, True
    except OSError:
        return exists_as_dir, False


def inspect_scratch_location(
    location_id: ScratchLocationId,
    path: Path | None,
    *,
    storage_root: Path,
    proc_mounts_path: Path | None = None,
) -> ScratchLocationInfo:
    """Collect read-only existence, writability, free space, and mount telemetry for one scratch option."""
    if path is None:
        return ScratchLocationInfo(
            id=location_id,
            path=None,
            configured=False,
            exists=False,
            writable=False,
            free_bytes=None,
            is_tmpfs=False,
            same_device_as_storage=None,
        )

    exists_as_dir, writable = check_directory_writable(path)
    probe_dir = path if exists_as_dir else _nearest_existing_directory(path)

    free_bytes: int | None = None
    same_device: bool | None = None
    if probe_dir is not None:
        try:
            free_bytes = int(shutil.disk_usage(probe_dir).free)
        except OSError:
            free_bytes = None

        storage_probe = _nearest_existing_directory(storage_root)
        if storage_probe is not None:
            try:
                same_device = probe_dir.stat().st_dev == storage_probe.stat().st_dev
            except OSError:
                same_device = None

    return ScratchLocationInfo(
        id=location_id,
        path=str(path),
        configured=True,
        exists=exists_as_dir,
        writable=writable,
        free_bytes=free_bytes,
        is_tmpfs=is_path_on_tmpfs(
            probe_dir if probe_dir is not None else path,
            proc_mounts_path=proc_mounts_path,
        ),
        same_device_as_storage=same_device,
    )


def get_excluded_storage_scratch_rel_prefix(
    user_settings: UserSettings,
    *,
    storage_root: Path,
) -> str | None:
    """Return normalized `/storage` relative folder to exclude from library scans/watcher, if active."""
    if user_settings.stage1_scratch_mode != AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER:
        return None
    if not user_settings.stage1_scratch_storage_folder:
        return None
    try:
        rel_posix, _ = normalize_storage_scratch_folder(
            user_settings.stage1_scratch_storage_folder,
            storage_root=storage_root,
        )
        return rel_posix
    except ValueError:
        return None


def is_rel_path_within_excluded_prefix(
    rel_path: str,
    excluded_prefix: str | None,
) -> bool:
    """Return True iff `rel_path` equals or resides inside `excluded_prefix`."""
    if not excluded_prefix:
        return False
    normalized = rel_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    return normalized == excluded_prefix or normalized.startswith(f"{excluded_prefix}/")


def resolve_stage1_scratch_dir(
    scratch_mode: str,
    *,
    settings: Settings,
    storage_folder: str | None = None,
) -> Path | None:
    """Resolve Stage 1 FFmpeg scratch directory, or `None` for source-folder mode."""
    if scratch_mode == AppConstants.STAGE1_SCRATCH_TMP:
        return get_tmp_scratch_dir()
    if scratch_mode == AppConstants.STAGE1_SCRATCH_DATA:
        return settings.data_scratch_dir
    if scratch_mode == AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER and storage_folder:
        try:
            _, resolved = normalize_storage_scratch_folder(
                storage_folder,
                storage_root=settings.storage_dir,
            )
        except ValueError:
            return None
        if resolved.exists() and not resolved.is_dir():
            return None
        return resolved
    if scratch_mode == AppConstants.STAGE1_SCRATCH_CUSTOM and settings.scratch_dir is not None:
        return settings.scratch_dir
    return None


def list_managed_scratch_dirs(
    settings: Settings,
    *,
    storage_folder: str | None = None,
) -> list[Path]:
    """Return all unique scratch directories MKV Forge may have written temp files into."""
    candidates: list[Path] = [
        get_tmp_scratch_dir(),
        settings.data_scratch_dir,
    ]
    if storage_folder:
        try:
            _, resolved_storage_scratch = normalize_storage_scratch_folder(
                storage_folder,
                storage_root=settings.storage_dir,
            )
            candidates.append(resolved_storage_scratch)
        except ValueError:
            pass
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
    scratch_dir = resolve_stage1_scratch_dir(
        scratch_mode,
        settings=settings,
        storage_folder=user_settings.stage1_scratch_storage_folder,
    )
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
