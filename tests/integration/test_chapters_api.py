"""Phase C2 integration tests for Chapters & Stateless Scene Frame REST APIs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.repositories import JobRepository, MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.models.schemas import ChapterEntry
from app.services.chapter_service import ChapterError
from app.services.scene_frame_service import SceneFrameCancelledError, SceneFrameError

FAKE_JPEG = b"\xff\xd8\xff\xe0" + (b"\x00" * 64) + b"\xff\xd9"


def _seed_file(
    repo: MediaFileRepository,
    storage_dir: Path,
    rel_path: str,
    *,
    duration_us: int = 120_000_000,
    video_codec: str | None = "HEVC",
    resolution: str | None = "1080p",
    chapters_count: int = 0,
    scan_status: str = "ready",
    raw_json_override: str | None = None,
    create_on_disk: bool = True,
) -> int:
    abs_path = storage_dir / rel_path
    if create_on_disk:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"fake-media-bytes")

    raw_json = (
        raw_json_override
        if raw_json_override is not None
        else json.dumps(
            {
                "container": {"properties": {"duration": duration_us * 1000}},
                "chapters": [{"num_entries": chapters_count}] if chapters_count > 0 else [],
                "tracks": [],
            }
        )
    )
    fid = repo.upsert(
        MediaFileUpsert(
            filepath=rel_path,
            folder="/" + str(Path(rel_path).parent).lstrip(".") if "/" in rel_path else "/",
            filename=Path(rel_path).name,
            size_bytes=16,
            mtime=1700000000.0,
            duration_us=duration_us,
            resolution=resolution,
            video_codec=video_codec,
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=raw_json,
            scanned_at=utc_now_iso(),
        )
    )
    if scan_status != "ready":
        with repo._db.connect() as conn:
            conn.execute(
                "UPDATE media_files SET scan_status = ? WHERE id = ?",
                (scan_status, fid),
            )
    return fid


class TestChaptersAndFrameApiEndpoints:
    """Verify GET /chapters, POST /chapters/parse, GET /frame, and all Guardrail #3 checks."""

    async def test_get_chapters_fast_path_zero_complex_mp4_and_errors(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        repo: MediaFileRepository = api_app.state.media_repo

        # 1. MKV with 0 cached chapters -> returns [] WITHOUT calling extract_chapters_from_file
        fid_zero = _seed_file(repo, test_settings.storage_dir, "movies/Zero.mkv", chapters_count=0)
        with patch("app.api.chapters.extract_chapters_from_file") as mock_extract:
            resp = await api_client.get(f"/api/files/{fid_zero}/chapters")
        assert resp.status_code == AppConstants.HTTP_OK
        assert resp.json() == {
            "file_id": fid_zero,
            "duration_us": 120_000_000,
            "chapters": [],
            "has_complex_chapters": False,
        }
        mock_extract.assert_not_called()

        # 2. MKV with existing chapters -> calls extract_chapters_from_file and surfaces has_complex_chapters
        fid_ch = _seed_file(repo, test_settings.storage_dir, "movies/HasCh.mkv", chapters_count=2)
        with patch(
            "app.api.chapters.extract_chapters_from_file",
            new=AsyncMock(
                return_value=(
                    [
                        ChapterEntry(start_ms=0, title="Intro"),
                        ChapterEntry(start_ms=60_000, title="Act 2"),
                    ],
                    True,
                )
            ),
        ):
            resp2 = await api_client.get(f"/api/files/{fid_ch}/chapters")
        assert resp2.status_code == AppConstants.HTTP_OK
        data2 = resp2.json()
        assert data2["has_complex_chapters"] is True
        assert [c["title"] for c in data2["chapters"]] == ["Intro", "Act 2"]

        # 3. MP4 with 0 cached mkvmerge chapters STILL probes via extract_chapters_from_file (ffprobe)
        fid_mp4 = _seed_file(repo, test_settings.storage_dir, "movies/Clip.mp4", chapters_count=0)
        with patch(
            "app.api.chapters.extract_chapters_from_file",
            new=AsyncMock(return_value=([ChapterEntry(start_ms=0, title="MP4 Ch 1")], False)),
        ) as mock_mp4:
            resp_mp4 = await api_client.get(f"/api/files/{fid_mp4}/chapters")
        assert resp_mp4.status_code == AppConstants.HTTP_OK
        assert resp_mp4.json()["chapters"] == [{"start_ms": 0, "title": "MP4 Ch 1"}]
        mock_mp4.assert_awaited_once()

        # 4. Extraction failure returns 502 Bad Gateway
        with patch(
            "app.api.chapters.extract_chapters_from_file",
            new=AsyncMock(side_effect=ChapterError("mkvextract crashed")),
        ):
            resp_err = await api_client.get(f"/api/files/{fid_ch}/chapters")
        assert resp_err.status_code == AppConstants.HTTP_BAD_GATEWAY

        # 5. Edge raw_json values (empty, bad JSON, non-dict JSON) handled cleanly
        for bad_raw in ("", "{invalid", "[]"):
            fid_bad = _seed_file(
                repo,
                test_settings.storage_dir,
                f"movies/BadRaw_{abs(hash(bad_raw))}.mkv",
                raw_json_override=bad_raw,
            )
            r = await api_client.get(f"/api/files/{fid_bad}/chapters")
            assert r.status_code == AppConstants.HTTP_OK
            assert r.json()["chapters"] == []

    async def test_parse_chapters_endpoint_auto_detects_and_validates(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        repo: MediaFileRepository = api_app.state.media_repo
        fid = _seed_file(repo, test_settings.storage_dir, "movies/ParseMe.mkv", duration_us=600_000_000)

        # Missing file -> 404
        r404 = await api_client.post("/api/files/999999/chapters/parse", json={"raw_text": "00:00 A"})
        assert r404.status_code == AppConstants.HTTP_NOT_FOUND

        # Valid plain text -> 200 with duration clamping warning
        r_ok = await api_client.post(
            f"/api/files/{fid}/chapters/parse",
            json={"raw_text": "00:00 Intro\n05:00 Mid\n20:00 Too Late", "filename": "ch.txt"},
        )
        assert r_ok.status_code == AppConstants.HTTP_OK
        body = r_ok.json()
        assert body["detected_format"] == "plain_text"
        assert [c["title"] for c in body["chapters"]] == ["Intro", "Mid"]
        assert any("past movie duration" in w for w in body["warnings"])

        # Malformed XML -> 400 Bad Request
        r_bad = await api_client.post(
            f"/api/files/{fid}/chapters/parse",
            json={"raw_text": "<Chapters><Broken></Chapters>"},
        )
        assert r_bad.status_code == AppConstants.HTTP_BAD_REQUEST

    async def test_frame_endpoint_guardrail3_409_active_replace_zero_disk_writes_and_cache_header(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        repo: MediaFileRepository = api_app.state.media_repo
        job_repo: JobRepository = api_app.state.job_repo

        fid_target = _seed_file(repo, test_settings.storage_dir, "movies/Target.mkv", duration_us=120_000_000)
        fid_other = _seed_file(repo, test_settings.storage_dir, "movies/Other.mkv", duration_us=120_000_000)
        fid_audio = _seed_file(
            repo,
            test_settings.storage_dir,
            "movies/AudioOnly.mkv",
            duration_us=60_000_000,
            video_codec=None,
            resolution=None,
        )
        fid_scanning = _seed_file(
            repo, test_settings.storage_dir, "movies/Scanning.mkv", scan_status="scanning"
        )
        fid_missing_disk = _seed_file(
            repo, test_settings.storage_dir, "movies/MissingOnDisk.mkv", create_on_disk=False
        )

        # Snapshot data_dir files BEFORE frame requests (prove Option A: zero server disk writes)
        data_files_before = set(test_settings.data_dir.rglob("*"))

        # 1. Active Replace Original job on Target.mkv -> 409 Conflict on Target, 200 OK (low_io=True) on Other!
        job_repo.create_job(
            job_id="job-replace-1",
            filepath="movies/Target.mkv",
            config_json=json.dumps({"output_mode": "replace"}),
        )
        job_repo.claim_next_queued_job()

        r_conflict = await api_client.get(f"/api/files/{fid_target}/frame?ts_ms=5000&mode=fast")
        assert r_conflict.status_code == AppConstants.HTTP_CONFLICT
        assert "Replace Original" in r_conflict.json()["detail"]

        captured_kwargs: dict = {}

        async def _fake_frame(abs_path: Path, **kwargs) -> bytes:
            captured_kwargs.update(kwargs)
            return FAKE_JPEG

        with patch("app.api.chapters.extract_scene_frame", side_effect=_fake_frame):
            r_other = await api_client.get(
                f"/api/files/{fid_other}/frame?ts_ms=999999&mode=accurate&v=120000000_1080p_HEVC"
            )
        assert r_other.status_code == AppConstants.HTTP_OK
        assert r_other.headers["content-type"] == "image/jpeg"
        assert (
            r_other.headers["cache-control"]
            == f"private, max-age={AppConstants.SCENE_FRAME_HTTP_CACHE_MAX_AGE_SEC}"
        )
        assert r_other.content == FAKE_JPEG
        # EOF clamped (120_000 - 500 = 119_500) and low_io_priority=True because unrelated job is active!
        assert captured_kwargs["ts_ms"] == 119_500
        assert captured_kwargs["mode"] == "accurate"
        assert captured_kwargs["low_io_priority"] is True

        # Zero new files written to <data_dir>
        data_files_after = set(test_settings.data_dir.rglob("*"))
        assert data_files_after == data_files_before

        # 2. Active Save As + delete_original=True on Target -> also 409 Conflict
        job_repo.mark_completed("job-replace-1", output_filepath="movies/Target.mkv")
        job_repo.create_job(
            job_id="job-saveas-del",
            filepath="movies/Target.mkv",
            config_json=json.dumps({"output_mode": "save_as", "delete_original": True}),
        )
        job_repo.claim_next_queued_job()
        assert (await api_client.get(f"/api/files/{fid_target}/frame?ts_ms=0")).status_code == AppConstants.HTTP_CONFLICT

        # 3. Active Keep Both job on Target -> read-only source is NOT modified in place -> 200 OK
        job_repo.mark_completed("job-saveas-del", output_filepath="movies/Target.mkv")
        job_repo.create_job(
            job_id="job-keepboth",
            filepath="movies/Target.mkv",
            config_json=json.dumps({"output_mode": "keep_both"}),
        )
        job_repo.claim_next_queued_job()
        with patch("app.api.chapters.extract_scene_frame", new=AsyncMock(return_value=FAKE_JPEG)):
            assert (await api_client.get(f"/api/files/{fid_target}/frame?ts_ms=0")).status_code == AppConstants.HTTP_OK

        # 4. Malformed / empty / non-dict active job config_json defensively blocks same file with 409
        job_repo.mark_completed("job-keepboth", output_filepath="movies/Target.remux.mkv")
        for idx, bad_cfg in enumerate(("", "{not-json", "[]")):
            jid = f"job-badcfg-{idx}"
            job_repo.create_job(
                job_id=jid,
                filepath="movies/Target.mkv",
                config_json=bad_cfg,
            )
            job_repo.claim_next_queued_job()
            assert (
                await api_client.get(f"/api/files/{fid_target}/frame?ts_ms=0")
            ).status_code == AppConstants.HTTP_CONFLICT
            job_repo.mark_completed(jid, output_filepath="movies/Target.mkv")

        # 5. Error & edge responses: 404 DB missing, 404 disk missing, 409 scanning, 400 audio-only, 400 unsafe path, 204 cancel, 502 error
        assert (await api_client.get("/api/files/999999/frame?ts_ms=0")).status_code == AppConstants.HTTP_NOT_FOUND
        assert (await api_client.get(f"/api/files/{fid_missing_disk}/frame?ts_ms=0")).status_code == AppConstants.HTTP_NOT_FOUND
        assert (await api_client.get(f"/api/files/{fid_scanning}/frame?ts_ms=0")).status_code == AppConstants.HTTP_CONFLICT
        assert (await api_client.get(f"/api/files/{fid_audio}/frame?ts_ms=0")).status_code == AppConstants.HTTP_BAD_REQUEST

        from app.utils.paths import PathSafetyError

        with patch("app.api.chapters.safe_resolve_path", side_effect=PathSafetyError("escape")):
            assert (await api_client.get(f"/api/files/{fid_other}/frame?ts_ms=0")).status_code == AppConstants.HTTP_BAD_REQUEST

        with patch(
            "app.api.chapters.extract_scene_frame",
            new=AsyncMock(side_effect=SceneFrameCancelledError("gone")),
        ):
            assert (await api_client.get(f"/api/files/{fid_other}/frame?ts_ms=0")).status_code == AppConstants.HTTP_NO_CONTENT

        with patch(
            "app.api.chapters.extract_scene_frame",
            new=AsyncMock(side_effect=SceneFrameError("ffmpeg boom")),
        ):
            assert (await api_client.get(f"/api/files/{fid_other}/frame?ts_ms=0")).status_code == AppConstants.HTTP_BAD_GATEWAY
