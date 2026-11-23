"""Single-folder disk reconciliation for instant ghost pruning and background probing."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import AppConstants
from app.db.repositories import MediaFileRepository
from app.services.folder_service import (
    DirectoryChild,
    DirectoryOtherFile,
    FolderNotFoundError,
    normalize_storage_folder_path,
    scan_storage_folder_snapshot,
)
from app.utils.paths import PathSafetyError, extract_folder


@dataclass(frozen=True, slots=True)
class FolderReconcileResult:
    """Result of reconciling one storage folder against SQLite without extra disk walks."""

    folder_exists: bool
    subfolders: list[DirectoryChild]
    pruned_count: int
    probe_candidates: list[str]
    other_files: list[DirectoryOtherFile] = ()  # type: ignore[assignment]


_FolderCacheKey = tuple[str, str, bool, bool, tuple[str, ...]]
_ReconcileFn = Callable[..., FolderReconcileResult]


def _parent_folder(normalized_folder: str) -> str:
    """Return the immediate parent display folder for a normalized storage folder."""
    if not normalized_folder or normalized_folder == "/":
        return "/"
    trimmed = normalized_folder.rstrip("/")
    idx = trimmed.rfind("/")
    return "/" if idx <= 0 else trimmed[:idx]


class FolderReconcileCache:
    """Thread-safe single-flight + short-TTL cache for `reconcile_folder_snapshot`.

    - Coalesces concurrent requests for the same `(storage_dir, folder, flags)`
      so N parallel `GET /api/files` calls trigger only ONE `os.scandir` pass.
    - Serves fresh snapshots within `ttl_sec` (default 2.0s).
    - Supports immediate, generation-guarded invalidation on any folder mutation
      so writes never reflect stale cached directory contents and in-flight
      pre-mutation scans cannot repopulate the cache after invalidation.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = AppConstants.FOLDER_RECONCILE_CACHE_TTL_SEC,
        max_entries: int = AppConstants.FOLDER_RECONCILE_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_sec = max(0.0, float(ttl_sec))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[_FolderCacheKey, tuple[float, FolderReconcileResult]] = {}
        self._in_flight: dict[_FolderCacheKey, asyncio.Task[FolderReconcileResult]] = {}
        self._folder_versions: dict[str, int] = {}
        self._global_version: int = 0

    def _current_version_locked(self, normalized_folder: str) -> tuple[int, int]:
        return (self._global_version, self._folder_versions.get(normalized_folder, 0))

    def _evict_if_needed_locked(self, now: float) -> None:
        if len(self._entries) < self._max_entries:
            return
        expired = [
            key
            for key, (expires_at, _val) in self._entries.items()
            if expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) >= self._max_entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest_key, None)

    def _prune_folder_versions_if_needed_locked(self) -> None:
        """Bound `_folder_versions` memory without weakening active version guards."""
        if len(self._folder_versions) > self._max_entries:
            self._global_version += 1
            self._folder_versions.clear()

    def invalidate_folder(
        self,
        raw_folder: str,
        *,
        include_parent: bool = False,
        include_descendants: bool = False,
    ) -> None:
        """Immediately invalidate cached snapshots for `raw_folder` (thread-safe)."""
        try:
            normalized = normalize_storage_folder_path(raw_folder)
        except PathSafetyError:
            self.invalidate_all()
            return

        targets: set[str] = {normalized}
        if include_parent:
            targets.add(_parent_folder(normalized))

        prefix = f"{normalized}/" if normalized != "/" else "/"
        with self._lock:
            for folder in targets:
                self._folder_versions[folder] = (
                    self._folder_versions.get(folder, 0) + 1
                )

            def _matches(entry_folder: str) -> bool:
                if entry_folder in targets:
                    return True
                if include_descendants:
                    if normalized == "/":
                        return True
                    return entry_folder.startswith(prefix)
                return False

            for key in [k for k in self._entries if _matches(k[1])]:
                self._entries.pop(key, None)
            for key in [k for k in self._in_flight if _matches(k[1])]:
                self._in_flight.pop(key, None)
            if include_descendants:
                for folder in list(self._folder_versions.keys()):
                    if _matches(folder) and folder not in targets:
                        self._folder_versions[folder] += 1
            self._prune_folder_versions_if_needed_locked()

    def invalidate_for_filepath(self, raw_filepath: str) -> None:
        """Invalidate the folder containing `raw_filepath` plus its parent (thread-safe)."""
        cleaned = (raw_filepath or "").strip()
        if not cleaned or cleaned == "/":
            self.invalidate_all()
            return
        folder = extract_folder(cleaned.lstrip("/"))
        self.invalidate_folder(folder, include_parent=True, include_descendants=False)
        # Also invalidate if `cleaned` itself refers to a folder path
        as_folder = f"/{cleaned.strip('/')}"
        if as_folder != folder:
            self.invalidate_folder(
                as_folder, include_parent=True, include_descendants=True
            )

    def invalidate_all(self) -> None:
        """Invalidate every cached folder snapshot immediately (thread-safe)."""
        with self._lock:
            self._global_version += 1
            self._folder_versions.clear()
            self._entries.clear()
            self._in_flight.clear()

    @staticmethod
    def _consume_orphan_task_exception(
        done_task: asyncio.Task[FolderReconcileResult],
    ) -> None:
        """Retrieve exception if all shielded waiters left, preventing unretrieved-task warnings."""
        if not done_task.cancelled():
            done_task.exception()

    async def get_or_reconcile(
        self,
        raw_folder: str,
        *,
        storage_dir: Path,
        media_repo: MediaFileRepository,
        check_grand_children: bool = True,
        show_media_files_only: bool = True,
        visible_extensions: frozenset[str] = AppConstants.SUPPORTED_MEDIA_EXTENSIONS,
        reconcile_fn: _ReconcileFn | None = None,
    ) -> FolderReconcileResult:
        """Return a cached or single-flight reconciled snapshot for `raw_folder`.

        Executes each single-flight scan inside a detached `asyncio.Task` awaited via
        `asyncio.shield(task)` so cancelling one caller never cancels or crashes
        innocent coalesced waiters, and `_in_flight[key]` is only removed by the
        detached task's own `finally` block.
        """
        runner = reconcile_fn if reconcile_fn is not None else reconcile_folder_snapshot
        try:
            normalized = normalize_storage_folder_path(raw_folder)
        except PathSafetyError:
            return FolderReconcileResult(
                folder_exists=False,
                subfolders=[],
                pruned_count=0,
                probe_candidates=[],
            )

        key: _FolderCacheKey = (
            str(storage_dir),
            normalized,
            bool(check_grand_children),
            bool(show_media_files_only),
            tuple(sorted(visible_extensions)),
        )
        loop = asyncio.get_running_loop()

        with self._lock:
            now = self._clock()
            cached = self._entries.get(key)
            if cached is not None:
                expires_at, cached_result = cached
                if now < expires_at:
                    return cached_result
                self._entries.pop(key, None)

            existing_task = self._in_flight.get(key)
            if existing_task is not None and not existing_task.done():
                task = existing_task
            else:
                start_version = self._current_version_locked(normalized)

                async def _run_and_cache() -> FolderReconcileResult:
                    try:
                        result = await asyncio.to_thread(
                            runner,
                            normalized,
                            storage_dir=storage_dir,
                            media_repo=media_repo,
                            check_grand_children=check_grand_children,
                            show_media_files_only=show_media_files_only,
                            visible_extensions=visible_extensions,
                        )
                    finally:
                        with self._lock:
                            if self._in_flight.get(key) is task:
                                self._in_flight.pop(key, None)

                    with self._lock:
                        # Only populate cache if no mutation invalidated this folder while scanning
                        if (
                            self._ttl_sec > 0.0
                            and self._current_version_locked(normalized) == start_version
                        ):
                            now_done = self._clock()
                            self._evict_if_needed_locked(now_done)
                            self._entries[key] = (now_done + self._ttl_sec, result)
                    return result

                task = loop.create_task(_run_and_cache())
                task.add_done_callback(self._consume_orphan_task_exception)
                self._in_flight[key] = task

        return await asyncio.shield(task)


