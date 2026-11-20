"""Unit and behavioral tests for JobWorker, cancellation guardrails, crash recovery, and SQLite busy_timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
)
from app.models.schemas import (
    AudioConversionConfig,
    JobConfig,
    TrackEditConfig,
)
from app.services.ffmpeg_runner import FFmpegTranscodeResult
from app.services.job_worker import (
    DuplicateJobError,
    JobNotCancellableError,
    JobWorker,
    estimate_eta,
    sweep_orphaned_temp_files,
    terminate_subprocess,
)
from app.services.mkv_runner import MkvRemuxResult, PipelineExecutionResult
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker


def _basic_config(*, convert_audio: bool = False) -> JobConfig:
    return JobConfig(
        audio_conversion=AudioConversionConfig(
            enabled=convert_audio,
            source_track_id=1,
            source_audio_index=0,
            target_codec="eac3",
            bitrate="640k",
        ),
        tracks=[
            TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
            TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
        ],
        output_mode="replace",
    )


def _make_worker(
    test_settings: Settings,
    db_manager: DatabaseManager,
    *,
    cancel_grace_sec: float = 0.2,
) -> tuple[JobWorker, JobRepository, JobSSEBroker]:
    media_repo = MediaFileRepository(db_manager)
    state_repo = AppStateRepository(db_manager)
    job_repo = JobRepository(db_manager)
    scanner = LibraryScanner(test_settings, media_repo, state_repo)
    broker = JobSSEBroker()
    worker = JobWorker(
        settings=test_settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        broker=broker,
        cancel_grace_sec=cancel_grace_sec,
    )
    return worker, job_repo, broker


class TestGuardrail3AndHelpers:
    """Verify SQLite busy_timeout=5000, ETA estimation, temp sweep, and SIGTERM->SIGKILL."""

    def test_sqlite_busy_timeout_is_5000ms(self, db_manager: DatabaseManager) -> None:
        with db_manager.connect() as conn:
            busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert int(busy_timeout) == AppConstants.SQLITE_BUSY_TIMEOUT_MS
        assert int(busy_timeout) == 5000

    @pytest.mark.parametrize(
        ("duration_us", "percent", "speed", "expected"),
        [
            (1_000_000_000, 50, "10.0x", "50s"),
            (3_600_000_000, 50, "10.0x", "3m 0s"),
            (7_200_000_000, 10, "1.0x", "1h 48m"),
            (0, 50, "10.0x", None),
            (1_000_000_000, 0, "10.0x", None),
            (1_000_000_000, 100, "10.0x", None),
            (1_000_000_000, 50, "N/A", None),
        ],
    )
    def test_estimate_eta(
        self, duration_us: int, percent: int, speed: str | None, expected: str | None
    ) -> None:
        assert estimate_eta(duration_us, percent, speed) == expected

    def test_sweep_orphaned_temp_files_only_deletes_mkvforge_temps(
        self, tmp_path: Path
    ) -> None:
        nested = tmp_path / "movies"
        nested.mkdir()
        keep = nested / "Dune.mkv"
        tmp1 = nested / "Dune.mkv.mkvforge.tmp"
        tmp2 = nested / "Dune.mkv.mkvforge.eac3"
        keep.write_bytes(b"keep")
        tmp1.write_bytes(b"orphan1")
        tmp2.write_bytes(b"orphan2")

        removed = sweep_orphaned_temp_files(tmp_path)
        assert removed == 2
        assert keep.exists()
        assert not tmp1.exists()
        assert not tmp2.exists()
        assert sweep_orphaned_temp_files(tmp_path / "does_not_exist") == 0

    async def test_terminate_subprocess_sigterm_then_sigkill_fallback(self) -> None:
        # 1. Clean exit on SIGTERM
        clean_proc = MagicMock()
        clean_proc.returncode = None
        clean_proc.terminate = MagicMock()
        clean_proc.kill = MagicMock()
        clean_proc.wait = AsyncMock(return_value=-15)

        await terminate_subprocess(clean_proc, grace_sec=0.1)
        clean_proc.terminate.assert_called_once()
        clean_proc.kill.assert_not_called()

        # 2. Stubborn process ignores SIGTERM -> falls back to SIGKILL
        stubborn = MagicMock()
        stubborn.returncode = None
        stubborn.pid = 99999
        stubborn.terminate = MagicMock()
        stubborn.kill = MagicMock()

        wait_calls = 0

        async def _slow_then_die() -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.sleep(5)
            return -9

        stubborn.wait = AsyncMock(side_effect=_slow_then_die)
        await terminate_subprocess(stubborn, grace_sec=0.05)
        stubborn.terminate.assert_called_once()
        stubborn.kill.assert_called_once()


class TestCrashRecoveryAndQueueSequencing:
    """Verify startup crash recovery, FIFO sequencing, and duplicate guard."""

    async def test_recover_from_crash_fails_stale_running_and_sweeps_temps(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        job_repo.create_job(job_id="stale-1", filepath="a.mkv", config_json="{}")
        job_repo.claim_next_queued_job()  # marks stale-1 as running
        job_repo.create_job(job_id="queued-2", filepath="b.mkv", config_json="{}")

        orphan = test_settings.storage_dir / "a.mkv.mkvforge.tmp"
        orphan.write_bytes(b"orphan")

        stale_count, temp_count = await worker.recover_from_crash()

        assert stale_count == 1
        assert temp_count == 1
        assert not orphan.exists()

        stale_row = job_repo.get_by_id("stale-1")
        assert stale_row is not None
        assert stale_row["status"] == AppConstants.JOB_STATUS_FAILED
        assert stale_row["error_message"] == AppConstants.CRASH_RECOVERY_ERROR_MESSAGE

        queued_row = job_repo.get_by_id("queued-2")
        assert queued_row is not None
        assert queued_row["status"] == AppConstants.JOB_STATUS_QUEUED

    async def test_sequential_fifo_execution_and_duplicate_guard(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        f1 = test_settings.storage_dir / "first.mkv"
        f2 = test_settings.storage_dir / "second.mkv"
        f1.write_bytes(b"111")
        f2.write_bytes(b"222")

        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        execution_order: list[str] = []
        active_concurrency = 0
        max_concurrency = 0

        async def _fake_pipeline(source_path: Path, _cfg, **kwargs):
            nonlocal active_concurrency, max_concurrency
            active_concurrency += 1
            max_concurrency = max(max_concurrency, active_concurrency)
            execution_order.append(source_path.name)
            kwargs["on_stage"]("remux", "Remuxing MKV")
            kwargs["on_progress"](50, None)
            await asyncio.sleep(0.03)
            active_concurrency -= 1
            return PipelineExecutionResult(
                output_path=source_path,
                output_rel_filepath=source_path.name,
                mkvmerge_exit_code=1,
                warnings=["Minor warning"],
            )

        with patch(
            "app.services.job_worker.execute_two_stage_pipeline",
            new=AsyncMock(side_effect=_fake_pipeline),
        ):
            j1 = await worker.enqueue_job(filepath="first.mkv", config=_basic_config())
            j2 = await worker.enqueue_job(filepath="second.mkv", config=_basic_config())

            # Duplicate enqueue for same file must raise DuplicateJobError
            with pytest.raises(DuplicateJobError):
                await worker.enqueue_job(filepath="first.mkv", config=_basic_config())

            worker.start()
            # Wait until both finish
            for _ in range(100):
                r1 = job_repo.get_by_id(j1["id"])
                r2 = job_repo.get_by_id(j2["id"])
                if (
                    r1["status"] == AppConstants.JOB_STATUS_COMPLETED
                    and r2["status"] == AppConstants.JOB_STATUS_COMPLETED
                ):
                    break
                await asyncio.sleep(0.01)

            await worker.shutdown()

        assert execution_order == ["first.mkv", "second.mkv"]
        assert max_concurrency == 1
        assert job_repo.get_by_id(j1["id"])["status"] == AppConstants.JOB_STATUS_COMPLETED
        assert job_repo.get_by_id(j2["id"])["status"] == AppConstants.JOB_STATUS_COMPLETED


class TestCancelGuardrailsAndGracefulShutdown:
    """Verify Guardrail #1 (cancel between stages & pre-swap) and Guardrail #2 (shutdown)."""

    async def test_cancel_between_stage1_and_stage2_prevents_remux_and_cleans_temp(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        src = test_settings.storage_dir / "Movie.mkv"
        src.write_bytes(b"original-movie")

        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        stage1_started = asyncio.Event()
        stage1_release = asyncio.Event()
        stage2_mock = AsyncMock()

        async def _fake_ffmpeg(_src, tmp_audio, **_kwargs):
            tmp_audio.write_bytes(b"temp-eac3")
            stage1_started.set()
            await stage1_release.wait()
            return FFmpegTranscodeResult(
                temp_audio_path=tmp_audio,
                channels=6,
                effective_duration_us=10_000_000,
                log_lines=[],
            )

        with (
            patch("app.services.mkv_runner.run_ffmpeg_transcode", new=AsyncMock(side_effect=_fake_ffmpeg)),
            patch("app.services.mkv_runner.run_mkvmerge_remux", new=stage2_mock),
        ):
            job = await worker.enqueue_job(
                filepath="Movie.mkv", config=_basic_config(convert_audio=True)
            )
            worker.start()
            await asyncio.wait_for(stage1_started.wait(), timeout=2.0)

            # Request cancel while Stage 1 is finishing
            await worker.cancel_job(job["id"])
            stage1_release.set()

            for _ in range(100):
                if worker.active_job_id is None:
                    break
                await asyncio.sleep(0.01)
            await worker.shutdown()

        # Guardrail #1: Stage 2 NEVER ran, original untouched, temp audio cleaned up!
        stage2_mock.assert_not_called()
        assert src.read_bytes() == b"original-movie"
        assert not (test_settings.storage_dir / "Movie.mkv.mkvforge.eac3").exists()
        assert job_repo.get_by_id(job["id"])["status"] == AppConstants.JOB_STATUS_CANCELLED

    async def test_cancel_immediately_before_atomic_swap_never_overwrites_file(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        src = test_settings.storage_dir / "NoOverwrite.mkv"
        src.write_bytes(b"original-bytes")

        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        remux_started = asyncio.Event()
        remux_release = asyncio.Event()

        async def _fake_mkvmerge(_src, out_tmp, _cfg, **_kwargs):
            out_tmp.write_bytes(b"new-remux-must-not-swap")
            remux_started.set()
            await remux_release.wait()
            return MkvRemuxResult(output_tmp_path=out_tmp, exit_code=0)

        with patch(
            "app.services.mkv_runner.run_mkvmerge_remux",
            new=AsyncMock(side_effect=_fake_mkvmerge),
        ):
            job = await worker.enqueue_job(
                filepath="NoOverwrite.mkv", config=_basic_config(convert_audio=False)
            )
            worker.start()
            await asyncio.wait_for(remux_started.wait(), timeout=2.0)

            # Cancel right as Stage 2 finishes, BEFORE atomic swap
            await worker.cancel_job(job["id"])
            remux_release.set()

            for _ in range(100):
                if worker.active_job_id is None:
                    break
                await asyncio.sleep(0.01)
            await worker.shutdown()

        # Original file MUST be completely untouched and temp remux deleted!
        assert src.read_bytes() == b"original-bytes"
        assert not (test_settings.storage_dir / "NoOverwrite.mkv.mkvforge.tmp").exists()
        assert job_repo.get_by_id(job["id"])["status"] == AppConstants.JOB_STATUS_CANCELLED

    async def test_graceful_shutdown_terminates_active_subprocess(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        src = test_settings.storage_dir / "LongJob.mkv"
        src.write_bytes(b"data")

        worker, _job_repo, _ = _make_worker(test_settings, db_manager)
        proc_started = asyncio.Event()
        proc_exited = asyncio.Event()

        fake_proc = MagicMock()
        fake_proc.returncode = None

        def _term() -> None:
            fake_proc.returncode = -15
            proc_exited.set()

        fake_proc.terminate = MagicMock(side_effect=_term)
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock(return_value=-15)

        async def _hanging_pipeline(_src, _cfg, **kwargs):
            kwargs["on_process_started"](fake_proc)
            proc_started.set()
            await proc_exited.wait()
            kwargs["check_cancelled"]()
            return PipelineExecutionResult(
                output_path=src,
                output_rel_filepath="LongJob.mkv",
                mkvmerge_exit_code=0,
            )

        with patch(
            "app.services.job_worker.execute_two_stage_pipeline",
            new=AsyncMock(side_effect=_hanging_pipeline),
        ):
            job = await worker.enqueue_job(filepath="LongJob.mkv", config=_basic_config())
            worker.start()
            await asyncio.wait_for(proc_started.wait(), timeout=2.0)

            # Guardrail #2: shutdown cleanly terminates active subprocess AND
            # marks the job failed ("Interrupted by container restart"), NOT user-cancelled.
            await worker.shutdown()

        fake_proc.terminate.assert_called_once()
        assert worker.is_running is False
        row = _job_repo.get_by_id(job["id"])
        assert row is not None
        assert row["status"] == AppConstants.JOB_STATUS_FAILED
        assert row["error_message"] == AppConstants.CRASH_RECOVERY_ERROR_MESSAGE

    async def test_cancel_queued_job_racing_into_active_still_aborts_worker(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        """If a queued job is claimed concurrently with cancel_job(), the live worker still aborts."""
        src = test_settings.storage_dir / "RaceCancel.mkv"
        src.write_bytes(b"original-race")

        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        job = await worker.enqueue_job(filepath="RaceCancel.mkv", config=_basic_config())
        job_id = str(job["id"])

        # Simulate the exact race window: get_by_id() returned 'queued',
        # yet the worker has ALREADY claimed `job_id` as `_active_job_id`!
        cancel_event = asyncio.Event()
        done_event = asyncio.Event()
        fake_proc = MagicMock()
        fake_proc.returncode = None

        def _term() -> None:
            fake_proc.returncode = -15
            done_event.set()

        fake_proc.terminate = MagicMock(side_effect=_term)
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock(return_value=-15)

        worker._active_job_id = job_id
        worker._active_cancel_event = cancel_event
        worker._active_done_event = done_event
        worker._active_proc = fake_proc

        cancelled = await worker.cancel_job(job_id)
        assert cancelled is not None
        assert cancelled["status"] == AppConstants.JOB_STATUS_CANCELLED
        assert cancel_event.is_set() is True
        fake_proc.terminate.assert_called_once()

    async def test_cancel_terminal_job_raises_not_cancellable(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        worker, job_repo, _ = _make_worker(test_settings, db_manager)
        job_repo.create_job(job_id="done-1", filepath="a.mkv", config_json="{}")
        job_repo.mark_completed("done-1", output_filepath="a.mkv")

        with pytest.raises(JobNotCancellableError):
            await worker.cancel_job("done-1")
        assert await worker.cancel_job("nonexistent") is None
