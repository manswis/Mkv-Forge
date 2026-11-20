"""Exhaustive tests for Two-Phase Indexing (Instant Placeholder -> Background Probe -> Ready / Error)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import AppConstants
from app.services.scanner import ProbeError


class TestTwoPhaseIndexing:
    """Verify instant scanning placeholders, 409 locks, incremental re-probe, and error recovery."""

    async def test_instant_placeholder_and_409_guards_while_scanning(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        movies_dir = storage_dir / "movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        (storage_dir / "archive").mkdir(parents=True, exist_ok=True)
        new_file = movies_dir / "FreshUpload.mkv"
        new_file.write_bytes(b"simulated-mkv-bytes")
        st = new_file.stat()

        # Phase A: Insert instant 'scanning' placeholder
        file_id, inserted = api_app.state.media_repo.upsert_scanning_placeholder(
            filepath="movies/FreshUpload.mkv",
            folder="/movies",
            filename="FreshUpload.mkv",
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
        )
        assert inserted is True
        assert file_id > 0

        # Idempotent second call must not re-insert
        same_id, inserted_again = api_app.state.media_repo.upsert_scanning_placeholder(
            filepath="movies/FreshUpload.mkv",
            folder="/movies",
            filename="FreshUpload.mkv",
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
        )
        assert same_id == file_id
        assert inserted_again is False

        # GET /api/files shows the file immediately with scan_status == 'scanning'
        files_resp = await api_client.get("/api/files", params={"folder": "/movies"})
        assert files_resp.status_code == AppConstants.HTTP_OK
        items = files_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["filename"] == "FreshUpload.mkv"
        assert items[0]["scan_status"] == "scanning"

        # Scanning files must NOT pollute workflow filter counts (no_dolby / aac51_only / has_metadata)
        facets = files_resp.json()["facets"]
        assert facets["no_dolby"] == 0
        assert facets["aac51_only"] == 0
        assert facets["has_metadata"] == 0

        # Backend 409 Conflict guards while scan_status == 'scanning':
        # 1. POST /api/jobs (by file_id and by filepath)
        job_by_id = await api_client.post(
            "/api/jobs",
            json={
                "file_id": file_id,
                "config": {"output_mode": "keep_both", "tracks": [{"id": 0, "type": "video", "enabled": True}]},
            },
        )
        assert job_by_id.status_code == AppConstants.HTTP_CONFLICT

        job_by_path = await api_client.post(
            "/api/jobs",
            json={
                "filepath": "movies/FreshUpload.mkv",
                "config": {"output_mode": "keep_both", "tracks": [{"id": 0, "type": "video", "enabled": True}]},
            },
        )
        assert job_by_path.status_code == AppConstants.HTTP_CONFLICT

        # 2. Rename -> 409 Conflict
        rename_resp = await api_client.post(
            f"/api/files/{file_id}/rename", json={"new_name": "Renamed.mkv"}
        )
        assert rename_resp.status_code == AppConstants.HTTP_CONFLICT

        # 3. Duplicate -> 409 Conflict
        dup_resp = await api_client.post(f"/api/files/{file_id}/duplicate")
        assert dup_resp.status_code == AppConstants.HTTP_CONFLICT

        # 4. Move -> 409 Conflict
        move_resp = await api_client.post(
            "/api/files/move",
            json={"file_ids": [file_id], "destination_folder": "/archive"},
        )
        assert move_resp.status_code == AppConstants.HTTP_CONFLICT

        # 5. Delete -> 409 Conflict
        del_resp = await api_client.post(
            "/api/files/delete", json={"file_ids": [file_id]}
        )
        assert del_resp.status_code == AppConstants.HTTP_CONFLICT
        assert new_file.exists()

    async def test_incremental_scan_reprobes_scanning_rows_and_marks_error_on_failure(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        movies_dir = storage_dir / "movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        stuck_file = movies_dir / "Interrupted.mkv"
        stuck_file.write_bytes(b"interrupted-file-data")
        st = stuck_file.stat()

        # Simulate a row left in 'scanning' after a container restart with identical mtime & size
        file_id, _ = api_app.state.media_repo.upsert_scanning_placeholder(
            filepath="movies/Interrupted.mkv",
            folder="/movies",
            filename="Interrupted.mkv",
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
        )

        # Case 1: Probing fails -> incremental scan MUST NOT skip it, and MUST mark scan_status = 'error'
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(side_effect=ProbeError("corrupt mkv header")),
        ):
            api_app.state.scanner.start_scan(full=False)
            await api_app.state.scanner.wait_for_scan()

        row_after_err = api_app.state.media_repo.get_by_id(file_id)
        assert row_after_err is not None
        assert row_after_err["scan_status"] == "error"

        # Case 2: On 'error' row, per-row Re-scan succeeds -> transitions to 'ready'
        valid_probe = {
            "container": {"properties": {"duration": 60_000_000_000}},
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC",
                    "properties": {"pixel_dimensions": "1920x1080"},
                }
            ],
        }
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(return_value=(valid_probe, json.dumps(valid_probe))),
        ):
            rescan_resp = await api_client.post(f"/api/files/{file_id}/rescan")
        assert rescan_resp.status_code == AppConstants.HTTP_OK
        assert rescan_resp.json()["scan_status"] == "ready"
        assert rescan_resp.json()["resolution"] == "1080p"

        # Case 3: Deleting an 'error' row IS allowed (never permanently stuck)
        api_app.state.media_repo.mark_scan_error(
            filepath="movies/Interrupted.mkv",
            folder="/movies",
            filename="Interrupted.mkv",
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
        )
        del_err = await api_client.post(
            "/api/files/delete", json={"file_ids": [file_id]}
        )
        assert del_err.status_code == AppConstants.HTTP_OK
        assert del_err.json()["succeeded"] == 1
        assert not stuck_file.exists()

    async def test_watcher_emits_instant_added_then_ready_or_error(
        self, api_app
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        movies_dir = storage_dir / "movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        dropped = movies_dir / "Dropped.mkv"
        dropped.write_bytes(b"fresh-drop")

        watcher = api_app.state.library_watcher
        events: list[tuple[str, str]] = []
        orig_record = api_app.state.library_broker.record_change

        def _capture(*, action: str, filepath: str, file_id: int | None = None) -> None:
            events.append((action, filepath))
            orig_record(action=action, filepath=filepath, file_id=file_id)

        api_app.state.library_broker.record_change = _capture

        # Simulate watcher probing failure -> must emit 'added' (placeholder) then 'updated' (error)
        with (
            patch.object(watcher, "_wait_for_stability", new=AsyncMock(return_value=(10, float(dropped.stat().st_mtime)))),
            patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(side_effect=ProbeError("unreadable")),
            ),
        ):
            await watcher._process_settled_upsert("movies/Dropped.mkv")

        assert events == [("added", "movies/Dropped.mkv"), ("updated", "movies/Dropped.mkv")]
        row = api_app.state.media_repo.get_by_filepath("movies/Dropped.mkv")
        assert row is not None
        assert row["scan_status"] == "error"

    def test_frontend_scanning_toast_guard_and_error_retry_landmarks(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        html = (Path(__file__).resolve().parents[2] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        assert "Scanning…" in html
        assert "Scan Failed" in html
        assert "isContextMenuFileScanning" in html

        script = f"""
        const fs = require("fs");
        const vm = require("vm");
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
          fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        const scanningFile = {{ id: 10, filename: "New.mkv", filepath: "movies/New.mkv", scan_status: "scanning" }};
        const errorFile = {{ id: 11, filename: "Bad.mkv", filepath: "movies/Bad.mkv", scan_status: "error" }};
        const readyFile = {{ id: 12, filename: "Good.mkv", filepath: "movies/Good.mkv", scan_status: "ready" }};
        app.files = [scanningFile, errorFile, readyFile];

        app.openEditor(scanningFile);
        const hashAfterScanningClick = ctx.location.hash;
        const toastAfterScanningClick = app.toasts.length > 0 ? app.toasts[app.toasts.length - 1].message : "";

        app.toggleFileSelection(10);
        const selectedAfterScanningToggle = [...app.selectedFileIds];

        app.toggleSelectAllPage();
        const selectedAfterSelectAll = [...app.selectedFileIds];

        console.log(JSON.stringify({{
          hashAfterScanningClick,
          toastAfterScanningClick,
          selectedAfterScanningToggle,
          selectedAfterSelectAll,
        }}));
        process.exit(0);
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        assert out["hashAfterScanningClick"] == "#/library"
        assert "Scanning metadata" in out["toastAfterScanningClick"]
        assert out["selectedAfterScanningToggle"] == []
        assert out["selectedAfterSelectAll"] == [11, 12]
