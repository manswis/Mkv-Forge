"""Configurable live filesystem watcher with write-stability debounce and job-safe I/O deferral."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable

from watchfiles import Change, awatch

from app.config import AppConstants, Settings
from app.db.repositories import JobRepository, MediaFileRepository
from app.models.schemas import WatcherStatus
from app.services.library_events import LibraryEventBroker
from app.services.scanner import LibraryScanner, ProbeError
from app.utils.paths import (
    PathSafetyError,
    extract_folder,
    is_supported_media_file,
    safe_resolve_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)

ActiveJobChecker = Callable[[], bool]


def is_watchable_media_path(path: Path) -> bool:
    """Return True only for supported `.mkv`/`.mp4` files, ignoring temp/partial downloads."""
    lower_name = path.name.lower()
    if not lower_name:
        return False
    for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES:
        if lower_name.endswith(suffix):
            return False
    return is_supported_media_file(path)


def should_watch_fs_event(change: Change, raw_path: str) -> bool:
    """Filter raw `watchfiles` events before waking the watcher loop or logging changes.

    Drops hidden dotfiles, internal `.mkvforge.` temp files, partial download files,
    and `Change.modified` bursts on non-media files.
    """
    if not raw_path or not raw_path.strip():
        return False
    path = Path(raw_path)
    name = path.name
    if not name or name.startswith("."):
        return False
    lower_name = name.lower()
    if AppConstants.TEMP_MARKER in lower_name:
        return False
    if any(
        lower_name.endswith(suffix)
        for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES
    ):
        return False
    if change == Change.modified and not is_watchable_media_path(path):
        return False
    return True


def _extract_job_target_paths(job_row: dict) -> set[str]:
    """Return relative filepaths currently owned by a running MKV Forge job."""
    owned: set[str] = set()
    filepath = job_row.get("filepath")
    if isinstance(filepath, str) and filepath.strip():
        owned.add(filepath.strip())
    output_filepath = job_row.get("output_filepath")
    if isinstance(output_filepath, str) and output_filepath.strip():
        owned.add(output_filepath.strip())

    raw_cfg = job_row.get("config_json")
    if isinstance(raw_cfg, str) and raw_cfg:
        try:
            parsed = json.loads(raw_cfg)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            out_path = parsed.get("output_path")
            if isinstance(out_path, str) and out_path.strip():
                owned.add(out_path.strip().lstrip("/"))
            # Replace .mp4 writes <stem>.mkv in same directory
            if (
                filepath
                and isinstance(filepath, str)
                and filepath.lower().endswith(AppConstants.EXT_MP4)
                and parsed.get("output_mode") == AppConstants.OUTPUT_MODE_REPLACE
            ):
                owned.add(f"{filepath[:-len(AppConstants.EXT_MP4)]}{AppConstants.EXT_MKV}")
    return owned


class LibraryWatcher:
    """Watches STORAGE_DIR for added/modified/deleted media files without disrupting jobs."""

    def __init__(
        self,
        *,
        settings: Settings,
        media_repo: MediaFileRepository,
        job_repo: JobRepository,
        scanner: LibraryScanner,
        library_broker: LibraryEventBroker,
        job_defer_poll_sec: float = AppConstants.WATCH_JOB_DEFER_POLL_SEC,
        poll_interval_ms: int = AppConstants.WATCH_POLL_INTERVAL_MS,
    ) -> None:
        self._settings = settings
        self._media_repo = media_repo
        self._job_repo = job_repo
        self._scanner = scanner
        self._library_broker = library_broker
        self._job_defer_poll_sec = max(0.01, float(job_defer_poll_sec))
        self._poll_interval_ms = poll_interval_ms

        self._stop_event = asyncio.Event()
        self._watch_task: asyncio.Task[None] | None = None
        self._periodic_task: asyncio.Task[None] | None = None
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def is_active(self) -> bool:
        """Return True if the live filesystem watcher loop is running."""
        return self._watch_task is not None and not self._watch_task.done()

    def get_status(self) -> WatcherStatus:
        """Return current watcher configuration and runtime status."""
        return WatcherStatus(
            mode=self._settings.watch_mode,  # type: ignore[arg-type]
            active=self.is_active,
            debounce_sec=self._settings.watch_debounce_sec,
            scan_interval_min=self._settings.scan_interval_min,
        )

    def start(self) -> None:
        """Start watcher and optional periodic incremental fallback if enabled."""
        self._stop_event.clear()

        if self._settings.watch_mode != AppConstants.WATCH_MODE_OFF:
            if not self.is_active:
                self._watch_task = asyncio.create_task(self._watch_loop())
                logger.info(
                    "Started library watcher (mode=%s, debounce=%.2fs, storage=%s)",
                    self._settings.watch_mode,
                    self._settings.watch_debounce_sec,
                    self._settings.storage_dir,
                )

        if self._settings.scan_interval_min > 0:
            if self._periodic_task is None or self._periodic_task.done():
                self._periodic_task = asyncio.create_task(self._periodic_scan_loop())
                logger.info(
                    "Started periodic incremental fallback scan every %d min",
                    self._settings.scan_interval_min,
                )

    async def shutdown(self) -> None:
        """Stop watcher, periodic fallback, and all in-flight debounce tasks cleanly."""
        self._stop_event.set()

        for task in list(self._debounce_tasks.values()):
            task.cancel()
        if self._debounce_tasks:
            await asyncio.gather(*self._debounce_tasks.values(), return_exceptions=True)
            self._debounce_tasks.clear()

        for bg_task in (self._watch_task, self._periodic_task):
            if bg_task is not None and not bg_task.done():
                bg_task.cancel()
                try:
                    await bg_task
                except asyncio.CancelledError:
                    pass
        self._watch_task = None
        self._periodic_task = None

    async def _watch_loop(self) -> None:
        """Watch STORAGE_DIR using native OS events (`watch`) or polling (`poll`)."""
        force_polling = self._settings.watch_mode == AppConstants.WATCH_MODE_POLL
        storage_dir = self._settings.storage_dir

        while not self._stop_event.is_set():
            if not await asyncio.to_thread(storage_dir.is_dir):
                logger.warning(
                    "Watcher waiting: storage directory %s is not mounted/present",
                    storage_dir,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.watch_debounce_sec,
                    )
                except asyncio.TimeoutError:
                    continue
                break

            try:
                async for changes in awatch(
                    storage_dir,
                    watch_filter=should_watch_fs_event,
                    stop_event=self._stop_event,
                    force_polling=force_polling,
                    poll_delay_ms=self._poll_interval_ms,
                    recursive=True,
                ):
                    if self._stop_event.is_set():
                        break
                    for change_type, raw_path in changes:
                        self.handle_raw_fs_event(change_type, Path(raw_path))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Library watcher error (retrying): %s", exc)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.watch_debounce_sec,
                    )
                except asyncio.TimeoutError:
                    continue

    async def _periodic_scan_loop(self) -> None:
        """Optional HDD-friendly periodic incremental fallback scan."""
        interval_sec = self._settings.scan_interval_min * 60
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_sec)
                break
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                break
            if self._scanner.is_running:
                continue
            active_job = await asyncio.to_thread(self._job_repo.get_active)
            if active_job is not None:
                continue
            self._scanner.start_scan(full=False)

    def _is_ignored_hidden_or_internal(self, path: Path) -> bool:
        """Return True for dot-hidden names or MKV Forge internal temp files."""
        name = path.name
        if not name or name.startswith("."):
            return True
        lower_name = name.lower()
        return AppConstants.TEMP_MARKER in lower_name

    def _is_ignored_name(self, path: Path) -> bool:
        """Return True for hidden names, internal temp files, or partial download suffixes."""
        if self._is_ignored_hidden_or_internal(path):
            return True
        lower_name = path.name.lower()
        return any(
            lower_name.endswith(suffix)
            for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES
        )

    def handle_raw_fs_event(self, change_type: Change, path: Path) -> None:
        """Filter one raw filesystem event and schedule media or folder synchronization."""
        if self._is_ignored_hidden_or_internal(path):
            return

        try:
            rel_filepath = to_relative_storage_path(path, self._settings.storage_dir)
        except PathSafetyError:
            return

        if not is_watchable_media_path(path):
            self._handle_non_media_fs_event(change_type, path, rel_filepath)
            return

        if self._is_ignored_name(path):
            return

        if change_type == Change.deleted:
            # Cancel any pending upsert debounce for this path and run single-row delete
            existing_task = self._debounce_tasks.pop(rel_filepath, None)
            if existing_task is not None and not existing_task.done():
                existing_task.cancel()
            task = asyncio.create_task(self._process_deleted(rel_filepath))
            self._debounce_tasks[rel_filepath] = task
            task.add_done_callback(
                lambda done_task, key=rel_filepath: self._cleanup_task_ref(key, done_task)
            )
            return

        # Added or modified: reset stability debounce timer for this filepath
        prev_task = self._debounce_tasks.pop(rel_filepath, None)
        if prev_task is not None and not prev_task.done():
            prev_task.cancel()

        task = asyncio.create_task(self._process_settled_upsert(rel_filepath))
        self._debounce_tasks[rel_filepath] = task
        task.add_done_callback(
            lambda done_task, key=rel_filepath: self._cleanup_task_ref(key, done_task)
        )

    @staticmethod
    def _path_is_dir_or_file_sync(path: Path) -> bool:
        try:
            return path.is_dir() or path.is_file()
        except OSError:
            return False

    async def _process_added_non_media(self, path: Path, rel_filepath: str) -> None:
        """Verify added non-media path off the event loop and emit library_changed."""
        try:
            exists = await asyncio.to_thread(self._path_is_dir_or_file_sync, path)
            if exists:
                self._library_broker.record_change(
                    action="updated", filepath=rel_filepath
                )
        except asyncio.CancelledError:
            raise

    def _handle_non_media_fs_event(
        self, change_type: Change, path: Path, rel_filepath: str
    ) -> None:
        """Handle directory and non-media file creation/deletion so Folder View stays live.

        Ignores `Change.modified` bursts on non-media files so multi-GB `.uploading`/`.part`
        writes never spam SSE; emits once on creation and once on deletion/rename.
        """
        if change_type == Change.added:
            key = f"nonmedia_add:{rel_filepath}"
            prev_task = self._debounce_tasks.pop(key, None)
            if prev_task is not None and not prev_task.done():
                prev_task.cancel()
            task = asyncio.create_task(
                self._process_added_non_media(path, rel_filepath)
            )
            self._debounce_tasks[key] = task
            task.add_done_callback(
                lambda done_task, k=key: self._cleanup_task_ref(k, done_task)
            )
            return

        if change_type == Change.deleted:
            key = f"dir:{rel_filepath}"
            prev_task = self._debounce_tasks.pop(key, None)
            if prev_task is not None and not prev_task.done():
                prev_task.cancel()
            task = asyncio.create_task(
                self._process_deleted_directory(
                    rel_filepath, has_suffix=bool(path.suffix)
                )
            )
            self._debounce_tasks[key] = task
            task.add_done_callback(
                lambda done_task, k=key: self._cleanup_task_ref(k, done_task)
            )

    def schedule_background_probe(self, rel_filepath: str) -> bool:
        """Queue a non-blocking background probe for a newly spotted/modified file.

        Deduplicates against any in-flight watcher task for `rel_filepath`. If the file's
        `mtime` is already older than `watch_debounce_sec`, skips the idle wait and probes
        immediately (still respecting active-job ownership and USB I/O deferral).
        """
        if self._stop_event.is_set():
            return False
        existing = self._debounce_tasks.get(rel_filepath)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(
            self._process_settled_upsert(rel_filepath, fast_if_aged=True)
        )
        self._debounce_tasks[rel_filepath] = task
        task.add_done_callback(
            lambda done_task, key=rel_filepath: self._cleanup_task_ref(key, done_task)
        )
        return True

    async def _process_deleted_directory(
        self, rel_filepath: str, *, has_suffix: bool
    ) -> None:
        """Prune orphaned SQLite rows if a folder vanished on disk and notify UI."""
        try:
            if not await asyncio.to_thread(self._settings.storage_dir.is_dir):
                return
            try:
                resolved = await asyncio.to_thread(
                    safe_resolve_path, rel_filepath, self._settings.storage_dir
                )
            except PathSafetyError:
                return
            if await asyncio.to_thread(resolved.exists):
                return

            display_folder = f"/{rel_filepath.strip('/')}"
            deleted_ids = await asyncio.to_thread(
                self._media_repo.delete_by_folder_prefix, display_folder
            )
            self._library_broker.record_change(
                action="deleted", filepath=rel_filepath
            )
        except asyncio.CancelledError:
            raise

    def _cleanup_task_ref(self, rel_filepath: str, task: asyncio.Task[None]) -> None:
        if self._debounce_tasks.get(rel_filepath) is task:
            self._debounce_tasks.pop(rel_filepath, None)

    async def _is_owned_by_running_job(self, rel_filepath: str) -> tuple[bool, bool]:
        """Return `(any_job_running, this_filepath_owned_by_running_job)`."""
        active_job = await asyncio.to_thread(self._job_repo.get_active)
        if active_job is None:
            return False, False
        owned = _extract_job_target_paths(active_job)
        return True, rel_filepath in owned

    async def _wait_for_stability(self, resolved_path: Path) -> tuple[int, float] | None:
        """Wait until `(size_bytes, mtime)` stay unchanged across `watch_debounce_sec`."""
        checks = 0
        while not self._stop_event.is_set() and checks < AppConstants.WATCH_MAX_STABILITY_CHECKS:
            checks += 1
            try:
                st1 = await asyncio.to_thread(resolved_path.stat)
            except OSError:
                return None
            size1, mtime1 = int(st1.st_size), float(st1.st_mtime)

            await asyncio.sleep(self._settings.watch_debounce_sec)
            if self._stop_event.is_set():
                return None

            try:
                st2 = await asyncio.to_thread(resolved_path.stat)
            except OSError:
                return None
            size2, mtime2 = int(st2.st_size), float(st2.st_mtime)

            if (
                size2 > 0
                and size1 == size2
                and abs(mtime1 - mtime2) <= AppConstants.MTIME_TOLERANCE_SEC
            ):
                return size2, mtime2

        return None

    async def _process_settled_upsert(
        self, rel_filepath: str, *, fast_if_aged: bool = False
    ) -> None:
        """Debounce writes, defer during active jobs, probe single file, and emit SSE."""
        try:
            any_running, owned = await self._is_owned_by_running_job(rel_filepath)
            if owned:
                logger.debug("Watcher suppressed active-job echo for %s", rel_filepath)
                return

            try:
                resolved = await asyncio.to_thread(
                    safe_resolve_path, rel_filepath, self._settings.storage_dir
                )
            except PathSafetyError:
                return

            # Phase A (Instant Discovery): insert a lightweight 'scanning' placeholder right
            # away so the UI shows the new file immediately before debounce/probing completes.
            try:
                st_initial = await asyncio.to_thread(resolved.stat)
                placeholder_id, inserted_new = await asyncio.to_thread(
                    self._media_repo.upsert_scanning_placeholder,
                    filepath=rel_filepath,
                    folder=extract_folder(rel_filepath),
                    filename=resolved.name,
                    size_bytes=int(st_initial.st_size),
                    mtime=float(st_initial.st_mtime),
                )
                if inserted_new:
                    self._library_broker.record_change(
                        action="added",
                        filepath=rel_filepath,
                        file_id=placeholder_id,
                    )
            except OSError:
                pass

            settled: tuple[int, float] | None = None
            if fast_if_aged:
                try:
                    st = await asyncio.to_thread(resolved.stat)
                    age_sec = time.time() - float(st.st_mtime)
                    if int(st.st_size) > 0 and age_sec >= self._settings.watch_debounce_sec:
                        settled = (int(st.st_size), float(st.st_mtime))
                except OSError:
                    settled = None

            if settled is None:
                settled = await self._wait_for_stability(resolved)
            if settled is None:
                # File disappeared while debouncing (e.g., cancelled copy or rename)
                if not await asyncio.to_thread(resolved.exists):
                    await self._process_deleted(rel_filepath)
                return

            # Defer probing while any remux/FFmpeg job is actively writing to USB/HDD
            while not self._stop_event.is_set():
                any_running, owned = await self._is_owned_by_running_job(rel_filepath)
                if owned:
                    logger.debug("Watcher suppressed active-job echo for %s", rel_filepath)
                    return
                if not any_running:
                    break
                await asyncio.sleep(self._job_defer_poll_sec)

            if self._stop_event.is_set():
                return

            # Skip redundant probe ONLY if SQLite already has scan_status == 'ready'
            # AND matching (size_bytes, mtime). Rows in 'scanning' MUST be probed.
            existing = await asyncio.to_thread(
                self._media_repo.get_by_filepath, rel_filepath
            )
            size_now, mtime_now = settled
            if (
                existing is not None
                and str(existing.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
                == AppConstants.FILE_SCAN_STATUS_READY
                and int(existing["size_bytes"]) == size_now
                and abs(float(existing["mtime"]) - mtime_now)
                <= AppConstants.MTIME_TOLERANCE_SEC
            ):
                return

            action = "added" if existing is None else "updated"
            try:
                refreshed = await self._scanner.probe_single_file(rel_filepath)
            except (ProbeError, PathSafetyError) as exc:
                logger.warning("Watcher failed to probe file %s: %s", rel_filepath, exc)
                err_id = await asyncio.to_thread(
                    self._media_repo.mark_scan_error,
                    filepath=rel_filepath,
                    folder=extract_folder(rel_filepath),
                    filename=resolved.name,
                    size_bytes=size_now,
                    mtime=mtime_now,
                )
                self._library_broker.record_change(
                    action="updated",
                    filepath=rel_filepath,
                    file_id=err_id,
                )
                return

            if refreshed is None:
                if existing is not None:
                    self._library_broker.record_change(
                        action="deleted",
                        filepath=rel_filepath,
                        file_id=int(existing["id"]),
                    )
                return

            self._library_broker.record_change(
                action=action,
                filepath=rel_filepath,
                file_id=int(refreshed["id"]),
            )
        except asyncio.CancelledError:
            raise

    async def _process_deleted(self, rel_filepath: str) -> None:
        """Remove a single deleted media row without wiping SQLite on storage unmount."""
        try:
            # Safety guard: never delete rows if STORAGE_DIR itself disappeared/unmounted
            if not await asyncio.to_thread(self._settings.storage_dir.is_dir):
                return

            any_running, owned = await self._is_owned_by_running_job(rel_filepath)
            if owned:
                logger.debug("Watcher suppressed active-job delete echo for %s", rel_filepath)
                return

            try:
                resolved = await asyncio.to_thread(
                    safe_resolve_path, rel_filepath, self._settings.storage_dir
                )
            except PathSafetyError:
                return

            # Guard against atomic replace (file still exists right after rename)
            if await asyncio.to_thread(resolved.exists):
                return

            existing = await asyncio.to_thread(
                self._media_repo.get_by_filepath, rel_filepath
            )
            if existing is None:
                return

            deleted_count = await asyncio.to_thread(
                self._media_repo.delete_by_filepaths, [rel_filepath]
            )
            if deleted_count > 0:
                self._library_broker.record_change(
                    action="deleted",
                    filepath=rel_filepath,
                    file_id=int(existing["id"]),
                )
        except asyncio.CancelledError:
            raise
