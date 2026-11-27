"""End-to-end integration & frontend VM tests for async Copy jobs and Bulk 3-dot bar."""

from __future__ import annotations

import asyncio
import errno
import json
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileUpsert, utc_now_iso
from app.main import create_app
from app.models.schemas import JobConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML_PATH = REPO_ROOT / "app" / "static" / "index.html"
APP_CSS_PATH = REPO_ROOT / "app" / "static" / "css" / "app.css"

_FakeUsage = namedtuple("_FakeUsage", ["total", "used", "free"])


def _seed_file(app, storage: Path, rel_path: str, data: bytes = b"mkv-bytes") -> int:
    full = storage / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    st = full.stat()
    p = Path(rel_path)
    folder = f"/{p.parent.as_posix()}" if p.parent.as_posix() != "." else "/"
    return app.state.media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_path,
            folder=folder,
            filename=p.name,
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
            duration_us=3_600_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json='[{"codec":"EAC3","channels":6,"language":"eng"}]',
            subtitle_count=1,
            container_title="Movie",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json='{"tracks": []}',
            scanned_at=utc_now_iso(),
        )
    )


async def _wait_for_job_terminal(
    client: httpx.AsyncClient, job_id: str, timeout_sec: float = 5.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        body = resp.json()
        for row in body.get("history", []):
            if row["id"] == job_id:
                return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"Job {job_id} did not reach terminal status in {timeout_sec}s")


