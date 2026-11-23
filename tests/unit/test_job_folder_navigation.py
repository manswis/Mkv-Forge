"""Unit & Node VM tests for clickable job history folder navigation and fallbacks."""

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


class TestJobFolderFormattingAndNavigation:
    """Verify folder resolution, root-level rendering, drawer close, and deleted-folder fallback."""

    def test_folder_of_and_resolve_job_folder_positive_and_edge_cases(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: null, globalThis: null }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Fmt = ctx.MKVForgeFormatters;

        console.log(JSON.stringify({{
          nestedRel: Fmt.folderOf("Shows/Season 1/Ep01.mkv"),
          nestedLeadingSlash: Fmt.folderOf("/Shows/Season 1/Ep01.mkv"),
          windowsSlashes: Fmt.folderOf("Shows\\\\Season 1\\\\Ep01.mkv"),
          rootBare: Fmt.folderOf("Movie.mkv"),
          rootSlash: Fmt.folderOf("/Movie.mkv"),
          rootDotSlash: Fmt.folderOf("./Movie.mkv"),
          emptyStr: Fmt.folderOf(""),
          nullPath: Fmt.folderOf(null),
          nonString: Fmt.folderOf(42),
          completedMoved: Fmt.resolveJobFolder({{
            status: "completed",
            filepath: "Inbox/Ep01.mkv",
            output_filepath: "Shows/S1/Ep01.mkv",
          }}),
          failedUsesSource: Fmt.resolveJobFolder({{
            status: "failed",
            filepath: "Shows/S1/Ep05.mkv",
            output_filepath: "Output/Ep05.mkv",
          }}),
          cancelledUsesSource: Fmt.resolveJobFolder({{
            status: "cancelled",
            filepath: "Movies/Film.mkv",
            output_filepath: null,
          }}),
          completedRootFile: Fmt.resolveJobFolder({{
            status: "completed",
            filepath: "Standalone.mkv",
            output_filepath: "Standalone.remux.mkv",
          }}),
          nullJob: Fmt.resolveJobFolder(null),
        }}));
        """
        out = _run_node(script)
        assert out["nestedRel"] == "/Shows/Season 1"
        assert out["nestedLeadingSlash"] == "/Shows/Season 1"
        assert out["windowsSlashes"] == "/Shows/Season 1"
        assert out["rootBare"] == "/"
        assert out["rootSlash"] == "/"
        assert out["rootDotSlash"] == "/"
        assert out["emptyStr"] == "/"
        assert out["nullPath"] == "/"
        assert out["nonString"] == "/"
        assert out["completedMoved"] == "/Shows/S1"
        assert out["failedUsesSource"] == "/Shows/S1"
        assert out["cancelledUsesSource"] == "/Movies"
        assert out["completedRootFile"] == "/"
        assert out["nullJob"] == "/"

    def test_open_job_folder_closes_drawer_and_falls_back_gracefully_when_folder_deleted(
        self,
    ) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");

        const entries = [{{ state: null, hash: "#/" }}];
        let idx = 0;
        const listeners = {{ popstate: [], hashchange: [] }};
        const toasts = [];
        const existingDirs = new Set(["/", "/Shows"]);

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
          innerWidth: 390,
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
              for (const fn of listeners.popstate) fn(ev);
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
          fetch: async (url) => {{
            const u = new URL(String(url), "http://localhost");
            if (u.pathname === "/api/files") {{
              const folder = u.searchParams.get("folder") || "/";
              const exists = existingDirs.has(folder);
              return {{
                ok: true,
                status: 200,
                json: async () => ({{
                  items: [],
                  folders: ["/Shows"],
                  subfolders: [],
                  other_files: [],
                  folder_counts: {{}},
                  audio_types: [],
                  facets: {{}},
                  total: 0,
                  total_pages: 0,
                  folder_exists: exists,
                }}),
              }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{}}) }};
          }},
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        ctx.URL = URL;
        vm.createContext(ctx);

        for (const f of [
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
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const app = ctx.mkvForgeApp();
        app.$nextTick = (cb) => cb();
        app.$refs = {{}};
        app.loadDashboard = async () => {{}};
        app.loadJobs = async () => {{}};
        app.showToast = (msg, kind) => toasts.push({{ msg, kind }});
        app.init();

        (async () => {{
          const flush = () => new Promise((r) => setTimeout(r, 25));

          // 1. Open from Live Jobs Drawer -> closes drawer, walks up from deleted /Shows/S1/Specials -> lands on /Shows with 1 toast
          app.openJobsDrawer();
          await flush();
          app.openJobFolder({{
            status: "failed",
            filepath: "Shows/S1/Specials/Ep05.mkv",
          }});
          await flush();

          const drawerClosed = !app.jobsDrawerOpen;
          const routeAfterMissing = app.route;
          const dirAfterMissing = app.currentDir;
          const toastsAfterMissing = [...toasts];

          // 2. Root-level file navigates cleanly to "/" with zero error toasts
          toasts.length = 0;
          app.openJobFolder({{
            status: "completed",
            filepath: "RootFilm.mkv",
            output_filepath: "RootFilm.remux.mkv",
          }});
          await flush();

          const dirAfterRoot = app.currentDir;
          const toastsAfterRoot = [...toasts];

          // 3. Completely missing top-level folder falls back to "/" with single toast
          toasts.length = 0;
          app.openJobFolder({{
            status: "failed",
            filepath: "DeletedRoot/Sub/Clip.mkv",
          }});
          await flush();

          const dirAfterFullDelete = app.currentDir;
          const toastsAfterFullDelete = [...toasts];

          // 4. Null job safely no-ops
          app.openJobFolder(null);

          console.log(JSON.stringify({{
            drawerClosed,
            routeAfterMissing,
            dirAfterMissing,
            toastsAfterMissing,
            dirAfterRoot,
            toastsAfterRoot,
            dirAfterFullDelete,
            toastsAfterFullDelete,
          }}));
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        out = _run_node(script)
        assert out["drawerClosed"] is True
        assert out["routeAfterMissing"] == "library"
        assert out["dirAfterMissing"] == "/Shows"
        assert len(out["toastsAfterMissing"]) == 1
        assert "/Shows/S1/Specials" in out["toastsAfterMissing"][0]["msg"]
        assert "/Shows" in out["toastsAfterMissing"][0]["msg"]
        assert out["dirAfterRoot"] == "/"
        assert out["toastsAfterRoot"] == []
        assert out["dirAfterFullDelete"] == "/"
        assert len(out["toastsAfterFullDelete"]) == 1
        assert "/DeletedRoot/Sub" in out["toastsAfterFullDelete"][0]["msg"]
        assert "opened /." in out["toastsAfterFullDelete"][0]["msg"]
