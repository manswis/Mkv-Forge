"""Single-worker sequential SQLite job queue, cancellation, and startup crash recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import AppConstants, Settings
from app.db.repositories import JobRepository, MediaFileRepository
from app.models.schemas import JobConfig, ParsedTrackInfo
from app.services.folder_sync import FolderReconcileCache
from app.services.mkv_runner import JobCancelledError, execute_two_stage_pipeline
from app.services.probe_parser import parse_mkvmerge_json
from app.services.processing_policy import (
    ProcessingRuntimePolicy,
    list_managed_scratch_dirs,
    resolve_processing_policy,
)
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker
from app.services.upload_service import (
    UploadService,
    extract_upload_ids_from_tracks,
)
from app.services.user_settings import UserSettingsService
from app.utils.paths import (
    extract_folder,
    is_mkvforge_temp_file,
    safe_resolve_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)

_SPEED_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)x", re.IGNORECASE)
_SECONDS_PER_MINUTE: int = 60
_SECONDS_PER_HOUR: int = 3600


class DuplicateJobError(RuntimeError):
    """Raised when a file already has a queued or running job."""

    def __init__(self, existing_job_id: str, filepath: str) -> None:
        self.existing_job_id = existing_job_id
        self.filepath = filepath
        super().__init__(
            f"A job ({existing_job_id}) is already queued or running for {filepath}"
        )


class JobNotCancellableError(RuntimeError):
    """Raised when attempting to cancel a job that has already finished."""


def estimate_eta(
    duration_us: int,
    percent: int,
    speed_str: str | None,
) -> str | None:
    """Estimate human-readable remaining time from duration, progress, and FFmpeg speed."""
    if duration_us <= 0 or percent <= 0 or percent >= 100 or not speed_str:
        return None
    match = _SPEED_RE.search(speed_str)
    if match is None:
        return None
    multiplier = float(match.group(1))
    if multiplier <= 0:
        return None

    remaining_media_sec = (duration_us / AppConstants.US_PER_SEC) * (
        (100 - percent) / 100.0
    )
    eta_sec = max(0, int(round(remaining_media_sec / multiplier)))

    if eta_sec >= _SECONDS_PER_HOUR:
        hours, rem = divmod(eta_sec, _SECONDS_PER_HOUR)
        minutes = rem // _SECONDS_PER_MINUTE
        return f"{hours}h {minutes}m"
    if eta_sec >= _SECONDS_PER_MINUTE:
        minutes, seconds = divmod(eta_sec, _SECONDS_PER_MINUTE)
        return f"{minutes}m {seconds}s"
    return f"{eta_sec}s"


def sweep_orphaned_temp_files(storage_root: Path) -> int:
    """Recursively remove leftover `*.mkvforge.*` temp files inside `storage_root`."""
    if not storage_root.is_dir():
        return 0

    removed = 0
    for root_str, _dirnames, filenames in os.walk(storage_root, followlinks=False):
        current_dir = Path(root_str)
        for name in filenames:
            candidate = current_dir / name
            if not is_mkvforge_temp_file(candidate):
                continue
            try:
                candidate.unlink()
                removed += 1
                logger.info("Removed orphaned temp file: %s", candidate)
            except OSError as exc:
                logger.warning("Failed to remove orphaned temp file %s: %s", candidate, exc)
    return removed


async def terminate_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    grace_sec: float = AppConstants.SUBPROCESS_CANCEL_GRACE_SEC,
) -> None:
    """Send SIGTERM to a subprocess, falling back to SIGKILL after `grace_sec`."""
    if proc.returncode is not None:
        return

    try:
        proc.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_sec)
        return
    except asyncio.TimeoutError:
        logger.warning(
            "Subprocess PID %s did not exit within %.1fs after SIGTERM; sending SIGKILL",
            getattr(proc, "pid", "?"),
            grace_sec,
        )

    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


class _AsyncJobProgressPersister:
    """Serialized, throttled off-loop SQLite progress writer for a single running job.

    Guarantees:
    - All SQLite writes execute via `asyncio.to_thread`, never blocking the event loop.
    - At most ONE worker-thread write is in-flight at a time (coalescing bursts).
    - `close_and_drain()` marks the persister closed, discards pending ticks, and
      awaits any in-flight write BEFORE terminal state persistence (`mark_completed`,
      `mark_failed`, `mark_cancelled`) so a delayed tick can never race terminal status.
    """

    def __init__(
        self,
        job_repo: JobRepository,
        job_id: str,
        *,
        throttle_sec: float = AppConstants.JOB_PROGRESS_DB_THROTTLE_SEC,
        min_delta_pct: int = AppConstants.JOB_PROGRESS_DB_MIN_DELTA_PCT,
    ) -> None:
        self._job_repo = job_repo
        self._job_id = job_id
        self._throttle_sec = max(0.0, float(throttle_sec))
        self._min_delta_pct = max(1, int(min_delta_pct))
        self._closed = False
        self._pending: tuple[str | None, int] | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_scheduled_stage: str | None = None
        self._last_scheduled_percent: int = -1
        self._last_write_at: float = 0.0

    def schedule(
        self,
        stage: str | None,
        percent: int,
        *,
        force: bool = False,
    ) -> bool:
        """Queue a coalesced progress update if it crosses throttle/delta bounds."""
        if self._closed:
            return False

        bounded = max(0, min(100, int(percent)))
        now = time.monotonic()
        stage_changed = stage != self._last_scheduled_stage
        pct_delta = abs(bounded - self._last_scheduled_percent)
        elapsed = now - self._last_write_at

        if not (
            force
            or stage_changed
            or bounded == 100
            or self._last_scheduled_percent < 0
            or pct_delta >= self._min_delta_pct
            or elapsed >= self._throttle_sec
        ):
            return False

        if not force and not stage_changed and bounded == self._last_scheduled_percent:
            return False

        self._last_scheduled_stage = stage
        self._last_scheduled_percent = bounded
        self._last_write_at = now
        self._pending = (stage, bounded)

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain_loop())
        return True

    async def _drain_loop(self) -> None:
        while not self._closed and self._pending is not None:
            stage, percent = self._pending
            self._pending = None
            try:
                still_running = await asyncio.to_thread(
                    self._job_repo.update_progress,
                    self._job_id,
                    stage=stage,
                    progress=percent,
                )
                if not still_running:
                    self._closed = True
                    self._pending = None
                    return
            except Exception as exc:
                logger.warning(
                    "Non-fatal progress persist error for job %s: %s",
                    self._job_id,
                    exc,
                )

    async def close_and_drain(self) -> None:
        """Stop accepting updates and wait for any in-flight DB write to settle."""
        self._closed = True
        self._pending = None
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "Progress writer drain error for job %s: %s",
                    self._job_id,
                    exc,
                )


class JobWorker:
    """Single-worker sequential job processor with cancellation and crash recovery."""

    def __init__(
        self,
        *,
        settings: Settings,
        media_repo: MediaFileRepository,
        job_repo: JobRepository,
        scanner: LibraryScanner,
        broker: JobSSEBroker,
        upload_service: UploadService | None = None,
        folder_cache: FolderReconcileCache | None = None,
        user_settings_service: UserSettingsService | None = None,
        cancel_grace_sec: float = AppConstants.SUBPROCESS_CANCEL_GRACE_SEC,
    ) -> None:
        self._settings = settings
        self._media_repo = media_repo
        self._job_repo = job_repo
        self._scanner = scanner
        self._broker = broker
        self._upload_service = upload_service or UploadService(settings)
        self._folder_cache = folder_cache
        self._user_settings_service = user_settings_service
        self._cancel_grace_sec = cancel_grace_sec

        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

        self._active_job_id: str | None = None
        self._active_cancel_event: asyncio.Event | None = None
        self._active_done_event: asyncio.Event | None = None
        self._active_proc: asyncio.subprocess.Process | None = None

    @property
    def is_running(self) -> bool:
        """Return True if the background worker loop is active."""
        return self._worker_task is not None and not self._worker_task.done()

    @property
    def active_job_id(self) -> str | None:
        """Return the ID of the job currently being executed, if any."""
        return self._active_job_id

    def _extract_upload_ids_from_job_row(self, job_row: dict[str, Any]) -> list[str]:
        """Parse persisted `config_json` and return any staged upload IDs owned by the job."""
        raw_cfg = job_row.get("config_json")
        if not isinstance(raw_cfg, str) or not raw_cfg:
            return []
        try:
            cfg = JobConfig.model_validate(json.loads(raw_cfg))
        except Exception:
            return []
        return extract_upload_ids_from_tracks(cfg.external_tracks)

    def _resolve_processing_policy(self) -> ProcessingRuntimePolicy:
        """Load current system settings and compute per-job CPU/I/O & scratch policy."""
        if self._user_settings_service is None:
            return ProcessingRuntimePolicy()
        effective = self._user_settings_service.get(user_id=None)
        return resolve_processing_policy(effective, settings=self._settings)

    def _resolve_chapter_language(self) -> str:
        """Return preferred chapter XML language from user settings, falling back to `und`."""
        if self._user_settings_service is None:
            return AppConstants.CHAPTER_DEFAULT_LANGUAGE
        preferred = (
            self._user_settings_service.get(user_id=None).preferred_language or ""
        ).strip()
        return preferred or AppConstants.CHAPTER_DEFAULT_LANGUAGE

    async def recover_from_crash(self) -> tuple[int, int]:
        """Fail stale `running` jobs, sweep `*.mkvforge.*` temps (storage + scratch), and sweep orphan uploads."""
        stale_jobs = await asyncio.to_thread(
            self._job_repo.fail_stale_running_jobs,
            AppConstants.CRASH_RECOVERY_ERROR_MESSAGE,
        )
        removed_temps = await asyncio.to_thread(
            sweep_orphaned_temp_files,
            self._settings.storage_dir,
        )
        storage_scratch_folder = (
            self._user_settings_service.get(user_id=None).stage1_scratch_storage_folder
            if self._user_settings_service is not None
            else None
        )
        for scratch_dir in list_managed_scratch_dirs(
            self._settings,
            storage_folder=storage_scratch_folder,
        ):
            removed_temps += await asyncio.to_thread(
                sweep_orphaned_temp_files,
                scratch_dir,
            )
        queued_rows = await asyncio.to_thread(self._job_repo.list_queued)
        active_upload_ids: set[str] = set()
        for row in queued_rows:
            active_upload_ids.update(self._extract_upload_ids_from_job_row(row))
        removed_uploads = await asyncio.to_thread(
            self._upload_service.sweep_orphaned_uploads,
            active_upload_ids,
        )
        if stale_jobs or removed_temps or removed_uploads:
            logger.warning(
                "Crash recovery complete: marked %d stale running job(s) failed, "
                "removed %d orphaned temp file(s), removed %d orphaned upload(s)",
                stale_jobs,
                removed_temps,
                removed_uploads,
            )
        return stale_jobs, removed_temps

    def start(self) -> None:
        """Start the single background worker loop if not already started."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._wake_event.set()

    async def shutdown(self) -> None:
        """Stop the worker loop gracefully and terminate any active subprocess."""
        # Only set `_stop_event` (NOT `_active_cancel_event`) so `_execute_job`
        # distinguishes a container restart from an explicit user cancellation.
        self._stop_event.set()
        if self._active_proc is not None:
            await terminate_subprocess(
                self._active_proc,
                grace_sec=self._cancel_grace_sec,
            )

        self._wake_event.set()
        if self._worker_task is not None:
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        self._broker.close()

    async def enqueue_job(
        self,
        *,
        filepath: str,
        config: JobConfig,
        created_by_user_id: int | None = None,
        created_by_name: str = AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate path, enforce duplicate guard, persist queued job, and wake worker."""
        resolved = await asyncio.to_thread(
            safe_resolve_path, filepath, self._settings.storage_dir
        )
        rel_filepath = to_relative_storage_path(resolved, self._settings.storage_dir)

        existing = await asyncio.to_thread(
            self._job_repo.get_active_job_for_filepath, rel_filepath
        )
        if existing is not None:
            raise DuplicateJobError(str(existing["id"]), rel_filepath)

        job_id = str(uuid.uuid4())
        row = await asyncio.to_thread(
            self._job_repo.create_job,
            job_id=job_id,
            filepath=rel_filepath,
            config_json=config.model_dump_json(),
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
            batch_id=batch_id,
        )
        self._wake_event.set()
        return row

    async def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        """Cancel a queued or currently running job.

        Returns the updated job row, or `None` if `job_id` does not exist.
        Raises `JobNotCancellableError` if the job is already terminal.
        """
        job_row = await asyncio.to_thread(self._job_repo.get_by_id, job_id)
        if job_row is None:
            return None

        status = str(job_row["status"])
        if status not in (
            AppConstants.JOB_STATUS_QUEUED,
            AppConstants.JOB_STATUS_RUNNING,
        ):
            raise JobNotCancellableError(
                f"Job {job_id} cannot be cancelled in status {status!r}"
            )

        if status == AppConstants.JOB_STATUS_QUEUED and job_id != self._active_job_id:
            await asyncio.to_thread(
                self._job_repo.mark_cancelled,
                job_id,
                error_message=AppConstants.CANCELLED_BY_USER_MESSAGE,
            )
            # Guard against queued -> running race: if the worker loop claimed
            # this job concurrently, fall through to abort the live execution.
            if job_id != self._active_job_id:
                queued_uploads = self._extract_upload_ids_from_job_row(job_row)
                if queued_uploads:
                    await asyncio.to_thread(
                        self._upload_service.cleanup_uploads, queued_uploads
                    )
                return await asyncio.to_thread(self._job_repo.get_by_id, job_id)

        done_event: asyncio.Event | None = None
        if job_id == self._active_job_id:
            done_event = self._active_done_event
            if self._active_cancel_event is not None:
                self._active_cancel_event.set()
            proc = self._active_proc
            if proc is not None:
                await terminate_subprocess(
                    proc,
                    grace_sec=self._cancel_grace_sec,
                )

        if done_event is not None:
            try:
                await asyncio.wait_for(
                    done_event.wait(),
                    timeout=self._cancel_grace_sec,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for cancelled job %s cleanup to settle",
                    job_id,
                )

        await asyncio.to_thread(
            self._job_repo.mark_cancelled,
            job_id,
            error_message=AppConstants.CANCELLED_BY_USER_MESSAGE,
        )
        return await asyncio.to_thread(self._job_repo.get_by_id, job_id)

    async def _worker_loop(self) -> None:
        """Sequentially pull and execute queued jobs one at a time."""
        while not self._stop_event.is_set():
            job_row = await asyncio.to_thread(self._job_repo.claim_next_queued_job)
            if job_row is None:
                self._wake_event.clear()
                if self._stop_event.is_set():
                    break
                await self._wake_event.wait()
                continue

            await self._execute_job(job_row)

    async def _execute_job(self, job_row: dict[str, Any]) -> None:
        """Run one claimed job end-to-end with cancel checks, progress, and persistence."""
        job_id = str(job_row["id"])
        rel_filepath = str(job_row["filepath"])
        cancel_event = asyncio.Event()
        done_event = asyncio.Event()

        self._active_job_id = job_id
        self._active_cancel_event = cancel_event
        self._active_done_event = done_event
        self._active_proc = None

        current_stage: str | None = None
        current_stage_label: str | None = "Starting job"
        last_reported_percent: int = 0
        progress_persister = _AsyncJobProgressPersister(self._job_repo, job_id)

        media_row = await asyncio.to_thread(
            self._media_repo.get_by_filepath, rel_filepath
        )
        duration_us = int(media_row["duration_us"]) if media_row is not None else 0

        self._broker.start_job(
            job_id=job_id,
            filepath=rel_filepath,
            stage=current_stage,
            stage_label=current_stage_label,
        )

        def _check_cancelled() -> None:
            if cancel_event.is_set() or self._stop_event.is_set():
                raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)

        def _on_stage(stage: str, stage_label: str) -> None:
            nonlocal current_stage, current_stage_label, last_reported_percent
            current_stage = stage
            current_stage_label = stage_label
            last_reported_percent = 0
            progress_persister.schedule(stage, 0, force=True)
            self._broker.publish_progress(
                job_id=job_id,
                filepath=rel_filepath,
                stage=stage,
                stage_label=stage_label,
                percent=0,
            )

        def _on_progress(percent: int, speed: str | None) -> None:
            nonlocal last_reported_percent
            bounded = max(0, min(100, int(percent)))
            last_reported_percent = bounded
            progress_persister.schedule(current_stage, bounded)
            self._broker.publish_progress(
                job_id=job_id,
                filepath=rel_filepath,
                stage=current_stage,
                stage_label=current_stage_label,
                percent=bounded,
                speed=speed,
                eta=estimate_eta(duration_us, bounded, speed),
            )

        def _on_log(line: str) -> None:
            self._broker.publish_log(
                job_id=job_id,
                filepath=rel_filepath,
                log_line=line,
            )

        def _on_process_started(proc: asyncio.subprocess.Process) -> None:
            self._active_proc = proc
            if cancel_event.is_set() or self._stop_event.is_set():
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass

        try:
            _check_cancelled()
            config = JobConfig.model_validate(json.loads(str(job_row["config_json"])))
            source_path = await asyncio.to_thread(
                safe_resolve_path, rel_filepath, self._settings.storage_dir
            )
            processing_policy = await asyncio.to_thread(
                self._resolve_processing_policy
            )
            chapter_language = await asyncio.to_thread(
                self._resolve_chapter_language
            )
            source_tracks: list[ParsedTrackInfo] | None = None
            if media_row is not None and media_row.get("raw_json"):
                try:
                    parsed_probe = parse_mkvmerge_json(
                        json.loads(str(media_row["raw_json"]))
                    )
                    source_tracks = parsed_probe.tracks
                except Exception:
                    source_tracks = None

            result = await execute_two_stage_pipeline(
                source_path,
                config,
                storage_root=self._settings.storage_dir,
                duration_us=duration_us,
                media_repo=self._media_repo,
                scanner=self._scanner,
                upload_service=self._upload_service,
                folder_cache=self._folder_cache,
                processing_policy=processing_policy,
                source_tracks=source_tracks,
                chapter_language=chapter_language,
                on_stage=_on_stage,
                on_progress=_on_progress,
                on_log=_on_log,
                on_process_started=_on_process_started,
                check_cancelled=_check_cancelled,
            )

            await progress_persister.close_and_drain()
            await asyncio.to_thread(
                self._job_repo.mark_completed,
                job_id,
                output_filepath=result.output_rel_filepath,
                warnings=result.warnings,
            )
            if self._folder_cache is not None:
                self._folder_cache.invalidate_folder(
                    extract_folder(rel_filepath), include_parent=True
                )
                self._folder_cache.invalidate_folder(
                    extract_folder(result.output_rel_filepath), include_parent=True
                )
            self._broker.publish_terminal(
                job_id=job_id,
                filepath=rel_filepath,
                status=AppConstants.JOB_STATUS_COMPLETED,
                stage=current_stage,
                stage_label="Completed",
                percent=100,
                warnings=result.warnings,
            )
        except JobCancelledError:
            await progress_persister.close_and_drain()
            interrupted_by_shutdown = (
                self._stop_event.is_set() and not cancel_event.is_set()
            )
            if interrupted_by_shutdown:
                await asyncio.to_thread(
                    self._job_repo.mark_failed,
                    job_id,
                    error_message=AppConstants.CRASH_RECOVERY_ERROR_MESSAGE,
                )
                self._broker.publish_terminal(
                    job_id=job_id,
                    filepath=rel_filepath,
                    status=AppConstants.JOB_STATUS_FAILED,
                    stage=current_stage,
                    stage_label="Interrupted",
                    percent=max(0, last_reported_percent),
                    error_message=AppConstants.CRASH_RECOVERY_ERROR_MESSAGE,
                )
            else:
                await asyncio.to_thread(
                    self._job_repo.mark_cancelled,
                    job_id,
                    error_message=AppConstants.CANCELLED_BY_USER_MESSAGE,
                )
                self._broker.publish_terminal(
                    job_id=job_id,
                    filepath=rel_filepath,
                    status=AppConstants.JOB_STATUS_CANCELLED,
                    stage=current_stage,
                    stage_label="Cancelled",
                    percent=max(0, last_reported_percent),
                    error_message=AppConstants.CANCELLED_BY_USER_MESSAGE,
                )
        except Exception as exc:
            await progress_persister.close_and_drain()
            error_msg = str(exc) or exc.__class__.__name__
            logger.error("Job %s failed for %s: %s", job_id, rel_filepath, error_msg)
            await asyncio.to_thread(
                self._job_repo.mark_failed,
                job_id,
                error_message=error_msg,
            )
            self._broker.publish_terminal(
                job_id=job_id,
                filepath=rel_filepath,
                status=AppConstants.JOB_STATUS_FAILED,
                stage=current_stage,
                stage_label="Failed",
                percent=max(0, last_reported_percent),
                error_message=error_msg,
            )
        finally:
            await progress_persister.close_and_drain()
            self._active_proc = None
            self._active_cancel_event = None
            self._active_done_event = None
            self._active_job_id = None
            done_event.set()