@pytest.fixture()
async def copy_api_env(tmp_path: Path):
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    (storage / "Movies").mkdir(parents=True)
    (storage / "Backup").mkdir(parents=True)
    data.mkdir(parents=True)

    settings = Settings(
        storage_dir=storage,
        data_dir=data,
        static_dir=REPO_ROOT / "app" / "static",
        health_timeout_sec=2.0,
        scan_timeout_sec=5.0,
        log_level="WARNING",
        watch_mode="off",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield app, storage, client


class TestCopyFilesApiAndWorker:
    """Integration tests for POST /api/files/copy + JobWorker + SSE progress."""

    @pytest.mark.asyncio
    async def test_single_file_cross_folder_and_same_folder_copy_e2e(
        self, copy_api_env
    ) -> None:
        app, storage, client = copy_api_env
        payload = b"M" * (128 * 1024)
        fid = _seed_file(app, storage, "Movies/Inception.mkv", payload)

        # 1. Cross-folder copy returns 202 Accepted and runs via JobWorker
        resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid], "destination_folder": "/Backup"},
        )
        assert resp.status_code == AppConstants.HTTP_ACCEPTED
        data = resp.json()
        assert data["enqueued"] == 1
        assert data["failed"] == 0
        assert data["batch_id"] is None
        job_id = data["jobs"][0]["id"]
        assert data["jobs"][0]["output_mode"] == "copy"
        assert data["jobs"][0]["changes"][0]["label"] == "Copy → /Backup"

        finished = await _wait_for_job_terminal(client, job_id)
        assert finished["status"] == AppConstants.JOB_STATUS_COMPLETED
        assert finished["progress"] == 100
        assert finished["output_filepath"] == "Backup/Inception.mkv"
        assert finished["changes"][0]["label"] == "Copied → /Backup/Inception.mkv"
        assert (storage / "Backup" / "Inception.mkv").read_bytes() == payload
        # Source file untouched
        assert (storage / "Movies" / "Inception.mkv").read_bytes() == payload
        # New file immediately indexed in SQLite
        indexed = app.state.media_repo.get_by_filepath("Backup/Inception.mkv")
        assert indexed is not None
        assert indexed["video_codec"] == "HEVC"

        # 2. Same-folder copy auto-allocates Inception (1).mkv
        same_resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid], "destination_folder": "/Movies"},
        )
        assert same_resp.status_code == AppConstants.HTTP_ACCEPTED
        same_job_id = same_resp.json()["jobs"][0]["id"]
        same_finished = await _wait_for_job_terminal(client, same_job_id)
        assert same_finished["status"] == AppConstants.JOB_STATUS_COMPLETED
        assert same_finished["output_filepath"] == "Movies/Inception (1).mkv"
        assert (storage / "Movies" / "Inception (1).mkv").read_bytes() == payload

    @pytest.mark.asyncio
    async def test_bulk_multi_file_copy_with_partial_skip_and_batch_id(
        self, copy_api_env
    ) -> None:
        app, storage, client = copy_api_env
        fid1 = _seed_file(app, storage, "Movies/One.mkv", b"one-bytes")
        fid2 = _seed_file(app, storage, "Movies/Two.mkv", b"two-bytes")

        resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid1, fid2, 999999], "destination_folder": "/Backup"},
        )
        assert resp.status_code == AppConstants.HTTP_ACCEPTED
        body = resp.json()
        assert body["enqueued"] == 2
        assert body["failed"] == 1
        assert body["batch_id"] is not None
        assert body["batch_id"].startswith(AppConstants.BATCH_ID_PREFIX)

        for job in body["jobs"]:
            done = await _wait_for_job_terminal(client, job["id"])
            assert done["status"] == AppConstants.JOB_STATUS_COMPLETED

        assert (storage / "Backup" / "One.mkv").read_bytes() == b"one-bytes"
        assert (storage / "Backup" / "Two.mkv").read_bytes() == b"two-bytes"

    @pytest.mark.asyncio
    async def test_copy_error_guards_traversal_missing_ro_disk_space_scanning_and_lock(
        self, copy_api_env
    ) -> None:
        app, storage, client = copy_api_env
        fid = _seed_file(app, storage, "Movies/Guard.mkv", b"guard-data")

        # 1. Path traversal -> 400
        bad_path = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid], "destination_folder": "/../etc"},
        )
        assert bad_path.status_code == AppConstants.HTTP_BAD_REQUEST

        # 2. Missing destination folder -> 404
        missing_dir = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid], "destination_folder": "/NoSuchFolder"},
        )
        assert missing_dir.status_code == AppConstants.HTTP_NOT_FOUND

        # 3. Missing file ID -> 404
        missing_file = await client.post(
            "/api/files/copy",
            json={"file_ids": [999999], "destination_folder": "/Backup"},
        )
        assert missing_file.status_code == AppConstants.HTTP_NOT_FOUND

        # 4. Read-only volume (:ro) -> 403
        with patch(
            "app.api.file_ops.ensure_copy_destination_writable",
            side_effect=__import__(
                "app.services.file_ops_service", fromlist=["FileOpPermissionError"]
            ).FileOpPermissionError("Read-only"),
        ):
            ro_resp = await client.post(
                "/api/files/copy",
                json={"file_ids": [fid], "destination_folder": "/Backup"},
            )
        assert ro_resp.status_code == AppConstants.HTTP_FORBIDDEN

        # 5. Insufficient disk space -> 507
        with patch.object(
            shutil,
            "disk_usage",
            return_value=_FakeUsage(10_000, 9_999, 1),
        ):
            no_space = await client.post(
                "/api/files/copy",
                json={"file_ids": [fid], "destination_folder": "/Backup"},
            )
        assert no_space.status_code == AppConstants.HTTP_INSUFFICIENT_STORAGE

        # 6. Scanning placeholder -> 409
        scan_id, _ = app.state.media_repo.upsert_scanning_placeholder(
            filepath="Movies/Scanning.mkv",
            folder="/Movies",
            filename="Scanning.mkv",
            size_bytes=100,
            mtime=1.0,
        )
        (storage / "Movies" / "Scanning.mkv").write_bytes(b"s" * 100)
        scanning_resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [scan_id], "destination_folder": "/Backup"},
        )
        assert scanning_resp.status_code == AppConstants.HTTP_CONFLICT

        # 7. Active job lock -> 409
        app.state.job_repo.create_job(
            job_id="active-lock-1",
            filepath="Movies/Guard.mkv",
            config_json=JobConfig().model_dump_json(),
        )
        locked_resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [fid], "destination_folder": "/Backup"},
        )
        assert locked_resp.status_code == AppConstants.HTTP_CONFLICT

        # 8. Source missing on disk -> 404
        ghost_id = _seed_file(app, storage, "Movies/Vanished.mkv", b"bye")
        (storage / "Movies" / "Vanished.mkv").unlink()
        ghost_resp = await client.post(
            "/api/files/copy",
            json={"file_ids": [ghost_id], "destination_folder": "/Backup"},
        )
        assert ghost_resp.status_code == AppConstants.HTTP_NOT_FOUND


