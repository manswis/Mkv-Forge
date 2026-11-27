"""Unit tests for the Single File Editor popup shell guardrails.

Validates all three architectural guardrails in a Node VM executing the real
frontend bundle (`constants.js`, `api.js`, `folder_tree.js`, `chapters.js`,
`editor.js`, `chapters_controller.js`, `library_return.js`, `nav_history.js`,
`app.js`):
1. Unsaved-changes protection on backdrop click, Escape key, and Close (✕) / Back.
2. Stacked scroll-lock safety when `z-50` child modals open and close over `z-40` Editor.
3. Cold deep-link hydration when landing directly on `#/files/{id}` in a fresh tab.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

ROOT_DIR = Path(__file__).resolve().parents[2]
JS_DIR = ROOT_DIR / "app" / "static" / "js"


def _run_node_json(script: str) -> dict:
    """Execute a Node.js verification script and parse its JSON stdout."""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node script failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(proc.stdout.strip())


class TestEditorPopupGuardrails:
    """Exhaustive positive, negative, and edge tests for Single File Editor popup UX."""

    def test_unsaved_changes_protection_backdrop_escape_and_confirm_flow(self) -> None:
        """Guardrail #1: Backdrop ignores dirty close; Escape/Close prompt confirm; child modals close first."""
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const jsDir = {json.dumps(str(JS_DIR))};

        let currentHash = "#/library";
        const keyListeners = [];
        const bodyStyle = {{ overflow: "" }};
        const storage = new Map();

        const sampleFile = {{
          id: 42,
          filename: "Dune.mkv",
          filepath: "Movies/Dune.mkv",
          folder: "/Movies",
          size_bytes: 5000000,
          duration_us: 120000000,
          container_title: "",
          has_global_tags: false,
          has_track_tags: false,
          attachments_count: 0,
          chapters_count: 0,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true, forced_track: false, track_name: "" }},
            {{ id: 1, type: "audio", codec: "E-AC-3", language: "eng", channels: 6, default_track: true, forced_track: false, track_name: "" }},
          ],
        }};

        const jsonResp = (data, status = 200) => ({{
          ok: status >= 200 && status < 300,
          status,
          json: async () => data,
          text: async () => JSON.stringify(data),
        }});

        const ctx = {{
          console,
          URLSearchParams,
          encodeURIComponent,
          decodeURIComponent,
          setTimeout,
          clearTimeout,
          AbortController,
          Blob,
          URL: {{ createObjectURL: () => "blob:1", revokeObjectURL: () => {{}} }},
          localStorage: {{
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => storage.set(k, String(v)),
            removeItem: (k) => storage.delete(k),
          }},
          sessionStorage: {{
            getItem: () => null,
            setItem: () => {{}},
            removeItem: () => {{}},
          }},
          document: {{
            body: {{ style: bodyStyle }},
            documentElement: {{ style: {{}} }},
            visibilityState: "visible",
            addEventListener: () => {{}},
          }},
          history: {{
            state: null,
            pushState: (_s, _t, u) => {{ if (u) currentHash = String(u); }},
            replaceState: (_s, _t, u) => {{ if (u) currentHash = String(u); }},
            back: () => {{}},
          }},
          window: null,
          globalThis: null,
          matchMedia: () => ({{ matches: false, addEventListener: () => {{}} }}),
          scrollTo: () => {{}},
          addEventListener: (ev, fn) => {{ if (ev === "keydown") keyListeners.push(fn); }},
          EventSource: function FakeES() {{ this.close = () => {{}}; }},
          fetch: async (url) => {{
            const u = String(url);
            if (u.includes("/api/auth/status")) return jsonResp({{ local_auth_enabled: false, authenticated: true }});
            if (u.includes("/api/settings/runtime")) return jsonResp({{}});
            if (u.includes("/api/settings")) return jsonResp({{ default_output_mode: "replace", default_remove_all_metadata: false }});
            if (u.includes("/api/dashboard")) return jsonResp({{ stats: {{}}, recent_jobs: [] }});
            if (u.includes("/api/jobs")) return jsonResp({{ active: null, queued: [], history: [] }});
            if (u.includes("/api/files/42/chapters")) return jsonResp({{ chapters: [], has_complex_chapters: false }});
            if (u.includes("/api/files/42")) return jsonResp(sampleFile);
            if (u.includes("/api/files")) {{
              return jsonResp({{
                items: [sampleFile],
                folders: ["/Movies"],
                subfolders: [],
                other_files: [],
                folder_counts: {{}},
                audio_types: [],
                facets: {{}},
                total: 1,
                total_pages: 1,
              }});
            }}
            return jsonResp({{}});
          }},
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        ctx.location = {{
          get hash() {{ return currentHash; }},
          set hash(v) {{ currentHash = String(v); }},
          pathname: "/",
          search: "",
        }};
        vm.createContext(ctx);

        const files = [
          "constants.js",
          "theme.js",
          "i18n.js",
          "formatters.js",
          "api.js",
          "folder_tree.js",
          "chapters.js",
          "editor.js",
          "chapters_controller.js",
          "pull_refresh.js",
          "toast_stack.js",
          "library_return.js",
          "playback_profile.js",
          "test_media.js",
          "batch.js",
          "nav_history.js",
          "app.js",
        ];
        for (const f of files) {{
          vm.runInContext(fs.readFileSync(jsDir + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const pressEscape = () => keyListeners.forEach((fn) => fn({{ key: "Escape" }}));

        (async () => {{
          const app = ctx.mkvForgeApp();
          app.$nextTick = (cb) => cb();
          app.init();
          await new Promise((r) => setTimeout(r, 20));

          // Open editor on clean file
          app.openEditor(sampleFile);
          app._applyHashState(currentHash, false);
          await app.loadEditor(42);

          // 1. Clean editor -> backdrop click closes immediately without confirm
          const cleanHasChanges = app.editorHasChanges;
          app.requestCloseEditor("backdrop");
          app._applyHashState(currentHash, false);
          const routeAfterCleanBackdrop = app.route;
          const discardAfterClean = app.editorDiscardConfirmOpen;

          // Re-open and make an unsaved edit (modify containerTitle)
          app.openEditor(sampleFile);
          app._applyHashState(currentHash, false);
          await app.loadEditor(42);
          app.editor.containerTitle = "Edited Title";
          const dirtyHasChanges = app.editorHasChanges;

          // 2. Dirty editor -> backdrop click MUST be ignored (no silent discard, no route change)
          app.requestCloseEditor("backdrop");
          const routeAfterDirtyBackdrop = app.route;
          const discardAfterDirtyBackdrop = app.editorDiscardConfirmOpen;
          const titleAfterDirtyBackdrop = app.editor.containerTitle;

          // 3. Dirty editor with child modal (Chapters) open -> Escape closes ONLY Chapters, NOT Editor!
          await app.openChaptersModal();
          pressEscape();
          const chaptersAfterEscape = app.chaptersModal;
          const routeAfterChildEscape = app.route;
          const discardAfterChildEscape = app.editorDiscardConfirmOpen;

          // 4. Dirty editor with NO child modal -> Escape opens Discard Confirm dialog
          pressEscape();
          const discardAfterEditorEscape = app.editorDiscardConfirmOpen;
          const routeWhileConfirmOpen = app.route;

          // 5. Pressing Escape again cancels discard ("Keep Editing") and preserves edits
          pressEscape();
          const discardAfterCancelEscape = app.editorDiscardConfirmOpen;
          const titleAfterCancel = app.editor.containerTitle;
          const routeAfterCancel = app.route;

          // 6. Close button (✕) opens Discard Confirm -> confirmDiscardEditorChanges closes Editor cleanly
          app.requestCloseEditor("button");
          const discardAfterCloseBtn = app.editorDiscardConfirmOpen;
          // Duplicate requestCloseEditor while confirm already open is a safe no-op
          app.requestCloseEditor("button");
          app.confirmDiscardEditorChanges();
          app._applyHashState(currentHash, false);
          const discardAfterConfirm = app.editorDiscardConfirmOpen;
          const routeAfterConfirm = app.route;

          // 7. Busy/submitting editor allows closing without blocking on unsaved-changes confirm
          app.openEditor(sampleFile);
          app._applyHashState(currentHash, false);
          await app.loadEditor(42);
          app.editor.containerTitle = "Busy Edit";
          app.editorSubmitting = true;
          app.requestCloseEditor("backdrop");
          app._applyHashState(currentHash, false);
          const routeAfterBusyClose = app.route;

          console.log(JSON.stringify({{
            cleanHasChanges,
            routeAfterCleanBackdrop,
            discardAfterClean,
            dirtyHasChanges,
            routeAfterDirtyBackdrop,
            discardAfterDirtyBackdrop,
            titleAfterDirtyBackdrop,
            chaptersAfterEscape,
            routeAfterChildEscape,
            discardAfterChildEscape,
            discardAfterEditorEscape,
            routeWhileConfirmOpen,
            discardAfterCancelEscape,
            titleAfterCancel,
            routeAfterCancel,
            discardAfterCloseBtn,
            discardAfterConfirm,
            routeAfterConfirm,
            routeAfterBusyClose,
          }}));
        }})();
        """
        out = _run_node_json(script)
        assert out["cleanHasChanges"] is False
        assert out["routeAfterCleanBackdrop"] == "library"
        assert out["discardAfterClean"] is False

        assert out["dirtyHasChanges"] is True
        assert out["routeAfterDirtyBackdrop"] == "files"
        assert out["discardAfterDirtyBackdrop"] is False
        assert out["titleAfterDirtyBackdrop"] == "Edited Title"

        assert out["chaptersAfterEscape"] is None
        assert out["routeAfterChildEscape"] == "files"
        assert out["discardAfterChildEscape"] is False

        assert out["discardAfterEditorEscape"] is True
        assert out["routeWhileConfirmOpen"] == "files"

        assert out["discardAfterCancelEscape"] is False
        assert out["titleAfterCancel"] == "Edited Title"
        assert out["routeAfterCancel"] == "files"

        assert out["discardAfterCloseBtn"] is True
        assert out["discardAfterConfirm"] is False
        assert out["routeAfterConfirm"] == "library"
        assert out["routeAfterBusyClose"] == "library"

    def test_stacked_scroll_lock_and_cold_deep_link_hydration(self) -> None:
        """Guardrails #2 & #3: Child modal close preserves Editor scroll-lock; cold #/files/{id} hydrates Library."""
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const jsDir = {json.dumps(str(JS_DIR))};

        let currentHash = "#/files/42";
        const bodyStyle = {{ overflow: "" }};
        const storage = new Map();
        const listCalls = [];

        const sampleFile = {{
          id: 42,
          filename: "Arrival.mkv",
          filepath: "Movies/SciFi/Arrival.mkv",
          folder: "/Movies/SciFi",
          size_bytes: 5000000,
          duration_us: 120000000,
          container_title: "",
          has_global_tags: false,
          has_track_tags: false,
          attachments_count: 0,
          chapters_count: 0,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", default_track: true, forced_track: false, track_name: "" }},
            {{ id: 1, type: "audio", codec: "AAC", language: "eng", channels: 6, default_track: true, forced_track: false, track_name: "" }},
          ],
        }};

        let failDetail = false;
        const jsonResp = (data, status = 200) => ({{
          ok: status >= 200 && status < 300,
          status,
          json: async () => data,
          text: async () => JSON.stringify(data),
        }});

        const ctx = {{
          console,
          URLSearchParams,
          encodeURIComponent,
          decodeURIComponent,
          setTimeout,
          clearTimeout,
          AbortController,
          Blob,
          URL: {{ createObjectURL: () => "blob:1", revokeObjectURL: () => {{}} }},
          localStorage: {{
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => storage.set(k, String(v)),
            removeItem: (k) => storage.delete(k),
          }},
          sessionStorage: {{
            getItem: () => null,
            setItem: () => {{}},
            removeItem: () => {{}},
          }},
          document: {{
            body: {{ style: bodyStyle }},
            documentElement: {{ style: {{}} }},
            visibilityState: "visible",
            addEventListener: () => {{}},
          }},
          history: {{
            state: null,
            pushState: (_s, _t, u) => {{ if (u) currentHash = String(u); }},
            replaceState: (_s, _t, u) => {{ if (u) currentHash = String(u); }},
            back: () => {{}},
          }},
          window: null,
          globalThis: null,
          matchMedia: () => ({{ matches: false, addEventListener: () => {{}} }}),
          scrollTo: () => {{}},
          addEventListener: () => {{}},
          fetch: async (url) => {{
            const u = String(url);
            if (u.includes("/api/files/42/chapters")) return jsonResp({{ chapters: [], has_complex_chapters: false }});
            if (u.includes("/api/files/42") || u.includes("/api/files/999")) {{
              if (failDetail) return jsonResp({{ detail: "Not found" }}, 404);
              return jsonResp(sampleFile);
            }}
            if (u.includes("/api/files")) {{
              listCalls.push(u);
              return jsonResp({{
                items: [sampleFile],
                folders: ["/Movies", "/Movies/SciFi"],
                subfolders: [],
                other_files: [],
                folder_counts: {{}},
                audio_types: [],
                facets: {{}},
                total: 1,
                total_pages: 1,
              }});
            }}
            return jsonResp({{}});
          }},
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        ctx.location = {{
          get hash() {{ return currentHash; }},
          set hash(v) {{ currentHash = String(v); }},
          pathname: "/",
          search: "",
        }};
        vm.createContext(ctx);

        const files = [
          "constants.js",
          "theme.js",
          "i18n.js",
          "formatters.js",
          "api.js",
          "folder_tree.js",
          "chapters.js",
          "editor.js",
          "chapters_controller.js",
          "pull_refresh.js",
          "toast_stack.js",
          "library_return.js",
          "playback_profile.js",
          "test_media.js",
          "batch.js",
          "nav_history.js",
          "app.js",
        ];
        for (const f of files) {{
          vm.runInContext(fs.readFileSync(jsDir + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        (async () => {{
          // === Cold Deep-Link Flow (#/files/42 in fresh tab) ===
          const app = ctx.mkvForgeApp();
          app.$nextTick = (cb) => cb();
          app._applyHashState("#/files/42", true);
          await new Promise((r) => setTimeout(r, 25));

          const coldShowLibraryViewport = app.showLibraryViewport;
          const coldIsEditorOpen = app.isEditorOpen;
          const coldScrollLocked = bodyStyle.overflow === "hidden" && app.hasActiveModalOverlay;
          const coldHydratedDir = app.currentDir;
          const coldHydratedCount = app.files.length;

          // === Stacked Scroll-Lock Safety (z-50 child modals over z-40 Editor) ===
          // 1. Open & close Chapters Sheet
          await app.openChaptersModal();
          const lockedWithChapters = app.hasActiveModalOverlay;
          app.closeChaptersModal();
          app._syncBodyScrollLock(app.hasActiveModalOverlay);
          const stillLockedAfterChaptersClose = app.hasActiveModalOverlay && bodyStyle.overflow === "hidden";

          // 2. Open & close Add Audio modal
          app.addAudioModalOpen = true;
          app.addAudioDraft = {{}};
          app._syncBodyScrollLock(app.hasActiveModalOverlay);
          app.closeAddAudioModal();
          app._syncBodyScrollLock(app.hasActiveModalOverlay);
          const stillLockedAfterAddAudioClose = app.hasActiveModalOverlay && bodyStyle.overflow === "hidden";

          // 3. Open & close Import Track modal
          app.importTrackModal = {{ kind: "subtitles" }};
          app._syncBodyScrollLock(app.hasActiveModalOverlay);
          app.closeImportTrackModal();
          app._syncBodyScrollLock(app.hasActiveModalOverlay);
          const stillLockedAfterImportClose = app.hasActiveModalOverlay && bodyStyle.overflow === "hidden";

          // 4. Close Single File Editor popup -> unlocks scroll AND leaves hydrated Library on /Movies/SciFi
          app.requestCloseEditor("button");
          app._applyHashState(currentHash, true);
          await new Promise((r) => setTimeout(r, 20));
          const routeAfterClose = app.route;
          const hashAfterClose = currentHash;
          const unlockedAfterEditorClose = !app.hasActiveModalOverlay && bodyStyle.overflow === "";
          const filesCountAfterClose = app.files.length;

          // === Negative Cold Deep-Link Flow (404 on #/files/999) ===
          failDetail = true;
          currentHash = "#/files/999";
          const appErr = ctx.mkvForgeApp();
          appErr.$nextTick = (cb) => cb();
          appErr._applyHashState("#/files/999", true);
          await new Promise((r) => setTimeout(r, 25));
          const errHydratedCount = appErr.files.length;
          appErr.requestCloseEditor("button");
          appErr._applyHashState(currentHash, true);
          await new Promise((r) => setTimeout(r, 20));
          const errRouteAfterClose = appErr.route;
          const errFilesAfterClose = appErr.files.length;
          const errUnlockedAfterClose = !appErr.hasActiveModalOverlay && bodyStyle.overflow === "";

          console.log(JSON.stringify({{
            coldShowLibraryViewport,
            coldIsEditorOpen,
            coldScrollLocked,
            coldHydratedDir,
            coldHydratedCount,
            lockedWithChapters,
            stillLockedAfterChaptersClose,
            stillLockedAfterAddAudioClose,
            stillLockedAfterImportClose,
            routeAfterClose,
            hashAfterClose,
            unlockedAfterEditorClose,
            filesCountAfterClose,
            errHydratedCount,
            errRouteAfterClose,
            errFilesAfterClose,
            errUnlockedAfterClose,
            listCallCount: listCalls.length,
          }}));
        }})();
        """
        out = _run_node_json(script)
        assert out["coldShowLibraryViewport"] is True
        assert out["coldIsEditorOpen"] is True
        assert out["coldScrollLocked"] is True
        assert out["coldHydratedDir"] == "/Movies/SciFi"
        assert out["coldHydratedCount"] == 1

        assert out["lockedWithChapters"] is True
        assert out["stillLockedAfterChaptersClose"] is True
        assert out["stillLockedAfterAddAudioClose"] is True
        assert out["stillLockedAfterImportClose"] is True

        assert out["routeAfterClose"] == "library"
        assert "SciFi" in out["hashAfterClose"]
        assert out["unlockedAfterEditorClose"] is True
        assert out["filesCountAfterClose"] == 1

        assert out["errHydratedCount"] == 1
        assert out["errRouteAfterClose"] == "library"
        assert out["errFilesAfterClose"] == 1
        assert out["errUnlockedAfterClose"] is True
        assert out["listCallCount"] >= 2
