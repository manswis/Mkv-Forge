"""Exhaustive unit & runtime tests for Library page + scroll position restoration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JS_DIR = PROJECT_ROOT / "app" / "static" / "js"


def _run_node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


class TestLibraryReturnModule:
    """100% positive, negative, and edge-case coverage for library_return.js."""

    def test_snapshot_normalization_storage_and_scroll_clamping(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: {{}}, globalThis: {{}}, URLSearchParams }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "library_return.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const LR = ctx.MKVForgeLibraryReturn;

        // 1. Snapshot creation (positive + malformed/negative edge cases)
        const valid = LR.createOriginSnapshot({{
          hash: "#/library?view=list&page=4",
          page: 4,
          view: "list",
          folder: "/Movies",
          scrollTop: 842.6,
          windowScrollY: 120.2,
          fileId: 99,
        }});
        const invalidNonLibrary = LR.createOriginSnapshot({{ hash: "#/dashboard", page: 4 }});
        const invalidNull = LR.createOriginSnapshot(null);
        const coercedEdge = LR.createOriginSnapshot({{
          hash: "  #/library?page=4  ",
          page: -10,
          view: "bogus",
          folder: "   ",
          scrollTop: -50,
          windowScrollY: NaN,
          fileId: "not-a-number",
        }});

        // 2. Hash canonical matching (param order independence + invalid inputs)
        const matchSame = LR.hashesMatch("#/library?view=list&page=4", "#/library?page=4&view=list");
        const matchDiffPage = LR.hashesMatch("#/library?view=list&page=4", "#/library?view=list&page=1");
        const matchNull = LR.hashesMatch(null, "#/library");

        // 3. SessionStorage persistence & corrupted/throwing storage resilience
        const backing = new Map();
        const goodStorage = {{
          getItem: (k) => (backing.has(k) ? backing.get(k) : null),
          setItem: (k, v) => backing.set(k, String(v)),
          removeItem: (k) => backing.delete(k),
        }};
        LR.saveOrigin(valid, goodStorage);
        const loaded = LR.loadOrigin(goodStorage);
        LR.clearOrigin(goodStorage);
        const afterClear = LR.loadOrigin(goodStorage);

        goodStorage.setItem(ctx.MKVForgeConstants.STORAGE_KEYS.LIBRARY_RETURN_STATE, "{{bad json");
        const loadedMalformed = LR.loadOrigin(goodStorage);

        const throwingStorage = {{
          getItem: () => {{ throw new Error("blocked"); }},
          setItem: () => {{ throw new Error("quota"); }},
          removeItem: () => {{ throw new Error("blocked"); }},
        }};
        const savedWithThrow = LR.saveOrigin(valid, throwingStorage);
        const loadedWithThrow = LR.loadOrigin(throwingStorage);
        LR.clearOrigin(throwingStorage);

        // 4. Scroll restoration: normal, clamped, unlaid-out retry, missing targets
        const readyContainer = {{ scrollHeight: 2000, clientHeight: 600, scrollTop: 0 }};
        const fakeWin = {{ scrolledTo: null, scrollTo(opts) {{ this.scrolledTo = opts; }} }};
        const resReady = LR.applyScrollPosition({{
          container: readyContainer,
          win: fakeWin,
          scrollTop: 843,
          windowScrollY: 120,
        }});

        const shortContainer = {{ scrollHeight: 800, clientHeight: 600, scrollTop: 0 }};
        const resClamped = LR.applyScrollPosition({{
          container: shortContainer,
          win: fakeWin,
          scrollTop: 500,
          windowScrollY: 0,
        }});

        const unmountedContainer = {{ scrollHeight: 0, clientHeight: 0, scrollTop: 0 }};
        const resRetry = LR.applyScrollPosition({{
          container: unmountedContainer,
          win: fakeWin,
          scrollTop: 500,
          windowScrollY: 0,
        }});

        const resEmpty = LR.applyScrollPosition({{}});

        console.log(JSON.stringify({{
          valid,
          invalidNonLibrary,
          invalidNull,
          coercedEdge,
          matchSame,
          matchDiffPage,
          matchNull,
          loaded,
          afterClear,
          loadedMalformed,
          savedWithThrow,
          loadedWithThrow,
          resReady,
          readyScrollTop: readyContainer.scrollTop,
          winScrolledTo: fakeWin.scrolledTo,
          resClamped,
          clampedScrollTop: shortContainer.scrollTop,
          resRetry,
          resEmpty,
        }}));
        """
        out = _run_node_json(script)

        assert out["valid"] == {
            "hash": "#/library?view=list&page=4",
            "page": 4,
            "view": "list",
            "folder": "/Movies",
            "scrollTop": 843,
            "windowScrollY": 120,
            "fileId": 99,
        }
        assert out["invalidNonLibrary"] is None
        assert out["invalidNull"] is None
        assert out["coercedEdge"] == {
            "hash": "#/library?page=4",
            "page": 1,
            "view": "dir",
            "folder": "/",
            "scrollTop": 0,
            "windowScrollY": 0,
            "fileId": None,
        }
        assert out["matchSame"] is True
        assert out["matchDiffPage"] is False
        assert out["matchNull"] is False
        assert out["loaded"] == out["valid"]
        assert out["afterClear"] is None
        assert out["loadedMalformed"] is None
        assert out["savedWithThrow"] == out["valid"]
        assert out["loadedWithThrow"] is None
        assert out["resReady"] == {
            "appliedContainer": True,
            "appliedWindow": True,
            "needsRetry": False,
        }
        assert out["readyScrollTop"] == 843
        assert out["resClamped"]["needsRetry"] is False
        assert out["clampedScrollTop"] == 200
        assert out["resRetry"]["needsRetry"] is True
        assert out["resEmpty"] == {
            "appliedContainer": False,
            "appliedWindow": False,
            "needsRetry": False,
        }


