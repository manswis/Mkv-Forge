"""Unit tests for throttled live scan hydration, guaranteed final sync, and safe Library guards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[2] / "app" / "static" / "js"


def _run_node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _harness_preamble() -> str:
    return f"""
    const fs = require("fs");
    const vm = require("vm");

    function createHarness({{ holdDashboard = false }} = {{}}) {{
      const apiCalls = [];
      let dashboardResolvers = [];
      let dashboardPayload = {{
        stats: {{ total_files: 1, aac51_only: 0, has_metadata: 0, needs_conversion: 0 }},
        analytics: {{ storage: {{}}, total_audio_tracks: 1, audio_codecs: [], resolutions: [], dynamic_ranges: [] }},
        recent_activity: [],
      }};
      const scheduledTimers = new Map();
      let nextTimerId = 1;
      let fakeNow = 10000;

      const ctx = {{
        URLSearchParams,
        AbortController,
        EventSource: class {{ addEventListener() {{}} close() {{}} }},
        Date: class extends Date {{
          static now() {{ return fakeNow; }}
        }},
        setTimeout: (fn, ms) => {{
          const id = nextTimerId++;
          scheduledTimers.set(id, {{ fn, ms }});
          return id;
        }},
        clearTimeout: (id) => {{
          scheduledTimers.delete(id);
        }},
        fetch: (url, opts) => {{
          const method = (opts && opts.method) || "GET";
          apiCalls.push([url, method]);
          if (url === "/api/dashboard") {{
            if (holdDashboard) {{
              return new Promise((resolve) => {{
                dashboardResolvers.push(() =>
                  resolve({{ ok: true, status: 200, json: async () => ({{ ...dashboardPayload }}) }})
                );
              }});
            }}
            return Promise.resolve({{
              ok: true,
              status: 200,
              json: async () => ({{ ...dashboardPayload }}),
            }});
          }}
          if (url.startsWith("/api/files")) {{
            return Promise.resolve({{
              ok: true,
              status: 200,
              json: async () => ({{
                items: [{{ id: 1, filepath: "Movies/A.mkv", filename: "A.mkv" }}],
                folders: ["Movies"],
                subfolders: [],
                other_files: [],
                folder_counts: {{}},
                audio_types: [],
                facets: {{ aac51_only: 0, has_metadata: 0, no_dolby: 0, needs_conversion: 0, audio_types: [] }},
                total: 1,
                total_pages: 1,
              }}),
            }});
          }}
          return Promise.resolve({{ ok: true, status: 200, json: async () => ({{}}) }});
        }},
        document: {{ addEventListener: () => {{}}, readyState: "complete", visibilityState: "visible" }},
        localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
        sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
        location: {{ hash: "#/" }},
        history: {{ replaceState: () => {{}} }},
        addEventListener: () => {{}},
        innerWidth: 1280,
        innerHeight: 800,
      }};
      ctx.window = ctx;
      ctx.globalThis = ctx;
      vm.createContext(ctx);
      for (const f of [
        "constants.js",
        "formatters.js",
        "api.js",
        "folder_tree.js",
        "editor.js",
        "pull_refresh.js",
        "toast_stack.js",
        "charts.js",
        "library_return.js",
        "playback_profile.js",
        "app.js",
      ]) {{
        vm.runInContext(
          fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"),
          ctx,
          {{ filename: f }}
        );
      }}
      const app = ctx.mkvForgeApp();
      return {{
        app,
        apiCalls,
        scheduledTimers,
        advanceTime(ms) {{ fakeNow += ms; }},
        setDashboardTotal(n) {{
          dashboardPayload = {{
            ...dashboardPayload,
            stats: {{ ...dashboardPayload.stats, total_files: n }},
          }};
        }},
        flushOneDashboard() {{
          const fn = dashboardResolvers.shift();
          if (fn) fn();
        }},
        async flushMicrotasks() {{
          for (let i = 0; i < 12; i += 1) {{
            await Promise.resolve();
          }}
        }},
        runTimers() {{
          const entries = Array.from(scheduledTimers.entries());
          scheduledTimers.clear();
          for (const [, t] of entries) t.fn();
        }},
      }};
    }}
    """


class TestScanLiveHydration:
    """Verify throttled live scan updates, guaranteed final sync, and safe Library guards."""

    def test_instant_first_paint_throttling_and_skipped_only_suppression(self) -> None:
        script = (
            _harness_preamble()
            + """
        async function run() {
          const h = createHarness();
          const { app, apiCalls } = h;
          app.route = "dashboard";

          // Skipped-only events should NOT trigger live dashboard refreshes
          app._handleScanEvent({ status: "running", scanned: 1, total: 100, skipped: 1 });
          app._handleScanEvent({ status: "running", scanned: 2, total: 100, skipped: 2 });
          await Promise.resolve();
          const callsAfterSkippedOnly = apiCalls.length;

          // First newly probed file triggers instant first paint
          h.setDashboardTotal(1);
          app._handleScanEvent({ status: "running", scanned: 3, total: 100, skipped: 2 });
          await h.flushMicrotasks();
          const callsAfterFirstProbed = apiCalls.filter((c) => c[0] === "/api/dashboard").length;

          // Rapid subsequent events within throttle window schedule a single trailing timer (no spam)
          h.advanceTime(200);
          app._handleScanEvent({ status: "running", scanned: 4, total: 100, skipped: 2 });
          h.advanceTime(200);
          app._handleScanEvent({ status: "running", scanned: 5, total: 100, skipped: 2 });
          const callsDuringThrottle = apiCalls.filter((c) => c[0] === "/api/dashboard").length;
          const pendingTimers = h.scheduledTimers.size;

          // Trailing timer fires once throttle window elapses
          h.advanceTime(1200);
          h.setDashboardTotal(3);
          h.runTimers();
          await h.flushMicrotasks();
          const callsAfterTrailing = apiCalls.filter((c) => c[0] === "/api/dashboard").length;

          // _disconnectStreams cancels any pending timer cleanly
          h.advanceTime(100);
          app._handleScanEvent({ status: "running", scanned: 6, total: 100, skipped: 2 });
          const timersBeforeDisconnect = h.scheduledTimers.size;
          app._disconnectStreams();
          const timersAfterDisconnect = h.scheduledTimers.size;

          console.log(JSON.stringify({
            callsAfterSkippedOnly,
            callsAfterFirstProbed,
            callsDuringThrottle,
            pendingTimers,
            callsAfterTrailing,
            timersBeforeDisconnect,
            timersAfterDisconnect,
          }));
        }
        run();
        """
        )
        out = _run_node_json(script)
        assert out["callsAfterSkippedOnly"] == 0
        assert out["callsAfterFirstProbed"] == 1
        assert out["callsDuringThrottle"] == 1
        assert out["pendingTimers"] == 1
        assert out["callsAfterTrailing"] == 2
        assert out["timersBeforeDisconnect"] == 1
        assert out["timersAfterDisconnect"] == 0

    def test_guaranteed_final_sync_when_completed_or_failed_arrives_mid_flight(self) -> None:
        script = (
            _harness_preamble()
            + """
        async function run() {
          const h = createHarness({ holdDashboard: true });
          const { app, apiCalls } = h;
          app.route = "dashboard";

          // 1. Trigger in-flight throttled refresh on first probed file
          h.setDashboardTotal(12);
          app._handleScanEvent({ status: "running", scanned: 12, total: 50, skipped: 0 });
          const inFlightDuringLive = app._scanLiveInFlight;
          const dashCallsWhileInFlight = apiCalls.filter((c) => c[0] === "/api/dashboard").length;

          // 2. Scan completes WHILE the live refresh is still in-flight!
          h.setDashboardTotal(50);
          app._handleScanEvent({ status: "completed", scanned: 50, total: 50, skipped: 0 });
          const pendingQueuedOnComplete = app._scanPendingFinalSync;
          const dashCallsBeforeResolve = apiCalls.filter((c) => c[0] === "/api/dashboard").length;

          // 3. Resolve the stale in-flight request -> guaranteed final sync fires immediately
          h.flushOneDashboard();
          await h.flushMicrotasks();
          const pendingClearedAfterDrain = app._scanPendingFinalSync === false;
          const dashCallsAfterDrain = apiCalls.filter((c) => c[0] === "/api/dashboard").length;

          // Resolve the final sync response and confirm 100% state landed
          h.flushOneDashboard();
          await h.flushMicrotasks();
          const finalTotalFiles = app.dashboard.stats.total_files;

          // 4. Repeat for FAILED terminal event arriving mid-flight
          h.setDashboardTotal(51);
          app._handleScanEvent({ status: "running", scanned: 1, total: 10, skipped: 0 });
          app._handleScanEvent({ status: "failed", scanned: 1, total: 10, skipped: 0, error_message: "disk io" });
          const pendingQueuedOnFail = app._scanPendingFinalSync;
          h.flushOneDashboard();
          await h.flushMicrotasks();
          const dashCallsAfterFailDrain = apiCalls.filter((c) => c[0] === "/api/dashboard").length;
          h.flushOneDashboard();
          await h.flushMicrotasks();

          console.log(JSON.stringify({
            inFlightDuringLive,
            dashCallsWhileInFlight,
            pendingQueuedOnComplete,
            dashCallsBeforeResolve,
            pendingClearedAfterDrain,
            dashCallsAfterDrain,
            finalTotalFiles,
            pendingQueuedOnFail,
            dashCallsAfterFailDrain,
          }));
        }
        run();
        """
        )
        out = _run_node_json(script)
        assert out["inFlightDuringLive"] is True
        assert out["dashCallsWhileInFlight"] == 1
        assert out["pendingQueuedOnComplete"] is True
        assert out["dashCallsBeforeResolve"] == 1
        assert out["pendingClearedAfterDrain"] is True
        assert out["dashCallsAfterDrain"] == 2
        assert out["finalTotalFiles"] == 50
        assert out["pendingQueuedOnFail"] is True
        assert out["dashCallsAfterFailDrain"] == 4

    def test_safe_library_auto_refresh_guards_and_silent_mode(self) -> None:
        script = (
            _harness_preamble()
            + """
        async function run() {
          const h = createHarness();
          const { app, apiCalls } = h;

          // Eligible: Library route, Page 1, no search, no overlays, no selection
          app.route = "library";
          app.page = 1;
          app.searchQuery = "";
          app.appliedSearchQuery = "";
          const allowedClean = app._canAutoRefreshLibraryDuringScan();

          // Guard 1: Page 2+ blocks auto-refresh
          app.page = 2;
          const blockedPage2 = !app._canAutoRefreshLibraryDuringScan();
          app.page = 1;

          // Guard 2: Active search query blocks auto-refresh
          app.searchQuery = "dune";
          const blockedSearch = !app._canAutoRefreshLibraryDuringScan();
          app.searchQuery = "";

          // Guard 3: Context menu blocks auto-refresh
          app.contextMenu = { type: "file", x: 10, y: 10 };
          const blockedContextMenu = !app._canAutoRefreshLibraryDuringScan();
          app.contextMenu = null;

          // Guard 4: Any open modal/drawer blocks auto-refresh
          app.deleteModal = { type: "file" };
          const blockedDeleteModal = !app._canAutoRefreshLibraryDuringScan();
          app.deleteModal = null;

          app.settingsModalOpen = true;
          const blockedSettings = !app._canAutoRefreshLibraryDuringScan();
          app.settingsModalOpen = false;

          app.jobsDrawerOpen = true;
          const blockedDrawer = !app._canAutoRefreshLibraryDuringScan();
          app.jobsDrawerOpen = false;

          // Guard 5: Active selection blocks auto-refresh
          app.selectedFileIds = [42];
          const blockedSelection = !app._canAutoRefreshLibraryDuringScan();
          app.selectedFileIds = [];

          // Guard 6: Editor route blocks auto-refresh
          app.route = "files";
          const blockedEditor = !app._canAutoRefreshLibraryDuringScan();

          // Silent library refresh during live scan on Page 1 never flips libraryLoading to true
          app.route = "library";
          app.libraryLoading = false;
          let loadingSeenTrue = false;
          const origListFiles = h.app.loadLibrary;
          app._handleScanEvent({ status: "running", scanned: 1, total: 20, skipped: 0 });
          if (app.libraryLoading) loadingSeenTrue = true;
          await Promise.resolve();
          await Promise.resolve();
          await Promise.resolve();
          const libraryCalls = apiCalls.filter((c) => c[0].startsWith("/api/files")).length;

          console.log(JSON.stringify({
            allowedClean,
            blockedPage2,
            blockedSearch,
            blockedContextMenu,
            blockedDeleteModal,
            blockedSettings,
            blockedDrawer,
            blockedSelection,
            blockedEditor,
            loadingSeenTrue,
            libraryCalls,
          }));
        }
        run();
        """
        )
        out = _run_node_json(script)
        assert out["allowedClean"] is True
        assert out["blockedPage2"] is True
        assert out["blockedSearch"] is True
        assert out["blockedContextMenu"] is True
        assert out["blockedDeleteModal"] is True
        assert out["blockedSettings"] is True
        assert out["blockedDrawer"] is True
        assert out["blockedSelection"] is True
        assert out["blockedEditor"] is True
        assert out["loadingSeenTrue"] is False
        assert out["libraryCalls"] == 1