class TestBulkBarAndCopyFrontendContract:
    """Verify Bulk 3-dot menu HTML/CSS integrity and Copy Folder Picker JS flow."""

    def test_bulk_bar_web_and_mobile_layout_and_compiled_css_classes(self) -> None:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        css = APP_CSS_PATH.read_text(encoding="utf-8")

        bar_start = html.index("<!-- Multi-Select Bulk Action Bar")
        bar_end = html.index("<!-- Library Error -->", bar_start)
        bar_html = html[bar_start:bar_end]

        # Outside buttons: Batch Process…, Clear (mobile row 1 sm:hidden + web sm:inline), 3-dot menu
        assert "openBatchModalForSelection()" in bar_html
        assert "sm:hidden" in bar_html
        assert "sm:inline" in bar_html
        assert "sm:flex" not in bar_html  # Not compiled in app.css!
        assert "bulkActionsMenuOpen" in bar_html
        assert "justify-between" in bar_html

        # 3-dot menu contains Rename… (when single), Copy…, Move…, Delete…
        menu_start = bar_html.index('aria-label="More selection actions"')
        menu_html = bar_html[menu_start:]
        assert "openRenameFileModal(singleSelectedFile)" in menu_html
        assert "openCopyFilesModal(selectedFileIds)" in menu_html
        assert "openMoveFilesModal(selectedFileIds)" in menu_html
        assert "openDeleteSelectedFilesModal()" in menu_html
        assert menu_html.index("Copy…") < menu_html.index("Move…") < menu_html.index("Delete…")

        # Verify critical responsive classes actually exist in compiled app.css
        for css_token in (".sm\\:hidden", ".sm\\:inline", ".sm\\:w-auto", ".justify-between"):
            assert css_token in css, f"Missing compiled Tailwind rule {css_token} in app.css"

    def test_frontend_copy_flow_and_bulk_menu_lifecycle_in_node_vm(self) -> None:
        script = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const jsDir = path.join(process.cwd(), 'app/static/js');
const calls = { copy: [], jobs: 0 };

