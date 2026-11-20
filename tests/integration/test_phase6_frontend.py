"""Integration and JS-contract tests for Phase 6 File Editor (+ Add Audio) & Live Jobs Drawer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.models.schemas import JobCreateRequest


class TestPhase6FrontendLandmarks:
    """Verify Phase 6 assets, offline compliance, + Add Audio modal, and Jobs Drawer."""

    async def test_editor_js_served_offline_with_zero_external_urls(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/static/js/editor.js")
        assert resp.status_code == AppConstants.HTTP_OK
        assert len(resp.content) > 0
        assert re.search(r"https?://", resp.text) is None

    async def test_index_html_contains_editor_and_live_jobs_drawer(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert re.search(r"https?://", html) is None
        assert "/static/js/editor.js" in html

        # Clean editor + unified Add / Convert menu + Base Audio modal + collapsed Advanced
        assert "Metadata Cleanup" in html
        assert "Add / Convert" in html
        assert "Import Subtitle…" in html
        assert "Import Audio…" in html
        assert "Base Audio Track (Source)" in html
        assert "Convert Audio To" in html
        assert "Dolby Ready" in html
        assert "Advanced Settings" in html
        assert "Audio Index 0 • Default" in html
        assert "Demoted (non-default)" in html
        assert "Dropped by Audio Fix" in html

        # Safety guardrails & sticky bottom bar (with No-Op guard + Output Behavior outside Advanced)
        assert "Warning: No video track selected" in html
        assert "Process File" in html
        assert "editorCanSubmit" in html
        assert "Output Behavior" in html
        assert "Save As…" in html
        assert (
            html.index("editor.outputMode = C.EDITOR.OUTPUT_KEEP_BOTH")
            < html.index("editor.outputMode = C.EDITOR.OUTPUT_REPLACE")
            < html.index("editor.outputMode = C.EDITOR.OUTPUT_SAVE_AS")
        )
        assert "Original Untouched" in html
        assert "Choose Destination Folder" in html
        assert "/static/js/folder_tree.js" in html
        assert "editorOutputPreview.targetFilename" in html
        assert "editorOutputPreview.originalFilename" in html
        assert "editorOutputPreview.targetDisplayPath" in html
        assert "Open Jobs Drawer" in html

        # Output Behavior card MUST appear BEFORE Advanced Settings (not buried inside it)
        assert html.index("Output Behavior") < html.index("Advanced Settings")

        # Live Jobs Drawer + Cancel + Smart-scrolling CLI log viewer
        assert "Live Jobs Drawer" in html
        assert "Cancel Job" in html
        assert "Show Live CLI Logs" in html
        assert "onLogScroll" in html
        assert "Jump to latest" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestEditorJsLogicAndBackendContract:
    """Execute app/static/js/editor.js in Node and validate logical matrix & Pydantic contract."""

    def test_logical_codec_matrix_noop_guard_and_pydantic_contract(
        self, project_root: Path
    ) -> None:
        script = f"""
        global.window = global;
        require({json.dumps(str(project_root / 'app/static/js/constants.js'))});
        require({json.dumps(str(project_root / 'app/static/js/editor.js'))});

        const Ed = window.MKVForgeEditor;

        function makeFile(filename, audioCodec, audioChannels, hasTags) {{
          return {{
            id: 42,
            filepath: "movies/" + filename,
            filename,
            container_title: hasTags ? "Junk.Title" : "",
            has_global_tags: hasTags,
            has_metadata: hasTags,
            has_aac_51: audioCodec === "AAC" && audioChannels === 6,
            has_ac3_eac3: audioCodec === "E-AC-3" || audioCodec === "AC-3",
            attachments_count: 0,
            chapters_count: 0,
            tracks: [
              {{ id: 0, type: "video", codec: "HEVC", language: "und", track_name: "", default_track: true, forced_track: false, pixel_dimensions: "1920x1080", minimum_timestamp_ns: 0, sync_delay_ms: 0 }},
              {{ id: 1, type: "audio", codec: audioCodec, language: "eng", track_name: "English", default_track: true, forced_track: false, audio_channels: audioChannels, audio_index: 0, minimum_timestamp_ns: 42000000, sync_delay_ms: 42 }},
            ],
          }};
        }}

        // 1. Clean EAC3 5.1 movie (like A Million Miles Away): EAC3 must NOT be offered, no-op guard = false when preserving metadata
        const eac3File = makeFile("A Million Miles Away.mkv", "E-AC-3", 6, false);
        const eac3State = Ed.createEditorState(eac3File, {{ default_remove_all_metadata: false }});
        const eac3Options = Ed.getAvailableAddAudioOptions(eac3State).map(o => o.codec);
        const eac3InitialHasChanges = Ed.hasPendingChanges(eac3State, eac3File);

        // 2. AC3 5.1 movie: only AAC offered
        const ac3Options = Ed.getAvailableAddAudioOptions(
          Ed.createEditorState(makeFile("Band.mkv", "AC-3", 6, false))
        ).map(o => o.codec);

        // 3. AAC 5.1 movie: EAC3 and AC3 offered
        const aacFile = makeFile("aac_51.mp4", "AAC", 6, true);
        const aacState = Ed.createEditorState(aacFile);
        const aacOptions = Ed.getAvailableAddAudioOptions(aacState).map(o => o.codec);

        // 4. DTS 5.1 movie: EAC3, AC3, AAC all offered
        const dtsOptions = Ed.getAvailableAddAudioOptions(
          Ed.createEditorState(makeFile("Dts.mkv", "DTS", 6, false))
        ).map(o => o.codec);

        // Stage AC3 5.1 added from base track #1 on EAC3 file
        eac3State.audioFix = {{
          enabled: true,
          sourceTrackId: 1,
          targetCodec: "ac3",
          bitrate: "448k",
          language: "eng",
          trackTitle: "English AC3 5.1",
          titleManuallyEdited: false,
          setAsDefault: true,
          dropSourceTrack: false,
        }};

        const display = Ed.buildDisplayTracks(eac3State);
        const summary = Ed.buildPendingSummary(eac3State, eac3File);
        const ac3Payload = Ed.buildJobRequestPayload(eac3State, eac3File);
        const eac3AfterAddHasChanges = Ed.hasPendingChanges(eac3State, eac3File);

        // Also test AAC target payload contract
        eac3State.audioFix.targetCodec = "aac";
        eac3State.audioFix.bitrate = "320k";
        const aacTargetPayload = Ed.buildJobRequestPayload(eac3State, eac3File);

        // Test default Keep Both + real-filename output previews (Keep Both, Replace MP4, Save As valid & invalid)
        const defaultOutputMode = aacState.outputMode;
        const mp4KeepPreview = Ed.buildOutputPreview(aacState, aacFile);
        aacState.outputMode = "replace";
        const mp4ReplacePreview = Ed.buildOutputPreview(aacState, aacFile);

        eac3State.outputMode = "save_as";
        // Same path as original must be rejected!
        eac3State.saveAs = {{ folder: "/movies", filename: "A Million Miles Away.mkv" }};
        const saveAsSamePreview = Ed.buildOutputPreview(eac3State, eac3File);
        // Traversal must be rejected!
        eac3State.saveAs = {{ folder: "../escape", filename: "Out.mkv" }};
        const saveAsTraversalPreview = Ed.buildOutputPreview(eac3State, eac3File);
        // Valid folder from tree picker + auto .mkv suffix
        eac3State.saveAs = {{ folder: "/test/dolby", filename: "A Million Miles Away (AC3)" }};
        const saveAsValidPreview = Ed.buildOutputPreview(eac3State, eac3File);
        const saveAsPayload = Ed.buildJobRequestPayload(eac3State, eac3File);

        console.log(JSON.stringify({{
          eac3Options,
          ac3Options,
          aacOptions,
          dtsOptions,
          eac3InitialHasChanges,
          eac3AfterAddHasChanges,
          defaultOutputMode,
          display,
          summary,
          ac3Payload,
          aacTargetPayload,
          mp4ReplacePreview,
          mp4KeepPreview,
          saveAsSamePreview,
          saveAsTraversalPreview,
          saveAsValidPreview,
          saveAsPayload,
        }}));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        # Logical codec dropdown matrix verified!
        assert out["eac3Options"] == ["ac3", "aac"]
        assert out["ac3Options"] == ["aac"]
        assert out["aacOptions"] == ["eac3", "ac3"]
        assert out["dtsOptions"] == ["eac3", "ac3", "aac"]

        # No-Op Submit Guard & Keep Both default verified!
        assert out["defaultOutputMode"] == "keep_both"
        assert out["eac3InitialHasChanges"] is False
        assert out["eac3AfterAddHasChanges"] is True

        # Highlighted inline [NEW] row shows base audio track/index and demotes original
        display = out["display"]
        assert display[1]["isVirtual"] is True
        assert display[1]["codecBadge"] == "AC3 5.1"
        assert "Track #1" in display[1]["sourceSummary"]
        assert "Audio #0" in display[1]["sourceSummary"]
        assert display[2]["isDemotedByNewDolby"] is True

        # Summary shows new format + source track
        assert "+ AC3 5.1 (from #1)" in out["summary"]

        # Real-filename output previews verified!
        assert out["mp4ReplacePreview"]["originalFilename"] == "aac_51.mp4"
        assert out["mp4ReplacePreview"]["targetFilename"] == "aac_51.mkv"
        assert out["mp4KeepPreview"]["targetFilename"] == "aac_51.remux.mkv"
        assert out["saveAsSamePreview"]["isSaveAsInvalid"] is True
        assert out["saveAsTraversalPreview"]["isSaveAsInvalid"] is True
        assert out["saveAsValidPreview"]["isSaveAsInvalid"] is False
        assert out["saveAsValidPreview"]["targetRelPath"] == "test/dolby/A Million Miles Away (AC3).mkv"
        assert out["saveAsValidPreview"]["targetDisplayPath"] == "/test/dolby/A Million Miles Away (AC3).mkv"

        # AC3, AAC, and Save-As payloads validate cleanly against backend Pydantic JobCreateRequest!
        ac3_req = JobCreateRequest.model_validate(out["ac3Payload"])
        assert ac3_req.config.audio_conversion.target_codec == "ac3"
        assert ac3_req.config.audio_conversion.bitrate == "448k"

        aac_req = JobCreateRequest.model_validate(out["aacTargetPayload"])
        assert aac_req.config.audio_conversion.target_codec == "aac"
        assert aac_req.config.audio_conversion.bitrate == "320k"

        save_as_req = JobCreateRequest.model_validate(out["saveAsPayload"])
        assert save_as_req.config.output_mode == "save_as"
        assert save_as_req.config.output_path == "test/dolby/A Million Miles Away (AC3).mkv"

    async def test_frontend_assets_force_etag_revalidation(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """HTML, JS, and CSS responses enforce Cache-Control: no-cache to prevent mixed bundles."""
        for path in ("/", "/static/js/app.js", "/static/js/constants.js", "/static/css/app.css"):
            resp = await api_client.get(path)
            assert resp.status_code == AppConstants.HTTP_OK
            assert resp.headers.get("cache-control") == AppConstants.CACHE_CONTROL_NO_CACHE

        # API endpoints remain unaffected by the static revalidation header
        dash = await api_client.get("/api/dashboard")
        assert dash.status_code == AppConstants.HTTP_OK
        assert dash.headers.get("cache-control") != AppConstants.CACHE_CONTROL_NO_CACHE

    async def test_files_listing_skips_grandchild_crawl_while_folders_endpoint_preserves_it(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        """GET /api/files?folder=/ stays shallow (no deep subdir crawl) while /api/folders keeps has_children."""
        storage_dir: Path = api_app.state.settings.storage_dir
        nested_dir = storage_dir / "movies" / "sci-fi"
        nested_dir.mkdir(parents=True, exist_ok=True)

        files_resp = await api_client.get("/api/files", params={"folder": "/"})
        assert files_resp.status_code == AppConstants.HTTP_OK
        subfolders = files_resp.json()["subfolders"]
        movies_entry = next(entry for entry in subfolders if entry["path"] == "/movies")
        assert movies_entry["has_children"] is False

        folders_resp = await api_client.get("/api/folders", params={"path": "/"})
        assert folders_resp.status_code == AppConstants.HTTP_OK
        folder_entries = folders_resp.json()["entries"]
        picker_movies = next(
            entry for entry in folder_entries if entry["path"] == "/movies"
        )
        assert picker_movies["has_children"] is True

    async def test_per_file_processing_lock_and_inline_progress_landmarks(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Verify UI landmarks for per-file busy locks, inline progress bars, Retry banner, and SSE lifecycle."""
        html = (await api_client.get("/")).text
        app_js = (await api_client.get("/static/js/app.js")).text
        api_js = (await api_client.get("/static/js/api.js")).text

        assert '<fieldset :disabled="isEditorFileBusy"' in html
        assert "isFileBusy(file)" in html
        assert "fileBusyProgressWidth(file)" in html
        assert "isContextMenuFileBusy" in html
        assert "View Live Progress" in html
        assert '@click="loadLibrary()"' in html
        assert "`Processing ${getFileJobStatus(file).percent}%`" not in html
        assert 'x-text="getFileJobStatus(file)?.state === C.STATUSES.RUNNING ? `${getFileJobStatus(file).percent}%` : `Q#${getFileJobStatus(file)?.queuePos || 1}`"' in html
        assert "@click=\"row.track.track_name = ''\"" in html

        assert "getFileJobStatus(file)" in app_js
        assert "isEditorFileBusy" in app_js
        assert "_disconnectStreams()" in app_js
        assert "beforeunload" in app_js
        assert "this.backToParentFolder();\n          this.openJobsDrawer();" in app_js
        assert "AbortController" in api_js

    def test_per_file_processing_lock_runtime_behavior(self) -> None:
        """Exercise per-file lock, selection guards, null-safety, and deferred SSE in Node."""
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const listeners = {{}};
        let sseOpen = 0;
        let sseTotalCreated = 0;
        const ctx = {{
          console, setTimeout: (fn) => {{ fn(); return 1; }}, clearTimeout: () => {{}},
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function(u) {{
            sseOpen++;
            sseTotalCreated++;
            this.url = u;
            this.addEventListener = () => {{}};
            this.close = () => {{ sseOpen--; }};
          }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: (name, fn) => {{ listeners[name] = fn; }},
          innerWidth: 1440, innerHeight: 900,
          fetch: async () => ({{
            ok: true, status: 200,
            json: async () => ({{
              items: [], subfolders: [], total: 0, total_pages: 0,
              folders: [], audio_types: [], facets: {{ aac51_only: 0, has_metadata: 0, no_dolby: 0, audio_types: [] }},
              folder_counts: {{}}, folder_exists: true,
              stats: {{ total_files: 3 }}, active: null, queued: [], history: [],
            }}),
          }}),
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        app.init();
        // Calling init() a second time must be a complete no-op (zero duplicate boot)
        app.init();
        const sseBeforeLoad = sseOpen;
        if (listeners.load) listeners.load();
        const sseAfterLoad = sseOpen;
        // Hidden tab releases SSE connection; resume restores single stream
        ctx.document.visibilityState = "hidden";
        app.handleAppHidden();
        const sseAfterHidden = sseOpen;
        ctx.document.visibilityState = "visible";
        app.handleAppResume();
        const sseAfterResume = sseOpen;
        if (listeners.beforeunload) listeners.beforeunload();
        const sseAfterUnload = sseOpen;

        const fRun = {{ id: 1, filepath: "movies/run.mkv", filename: "run.mkv" }};
        const fQueue = {{ id: 2, filepath: "movies/queue.mkv", filename: "queue.mkv" }};
        const fIdle = {{ id: 3, filepath: "movies/idle.mkv", filename: "idle.mkv" }};
        app.files = [fRun, fQueue, fIdle];
        app.liveJob = {{
          ...app.liveJob,
          status: "running",
          job_id: "job-1",
          filepath: "movies/run.mkv",
          percent: 0,
          stage_label: "Converting audio",
        }};
        app.jobsState = {{
          active: {{ id: "job-1", filepath: "movies/run.mkv", progress: 0 }},
          queued: [{{ id: "job-2", filepath: "movies/queue.mkv" }}],
          history: [],
        }};

        // Negative & null-safety checks
        const nullStatus = app.getFileJobStatus(null);
        const missingPathStatus = app.getFileJobStatus({{ id: 99 }});

        // Running (0% floor check), queued (#1), and idle file checks
        const runStatus = app.getFileJobStatus(fRun);
        const runBarFloor = app.fileBusyProgressWidth(fRun);
        const queueStatus = app.getFileJobStatus(fQueue);
        const idleBusy = app.isFileBusy(fIdle);

        // Selection guards must reject busy files and keep idle files selectable
        app.toggleFileSelection(1);
        app.toggleFileSelection(2);
        const afterBusyToggles = [...app.selectedFileIds];
        app.toggleSelectAllPage();
        const afterSelectAll = [...app.selectedFileIds];

        // Context menu destructive guard on busy file
        let renameModalBefore = app.renameModal;
        app.contextMenu = {{ kind: "file", file: fRun, title: fRun.filename }};
        const menuBusy = app.isContextMenuFileBusy;
        app.runContextMenuAction("rename");
        const renameModalAfterBusy = app.renameModal;

        // Editor lock follows open file specifically
        app.editorFile = fRun;
        const editorBusyForRunning = app.isEditorFileBusy;
        app.editorFile = fQueue;
        const editorBusyForQueued = app.isEditorFileBusy;
        app.editorFile = fIdle;
        const editorBusyForIdle = app.isEditorFileBusy;

        console.log(JSON.stringify({{
          sseBeforeLoad,
          sseAfterLoad,
          sseAfterHidden,
          sseAfterResume,
          sseAfterUnload,
          sseTotalCreated,
          nullStatus,
          missingPathStatus,
          runStatus,
          runBarFloor,
          queueStatus,
          idleBusy,
          afterBusyToggles,
          afterSelectAll,
          menuBusy,
          renameBlocked: renameModalBefore === null && renameModalAfterBusy === null,
          editorBusyForRunning,
          editorBusyForQueued,
          editorBusyForIdle,
        }}));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        assert out["sseBeforeLoad"] == 0
        assert out["sseAfterLoad"] == 1
        assert out["sseAfterHidden"] == 0
        assert out["sseAfterResume"] == 1
        assert out["sseAfterUnload"] == 0
        assert out["nullStatus"] is None
        assert out["missingPathStatus"] is None
        assert out["runStatus"]["state"] == "running"
        assert out["runStatus"]["percent"] == 0
        assert out["runBarFloor"] == 4
        assert out["queueStatus"]["state"] == "queued"
        assert out["queueStatus"]["queuePos"] == 1
        assert out["idleBusy"] is False
        assert out["afterBusyToggles"] == []
        assert out["afterSelectAll"] == [3]
        assert out["menuBusy"] is True
        assert out["renameBlocked"] is True
        assert out["editorBusyForRunning"] is True
        assert out["editorBusyForQueued"] is True
        assert out["editorBusyForIdle"] is False

    async def test_editor_parent_navigation_and_tappable_breadcrumbs(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Verify Editor back button targets parent folder and every path segment is tappable."""
        html = (await api_client.get("/")).text
        assert '@click="backToParentFolder()"' in html
        assert "editorFolderBreadcrumbs" in html
        assert '@click="openFolderInLibrary(crumb.path)"' in html

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const hashLog = [];
        const ctx = {{
          console, setTimeout: () => 1, clearTimeout: () => {{}},
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{
            _hash: "#/files/42",
            get hash() {{ return this._hash; }},
            set hash(v) {{ this._hash = v; hashLog.push(v); }},
            origin: "http://localhost",
          }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1440, innerHeight: 900,
          fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const FT = ctx.MKVForgeFolderTree;
        const app = ctx.mkvForgeApp();

        // Pure helper edge cases (nested with spaces, root, null/empty)
        const nestedCrumbs = FT.buildBreadcrumbs("/shows/Adarsh Baal Vidyalaya 2026/Season 1", {{ includeRoot: false }});
        const rootCrumbs = FT.buildBreadcrumbs("/", {{ includeRoot: false }});
        const nullCrumbs = FT.buildBreadcrumbs(null, {{ includeRoot: false }});
        const parentNameNested = FT.getParentFolderName("/shows/Adarsh Baal Vidyalaya 2026/Season 1/");
        const parentNameRoot = FT.getParentFolderName("/");

        // Editor state: user opened file from All Files + active search
        app.route = "files";
        app.libraryView = "list";
        app.searchQuery = "Adarsh";
        app.editorFile = {{
          id: 42,
          filename: "Adarsh Baal Vidyalaya S01E05 Child.Safety.mkv",
          folder: "/shows/Adarsh Baal Vidyalaya 2026/Season 1",
        }};
        const backLabelNested = app.editorBackLabel;
        const edCrumbs = app.editorFolderBreadcrumbs;

        // Tapping Back navigates to parent folder in Folders view
        app.backToParentFolder();
        const backTargetHash = hashLog[hashLog.length - 1];
        const viewAfterBack = app.libraryView;

        // Tapping an ancestor segment ("shows") navigates directly to /shows
        app.openFolderInLibrary(edCrumbs[0].path);
        const showsTargetHash = hashLog[hashLog.length - 1];

        // Root file edge case
        app.editorFile = {{ id: 7, filename: "root.mkv", folder: "/" }};
        const backLabelRoot = app.editorBackLabel;
        app.backToParentFolder();
        const rootTargetHash = hashLog[hashLog.length - 1];

        // Null editorFile safety
        app.editorFile = null;
        const backLabelNull = app.editorBackLabel;
        const crumbsNull = app.editorFolderBreadcrumbs;

        console.log(JSON.stringify({{
          nestedCrumbs,
          rootCrumbs,
          nullCrumbs,
          parentNameNested,
          parentNameRoot,
          backLabelNested,
          backTargetHash,
          viewAfterBack,
          showsTargetHash,
          backLabelRoot,
          rootTargetHash,
          backLabelNull,
          crumbsNull,
        }}));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        assert out["nestedCrumbs"] == [
            {"name": "shows", "path": "/shows"},
            {"name": "Adarsh Baal Vidyalaya 2026", "path": "/shows/Adarsh Baal Vidyalaya 2026"},
            {"name": "Season 1", "path": "/shows/Adarsh Baal Vidyalaya 2026/Season 1"},
        ]
        assert out["rootCrumbs"] == [{"name": "Storage", "path": "/"}]
        assert out["nullCrumbs"] == [{"name": "Storage", "path": "/"}]
        assert out["parentNameNested"] == "Season 1"
        assert out["parentNameRoot"] == "Storage"
        assert out["backLabelNested"] == "Back to Season 1"
        assert out["viewAfterBack"] == "dir"
        assert (
            out["backTargetHash"]
            == "#/library?folder=%2Fshows%2FAdarsh+Baal+Vidyalaya+2026%2FSeason+1"
        )
        assert out["showsTargetHash"] == "#/library?folder=%2Fshows"
        assert out["backLabelRoot"] == "Back to Storage"
        assert out["rootTargetHash"] == "#/library"
        assert out["backLabelNull"] == "Back to Parent"
        assert out["crumbsNull"] == []
