"""Incremental media library scanner and single-file mkvmerge prober."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import AppConstants, Settings
from app.db.repositories import (
    AppStateRepository,
    MediaFileRepository,
    MediaFileUpsert,
    utc_now_iso,
)
from app.models.schemas import ScanProgressPayload
from app.services.folder_sync import FolderReconcileCache
from app.services.probe_parser import parse_mkvmerge_json
from app.services.processing_policy import (
    build_nice_preexec_fn,
    get_excluded_storage_scratch_rel_prefix,
    is_ionice_available,
    is_rel_path_within_excluded_prefix,
    wrap_command_with_io_priority,
)
from app.services.user_settings import UserSettingsService
from app.utils.paths import (
    PathSafetyError,
    extract_folder,
    is_supported_media_file,
    safe_resolve_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    """Raised when `mkvmerge -J` fails, times out, or returns invalid JSON."""


@dataclass(frozen=True, slots=True)
class DiscoveredMediaFile:
    """Filesystem entry discovered during a storage walk."""

    rel_filepath: str
    abs_path: Path
    mtime: float
    size_bytes: int


def _discover_media_files(
    storage_root: Path,
    *,
    excluded_rel_prefix: str | None = None,
) -> tuple[list[DiscoveredMediaFile], int]:
    """Recursively collect supported media files inside `storage_root`.

    Runs in a worker thread via `asyncio.to_thread` to avoid blocking the event loop.
    Automatically skips any active Stage 1 storage temp subfolder (`excluded_rel_prefix`).
    Returns `(discovered_files, stat_error_count)`.
    """
    resolved_root = storage_root.resolve(strict=False)
    discovered: list[DiscoveredMediaFile] = []
    stat_errors = 0

    for root_str, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current_dir = Path(root_str)
        if excluded_rel_prefix:
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                try:
                    sub_rel = to_relative_storage_path(
                        (current_dir / dirname).resolve(strict=False),
                        resolved_root,
                    )
                except PathSafetyError:
                    continue
                if is_rel_path_within_excluded_prefix(sub_rel, excluded_rel_prefix):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
        else:
            dirnames.sort()
        filenames.sort()

        for filename in filenames:
            candidate = current_dir / filename
            if not is_supported_media_file(candidate):
                continue

            try:
                safe_path = safe_resolve_path(candidate, resolved_root)
                rel_path = to_relative_storage_path(safe_path, resolved_root)
                if is_rel_path_within_excluded_prefix(rel_path, excluded_rel_prefix):
                    continue
                stat_result = safe_path.stat()
            except (PathSafetyError, OSError) as exc:
                logger.warning("Skipping unreadable or unsafe path %s: %s", candidate, exc)
                stat_errors += 1
                continue

            discovered.append(
                DiscoveredMediaFile(
                    rel_filepath=rel_path,
                    abs_path=safe_path,
                    mtime=float(stat_result.st_mtime),
                    size_bytes=int(stat_result.st_size),
                )
            )

    return discovered, stat_errors


_IONICE_ERROR_MARKERS: tuple[str, ...] = ("ionice:", "ioprio_set")
_ionice_probe_disabled: bool = False


def _is_ionice_wrapper_failure(stderr_bytes: bytes) -> bool:
    """Return True when stderr indicates `ionice` itself failed due to permissions/container limits."""
    stderr_lower = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").lower()
    return any(marker in stderr_lower for marker in _IONICE_ERROR_MARKERS)


def _disable_ionice_probe(reason: str) -> None:
    """Latch `ionice` probing off for this container lifetime and log WARNING once."""
    global _ionice_probe_disabled
    if not _ionice_probe_disabled:
        _ionice_probe_disabled = True
        logger.warning(
            "ionice probe wrapper unavailable (%s); disabling ionice for future probes "
            "and falling back to direct probe",
            reason,
        )
    else:
        logger.debug(
            "ionice probe wrapper unavailable (%s); falling back to direct probe",
            reason,
        )


async def _exec_probe_subprocess(
    base_argv: list[str],
    *,
    timeout_sec: float,
    low_io_priority: bool = False,
) -> tuple[int, bytes, bytes]:
    """Run a metadata probe subprocess with optional best-effort `ionice` + `nice` priority.

    When `low_io_priority` is True:
    - Prefixes `base_argv` with `ionice -t -c 3` if `ionice` is installed and not disabled.
    - Applies `os.nice(LOW_PRIORITY_NICE_INCREMENT)` via `preexec_fn` when supported.
    - Automatically falls back to running `base_argv` directly and latches `ionice` off
      for the rest of the container lifetime if `ionice` cannot be launched or fails due
      to container permission restrictions (`EPERM`/`ioprio_set`).
    """
    use_ionice = bool(
        low_io_priority and not _ionice_probe_disabled and is_ionice_available()
    )
    preexec_fn = (
        build_nice_preexec_fn(AppConstants.LOW_PRIORITY_NICE_INCREMENT)
        if low_io_priority
        else None
    )

    async def _run_once(argv: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec_fn,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        return int(proc.returncode or 0), stdout_bytes, stderr_bytes

    if use_ionice:
        wrapped_argv = wrap_command_with_io_priority(
            base_argv,
            use_ionice_idle=True,
        )
        try:
            returncode, stdout_bytes, stderr_bytes = await _run_once(wrapped_argv)
        except asyncio.TimeoutError:
            raise
        except (FileNotFoundError, PermissionError, OSError) as exc:
            _disable_ionice_probe(str(exc))
        else:
            if returncode == AppConstants.EXIT_CODE_SUCCESS or not _is_ionice_wrapper_failure(
                stderr_bytes
            ):
                return returncode, stdout_bytes, stderr_bytes
            detail = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
            _disable_ionice_probe(f"exit {returncode}: {detail}")

    return await _run_once(base_argv)


async def probe_container_tags_with_ffprobe(
    file_path: Path,
    timeout_sec: float = 3.0,
    *,
    low_io_priority: bool = False,
) -> dict[str, str]:
    """Read container-level tags via `ffprobe -show_format` when global_tags are present.

    `mkvmerge -J` reports `"global_tags": [{"num_entries": N}]` without tag key/value
    strings; `ffprobe` exposes the actual global tag strings (e.g. `TITLE`, `COMMENT`).
    Degrades silently to `{}` if `ffprobe` is unavailable or fails.
    """
    try:
        returncode, stdout_bytes, _ = await _exec_probe_subprocess(
            [
                AppConstants.TOOL_FFPROBE,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags",
                str(file_path),
            ],
            timeout_sec=timeout_sec,
            low_io_priority=low_io_priority,
        )
        if returncode != AppConstants.EXIT_CODE_SUCCESS:
            return {}
        data = json.loads(
            stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace")
        )
        raw_tags = (
            data.get("format", {}).get("tags", {})
            if isinstance(data, dict) and isinstance(data.get("format"), dict)
            else {}
        )
        if not isinstance(raw_tags, dict):
            return {}
        return {
            str(k).strip(): str(v).strip()
            for k, v in raw_tags.items()
            if str(k).strip() and v is not None and str(v).strip()
        }
    except Exception:
        return {}


async def run_mkvmerge_probe(
    file_path: Path,
    timeout_sec: float,
    *,
    low_io_priority: bool = False,
) -> tuple[dict[str, Any], str]:
    """Execute `mkvmerge -J <file_path>` with a strict timeout and return parsed JSON."""
    base_argv = [
        AppConstants.TOOL_MKVMERGE,
        AppConstants.MKVMERGE_IDENTIFY_FLAG,
        str(file_path),
    ]
    try:
        returncode, stdout_bytes, stderr_bytes = await _exec_probe_subprocess(
            base_argv,
            timeout_sec=timeout_sec,
            low_io_priority=low_io_priority,
        )
    except asyncio.TimeoutError as exc:
        raise ProbeError(
            f"mkvmerge -J timed out after {timeout_sec}s for {file_path.name}"
        ) from exc
    except FileNotFoundError as exc:
        raise ProbeError("mkvmerge executable not found in PATH") from exc
    except OSError as exc:
        raise ProbeError(f"Failed to launch mkvmerge: {exc}") from exc

    raw_text = stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace")
    if returncode != AppConstants.EXIT_CODE_SUCCESS:
        stderr_text = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
        excerpt = (stderr_text or raw_text.strip())[: AppConstants.MAX_ERROR_EXCERPT_LEN]
        raise ProbeError(
            f"mkvmerge -J failed with exit code {returncode}: {excerpt}"
        )

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Invalid JSON returned by mkvmerge -J: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ProbeError("Unexpected non-object JSON payload from mkvmerge -J")

    global_tags = parsed.get("global_tags")
    if isinstance(global_tags, list) and len(global_tags) > 0:
        container_tags = await probe_container_tags_with_ffprobe(
            file_path,
            low_io_priority=low_io_priority,
        )
        if container_tags:
            parsed["container_tags"] = container_tags
            raw_text = json.dumps(parsed, separators=(",", ":"))

    return parsed, raw_text


class LibraryScanner:
    """Coordinates incremental/full library scans, single-file probes, and SSE updates."""

    def __init__(
        self,
        settings: Settings,
        media_repo: MediaFileRepository,
        state_repo: AppStateRepository,
        folder_cache: FolderReconcileCache | None = None,
        user_settings_service: UserSettingsService | None = None,
    ) -> None:
        self._settings = settings
        self._media_repo = media_repo
        self._state_repo = state_repo
        self._folder_cache = folder_cache
        self._user_settings_service = user_settings_service
        self._task: asyncio.Task[None] | None = None
        self._progress: ScanProgressPayload = ScanProgressPayload(
            status=AppConstants.SCAN_STATUS_IDLE
        )
        self._subscribers: set[asyncio.Queue[ScanProgressPayload | None]] = set()

    def _get_excluded_scratch_prefix_sync(self) -> str | None:
        """Return active `/storage` temp folder prefix to exclude from scanning, if any."""
        if self._user_settings_service is None:
            return None
        effective = self._user_settings_service.get(user_id=None)
        return get_excluded_storage_scratch_rel_prefix(
            effective, storage_root=self._settings.storage_dir
        )

    @property
    def is_running(self) -> bool:
        """Return True if a background scan is currently active."""
        return self._task is not None and not self._task.done()

    @property
    def current_progress(self) -> ScanProgressPayload:
        """Return the latest scan progress snapshot."""
        return self._progress

    def _publish(self, payload: ScanProgressPayload) -> None:
        """Update cached progress and notify all connected SSE subscribers."""
        self._progress = payload
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def start_scan(self, *, full: bool = False) -> tuple[bool, ScanProgressPayload]:
        """Launch a background scan if none is active.

        Returns `(started, current_progress)`.
        """
        if self.is_running:
            return False, self._progress

        initial = ScanProgressPayload(
            scanned=0,
            total=0,
            skipped=0,
            errors=0,
            current_file=None,
            status=AppConstants.SCAN_STATUS_RUNNING,
        )
        self._publish(initial)
        self._task = asyncio.create_task(self._run_scan(full=full))
        return True, initial

    async def wait_for_scan(self) -> None:
        """Await the currently running scan task if any (useful in tests/shutdown)."""
        if self._task is not None:
            await self._task

    async def shutdown(self) -> None:
        """Cancel any active scan and signal SSE subscribers to close."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[ScanProgressPayload]:
        """Subscribe to real-time scan progress snapshots."""
        queue: asyncio.Queue[ScanProgressPayload | None] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        try:
            yield self._progress
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._subscribers.discard(queue)

    async def probe_single_file(
        self,
        rel_filepath: str,
        *,
        low_io_priority: bool = False,
    ) -> dict[str, Any] | None:
        """Re-probe one media file on disk and refresh its SQLite cache entry.

        Returns the refreshed DB row dict, or `None` if the file no longer exists on disk.
        """
        abs_path = await asyncio.to_thread(
            safe_resolve_path, rel_filepath, self._settings.storage_dir
        )
        normalized_rel = to_relative_storage_path(abs_path, self._settings.storage_dir)

        excluded_prefix = await asyncio.to_thread(self._get_excluded_scratch_prefix_sync)
        if is_rel_path_within_excluded_prefix(normalized_rel, excluded_prefix):
            await asyncio.to_thread(
                self._media_repo.delete_by_filepaths, [normalized_rel]
            )
            if self._folder_cache is not None:
                self._folder_cache.invalidate_for_filepath(normalized_rel)
            return None

        if not await asyncio.to_thread(abs_path.is_file):
            await asyncio.to_thread(
                self._media_repo.delete_by_filepaths, [normalized_rel]
            )
            if self._folder_cache is not None:
                self._folder_cache.invalidate_for_filepath(normalized_rel)
            return None

        stat_result = await asyncio.to_thread(abs_path.stat)
        if low_io_priority:
            parsed_dict, raw_json = await run_mkvmerge_probe(
                abs_path,
                self._settings.scan_timeout_sec,
                low_io_priority=True,
            )
        else:
            parsed_dict, raw_json = await run_mkvmerge_probe(
                abs_path,
                self._settings.scan_timeout_sec,
            )
        upsert_record = self._build_upsert_record(
            rel_filepath=normalized_rel,
            filename=abs_path.name,
            size_bytes=int(stat_result.st_size),
            mtime=float(stat_result.st_mtime),
            parsed_dict=parsed_dict,
            raw_json=raw_json,
        )
        file_id = await asyncio.to_thread(self._media_repo.upsert, upsert_record)
        if self._folder_cache is not None:
            self._folder_cache.invalidate_for_filepath(normalized_rel)
        return await asyncio.to_thread(self._media_repo.get_by_id, file_id)

    def _build_upsert_record(
        self,
        *,
        rel_filepath: str,
        filename: str,
        size_bytes: int,
        mtime: float,
        parsed_dict: dict[str, Any],
        raw_json: str,
    ) -> MediaFileUpsert:
        """Construct a MediaFileUpsert from parsed `mkvmerge -J` data."""
        probe = parse_mkvmerge_json(parsed_dict)
        return MediaFileUpsert(
            filepath=rel_filepath,
            folder=extract_folder(rel_filepath),
            filename=filename,
            size_bytes=size_bytes,
            mtime=mtime,
            duration_us=probe.duration_us,
            resolution=probe.resolution,
            video_codec=probe.video_codec,
            video_dynamic_range=probe.video_dynamic_range,
            audio_summary_json=probe.audio_summary_json,
            subtitle_count=probe.subtitle_count,
            container_title=probe.container_title,
            has_global_tags=probe.has_global_tags,
            has_aac_51=probe.has_aac_51,
            has_ac3_eac3=probe.has_ac3_eac3,
            raw_json=raw_json,
            scanned_at=utc_now_iso(),
        )

    async def _run_scan(self, *, full: bool) -> None:
        """Execute incremental or full library scan and publish progress updates."""
        storage_root = self._settings.storage_dir
        if not await asyncio.to_thread(storage_root.is_dir):
            message = f"Storage directory does not exist: {storage_root}"
            logger.error(message)
            await asyncio.to_thread(
                self._state_repo.set,
                AppConstants.STATE_KEY_LAST_SCAN_STATUS,
                AppConstants.SCAN_STATUS_FAILED,
            )
            self._publish(
                ScanProgressPayload(
                    status=AppConstants.SCAN_STATUS_FAILED,
                    error_message=message,
                )
            )
            return

        try:
            excluded_prefix = await asyncio.to_thread(
                self._get_excluded_scratch_prefix_sync
            )
            discovered, stat_errors = await asyncio.to_thread(
                _discover_media_files,
                storage_root,
                excluded_rel_prefix=excluded_prefix,
            )
            existing_index = await asyncio.to_thread(self._media_repo.get_scan_index)

            discovered_paths = {item.rel_filepath for item in discovered}
            stale_paths = set(existing_index.keys()) - discovered_paths
            if stale_paths:
                await asyncio.to_thread(
                    self._media_repo.delete_by_filepaths, stale_paths
                )

            total = len(discovered)
            scanned = 0
            skipped = 0
            errors = stat_errors

            self._publish(
                ScanProgressPayload(
                    scanned=0,
                    total=total,
                    skipped=0,
                    errors=errors,
                    current_file=None,
                    status=AppConstants.SCAN_STATUS_RUNNING,
                )
            )

            for entry in discovered:
                if not full and entry.rel_filepath in existing_index:
                    prev_mtime, prev_size, prev_status = existing_index[entry.rel_filepath]
                    if (
                        prev_status == AppConstants.FILE_SCAN_STATUS_READY
                        and abs(prev_mtime - entry.mtime) <= AppConstants.MTIME_TOLERANCE_SEC
                        and prev_size == entry.size_bytes
                    ):
                        skipped += 1
                        scanned += 1
                        self._publish(
                            ScanProgressPayload(
                                scanned=scanned,
                                total=total,
                                skipped=skipped,
                                errors=errors,
                                current_file=entry.rel_filepath,
                                status=AppConstants.SCAN_STATUS_RUNNING,
                            )
                        )
                        continue

                self._publish(
                    ScanProgressPayload(
                        scanned=scanned,
                        total=total,
                        skipped=skipped,
                        errors=errors,
                        current_file=entry.rel_filepath,
                        status=AppConstants.SCAN_STATUS_RUNNING,
                    )
                )

                try:
                    parsed_dict, raw_json = await run_mkvmerge_probe(
                        entry.abs_path,
                        self._settings.scan_timeout_sec,
                    )
                    upsert_record = self._build_upsert_record(
                        rel_filepath=entry.rel_filepath,
                        filename=entry.abs_path.name,
                        size_bytes=entry.size_bytes,
                        mtime=entry.mtime,
                        parsed_dict=parsed_dict,
                        raw_json=raw_json,
                    )
                    await asyncio.to_thread(self._media_repo.upsert, upsert_record)
                except ProbeError as exc:
                    logger.warning("Skipping %s: %s", entry.rel_filepath, exc)
                    await asyncio.to_thread(
                        self._media_repo.mark_scan_error,
                        filepath=entry.rel_filepath,
                        folder=extract_folder(entry.rel_filepath),
                        filename=entry.abs_path.name,
                        size_bytes=entry.size_bytes,
                        mtime=entry.mtime,
                    )
                    errors += 1

                scanned += 1
                self._publish(
                    ScanProgressPayload(
                        scanned=scanned,
                        total=total,
                        skipped=skipped,
                        errors=errors,
                        current_file=entry.rel_filepath,
                        status=AppConstants.SCAN_STATUS_RUNNING,
                    )
                )

            # Always record last_scan_at even if 0 files changed
            await asyncio.to_thread(
                self._state_repo.set,
                AppConstants.STATE_KEY_LAST_SCAN_AT,
                utc_now_iso(),
            )
            await asyncio.to_thread(
                self._state_repo.set,
                AppConstants.STATE_KEY_LAST_SCAN_STATUS,
                AppConstants.SCAN_STATUS_COMPLETED,
            )

            if self._folder_cache is not None:
                self._folder_cache.invalidate_all()

            self._publish(
                ScanProgressPayload(
                    scanned=scanned,
                    total=total,
                    skipped=skipped,
                    errors=errors,
                    current_file=None,
                    status=AppConstants.SCAN_STATUS_COMPLETED,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Unexpected library scan failure")
            await asyncio.to_thread(
                self._state_repo.set,
                AppConstants.STATE_KEY_LAST_SCAN_STATUS,
                AppConstants.SCAN_STATUS_FAILED,
            )
            self._publish(
                ScanProgressPayload(
                    scanned=self._progress.scanned,
                    total=self._progress.total,
                    skipped=self._progress.skipped,
                    errors=self._progress.errors + 1,
                    current_file=self._progress.current_file,
                    status=AppConstants.SCAN_STATUS_FAILED,
                    error_message=str(exc),
                )
            )
