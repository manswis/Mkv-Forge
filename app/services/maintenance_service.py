"""Database optimization, temp/upload cleanup, ghost pruning, and idle-only maintenance scheduler."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    utc_now_iso,
)
from app.models.schemas import (
    FactoryResetResponse,
    MaintenanceActionResponse,
    MaintenanceRunAllResponse,
    MaintenanceStatusResponse,
)
from app.services.library_events import LibraryEventBroker
from app.services.processing_policy import list_managed_scratch_dirs
from app.services.scanner import LibraryScanner
from app.services.user_settings import UserSettingsService
from app.utils.paths import PathSafetyError, safe_resolve_path

logger = logging.getLogger(__name__)


class MaintenanceBusyError(RuntimeError):
    """Raised when maintenance cannot run because a scan or remux job is active."""


class StorageUnmountedError(RuntimeError):
    """Raised when storage appears unmounted/empty to prevent wiping the media index."""


def _parse_iso_utc(raw: str | None) -> datetime | None:
    """Safely parse an ISO-8601 timestamp into a timezone-aware UTC datetime."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _file_size_or_zero(path: Path) -> int:
    """Return file size in bytes or 0 if missing/inaccessible."""
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


class MaintenanceService:
    """Coordinates safe SQLite optimization, temp/upload cleanup, and scheduled maintenance."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: DatabaseManager,
        state_repo: AppStateRepository,
        media_repo: MediaFileRepository,
        job_repo: JobRepository,
        scanner: LibraryScanner,
        library_broker: LibraryEventBroker,
        user_settings_service: UserSettingsService,
        scheduler_poll_sec: float = AppConstants.MAINTENANCE_SCHEDULER_POLL_SEC,
    ) -> None:
        self._settings = settings
        self._db = db
        self._state_repo = state_repo
        self._media_repo = media_repo
        self._job_repo = job_repo
        self._scanner = scanner
        self._library_broker = library_broker
        self._user_settings_service = user_settings_service
        self._scheduler_poll_sec = max(0.05, float(scheduler_poll_sec))

        self._op_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None

    @property
    def op_lock(self) -> asyncio.Lock:
        """Async lock serializing concurrent maintenance invocations."""
        return self._op_lock

    def _get_busy_reason(self) -> str | None:
        """Return a human-readable busy reason if a scan or job is active, else None."""
        if self._scanner.is_running:
            return "A library scan is currently running."
        try:
            if self._job_repo.get_active() is not None:
                return "A processing job is currently running."
        except sqlite3.DatabaseError:
            # Allow recovery paths (such as Factory Reset) to inspect corrupted DB safely
            return None
        return None

    def _ensure_not_busy(self) -> None:
        reason = self._get_busy_reason()
        if reason is not None:
            raise MaintenanceBusyError(reason)

    def _collect_active_upload_ids(self) -> set[str]:
        """Return all staged upload_ids referenced by queued or running jobs."""
        active_ids: set[str] = set()
        for raw_cfg in self._job_repo.list_active_and_queued_configs():
            try:
                parsed = json.loads(raw_cfg)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            ext_tracks = parsed.get("external_tracks")
            if not isinstance(ext_tracks, list):
                continue
            for track in ext_tracks:
                if (
                    isinstance(track, dict)
                    and track.get("source") == AppConstants.EXTERNAL_TRACK_SOURCE_UPLOAD
                    and isinstance(track.get("upload_id"), str)
                    and track["upload_id"].strip()
                ):
                    active_ids.add(track["upload_id"].strip())
        return active_ids

    def _find_orphaned_uploads(
        self, *, min_age_sec: int, now_ts: float | None = None
    ) -> list[tuple[Path, int]]:
        """List unreferenced staged upload files older than `min_age_sec`."""
        uploads_dir = self._settings.uploads_dir
        if not uploads_dir.is_dir():
            return []
        active_ids = self._collect_active_upload_ids()
        ref_now = time.time() if now_ts is None else now_ts
        found: list[tuple[Path, int]] = []
        try:
            with os.scandir(uploads_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name in active_ids:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    age_sec = ref_now - float(st.st_mtime)
                    if min_age_sec > 0 and age_sec < min_age_sec:
                        continue
                    found.append((Path(entry.path), int(st.st_size)))
        except OSError:
            return []
        return found

    def _find_stray_temp_files(self) -> list[tuple[Path, int]]:
        """Walk storage_dir and managed scratch dirs for leftover `*.mkvforge.*` temp files."""
        roots: list[Path] = [
            self._settings.storage_dir,
            *list_managed_scratch_dirs(self._settings),
        ]
        found: list[tuple[Path, int]] = []
        seen: set[str] = set()
        for base_dir in roots:
            if not base_dir.is_dir():
                continue
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    if AppConstants.TEMP_MARKER not in name:
                        continue
                    candidate = Path(root) / name
                    key = str(candidate)
                    if key in seen:
                        continue
                    try:
                        if candidate.is_symlink():
                            continue
                        size = int(candidate.stat().st_size)
                    except OSError:
                        continue
                    seen.add(key)
                    found.append((candidate, size))
        return found

    def _db_file_sizes(self) -> tuple[int, int]:
        """Return `(db_bytes, wal_bytes)` for the active SQLite database."""
        db_path = self._db.db_path
        wal_path = Path(f"{db_path}-wal")
        return _file_size_or_zero(db_path), _file_size_or_zero(wal_path)

    def get_status(
        self, *, min_age_sec: int = AppConstants.ORPHAN_UPLOAD_MIN_AGE_SEC
    ) -> MaintenanceStatusResponse:
        """Collect read-only storage, database freelist, and orphan temp diagnostics."""
        db_bytes, wal_bytes = self._db_file_sizes()
        reclaimable_bytes = 0
        with self._db.connect() as conn:
            page_size_row = conn.execute("PRAGMA page_size;").fetchone()
            freelist_row = conn.execute("PRAGMA freelist_count;").fetchone()
            page_size = int(page_size_row[0]) if page_size_row else 0
            freelist_count = int(freelist_row[0]) if freelist_row else 0
            reclaimable_bytes = max(0, page_size * freelist_count)

        counts = self._media_repo.get_dashboard_counts()
        terminal_jobs = self._job_repo.count_terminal_jobs()
        busy_reason = self._get_busy_reason()

        orphans = self._find_orphaned_uploads(min_age_sec=min_age_sec)
        stray_temps = [] if busy_reason is not None else self._find_stray_temp_files()

        return MaintenanceStatusResponse(
            db_size_bytes=db_bytes,
            wal_size_bytes=wal_bytes,
            reclaimable_bytes=reclaimable_bytes,
            indexed_files_count=counts["total_files"],
            terminal_jobs_count=terminal_jobs,
            orphaned_uploads_count=len(orphans),
            orphaned_uploads_bytes=sum(size for _, size in orphans),
            stray_temp_files_count=len(stray_temps),
            stray_temp_files_bytes=sum(size for _, size in stray_temps),
            last_auto_cleanup_at=self._state_repo.get(
                AppConstants.STATE_KEY_LAST_AUTO_CLEANUP_AT
            ),
            busy=busy_reason is not None,
            busy_reason=busy_reason,
        )

    def optimize_database(self) -> MaintenanceActionResponse:
        """Run WAL truncation, SQLite VACUUM, and PRAGMA optimize when idle."""
        self._ensure_not_busy()
        started = time.perf_counter()
        db_before, wal_before = self._db_file_sizes()
        bytes_before = db_before + wal_before

        conn = sqlite3.connect(
            str(self._db.db_path),
            timeout=AppConstants.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout={AppConstants.SQLITE_BUSY_TIMEOUT_MS};")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("VACUUM;")
            conn.execute("PRAGMA optimize;")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()

        db_after, wal_after = self._db_file_sizes()
        bytes_after = db_after + wal_after
        reclaimed = max(0, bytes_before - bytes_after)
        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)

        logger.info(
            "Maintenance action=optimize_db bytes_before=%d bytes_after=%d reclaimed=%d ms=%.1f",
            bytes_before,
            bytes_after,
            reclaimed,
            duration_ms,
        )
        return MaintenanceActionResponse(
            action="optimize_db",
            removed_count=0,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            bytes_reclaimed=reclaimed,
            duration_ms=duration_ms,
            message=(
                f"Database optimized (reclaimed {reclaimed} bytes)."
                if reclaimed > 0
                else "Database optimized and compacted."
            ),
        )

    def clean_temp_and_uploads(
        self,
        *,
        min_age_sec: int = AppConstants.ORPHAN_UPLOAD_MIN_AGE_SEC,
    ) -> MaintenanceActionResponse:
        """Delete orphaned staged uploads older than `min_age_sec` and stray `.mkvforge.*` files."""
        self._ensure_not_busy()
        started = time.perf_counter()

        orphans = self._find_orphaned_uploads(min_age_sec=min_age_sec)
        stray_temps = self._find_stray_temp_files()
        targets = [*orphans, *stray_temps]
        bytes_before = sum(size for _, size in targets)
        removed = 0
        reclaimed = 0

        for path, size in targets:
            try:
                path.unlink(missing_ok=True)
                removed += 1
                reclaimed += size
            except OSError:
                continue

        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        logger.info(
            "Maintenance action=clean_temp removed=%d reclaimed=%d ms=%.1f",
            removed,
            reclaimed,
            duration_ms,
        )
        return MaintenanceActionResponse(
            action="clean_temp",
            removed_count=removed,
            bytes_before=bytes_before,
            bytes_after=max(0, bytes_before - reclaimed),
            bytes_reclaimed=reclaimed,
            duration_ms=duration_ms,
            message=(
                f"Removed {removed} temporary/staged file(s)."
                if removed > 0
                else "No stale temporary or staged files found."
            ),
        )

    def prune_missing_files(self) -> MaintenanceActionResponse:
        """Remove SQLite rows for files no longer on disk, with strict USB-unmount protection."""
        self._ensure_not_busy()
        started = time.perf_counter()

        indexed = self._media_repo.list_all_filepaths()
        if not indexed:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            return MaintenanceActionResponse(
                action="prune_missing",
                removed_count=0,
                duration_ms=duration_ms,
                message="No indexed media files to check.",
            )

        storage_dir = self._settings.storage_dir
        if not storage_dir.is_dir():
            raise StorageUnmountedError(
                "Storage directory is not mounted; refusing to prune media index."
            )

        # Guardrail #2: abort if /storage has 0 visible entries while DB has indexed rows
        try:
            with os.scandir(storage_dir) as top_entries:
                has_any_entry = any(not e.name.startswith(".") for e in top_entries)
        except OSError as exc:
            raise StorageUnmountedError(
                "Storage directory is unreadable; refusing to prune media index."
            ) from exc

        if not has_any_entry:
            raise StorageUnmountedError(
                "Storage directory appears empty (possible unmounted drive); refusing to wipe index."
            )

        vanished_paths: list[str] = []
        existing_count = 0
        for _file_id, rel_path in indexed:
            try:
                resolved = safe_resolve_path(rel_path, storage_dir)
            except PathSafetyError:
                vanished_paths.append(rel_path)
                continue
            if resolved.is_file():
                existing_count += 1
            else:
                vanished_paths.append(rel_path)

        # Second safety gate: if 0 indexed files exist on disk while DB has rows, treat as unmounted!
        if existing_count == 0 and len(vanished_paths) > 0:
            raise StorageUnmountedError(
                "Zero indexed files found on disk (possible unmounted drive); refusing to wipe index."
            )

        removed = (
            self._media_repo.delete_by_filepaths(vanished_paths)
            if vanished_paths
            else 0
        )
        if removed > 0:
            self._library_broker.record_change(action="deleted", filepath="/")

        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        logger.info(
            "Maintenance action=prune_missing checked=%d removed=%d ms=%.1f",
            len(indexed),
            removed,
            duration_ms,
        )
        return MaintenanceActionResponse(
            action="prune_missing",
            removed_count=removed,
            duration_ms=duration_ms,
            message=(
                f"Pruned {removed} missing file(s) from index."
                if removed > 0
                else "All indexed files exist on disk."
            ),
        )

    def prune_old_jobs(
        self, *, retention_days: int | None = None
    ) -> MaintenanceActionResponse:
        """Delete terminal job history rows older than `retention_days` (0 = keep forever)."""
        started = time.perf_counter()
        days = (
            retention_days
            if retention_days is not None
            else self._user_settings_service.get().job_history_retention_days
        )
        if days <= 0:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            return MaintenanceActionResponse(
                action="prune_jobs",
                removed_count=0,
                duration_ms=duration_ms,
                message="Job history retention is set to Never; no rows removed.",
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = self._job_repo.prune_terminal_jobs_older_than(cutoff.isoformat())
        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        logger.info(
            "Maintenance action=prune_jobs retention_days=%d removed=%d ms=%.1f",
            days,
            removed,
            duration_ms,
        )
        return MaintenanceActionResponse(
            action="prune_jobs",
            removed_count=removed,
            duration_ms=duration_ms,
            message=(
                f"Pruned {removed} job history record(s) older than {days} days."
                if removed > 0
                else f"No job history records older than {days} days."
            ),
        )

    def run_all(
        self,
        *,
        min_age_sec: int = AppConstants.ORPHAN_UPLOAD_MIN_AGE_SEC,
    ) -> MaintenanceRunAllResponse:
        """Run clean_temp -> prune_missing -> prune_jobs -> optimize_db safely in sequence."""
        self._ensure_not_busy()
        started = time.perf_counter()

        clean_res = self.clean_temp_and_uploads(min_age_sec=min_age_sec)
        prune_missing_res = self.prune_missing_files()
        prune_jobs_res = self.prune_old_jobs()
        optimize_res = self.optimize_database()

        self._state_repo.set(
            AppConstants.STATE_KEY_LAST_AUTO_CLEANUP_AT, utc_now_iso()
        )
        total_reclaimed = clean_res.bytes_reclaimed + optimize_res.bytes_reclaimed
        total_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return MaintenanceRunAllResponse(
            clean_temp=clean_res,
            prune_missing=prune_missing_res,
            prune_jobs=prune_jobs_res,
            optimize_db=optimize_res,
            total_bytes_reclaimed=total_reclaimed,
            total_duration_ms=total_ms,
        )

    def is_scheduled_cleanup_due(self, *, now_utc: datetime | None = None) -> bool:
        """Return True if scheduled auto-cleanup is enabled and interval has elapsed."""
        cfg = self._user_settings_service.get()
        if not cfg.auto_cleanup_enabled:
            return False
        if not (
            cfg.auto_cleanup_temp
            or cfg.auto_cleanup_jobs
            or cfg.auto_cleanup_optimize_db
        ):
            return False

        last_raw = self._state_repo.get(AppConstants.STATE_KEY_LAST_AUTO_CLEANUP_AT)
        last_dt = _parse_iso_utc(last_raw)
        if last_dt is None:
            return True

        ref_now = now_utc or datetime.now(timezone.utc)
        elapsed_sec = (ref_now - last_dt).total_seconds()
        required_sec = cfg.auto_cleanup_interval_days * AppConstants.SECONDS_PER_DAY
        return elapsed_sec >= required_sec

    async def maybe_run_scheduled_cleanup(self) -> bool:
        """Execute enabled scheduled cleanup tasks ONLY if due and system is completely idle."""
        if not await asyncio.to_thread(self.is_scheduled_cleanup_due):
            return False
        if self._op_lock.locked():
            return False

        async with self._op_lock:
            if await asyncio.to_thread(self._get_busy_reason) is not None:
                return False
            cfg = await asyncio.to_thread(self._user_settings_service.get)
            if not cfg.auto_cleanup_enabled:
                return False

            ran_any = False
            try:
                if cfg.auto_cleanup_temp:
                    await asyncio.to_thread(self.clean_temp_and_uploads)
                    ran_any = True
                if cfg.auto_cleanup_jobs:
                    await asyncio.to_thread(
                        self.prune_old_jobs,
                        retention_days=cfg.job_history_retention_days,
                    )
                    ran_any = True
                if cfg.auto_cleanup_optimize_db:
                    await asyncio.to_thread(self.optimize_database)
                    ran_any = True
                if ran_any:
                    await asyncio.to_thread(
                        self._state_repo.set,
                        AppConstants.STATE_KEY_LAST_AUTO_CLEANUP_AT,
                        utc_now_iso(),
                    )
            except MaintenanceBusyError:
                return False
            except Exception as exc:
                logger.warning(
                    "Scheduled maintenance skipped due to error_type=%s",
                    type(exc).__name__,
                )
                return False
            return ran_any

    def start_scheduler(self) -> None:
        """Start the idle-only periodic maintenance check loop."""
        self._stop_event.clear()
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def shutdown_scheduler(self) -> None:
        """Stop the periodic maintenance scheduler cleanly."""
        self._stop_event.set()
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self._scheduler_task = None

    def _rebuild_database_and_clear_uploads(self) -> tuple[int, int]:
        """Physically delete SQLite DB (+WAL/SHM), clear uploads contents, and reinitialize schema.

        Never touches `settings.storage_dir` media files. Keeps `settings.uploads_dir`
        directory intact so subsequent uploads succeed without FileNotFoundError.
        """
        gc.collect()
        removed_db_files = 0
        db_path = self._db.db_path
        db_candidates = (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
            Path(f"{db_path}-journal"),
        )
        for candidate in db_candidates:
            try:
                if candidate.is_file() or candidate.is_symlink():
                    candidate.unlink()
                    removed_db_files += 1
            except OSError as exc:
                logger.warning("Could not unlink %s during factory reset: %s", candidate, exc)

        removed_uploads = 0
        uploads_dir = self._settings.uploads_dir
        if uploads_dir.is_dir():
            try:
                with os.scandir(uploads_dir) as entries:
                    for entry in entries:
                        entry_path = Path(entry.path)
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                shutil.rmtree(entry_path)
                            else:
                                entry_path.unlink()
                            removed_uploads += 1
                        except OSError as exc:
                            logger.warning(
                                "Could not remove staged upload %s during factory reset: %s",
                                entry_path,
                                exc,
                            )
            except OSError as exc:
                logger.warning("Could not scan uploads dir %s: %s", uploads_dir, exc)
        uploads_dir.mkdir(parents=True, exist_ok=True)

        thumbs_dir = self._settings.data_dir / AppConstants.THUMBNAILS_SUBDIR_NAME
        if thumbs_dir.is_dir():
            try:
                shutil.rmtree(thumbs_dir)
            except OSError as exc:
                logger.warning("Could not clear thumbnails cache %s: %s", thumbs_dir, exc)

        # Create a brand-new, clean WAL SQLite schema
        self._db.initialize()
        return removed_db_files, removed_uploads

    async def factory_reset(
        self,
        *,
        job_worker: Any | None = None,
        library_watcher: Any | None = None,
        job_broker: Any | None = None,
    ) -> FactoryResetResponse:
        """Pause background loops, physically rebuild SQLite, reset SSE buffers, and resume loops."""
        if self._op_lock.locked():
            raise MaintenanceBusyError("Another maintenance operation is already in progress.")

        started = time.perf_counter()
        async with self._op_lock:
            if self._scanner.is_running:
                raise MaintenanceBusyError(
                    "Cannot factory reset while a library scan is running."
                )
            if job_worker is not None and getattr(job_worker, "active_job_id", None) is not None:
                raise MaintenanceBusyError(
                    "Cannot factory reset while a conversion job is running."
                )
            self._ensure_not_busy()

            was_scheduler_running = (
                self._scheduler_task is not None and not self._scheduler_task.done()
            )
            was_watcher_running = bool(
                library_watcher is not None and getattr(library_watcher, "is_active", False)
            )
            was_worker_running = bool(
                job_worker is not None and getattr(job_worker, "is_running", False)
            )

            try:
                # 1. Pause all background loops so no SQLite connections touch the unlinked inode
                await self.shutdown_scheduler()
                if library_watcher is not None:
                    await library_watcher.shutdown()
                if job_worker is not None:
                    await job_worker.shutdown()

                # Final guard against race right before physical DB unlink
                if self._scanner.is_running:
                    raise MaintenanceBusyError(
                        "Cannot factory reset while a library scan is running."
                    )

                # 2. Physically delete DB (+ WAL/SHM), clear uploads contents, rebuild fresh DB
                removed_db_files, removed_uploads = await asyncio.to_thread(
                    self._rebuild_database_and_clear_uploads
                )

                # 3. Reset in-memory SSE brokers & log ring buffers (stay open for new subscribers)
                if job_broker is not None and hasattr(job_broker, "reset"):
                    job_broker.reset()
                self._library_broker.reset()
            finally:
                # 4. Always resume background loops against the newly initialized DB
                if was_worker_running and job_worker is not None:
                    job_worker.start()
                if was_watcher_running and library_watcher is not None:
                    library_watcher.start()
                if was_scheduler_running:
                    self.start_scheduler()

        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        logger.warning(
            "Factory reset completed: removed_db_files=%d removed_uploads=%d ms=%.1f",
            removed_db_files,
            removed_uploads,
            duration_ms,
        )
        return FactoryResetResponse(
            status="reset",
            removed_db_files=removed_db_files,
            removed_uploads=removed_uploads,
            duration_ms=duration_ms,
            message="MKV Forge app data reset. Your media files were not touched.",
        )

    async def _scheduler_loop(self) -> None:
        """Wait for poll intervals and trigger idle-only scheduled cleanup when due."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._scheduler_poll_sec,
                )
                break
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                break
            await self.maybe_run_scheduled_cleanup()
