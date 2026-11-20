"""Integration tests for Phase 4 Job APIs, SSE headers/replay, and lifespan recovery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileUpsert, utc_now_iso
from app.main import create_app


def _job_payload(*, file_id: int | None = None, filepath: str | None = None) -> dict:
    body: dict = {
        "config": {
            "container": {"title": "", "strip_global_tags": True},
            "audio_conversion": {"enabled": False},
            "tracks": [
                {"id": 0, "type": "video", "enabled": True, "default_track": True},
                {"id": 1, "type": "audio", "enabled": True, "default_track": True},
            ],
            "output_mode": "replace",
        }
    }
    if file_id is not None:
        body["file_id"] = file_id
    if filepath is not None:
        body["filepath"] = filepath
    return body


class TestJobsApi:
    """Verify POST/GET/DELETE /api/jobs and duplicate/validation guards."""

    async def test_enqueue_list_duplicate_409_and_cancel_queued_job(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        movie = test_settings.storage_dir / "movies" / "Dune.mkv"
        movie.parent.mkdir(parents=True)
        movie.write_bytes(b"movie-data")

        file_id = api_app.state.media_repo.upsert(
            MediaFileUpsert(
                filepath="movies/Dune.mkv",
                folder="/movies",
                filename="Dune.mkv",
                size_bytes=10,
                mtime=1700000000.0,
                duration_us=1_000_000,
                resolution="1080p",
                video_codec="HEVC",
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

        # Enqueue by file_id (worker not started in api_app fixture -> stays queued)
        resp = await api_client.post("/api/jobs", json=_job_payload(file_id=file_id))
        assert resp.status_code == AppConstants.HTTP_ACCEPTED
        created = resp.json()
        assert created["filepath"] == "movies/Dune.mkv"
        assert created["status"] == AppConstants.JOB_STATUS_QUEUED

        # Duplicate job for same file (even via filepath instead of file_id) -> 409 Conflict
        dup = await api_client.post(
            "/api/jobs", json=_job_payload(filepath="movies/Dune.mkv")
        )
        assert dup.status_code == AppConstants.HTTP_CONFLICT

        # GET /api/jobs shows queued item
        list_resp = await api_client.get("/api/jobs")
        assert list_resp.status_code == AppConstants.HTTP_OK
        listed = list_resp.json()
        assert listed["active"] is None
        assert len(listed["queued"]) == 1
        assert listed["queued"][0]["id"] == created["id"]

        # DELETE /api/jobs/{id} cancels queued job cleanly
        del_resp = await api_client.delete(f"/api/jobs/{created['id']}")
        assert del_resp.status_code == AppConstants.HTTP_OK
        assert del_resp.json()["status"] == AppConstants.JOB_STATUS_CANCELLED

        # Cancelling an already-cancelled job returns 409 Conflict
        again = await api_client.delete(f"/api/jobs/{created['id']}")
        assert again.status_code == AppConstants.HTTP_CONFLICT

        # Now that previous job is cancelled, re-enqueuing same file succeeds!
        requeue = await api_client.post("/api/jobs", json=_job_payload(file_id=file_id))
        assert requeue.status_code == AppConstants.HTTP_ACCEPTED

    async def test_validation_and_error_responses(
        self, api_client: httpx.AsyncClient
    ) -> None:
        # Neither file_id nor filepath -> 422
        missing_target = await api_client.post("/api/jobs", json=_job_payload())
        assert missing_target.status_code == 422

        # Unknown file_id -> 404
        unknown_file = await api_client.post(
            "/api/jobs", json=_job_payload(file_id=99999)
        )
        assert unknown_file.status_code == AppConstants.HTTP_NOT_FOUND

        # Path traversal -> 400
        traversal = await api_client.post(
            "/api/jobs", json=_job_payload(filepath="../../etc/passwd")
        )
        assert traversal.status_code == AppConstants.HTTP_BAD_REQUEST

        # Unknown job cancel -> 404
        unknown_job = await api_client.delete("/api/jobs/nonexistent-uuid")
        assert unknown_job.status_code == AppConstants.HTTP_NOT_FOUND

        # Same-codec conversion (E-AC-3 -> eac3) rejected synchronously with 400
        bad_codec_payload = _job_payload(filepath="movies/Dune.mkv")
        bad_codec_payload["config"]["audio_conversion"] = {
            "enabled": True,
            "source_track_id": 1,
            "source_audio_index": 0,
            "source_codec": "E-AC-3",
            "target_codec": "eac3",
            "bitrate": "640k",
        }
        same_codec = await api_client.post("/api/jobs", json=bad_codec_payload)
        assert same_codec.status_code == AppConstants.HTTP_BAD_REQUEST
        assert "already EAC3" in same_codec.json()["detail"]

    async def test_save_as_api_validation_and_collision_guard(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        (movies / "Source.mkv").write_bytes(b"src")
        (movies / "Taken.mkv").write_bytes(b"existing")

        # 1. save_as without output_path -> 422
        missing_path = _job_payload(filepath="movies/Source.mkv")
        missing_path["config"]["output_mode"] = "save_as"
        resp_missing = await api_client.post("/api/jobs", json=missing_path)
        assert resp_missing.status_code == 422

        # 2. save_as with path traversal -> 400
        traversal_out = _job_payload(filepath="movies/Source.mkv")
        traversal_out["config"]["output_mode"] = "save_as"
        traversal_out["config"]["output_path"] = "../escape.mkv"
        resp_trav = await api_client.post("/api/jobs", json=traversal_out)
        assert resp_trav.status_code == AppConstants.HTTP_BAD_REQUEST

        # 3. save_as pointing to exact same file -> 400
        same_out = _job_payload(filepath="movies/Source.mkv")
        same_out["config"]["output_mode"] = "save_as"
        same_out["config"]["output_path"] = "movies/Source.mkv"
        resp_same = await api_client.post("/api/jobs", json=same_out)
        assert resp_same.status_code == AppConstants.HTTP_BAD_REQUEST

        # 4. save_as pointing to an existing file -> 409 Conflict
        collision_out = _job_payload(filepath="movies/Source.mkv")
        collision_out["config"]["output_mode"] = "save_as"
        collision_out["config"]["output_path"] = "movies/Taken.mkv"
        resp_col = await api_client.post("/api/jobs", json=collision_out)
        assert resp_col.status_code == AppConstants.HTTP_CONFLICT

        # 5. Valid save_as in a new subfolder -> 202 Accepted
        valid_out = _job_payload(filepath="movies/Source.mkv")
        valid_out["config"]["output_mode"] = "save_as"
        valid_out["config"]["output_path"] = "test/exports/Source (Fixed).mkv"
        resp_ok = await api_client.post("/api/jobs", json=valid_out)
        assert resp_ok.status_code == AppConstants.HTTP_ACCEPTED

    async def test_history_change_summaries_delete_one_and_clear_all(
        self, api_client: httpx.AsyncClient, api_app
    ) -> None:
        repo = api_app.state.job_repo
        cfg_completed = json.dumps(
            {
                "container": {"title": "", "strip_global_tags": True},
                "audio_conversion": {
                    "enabled": True,
                    "source_track_id": 1,
                    "source_channels": 6,
                    "target_codec": "eac3",
                    "bitrate": "640k",
                    "language": "eng",
                    "set_as_default": True,
                },
                "tracks": [
                    {"id": 0, "type": "video", "enabled": True},
                    {"id": 1, "type": "audio", "enabled": True},
                    {"id": 2, "type": "subtitles", "enabled": False},
                ],
                "output_mode": "replace",
            }
        )
        repo.create_job(job_id="hist-done", filepath="movies/A.mp4", config_json=cfg_completed)
        repo.mark_completed("hist-done", output_filepath="movies/A.mkv")

        repo.create_job(job_id="hist-fail", filepath="movies/B.mkv", config_json="{}")
        repo.mark_failed("hist-fail", error_message="Disk full")

        repo.create_job(job_id="still-queued", filepath="movies/C.mkv", config_json="{}")

        # GET /api/jobs exposes structured changes on history items
        listed = (await api_client.get("/api/jobs")).json()
        assert len(listed["queued"]) == 1
        assert len(listed["history"]) == 2
        done_item = next(j for j in listed["history"] if j["id"] == "hist-done")
        labels = [c["label"] for c in done_item["changes"]]
        assert "+ EAC3 5.1 (640k • eng) • Default" in labels
        assert "Cleared title" in labels
        assert "Stripped global tags" in labels
        assert "Removed 1 subtitle track" in labels
        assert ".mp4 → A.mkv" in labels

        # Refuses deleting a queued/active job from history -> 409 Conflict
        del_queued = await api_client.delete("/api/jobs/history/still-queued")
        assert del_queued.status_code == AppConstants.HTTP_CONFLICT

        # Unknown history ID -> 404
        del_missing = await api_client.delete("/api/jobs/history/nonexistent")
        assert del_missing.status_code == AppConstants.HTTP_NOT_FOUND

        # Delete ONE terminal history item -> 200 OK
        del_one = await api_client.delete("/api/jobs/history/hist-done")
        assert del_one.status_code == AppConstants.HTTP_OK
        assert del_one.json() == {"deleted": 1, "id": "hist-done"}

        after_one = (await api_client.get("/api/jobs")).json()
        assert [j["id"] for j in after_one["history"]] == ["hist-fail"]
        assert len(after_one["queued"]) == 1

        # Clear ALL remaining history -> deletes hist-fail, preserves still-queued!
        clear_all = await api_client.delete("/api/jobs/history")
        assert clear_all.status_code == AppConstants.HTTP_OK
        assert clear_all.json() == {"deleted": 1, "id": None}

        after_clear = (await api_client.get("/api/jobs")).json()
        assert after_clear["history"] == []
        assert [j["id"] for j in after_clear["queued"]] == ["still-queued"]


class TestJobsSseAndLifespanRecovery:
    """Verify GET /api/jobs/stream SSE headers/replay and FastAPI lifespan hooks."""

    async def test_jobs_stream_headers_ping_and_log_replay(self, api_app) -> None:
        from app.api.jobs import stream_job_progress

        broker = api_app.state.job_broker
        broker.start_job(
            job_id="active-1",
            filepath="movies/Test.mkv",
            stage="transcode",
            stage_label="Stage 1 of 2: Encoding EAC3",
        )
        broker.publish_log(
            job_id="active-1", filepath="movies/Test.mkv", log_line="ffmpeg line 1"
        )

        class _FakeRequest:
            def __init__(self, app) -> None:
                self.app = app
                self._checks = 0

            async def is_disconnected(self) -> bool:
                self._checks += 1
                return self._checks > 1

        response = await stream_job_progress(_FakeRequest(api_app))
        assert (
            response.headers.get(AppConstants.SSE_ACCEL_BUFFERING_HEADER)
            == AppConstants.SSE_ACCEL_BUFFERING_VALUE
        )
        assert response.ping_interval == AppConstants.SSE_PING_INTERVAL_SEC

        first_event = await anext(response.body_iterator)
        assert first_event["event"] == AppConstants.SSE_JOB_EVENT_NAME
        parsed = json.loads(first_event["data"])
        assert parsed["job_id"] == "active-1"
        assert len(parsed["recent_logs"]) == 1
        assert parsed["recent_logs"][0].endswith("ffmpeg line 1")

    async def test_lifespan_runs_crash_recovery_and_starts_stops_worker(
        self, test_settings: Settings
    ) -> None:
        # Pre-seed a stale running job and orphaned temp file before startup
        app = create_app(settings=test_settings)
        app.state.db.initialize()
        app.state.job_repo.create_job(
            job_id="crashed-job", filepath="movies/X.mkv", config_json="{}"
        )
        app.state.job_repo.claim_next_queued_job()
        orphan = test_settings.storage_dir / "X.mkv.mkvforge.tmp"
        orphan.write_bytes(b"leftover")

        async with app.router.lifespan_context(app):
            assert app.state.job_worker.is_running is True
            assert not orphan.exists()
            recovered = app.state.job_repo.get_by_id("crashed-job")
            assert recovered is not None
            assert recovered["status"] == AppConstants.JOB_STATUS_FAILED
            assert recovered["error_message"] == AppConstants.CRASH_RECOVERY_ERROR_MESSAGE

        assert app.state.job_worker.is_running is False
