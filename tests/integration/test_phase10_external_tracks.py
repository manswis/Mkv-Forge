"""Phase 10 integration tests: uploads API, storage track browser, mkv_builder multi-input & JS contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.models.schemas import (
    AudioConversionConfig,
    ExternalTrackConfig,
    JobConfig,
    JobCreateRequest,
    TrackEditConfig,
)
from app.services.job_summary import summarize_job_changes
from app.services.mkv_builder import build_mkvmerge_command


def _seed_movie(repo: MediaFileRepository, storage: Path, rel: str = "movies/Film.mkv") -> int:
    full = storage / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"fake-mkv-payload")
    return repo.upsert(
        MediaFileUpsert(
            filepath=rel,
            folder="/movies",
            filename=full.name,
            size_bytes=16,
            mtime=1700000000.0,
            duration_us=1_000_000,
            resolution="1080p",
            video_codec="HEVC",
            video_dynamic_range="SDR",
            audio_summary_json='[{"codec":"AAC","channels":6,"channel_layout":"5.1","language":"eng"}]',
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=True,
            has_ac3_eac3=False,
            raw_json='{"tracks":[]}',
            scanned_at=utc_now_iso(),
        )
    )


class TestUploadsAndStorageTrackDiscoveryApi:
    """Verify /api/uploads/track, /api/folders?include_files=true, and job cancel cleanup."""

    async def test_upload_stage_delete_and_error_guards(
        self, api_client: httpx.AsyncClient, api_app
    ) -> None:
        # 1. Happy path subtitle upload
        resp = await api_client.post(
            "/api/uploads/track",
            params={"kind": "subtitles", "filename": "English.srt"},
            content=b"1\n00:00:01,000 --> 00:00:03,000\nHello\n",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == AppConstants.HTTP_CREATED
        data = resp.json()
        upload_id = data["upload_id"]
        assert upload_id.endswith(".srt")
        assert data["kind"] == "subtitles"
        assert data["original_filename"] == "English.srt"
        assert data["size_bytes"] > 0

        # 2. Unsupported extension rejected with 400
        bad_ext = await api_client.post(
            "/api/uploads/track",
            params={"kind": "subtitles", "filename": "Video.mkv"},
            content=b"bytes",
        )
        assert bad_ext.status_code == AppConstants.HTTP_BAD_REQUEST

        # 3. Oversized Content-Length rejected with 413
        too_big = await api_client.post(
            "/api/uploads/track",
            params={"kind": "subtitles", "filename": "Giant.srt"},
            content=b"tiny",
            headers={"Content-Length": str(50 * 1024 * 1024)},
        )
        assert too_big.status_code == AppConstants.HTTP_PAYLOAD_TOO_LARGE

        # 4. Discard staged upload via DELETE
        del_resp = await api_client.delete(f"/api/uploads/{upload_id}")
        assert del_resp.status_code == AppConstants.HTTP_OK
        assert del_resp.json()["deleted"] is True

        # Second delete returns 404
        missing = await api_client.delete(f"/api/uploads/{upload_id}")
        assert missing.status_code == AppConstants.HTTP_NOT_FOUND

    async def test_folders_api_lists_filtered_external_track_files(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        (movies / "Subs").mkdir(parents=True, exist_ok=True)
        (movies / "Film.mkv").write_bytes(b"video")
        (movies / "English.srt").write_bytes(b"sub1")
        (movies / "Hindi.ass").write_bytes(b"sub2")
        (movies / "Commentary.ac3").write_bytes(b"aud1")
        (movies / "Partial.srt.part").write_bytes(b"skip")
        (movies / ".hidden.srt").write_bytes(b"skip")

        sub_resp = await api_client.get(
            "/api/folders",
            params={"path": "/movies", "include_files": "true", "kind": "subtitles"},
        )
        assert sub_resp.status_code == AppConstants.HTTP_OK
        sub_data = sub_resp.json()
        assert [e["name"] for e in sub_data["entries"]] == ["Subs"]
        assert [f["name"] for f in sub_data["files"]] == ["English.srt", "Hindi.ass"]
        assert all(f["kind"] == "subtitles" for f in sub_data["files"])

        aud_resp = await api_client.get(
            "/api/folders",
            params={"path": "/movies", "include_files": "true", "kind": "audio"},
        )
        assert aud_resp.status_code == AppConstants.HTTP_OK
        assert [f["name"] for f in aud_resp.json()["files"]] == ["Commentary.ac3"]

    async def test_job_enqueue_validates_external_tracks_and_cancel_cleans_staged_upload(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        file_id = _seed_movie(api_app.state.media_repo, test_settings.storage_dir)

        # Stage an upload
        up_resp = await api_client.post(
            "/api/uploads/track",
            params={"kind": "subtitles", "filename": "English.srt"},
            content=b"1\n00:00:00,000 --> 00:00:01,000\nLine\n",
        )
        upload_id = up_resp.json()["upload_id"]
        staged_file = api_app.state.upload_service.resolve_upload_path(upload_id)
        assert staged_file.is_file()

        # Missing storage external file rejected synchronously with 404
        bad_job = await api_client.post(
            "/api/jobs",
            json={
                "file_id": file_id,
                "config": {
                    "tracks": [{"id": 0, "type": "video", "enabled": True}],
                    "external_tracks": [
                        {
                            "id": "ext-1",
                            "kind": "subtitles",
                            "source": "storage",
                            "path": "movies/DoesNotExist.srt",
                        }
                    ],
                },
            },
        )
        assert bad_job.status_code == AppConstants.HTTP_NOT_FOUND

        # Enqueue valid job with uploaded subtitle (worker is not started in api_client fixture)
        ok_job = await api_client.post(
            "/api/jobs",
            json={
                "file_id": file_id,
                "config": {
                    "tracks": [{"id": 0, "type": "video", "enabled": True}],
                    "external_tracks": [
                        {
                            "id": "ext-1",
                            "kind": "subtitles",
                            "source": "upload",
                            "upload_id": upload_id,
                            "original_filename": "English.srt",
                            "track_name": "English SDH",
                            "language": "eng",
                            "default_track": True,
                            "sync_delay_ms": 250,
                        }
                    ],
                    "output_mode": "keep_both",
                },
            },
        )
        assert ok_job.status_code == AppConstants.HTTP_ACCEPTED
        job_data = ok_job.json()
        assert any(
            c["kind"] == "added" and "Imported Sub" in c["label"]
            for c in job_data["changes"]
        )

        # Cancel queued job -> staged upload must be cleaned up automatically
        cancel_resp = await api_client.delete(f"/api/jobs/{job_data['id']}")
        assert cancel_resp.status_code == AppConstants.HTTP_OK
        assert not staged_file.exists()


class TestMultiInputMkvBuilderAndSummary:
    """Verify mkvmerge multi-input argument ordering, isolation, sync, and double-default guard."""

    def test_mkv_builder_multi_input_ordering_sync_and_single_default(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Movie.mkv"
        out_tmp = tmp_path / "Movie.mkv.mkvforge.tmp.mkv"
        temp_aud = tmp_path / "Movie.mkv.mkvforge.eac3"
        ext_aud = tmp_path / "Hindi.ac3"
        ext_sub = tmp_path / "English.srt"

        cfg = JobConfig(
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                target_codec="eac3",
                bitrate="640k",
                track_title="English EAC3 5.1",
                language="eng",
                set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
                TrackEditConfig(id=2, type="subtitles", enabled=True, default_track=True),
            ],
            external_tracks=[
                ExternalTrackConfig(
                    id="ext-1",
                    kind="audio",
                    source="storage",
                    path="movies/Hindi.ac3",
                    original_filename="Hindi.ac3",
                    track_name="Hindi Commentary",
                    language="hin",
                    default_track=True,  # Must be demoted because converted audio is Default!
                    sync_delay_ms=-120,
                    order_index=3,
                ),
                ExternalTrackConfig(
                    id="ext-2",
                    kind="subtitles",
                    source="upload",
                    upload_id="a" * 32 + ".srt",
                    original_filename="English.srt",
                    track_name="English SDH",
                    language="eng",
                    default_track=True,  # Must demote source subtitle #2!
                    forced_track=True,
                    sync_delay_ms=350,
                    order_index=4,
                ),
            ],
            output_mode="keep_both",
        )

        argv = build_mkvmerge_command(
            src,
            out_tmp,
            cfg,
            temp_audio_path=temp_aud,
            resolved_external_tracks=[
                (cfg.external_tracks[0], ext_aud),
                (cfg.external_tracks[1], ext_sub),
            ],
        )

        idx_temp = argv.index(str(temp_aud))
        idx_src = argv.index(str(src))
        idx_ext_aud = argv.index(str(ext_aud))
        idx_ext_sub = argv.index(str(ext_sub))
        assert idx_temp < idx_src < idx_ext_aud < idx_ext_sub

        # Source audio #1 AND source subtitle #2 both demoted to default:no
        src_slice = argv[idx_temp + 1 : idx_src]
        assert "1:no" in src_slice
        assert "2:no" in src_slice

        # External audio (file 2) strips tags/video/subs, demotes default because Stage 1 owns audio default, applies sync -120
        ext_aud_slice = argv[idx_src + 1 : idx_ext_aud]
        assert "--no-global-tags" in ext_aud_slice
        assert "--no-video" in ext_aud_slice
        assert "--no-subtitles" in ext_aud_slice
        assert "0:no" in ext_aud_slice
        assert "0:-120" in ext_aud_slice
        assert "0:Hindi Commentary" in ext_aud_slice

        # External subtitle (file 3) strips tags/video/audio, owns default:yes & forced:yes, applies sync 350
        ext_sub_slice = argv[idx_ext_aud + 1 : idx_ext_sub]
        assert "--no-global-tags" in ext_sub_slice
        assert "--no-video" in ext_sub_slice
        assert "--no-audio" in ext_sub_slice
        assert "0:yes" in ext_sub_slice
        assert "0:350" in ext_sub_slice
        assert "0:English SDH" in ext_sub_slice

        # --track-order includes file 0 (Stage 1), file 1 (source), file 2 (ext audio), file 3 (ext sub)
        order_val = argv[argv.index("--track-order") + 1]
        assert order_val == "1:0,0:0,1:1,2:0,3:0,1:2"

        # Job summary surfaces both imported tracks
        _mode, changes = summarize_job_changes(
            cfg.model_dump_json(), filepath="movies/Movie.mkv"
        )
        labels = [c.label for c in changes]
        assert any("Imported Audio" in label and "Hindi Commentary" in label for label in labels)
        assert any("Imported Sub" in label and "English SDH" in label for label in labels)


class TestPhase10EditorJsContract:
    """Verify editor.js external track add/reorder/remove and Pydantic JobCreateRequest contract."""

    def test_editor_js_external_tracks_contract_and_reordering(self) -> None:
        script = r"""
        global.window = global;
        require('./app/static/js/constants.js');
        require('./app/static/js/editor.js');
        const Ed = global.MKVForgeEditor;

        const fileDetail = {
          id: 9,
          filepath: "movies/Clean.mkv",
          folder: "/movies",
          filename: "Clean.mkv",
          container_title: "",
          has_global_tags: false,
          has_metadata: false,
          tracks: [
            { id: 0, type: "video", codec: "HEVC", language: "und", track_name: "", default_track: true, forced_track: false },
            { id: 1, type: "audio", codec: "EAC3", audio_channels: 6, audio_index: 0, language: "eng", track_name: "", default_track: true, forced_track: false },
          ],
        };

        const state = Ed.createEditorState(fileDetail);
        const beforeHasChanges = Ed.hasPendingChanges(state, fileDetail);

        const extSub = Ed.upsertExternalTrack(state, {
          kind: "subtitles",
          source: "storage",
          path: "/movies/English.srt",
          originalFilename: "English.srt",
          trackName: "English",
          language: "eng",
          defaultTrack: true,
          forcedTrack: false,
          syncDelayMs: 150,
        });

        const extAud = Ed.upsertExternalTrack(state, {
          kind: "audio",
          source: "upload",
          uploadId: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ac3",
          originalFilename: "Hindi.ac3",
          trackName: "Hindi AC3",
          language: "hin",
          defaultTrack: false,
          forcedTrack: false,
          syncDelayMs: -50,
        });

        // Move imported audio up above source audio #1
        Ed.moveOrderedTrack(state, `ext:${extAud.id}`, -1);

        const display = Ed.buildDisplayTracks(state);
        const afterHasChanges = Ed.hasPendingChanges(state, fileDetail);
        const keptCount = Ed.countEffectiveKeptTracks(state);
        const summary = Ed.buildPendingSummary(state, fileDetail);
        const payload = Ed.buildJobRequestPayload(state, fileDetail);

        console.log(JSON.stringify({
          beforeHasChanges,
          afterHasChanges,
          keptCount,
          summary,
          displayKeys: display.map((r) => r.key),
          payload,
        }));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["beforeHasChanges"] is False
        assert out["afterHasChanges"] is True
        assert out["keptCount"] == 4
        assert "Imported Audio" in out["summary"]
        assert "Imported Sub" in out["summary"]
        # Imported audio moved above source audio track #1
        assert out["displayKeys"] == ["track-0", "ext-ext-2", "track-1", "ext-ext-1"]

        validated = JobCreateRequest.model_validate(out["payload"])
        assert len(validated.config.external_tracks) == 2
        by_kind = {t.kind: t for t in validated.config.external_tracks}
        assert by_kind["audio"].source == "upload"
        assert by_kind["audio"].order_index == 1
        assert by_kind["audio"].sync_delay_ms == -50
        assert by_kind["subtitles"].source == "storage"
        assert by_kind["subtitles"].path == "movies/English.srt"
        assert by_kind["subtitles"].order_index == 3
        assert by_kind["subtitles"].sync_delay_ms == 150