class TestAppControllerLibraryOriginRestoration:
    """Verify end-to-end page & scroll restoration on conversion, Back button, reload, and Browser Back."""

    def test_conversion_and_back_button_preserve_page_and_scroll_position(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");

        const sessionMap = new Map();
        const localMap = new Map();
        const container = {{
          scrollHeight: 2400,
          clientHeight: 700,
          scrollTop: 0,
          scrollTo(opts) {{
            this.scrollTop = typeof opts === "object" ? opts.top : opts;
          }},
        }};
        let currentHash = "#/library?view=list&no_dolby=1&page=4";
        let currentWindowScrollY = 0;

        const locationObj = {{
          get hash() {{ return currentHash; }},
          set hash(val) {{ currentHash = val; }},
        }};

        const ctx = {{
          URLSearchParams,
          setTimeout,
          clearTimeout,
          requestAnimationFrame: (cb) => cb(),
          document: {{
            documentElement: {{ scrollTop: 0 }},
            addEventListener: () => {{}},
            readyState: "complete",
          }},
          localStorage: {{
            getItem: (k) => (localMap.has(k) ? localMap.get(k) : null),
            setItem: (k, v) => localMap.set(k, String(v)),
            removeItem: (k) => localMap.delete(k),
          }},
          sessionStorage: {{
            getItem: (k) => (sessionMap.has(k) ? sessionMap.get(k) : null),
            setItem: (k, v) => sessionMap.set(k, String(v)),
            removeItem: (k) => sessionMap.delete(k),
          }},
          location: locationObj,
          history: {{
            replaceState: (_s, _t, url) => {{ currentHash = url; }},
          }},
          get scrollY() {{ return currentWindowScrollY; }},
          scrollTo(arg1, arg2) {{
            currentWindowScrollY = typeof arg1 === "object" ? arg1.top : (arg2 || 0);
          }},
          addEventListener: () => {{}},
          innerWidth: 390,
          innerHeight: 844,
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
          "app.js",
        ];
        for (const f of files) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        // Stub API so loadLibrary resolves synchronously via Promise
        let listCalls = [];
        ctx.MKVForgeApi.listFiles = async (params) => {{
          listCalls.push(params);
          return {{
            items: [{{ id: 42, filename: "Movie.mkv", folder: "/Movies", filepath: "Movies/Movie.mkv" }}],
            folders: ["/Movies"],
            subfolders: [],
            other_files: [],
            folder_counts: {{}},
            audio_types: [],
            facets: {{ aac51_only: 0, has_metadata: 0, no_dolby: 1, audio_types: [] }},
            total: 200,
            total_pages: 4,
          }};
        }};

        async function run() {{
          const app = ctx.mkvForgeApp();
          app.$refs = {{ libraryScrollContainer: container }};
          app.$nextTick = (cb) => cb();

          // Scenario A: User is on All Files, no_dolby=1, Page 4, scrolled to 920px
          app._applyHashState("#/library?view=list&no_dolby=1&page=4", true);
          await new Promise((r) => setTimeout(r, 10));
          container.scrollTop = 920;
          currentWindowScrollY = 40;

          // User taps media file -> opens editor
          const targetFile = {{ id: 42, filename: "Movie.mkv", folder: "/Movies", filepath: "Movies/Movie.mkv" }};
          app.openEditor(targetFile);
          app._applyHashState(currentHash, false);
          app.editorFile = targetFile;
          const labelFromListPage4 = app.editorBackLabel;

          // Simulate user scrolling down in Editor to tap Process File
          container.scrollTop = 0;
          currentWindowScrollY = 600;

          // Conversion completes queueing -> calls backToParentFolder()
          app.backToParentFolder();
          const hashAfterConvert = currentHash;
          app._applyHashState(currentHash, true);
          await new Promise((r) => setTimeout(r, 10));

          const pageAfterConvert = app.page;
          const viewAfterConvert = app.libraryView;
          const noDolbyAfterConvert = app.noDolby;
          const containerScrollAfterConvert = container.scrollTop;
          const windowScrollAfterConvert = currentWindowScrollY;

          // Scenario B: Folders view /Movies Page 4 -> tap file -> tap "< Back to Movies (Page 4)"
          app.openDirectory("/Movies");
          await new Promise((r) => setTimeout(r, 10));
          app.libraryView = "dir";
          app.currentDir = "/Movies";
          app.goToPage(4);
          await new Promise((r) => setTimeout(r, 10));
          container.scrollTop = 640;
          currentWindowScrollY = 0;

          app.openEditor(targetFile);
          app._applyHashState(currentHash, false);
          app.editorFile = targetFile;
          const labelFromFolderPage4 = app.editorBackLabel;

          // Simulate tab reload while in Editor (new app instance reads sessionStorage)
          const reloadedApp = ctx.mkvForgeApp();
          reloadedApp.$refs = {{ libraryScrollContainer: container }};
          reloadedApp.$nextTick = (cb) => cb();
          reloadedApp._applyHashState("#/files/42", false);
          reloadedApp.editorFile = targetFile;
          container.scrollTop = 0;

          reloadedApp.backToParentFolder();
          const hashAfterReloadBack = currentHash;
          reloadedApp._applyHashState(currentHash, true);
          await new Promise((r) => setTimeout(r, 10));

          const pageAfterReloadBack = reloadedApp.page;
          const dirAfterReloadBack = reloadedApp.currentDir;
          const scrollAfterReloadBack = container.scrollTop;

          // Scenario C: Browser Back button (hashchange from editor back to page 4)
          reloadedApp.openEditor(targetFile);
          reloadedApp._applyHashState(currentHash, false);
          container.scrollTop = 0;
          currentHash = hashAfterReloadBack;
          reloadedApp._applyHashState(currentHash, true);
          await new Promise((r) => setTimeout(r, 10));
          const scrollAfterBrowserBack = container.scrollTop;

          // Scenario D: Explicit goToPage clears saved origin
          reloadedApp.goToPage(2);
          const originAfterExplicitPageChange = reloadedApp._getLibraryReturnOrigin();

          console.log(JSON.stringify({{
            labelFromListPage4,
            hashAfterConvert,
            pageAfterConvert,
            viewAfterConvert,
            noDolbyAfterConvert,
            containerScrollAfterConvert,
            windowScrollAfterConvert,
            labelFromFolderPage4,
            hashAfterReloadBack,
            pageAfterReloadBack,
            dirAfterReloadBack,
            scrollAfterReloadBack,
            scrollAfterBrowserBack,
            originAfterExplicitPageChange,
          }}));
        }}

        run();
        """
        out = _run_node_json(script)

        assert out["labelFromListPage4"] == "Back to Library (Page 4)"
        assert out["hashAfterConvert"] == "#/library?view=list&no_dolby=1&page=4"
        assert out["pageAfterConvert"] == 4
        assert out["viewAfterConvert"] == "list"
        assert out["noDolbyAfterConvert"] is True
        assert out["containerScrollAfterConvert"] == 920
        assert out["windowScrollAfterConvert"] == 40

        assert out["labelFromFolderPage4"] == "Back to Movies (Page 4)"
        assert "folder=%2FMovies" in out["hashAfterReloadBack"]
        assert "page=4" in out["hashAfterReloadBack"]
        assert out["pageAfterReloadBack"] == 4
        assert out["dirAfterReloadBack"] == "/Movies"
        assert out["scrollAfterReloadBack"] == 640

        assert out["scrollAfterBrowserBack"] == 640
        assert out["originAfterExplicitPageChange"] is None
