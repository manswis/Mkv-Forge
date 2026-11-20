"""Integration and JS-contract tests for mobile Pull-to-Refresh."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants


class TestMobilePullToRefresh:
    """Verify offline serving, HTML landmarks, gesture math, and route-aware refresh behavior."""

    async def test_pull_refresh_script_served_offline_and_wired_in_html(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/static/js/pull_refresh.js")
        assert resp.status_code == AppConstants.HTTP_OK
        assert resp.headers.get("cache-control") == AppConstants.CACHE_CONTROL_NO_CACHE
        assert re.search(r"https?://", resp.text) is None

        html = (await api_client.get("/")).text
        assert "/static/js/pull_refresh.js" in html
        assert "pullRefresh.distancePx" in html
        assert "Release to refresh" in html
        assert "Pull to refresh" in html

    def test_pull_refresh_pure_math_guards_and_controller_lifecycle(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const fetchedUrls = [];
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{
            visibilityState: "visible",
            readyState: "loading",
            documentElement: {{ scrollTop: 0 }},
            addEventListener: () => {{}},
          }},
          addEventListener: () => {{}},
          innerWidth: 390,
          innerHeight: 844,
          scrollY: 0,
          fetch: async (url) => {{
            fetchedUrls.push(url);
            return {{
              ok: true, status: 200,
              json: async () => ({{
                items: [], subfolders: [], total: 0, total_pages: 0,
                folders: [], audio_types: [],
                facets: {{ aac51_only: 0, has_metadata: 0, no_dolby: 0, audio_types: [] }},
                folder_counts: {{}}, folder_exists: true,
                stats: {{ total_files: 1 }}, active: null, queued: [], history: [],
              }}),
            }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const C = ctx.MKVForgeConstants;
        const PR = ctx.MKVForgePullRefresh;

        // 1. Pure eligibility & math edge cases
        const allowMobileTop = PR.shouldStartPull({{ viewportWidth: 390, scrollY: 0, touchCount: 1, hasOpenOverlay: false, isBlockedTarget: false, isRefreshing: false }});
        const rejectDesktop = PR.shouldStartPull({{ viewportWidth: 1024, scrollY: 0, touchCount: 1, hasOpenOverlay: false, isBlockedTarget: false, isRefreshing: false }});
        const rejectScrolled = PR.shouldStartPull({{ viewportWidth: 390, scrollY: 12, touchCount: 1, hasOpenOverlay: false, isBlockedTarget: false, isRefreshing: false }});
        const rejectMultiTouch = PR.shouldStartPull({{ viewportWidth: 390, scrollY: 0, touchCount: 2, hasOpenOverlay: false, isBlockedTarget: false, isRefreshing: false }});
        const rejectOverlay = PR.shouldStartPull({{ viewportWidth: 390, scrollY: 0, touchCount: 1, hasOpenOverlay: true, isBlockedTarget: false, isRefreshing: false }});
        const blockInput = PR.isInteractiveOrScrollableTarget({{ tagName: "INPUT", parentElement: null }});
        const blockScrolledAncestor = PR.isInteractiveOrScrollableTarget({{ tagName: "DIV", scrollTop: 0, parentElement: {{ tagName: "DIV", scrollTop: 25, parentElement: null }} }});
        const allowPlainDiv = PR.isInteractiveOrScrollableTarget({{ tagName: "DIV", scrollTop: 0, parentElement: null }});
        const horizCancel = PR.shouldCancelForHorizontal(40, 15);
        const vertKeep = PR.shouldCancelForHorizontal(8, 35);

        const negMetrics = PR.computePullMetrics(-20);
        const nanMetrics = PR.computePullMetrics(NaN);
        const halfMetrics = PR.computePullMetrics(80);
        const readyMetrics = PR.computePullMetrics(180);
        const maxMetrics = PR.computePullMetrics(9999);

        // 2. Controller gesture simulation on Library
        const app = ctx.mkvForgeApp();
        app.route = C.ROUTES.LIBRARY;

        // Short pull (below threshold) must NOT trigger fetch
        app.onPullTouchStart({{ touches: [{{ clientX: 100, clientY: 100 }}], target: {{ tagName: "DIV" }} }});
        app.onPullTouchMove({{ touches: [{{ clientX: 100, clientY: 140 }}], cancelable: true, preventDefault: () => {{}} }});
        const distShort = app.pullRefresh.distancePx;
        app.onPullTouchEnd();
        const distAfterShortEnd = app.pullRefresh.distancePx;
        const fetchesAfterShort = fetchedUrls.length;

        // Full pull (past threshold) triggers Library + Dashboard + Jobs refresh
        let preventedDefault = false;
        app.onPullTouchStart({{ touches: [{{ clientX: 100, clientY: 100 }}], target: {{ tagName: "DIV" }} }});
        app.onPullTouchMove({{ touches: [{{ clientX: 102, clientY: 280 }}], cancelable: true, preventDefault: () => {{ preventedDefault = true; }} }});
        const readyDuringPull = app.pullRefresh.ready;
        app.onPullTouchEnd();

        setTimeout(async () => {{
          const fetchesAfterFull = [...fetchedUrls];
          const refreshingDone = app.pullRefresh.refreshing === false && app.pullRefresh.distancePx === 0;

          // 3. Editor unsaved-edits guard: pull-to-refresh syncs jobs ONLY (never wipes unsaved form!)
          fetchedUrls.length = 0;
          app.route = C.ROUTES.EDITOR;
          app.editorFileId = 42;
          app.editorFile = {{
            id: 42, filepath: "movies/A.mkv", filename: "A.mkv", folder: "/movies",
            container_title: "Orig", has_global_tags: false, tracks: [{{ id: 0, type: "video", codec: "HEVC", language: "und", track_name: "", default_track: true, forced_track: false }}],
          }};
          app.editor = ctx.MKVForgeEditor.createEditorState(app.editorFile);
          app.editor.containerTitle = "Unsaved User Edit";
          await app.performPullRefresh();
          const editorTitlePreserved = app.editor.containerTitle === "Unsaved User Edit";
          const editorFetchesWhenDirty = [...fetchedUrls];

          console.log(JSON.stringify({{
            allowMobileTop, rejectDesktop, rejectScrolled, rejectMultiTouch, rejectOverlay,
            blockInput, blockScrolledAncestor, allowPlainDiv, horizCancel, vertKeep,
            negMetrics, nanMetrics, halfMetrics, readyMetrics, maxMetrics,
            distShort, distAfterShortEnd, fetchesAfterShort,
            preventedDefault, readyDuringPull, fetchesAfterFull, refreshingDone,
            editorTitlePreserved, editorFetchesWhenDirty,
          }}));
        }}, 450);
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        assert out["allowMobileTop"] is True
        assert out["rejectDesktop"] is False
        assert out["rejectScrolled"] is False
        assert out["rejectMultiTouch"] is False
        assert out["rejectOverlay"] is False
        assert out["blockInput"] is True
        assert out["blockScrolledAncestor"] is True
        assert out["allowPlainDiv"] is False
        assert out["horizCancel"] is True
        assert out["vertKeep"] is False

        assert out["negMetrics"] == {"distancePx": 0, "progress": 0, "ready": False, "rotationDeg": 0}
        assert out["nanMetrics"] == {"distancePx": 0, "progress": 0, "ready": False, "rotationDeg": 0}
        assert out["halfMetrics"]["ready"] is False
        assert out["readyMetrics"]["ready"] is True
        assert out["maxMetrics"]["distancePx"] == 108

        assert out["distShort"] > 0
        assert out["distAfterShortEnd"] == 0
        assert out["fetchesAfterShort"] == 0

        assert out["preventedDefault"] is True
        assert out["readyDuringPull"] is True
        assert any(u.startswith("/api/files") for u in out["fetchesAfterFull"])
        assert "/api/dashboard" in out["fetchesAfterFull"]
        assert "/api/jobs" in out["fetchesAfterFull"]
        assert out["refreshingDone"] is True

        assert out["editorTitlePreserved"] is True
        assert out["editorFetchesWhenDirty"] == ["/api/jobs"]
