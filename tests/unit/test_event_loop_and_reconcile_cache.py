"""Exhaustive unit & regression tests for event-loop offloading, folder reconcile cache, and frontend storm guards."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
)
from app.main import create_app
from app.services.folder_sync import (
    FolderReconcileCache,
    FolderReconcileResult,
    reconcile_folder_snapshot,
)
from app.services.job_worker import _AsyncJobProgressPersister
from app.services.library_events import LibraryEventBroker
from app.services.mkv_runner import finalize_remux_output

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_JS_DIR = REPO_ROOT / "app" / "static" / "js"


def _seed_media_row(media_repo: MediaFileRepository, rel_filepath: str) -> int:
    path = Path(rel_filepath)
    folder = "/" if str(path.parent) in (".", "") else f"/{path.parent.as_posix()}"
    return media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_filepath,
            folder=folder,
            filename=path.name,
            size_bytes=1024,
            mtime=1700000000.0,
            duration_us=60_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json="{}",
            scanned_at="2026-01-01T00:00:00Z",
        )
    )


class TestGuardrail1SafeAsyncProgressWrites:
    """Guardrail #1: Offloaded progress writes are thread-safe and never overwrite terminal states."""

    @pytest.mark.parametrize(
        "terminal_action",
        ["completed", "failed", "cancelled"],
    )
    def test_update_progress_never_overwrites_terminal_job_status(
        self, tmp_path: Path, terminal_action: str
    ) -> None:
        db = DatabaseManager(tmp_path / "jobs.db")
        db.initialize()
        job_repo = JobRepository(db)

        job_repo.create_job(
            job_id="job-term-1",
            filepath="Movies/Sample.mkv",
            config_json="{}",
        )
        claimed = job_repo.claim_next_queued_job()
        assert claimed is not None

        # Active running job accepts progress update
        assert job_repo.update_progress("job-term-1", stage="remux", progress=55) is True
        mid = job_repo.get_by_id("job-term-1")
        assert mid is not None
        assert mid["progress"] == 55

        # Transition to terminal state
        if terminal_action == "completed":
            job_repo.mark_completed("job-term-1", output_filepath="Movies/Sample.mkv")
            expected_status = AppConstants.JOB_STATUS_COMPLETED
            expected_progress = 100
        elif terminal_action == "failed":
            job_repo.mark_failed("job-term-1", error_message="Disk full")
            expected_status = AppConstants.JOB_STATUS_FAILED
            expected_progress = 55
        else:
            job_repo.mark_cancelled("job-term-1")
            expected_status = AppConstants.JOB_STATUS_CANCELLED
            expected_progress = 55

        # Delayed 99% progress tick MUST be rejected and leave terminal status/progress intact
        assert job_repo.update_progress("job-term-1", stage="remux", progress=99) is False
        after = job_repo.get_by_id("job-term-1")
        assert after is not None
        assert after["status"] == expected_status
        assert after["progress"] == expected_progress

    async def test_persister_offloads_coalesces_and_drains_before_terminal(
        self, tmp_path: Path
    ) -> None:
        db = DatabaseManager(tmp_path / "persister.db")
        db.initialize()
        job_repo = JobRepository(db)
        job_repo.create_job(
            job_id="job-async-1",
            filepath="Movies/Big.mkv",
            config_json="{}",
        )
        job_repo.claim_next_queued_job()

        writer_threads: list[int] = []
        main_thread_id = threading.get_ident()
        orig_update = job_repo.update_progress

        def _slow_update(job_id: str, *, stage: str | None, progress: int) -> bool:
            writer_threads.append(threading.get_ident())
            time.sleep(0.03)
            return orig_update(job_id, stage=stage, progress=progress)

        with patch.object(job_repo, "update_progress", side_effect=_slow_update):
            persister = _AsyncJobProgressPersister(
                job_repo,
                "job-async-1",
                throttle_sec=0.5,
                min_delta_pct=5,
            )
            # Rapid burst of 20 updates -> coalesced & offloaded
            assert persister.schedule("transcode", 0, force=True) is True
            for pct in range(1, 99):
                persister.schedule("transcode", pct)
            persister.schedule("remux", 99)

            # Event loop stays responsive while worker thread writes
            await asyncio.sleep(0.005)

            # Drain MUST wait for in-flight thread write before terminal transition
            await persister.close_and_drain()
            # Subsequent ticks after close MUST be ignored
            assert persister.schedule("remux", 99, force=True) is False

            job_repo.mark_completed("job-async-1", output_filepath="Movies/Big.mkv")

        assert len(writer_threads) >= 1
        assert all(tid != main_thread_id for tid in writer_threads)
        # Coalesced: far fewer DB writes than the 99 ticks fired
        assert len(writer_threads) <= 5
        final_row = job_repo.get_by_id("job-async-1")
        assert final_row is not None
        assert final_row["status"] == AppConstants.JOB_STATUS_COMPLETED
        assert final_row["progress"] == 100

    async def test_persister_handles_db_exception_safely(self, tmp_path: Path) -> None:
        db = DatabaseManager(tmp_path / "err.db")
        db.initialize()
        job_repo = JobRepository(db)

        with patch.object(job_repo, "update_progress", side_effect=RuntimeError("locked")):
            persister = _AsyncJobProgressPersister(job_repo, "job-err")
            assert persister.schedule("remux", 10, force=True) is True
            await persister.close_and_drain()
            # No unhandled exception escapes