def reconcile_folder_snapshot(
    raw_folder: str,
    *,
    storage_dir: Path,
    media_repo: MediaFileRepository,
    check_grand_children: bool = True,
    show_media_files_only: bool = True,
    visible_extensions: frozenset[str] = AppConstants.SUPPORTED_MEDIA_EXTENSIONS,
) -> FolderReconcileResult:
    """Reconcile immediate files/subfolders of `raw_folder` in ONE `os.scandir` pass.

    - Never mutates SQLite if `storage_dir` itself is unmounted/missing.
    - Prunes vanished rows immediately so refreshed folders never show ghost files.
    - Returns new or modified filepaths for non-blocking background probing.
    """
    try:
        normalized = normalize_storage_folder_path(raw_folder)
    except PathSafetyError:
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    if not storage_dir.is_dir():
        return FolderReconcileResult(
            folder_exists=True,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    try:
        snapshot = scan_storage_folder_snapshot(
            normalized,
            storage_dir,
            check_grand_children=check_grand_children,
            show_media_files_only=show_media_files_only,
            visible_extensions=visible_extensions,
        )
    except FolderNotFoundError:
        pruned = 0
        if normalized != "/":
            pruned = len(media_repo.delete_by_folder_prefix(normalized))
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=pruned,
            probe_candidates=[],
        )
    except PathSafetyError:
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    db_states = media_repo.list_direct_file_states(snapshot.path)
    disk_map = {entry.rel_filepath: entry for entry in snapshot.media_files}

    vanished_paths = [path for path in db_states if path not in disk_map]
    pruned_count = (
        media_repo.delete_by_filepaths(vanished_paths) if vanished_paths else 0
    )

    probe_candidates: list[str] = []
    for rel_path, entry in disk_map.items():
        if entry.size_bytes <= 0:
            continue
        existing = db_states.get(rel_path)
        if existing is None:
            # Phase A: Insert instant 'scanning' placeholder so the file appears immediately
            media_repo.upsert_scanning_placeholder(
                filepath=rel_path,
                folder=snapshot.path,
                filename=Path(rel_path).name,
                size_bytes=entry.size_bytes,
                mtime=entry.mtime,
            )
            probe_candidates.append(rel_path)
            continue
        _file_id, db_size, db_mtime, db_status = existing
        if (
            db_status == AppConstants.FILE_SCAN_STATUS_SCANNING
            or db_size != entry.size_bytes
            or abs(db_mtime - entry.mtime) > AppConstants.MTIME_TOLERANCE_SEC
        ):
            probe_candidates.append(rel_path)

    return FolderReconcileResult(
        folder_exists=True,
        subfolders=snapshot.subfolders,
        pruned_count=pruned_count,
        probe_candidates=probe_candidates,
        other_files=snapshot.other_files,
    )