const ctx = {
  console,
  setTimeout,
  clearTimeout,
  Promise,
  URL,
  URLSearchParams,
  Date,
  Math,
  JSON,
  Array,
  Object,
  Number,
  String,
  Boolean,
  RegExp,
  Map,
  Set,
  Error,
  encodeURIComponent,
  decodeURIComponent,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  history: { state: null, pushState: () => {}, replaceState: () => {} },
  location: { hash: '#/library', pathname: '/', search: '', href: 'http://localhost/#/library' },
  document: {
    body: { style: { overflow: '' } },
    documentElement: { style: { setProperty: () => {} }, dataset: {}, classList: { toggle: () => {}, add: () => {}, remove: () => {} } },
    addEventListener: () => {},
    querySelector: () => null,
    visibilityState: 'visible',
  },
  navigator: { clipboard: { writeText: async () => {} } },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  fetch: async (url, opts = {}) => {
    if (String(url).startsWith('/api/folders/children')) {
      return { ok: true, status: 200, json: async () => ({ path: '/', children: [] }) };
    }
    if (String(url) === '/api/files/copy') {
      const body = JSON.parse(opts.body);
      calls.copy.push(body);
      if (body.destination_folder === '/ReadOnly') {
        return { ok: false, status: 403, json: async () => ({ detail: 'Storage volume is read-only' }) };
      }
      return {
        ok: true,
        status: 202,
        json: async () => ({
          enqueued: body.file_ids.length,
          failed: 0,
          batch_id: null,
          jobs: [{ id: 'job-copy-1', status: 'queued' }],
          results: body.file_ids.map((id) => ({ file_id: id, ok: true, status_code: 202 })),
        }),
      };
    }
    if (String(url).startsWith('/api/jobs')) {
      calls.jobs += 1;
      return { ok: true, status: 200, json: async () => ({ active: null, queued: [], history: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  },
};
ctx.window = ctx;
ctx.globalThis = ctx;

for (const file of [
  'constants.js',
  'formatters.js',
  'folder_tree.js',
  'chapters.js',
  'chapters_controller.js',
  'api.js',
  'app.js',
]) {
  vm.runInNewContext(fs.readFileSync(path.join(jsDir, file), 'utf8'), ctx, { filename: file });
}

(async () => {
  const app = ctx.mkvForgeApp();
  app.toasts = [];
  app.showToast = (msg, kind) => app.toasts.push({ msg, kind });
  app.loadJobs = async () => { calls.jobs += 1; };

  // 1. Context menu Copy… opens Folder Picker pre-selected to file's folder
  const file = { id: 42, filename: 'Movie.mkv', filepath: 'Movies/Movie.mkv', folder: '/Movies', scan_status: 'ready' };
  app.contextMenu = { kind: 'file', file, x: 0, y: 0 };
  app.runContextMenuAction('copy');
  await new Promise((r) => setTimeout(r, 15));

  if (!app.folderPickerOpen) throw new Error('Folder picker did not open');
  if (app.folderPickerPurpose !== 'copy_files') throw new Error('Wrong purpose: ' + app.folderPickerPurpose);
  if (app.folderTree.selectedPath !== '/Movies') throw new Error('Expected /Movies initialPath, got ' + app.folderTree.selectedPath);
  if (app.folderPickerConfirmLabel !== 'Copy Here') throw new Error('Wrong confirm label');

  // 2. Error response keeps picker open and surfaces inline error
  app.folderTree.selectedPath = '/ReadOnly';
  await app.confirmFolderPicker();
  if (!app.folderPickerOpen) throw new Error('Picker should remain open on error');
  if (!app.folderTree.error || !app.folderTree.error.includes('read-only')) {
    throw new Error('Expected read-only inline error, got ' + app.folderTree.error);
  }

  // 3. Successful copy closes picker, calls /api/files/copy, refreshes jobs, shows toast
  app.folderTree.selectedPath = '/Backup';
  await app.confirmFolderPicker();
  if (app.folderPickerOpen) throw new Error('Picker should close after enqueue');
  if (calls.copy.length !== 2 || calls.copy[1].destination_folder !== '/Backup') {
    throw new Error('Unexpected copy calls: ' + JSON.stringify(calls.copy));
  }
  if (calls.jobs < 1) throw new Error('Expected loadJobs to be triggered');
  if (!app.toasts.some((t) => t.kind === 'success' && t.msg.includes('/Backup'))) {
    throw new Error('Expected success toast: ' + JSON.stringify(app.toasts));
  }

  // 4. Bulk 3-dot menu closes on clearSelection and openCopyFilesModal
  app.selectedFileIds = [42, 43];
  app.bulkActionsMenuOpen = true;
  if (!app._hasBlockingOverlayOpen()) throw new Error('Expected blocking overlay when bulk menu open');
  app.clearSelection();
  if (app.bulkActionsMenuOpen) throw new Error('bulkActionsMenuOpen must close on clearSelection');

  app.selectedFileIds = [42, 43];
  app.bulkActionsMenuOpen = true;
  await app.openCopyFilesModal(app.selectedFileIds);
  if (app.bulkActionsMenuOpen) throw new Error('bulkActionsMenuOpen must close when opening Copy modal');
  if (app.folderPickerTitle !== 'Copy 2 Files To…') throw new Error('Unexpected bulk title: ' + app.folderPickerTitle);

  console.log('OK');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "OK" in proc.stdout