class TestGuardrail2FolderReconcileCacheAndInvalidation:
    """Part 2 + Guardrail #2: Single-flight dedup, 2s TTL, race-free invalidation on mutations."""

    async def test_single_flight_coalesces_concurrent_requests_and_respects_ttl(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        (storage / "Movies").mkdir(parents=True)
        db = DatabaseManager(tmp_path / "cache.db")
        db.initialize()
        media_repo = MediaFileRepository(db)

        now_box = [100.0]
        cache = FolderReconcileCache(ttl_sec=2.0, clock=lambda: now_box[0])
        scan_calls = 0

        def _counting_reconcile(raw_folder: str, **kwargs) -> FolderReconcileResult:
            nonlocal scan_calls
            scan_calls += 1
            time.sleep(0.04)
            return FolderReconcileResult(
                folder_exists=True,
                subfolders=[],
                pruned_count=0,
                probe_candidates=[],
            )

        # 5 concurrent requests for the same folder share ONE disk scan
        results = await asyncio.gather(
            *[
                cache.get_or_reconcile(
                    "/Movies",
                    storage_dir=storage,
                    media_repo=media_repo,
                    reconcile_fn=_counting_reconcile,
                )
                for _ in range(5)
            ]
        )
        assert scan_calls == 1
        assert all(r.folder_exists for r in results)

        # Within 2.0s TTL -> cached hit, no new disk scan
        now_box[0] = 101.5
        cached_res = await cache.get_or_reconcile(
            "/Movies",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_counting_reconcile,
        )
        assert cached_res.folder_exists is True
        assert scan_calls == 1

        # After TTL expires -> fresh scan
        now_box[0] = 102.1
        await cache.get_or_reconcile(
            "/Movies",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_counting_reconcile,
        )
        assert scan_calls == 2

        # Immediate invalidation inside TTL -> forces immediate fresh scan
        cache.invalidate_folder("/Movies")
        now_box[0] = 102.2
        await cache.get_or_reconcile(
            "/Movies",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_counting_reconcile,
        )
        assert scan_calls == 3

    async def test_invalidation_during_in_flight_scan_prevents_stale_cache_poisoning(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        (storage / "Shows").mkdir(parents=True)
        db = DatabaseManager(tmp_path / "race.db")
        db.initialize()
        media_repo = MediaFileRepository(db)

        cache = FolderReconcileCache(ttl_sec=10.0)
        started_event = threading.Event()
        release_event = threading.Event()
        call_num = 0

        def _staged_reconcile(raw_folder: str, **kwargs) -> FolderReconcileResult:
            nonlocal call_num
            call_num += 1
            current = call_num
            if current == 1:
                started_event.set()
                release_event.wait(timeout=2.0)
            return FolderReconcileResult(
                folder_exists=True,
                subfolders=[],
                pruned_count=current,
                probe_candidates=[],
            )

        task1 = asyncio.create_task(
            cache.get_or_reconcile(
                "/Shows",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=_staged_reconcile,
            )
        )
        while not started_event.is_set():
            await asyncio.sleep(0.005)

        # Mutation happens WHILE scan #1 is still in flight
        cache.invalidate_for_filepath("Shows/Ep01.mkv")
        release_event.set()
        res1 = await task1
        assert res1.pruned_count == 1

        # Next call MUST NOT get poisoned scan #1 result; it must run scan #2
        res2 = await cache.get_or_reconcile(
            "/Shows",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_staged_reconcile,
        )
        assert res2.pruned_count == 2
        assert call_num == 2

    async def test_cache_handles_path_traversal_errors_and_eviction(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir(parents=True)
        db = DatabaseManager(tmp_path / "edge.db")
        db.initialize()
        media_repo = MediaFileRepository(db)

        cache = FolderReconcileCache(ttl_sec=5.0, max_entries=2)

        # Path traversal fails safely
        bad = await cache.get_or_reconcile(
            "/../../etc",
            storage_dir=storage,
            media_repo=media_repo,
        )
        assert bad.folder_exists is False

        # Exception cleans up in-flight future so subsequent call succeeds
        boom_once = {"raised": False}

        def _flaky_reconcile(raw_folder: str, **kwargs) -> FolderReconcileResult:
            if not boom_once["raised"]:
                boom_once["raised"] = True
                raise OSError("transient disk error")
            return FolderReconcileResult(
                folder_exists=True, subfolders=[], pruned_count=0, probe_candidates=[]
            )

        with pytest.raises(OSError, match="transient disk error"):
            await cache.get_or_reconcile(
                "/",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=_flaky_reconcile,
            )
        recovered = await cache.get_or_reconcile(
            "/",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_flaky_reconcile,
        )
        assert recovered.folder_exists is True

        # Max entries bound enforced without error
        for idx in range(4):
            await cache.get_or_reconcile(
                f"/Dir{idx}",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=lambda *_a, **_kw: FolderReconcileResult(
                    folder_exists=True, subfolders=[], pruned_count=0, probe_candidates=[]
                ),
            )
        assert len(cache._entries) <= 2

        # _folder_versions stays bounded even after many distinct folder invalidations
        for idx in range(10):
            cache.invalidate_folder(f"/Ephemeral{idx}")
        assert len(cache._folder_versions) <= 2

    async def test_single_flight_survives_owner_cancellation_without_killing_waiters(
        self, tmp_path: Path
    ) -> None:
        """Finding #1: Cancelling the initiating caller must NOT cancel or pop the shared task."""
        storage = tmp_path / "storage"
        (storage / "Movies").mkdir(parents=True)
        db = DatabaseManager(tmp_path / "cancel_sf.db")
        db.initialize()
        media_repo = MediaFileRepository(db)

        cache = FolderReconcileCache(ttl_sec=10.0)
        started_event = threading.Event()
        release_event = threading.Event()
        scan_calls = 0

        def _blocking_reconcile(raw_folder: str, **kwargs) -> FolderReconcileResult:
            nonlocal scan_calls
            scan_calls += 1
            started_event.set()
            release_event.wait(timeout=2.0)
            return FolderReconcileResult(
                folder_exists=True,
                subfolders=[],
                pruned_count=7,
                probe_candidates=[],
            )

        owner_req = asyncio.create_task(
            cache.get_or_reconcile(
                "/Movies",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=_blocking_reconcile,
            )
        )
        while not started_event.is_set():
            await asyncio.sleep(0.005)

        waiter_b = asyncio.create_task(
            cache.get_or_reconcile(
                "/Movies",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=_blocking_reconcile,
            )
        )
        await asyncio.sleep(0.01)

        # Cancel the OWNER request mid-flight
        owner_req.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_req

        # _in_flight MUST still hold the active detached task (not popped by cancelled owner!)
        assert len(cache._in_flight) == 1

        # Request C arriving after owner cancelled MUST still coalesce onto the SAME scan
        waiter_c = asyncio.create_task(
            cache.get_or_reconcile(
                "/Movies",
                storage_dir=storage,
                media_repo=media_repo,
                reconcile_fn=_blocking_reconcile,
            )
        )
        await asyncio.sleep(0.01)

        release_event.set()
        res_b, res_c = await asyncio.gather(waiter_b, waiter_c)
        assert scan_calls == 1
        assert res_b.pruned_count == 7
        assert res_c.pruned_count == 7
        assert len(cache._in_flight) == 0

        # Result was cached cleanly so a subsequent read still hits TTL
        res_d = await cache.get_or_reconcile(
            "/Movies",
            storage_dir=storage,
            media_repo=media_repo,
            reconcile_fn=_blocking_reconcile,
        )
        assert res_d.pruned_count == 7
        assert scan_calls == 1

    async def test_finalize_remux_output_offloads_swap_and_invalidates_folders(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        src_dir = storage / "Source"
        out_dir = storage / "Output"
        src_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        source_file = src_dir / "Movie.mkv"
        source_file.write_bytes(b"original-content")
        tmp_file = out_dir / "Movie.mkv.mkvforge.tmp"
        tmp_file.write_bytes(b"remuxed-content")

        cache = FolderReconcileCache(ttl_sec=60.0)
        db = DatabaseManager(tmp_path / "fin.db")
        db.initialize()
        media_repo = MediaFileRepository(db)

        # Warm cache for both /Source and /Output
        scans = {"/Source": 0, "/Output": 0}

        def _rec(folder: str, **kwargs) -> FolderReconcileResult:
            scans[folder] = scans.get(folder, 0) + 1
            return FolderReconcileResult(
                folder_exists=True, subfolders=[], pruned_count=0, probe_candidates=[]
            )

        await cache.get_or_reconcile("/Source", storage_dir=storage, media_repo=media_repo, reconcile_fn=_rec)
        await cache.get_or_reconcile("/Output", storage_dir=storage, media_repo=media_repo, reconcile_fn=_rec)
        assert scans == {"/Source": 1, "/Output": 1}

        final_path, final_rel, _deleted = await finalize_remux_output(
            source_file,
            tmp_file,
            output_mode=AppConstants.OUTPUT_MODE_SAVE_AS,
            storage_root=storage,
            output_path="Output/Movie_Converted.mkv",
            delete_original=False,
            folder_cache=cache,
        )
        assert final_path.is_file()
        assert final_rel == "Output/Movie_Converted.mkv"

        # Both source and output folders must be invalidated immediately
        await cache.get_or_reconcile("/Source", storage_dir=storage, media_repo=media_repo, reconcile_fn=_rec)
        await cache.get_or_reconcile("/Output", storage_dir=storage, media_repo=media_repo, reconcile_fn=_rec)
        assert scans == {"/Source": 2, "/Output": 2}

    async def test_api_mutations_immediately_invalidate_folder_reconcile_cache(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        movies = storage / "Movies"
        archive = storage / "Archive"
        movies.mkdir(parents=True)
        archive.mkdir(parents=True)
        clip = movies / "Clip.mkv"
        clip.write_bytes(b"fake-mkv-bytes")

        settings = Settings(
            storage_dir=storage,
            data_dir=tmp_path / "data",
            static_dir=REPO_ROOT / "app" / "static",
            health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
            scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
            log_level=AppConstants.DEFAULT_LOG_LEVEL,
            watch_mode=AppConstants.WATCH_MODE_OFF,
        )
        app = create_app(settings)
        app.state.db.initialize()
        app.state.library_broker.bind_loop(asyncio.get_running_loop())
        file_id = _seed_media_row(app.state.media_repo, "Movies/Clip.mkv")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Warm /Movies cache
            r1 = await client.get("/api/files", params={"folder": "/Movies"})
            assert r1.status_code == 200
            assert r1.json()["subfolders"] == []

            # 2. Create a subfolder inside /Movies -> MUST invalidate immediately (not wait 2s!)
            c_res = await client.post(
                "/api/folders", json={"parent": "/Movies", "name": "Extras"}
            )
            assert c_res.status_code == 201

            r2 = await client.get("/api/files", params={"folder": "/Movies"})
            assert r2.status_code == 200
            assert [s["name"] for s in r2.json()["subfolders"]] == ["Extras"]

            # 3. Rename file inside /Movies -> MUST invalidate immediately
            ren_res = await client.post(
                f"/api/files/{file_id}/rename", json={"new_name": "ClipRenamed.mkv"}
            )
            assert ren_res.status_code == 200

            r3 = await client.get("/api/files", params={"folder": "/Movies"})
            assert r3.status_code == 200
            assert [i["filename"] for i in r3.json()["items"]] == ["ClipRenamed.mkv"]

            # 4. Move file to /Archive -> MUST invalidate both /Movies and /Archive immediately
            await client.get("/api/files", params={"folder": "/Archive"})
            mv_res = await client.post(
                "/api/files/move",
                json={"file_ids": [file_id], "destination_folder": "/Archive"},
            )
            assert mv_res.status_code == 200

            r4_movies = await client.get("/api/files", params={"folder": "/Movies"})
            r4_archive = await client.get("/api/files", params={"folder": "/Archive"})
            assert r4_movies.json()["items"] == []
            assert [i["filename"] for i in r4_archive.json()["items"]] == ["ClipRenamed.mkv"]

            # 5. Delete file -> MUST invalidate immediately
            del_res = await client.post("/api/files/delete", json={"file_ids": [file_id]})
            assert del_res.status_code == 200
            r5_archive = await client.get("/api/files", params={"folder": "/Archive"})
            assert r5_archive.json()["items"] == []

        app.state.library_broker.close()


class TestPart3AndGuardrail3FrontendResilience:
    """Part 3 + Guardrail #3: Request coalescing, SSE log batching, and scoped stale fallback."""

    def test_frontend_coalescing_log_batching_and_scoped_stale_fallback(self) -> None:
        script = f"""
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const jsDir = {json.dumps(str(STATIC_JS_DIR))};
const storageMap = new Map();

const sandbox = {{
  console,
  setTimeout,
  clearTimeout,
  queueMicrotask,
  URLSearchParams,
  AbortController,
  localStorage: {{
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: (k) => storageMap.delete(k),
  }},
  sessionStorage: {{
    getItem: () => null,
    setItem: () => {{}},
    removeItem: () => {{}},
  }},
  location: {{ hash: "#/library" }},
  history: {{
    state: null,
    replaceState: (_s, _t, u) => {{ sandbox.location.hash = String(u || ""); }},
    pushState: (_s, _t, u) => {{ sandbox.location.hash = String(u || ""); }},
  }},
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  innerWidth: 1280,
  innerHeight: 800,
  document: {{
    hidden: false,
    documentElement: {{ style: {{}}, dataset: {{}}, classList: {{ add() {{}}, remove() {{}} }} }},
    addEventListener: () => {{}},
    querySelector: () => null,
  }},
  matchMedia: () => ({{ matches: false, addEventListener: () => {{}} }}),
}};
sandbox.window = sandbox;
vm.createContext(sandbox);

for (const file of [
  "constants.js",
  "formatters.js",
  "folder_tree.js",
  "editor.js",
  "toast_stack.js",
  "api.js",
]) {{
  const code = fs.readFileSync(path.join(jsDir, file), "utf8");
  vm.runInContext(code, sandbox, {{ filename: file }});
}}

let getJobsCalls = 0;
let getJobsBehavior = async () => {{
  await new Promise((r) => setTimeout(r, 25));
  return {{ active: null, queued: [], history: [{{ id: "j1", status: "completed" }}] }};
}};
let listFilesBehavior = null;
let listFilesCalls = 0;
let dashCalls = 0;

sandbox.MKVForgeApi = {{
  ...sandbox.MKVForgeApi,
  getJobs: async () => {{
    getJobsCalls += 1;
    return getJobsBehavior(getJobsCalls);
  }},
  listFiles: async (params) => {{
    listFilesCalls += 1;
    return listFilesBehavior(params);
  }},
  getDashboard: async () => {{
    dashCalls += 1;
    return {{ stats: {{ total_files: 2 }}, analytics: {{ storage: {{}} }}, recent_activity: [] }};
  }},
}};

vm.runInContext(
  fs.readFileSync(path.join(jsDir, "app.js"), "utf8"),
  sandbox,
  {{ filename: "app.js" }}
);

(async () => {{
  const C = sandbox.MKVForgeConstants;
  if (C.TIMINGS_MS.LIBRARY_CHANGED_DEBOUNCE !== 250) {{
    throw new Error("Expected LIBRARY_CHANGED_DEBOUNCE=250");
  }}
  if (C.TIMINGS_MS.LIVE_LOG_FLUSH !== 100) {{
    throw new Error("Expected LIVE_LOG_FLUSH=100");
  }}

  const ctrl = sandbox.mkvForgeApp();
  ctrl.$nextTick = (fn) => Promise.resolve().then(fn);
  ctrl.$refs = {{}};
  ctrl.route = C.ROUTES.LIBRARY;
  ctrl.libraryView = C.LIBRARY_VIEWS.DIR;

  await Promise.all([ctrl.loadJobs(), ctrl.loadJobs(), ctrl.loadJobs(), ctrl.loadJobs()]);
  if (getJobsCalls !== 1) {{
    throw new Error("Expected 4 concurrent loadJobs() to coalesce into 1 call, got " + getJobsCalls);
  }}
  // After completion, a subsequent loadJobs() triggers a fresh request
  await ctrl.loadJobs();
  if (getJobsCalls !== 2) {{
    throw new Error("Expected sequential loadJobs() to trigger 2nd call, got " + getJobsCalls);
  }}

  // 2. Pure log_line SSE frames do NOT clone liveJob and batch into liveLogs
  ctrl._handleJobEvent({{
    job_id: "job-99",
    filepath: "Movies/Test.mkv",
    status: C.STATUSES.RUNNING,
    stage: "remux",
    stage_label: "Remuxing MKV",
    percent: 42,
    speed: null,
    eta: null,
    error_message: null,
    warnings: [],
    recent_logs: ["init"],
  }});
  const liveJobRefBefore = ctrl.liveJob;
  for (let i = 0; i < 20; i++) {{
    ctrl._handleJobEvent({{
      job_id: "job-99",
      filepath: "Movies/Test.mkv",
      status: C.STATUSES.RUNNING,
      stage: "remux",
      stage_label: "Remuxing MKV",
      percent: 42,
      speed: null,
      eta: null,
      error_message: null,
      warnings: [],
      log_line: "line-" + i,
    }});
  }}
  if (ctrl.liveJob !== liveJobRefBefore) {{
    throw new Error("Pure log_line SSE frames must NOT clone liveJob reference");
  }}
  if (ctrl.liveLogs.length !== 1) {{
    throw new Error("Expected log lines buffered before 100ms flush, got " + ctrl.liveLogs.length);
  }}
  await new Promise((r) => setTimeout(r, 140));
  if (ctrl.liveLogs.length !== 21 || ctrl.liveLogs[20] !== "line-19") {{
    throw new Error("Expected batched flush of 21 log lines, got " + ctrl.liveLogs.length);
  }}

  // 3. Debounce burst of terminal job_progress + library_changed into ONE refresh
  listFilesBehavior = async () => ({{
    items: [{{ id: 1, filename: "A.mkv", folder: "/Movies", filepath: "Movies/A.mkv" }}],
    subfolders: [],
    other_files: [],
    total: 1,
    total_pages: 1,
    folder_exists: true,
  }});
  ctrl.currentDir = "/Movies";
  await ctrl.loadLibrary({{ clearStale: true }});
  dashCalls = 0;
  listFilesCalls = 0;

  // Terminal job event + 3 watcher library_changed events within 60ms
  ctrl._handleJobEvent({{
    job_id: "job-99",
    filepath: "Movies/Test.mkv",
    status: C.STATUSES.COMPLETED,
    stage: "remux",
    stage_label: "Completed",
    percent: 100,
    log_line: "done-line",
  }});
  // Terminal flushes pending log immediately
  if (ctrl.liveLogs[ctrl.liveLogs.length - 1] !== "done-line") {{
    throw new Error("Expected terminal status to flush final log line immediately");
  }}
  ctrl._handleLibraryChangedEvent({{ action: "updated", filepaths: ["Movies/Test.mkv"], file_ids: [1] }});
  await new Promise((r) => setTimeout(r, 40));
  ctrl._handleLibraryChangedEvent({{ action: "updated", filepaths: ["Movies/Test.mkv"], file_ids: [1] }});
  if (dashCalls !== 0 || listFilesCalls !== 0) {{
    throw new Error("Expected debounce delay before refreshing, got dash=" + dashCalls + " files=" + listFilesCalls);
  }}
  await new Promise((r) => setTimeout(r, 310));
  if (dashCalls !== 1 || listFilesCalls !== 1) {{
    throw new Error("Expected coalesced single refresh (1 dash, 1 files), got dash=" + dashCalls + " files=" + listFilesCalls);
  }}

  // 4. Guardrail #3: Same-folder error preserves files; different-folder error clears files!
  // 4a. Same-folder (/Movies) timeout -> keeps A.mkv visible + shows libraryError
  listFilesBehavior = async () => {{
    throw new Error("Request timed out");
  }};
  await ctrl.loadLibrary({{ preserveSelection: true }});
  if (ctrl.files.length !== 1 || ctrl.files[0].filename !== "A.mkv") {{
    throw new Error("Same-folder transient error must preserve existing files, got " + JSON.stringify(ctrl.files));
  }}
  if (!ctrl.libraryError || !ctrl.libraryError.includes("timed out")) {{
    throw new Error("Expected libraryError set on same-folder failure");
  }}

  // 4b. Navigate to DIFFERENT folder (/Shows) and fail -> MUST clear files (never leak /Movies under /Shows!)
  ctrl.currentDir = "/Shows";
  await ctrl.loadLibrary();
  if (ctrl.files.length !== 0) {{
    throw new Error("Different-folder navigation error must clear previous folder files, got " + JSON.stringify(ctrl.files));
  }}
  if (!ctrl.libraryError || !ctrl.libraryError.includes("timed out")) {{
    throw new Error("Expected libraryError set on different-folder failure");
  }}

  // 4c. Retry on /Shows succeeds -> shows /Shows files and clears libraryError
  listFilesBehavior = async () => ({{
    items: [{{ id: 2, filename: "Show01.mkv", folder: "/Shows", filepath: "Shows/Show01.mkv" }}],
    subfolders: [],
    other_files: [],
    total: 1,
    total_pages: 1,
    folder_exists: true,
  }});
  await ctrl.loadLibrary();
  if (ctrl.files.length !== 1 || ctrl.files[0].filename !== "Show01.mkv" || ctrl.libraryError !== null) {{
    throw new Error("Expected clean recovery on retry");
  }}

  // 5. Finding #2: Mid-flight loadJobs({{ force: true }}) triggers ONE trailing refresh
  //    and transient error on trailing refresh NEVER causes an infinite loop
  getJobsCalls = 0;
  let serverVersion = "v1-running";
  getJobsBehavior = async () => {{
    const snap = serverVersion;
    await new Promise((r) => setTimeout(r, 35));
    return {{
      active: snap === "v1-running" ? {{ id: "j-live", status: "running" }} : null,
      queued: [],
      history: snap === "v2-completed" ? [{{ id: "j-live", status: "completed" }}] : [],
    }};
  }};
  const pInitial = ctrl.loadJobs();
  await new Promise((r) => setTimeout(r, 10));
  serverVersion = "v2-completed";
  const pForced1 = ctrl.loadJobs({{ force: true }});
  const pForced2 = ctrl.loadJobs({{ force: true }});
  await Promise.all([pInitial, pForced1, pForced2]);
  if (getJobsCalls !== 2) {{
    throw new Error("Expected initial + 1 trailing refresh (2 calls), got " + getJobsCalls);
  }}
  if (ctrl.jobsState.active !== null || ctrl.jobsState.history.length !== 1) {{
    throw new Error("Expected fresh post-mutation jobsState after trailing refresh: " + JSON.stringify(ctrl.jobsState));
  }}

  // 5b. Transient error during trailing refresh MUST stop cleanly (no infinite retry loop)
  getJobsCalls = 0;
  getJobsBehavior = async (callNum) => {{
    await new Promise((r) => setTimeout(r, 20));
    if (callNum === 2) {{
      throw new Error("503 transient jobs error");
    }}
    return {{ active: {{ id: "j2", status: "running" }}, queued: [], history: [] }};
  }};
  const pErr1 = ctrl.loadJobs();
  await new Promise((r) => setTimeout(r, 5));
  const pErr2 = ctrl.loadJobs({{ force: true }});
  await Promise.all([pErr1, pErr2]);
  if (getJobsCalls !== 2) {{
    throw new Error("Expected exactly 2 calls (no infinite loop on error), got " + getJobsCalls);
  }}
  if (!ctrl.jobsError || !ctrl.jobsError.includes("503 transient")) {{
    throw new Error("Expected jobsError populated on failed trailing refresh");
  }}
  if (ctrl._loadJobsPromise !== null || ctrl._loadJobsPendingRefresh !== false) {{
    throw new Error("Expected loadJobs state fully reset after error");
  }}

  // 6. Finding #3: _disconnectStreams() cancels pending library_changed and log flush timers
  dashCalls = 0;
  listFilesCalls = 0;
  ctrl._handleLibraryChangedEvent({{ action: "updated", filepaths: ["Shows/Show01.mkv"], file_ids: [2] }});
  ctrl._enqueueLiveLogLine("buffered-before-disconnect");
  ctrl._disconnectStreams();
  await new Promise((r) => setTimeout(r, 320));
  if (dashCalls !== 0 || listFilesCalls !== 0) {{
    throw new Error("Expected _disconnectStreams() to cancel pending library_changed refresh");
  }}
  if (ctrl._pendingLogLines.length !== 0 || ctrl._liveLogFlushTimer !== null) {{
    throw new Error("Expected _disconnectStreams() to clear pending log buffer and timer");
  }}

  console.log("ALL_FRONTEND_ASSERTIONS_PASSED");
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}});
"""
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc.returncode == 0, (
            f"Node VM frontend test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        assert "ALL_FRONTEND_ASSERTIONS_PASSED" in proc.stdout
