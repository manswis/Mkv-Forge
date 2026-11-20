"""Integration and Node tests for the floating toast stack notifications."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants


class TestFloatingToastStack:
    async def test_toast_markup_is_fixed_overlay_and_legacy_banner_removed(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        js = await api_client.get("/static/js/toast_stack.js")
        assert js.status_code == AppConstants.HTTP_OK
        assert re.search(r"https?://", js.text) is None
        assert "/static/js/toast_stack.js" in html
        assert 'x-for="t in toasts"' in html
        assert "pointer-events-none fixed right-3 top-3" in html
        assert "md:right-6 md:top-5" in html
        assert "overflow-hidden rounded-lg border px-4 py-3" in html
        assert "toast !== null" not in html
        assert 'aria-label="Library actions menu"' in html
        assert 'toggleMobileSelectFromMenu()' in html
        assert ':placeholder="librarySearchPlaceholder"' in html
        css = (await api_client.get("/static/css/app.css")).text
        assert "toast-progress" in css

    def test_mobile_menu_and_library_placeholder_behavior(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/dashboard", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 390, innerHeight: 844,
          fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ items: [], folders: [], total: 0 }}) }}),
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        const defaultPlaceholder = app.librarySearchPlaceholder;
        app.totalFiles = 389;
        const countPlaceholder = app.librarySearchPlaceholder;

        app.scanMenuOpen = true;
        app.setLibraryView("list");
        const menuAfterView = app.scanMenuOpen;
        const hashAfterView = ctx.location.hash;
        const viewAfterSet = app.libraryView;
        app._applyHashState(ctx.location.hash, false);
        const routeAfterView = app.route;

        app.scanMenuOpen = true;
        app.toggleMobileSelectFromMenu();
        const selectAfterToggleOn = app.mobileSelectMode;
        const menuAfterSelect = app.scanMenuOpen;
        app.toggleMobileSelectFromMenu();
        const selectAfterToggleOff = app.mobileSelectMode;

        console.log(JSON.stringify({{
          defaultPlaceholder, countPlaceholder,
          menuAfterView, hashAfterView, routeAfterView, viewAfterSet,
          selectAfterToggleOn, menuAfterSelect, selectAfterToggleOff,
        }}));
        process.exit(0);
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)
        assert out["defaultPlaceholder"] == "Search files, movies, shows…"
        assert out["countPlaceholder"] == "Search 389 files…"
        assert out["menuAfterView"] is False
        assert "view=list" in out["hashAfterView"]
        assert out["routeAfterView"] == "library"
        assert out["viewAfterSet"] == "list"
        assert out["selectAfterToggleOn"] is True
        assert out["menuAfterSelect"] is False
        assert out["selectAfterToggleOff"] is False

    def test_toast_stack_runtime_behavior(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/settings", origin: "http://localhost" }},
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
        const TS = ctx.MKVForgeToastStack;
        const app = ctx.mkvForgeApp();

        const emptyMsg = TS.pushToast([], "   ", "success", 1).toast;
        const nullMsg = TS.pushToast([], null, "success", 1).toast;
        const unknownType = TS.pushToast([], "x", "weird", 1).toast.type;
        const errDur = TS.durationFor("error");
        const okDur = TS.durationFor("success");

        app.showToast("Settings saved.", "success");
        app.showToast("Settings saved.", "success");
        const afterDup = app.toasts.length;
        const dupId = app.toasts[0].id;
        app.showToast("Failed A", "error");
        app.showToast("Info B", "info");
        app.showToast("Info C", "info");
        app.showToast("Info D", "info");
        const capped = app.toasts.map((t) => t.message);
        const timerKeysCapped = Object.keys(app._toastTimers).length;
        app.dismissToast(app.toasts[0].id);
        app.dismissToast(99999);
        const afterDismiss = app.toasts.length;
        const timerKeysAfter = Object.keys(app._toastTimers).length;
        app.toasts.forEach((t) => app.dismissToast(t.id));

        console.log(JSON.stringify({{
          emptyMsg, nullMsg, unknownType, errDur, okDur,
          afterDup, dupId, capped, timerKeysCapped, afterDismiss, timerKeysAfter,
          finalLen: app.toasts.length, finalTimers: Object.keys(app._toastTimers).length,
        }}));
        process.exit(0);
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)
        assert out["emptyMsg"] is None and out["nullMsg"] is None
        assert out["unknownType"] == "info"
        assert out["errDur"] > out["okDur"]
        assert out["afterDup"] == 1
        assert out["capped"] == ["Failed A", "Info B", "Info C", "Info D"]
        assert out["timerKeysCapped"] == 4
        assert out["afterDismiss"] == 3
        assert out["timerKeysAfter"] == 3
        assert out["finalLen"] == 0 and out["finalTimers"] == 0
