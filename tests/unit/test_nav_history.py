"""Unit and Node VM tests for mobile browser Back-gesture & folder navigation history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[2] / "app" / "static" / "js"


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestNavHistoryUnit:
    """Pure unit tests for MKVForgeNavHistory helpers and mobile overlay stack."""

    def test_mobile_viewport_and_ancestor_paths_positive_and_negative(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: null, globalThis: null }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "nav_history.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const NH = ctx.MKVForgeNavHistory;

        console.log(JSON.stringify({{
          mobile390: NH.isMobileViewport({{ innerWidth: 390 }}),
          mobile767: NH.isMobileViewport({{ innerWidth: 767 }}),
          desktop768: NH.isMobileViewport({{ innerWidth: 768 }}),
          desktop1280: NH.isMobileViewport({{ innerWidth: 1280 }}),
          invalidZero: NH.isMobileViewport({{ innerWidth: 0 }}),
          invalidNaN: NH.isMobileViewport({{ innerWidth: "abc" }}),
          invalidNull: NH.isMobileViewport(null),
          nestedPath: NH.getFolderAncestorPaths("/path a/sub path b/sub path c"),
          dirtySlashes: NH.getFolderAncestorPaths("///path a//sub path b///"),
          singleSegment: NH.getFolderAncestorPaths("/Movies"),
          rootPath: NH.getFolderAncestorPaths("/"),
          emptyPath: NH.getFolderAncestorPaths(""),
          nullPath: NH.getFolderAncestorPaths(null),
          nonStringPath: NH.getFolderAncestorPaths(12345),
        }}));
        """
        out = _run_node(script)
        assert out["mobile390"] is True
        assert out["mobile767"] is True
        assert out["desktop768"] is False
        assert out["desktop1280"] is False
        assert out["invalidZero"] is False
        assert out["invalidNaN"] is False
        assert out["invalidNull"] is False
        assert out["nestedPath"] == [
            "/",
            "/path a",
            "/path a/sub path b",
            "/path a/sub path b/sub path c",
        ]
        assert out["dirtySlashes"] == ["/", "/path a", "/path a/sub path b"]
        assert out["singleSegment"] == ["/", "/Movies"]
        assert out["rootPath"] == ["/"]
        assert out["emptyPath"] == ["/"]
        assert out["nullPath"] == ["/"]
        assert out["nonStringPath"] == ["/"]

    def test_overlay_stack_edge_cases_middle_removal_forward_and_missing_history(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: null, globalThis: null }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "nav_history.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const NH = ctx.MKVForgeNavHistory;

        function createMockWin(width, initialHash = "#/library") {{
          const entries = [{{ state: NH.createRouteState(), hash: initialHash }}];
          let idx = 0;
          const listeners = {{ popstate: [] }};
          const win = {{
            innerWidth: width,
            location: {{
              get hash() {{ return entries[idx].hash; }},
              set hash(v) {{
                entries.splice(idx + 1);
                entries.push({{ state: null, hash: String(v) }});
                idx = entries.length - 1;
              }},
            }},
            history: {{
              get state() {{ return entries[idx].state; }},
              pushState(state, _t, url) {{
                entries.splice(idx + 1);
                entries.push({{ state, hash: String(url) }});
                idx = entries.length - 1;
              }},
              replaceState(state, _t, url) {{
                entries[idx] = {{ state, hash: String(url) }};
              }},
              go(delta) {{
                const target = idx + delta;
                if (target < 0 || target >= entries.length) return;
                idx = target;
                const ev = {{ state: entries[idx].state }};
                queueMicrotask(() => {{
                  for (const fn of listeners.popstate) fn(ev);
                }});
              }},
              back() {{ this.go(-1); }},
              forward() {{ this.go(1); }},
            }},
            addEventListener(name, fn) {{
              if (!listeners[name]) listeners[name] = [];
              listeners[name].push(fn);
            }},
            _entries: entries,
            _getIndex: () => idx,
          }};
          return win;
        }}

        (async () => {{
          const flush = () => new Promise((r) => setTimeout(r, 5));

          // 1. Middle-layer removal: selection pushed, then jobs_drawer pushed, then selection cleared while drawer stays open
          const win = createMockWin(390, "#/library?view=dir&folder=%2FMovies");
          const mgr = NH.createMobileNavManager({{ win }});
          const popResults = [];
          win.addEventListener("popstate", (ev) => {{
            popResults.push(mgr.handlePopState(ev));
          }});

          mgr.reconcile(["selection"]);
          mgr.reconcile(["selection", "jobs_drawer"]);
          const depthWithBoth = mgr.getDepth();
          // Middle layer "selection" vanishes while "jobs_drawer" remains open
          mgr.reconcile(["jobs_drawer"]);
          const depthAfterMiddleRemoved = mgr.getDepth();
          // User swipes back -> closes jobs_drawer AND auto-skips stale selection entry!
          win.history.back();
          await flush();
          const depthAfterBack = mgr.getDepth();
          const indexAfterBack = win._getIndex();

          // 2. Forward button onto an old overlay sentinel is safely neutralized
          win.history.forward();
          await flush();
          const forwardResult = popResults[popResults.length - 1];
          const stateAfterForward = NH.readNavState(win.history.state);

          // 3. Missing history APIs (degraded environment) never throw
          const brokenWin = {{ innerWidth: 390, location: {{ hash: "#/" }}, history: {{}} }};
          const safeMgr = NH.createMobileNavManager({{ win: brokenWin }});
          safeMgr.reconcile(["selection", null, ""]);
          const nullPopRes = safeMgr.handlePopState(null);
          const navEmpty = safeMgr.navigateHash("");

          console.log(JSON.stringify({{
            depthWithBoth,
            depthAfterMiddleRemoved,
            depthAfterBack,
            indexAfterBack,
            firstDismissed: popResults[0] ? popResults[0].dismissedKeys : null,
            secondIgnored: popResults[1] ? popResults[1].ignoredProgrammatic : null,
            forwardHandled: forwardResult ? forwardResult.handled : null,
            stateKindAfterForward: stateAfterForward ? stateAfterForward.kind : null,
            nullPopHandled: nullPopRes.handled,
            navEmpty,
          }}));
        }})();
        """
        out = _run_node(script)
        assert out["depthWithBoth"] == 2
        assert out["depthAfterMiddleRemoved"] == 2  # kept until top unwinds
        assert out["firstDismissed"] == ["jobs_drawer"]
        assert out["secondIgnored"] is True  # auto-skipped stale selection entry
        assert out["depthAfterBack"] == 0
        assert out["indexAfterBack"] == 0  # back at base route entry!
        assert out["forwardHandled"] is True
        assert out["stateKindAfterForward"] == "route"
        assert out["nullPopHandled"] is False
        assert out["navEmpty"] is False


class TestAppMobileBackGestureFlow:
    """End-to-end Node VM tests of app.js + nav_history.js with simulated browser history."""

    def test_mobile_folder_hierarchy_jobs_drawer_selection_and_desktop_isolation(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");

        function buildHarness({{ width, initialHash = "#/" }}) {{
          const entries = [{{ state: null, hash: initialHash }}];
          let idx = 0;
          const listeners = {{ popstate: [], hashchange: [] }};
          let listCalls = 0;
          let dashboardCalls = 0;
          let jobsCalls = 0;

          const ctx = {{
            console,
            setTimeout,
            clearTimeout,
            queueMicrotask,
            URLSearchParams,
            Intl,
            Date,
            Math,
            JSON,
            Promise,
            innerWidth: width,
            innerHeight: 844,
            document: {{
              documentElement: {{ classList: {{ add() {{}}, remove() {{}} }}, setAttribute() {{}} }},
              querySelector: () => null,
              addEventListener: () => {{}},
              readyState: "complete",
              hidden: false,
            }},
            localStorage: {{
              _m: new Map(),
              getItem(k) {{ return this._m.has(k) ? this._m.get(k) : null; }},
              setItem(k, v) {{ this._m.set(k, String(v)); }},
              removeItem(k) {{ this._m.delete(k); }},
            }},
            sessionStorage: {{
              _m: new Map(),
              getItem(k) {{ return this._m.has(k) ? this._m.get(k) : null; }},
              setItem(k, v) {{ this._m.set(k, String(v)); }},
              removeItem(k) {{ this._m.delete(k); }},
            }},
            location: {{
              get hash() {{ return entries[idx].hash; }},
              set hash(v) {{
                const prev = entries[idx].hash;
                entries.splice(idx + 1);
                entries.push({{ state: null, hash: String(v) }});
                idx = entries.length - 1;
                if (prev !== entries[idx].hash) {{
                  for (const fn of listeners.hashchange) fn();
                }}
              }},
            }},
            history: {{
              get state() {{ return entries[idx].state; }},
              pushState(state, _t, url) {{
                entries.splice(idx + 1);
                entries.push({{ state, hash: String(url) }});
                idx = entries.length - 1;
              }},
              replaceState(state, _t, url) {{
                entries[idx] = {{ state, hash: String(url) }};
              }},
              go(delta) {{
                const target = idx + delta;
                if (target < 0 || target >= entries.length) return;
                const prevHash = entries[idx].hash;
                idx = target;
                const ev = {{ state: entries[idx].state }};
                for (const fn of listeners.popstate) fn(ev);
                if (prevHash !== entries[idx].hash) {{
                  for (const fn of listeners.hashchange) fn();
                }}
              }},
              back() {{ this.go(-1); }},
            }},
            addEventListener(name, fn) {{
              if (!listeners[name]) listeners[name] = [];
              listeners[name].push(fn);
            }},
            removeEventListener() {{}},
            scrollTo() {{}},
            scrollY: 0,
            EventSource: class {{ close() {{}} addEventListener() {{}} }},
          }};
          ctx.window = ctx;
          ctx.globalThis = ctx;
          vm.createContext(ctx);

          const files = [
            "constants.js",
            "formatters.js",
            "api.js",
            "folder_tree.js",
            "editor.js",
            "pull_refresh.js",
            "toast_stack.js",
            "library_return.js",
            "playback_profile.js",
            "batch.js",
            "nav_history.js",
            "app.js",
          ];
          for (const f of files) {{
            vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
          }}

          ctx.MKVForgeApi.getDashboard = async () => {{ dashboardCalls += 1; return {{ stats: {{}}, recent_jobs: [] }}; }};
          ctx.MKVForgeApi.listJobs = async () => {{ jobsCalls += 1; return {{ items: [] }}; }};
          ctx.MKVForgeApi.getScanStatus = async () => ({{ status: "idle" }});
          ctx.MKVForgeApi.getAuthStatus = async () => ({{ auth_enabled: false, authenticated: true }});
          ctx.MKVForgeApi.getSettings = async () => ({{}});
          ctx.MKVForgeApi.listFiles = async (params) => {{
            listCalls += 1;
            return {{
              items: [{{ id: 10, filename: "Ep01.mkv", folder: params.folder || "/", filepath: "Ep01.mkv" }}],
              folders: ["/path a", "/path a/sub path b", "/path a/sub path b/sub path c"],
              subfolders: [],
              other_files: [],
              folder_counts: {{}},
              audio_types: [],
              facets: {{}},
              total: 1,
              total_pages: 1,
            }};
          }};

          const app = ctx.mkvForgeApp();
          app.$nextTick = (cb) => cb();
          app.$refs = {{}};
          app.loadDashboard = async () => {{ dashboardCalls += 1; }};
          app.loadJobs = async () => {{ jobsCalls += 1; }};
          app.loadLibrary = async () => {{
            listCalls += 1;
            app.files = [{{ id: 10, filename: "Ep01.mkv", folder: app.currentDir || "/", filepath: "Ep01.mkv" }}];
          }};
          app.init();

          return {{
            app,
            ctx,
            back: () => ctx.history.back(),
            getHash: () => ctx.location.hash,
            getCounts: () => ({{ listCalls, dashboardCalls, jobsCalls }}),
            resetCounts: () => {{ listCalls = 0; dashboardCalls = 0; jobsCalls = 0; }},
          }};
        }}

        (async () => {{
          const flush = () => new Promise((r) => setTimeout(r, 5));

          // === SCENARIO 1: Mobile drill-down /path a -> /path a/sub path b -> /path a/sub path b/sub path c ===
          const m = buildHarness({{ width: 390, initialHash: "#/" }});
          m.app.navigate("library");
          await flush();
          m.app.openDirectory("/path a");
          await flush();
          m.app.openDirectory("/path a/sub path b");
          await flush();
          m.app.openDirectory("/path a/sub path b/sub path c");
          await flush();

          // Back #1: must go to /path a/sub path b (NOT Dashboard!) and only call listFiles ONCE (deduplicated)
          m.resetCounts();
          m.back();
          await flush();
          const dirAfterBack1 = m.app.currentDir;
          const routeAfterBack1 = m.app.route;
          const listCallsOnBack1 = m.getCounts().listCalls;

          // === SCENARIO 2: Open Job Queue Drawer inside /path a/sub path b -> Back closes drawer ===
          m.app.openJobsDrawer();
          await flush();
          const drawerOpenBeforeBack = m.app.jobsDrawerOpen;
          m.back();
          await flush();
          const drawerOpenAfterBack = m.app.jobsDrawerOpen;
          const dirAfterDrawerBack = m.app.currentDir;
          const routeAfterDrawerBack = m.app.route;

          // === SCENARIO 3: Select file inside /path a/sub path b -> Back unselects & stays in folder ===
          m.app.toggleMobileSelectMode();
          m.app.toggleFileSelection(10);
          await flush();
          const selectedBeforeBack = m.app.selectedFileIds.length;
          m.back();
          await flush();
          const selectedAfterBack = m.app.selectedFileIds.length;
          const selectModeAfterBack = m.app.mobileSelectMode;
          const dirAfterSelectBack = m.app.currentDir;

          // === SCENARIO 4: Next Back walks cleanly to /path a -> / -> Dashboard ===
          m.back();
          await flush();
          const dirAfterBack2 = m.app.currentDir;
          m.back();
          await flush();
          const dirAfterBack3 = m.app.currentDir;
          m.back();
          await flush();
          const routeAfterBack4 = m.app.route;

          // === SCENARIO 5: Direct deep load / refresh on /path a/sub path b/sub path c on mobile ===
          const deep = buildHarness({{
            width: 390,
            initialHash: "#/library?view=dir&folder=%2Fpath+a%2Fsub+path+b%2Fsub+path+c",
          }});
          await flush();
          deep.back();
          await flush();
          const deepDirAfterBack1 = deep.app.currentDir;
          deep.back();
          await flush();
          const deepDirAfterBack2 = deep.app.currentDir;
          deep.back();
          await flush();
          const deepDirAfterBack3 = deep.app.currentDir;

          // === SCENARIO 6: Nested layers (Selection -> Folder Picker) + UI button close cleans sentinel ===
          const nested = buildHarness({{ width: 390, initialHash: "#/" }});
          nested.app.navigate("library");
          nested.app.openDirectory("/path a");
          nested.app.openDirectory("/path a/sub path b");
          await flush();
          nested.app.toggleMobileSelectMode();
          nested.app.toggleFileSelection(10);
          await flush();
          nested.app.jobsDrawerOpen = true;
          nested.app._syncMobileNavLayers();
          await flush();
          // Close drawer via UI button (NOT back gesture) -> must cleanly pop sentinel so 1 Back clears selection!
          nested.app.closeJobsDrawer();
          await flush();
          nested.back();
          await flush();
          const nestedSelectAfterOneBack = nested.app.selectedFileIds.length;
          const nestedDirAfterOneBack = nested.app.currentDir;
          // Next Back goes straight to /path a (no phantom second back!)
          nested.back();
          await flush();
          const nestedDirAfterSecondBack = nested.app.currentDir;

          // === SCENARIO 7: Overlay -> Route Nav (scanMenu -> Jobs) leaves zero orphan; Back returns to folder ===
          const fromMenu = buildHarness({{ width: 390, initialHash: "#/" }});
          fromMenu.app.navigate("library");
          fromMenu.app.openDirectory("/path a");
          await flush();
          fromMenu.app.scanMenuOpen = true;
          fromMenu.app._syncMobileNavLayers();
          fromMenu.app.scanMenuOpen = false;
          fromMenu.app.navigate("jobs");
          await flush();
          const routeAtJobs = fromMenu.app.route;
          fromMenu.back();
          await flush();
          const routeAfterJobsBack = fromMenu.app.route;
          const dirAfterJobsBack = fromMenu.app.currentDir;

          // === SCENARIO 8: Desktop (1280px) does NOT push overlay sentinels, but folder Back still works ===
          const desk = buildHarness({{ width: 1280, initialHash: "#/" }});
          desk.app.navigate("library");
          desk.app.openDirectory("/path a");
          desk.app.openDirectory("/path a/sub path b");
          await flush();
          desk.app.openJobsDrawer();
          await flush();
          const deskOverlayDepth = desk.app._mobileNav.getDepth();
          desk.app.closeJobsDrawer();
          await flush();
          desk.back();
          await flush();
          const deskDirAfterBack = desk.app.currentDir;

          console.log(JSON.stringify({{
            dirAfterBack1,
            routeAfterBack1,
            listCallsOnBack1,
            drawerOpenBeforeBack,
            drawerOpenAfterBack,
            dirAfterDrawerBack,
            routeAfterDrawerBack,
            selectedBeforeBack,
            selectedAfterBack,
            selectModeAfterBack,
            dirAfterSelectBack,
            dirAfterBack2,
            dirAfterBack3,
            routeAfterBack4,
            deepDirAfterBack1,
            deepDirAfterBack2,
            deepDirAfterBack3,
            nestedSelectAfterOneBack,
            nestedDirAfterOneBack,
            nestedDirAfterSecondBack,
            routeAtJobs,
            routeAfterJobsBack,
            dirAfterJobsBack,
            deskOverlayDepth,
            deskDirAfterBack,
          }}));
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        out = _run_node(script)
        assert out["dirAfterBack1"] == "/path a/sub path b"
        assert out["routeAfterBack1"] == "library"
        assert out["listCallsOnBack1"] == 1  # popstate + hashchange deduplicated!
        assert out["drawerOpenBeforeBack"] is True
        assert out["drawerOpenAfterBack"] is False
        assert out["dirAfterDrawerBack"] == "/path a/sub path b"
        assert out["routeAfterDrawerBack"] == "library"
        assert out["selectedBeforeBack"] == 1
        assert out["selectedAfterBack"] == 0
        assert out["selectModeAfterBack"] is False
        assert out["dirAfterSelectBack"] == "/path a/sub path b"
        assert out["dirAfterBack2"] == "/path a"
        assert out["dirAfterBack3"] == "/"
        assert out["routeAfterBack4"] == "dashboard"
        assert out["deepDirAfterBack1"] == "/path a/sub path b"
        assert out["deepDirAfterBack2"] == "/path a"
        assert out["deepDirAfterBack3"] == "/"
        assert out["nestedSelectAfterOneBack"] == 0
        assert out["nestedDirAfterOneBack"] == "/path a/sub path b"
        assert out["nestedDirAfterSecondBack"] == "/path a"
        assert out["routeAtJobs"] == "jobs"
        assert out["routeAfterJobsBack"] == "library"
        assert out["dirAfterJobsBack"] == "/path a"
        assert out["deskOverlayDepth"] == 0
        assert out["deskDirAfterBack"] == "/path a"
