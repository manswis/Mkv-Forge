"""Exhaustive integration & Node VM tests for Single-File Download & Rearranged Context Menu."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.db.repositories import MediaFileUpsert

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_DIR = REPO_ROOT / "app" / "static" / "js"


def _seed_media_row(api_app, rel_path: str, size_bytes: int, *, scan_status: str = "ready") -> int:
    p = Path(rel_path)
    folder = f"/{p.parent.as_posix()}" if p.parent.as_posix() != "." else "/"
    if scan_status == AppConstants.FILE_SCAN_STATUS_SCANNING:
        fid, _inserted = api_app.state.media_repo.upsert_scanning_placeholder(
            filepath=rel_path,
            folder=folder,
            filename=p.name,
            size_bytes=size_bytes,
            mtime=1700000000.0,
        )
        return fid
    return api_app.state.media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_path,
            folder=folder,
            filename=p.name,
            size_bytes=size_bytes,
            mtime=1700000000.0,
            duration_us=1_000_000,
            resolution="1080p",
            video_codec="HEVC",
            video_dynamic_range="SDR",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json='{"tracks":[]}',
            scanned_at="2026-11-24T12:00:00Z",
        )
    )


class TestSingleFileDownloadEndpoints:
    """Verify HEAD/GET downloads, HTTP 206 Range resume, X-Accel-Buffering, RFC 5987, and all safety guards."""

    async def test_media_file_download_head_get_range_and_rfc5987_headers(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage: Path = api_app.state.settings.storage_dir
        movies = storage / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        unicode_name = "Café Movie (2025) [4K].mkv"
        payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        (movies / unicode_name).write_bytes(payload)
        file_id = _seed_media_row(api_app, f"movies/{unicode_name}", len(payload))

        # 1. HEAD preflight -> 200 OK, exact headers, zero body
        head_resp = await api_client.head(f"/api/files/{file_id}/download")
        assert head_resp.status_code == AppConstants.HTTP_OK
        assert head_resp.content == b""
        assert head_resp.headers["x-accel-buffering"] == "no"
        assert head_resp.headers["cache-control"] == "no-store"
        assert head_resp.headers["accept-ranges"] == "bytes"
        assert int(head_resp.headers["content-length"]) == len(payload)
        cd = head_resp.headers["content-disposition"]
        assert cd.startswith("attachment;")
        assert "filename*=" in cd  # RFC 5987 UTF-8 encoding for non-ASCII filename

        # 2. Full GET download -> 200 OK + exact payload
        get_resp = await api_client.get(f"/api/files/{file_id}/download")
        assert get_resp.status_code == AppConstants.HTTP_OK
        assert get_resp.content == payload
        assert get_resp.headers["x-accel-buffering"] == "no"

        # 3. HTTP Range request -> 206 Partial Content (browser pause/resume)
        range_resp = await api_client.get(
            f"/api/files/{file_id}/download",
            headers={"Range": "bytes=10-19"},
        )
        assert range_resp.status_code == 206
        assert range_resp.content == payload[10:20]
        assert range_resp.headers["content-range"] == f"bytes 10-19/{len(payload)}"
        assert range_resp.headers["x-accel-buffering"] == "no"

        # 4. Open-ended Range request -> 206 Partial Content through EOF
        open_range = await api_client.get(
            f"/api/files/{file_id}/download",
            headers={"Range": "bytes=10-"},
        )
        assert open_range.status_code == 206
        assert open_range.content == payload[10:]
        assert open_range.headers["content-range"] == f"bytes 10-{len(payload) - 1}/{len(payload)}"

        # 5. Unsatisfiable Range request -> 416 Range Not Satisfiable
        bad_range = await api_client.get(
            f"/api/files/{file_id}/download",
            headers={"Range": "bytes=9999-10000"},
        )
        assert bad_range.status_code == 416

    async def test_media_file_download_guards_active_job_scanning_and_missing(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage: Path = api_app.state.settings.storage_dir
        movies = storage / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        (movies / "Active.mkv").write_bytes(b"video-data")
        active_id = _seed_media_row(api_app, "movies/Active.mkv", 10)

        # Active job lock -> 409 Conflict on both HEAD and GET (including unnormalized /other/download paths!)
        api_app.state.job_repo.create_job(
            job_id="dl-lock-job", filepath="movies/Active.mkv", config_json="{}"
        )
        assert (await api_client.head(f"/api/files/{active_id}/download")).status_code == AppConstants.HTTP_CONFLICT
        assert (await api_client.get(f"/api/files/{active_id}/download")).status_code == AppConstants.HTTP_CONFLICT
        for unnormalized in ("movies/Active.mkv", "movies//Active.mkv"):
            assert (
                await api_client.head("/api/files/other/download", params={"filepath": unnormalized})
            ).status_code == AppConstants.HTTP_CONFLICT
            assert (
                await api_client.get("/api/files/other/download", params={"filepath": unnormalized})
            ).status_code == AppConstants.HTTP_CONFLICT
        assert (
            await api_client.head("/api/files/other/download", params={"filepath": "/movies/Active.mkv"})
        ).status_code == AppConstants.HTTP_BAD_REQUEST

        # Scanning placeholder -> 409 Conflict
        (movies / "Scanning.mkv").write_bytes(b"scanning-data")
        scan_id = _seed_media_row(api_app, "movies/Scanning.mkv", 13, scan_status="scanning")
        assert (await api_client.head(f"/api/files/{scan_id}/download")).status_code == AppConstants.HTTP_CONFLICT
        assert (await api_client.get(f"/api/files/{scan_id}/download")).status_code == AppConstants.HTTP_CONFLICT

        # Missing DB row -> 404
        assert (await api_client.head("/api/files/99999/download")).status_code == AppConstants.HTTP_NOT_FOUND
        assert (await api_client.get("/api/files/99999/download")).status_code == AppConstants.HTTP_NOT_FOUND

        # Indexed row whose file was deleted on disk -> 404
        ghost_id = _seed_media_row(api_app, "movies/Gone.mkv", 100)
        assert (await api_client.head(f"/api/files/{ghost_id}/download")).status_code == AppConstants.HTTP_NOT_FOUND
        assert (await api_client.get(f"/api/files/{ghost_id}/download")).status_code == AppConstants.HTTP_NOT_FOUND

    async def test_other_file_download_route_ordering_range_and_security_blocks(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage: Path = api_app.state.settings.storage_dir
        songs = storage / "downloads" / "songs"
        songs.mkdir(parents=True, exist_ok=True)
        song_name = "Maa- Pahla Pyar || Song.opus"
        song_bytes = b"OggS-audio-stream-bytes-1234567890"
        (songs / song_name).write_bytes(song_bytes)

        # Blocked files: temp, partial, hidden, AppleDouble, and directories
        (songs / "temp.mkv.mkvforge.tmp").write_bytes(b"tmp")
        (songs / "partial.mkv.part").write_bytes(b"part")
        (songs / "scratch.tmp").write_bytes(b"tmp2")
        (songs / ".DS_Store").write_bytes(b"junk")
        (songs / "._Hidden.opus").write_bytes(b"appledouble")

        # 1. Valid Other File HEAD + GET + Range (proves /other/download registered before /{file_id})
        rel = f"downloads/songs/{song_name}"
        head_r = await api_client.head("/api/files/other/download", params={"filepath": rel})
        assert head_r.status_code == AppConstants.HTTP_OK
        assert head_r.headers["x-accel-buffering"] == "no"
        assert head_r.headers["accept-ranges"] == "bytes"
        assert "attachment;" in head_r.headers["content-disposition"]

        get_r = await api_client.get("/api/files/other/download", params={"filepath": rel})
        assert get_r.status_code == AppConstants.HTTP_OK
        assert get_r.content == song_bytes

        range_r = await api_client.get(
            "/api/files/other/download",
            params={"filepath": rel},
            headers={"Range": "bytes=0-3"},
        )
        assert range_r.status_code == 206
        assert range_r.content == b"OggS"

        # 2. Directories blocked -> 400 Bad Request
        assert (
            await api_client.head("/api/files/other/download", params={"filepath": "downloads/songs"})
        ).status_code == AppConstants.HTTP_BAD_REQUEST
        assert (
            await api_client.get("/api/files/other/download", params={"filepath": "downloads/songs"})
        ).status_code == AppConstants.HTTP_BAD_REQUEST

        # Hidden parent directories must ALSO be blocked -> 403 Forbidden
        hidden_dir = storage / ".git"
        hidden_dir.mkdir(parents=True, exist_ok=True)
        (hidden_dir / "config").write_text("[core]\n", encoding="utf-8")
        nested_hidden = songs / ".recycle"
        nested_hidden.mkdir(parents=True, exist_ok=True)
        (nested_hidden / "secret.opus").write_bytes(b"secret")

        # 3. Temp, .part, .mkvforge.*, hidden basenames & hidden parent dirs blocked -> 403 Forbidden
        for blocked in (
            "downloads/songs/temp.mkv.mkvforge.tmp",
            "downloads/songs/partial.mkv.part",
            "downloads/songs/scratch.tmp",
            "downloads/songs/.DS_Store",
            "downloads/songs/._Hidden.opus",
            ".git/config",
            "downloads/songs/.recycle/secret.opus",
        ):
            assert (
                await api_client.head("/api/files/other/download", params={"filepath": blocked})
            ).status_code == AppConstants.HTTP_FORBIDDEN
            assert (
                await api_client.get("/api/files/other/download", params={"filepath": blocked})
            ).status_code == AppConstants.HTTP_FORBIDDEN

        # 4. Path traversal blocked -> 400 Bad Request
        assert (
            await api_client.head("/api/files/other/download", params={"filepath": "../../etc/passwd"})
        ).status_code == AppConstants.HTTP_BAD_REQUEST

        # 5. Missing file -> 404 Not Found
        assert (
            await api_client.head("/api/files/other/download", params={"filepath": "downloads/songs/missing.opus"})
        ).status_code == AppConstants.HTTP_NOT_FOUND

    async def test_local_auth_enforces_401_and_allows_cookie_and_api_key_downloads(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage: Path = api_app.state.settings.storage_dir
        movies = storage / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        (movies / "Private.mkv").write_bytes(b"private-movie-bytes")
        (movies / "notes.srt").write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        file_id = _seed_media_row(api_app, "movies/Private.mkv", 19)

        # Enable local auth (sets HttpOnly session cookie on api_client)
        setup_resp = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "alice",
                "display_name": "Alice",
                "password": "strong-password-123",
            },
        )
        assert setup_resp.status_code == AppConstants.HTTP_OK

        # Create an API key while signed in
        key_resp = await api_client.post("/api/auth/api-keys", json={"name": "Downloader"})
        assert key_resp.status_code == AppConstants.HTTP_OK
        raw_api_key = key_resp.json()["key"]

        # 1. Authenticated session cookie -> 200 OK on HEAD & GET for both media & other
        assert (await api_client.head(f"/api/files/{file_id}/download")).status_code == AppConstants.HTTP_OK
        assert (await api_client.get(f"/api/files/{file_id}/download")).content == b"private-movie-bytes"
        assert (
            await api_client.head("/api/files/other/download", params={"filepath": "movies/notes.srt"})
        ).status_code == AppConstants.HTTP_OK

        # 2. Unauthenticated client -> 401 Unauthorized on HEAD & GET
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon_client:
            assert (await anon_client.head(f"/api/files/{file_id}/download")).status_code == AppConstants.HTTP_UNAUTHORIZED
            assert (await anon_client.get(f"/api/files/{file_id}/download")).status_code == AppConstants.HTTP_UNAUTHORIZED
            assert (
                await anon_client.head("/api/files/other/download", params={"filepath": "movies/notes.srt"})
            ).status_code == AppConstants.HTTP_UNAUTHORIZED
            assert (
                await anon_client.get("/api/files/other/download", params={"filepath": "movies/notes.srt"})
            ).status_code == AppConstants.HTTP_UNAUTHORIZED

            # 3. Valid X-API-Key header -> 200 OK on HEAD & GET
            key_hdr = {AppConstants.AUTH_API_KEY_HEADER: raw_api_key}
            assert (
                await anon_client.head(f"/api/files/{file_id}/download", headers=key_hdr)
            ).status_code == AppConstants.HTTP_OK
            assert (
                await anon_client.get(
                    "/api/files/other/download",
                    params={"filepath": "movies/notes.srt"},
                    headers=key_hdr,
                )
            ).status_code == AppConstants.HTTP_OK


class TestFrontendDownloadPreflightAndContextMenu:
    """Node VM tests verifying HEAD preflight error toasts, native anchor download, and rearranged context menu."""

    def test_frontend_head_preflight_toasts_and_native_download_trigger(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const fetchCalls = [];
        const clickedAnchors = [];
        const unauthorizedCalls = [];
        let mockHeadStatus = 200;
        let mockFetchError = null;

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
            addEventListener: () => {{}},
            body: {{
              appendChild: () => {{}},
              removeChild: () => {{}},
            }},
            createElement: (tag) => ({{
              tag,
              href: "",
              rel: "",
              style: {{}},
              attrs: {{}},
              setAttribute(k, v) {{ this.attrs[k] = v; }},
              click() {{ clickedAnchors.push({{ href: this.href, download: this.attrs.download }}); }},
            }}),
          }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
          fetch: async (url, opts) => {{
            fetchCalls.push({{ url, method: (opts && opts.method) || "GET" }});
            if (mockFetchError) {{
              const err = mockFetchError;
              mockFetchError = null;
              throw err;
            }}
            return {{
              ok: mockHeadStatus >= 200 && mockHeadStatus < 300,
              status: mockHeadStatus,
              headers: {{ get: (k) => (k.toLowerCase() === "content-length" ? "4096" : null) }},
            }};
          }},
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        (async () => {{
          ctx.MKVForgeApi.setUnauthorizedHandler((err, url) => unauthorizedCalls.push({{ status: err.status, url }}));
          const app = ctx.mkvForgeApp();
          const toasts = [];
          app.showToast = (msg, type) => toasts.push({{ msg, type }});

          const mediaFile = {{ id: 7, filename: "Aspirants.S03E02.mkv", filepath: "shows/Aspirants.S03E02.mkv", scan_status: "ready" }};
          const otherFile = {{ name: "Sadi Gali.opus", filepath: "downloads/songs/Sadi Gali.opus", ext: ".opus" }};

          // 1. Media File download -> HEAD 200 -> triggers native <a download>
          mockHeadStatus = 200;
          const okMedia = await app.downloadMediaFile(mediaFile);

          // 2. Other File download via context menu -> HEAD 200 -> triggers native <a download>
          app.openOtherFileContextMenu(otherFile, {{ clientX: 100, clientY: 100 }});
          const otherMenuHeightOk = ctx.MKVForgeConstants.CONTEXT_MENU.OTHER_FILE_MENU_HEIGHT_PX > ctx.MKVForgeConstants.CONTEXT_MENU.FOLDER_MENU_HEIGHT_PX;
          app.runContextMenuAction("download");
          await new Promise((r) => setTimeout(r, 10));

          // 3. HEAD 409 Conflict -> surfaces info toast, does NOT trigger download
          mockHeadStatus = 409;
          const res409 = await app.downloadMediaFile(mediaFile);

          // 4. HEAD 404 Not Found -> surfaces error toast, does NOT trigger download
          mockHeadStatus = 404;
          const res404 = await app.downloadOtherFile(otherFile);

          // 5. HEAD 401 Unauthorized -> invokes unauthorized handler, does NOT trigger download
          mockHeadStatus = 401;
          const res401 = await app.downloadMediaFile(mediaFile);

          // 6. HEAD network error -> surfaces error toast, does NOT trigger download
          mockHeadStatus = 200;
          mockFetchError = new Error("Network down");
          const resNetErr = await app.downloadMediaFile(mediaFile);

          // 7. Client-side busy & scanning guards short-circuit before hitting network
          const callsBeforeGuards = fetchCalls.length;
          const scanningFile = {{ id: 8, filename: "Scan.mkv", filepath: "shows/Scan.mkv", scan_status: "scanning" }};
          const resScanning = await app.downloadMediaFile(scanningFile);
          app.jobsState.active = {{ id: "j1", filepath: "shows/Aspirants.S03E02.mkv", status: "running", progress: 50 }};
          app.liveJob = {{ status: "running", job_id: "j1", filepath: "shows/Aspirants.S03E02.mkv", percent: 50 }};
          const resBusy = await app.downloadMediaFile(mediaFile);
          app.openFileContextMenu(mediaFile, {{ clientX: 50, clientY: 50 }});
          app.runContextMenuAction("download");
          const callsAfterGuards = fetchCalls.length;

          console.log(JSON.stringify({{
            okMedia,
            otherMenuHeightOk,
            res409,
            res404,
            res401,
            resNetErr,
            resScanning,
            resBusy,
            guardNetworkCalls: callsAfterGuards - callsBeforeGuards,
            fetchCalls,
            clickedAnchors,
            toasts,
            unauthorizedCalls,
          }}));
        }})();
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout.strip())

        assert out["okMedia"] is True
        assert out["otherMenuHeightOk"] is True
        assert out["res409"] is False
        assert out["res404"] is False
        assert out["res401"] is False
        assert out["resNetErr"] is False
        assert out["resScanning"] is False
        assert out["resBusy"] is False
        assert out["guardNetworkCalls"] == 0

        # Every preflight strictly used HEAD
        assert all(call["method"] == "HEAD" for call in out["fetchCalls"])
        assert len(out["fetchCalls"]) == 6

        # Only the 2 successful HEAD 200 calls triggered native anchor downloads
        assert out["clickedAnchors"] == [
            {"href": "/api/files/7/download", "download": "Aspirants.S03E02.mkv"},
            {
                "href": "/api/files/other/download?filepath=downloads%2Fsongs%2FSadi%20Gali.opus",
                "download": "Sadi Gali.opus",
            },
        ]

        # 409 -> info toast, 404 -> error toast, network error -> error toast, scanning/busy -> info toasts
        assert out["toasts"][0]["type"] == "info"
        assert "busy" in out["toasts"][0]["msg"].lower() or "processed" in out["toasts"][0]["msg"].lower()
        assert out["toasts"][1]["type"] == "error"
        assert "no longer available" in out["toasts"][1]["msg"].lower()
        assert out["toasts"][2]["type"] == "error"
        assert "network down" in out["toasts"][2]["msg"].lower()
        assert out["toasts"][3]["type"] == "info"
        assert "scanning" in out["toasts"][3]["msg"].lower()
        assert out["unauthorizedCalls"] == [{"status": 401, "url": "/api/files/7/download"}]
