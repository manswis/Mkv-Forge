"""Chunked, cancellable, progress-reporting file copy runner for JobWorker."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.config import AppConstants
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.services.file_ops_service import (
    FileOpConflictError,
    FileOpError,
    FileOpNotFoundError,
    FileOpPermissionError,
    _raise_if_permission_os_error,
    _safe_move_file_no_overwrite,
)
from app.services.folder_service import normalize_storage_folder_path
from app.services.folder_sync import FolderReconcileCache
from app.services.library_events import LibraryEventBroker
from app.services.mkv_runner import (
    JobCancelledError,
    PipelineExecutionResult,
    StageCallback,
)
from app.services.preflight import InsufficientDiskSpaceError
from app.services.scanner import LibraryScanner
from app.utils.paths import (
    extract_folder,
    safe_resolve_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)

_COPY_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\s+\(\d+\)$")

CopyProgressCallback = Callable[[int, str | None, str | None], None]
CopyLogCallback = Callable[[str], None]


def format_transfer_speed(bytes_per_sec: float) -> str | None:
    """Format raw bytes/sec throughput into a concise human-readable rate string."""
    if bytes_per_sec <= 0:
        return None
    if bytes_per_sec >= AppConstants.FILE_COPY_BYTES_PER_GB:
        return f"{bytes_per_sec / AppConstants.FILE_COPY_BYTES_PER_GB:.2f} GB/s"
    if bytes_per_sec >= AppConstants.FILE_COPY_BYTES_PER_MB:
        return f"{bytes_per_sec / AppConstants.FILE_COPY_BYTES_PER_MB:.1f} MB/s"
    if bytes_per_sec >= AppConstants.FILE_COPY_BYTES_PER_KB:
        return f"{bytes_per_sec / AppConstants.FILE_COPY_BYTES_PER_KB:.0f} KB/s"
    return f"{int(round(bytes_per_sec))} B/s"


def format_eta_seconds(seconds: float | None) -> str | None:
    """Format remaining copy seconds into a compact ETA string (e.g. `1m 42s`)."""
    if seconds is None or seconds < 0:
        return None
    eta_sec = max(0, int(round(seconds)))
    if eta_sec >= AppConstants.SEC_PER_HOUR:
        hours, rem = divmod(eta_sec, AppConstants.SEC_PER_HOUR)
        minutes = rem // AppConstants.SEC_PER_MIN
        return f"{hours}h {minutes}m"
    if eta_sec >= AppConstants.SEC_PER_MIN:
        minutes, secs = divmod(eta_sec, AppConstants.SEC_PER_MIN)
        return f"{minutes}m {secs}s"
    return f"{eta_sec}s"


def resolve_copy_destination_dir(
    destination_folder: str,
    storage_dir: Path,
) -> tuple[Path, str]:
    """Resolve and validate `destination_folder` inside `storage_dir`."""
    normalized = normalize_storage_folder_path(destination_folder)
    root = storage_dir.resolve(strict=False)
    if normalized == "/":
        dst_dir = root
    else:
        dst_dir = safe_resolve_path(normalized.lstrip("/"), root, allow_root=True)
    if not dst_dir.is_dir():
        raise FileOpNotFoundError(f"Destination folder does not exist: {normalized}")
    return dst_dir, normalized


def resolve_copy_destination_path(
    src_path: Path,
    dst_dir: Path,
    storage_dir: Path,
) -> Path:
    """Resolve a non-colliding copy target path inside `dst_dir`.

    Preserves `src_path.name` when copying to another folder without collision;
    allocates `<base> (1)<ext>`, `<base> (2)<ext>`, ... when copying into the
    same folder or when the filename already exists in `dst_dir`.
    """
    direct_candidate = safe_resolve_path(dst_dir / src_path.name, storage_dir)
    if direct_candidate != src_path and not direct_candidate.exists():
        return direct_candidate

    stem = src_path.stem
    match = _COPY_SUFFIX_RE.match(stem)
    base_stem = (
        match.group("base").strip()
        if match and match.group("base").strip()
        else stem
    )
    ext = src_path.suffix

    for index in range(1, AppConstants.MAX_DUPLICATE_COLLISIONS + 1):
        candidate_name = f"{base_stem} ({index}){ext}"
        candidate_path = safe_resolve_path(dst_dir / candidate_name, storage_dir)
        if not candidate_path.exists():
            return candidate_path

    raise FileOpConflictError(
        f"Unable to allocate copy filename for {src_path.name}"
    )


def ensure_copy_destination_writable(dst_dir: Path) -> None:
    """Verify `dst_dir` accepts writes, raising HTTP 403 `FileOpPermissionError` on `:ro`."""
    probe = dst_dir / f"{AppConstants.FILE_COPY_PROBE_PREFIX}{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(probe, "wb"):
            pass
    except OSError as exc:
        _raise_if_permission_os_error(exc, f"copy into {dst_dir.name or '/'}")
        raise FileOpError(
            f"Destination folder is not writable: {exc.strerror or exc}"
        ) from exc
    finally:
        if probe.exists():
            try:
                probe.unlink()
            except OSError:
                pass


def check_copy_disk_space(
    required_bytes: int,
    dst_dir: Path,
    *,
    margin_bytes: int = AppConstants.FILE_COPY_DISK_MARGIN_BYTES,
) -> int:
    """Ensure `dst_dir` filesystem has `required_bytes + margin_bytes` available."""
    if required_bytes < 0:
        raise ValueError(f"required_bytes must be non-negative, got {required_bytes}")
    try:
        free_bytes = int(shutil.disk_usage(dst_dir).free)
    except OSError as exc:
        raise FileOpError(
            f"Unable to inspect free disk space in destination folder: {exc}"
        ) from exc
    needed = required_bytes + max(0, int(margin_bytes))
    if free_bytes < needed:
        free_mb = free_bytes / AppConstants.FILE_COPY_BYTES_PER_MB
        need_mb = needed / AppConstants.FILE_COPY_BYTES_PER_MB
        raise InsufficientDiskSpaceError(
            f"Insufficient disk space in destination folder: "
            f"{free_mb:.1f} MiB free, {need_mb:.1f} MiB required"
        )
    return free_bytes


def _copy_file_chunked_sync(
    src_path: Path,
    tmp_path: Path,
    *,
    total_bytes: int,
    chunk_bytes: int,
    emit_interval_sec: float,
    is_cancelled: Callable[[], bool],
    report_progress: Callable[[int, str | None, str | None], None],
) -> None:
    """Stream `src_path` -> `tmp_path` in bounded chunks with EMA throughput & cancellation."""
    effective_chunk = max(AppConstants.FILE_COPY_MIN_CHUNK_BYTES, int(chunk_bytes))
    copied = 0
    started_at = time.monotonic()
    last_tick_at = started_at
    last_emit_at = started_at
    bytes_since_tick = 0
    ema_speed_bps: float | None = None

    with open(src_path, "rb") as src_fp, open(tmp_path, "wb") as dst_fp:
        if total_bytes == 0:
            if is_cancelled():
                raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)
            report_progress(100, None, "0s")
            return

        while True:
            if is_cancelled():
                raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)

            buf = src_fp.read(effective_chunk)
            if not buf:
                break

            dst_fp.write(buf)
            chunk_len = len(buf)
            copied += chunk_len
            bytes_since_tick += chunk_len

            now = time.monotonic()
            tick_elapsed = now - last_tick_at
            if tick_elapsed > 0:
                inst_bps = bytes_since_tick / tick_elapsed
                if ema_speed_bps is None:
                    ema_speed_bps = inst_bps
                else:
                    alpha = AppConstants.FILE_COPY_SPEED_EMA_ALPHA
                    ema_speed_bps = alpha * inst_bps + (1.0 - alpha) * ema_speed_bps
                last_tick_at = now
                bytes_since_tick = 0

            if copied >= total_bytes or (now - last_emit_at) >= emit_interval_sec:
                pct = min(99, int((copied * 100) // total_bytes))
                if copied >= total_bytes:
                    pct = 100
                remaining = max(0, total_bytes - copied)
                eta_sec = (
                    (remaining / ema_speed_bps)
                    if ema_speed_bps and ema_speed_bps > 0 and remaining > 0
                    else (0.0 if remaining == 0 else None)
                )
                report_progress(
                    pct,
                    format_transfer_speed(ema_speed_bps or 0.0),
                    format_eta_seconds(eta_sec),
                )
                last_emit_at = now

        dst_fp.flush()

    if is_cancelled():
        raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)


async def execute_file_copy(
    source_path: Path,
    *,
    destination_folder: str,
    storage_root: Path,
    media_repo: MediaFileRepository,
    media_row: dict[str, Any] | None = None,
    scanner: LibraryScanner | None = None,
    folder_cache: FolderReconcileCache | None = None,
    library_broker: LibraryEventBroker | None = None,
    on_stage: StageCallback | None = None,
    on_progress: CopyProgressCallback | None = None,
    on_log: CopyLogCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    chunk_bytes: int = AppConstants.FILE_COPY_CHUNK_BYTES,
    emit_interval_sec: float = AppConstants.FILE_COPY_PROGRESS_EMIT_INTERVAL_SEC,
    disk_margin_bytes: int = AppConstants.FILE_COPY_DISK_MARGIN_BYTES,
) -> PipelineExecutionResult:
    """Copy `source_path` into `destination_folder` with live progress, cancel, and DB indexing."""
    cancel_check = is_cancelled or (lambda: False)
    if cancel_check():
        raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)

    if not source_path.is_file():
        raise FileOpNotFoundError(f"Source file not found on disk: {source_path.name}")

    dst_dir, normalized_dst = await asyncio.to_thread(
        resolve_copy_destination_dir, destination_folder, storage_root
    )
    total_bytes = int(source_path.stat().st_size)
    await asyncio.to_thread(ensure_copy_destination_writable, dst_dir)
    await asyncio.to_thread(
        check_copy_disk_space,
        total_bytes,
        dst_dir,
        margin_bytes=disk_margin_bytes,
    )

    dst_path = await asyncio.to_thread(
        resolve_copy_destination_path, source_path, dst_dir, storage_root
    )
    tmp_path = dst_dir / (
        f".{dst_path.name}{AppConstants.TEMP_MARKER}"
        f"{AppConstants.FILE_COPY_TEMP_PREFIX}{uuid.uuid4().hex[:8]}.tmp"
    )

    stage_label = f"Copying to {normalized_dst}"
    if on_stage is not None:
        on_stage(AppConstants.JOB_STAGE_COPYING, stage_label)
    if on_log is not None:
        on_log(f"Copying {source_path.name} -> {normalized_dst} ({total_bytes} bytes)")

    loop = asyncio.get_running_loop()

    def _thread_report_progress(
        pct: int, speed_str: str | None, eta_str: str | None
    ) -> None:
        if on_progress is not None:
            loop.call_soon_threadsafe(on_progress, pct, speed_str, eta_str)

    try:
        await asyncio.to_thread(
            _copy_file_chunked_sync,
            source_path,
            tmp_path,
            total_bytes=total_bytes,
            chunk_bytes=chunk_bytes,
            emit_interval_sec=emit_interval_sec,
            is_cancelled=cancel_check,
            report_progress=_thread_report_progress,
        )

        # Preserve timestamps best-effort (ignore SMB/CIFS permission quirks)
        try:
            await asyncio.to_thread(shutil.copystat, str(source_path), str(tmp_path))
        except OSError:
            pass

        # Re-check destination collision right before atomic finalize so a long
        # copy never fails if another file with the same name appeared mid-transfer.
        def _finalize_atomic() -> Path:
            final_dst = dst_path
            if final_dst.exists():
                final_dst = resolve_copy_destination_path(
                    source_path, dst_dir, storage_root
                )
            _safe_move_file_no_overwrite(
                tmp_path,
                final_dst,
                action=f"copy {source_path.name}",
            )
            return final_dst

        final_dst = await asyncio.to_thread(_finalize_atomic)
    except JobCancelledError:
        if on_log is not None:
            on_log(f"Copy cancelled for {source_path.name}; cleaning up partial file")
        raise
    except OSError as exc:
        _raise_if_permission_os_error(exc, f"copy {source_path.name}")
        raise FileOpError(f"Failed to copy {source_path.name}: {exc}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    new_rel = to_relative_storage_path(final_dst, storage_root)
    new_folder = extract_folder(new_rel)

    def _index_copied_file() -> int | None:
        if media_row is not None:
            stat = final_dst.stat()
            return media_repo.upsert(
                MediaFileUpsert(
                    filepath=new_rel,
                    folder=new_folder,
                    filename=final_dst.name,
                    size_bytes=int(stat.st_size),
                    mtime=float(stat.st_mtime),
                    duration_us=int(media_row.get("duration_us") or 0),
                    resolution=media_row.get("resolution"),
                    video_codec=media_row.get("video_codec"),
                    video_dynamic_range=media_row.get("video_dynamic_range"),
                    audio_summary_json=str(media_row.get("audio_summary_json") or "[]"),
                    subtitle_count=int(media_row.get("subtitle_count") or 0),
                    container_title=str(media_row.get("container_title") or ""),
                    has_global_tags=bool(media_row.get("has_global_tags")),
                    has_aac_51=bool(media_row.get("has_aac_51")),
                    has_ac3_eac3=bool(media_row.get("has_ac3_eac3")),
                    raw_json=str(media_row.get("raw_json") or "{}"),
                    scanned_at=utc_now_iso(),
                )
            )
        return None

    new_file_id = await asyncio.to_thread(_index_copied_file)
    if new_file_id is None and scanner is not None:
        await scanner.scan_single_file(final_dst)
        indexed = await asyncio.to_thread(media_repo.get_by_filepath, new_rel)
        if indexed is not None:
            new_file_id = int(indexed["id"])

    if folder_cache is not None:
        folder_cache.invalidate_folder(normalized_dst, include_parent=True)
        folder_cache.invalidate_for_filepath(new_rel)

    if library_broker is not None:
        library_broker.record_change(
            action="added",
            filepath=new_rel,
            file_id=new_file_id,
        )

    if on_log is not None:
        on_log(f"Copied {source_path.name} -> /{new_rel}")

    return PipelineExecutionResult(
        output_path=final_dst,
        output_rel_filepath=new_rel,
        mkvmerge_exit_code=AppConstants.EXIT_CODE_SUCCESS,
        warnings=[],
    )
