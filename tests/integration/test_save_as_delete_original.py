"""Exhaustive tests for Save As 'Delete original', 'Confirm Before Deleting File' setting, and mobile Output Mode UI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.db.repositories import MediaFileUpsert, utc_now_iso
from app.services.mkv_runner import TargetFileCollisionError, finalize_remux_output

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TestConfirmDeleteFileSetting:
    """Verify per-user confirm_delete_file setting defaults to True and round-trips cleanly."""

    async def test_confirm_delete_file_default_enabled_and_toggleable(
        self, api_client: httpx.AsyncClient
    ) -> None:
        r0 = await api_client.get("/api/settings")
        assert r0.status_code == AppConstants.HTTP_OK
        assert r0.json()["confirm_delete_file"] is True

        r1 = await api_client.patch("/api/settings", json={"confirm_delete_file": False})
        assert r1.status_code == AppConstants.HTTP_OK
        assert r1.json()["confirm_delete_file"] is False

        r2 = await api_client.patch("/api/settings", json={"confirm_delete_file": True})
        assert r2.status_code == AppConstants.HTTP_OK
        assert r2.json()["confirm_delete_file"] is True


class TestSaveAsDeleteOriginalBackend:
    """Verify Save As deletes original ONLY on success when delete_original=True."""

    async def test_save_as_delete_original_true_removes_source_and_db_row_after_success(
        self, api_app
    ) -> None:
        storage_root: Path = api_app.state.settings.storage_dir
        repo = api_app.state.media_repo

        src = storage_root / "movies" / "Original.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"original-video")

        tmp = storage_root / "output" / "NewCopy.mkv.mkvforge.tmp"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"remuxed-video")

        repo.upsert(
            MediaFileUpsert(
                filepath="movies/Original.mkv",
                folder="/movies",
                filename="Original.mkv",
                size_bytes=14,
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
                raw_json="{}",
                scanned_at=utc_now_iso(),
            )
        )

        final_path, final_rel, original_deleted = await finalize_remux_output(
            src,
            tmp,
            output_mode=AppConstants.OUTPUT_MODE_SAVE_AS,
            storage_root=storage_root,
            output_path="output/NewCopy.mkv",
            delete_original=True,
            media_repo=repo,
        )

        assert final_rel == "output/NewCopy.mkv"
        assert final_path.is_file()
        assert final_path.read_bytes() == b"remuxed-video"
        assert original_deleted is True
        assert not src.exists()
        assert repo.get_by_filepath("movies/Original.mkv") is None

    async def test_save_as_delete_original_false_preserves_source(self, api_app) -> None:
        storage_root: Path = api_app.state.settings.storage_dir
        src = storage_root / "movies" / "KeepMe.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"keep-original")

        tmp = storage_root / "movies" / "KeepMeCopy.mkv.mkvforge.tmp"
        tmp.write_bytes(b"new-copy")

        final_path, _rel, original_deleted = await finalize_remux_output(
            src,
            tmp,
            output_mode=AppConstants.OUTPUT_MODE_SAVE_AS,
            storage_root=storage_root,
            output_path="movies/KeepMeCopy.mkv",
            delete_original=False,
        )

        assert final_path.is_file()
        assert src.is_file()
        assert original_deleted is False

    async def test_save_as_collision_failure_never_deletes_original(self, api_app) -> None:
        storage_root: Path = api_app.state.settings.storage_dir
        src = storage_root / "movies" / "SafeOriginal.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"safe-original")

        existing_dest = storage_root / "movies" / "AlreadyExists.mkv"
        existing_dest.write_bytes(b"existing")

        tmp = storage_root / "movies" / "AlreadyExists.mkv.mkvforge.tmp"
        tmp.write_bytes(b"new-data")

        with pytest.raises(TargetFileCollisionError):
            await finalize_remux_output(
                src,
                tmp,
                output_mode=AppConstants.OUTPUT_MODE_SAVE_AS,
                storage_root=storage_root,
                output_path="movies/AlreadyExists.mkv",
                delete_original=True,
            )

        # Original must remain completely intact on failure
        assert src.is_file()
        assert src.read_bytes() == b"safe-original"


class TestSaveAsAndMobileOutputModeFrontend:
    """Verify editor.js payload contract, delete confirmation wiring, and mobile single-line controls."""

    def test_editor_js_delete_original_defaults_false_and_only_applies_to_save_as(self) -> None:
        script = """
        const fs = require('fs');
        const vm = require('vm');
        const sandbox = { window: {} };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/constants.js', 'utf8'), sandbox);
        vm.runInNewContext(fs.readFileSync('app/static/js/editor.js', 'utf8'), sandbox);
        const Ed = sandbox.MKVForgeEditor;
        const fileDetail = {
          filepath: 'movies/Film.mkv',
          folder: '/movies',
          filename: 'Film.mkv',
          container_title: 'Old Title',
          has_global_tags: false,
          tracks: [
            { id: 0, type: 'video', codec: 'HEVC', language: 'und', track_name: '', default_track: true, forced_track: false },
          ],
          attachments: [],
        };
        const state = Ed.createEditorState(fileDetail, {});
        const defaultFlag = state.saveAs.deleteOriginal;

        // Toggle on in save_as mode -> payload sends delete_original: true
        state.outputMode = 'save_as';
        state.saveAs.deleteOriginal = true;
        const saveAsPayload = Ed.buildJobRequestPayload(state, fileDetail);

        // Switch back to keep_both -> payload MUST force delete_original: false
        state.outputMode = 'keep_both';
        const keepBothPayload = Ed.buildJobRequestPayload(state, fileDetail);

        process.stdout.write(JSON.stringify({
          defaultFlag,
          saveAsDelete: saveAsPayload.config.delete_original,
          keepBothDelete: keepBothPayload.config.delete_original,
        }));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout)
        assert res["defaultFlag"] is False
        assert res["saveAsDelete"] is True
        assert res["keepBothDelete"] is False

    async def test_ui_landmarks_for_save_as_toggle_confirm_setting_and_mobile_segmented_control(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "Delete original file" in html
        assert "editor.saveAs.deleteOriginal" in html
        assert "Confirm Before Deleting File" in html
        assert "userSettings.confirm_delete_file" in html
        assert "Save As & Delete Original?" in html
        assert 'Replace<span class="hidden sm:inline"> Original</span>' in html
