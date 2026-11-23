"""Integration tests for GET /api/files/{id}/info and the File Info modal UI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants
from app.db.repositories import MediaFileRepository, MediaFileUpsert


def _seed_media_row(repo: MediaFileRepository, *, raw_json: str) -> int:
    return repo.upsert(
        MediaFileUpsert(
            filepath="shows/Show/Ep1.mkv",
            folder="/shows/Show",
            filename="Ep1.mkv",
            size_bytes=500_000_000,
            mtime=1700000000.0,
            duration_us=1_000_000_000,
            resolution="1080p",
            video_codec="HEVC",
            video_dynamic_range="SDR",
            audio_summary_json='[{"codec":"EAC3","channels":6,"channel_layout":"5.1","language":"eng"}]',
            subtitle_count=1,
            container_title="Episode One",
            has_global_tags=True,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=raw_json,
            scanned_at="2026-11-19T20:00:00Z",
        )
    )


class TestFileInfoEndpointAndModal:
    """Verify /api/files/{id}/info API responses, error handling, and file-only context menu UI."""

    async def test_get_file_info_returns_full_metadata_and_404_when_missing(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        raw = json.dumps(
            {
                "container": {
                    "type": "Matroska",
                    "properties": {"title": "Episode One", "duration": 1_000_000_000_000},
                },
                "tracks": [
                    {
                        "id": 0,
                        "type": "video",
                        "codec": "HEVC/H.265/MPEG-H",
                        "properties": {"pixel_dimensions": "1920x1080", "default_track": True},
                    },
                    {
                        "id": 1,
                        "type": "audio",
                        "codec": "E-AC-3",
                        "properties": {"audio_channels": 6, "audio_sampling_frequency": 48000, "language": "eng"},
                    },
                    {
                        "id": 2,
                        "type": "subtitles",
                        "codec": "SubRip/SRT",
                        "properties": {"language": "eng", "text_subtitles": True},
                    },
                ],
                "attachments": [
                    {"id": 1, "file_name": "Font.ttf", "content_type": "font/ttf", "size": 12345}
                ],
                "chapters": [{"num_entries": 6}],
                "global_tags": [{"num_entries": 2}],
            }
        )
        file_id = _seed_media_row(api_app.state.media_repo, raw_json=raw)

        resp = await api_client.get(f"/api/files/{file_id}/info")
        assert resp.status_code == AppConstants.HTTP_OK
        body = resp.json()
        assert body["id"] == file_id
        assert body["container"]["container_type"] == "Matroska"
        assert body["container"]["attachments_count"] == 1
        assert body["container"]["chapters_count"] == 6
        assert body["container"]["global_tags_count"] == 2
        assert len(body["video_tracks"]) == 1
        assert len(body["audio_tracks"]) == 1
        assert len(body["subtitle_tracks"]) == 1
        assert body["attachments"][0]["file_name"] == "Font.ttf"

        missing = await api_client.get("/api/files/999999/info")
        assert missing.status_code == AppConstants.HTTP_NOT_FOUND

    async def test_corrupt_raw_json_still_returns_safe_info_payload(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        file_id = _seed_media_row(api_app.state.media_repo, raw_json="{corrupt json")
        resp = await api_client.get(f"/api/files/{file_id}/info")
        assert resp.status_code == AppConstants.HTTP_OK
        body = resp.json()
        assert body["container"]["container_type"] == "Unknown"
        assert body["attachments"] == []

    async def test_info_menu_is_file_only_and_formatters_handle_edge_cases(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "runContextMenuAction('info')" in html
        assert "contextMenu && contextMenu.kind === 'file'" in html
        assert "infoModal !== null" in html
        assert "No embedded attachments in container." in html

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        let fetchedInfoUrl = null;
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1440, innerHeight: 900,
          fetch: async (url) => {{
            fetchedInfoUrl = url;
            return {{ ok: true, status: 200, json: async () => ({{ id: 5, filename: "A.mkv", filepath: "A.mkv", container: {{ container_type: "Matroska" }}, video_tracks: [], audio_tracks: [], subtitle_tracks: [], other_tracks: [], attachments: [], warnings: [] }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Fmt = ctx.MKVForgeFormatters;
        const app = ctx.mkvForgeApp();

        // Info action works even when file is actively processing (read-only)
        const busyFile = {{ id: 5, filepath: "movies/A.mkv", filename: "A.mkv" }};
        app.liveJob = {{ ...app.liveJob, status: "running", job_id: "j1", filepath: "movies/A.mkv", percent: 50 }};
        app.contextMenu = {{ kind: "file", file: busyFile, title: busyFile.filename }};
        app.runContextMenuAction("info");

        setTimeout(() => {{
          console.log(JSON.stringify({{
            fetchedInfoUrl,
            modalOpen: app.infoModal !== null,
            modalLoadedId: app.infoModal && app.infoModal.data ? app.infoModal.data.id : null,
            bitrateMbps: Fmt.formatBitrateBps(12500000),
            bitrateKbps: Fmt.formatBitrateBps(640000),
            bitrateZero: Fmt.formatBitrateBps(0),
            bitrateNull: Fmt.formatBitrateBps(null),
            sr48: Fmt.formatSampleRateHz(48000),
            sr441: Fmt.formatSampleRateHz(44100),
            srNull: Fmt.formatSampleRateHz(null),
          }}));
        }}, 50);
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["fetchedInfoUrl"] == "/api/files/5/info"
        assert out["modalOpen"] is True
        assert out["modalLoadedId"] == 5
        assert out["bitrateMbps"] == "12.50 Mbps"
        assert out["bitrateKbps"] == "640 kbps"
        assert out["bitrateZero"] == "—"
        assert out["bitrateNull"] == "—"
        assert out["sr48"] == "48 kHz"
        assert out["sr441"] == "44.1 kHz"
        assert out["srNull"] == "—"

    async def test_context_menu_process_opens_editor_and_guards_locked_or_invalid_files(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "runContextMenuAction('info')" in html
        assert "runContextMenuAction('process')" in html
        assert "openFileInfoModal(file)" in html

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const fetchCalls = [];
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1440, innerHeight: 900,
          fetch: async (url) => {{
            fetchCalls.push(url);
            return {{ ok: true, status: 200, json: async () => ({{ id: 7, filename: "Ready.mkv", filepath: "Ready.mkv", container: {{ container_type: "Matroska" }}, video_tracks: [], audio_tracks: [], subtitle_tracks: [], other_tracks: [], attachments: [], warnings: [] }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        app.route = "library";

        // 1. Positive: context menu Process on ready file opens MKV Editor
        const readyFile = {{ id: 7, filepath: "movies/Ready.mkv", filename: "Ready.mkv", scan_status: "ready" }};
        app.contextMenu = {{ kind: "file", file: readyFile, title: readyFile.filename }};
        app.runContextMenuAction("process");
        const hashAfterReadyProcess = ctx.location.hash;

        // 2. Negative: context menu Process on busy file is blocked with toast
        ctx.location.hash = "#/library";
        const busyFile = {{ id: 8, filepath: "movies/Busy.mkv", filename: "Busy.mkv", scan_status: "ready" }};
        app.liveJob = {{ ...app.liveJob, status: "running", job_id: "j8", filepath: "movies/Busy.mkv", percent: 42 }};
        app.contextMenu = {{ kind: "file", file: busyFile, title: busyFile.filename }};
        app.runContextMenuAction("process");
        const hashAfterBusyProcess = ctx.location.hash;
        const busyToastCount = app.toasts.length;

        // 3. Negative: context menu Process on scanning file is blocked
        const scanningFile = {{ id: 9, filepath: "movies/Scan.mkv", filename: "Scan.mkv", scan_status: "scanning" }};
        app.contextMenu = {{ kind: "file", file: scanningFile, title: scanningFile.filename }};
        app.runContextMenuAction("process");
        const hashAfterScanningProcess = ctx.location.hash;

        // 4. Negative: context menu Process on scan-error file is blocked
        const errorFile = {{ id: 10, filepath: "movies/Err.mkv", filename: "Err.mkv", scan_status: "error" }};
        app.contextMenu = {{ kind: "file", file: errorFile, title: errorFile.filename }};
        app.runContextMenuAction("process");
        const hashAfterErrorProcess = ctx.location.hash;

        // 5. Negative: tapping scanning/error/null files to open Info modal safely blocks without fetching
        app.openFileInfoModal(null);
        app.openFileInfoModal({{}});
        app.openFileInfoModal(scanningFile);
        app.openFileInfoModal(errorFile);
        const modalAfterGuardedCalls = app.infoModal;
        const fetchesAfterGuardedCalls = fetchCalls.length;

        // 6. Positive: tapping ready file opens Info modal and fetches /api/files/7/info
        app.openFileInfoModal(readyFile);
        setTimeout(() => {{
          console.log(JSON.stringify({{
            hashAfterReadyProcess,
            hashAfterBusyProcess,
            busyToastCount,
            hashAfterScanningProcess,
            hashAfterErrorProcess,
            modalAfterGuardedCalls,
            fetchesAfterGuardedCalls,
            modalOpenAfterReady: app.infoModal !== null,
            loadedIdAfterReady: app.infoModal && app.infoModal.data ? app.infoModal.data.id : null,
            fetchCalls,
          }}));
        }}, 50);
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["hashAfterReadyProcess"] == "#/files/7"
        assert out["hashAfterBusyProcess"] == "#/library"
        assert out["busyToastCount"] >= 1
        assert out["hashAfterScanningProcess"] == "#/library"
        assert out["hashAfterErrorProcess"] == "#/library"
        assert out["modalAfterGuardedCalls"] is None
        assert out["fetchesAfterGuardedCalls"] == 0
        assert out["modalOpenAfterReady"] is True
        assert out["loadedIdAfterReady"] == 7
        assert out["fetchCalls"] == ["/api/files/7/info"]

    async def test_touch_long_press_select_mode_ghost_click_scroll_cancel_and_context_menu(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "startFileLongPress(file, $event)" in html
        assert "onFileTileClick(file)" in html

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const fetchCalls = [];
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 390, innerHeight: 844,
          fetch: async (url) => {{
            fetchCalls.push(url);
            return {{ ok: true, status: 200, json: async () => ({{ id: 1, filename: "A.mkv", filepath: "A.mkv", container: {{ container_type: "Matroska" }}, video_tracks: [], audio_tracks: [], subtitle_tracks: [], other_tracks: [], attachments: [], warnings: [] }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        const file1 = {{ id: 1, filepath: "A.mkv", filename: "A.mkv", scan_status: "ready" }};
        const file2 = {{ id: 2, filepath: "B.mkv", filename: "B.mkv", scan_status: "ready" }};
        app.files = [file1, file2];

        (async () => {{
          // 1. Scroll-safe cancellation: moving > 10px during touch cancels long-press
          app.startFileLongPress(file1, {{ touches: [{{ clientX: 100, clientY: 100 }}] }});
          app.moveFileLongPress({{ touches: [{{ clientX: 100, clientY: 118 }}] }});
          await new Promise((r) => setTimeout(r, 430));
          const modeAfterScrollCancel = app.mobileSelectMode;
          const selectedAfterScrollCancel = [...app.selectedFileIds];
          app.endFileLongPress();

          // 2. Clean 400ms long-press enters mobileSelectMode, selects file1, suppresses touch contextmenu & trailing ghost click!
          app.startFileLongPress(file1, {{ touches: [{{ clientX: 100, clientY: 100 }}] }});
          app.handleFileCardContextMenu(file1, {{ clientX: 100, clientY: 100 }});
          const contextMenuDuringTouch = app.contextMenu;
          await new Promise((r) => setTimeout(r, 430));
          app.endFileLongPress();
          // Trailing ghost click fired by mobile browser right after touchend
          app.onFileTileClick(file1);
          const modeAfterLongPress = app.mobileSelectMode;
          const selectedAfterGhostClick = [...app.selectedFileIds];
          const infoModalAfterGhostClick = app.infoModal;

          // 3. Subsequent normal tap on file2 in select mode selects file2
          app.onFileTileClick(file2);
          const selectedAfterSecondTap = [...app.selectedFileIds];

          // 4. Desktop mouse right-click (outside touch suppress window) opens context menu normally
          app._suppressClickUntilMs = 0;
          app._touchPressActive = false;
          app.handleFileCardContextMenu(file1, {{ clientX: 120, clientY: 120, pointerType: "mouse" }});
          const desktopContextMenuOpened = Boolean(app.contextMenu && app.contextMenu.kind === "file");

          console.log(JSON.stringify({{
            modeAfterScrollCancel,
            selectedAfterScrollCancel,
            contextMenuDuringTouch,
            modeAfterLongPress,
            selectedAfterGhostClick,
            infoModalAfterGhostClick,
            selectedAfterSecondTap,
            desktopContextMenuOpened,
          }}));
        }})();
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["modeAfterScrollCancel"] is False
        assert out["selectedAfterScrollCancel"] == []
        assert out["contextMenuDuringTouch"] is None
        assert out["modeAfterLongPress"] is True
        assert out["selectedAfterGhostClick"] == [1]
        assert out["infoModalAfterGhostClick"] is None
        assert out["selectedAfterSecondTap"] == [1, 2]
        assert out["desktopContextMenuOpened"] is True

    async def test_editor_attachment_and_tag_inspection_and_selective_removal(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        raw_payload = {
            "container": {"type": "Matroska", "properties": {"duration": 60_000_000_000}},
            "container_tags": {"TITLE": "Downloaded From PromoSite"},
            "global_tags": [{"num_entries": 1}],
            "track_tags": [{"num_entries": 2, "track_id": 0}],
            "attachments": [
                {
                    "id": 1,
                    "file_name": "Logo-Attachment.png",
                    "content_type": "image/png",
                    "size": 25923,
                    "properties": {"uid": 164502148357},
                },
                {
                    "id": 2,
                    "file_name": "Font.ttf",
                    "content_type": "application/x-truetype-font",
                    "size": 120000,
                },
            ],
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC",
                    "properties": {"pixel_dimensions": "1920x1080", "tag_title": "Video Promo"},
                }
            ],
        }
        file_id = api_app.state.media_repo.upsert(
            MediaFileUpsert(
                filepath="movies/Promo.mkv",
                folder="/movies",
                filename="Promo.mkv",
                size_bytes=500000,
                mtime=1700000000.0,
                duration_us=60_000_000,
                resolution="1080p",
                video_codec="HEVC",
                video_dynamic_range="SDR",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=True,
                has_aac_51=False,
                has_ac3_eac3=False,
                raw_json=json.dumps(raw_payload),
                scanned_at="2026-11-19T20:00:00Z",
            )
        )

        detail_resp = await api_client.get(f"/api/files/{file_id}")
        assert detail_resp.status_code == AppConstants.HTTP_OK
        detail = detail_resp.json()
        assert detail["global_tags_count"] == 1
        assert detail["track_tags_count"] == 2
        assert len(detail["attachments"]) == 2
        assert detail["attachments"][0]["file_name"] == "Logo-Attachment.png"
        assert detail["attachments"][0]["uid"] == "164502148357"
        assert any(
            t["scope"] == "Global" and t["key"] == "TITLE" and t["value"] == "Downloaded From PromoSite"
            for t in detail["tag_entries"]
        )

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ console }};
        ctx.window = ctx;
        vm.createContext(ctx);
        vm.runInContext(fs.readFileSync({json.dumps(str(js_dir / "constants.js"))}, "utf8"), ctx);
        vm.runInContext(fs.readFileSync({json.dumps(str(js_dir / "editor.js"))}, "utf8"), ctx);
        const Ed = ctx.MKVForgeEditor;
        const detail = {json.dumps(detail)};
        const state = Ed.createEditorState(detail);

        // Default state: all tags & attachments are set to remove (unchecked), chapters are kept (checked)
        const defaultStripGlobal = state.stripGlobalTags;
        const defaultStripTrack = state.stripTrackTags;
        const defaultStripAttachments = state.stripAttachments;
        const defaultStripChapters = state.stripChapters;
        const defaultAttEnabled = state.attachments.map((a) => a.enabled);
        const payloadDefaultRemoved = Ed.buildJobRequestPayload(state, detail);

        // 1. Check attachment #2 (Font.ttf) to keep it while #1 (Logo-Attachment.png) stays unchecked (removed)
        Ed.toggleAttachmentEnabled(state, 2);
        const hasPendingPartial = Ed.hasPendingChanges(state, detail);
        const summaryPartial = Ed.buildPendingSummary(state, detail);
        const payloadPartial = Ed.buildJobRequestPayload(state, detail);

        // 2. Also check attachment #1 so both attachments are kept
        Ed.toggleAttachmentEnabled(state, 1);
        const payloadAllKept = Ed.buildJobRequestPayload(state, detail);

        console.log(JSON.stringify({{
          defaultStripGlobal,
          defaultStripTrack,
          defaultStripAttachments,
          defaultStripChapters,
          defaultAttEnabled,
          hasPendingPartial,
          summaryPartial,
          partialContainer: payloadPartial.config.container,
          removedContainer: payloadDefaultRemoved.config.container,
          keptContainer: payloadAllKept.config.container,
        }}));
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        assert out["defaultStripGlobal"] is True
        assert out["defaultStripTrack"] is True
        assert out["defaultStripAttachments"] is True
        assert out["defaultStripChapters"] is False
        assert out["defaultAttEnabled"] == [False, False]
        assert out["hasPendingPartial"] is True
        assert "Drop 1 Attachment" in out["summaryPartial"]
        assert out["partialContainer"]["strip_attachments"] is False
        assert out["partialContainer"]["keep_attachment_ids"] == [2]
        assert out["removedContainer"]["strip_attachments"] is True
        assert out["removedContainer"]["keep_attachment_ids"] is None
        assert out["keptContainer"]["strip_attachments"] is False
        assert out["keptContainer"]["keep_attachment_ids"] is None
