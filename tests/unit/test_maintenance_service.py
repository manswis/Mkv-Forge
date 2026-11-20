"""Unit tests for app.services.maintenance_service."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
)
from app.models.schemas import UserSettingsUpdate
from app.services.library_events import LibraryEventBroker
from app.services.maintenance_service import (
    MaintenanceBusyError,
    MaintenanceService,
    StorageUnmountedError,
)
from app.services.scanner import LibraryScanner
from app.services.user_settings import UserSettingsService


def _build_service(tmp_path: Path) -> tuple[MaintenanceService, Settings, MediaFileRepository, JobRepository, AppStateRepository, UserSettingsService]:
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    storage.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        storage_dir=storage,
        data_dir=data,
        static_dir=Path("app/static"),
        health_timeout_sec=5.0,
        scan_timeout_sec=10.0,
        log_level="INFO",
        watch_mode="off",
    )
    db = DatabaseManager(settings.db_path)
    db.initialize()
    state_repo = AppStateRepository(db)
    media_repo = MediaFileRepository(db)
    job_repo = JobRepository(db)
    scanner = LibraryScanner(settings=settings, media_repo=media_repo, state_repo=state_repo)
    broker = LibraryEventBroker(coalesce_sec=0.0)
    user_settings_service = UserSettingsService(state_repo=state_repo)
    svc = MaintenanceService(
        settings=settings,
        db=db,
        state_repo=state_repo,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        library_broker=broker,
        user_settings_service=user_settings_service,
    )
    return svc, settings, media_repo, job_repo, state_repo, user_settings_service


def _seed_media(repo: MediaFileRepository, rel_path: str, raw_padding: int = 0) -> int:
    folder = "/" if "/" not in rel_path else f"/{rel_path.rsplit('/', 1)[0]}"
    filename = rel_path.rsplit("/", 1)[-1]
    raw_json = json.dumps({"pad": "x" * raw_padding})
    return repo.upsert(
        MediaFileUpsert(
            filepath=rel_path,
            folder=folder,
            filename=filename,
            size_bytes=1024,
            mtime=1700000000.0,
            duration_us=1_000_000,
            resolution="1080p",
            video_codec="HEVC",
            video_dynamic_range="SDR",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=raw_json,
            scanned_at="2026-11-19T20:00:00Z",
        )
    )


class TestMaintenanceService:
    """Exhaustive positive, negative, and safety-guard tests for MaintenanceService."""

    def test_optimize_database_reclaims_freelist_pages(self, tmp_path: Path) -> None:
        svc, _settings, media_repo, _job_repo, _state_repo, _usvc = _build_service(tmp_path)

        # Seed heavy rows then delete them to create SQLite freelist pages
        paths = [f"movies/bulk_{i}.mkv" for i in range(15)]
        for p in paths:
            _seed_media(media_repo, p, raw_padding=20_000)
        media_repo.delete_by_filepaths(paths)

        before_status = svc.get_status()
        assert before_status.reclaimable_bytes > 0

        res = svc.optimize_database()
        assert res.action == "optimize_db"
        assert res.bytes_after <= res.bytes_before
        assert svc.get_status().reclaimable_bytes == 0

    def test_clean_temp_and_uploads_preserves_active_and_fresh_uploads(self, tmp_path: Path) -> None:
        svc, settings, _media_repo, job_repo, _state_repo, _usvc = _build_service(tmp_path)
        uploads_dir = settings.uploads_dir
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # 1. Referenced by a queued job (must NEVER be deleted even if old)
        active_upload = uploads_dir / "active1234.srt"
        active_upload.write_bytes(b"active-sub")
        old_mtime = time.time() - 7200
        os.utime(active_upload, (old_mtime, old_mtime))
        job_repo.create_job(
            job_id="queued-1",
            filepath="movies/A.mkv",
            config_json=json.dumps(
                {
                    "external_tracks": [
                        {"source": "upload", "upload_id": "active1234.srt", "kind": "subtitles"}
                    ]
                }
            ),
        )

        # 2. Fresh unreferenced upload (< 1 hour old — user may still be editing!)
        fresh_upload = uploads_dir / "fresh5678.srt"
        fresh_upload.write_bytes(b"fresh-sub")

        # 3. Stale unreferenced upload (> 1 hour old — safe to clean)
        stale_upload = uploads_dir / "stale9999.srt"
        stale_upload.write_bytes(b"stale-sub-bytes")
        os.utime(stale_upload, (old_mtime, old_mtime))

        # 4. Stray .mkvforge.tmp in storage + real media file
        movies_dir = settings.storage_dir / "movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        stray_tmp = movies_dir / "A.mkv.mkvforge.tmp"
        stray_tmp.write_bytes(b"leftover-temp")
        real_mkv = movies_dir / "A.mkv"
        real_mkv.write_bytes(b"real-movie")

        status = svc.get_status()
        assert status.orphaned_uploads_count == 1
        assert status.stray_temp_files_count == 1

        res = svc.clean_temp_and_uploads()
        assert res.removed_count == 2
        assert res.bytes_reclaimed == len(b"stale-sub-bytes") + len(b"leftover-temp")
        assert active_upload.exists()
        assert fresh_upload.exists()
        assert not stale_upload.exists()
        assert not stray_tmp.exists()
        assert real_mkv.exists()

    def test_prune_missing_files_removes_ghosts_and_blocks_on_unmounted_storage(
        self, tmp_path: Path
    ) -> None:
        svc, settings, media_repo, _job_repo, _state_repo, _usvc = _build_service(tmp_path)
        movies_dir = settings.storage_dir / "movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        present = movies_dir / "Present.mkv"
        present.write_bytes(b"ok")

        _seed_media(media_repo, "movies/Present.mkv")
        _seed_media(media_repo, "movies/Vanished.mkv")

        # Happy path: 1 exists, 1 vanished -> prunes only Vanished.mkv
        res = svc.prune_missing_files()
        assert res.removed_count == 1
        assert [p for _, p in media_repo.list_all_filepaths()] == ["movies/Present.mkv"]

        # Guardrail #2a: 0 indexed files found on disk while DB has rows -> refuse!
        present.unlink()
        with pytest.raises(StorageUnmountedError):
            svc.prune_missing_files()
        assert len(media_repo.list_all_filepaths()) == 1

        # Guardrail #2b: storage dir completely empty -> refuse!
        movies_dir.rmdir()
        with pytest.raises(StorageUnmountedError):
            svc.prune_missing_files()
        assert len(media_repo.list_all_filepaths()) == 1

        # Guardrail #2c: storage dir missing/unmounted -> refuse!
        settings.storage_dir.rmdir()
        with pytest.raises(StorageUnmountedError):
            svc.prune_missing_files()
        assert len(media_repo.list_all_filepaths()) == 1

    def test_prune_old_jobs_respects_retention_and_protects_active_and_queued(
        self, tmp_path: Path
    ) -> None:
        svc, _settings, _media_repo, job_repo, _state_repo, _usvc = _build_service(tmp_path)

        job_repo.create_job(job_id="old-done", filepath="a.mkv", config_json="{}")
        job_repo.mark_completed("old-done", output_filepath="a.mkv")
        job_repo.create_job(job_id="recent-done", filepath="b.mkv", config_json="{}")
        job_repo.mark_completed("recent-done", output_filepath="b.mkv")
        job_repo.create_job(job_id="old-queued", filepath="c.mkv", config_json="{}")

        old_iso = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        with job_repo._db.connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE jobs SET created_at = ?, completed_at = ? WHERE id = ?",
                (old_iso, old_iso, "old-done"),
            )
            conn.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (old_iso, "old-queued"),
            )

        # Retention = 0 (Never) deletes nothing
        never_res = svc.prune_old_jobs(retention_days=0)
        assert never_res.removed_count == 0

        # Retention = 30 removes old-done only, keeping recent-done and old-queued
        res = svc.prune_old_jobs(retention_days=30)
        assert res.removed_count == 1
        assert job_repo.get_by_id("old-done") is None
        assert job_repo.get_by_id("recent-done") is not None
        assert job_repo.get_by_id("old-queued") is not None

    async def test_scheduled_cleanup_runs_only_when_due_and_idle(self, tmp_path: Path) -> None:
        svc, _settings, _media_repo, job_repo, state_repo, user_settings = _build_service(tmp_path)

        # Disabled -> never runs
        user_settings.update(UserSettingsUpdate(auto_cleanup_enabled=False))
        assert svc.is_scheduled_cleanup_due() is False
        assert await svc.maybe_run_scheduled_cleanup() is False

        # Enabled & never run before -> due, but blocked while a job is running
        user_settings.update(UserSettingsUpdate(auto_cleanup_enabled=True))
        job_repo.create_job(job_id="running-1", filepath="a.mkv", config_json="{}")
        job_repo.claim_next_queued_job()
        assert await svc.maybe_run_scheduled_cleanup() is False
        with pytest.raises(MaintenanceBusyError):
            svc.optimize_database()

        # Once job completes -> scheduled cleanup runs and stamps last_auto_cleanup_at
        job_repo.mark_completed("running-1", output_filepath="a.mkv")
        assert await svc.maybe_run_scheduled_cleanup() is True
        assert state_repo.get(AppConstants.STATE_KEY_LAST_AUTO_CLEANUP_AT) is not None
        # Immediately afterward -> no longer due
        assert svc.is_scheduled_cleanup_due() is False
