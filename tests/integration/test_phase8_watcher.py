"""Phase 8 tests: live filesystem watcher, stability debounce, job deferral, and SSE refresh."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from watchfiles import Change

from app.config import AppConstants, Settings, load_settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
    utc_now_iso,
)
from app.services.library_events import LibraryEventBroker
from app.services.scanner import LibraryScanner
from app.services.watcher import (
    LibraryWatcher,
    is_watchable_media_path,
    should_watch_fs_event,
)


def _fake_probe_payload(title: str = "") -> dict:
    return {
        "container": {"properties": {"duration": 5_000_000_000, "title": title}},
        "global_tags": [],
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "H.264",
                "properties": {"pixel_dimensions": "1920x1080"},
            },
            {
                "id": 1,
                "type": "audio",
                "codec": "AAC",
                "properties": {"audio_channels": 6, "language": "eng"},
            },
        ],
    }


def _seed_media_row(repo: MediaFileRepository, filepath: str) -> int:
    return repo.upsert(
        MediaFileUpsert(
            filepath=filepath,
            folder="/movies",
            filename=Path(filepath).name,
            size_bytes=123,
            mtime=1700000000.0,
            duration_us=5_000_000,
            resolution="1080p",
            video_codec="H.264",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=True,
            has_ac3_eac3=False,
            raw_json="{}",
            scanned_at=utc_now_iso(),
        )
    )


def _build_watcher_stack(
    settings: Settings,
    db_manager: DatabaseManager,
    *,
    coalesce_sec: float = 0.05,
    job_defer_poll_sec: float = 0.03,
) -> tuple[
    LibraryWatcher,
    LibraryEventBroker,
    MediaFileRepository,
    JobRepository,
    LibraryScanner,
]:
    media_repo = MediaFileRepository(db_manager)
    state_repo = AppStateRepository(db_manager)
    job_repo = JobRepository(db_manager)
    scanner = LibraryScanner(settings, media_repo, state_repo)
    broker = LibraryEventBroker(coalesce_sec=coalesce_sec)
    watcher = LibraryWatcher(
        settings=settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        library_broker=broker,
        job_defer_poll_sec=job_defer_poll_sec,
    )
    return watcher, broker, media_repo, job_repo, scanner


class TestWatcherConfigAndFiltering:
    """Verify env var validation and partial/temp download ignore rules."""

    def test_load_settings_parses_watcher_env_vars_and_rejects_invalid(self) -> None:
        settings = load_settings(
            env={
                "MKVFORGE_WATCH_MODE": "poll",
                "MKVFORGE_WATCH_DEBOUNCE_SEC": "12.5",
                "MKVFORGE_SCAN_INTERVAL_MIN": "30",
            }
        )
        assert settings.watch_mode == "poll"
        assert settings.watch_debounce_sec == 12.5
        assert settings.scan_interval_min == 30

        with pytest.raises(ValueError, match="MKVFORGE_WATCH_MODE"):
            load_settings(env={"MKVFORGE_WATCH_MODE": "invalid"})
        with pytest.raises(ValueError, match="MKVFORGE_SCAN_INTERVAL_MIN"):
            load_settings(env={"MKVFORGE_SCAN_INTERVAL_MIN": "-5"})

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Movie.mkv", True),
            ("Clip.mp4", True),
            ("Movie.mkv.part", False),
            ("Movie.mkv.!qB", False),
            ("Movie.mkv.tmp", False),
            ("Movie.mkv.crdownload", False),
            ("Movie.mkv.partial", False),
            ("Movie.mkv.mkvforge.tmp", False),
            ("Movie.mkv.mkvforge.eac3", False),
            ("._Movie.mkv", False),
            ("Subtitles.srt", False),
        ],
    )
    def test_is_watchable_media_path(self, filename: str, expected: bool) -> None:
        assert is_watchable_media_path(Path("/storage/movies") / filename) is expected

    @pytest.mark.parametrize(
        ("change_type", "raw_path", "expected"),
        [
            (Change.added, "/storage/movies/Movie.mkv", True),
            (Change.modified, "/storage/movies/Movie.mkv", True),
            (Change.deleted, "/storage/movies/Movie.mp4", True),
            (Change.added, "/storage/movies/NewFolder", True),
            (Change.deleted, "/storage/movies/OldFolder", True),
            (Change.added, "/storage/movies/Subtitles.srt", True),
            (Change.modified, "/storage/movies/Subtitles.srt", False),
            (Change.modified, "/storage/movies/Uploading.bin", False),
            (Change.added, "/storage/movies/.DS_Store", False),
            (Change.modified, "/storage/movies/._Movie.mkv", False),
            (Change.modified, "/storage/movies/Movie.mkv.mkvforge.tmp", False),
            (Change.modified, "/storage/movies/Movie.mkv.mkvforge.tmp.eac3", False),
            (Change.modified, "/storage/movies/Movie.mkv.part", False),
            (Change.modified, "/storage/movies/Movie.mkv.!qB", False),
            (Change.modified, "/storage/movies/Movie.mkv.crdownload", False),
            (Change.added, "", False),
            (Change.added, "   ", False),
        ],
    )
    def test_should_watch_fs_event_filters_noise_before_watchfiles_logging(
        self, change_type: Change, raw_path: str, expected: bool
    ) -> None:
        assert should_watch_fs_event(change_type, raw_path) is expected


class TestWatcherDebounceSuppressionAndDeletes:
    """Verify stability debounce, active-job echo suppression, I/O deferral, and safe deletes."""

    async def test_stability_debounce_waits_for_growing_file_then_probes_and_emits(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        fast_settings = Settings(
            storage_dir=test_settings.storage_dir,
            data_dir=test_settings.data_dir,
            static_dir=test_settings.static_dir,
            health_timeout_sec=test_settings.health_timeout_sec,
            scan_timeout_sec=test_settings.scan_timeout_sec,
            log_level=test_settings.log_level,
            watch_mode="watch",
            watch_debounce_sec=0.12,
            scan_interval_min=0,
        )
        watcher, broker, media_repo, _job_repo, _scanner = _build_watcher_stack(
            fast_settings, db_manager, coalesce_sec=0.03
        )

        movies = fast_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        new_file = movies / "CopyingMovie.mkv"
        new_file.write_bytes(b"chunk-1")

        probe_calls: list[str] = []
        fake_data = _fake_probe_payload()

        async def _fake_probe(path: Path, _timeout: float):
            probe_calls.append(path.name)
            return fake_data, json.dumps(fake_data)

        received = []

        async def _collect_two():
            async for evt in broker.subscribe():
                received.append(evt)
                if len(received) >= 2:
                    break

        sub_task = asyncio.create_task(_collect_two())
        await asyncio.sleep(0.01)

        try:
            with patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(side_effect=_fake_probe),
            ):
                watcher.handle_raw_fs_event(Change.added, new_file)
                # Keep appending while inside debounce window -> MUST NOT probe yet!
                await asyncio.sleep(0.05)
                with new_file.open("ab") as fh:
                    fh.write(b"chunk-2-still-copying")
                assert probe_calls == []

                await asyncio.sleep(0.05)
                with new_file.open("ab") as fh:
                    fh.write(b"chunk-3-final")
                assert probe_calls == []

                # Now stop writing and let stability check pass
                await asyncio.wait_for(sub_task, timeout=3.0)

            assert probe_calls == ["CopyingMovie.mkv"]
            row = media_repo.get_by_filepath("movies/CopyingMovie.mkv")
            assert row is not None
            assert row["scan_status"] == "ready"
            assert len(received) == 2
            assert received[0].action == "added"
            assert received[1].action == "updated"
            assert received[1].filepaths == ["movies/CopyingMovie.mkv"]
            assert received[0].file_ids == [int(row["id"])]
        finally:
            await watcher.shutdown()
            broker.close()
            if not sub_task.done():
                sub_task.cancel()

    async def test_suppresses_active_job_echo_and_defers_unrelated_file_until_job_finishes(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        fast_settings = Settings(
            storage_dir=test_settings.storage_dir,
            data_dir=test_settings.data_dir,
            static_dir=test_settings.static_dir,
            health_timeout_sec=test_settings.health_timeout_sec,
            scan_timeout_sec=test_settings.scan_timeout_sec,
            log_level=test_settings.log_level,
            watch_mode="watch",
            watch_debounce_sec=0.05,
            scan_interval_min=0,
        )
        watcher, broker, media_repo, job_repo, _scanner = _build_watcher_stack(
            fast_settings, db_manager, coalesce_sec=0.02, job_defer_poll_sec=0.03
        )

        movies = fast_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        owned_file = movies / "ActiveJob.mkv"
        owned_file.write_bytes(b"active-job-file")
        other_file = movies / "BrandNew.mkv"
        other_file.write_bytes(b"brand-new-file")

        # Mark ActiveJob.mkv as currently running in SQLite
        job_repo.create_job(
            job_id="running-1",
            filepath="movies/ActiveJob.mkv",
            config_json=json.dumps({"output_mode": "replace"}),
        )
        job_repo.claim_next_queued_job()

        probed: list[str] = []
        fake_data = _fake_probe_payload()

        async def _fake_probe(path: Path, _timeout: float):
            probed.append(path.name)
            return fake_data, json.dumps(fake_data)

        try:
            with patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(side_effect=_fake_probe),
            ):
                # 1. Self-echo for running job -> suppressed immediately
                watcher.handle_raw_fs_event(Change.modified, owned_file)
                # 2. Unrelated new file -> deferred while running-1 is active
                watcher.handle_raw_fs_event(Change.added, other_file)

                await asyncio.sleep(0.15)
                assert probed == []  # Neither file probed while job is running!

                # Now complete the running job -> BrandNew.mkv should probe right away!
                job_repo.mark_completed("running-1", output_filepath="movies/ActiveJob.mkv")

                for _ in range(50):
                    if "BrandNew.mkv" in probed:
                        break
                    await asyncio.sleep(0.02)

            assert probed == ["BrandNew.mkv"]
            assert media_repo.get_by_filepath("movies/BrandNew.mkv") is not None
            assert media_repo.get_by_filepath("movies/ActiveJob.mkv") is None
        finally:
            await watcher.shutdown()
            broker.close()

    async def test_single_file_delete_preserves_other_rows_and_unmounted_guard(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        watcher, broker, media_repo, _job_repo, _scanner = _build_watcher_stack(
            test_settings, db_manager, coalesce_sec=0.02
        )
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        keep_file = movies / "Keep.mkv"
        keep_file.write_bytes(b"keep")
        del_file = movies / "DeleteMe.mkv"
        del_file.write_bytes(b"gone-soon")

        _seed_media_row(media_repo, "movies/Keep.mkv")
        del_id = _seed_media_row(media_repo, "movies/DeleteMe.mkv")

        events = []

        async def _sub():
            async for evt in broker.subscribe():
                events.append(evt)
                break

        sub_task = asyncio.create_task(_sub())
        await asyncio.sleep(0.01)

        try:
            del_file.unlink()
            watcher.handle_raw_fs_event(Change.deleted, del_file)
            await asyncio.wait_for(sub_task, timeout=2.0)

            assert media_repo.get_by_filepath("movies/DeleteMe.mkv") is None
            assert media_repo.get_by_filepath("movies/Keep.mkv") is not None
            assert events[0].action == "deleted"
            assert events[0].file_ids == [del_id]

            # Unmounted-storage safety guard: removing storage_dir must NEVER delete rows
            keep_file.unlink()
            movies.rmdir()
            test_settings.storage_dir.rmdir()
            await watcher._process_deleted("movies/Keep.mkv")
            assert media_repo.get_by_filepath("movies/Keep.mkv") is not None
        finally:
            await watcher.shutdown()
            broker.close()
            if not sub_task.done():
                sub_task.cancel()


class TestBurstCoalescingSseAndWatchModeOff:
    """Verify burst coalescing, `/api/scan/stream` library_changed SSE, and off mode."""

    async def test_burst_coalescing_and_scan_stream_delivery(
        self, api_app
    ) -> None:
        from app.api.scan import stream_scan_progress

        # Replace broker with fast coalesce window for crisp testing
        broker = LibraryEventBroker(coalesce_sec=0.04)
        api_app.state.library_broker = broker

        class _FakeRequest:
            def __init__(self, app) -> None:
                self.app = app
                self.disconnected = False

            async def is_disconnected(self) -> bool:
                return self.disconnected

        req = _FakeRequest(api_app)
        response = await stream_scan_progress(req)
        assert (
            response.headers.get(AppConstants.SSE_ACCEL_BUFFERING_HEADER)
            == AppConstants.SSE_ACCEL_BUFFERING_VALUE
        )
        assert response.ping_interval == AppConstants.SSE_PING_INTERVAL_SEC

        # First event is always initial scan_progress snapshot
        first = await anext(response.body_iterator)
        assert first["event"] == AppConstants.SSE_SCAN_EVENT_NAME

        # Emit a rapid burst of 4 additions -> coalesces into ONE library_changed event
        for idx in range(4):
            broker.record_change(
                action="added",
                filepath=f"shows/Episode{idx + 1}.mkv",
                file_id=100 + idx,
            )

        second = await asyncio.wait_for(anext(response.body_iterator), timeout=2.0)
        assert second["event"] == AppConstants.SSE_LIBRARY_CHANGED_EVENT_NAME
        parsed = json.loads(second["data"])
        assert parsed["action"] == "added"
        assert parsed["count"] == 4
        assert len(parsed["filepaths"]) == 4

        req.disconnected = True
        broker.close()

    async def test_watch_mode_off_disables_watcher_and_dashboard_reports_status(
        self, api_client: httpx.AsyncClient, api_app
    ) -> None:
        api_app.state.library_watcher.start()
        assert api_app.state.library_watcher.is_active is False

        resp = await api_client.get("/api/dashboard")
        assert resp.status_code == AppConstants.HTTP_OK
        data = resp.json()
        assert data["watcher"]["mode"] == "off"
        assert data["watcher"]["active"] is False

        # Frontend auto-reload & editor-safety UI landmarks
        html = (await api_client.get("/")).text
        app_js = (await api_client.get("/static/js/app.js")).text
        assert "pendingLibraryChangesCount" not in html
        assert "applyPendingLibraryRefresh" not in html
        assert "editorExternalChange" in html
        assert "reloadEditorAfterExternalChange" in html
        assert "_handleLibraryChangedEvent" in app_js
        assert "_pendingLibraryReload" in app_js
        assert "preserveSelection" in app_js
        assert "folder_exists" in app_js
        assert "library_changed" in (await api_client.get("/static/js/constants.js")).text


class TestDirectoryWatchingAndFolderSelfHeal:
    """Verify folder add/delete watcher events and single-folder refresh self-healing."""

    async def test_watcher_emits_on_directory_create_and_prunes_on_directory_delete(
        self, test_settings: Settings, db_manager
    ) -> None:
        media_repo = MediaFileRepository(db_manager)
        job_repo = JobRepository(db_manager)
        broker = LibraryEventBroker(coalesce_sec=0.03)
        broker.bind_loop(asyncio.get_running_loop())
        scanner = LibraryScanner(
            settings=test_settings,
            media_repo=media_repo,
            state_repo=AppStateRepository(db_manager),
        )
        watcher = LibraryWatcher(
            settings=test_settings,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            library_broker=broker,
        )

        # Seed a file inside /shows/OldSeason and a keep file in /movies
        old_dir = test_settings.storage_dir / "shows" / "OldSeason"
        old_dir.mkdir(parents=True, exist_ok=True)
        old_ep = old_dir / "Ep01.mkv"
        old_ep.write_bytes(b"episode")
        media_repo.upsert(
            MediaFileUpsert(
                filepath="shows/OldSeason/Ep01.mkv",
                folder="/shows/OldSeason",
                filename="Ep01.mkv",
                size_bytes=7,
                mtime=1700000000.0,
                duration_us=1_000_000,
                resolution="1080p",
                video_codec="HEVC",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=True,
                raw_json='{"tracks":[]}',
                scanned_at=utc_now_iso(),
            )
        )

        events: list = []

        async def _collect(target: int) -> None:
            async for evt in broker.subscribe():
                events.append(evt)
                if len(events) >= target:
                    break

        sub_task = asyncio.create_task(_collect(2))
        await asyncio.sleep(0.02)

        try:
            # 1. Directory created externally -> emits updated event
            new_dir = test_settings.storage_dir / "shows" / "NewSeason"
            new_dir.mkdir(parents=True, exist_ok=True)
            watcher.handle_raw_fs_event(Change.added, new_dir)

            # Unrelated sidecar .nfo deletion -> must NOT emit noise
            sidecar = test_settings.storage_dir / "shows" / "info.nfo"
            watcher.handle_raw_fs_event(Change.deleted, sidecar)

            await asyncio.sleep(0.08)
            assert len(events) == 1
            assert events[0].action == "updated"

            # 2. Directory deleted externally -> prunes nested SQLite rows & emits deleted
            old_ep.unlink()
            old_dir.rmdir()
            watcher.handle_raw_fs_event(Change.deleted, old_dir)

            await asyncio.wait_for(sub_task, timeout=2.0)
            assert len(events) == 2
            assert events[1].action == "deleted"
            assert media_repo.get_by_filepath("shows/OldSeason/Ep01.mkv") is None
        finally:
            await watcher.shutdown()
            broker.close()
            if not sub_task.done():
                sub_task.cancel()

    async def test_browse_folder_prunes_ghosts_queues_background_probe_and_handles_missing_dir(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        import os

        storage = test_settings.storage_dir
        repo: MediaFileRepository = api_app.state.media_repo
        watcher: LibraryWatcher = api_app.state.library_watcher
        scanner: LibraryScanner = api_app.state.scanner

        test_dir = storage / "test"
        test_dir.mkdir(parents=True, exist_ok=True)

        # Seed a ghost DB row (file already deleted on disk)
        repo.upsert(
            MediaFileUpsert(
                filepath="test/GhostDeleted.mkv",
                folder="/test",
                filename="GhostDeleted.mkv",
                size_bytes=1234,
                mtime=1700000000.0,
                duration_us=1_000_000,
                resolution="1080p",
                video_codec="HEVC",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=True,
                raw_json='{"tracks":[]}',
                scanned_at=utc_now_iso(),
            )
        )

        # Create a brand-new settled media file on disk (older than watch_debounce_sec)
        # plus an ignored .part file and a 0-byte placeholder
        new_file = test_dir / "FreshDrop.mkv"
        new_file.write_bytes(b"settled-media-bytes")
        old_ts = 1700000000.0
        os.utime(new_file, (old_ts, old_ts))
        (test_dir / "Downloading.mkv.part").write_bytes(b"partial")
        (test_dir / "ZeroByte.mkv").write_bytes(b"")

        probed_paths: list[str] = []

        async def _fake_probe(rel_path: str):
            probed_paths.append(rel_path)
            fid = repo.upsert(
                MediaFileUpsert(
                    filepath=rel_path,
                    folder="/test",
                    filename="FreshDrop.mkv",
                    size_bytes=len(b"settled-media-bytes"),
                    mtime=old_ts,
                    duration_us=1_000_000,
                    resolution="1080p",
                    video_codec="HEVC",
                    audio_summary_json="[]",
                    subtitle_count=0,
                    container_title="",
                    has_global_tags=False,
                    has_aac_51=False,
                    has_ac3_eac3=True,
                    raw_json='{"tracks":[]}',
                    scanned_at=utc_now_iso(),
                )
            )
            return repo.get_by_id(fid)

        scanner.probe_single_file = _fake_probe  # type: ignore[method-assign]

        # 1. Refreshing /test immediately prunes GhostDeleted.mkv & shows FreshDrop.mkv as 'scanning'
        resp = await api_client.get("/api/files", params={"folder": "/test"})
        assert resp.status_code == AppConstants.HTTP_OK
        payload = resp.json()
        assert payload["folder_exists"] is True
        assert [item["filename"] for item in payload["items"]] == ["FreshDrop.mkv"]
        assert payload["items"][0]["scan_status"] == "scanning"
        assert repo.get_by_filepath("test/GhostDeleted.mkv") is None

        # Wait briefly for fast-aged background probe to finish
        for _ in range(30):
            if "test/FreshDrop.mkv" in probed_paths:
                break
            await asyncio.sleep(0.02)

        assert probed_paths == ["test/FreshDrop.mkv"]
        assert repo.get_by_filepath("test/FreshDrop.mkv") is not None

        # 2. Requesting a folder deleted externally returns folder_exists=False and prunes rows
        new_file.unlink()
        (test_dir / "Downloading.mkv.part").unlink()
        (test_dir / "ZeroByte.mkv").unlink()
        test_dir.rmdir()

        gone_resp = await api_client.get("/api/files", params={"folder": "/test"})
        assert gone_resp.status_code == AppConstants.HTTP_OK
        assert gone_resp.json()["folder_exists"] is False
        assert repo.get_by_filepath("test/FreshDrop.mkv") is None

        await watcher.shutdown()
