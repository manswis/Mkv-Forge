"""Tests for MKV Editor 'Sanitize metadata' action and Library pinned-bottom pagination scroll layout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TestEditorSanitizeMetadata:
    """Verify track title formatting and 'Sanitize metadata' button wiring."""

    def test_format_track_badge_matches_expected_video_audio_subtitle_formats(self) -> None:
        script = """
        const fs = require('fs');
        const vm = require('vm');
        const sandbox = { window: {} };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/constants.js', 'utf8'), sandbox);
        vm.runInNewContext(fs.readFileSync('app/static/js/editor.js', 'utf8'), sandbox);
        const Ed = sandbox.MKVForgeEditor;
        const results = {
          video: Ed.formatTrackBadge({
            type: 'video',
            codec: 'HEVC/H.265/MPEG-H',
            pixel_dimensions: '1920x1080',
            language: 'und',
          }),
          audio: Ed.formatTrackBadge({
            type: 'audio',
            codec: 'AAC',
            audio_channels: 6,
            language: 'hin',
          }),
          subtitle: Ed.formatTrackBadge({
            type: 'subtitles',
            codec: 'SubRip/SRT',
            language: 'eng',
          }),
          fallback: Ed.formatTrackBadge({
            type: 'audio',
            codec: '',
            audio_channels: null,
            language: 'und',
          }),
          addedEac3: Ed.buildDefaultAudioTitle('hin', 'eac3', 6, '640k'),
          addedAc3: Ed.buildDefaultAudioTitle('eng', 'ac3', 6, '448k'),
          addedUnd: Ed.buildDefaultAudioTitle('und', 'eac3', 2, '384k'),
        };
        process.stdout.write(JSON.stringify(results));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        assert data["video"] == "Video • HEVC/H.265/MPEG-H • 1920x1080"
        assert data["audio"] == "Audio • AAC • 5.1 • hin"
        assert data["subtitle"] == "Sub • SubRip/SRT • eng"
        assert data["fallback"] == "Audio • Unknown"
        assert data["addedEac3"] == "EAC3 5.1 • 640k • hin • muxed"
        assert data["addedAc3"] == "AC3 5.1 • 448k • eng • muxed"
        assert data["addedUnd"] == "EAC3 2.0 • 384k • muxed"

    async def test_editor_ui_contains_sanitize_metadata_text_button(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/")
        assert resp.status_code == AppConstants.HTTP_OK
        html = resp.text

        assert "Smart fill" in html
        assert '@click="sanitizeTrackMetadata(row.track)"' in html
        assert ':disabled="row.track.track_name === Ed.formatTrackBadge(row.track)"' in html

        app_js = (_PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        assert "sanitizeTrackMetadata(track)" in app_js
        assert "if (!track || !track.enabled) return;" in app_js
        assert "track.track_name = Ed.formatTrackBadge(track);" in app_js


class TestLibraryScrollViewportAndPagination:
    """Verify Library uses a dynamic scroll viewport with pinned bottom pagination respecting Settings."""

    async def test_library_pagination_sits_below_scroll_container_and_respects_page_limit(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/")
        assert resp.status_code == AppConstants.HTTP_OK
        html = resp.text

        scroll_idx = html.index('x-ref="libraryScrollContainer"')
        other_files_idx = html.index("Other Files")
        pagination_idx = html.index("Pinned Bottom Pagination Controls")
        prev_idx = html.index("← Previous")
        next_idx = html.index("Next →")

        # File list + Other Files stay inside the scroll container; Pagination is anchored cleanly below it
        assert scroll_idx < other_files_idx < pagination_idx < prev_idx < next_idx
        assert "overflow-y-auto" in html[scroll_idx : scroll_idx + 200]
        assert "`${limit} per page`" in html

        # Changing page_limit in Settings triggers Library reload from page 1
        app_js = (_PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        assert "patch.page_limit !== undefined" in app_js
        assert "this.$refs?.libraryScrollContainer" in app_js

    async def test_settings_page_limit_controls_api_files_page_size(
        self, api_client: httpx.AsyncClient
    ) -> None:
        patch_resp = await api_client.patch("/api/settings", json={"page_limit": 25})
        assert patch_resp.status_code == AppConstants.HTTP_OK
        assert patch_resp.json()["page_limit"] == 25

        # Invalid page_limit is safely rejected with 422
        bad_resp = await api_client.patch("/api/settings", json={"page_limit": 13})
        assert bad_resp.status_code == httpx.codes.UNPROCESSABLE_ENTITY


class TestRemoveAllMetadataDefaultAndSanitizeAll:
    """Verify 'Remove All Metadata by Default' setting, editor clean/preserve states, and 'Sanitize all'."""

    async def test_settings_default_remove_all_metadata_round_trip(
        self, api_client: httpx.AsyncClient
    ) -> None:
        r0 = await api_client.get("/api/settings")
        assert r0.status_code == AppConstants.HTTP_OK
        assert r0.json()["default_remove_all_metadata"] is True

        r1 = await api_client.patch(
            "/api/settings", json={"default_remove_all_metadata": False}
        )
        assert r1.status_code == AppConstants.HTTP_OK
        assert r1.json()["default_remove_all_metadata"] is False

        r2 = await api_client.patch(
            "/api/settings", json={"default_remove_all_metadata": True}
        )
        assert r2.status_code == AppConstants.HTTP_OK
        assert r2.json()["default_remove_all_metadata"] is True

    def test_editor_state_clean_by_default_vs_preserve_when_disabled(self) -> None:
        script = """
        const fs = require('fs');
        const vm = require('vm');
        const sandbox = { window: {} };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/constants.js', 'utf8'), sandbox);
        vm.runInNewContext(fs.readFileSync('app/static/js/editor.js', 'utf8'), sandbox);
        const Ed = sandbox.MKVForgeEditor;

        const fileDetail = {
          id: 42,
          filepath: 'movies/Release.mkv',
          folder: '/movies',
          filename: 'Release.mkv',
          container_title: 'Junk.Release.Group.Title',
          has_global_tags: true,
          has_track_tags: true,
          attachments_count: 1,
          attachments: [
            { id: 1, file_name: 'font.ttf', content_type: 'font/ttf', size_bytes: 1234 },
          ],
          tracks: [
            { id: 0, type: 'video', codec: 'HEVC', pixel_dimensions: '1920x1080', language: 'und', track_name: 'Rip Video', default_track: true, forced_track: false },
            { id: 1, type: 'audio', codec: 'AAC', audio_channels: 6, language: 'hin', track_name: 'Rip Audio', default_track: true, forced_track: false },
          ],
        };

        // 1. Default ON -> clean slate, chapters preserved, attachments unchecked
        const cleanState = Ed.createEditorState(fileDetail, { default_remove_all_metadata: true });
        const cleanHasChanges = Ed.hasPendingChanges(cleanState, fileDetail);

        // Re-checking attachment manually restores it and clears stripAttachments
        Ed.toggleAttachmentEnabled(cleanState, 1);
        const afterReenableAtt = {
          attEnabled: cleanState.attachments[0].enabled,
          stripAttachments: cleanState.stripAttachments,
        };

        // 2. Disabled OFF -> everything preserved untouched, no pending changes
        const keepState = Ed.createEditorState(fileDetail, { default_remove_all_metadata: false });
        const keepHasChanges = Ed.hasPendingChanges(keepState, fileDetail);

        process.stdout.write(JSON.stringify({
          clean: {
            containerTitle: cleanState.containerTitle,
            trackNames: cleanState.tracks.map((t) => t.track_name),
            stripGlobalTags: cleanState.stripGlobalTags,
            stripTrackTags: cleanState.stripTrackTags,
            stripChapters: cleanState.stripChapters,
            hasChanges: cleanHasChanges,
            afterReenableAtt,
          },
          keep: {
            containerTitle: keepState.containerTitle,
            trackNames: keepState.tracks.map((t) => t.track_name),
            stripGlobalTags: keepState.stripGlobalTags,
            stripTrackTags: keepState.stripTrackTags,
            stripAttachments: keepState.stripAttachments,
            attEnabled: keepState.attachments[0].enabled,
            hasChanges: keepHasChanges,
          },
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

        assert res["clean"]["containerTitle"] == ""
        assert res["clean"]["trackNames"] == ["", ""]
        assert res["clean"]["stripGlobalTags"] is True
        assert res["clean"]["stripTrackTags"] is True
        assert res["clean"]["stripChapters"] is False
        assert res["clean"]["hasChanges"] is True
        assert res["clean"]["afterReenableAtt"] == {
            "attEnabled": True,
            "stripAttachments": False,
        }

        assert res["keep"]["containerTitle"] == "Junk.Release.Group.Title"
        assert res["keep"]["trackNames"] == ["Rip Video", "Rip Audio"]
        assert res["keep"]["stripGlobalTags"] is False
        assert res["keep"]["stripTrackTags"] is False
        assert res["keep"]["stripAttachments"] is False
        assert res["keep"]["attEnabled"] is True
        assert res["keep"]["hasChanges"] is False

    async def test_ui_landmarks_for_remove_all_metadata_and_sanitize_all(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "Remove All Metadata by Default" in html
        assert "userSettings.default_remove_all_metadata" in html
        assert "Smart fill all" in html
        assert '@click="sanitizeAllTracksMetadata()"' in html
        # Structured high-contrast Output Preview & 2-line full-filename List View
        assert "Filename &amp; Streams" in html
        assert "[overflow-wrap:anywhere]" in html
        assert "Deleted after Save As succeeds" in html
        input_css = (_PROJECT_ROOT / "frontend/input.css").read_text(encoding="utf-8")
        assert "--color-status-ok:" in input_css
        assert "--color-status-warn:" in input_css

        # Queued file lock banner & notification landmarks
        assert "Controls are locked. Remove this file from the job queue to modify." in html
        assert "Remove from Queue" in html
        app_js = (_PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        assert "_busyLockMessage(file)" in app_js
        assert "Remove it from the job queue to modify." in app_js
        assert "this.showToast(this._busyLockMessage(detail), \"info\")" in app_js

