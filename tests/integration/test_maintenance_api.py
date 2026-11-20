"""Integration tests for Phase 11 Settings UI, expanded /api/settings, and /api/maintenance/*."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants
from app.db.repositories import MediaFileUpsert


class TestPhase11SettingsAndMaintenanceApi:
    """Verify expanded user settings persistence, maintenance endpoints, and 409 safety locks."""

    async def test_expanded_settings_round_trip_and_validation(
        self, api_client: httpx.AsyncClient
    ) -> None:
        initial = await api_client.get("/api/settings")
        assert initial.status_code == AppConstants.HTTP_OK
        defaults = initial.json()
        assert defaults["show_video_details"] is True
        assert defaults["default_library_view"] == "dir"
        assert defaults["default_folder_layout"] == "list"
        assert defaults["page_limit"] == 50
        assert defaults["default_output_mode"] == "keep_both"
        assert defaults["default_dolby_codec"] == "eac3"
        assert defaults["default_dolby_bitrate"] == "640k"
        assert defaults["auto_cleanup_enabled"] is True
        assert defaults["auto_cleanup_interval_days"] == 7
        assert defaults["job_history_retention_days"] == 30
        assert defaults["confirm_cancel_job"] is True

        updated = await api_client.patch(
            "/api/settings",
            json={
                "page_limit": 100,
                "default_output_mode": "replace",
                "default_dolby_codec": "ac3",
                "default_dolby_bitrate": "448k",
                "default_strip_global_tags": True,
                "preferred_language": "hin",
                "auto_cleanup_interval_days": 30,
                "job_history_retention_days": 90,
                "confirm_cancel_job": False,
            },
        )
        assert updated.status_code == AppConstants.HTTP_OK
        body = updated.json()
        assert body["page_limit"] == 100
        assert body["default_output_mode"] == "replace"
        assert body["default_dolby_codec"] == "ac3"
        assert body["default_dolby_bitrate"] == "448k"
        assert body["default_strip_global_tags"] is True
        assert body["preferred_language"] == "hin"
        assert body["auto_cleanup_interval_days"] == 30
        assert body["job_history_retention_days"] == 90
        assert body["confirm_cancel_job"] is False

        # Invalid enum/literal values rejected with 422
        bad = await api_client.patch("/api/settings", json={"page_limit": 999})
        assert bad.status_code == 422
        bad_interval = await api_client.patch(
            "/api/settings", json={"auto_cleanup_interval_days": 5}
        )
        assert bad_interval.status_code == 422

    async def test_maintenance_endpoints_and_busy_or_unmounted_409_guards(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        status_resp = await api_client.get("/api/maintenance/status")
        assert status_resp.status_code == AppConstants.HTTP_OK
        status = status_resp.json()
        assert status["busy"] is False
        assert status["db_size_bytes"] > 0

        # Seed a real file on disk + a vanished ghost row in SQLite
        storage_dir: Path = api_app.state.settings.storage_dir
        (storage_dir / "movies").mkdir(parents=True, exist_ok=True)
        real_file = storage_dir / "movies" / "Keep.mkv"
        real_file.write_bytes(b"real")
        for rel in ("movies/Keep.mkv", "movies/Ghost.mkv"):
            api_app.state.media_repo.upsert(
                MediaFileUpsert(
                    filepath=rel,
                    folder="/movies",
                    filename=rel.split("/")[-1],
                    size_bytes=100,
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
                    raw_json="{}",
                    scanned_at="2026-11-19T20:00:00Z",
                )
            )

        run_all = await api_client.post("/api/maintenance/run-all")
        assert run_all.status_code == AppConstants.HTTP_OK
        all_body = run_all.json()
        assert all_body["prune_missing"]["removed_count"] == 1
        assert all_body["optimize_db"]["action"] == "optimize_db"

        # Active job lock -> 409 Conflict
        api_app.state.job_repo.create_job(
            job_id="active-lock-1", filepath="movies/Keep.mkv", config_json="{}"
        )
        api_app.state.job_repo.claim_next_queued_job()
        busy_resp = await api_client.post("/api/maintenance/optimize-db")
        assert busy_resp.status_code == AppConstants.HTTP_CONFLICT
        busy_status = (await api_client.get("/api/maintenance/status")).json()
        assert busy_status["busy"] is True
        api_app.state.job_repo.mark_completed(
            "active-lock-1", output_filepath="movies/Keep.mkv"
        )

        # USB unmount guard -> remove real_file so 0 indexed files exist on disk -> 409 Conflict!
        real_file.unlink()
        unmount_resp = await api_client.post("/api/maintenance/prune-missing")
        assert unmount_resp.status_code == AppConstants.HTTP_CONFLICT
        assert len(api_app.state.media_repo.list_all_filepaths()) == 1

    async def test_settings_ui_landmarks_and_editor_defaults_hydration(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "settingsModalOpen || route === C.ROUTES.SETTINGS" in html
        assert "p-2.5 sm:p-4 md:p-6 lg:p-8" in html
        assert "rounded-2xl border border-zinc-700/80 bg-canvas shadow-2xl" in html
        assert 'aria-label="Close settings"' in html
        assert 'aria-label="Refresh settings and diagnostics"' not in html
        assert "selectWebSettingsSection(sec.id)" in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.APPEARANCE)" in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.AUDIO_TRACKS)" in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.OUTPUT_METADATA)" in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.MAINTENANCE)" in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.AUTO_CLEANUP)" in html
        assert "runMaintenance('run-all')" in html
        assert "runMaintenance('optimize-db')" in html
        assert "runMaintenance('clean-temp')" in html
        assert "runMaintenance('prune-missing')" in html
        assert "runMaintenance('prune-jobs')" in html
        assert "Scheduled Auto-Cleanup (Idle Only)" in html
        assert "Confirm Before Cancelling Job" in html
        assert 'aria-label="Confirm Cancel Job"' in html
        assert html.count('aria-label="Open Settings"') == 2

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const cancelledJobIds = [];
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
          innerWidth: 1200, innerHeight: 800,
          fetch: async (url, opts) => {{
            const m = String(url).match(/\\/api\\/jobs\\/([^/?#]+)$/);
            if (m && opts && opts.method === "DELETE") {{
              cancelledJobIds.push(m[1]);
              return {{ ok: true, status: 200, json: async () => ({{ id: m[1], status: "cancelled" }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{}}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        (async () => {{
          const Ed = ctx.MKVForgeEditor;
          const app = ctx.mkvForgeApp();
          const file = {{
            id: 1, filename: "Sample.mkv", filepath: "movies/Sample.mkv", folder: "/movies",
            container_title: "", has_global_tags: false, has_metadata: false,
            tracks: [{{ id: 0, type: "video", codec: "HEVC", language: "und", track_name: "", default_track: true, forced_track: false }}],
          }};
          const state = Ed.createEditorState(file, {{
            default_output_mode: "replace",
            default_dolby_codec: "ac3",
            default_dolby_bitrate: "448k",
            default_strip_global_tags: true,
            preferred_language: "hin",
          }});

          // Settings popup & iPadOS / iOS accordion behavior
          const initialSection = app.activeSettingsSection;
          const initialMobileSection = app.mobileSettingsSection;
          app.openSettings();
          const openAfterTap = app.isSettingsOpen;
          app.toggleMobileSettingsSection("maintenance");
          const mobileAfterSwitch = app.mobileSettingsSection;
          app.toggleMobileSettingsSection("maintenance");
          const mobileAfterCollapse = app.mobileSettingsSection;
          app.selectWebSettingsSection("output_metadata");
          const webAfterSelect = app.activeSettingsSection;
          app.closeSettings();
          const openAfterCancel = app.isSettingsOpen;

          // Cancel Job confirmation popup & confirm_cancel_job setting
          app.jobs = [
            {{ id: "job-1", filepath: "movies/One.mkv", status: "running", progress: 42 }},
            {{ id: "job-2", filepath: "movies/Two.mkv", status: "queued", progress: 0 }},
          ];
          const defaultConfirmSetting = app.userSettings.confirm_cancel_job;
          await app.cancelJobById("job-1");
          const modalOpenedForJob1 = app.cancelJobModal ? app.cancelJobModal.jobId : null;
          const cancelledBeforeConfirm = cancelledJobIds.slice();
          app.closeCancelJobModal();
          const modalAfterDismiss = app.cancelJobModal;

          await app.cancelJobById("job-1");
          await app.confirmCancelJobModal();
          const cancelledAfterConfirm = cancelledJobIds.slice();
          const modalAfterConfirm = app.cancelJobModal;

          app.userSettings.confirm_cancel_job = false;
          await app.cancelJobById("job-2");
          const cancelledWhenDisabled = cancelledJobIds.slice();
          const modalWhenDisabled = app.cancelJobModal;

          console.log(JSON.stringify({{
            outputMode: state.outputMode,
            stripGlobalTags: state.stripGlobalTags,
            targetCodec: state.audioFix.targetCodec,
            bitrate: state.audioFix.bitrate,
            language: state.audioFix.language,
            initialSection,
            initialMobileSection,
            openAfterTap,
            mobileAfterSwitch,
            mobileAfterCollapse,
            webAfterSelect,
            openAfterCancel,
            defaultConfirmSetting,
            modalOpenedForJob1,
            cancelledBeforeConfirm,
            modalAfterDismiss,
            cancelledAfterConfirm,
            modalAfterConfirm,
            cancelledWhenDisabled,
            modalWhenDisabled,
          }}));
          process.exit(0);
        }})();
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        assert out == {
            "outputMode": "replace",
            "stripGlobalTags": True,
            "targetCodec": "ac3",
            "bitrate": "448k",
            "language": "hin",
            "initialSection": "appearance",
            "initialMobileSection": "appearance",
            "openAfterTap": True,
            "mobileAfterSwitch": "maintenance",
            "mobileAfterCollapse": None,
            "webAfterSelect": "output_metadata",
            "openAfterCancel": False,
            "defaultConfirmSetting": True,
            "modalOpenedForJob1": "job-1",
            "cancelledBeforeConfirm": [],
            "modalAfterDismiss": None,
            "cancelledAfterConfirm": ["job-1"],
            "modalAfterConfirm": None,
            "cancelledWhenDisabled": ["job-1", "job-2"],
            "modalWhenDisabled": None,
        }
