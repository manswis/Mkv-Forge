"""Unit tests for serialized concurrent watcher probing, active-job exclusion, and ionice fallback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from watchfiles import Change

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, JobRepository, MediaFileRepository
from app.services.library_events import LibraryEventBroker
from app.services.scanner import LibraryScanner, ProbeError, run_mkvmerge_probe
from app.services.watcher import (
    LibraryWatcher,
    _is_path_owned_by_active_job,
    is_temp_or_partial_rel_path,
)


def _fake_probe_payload() -> dict:
    return {
        "container": {"properties": {"duration": 5_000_000_000, "title": ""}},
        "global_tags": [],
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "HEVC",
                "properties": {"pixel_dimensions": "1920x1080"},
            },
            {
                "id": 1,
                "type": "audio",
                "codec": "EAC3",
                "properties": {"audio_channels": 6, "language": "eng"},
            },
        ],
    }


def _make_watcher_env(
    tmp_path: Path, *, debounce_sec: float = 0.05
) -> tuple[
    LibraryWatcher,
    LibraryEventBroker,
    MediaFileRepository,
    JobRepository,
    Settings,
]:
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    static = tmp_path / "static"
    storage.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    static.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        storage_dir=storage,
        data_dir=data,
        static_dir=static,
        health_timeout_sec=2.0,
        scan_timeout_sec=5.0,
        log_level="INFO",
        watch_mode="watch",
        watch_debounce_sec=debounce_sec,
        scan_interval_min=0,
    )
    db = DatabaseManager(data / AppConstants.DB_FILENAME)
    db.initialize()
    media_repo = MediaFileRepository(db)
    state_repo = AppStateRepository(db)
    job_repo = JobRepository(db)
    scanner = LibraryScanner(settings, media_repo, state_repo)
    broker = LibraryEventBroker(coalesce_sec=0.02)
    watcher = LibraryWatcher(
        settings=settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        library_broker=broker,
    )
    return watcher, broker, media_repo, job_repo, settings


class TestGuardrail1ActiveJobOwnershipAndTempFiltering:
    """Verify strict active-job exclusion across all output modes and temp/partial paths."""

    def test_owned_guard_covers_source_output_save_as_mp4_replace_and_keep_both(self) -> None:
        # 1. Source + explicit output_filepath + save_as output_path
        save_as_job = {
            "filepath": "movies/Source.mkv",
            "output_filepath": "movies/Rendered.mkv",
            "config_json": json.dumps(
                {"output_mode": "save_as", "output_path": "/archive/Saved.mkv"}
            ),
        }
        assert _is_path_owned_by_active_job("movies/Source.mkv", save_as_job) is True
        assert _is_path_owned_by_active_job("/movies/Source.mkv", save_as_job) is True
        assert _is_path_owned_by_active_job("movies/Rendered.mkv", save_as_job) is True
        assert _is_path_owned_by_active_job("archive/Saved.mkv", save_as_job) is True
        assert _is_path_owned_by_active_job("movies/Unrelated.mkv", save_as_job) is False

        # 2. MP4 replace target (<stem>.mkv in same folder)
        mp4_replace_job = {
            "filepath": "shows/Ep01.mp4",
            "output_filepath": None,
            "config_json": json.dumps({"output_mode": "replace"}),
        }
        assert _is_path_owned_by_active_job("shows/Ep01.mp4", mp4_replace_job) is True
        assert _is_path_owned_by_active_job("shows/Ep01.mkv", mp4_replace_job) is True
        assert _is_path_owned_by_active_job("other/Ep01.mkv", mp4_replace_job) is False

        # 3. Keep-Both targets (<stem>.remux.mkv and <stem>.remux.<N>.mkv in same folder)
        keep_both_job = {
            "filepath": "movies/Dune.mkv",
            "output_filepath": None,
            "config_json": json.dumps({"output_mode": "keep_both"}),
        }
        assert _is_path_owned_by_active_job("movies/Dune.mkv", keep_both_job) is True
        assert _is_path_owned_by_active_job("movies/Dune.remux.mkv", keep_both_job) is True
        assert _is_path_owned_by_active_job("movies/Dune.remux.2.mkv", keep_both_job) is True
        assert _is_path_owned_by_active_job("movies/Dune.remux.15.mkv", keep_both_job) is True
        assert _is_path_owned_by_active_job("other/Dune.remux.mkv", keep_both_job) is False
        assert _is_path_owned_by_active_job("movies/Dune2.mkv", keep_both_job) is False

    def test_owned_guard_handles_null_empty_and_malformed_json_safely(self) -> None:
        assert _is_path_owned_by_active_job("movies/Any.mkv", None) is False
        assert _is_path_owned_by_active_job("", {"filepath": "movies/Any.mkv"}) is False
        bad_json_job = {"filepath": "movies/Broken.mkv", "config_json": "{not-json"}
        assert _is_path_owned_by_active_job("movies/Broken.mkv", bad_json_job) is True
        assert _is_path_owned_by_active_job("movies/Other.mkv", bad_json_job) is False

    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            ("movies/Movie.mkv", False),
            ("movies/Clip.mp4", False),
            ("movies/Movie.mkv.mkvforge.tmp", True),
            ("movies/Movie.mkv.mkvforge.eac3", True),
            ("movies/Movie.MKVFORGE.TMP", True),
            ("movies/Movie.mkv.part", True),
            ("movies/Movie.mkv.!qB", True),
            ("movies/Movie.mkv.tmp", True),
            ("movies/.hidden.mkv", True),
            ("", True),
            ("   ", True),
        ],
    )
    def test_temp_or_partial_rel_path_rejects_artifacts(
        self, rel_path: str, expected: bool
    ) -> None:
        assert is_temp_or_partial_rel_path(rel_path) is expected

    async def test_schedule_background_probe_rejects_temp_artifacts(
        self, tmp_path: Path
    ) -> None:
        watcher, broker, _media_repo, _job_repo, _settings = _make_watcher_env(tmp_path)
        try:
            assert watcher.schedule_background_probe("movies/Active.mkv.mkvforge.tmp") is False
            assert watcher.schedule_background_probe("movies/Copying.mkv.part") is False
            assert watcher.schedule_background_probe("movies/.dot.mkv") is False
        finally:
            await watcher.shutdown()
            broker.close()


class TestGuardrail2And3SerializedProbingAndStabilityDuringActiveJob:
    """Verify 1-at-a-time serialization, low I/O flag, and write-stability during active jobs."""

    async def test_unrelated_files_probe_serially_one_at_a_time_while_job_runs(
        self, tmp_path: Path
    ) -> None:
        watcher, broker, media_repo, job_repo, settings = _make_watcher_env(
            tmp_path, debounce_sec=0.05
        )
        movies = settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)

        # Active job running on an unrelated file
        (movies / "Converting.mkv").write_bytes(b"busy")
        job_repo.create_job(
            job_id="job-busy",
            filepath="movies/Converting.mkv",
            config_json=json.dumps({"output_mode": "replace"}),
        )
        job_repo.claim_next_queued_job()

        dropped_files = []
        for idx in range(3):
            f = movies / f"Drop{idx}.mkv"
            f.write_bytes(f"payload-{idx}".encode())
            dropped_files.append(f)

        in_flight = 0
        max_in_flight = 0
        probed_calls: list[tuple[str, bool]] = []
        fake_data = _fake_probe_payload()

        async def _slow_probe(
            path: Path, _timeout: float, *, low_io_priority: bool = False
        ):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            probed_calls.append((path.name, low_io_priority))
            await asyncio.sleep(0.06)
            in_flight -= 1
            return fake_data, json.dumps(fake_data)

        try:
            with patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(side_effect=_slow_probe),
            ):
                for f in dropped_files:
                    watcher.handle_raw_fs_event(Change.added, f)

                for _ in range(100):
                    all_ready = all(
                        (row := media_repo.get_by_filepath(f"movies/Drop{idx}.mkv")) is not None
                        and row["scan_status"] == "ready"
                        for idx in range(3)
                    )
                    if len(probed_calls) == 3 and all_ready:
                        break
                    await asyncio.sleep(0.02)

            assert len(probed_calls) == 3
            assert max_in_flight == AppConstants.WATCH_PROBE_CONCURRENCY == 1
            assert all(low_io is True for _, low_io in probed_calls)
            for idx in range(3):
                row = media_repo.get_by_filepath(f"movies/Drop{idx}.mkv")
                assert row is not None and row["scan_status"] == "ready"
        finally:
            await watcher.shutdown()
            broker.close()

    async def test_growing_file_still_waits_for_write_stability_while_job_runs(
        self, tmp_path: Path
    ) -> None:
        watcher, broker, media_repo, job_repo, settings = _make_watcher_env(
            tmp_path, debounce_sec=0.12
        )
        movies = settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)

        (movies / "Active.mkv").write_bytes(b"busy")
        job_repo.create_job(
            job_id="job-active",
            filepath="movies/Active.mkv",
            config_json=json.dumps({"output_mode": "replace"}),
        )
        job_repo.claim_next_queued_job()

        copying = movies / "CopyingDuringJob.mkv"
        copying.write_bytes(b"chunk-1")

        probed: list[tuple[str, bool]] = []
        fake_data = _fake_probe_payload()

        async def _fake_probe(
            path: Path, _timeout: float, *, low_io_priority: bool = False
        ):
            probed.append((path.name, low_io_priority))
            return fake_data, json.dumps(fake_data)

        try:
            with patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(side_effect=_fake_probe),
            ):
                watcher.handle_raw_fs_event(Change.added, copying)
                # Keep appending inside debounce window -> MUST NOT probe mid-copy!
                await asyncio.sleep(0.05)
                with copying.open("ab") as fh:
                    fh.write(b"chunk-2")
                assert probed == []

                await asyncio.sleep(0.05)
                with copying.open("ab") as fh:
                    fh.write(b"chunk-3-done")
                assert probed == []

                # Wait for stability window and DB upsert to settle while job is STILL running
                for _ in range(80):
                    row_now = media_repo.get_by_filepath("movies/CopyingDuringJob.mkv")
                    if probed and row_now is not None and row_now["scan_status"] == "ready":
                        break
                    await asyncio.sleep(0.02)

            assert probed == [("CopyingDuringJob.mkv", True)]
            row = media_repo.get_by_filepath("movies/CopyingDuringJob.mkv")
            assert row is not None and row["scan_status"] == "ready"
        finally:
            await watcher.shutdown()
            broker.close()


class TestGuardrail3SafeIoniceFallback:
    """Verify `run_mkvmerge_probe` uses ionice when available and falls back cleanly on errors."""

    @pytest.fixture(autouse=True)
    def _reset_ionice_latch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.services.scanner as scanner_mod

        monkeypatch.setattr(scanner_mod, "_ionice_probe_disabled", False)

    async def test_low_io_wraps_with_ionice_when_available(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.mkv"
        sample.write_bytes(b"mkv")
        captured_argvs: list[tuple[str, ...]] = []
        fake_json = json.dumps(_fake_probe_payload()).encode()

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(fake_json, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            parsed, _raw = await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        assert parsed["tracks"][0]["codec"] == "HEVC"
        assert len(captured_argvs) == 1
        assert captured_argvs[0][:4] == (
            AppConstants.IONICE_TOOL,
            *AppConstants.IONICE_IDLE_CLASS_ARGS,
        )
        assert captured_argvs[0][4] == AppConstants.TOOL_MKVMERGE

    async def test_low_io_skips_ionice_when_binary_missing(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.mkv"
        sample.write_bytes(b"mkv")
        captured_argvs: list[tuple[str, ...]] = []
        fake_json = json.dumps(_fake_probe_payload()).encode()

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(fake_json, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=False),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            parsed, _raw = await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        assert parsed["tracks"][0]["codec"] == "HEVC"
        assert captured_argvs == [
            (AppConstants.TOOL_MKVMERGE, AppConstants.MKVMERGE_IDENTIFY_FLAG, str(sample))
        ]

    async def test_low_io_falls_back_when_ionice_launch_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "sample.mkv"
        sample.write_bytes(b"mkv")
        captured_argvs: list[tuple[str, ...]] = []
        fake_json = json.dumps(_fake_probe_payload()).encode()

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            if argv[0] == AppConstants.IONICE_TOOL:
                raise PermissionError("ionice blocked by container policy")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(fake_json, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            parsed, _raw = await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        assert parsed["tracks"][0]["codec"] == "HEVC"
        assert len(captured_argvs) == 2
        assert captured_argvs[0][0] == AppConstants.IONICE_TOOL
        assert captured_argvs[1][0] == AppConstants.TOOL_MKVMERGE

    async def test_low_io_falls_back_when_ionice_exits_with_ioprio_error(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "sample.mkv"
        sample.write_bytes(b"mkv")
        captured_argvs: list[tuple[str, ...]] = []
        fake_json = json.dumps(_fake_probe_payload()).encode()

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            if argv[0] == AppConstants.IONICE_TOOL:
                return SimpleNamespace(
                    returncode=1,
                    communicate=AsyncMock(
                        return_value=(b"", b"ionice: ioprio_set failed: Operation not permitted")
                    ),
                    kill=lambda: None,
                    wait=AsyncMock(),
                )
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(fake_json, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            parsed, _raw = await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        assert parsed["tracks"][0]["codec"] == "HEVC"
        assert len(captured_argvs) == 2
        assert captured_argvs[0][0] == AppConstants.IONICE_TOOL
        assert captured_argvs[1][0] == AppConstants.TOOL_MKVMERGE

    async def test_genuine_mkvmerge_failure_under_ionice_still_raises_probe_error(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "corrupt.mkv"
        sample.write_bytes(b"not-a-valid-mkv")
        captured_argvs: list[tuple[str, ...]] = []

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            return SimpleNamespace(
                returncode=2,
                communicate=AsyncMock(return_value=(b"", b"Error: Unknown file type")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            pytest.raises(ProbeError, match="exit code 2"),
        ):
            await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        # Must NOT retry when mkvmerge itself reported a corrupt file
        assert len(captured_argvs) == 1

    async def test_timeout_under_ionice_does_not_retry_and_raises_probe_error(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "slow.mkv"
        sample.write_bytes(b"slow-bytes")
        captured_argvs: list[tuple[str, ...]] = []
        killed = False

        async def _hang():
            await asyncio.sleep(5)
            return b"", b""

        def _kill():
            nonlocal killed
            killed = True

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            return SimpleNamespace(
                returncode=None,
                communicate=AsyncMock(side_effect=_hang),
                kill=_kill,
                wait=AsyncMock(),
            )

        with (
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            pytest.raises(ProbeError, match="timed out"),
        ):
            await run_mkvmerge_probe(sample, 0.05, low_io_priority=True)

        assert killed is True
        assert len(captured_argvs) == 1

    async def test_ionice_fallback_latches_off_after_first_failure_and_stops_double_spawning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import app.services.scanner as scanner_mod

        sample = tmp_path / "repeat.mkv"
        sample.write_bytes(b"mkv")
        fake_json = json.dumps(_fake_probe_payload()).encode()
        captured_argvs: list[tuple[str, ...]] = []

        async def _fake_exec(*argv, **_kwargs):
            captured_argvs.append(argv)
            if argv[0] == AppConstants.IONICE_TOOL:
                raise PermissionError("EPERM in container")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(fake_json, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            caplog.at_level(logging.DEBUG, logger="app.services.scanner"),
            patch("app.services.scanner.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)
            await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)
            await run_mkvmerge_probe(sample, 5.0, low_io_priority=True)

        assert scanner_mod._ionice_probe_disabled is True
        # Probe 1: ionice (fails) -> mkvmerge (2 calls); Probes 2 & 3: direct mkvmerge only (1 call each)
        assert [argv[0] for argv in captured_argvs] == [
            AppConstants.IONICE_TOOL,
            AppConstants.TOOL_MKVMERGE,
            AppConstants.TOOL_MKVMERGE,
            AppConstants.TOOL_MKVMERGE,
        ]
        fallback_records = [
            r for r in caplog.records if "ionice probe wrapper unavailable" in r.getMessage()
        ]
        assert len(fallback_records) == 1
        assert fallback_records[0].levelno == logging.WARNING

