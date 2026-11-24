"""Exhaustive unit, API & Node VM tests for Other Files Grid View and Album/Cover Art Thumbnails."""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import AppConstants
from app.services.thumbnail_service import (
    extract_embedded_audio_picture_sync,
    get_or_extract_other_file_thumbnail,
    get_other_thumbnail_cache_path,
    is_other_file_thumbnail_candidate,
)
from app.utils.paths import PathSafetyError

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_DIR = REPO_ROOT / "app" / "static" / "js"
INDEX_HTML_PATH = REPO_ROOT / "app" / "static" / "index.html"

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
FAKE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"


def _build_flac_picture_block(
    image_bytes: bytes,
    *,
    picture_type: int = 3,
    mime: bytes = b"image/jpeg",
    desc: bytes = b"Cover",
) -> bytes:
    return b"".join(
        [
            struct.pack(">II", picture_type, len(mime)),
            mime,
            struct.pack(">I", len(desc)),
            desc,
            struct.pack(">IIII", 500, 500, 24, 0),
            struct.pack(">I", len(image_bytes)),
            image_bytes,
        ]
    )


def _build_ogg_pages_for_packets(packets: list[bytes]) -> bytes:
    """Wrap logical packets into valid RFC 3533 Ogg pages (including multi-segment packets >255B)."""
    pages: list[bytes] = []
    seq = 0
    for pkt_idx, packet in enumerate(packets):
        # Split packet into 255-byte lacing segments + final <255 segment
        segments: list[int] = []
        pos = 0
        while pos + 255 <= len(packet):
            segments.append(255)
            pos += 255
        segments.append(len(packet) - pos)

        # Chunk segments into pages (max 255 segments per page)
        seg_pos = 0
        body_pos = 0
        first_chunk = True
        while seg_pos < len(segments):
            chunk_segs = segments[seg_pos : seg_pos + 255]
            chunk_body_len = sum(chunk_segs)
            chunk_body = packet[body_pos : body_pos + chunk_body_len]
            flags = 0x02 if (pkt_idx == 0 and first_chunk) else (0x01 if not first_chunk else 0x00)
            header = struct.pack(
                "<4sBBqIIIB",
                b"OggS",
                0,
                flags,
                0,
                1,
                seq,
                0,
                len(chunk_segs),
            )
            pages.append(header + bytes(chunk_segs) + chunk_body)
            seg_pos += len(chunk_segs)
            body_pos += chunk_body_len
            seq += 1
            first_chunk = False
    return b"".join(pages)


def _build_opus_with_pictures(pictures: list[tuple[int, bytes]]) -> bytes:
    """Build a minimal valid Ogg Opus file containing OpusHead + OpusTags with METADATA_BLOCK_PICTURE."""
    opus_head = b"OpusHead" + struct.pack("<BBHIHB", 1, 2, 0, 48000, 0, 0)
    vendor = b"mkvforge-test"
    comments: list[bytes] = [b"TITLE=Test Song"]
    for pic_type, img_bytes in pictures:
        block = _build_flac_picture_block(img_bytes, picture_type=pic_type)
        b64 = base64.b64encode(block)
        comments.append(b"METADATA_BLOCK_PICTURE=" + b64)
    opus_tags = bytearray(b"OpusTags")
    opus_tags.extend(struct.pack("<I", len(vendor)))
    opus_tags.extend(vendor)
    opus_tags.extend(struct.pack("<I", len(comments)))
    for c in comments:
        opus_tags.extend(struct.pack("<I", len(c)))
        opus_tags.extend(c)
    return _build_ogg_pages_for_packets([opus_head, bytes(opus_tags)])


def _build_native_flac_with_picture(image_bytes: bytes, *, picture_type: int = 3) -> bytes:
    """Build a minimal valid native FLAC file with a PICTURE (type 6) metadata block."""
    streaminfo = b"\x00" * 34
    si_hdr = bytes([0x00]) + len(streaminfo).to_bytes(3, "big")
    pic_block = _build_flac_picture_block(image_bytes, picture_type=picture_type)
    pic_hdr = bytes([0x80 | AppConstants.FLAC_BLOCK_TYPE_PICTURE]) + len(pic_block).to_bytes(3, "big")
    return b"fLaC" + si_hdr + streaminfo + pic_hdr + pic_block


class TestOtherFileAlbumArtExtraction:
    """Verify Ogg Opus/Vorbis, native FLAC, direct image, FFmpeg fallback, caching, and negative safety."""

    def test_extension_candidate_classification(self) -> None:
        for ext in (".opus", ".MP3", ".flac", ".m4a", ".ogg", ".jpg", ".png", ".webp", "songs/a.opus"):
            assert is_other_file_thumbnail_candidate(ext) is True
        for ext in (".srt", ".ass", ".nfo", ".txt", ".pdf", "", None):
            assert is_other_file_thumbnail_candidate(ext) is False

    def test_extracts_multipage_opus_front_cover_and_prefers_front_over_back(
        self, tmp_path: Path
    ) -> None:
        # Large JPEG (>1000 bytes) forces multi-segment Ogg lacing (>255B segments)
        large_front_jpg = FAKE_JPEG[:-2] + (b"\x00" * 1200) + b"\xff\xd9"
        opus_file = tmp_path / "Song.opus"
        opus_file.write_bytes(
            _build_opus_with_pictures(
                [
                    (4, FAKE_PNG),  # Back cover
                    (3, large_front_jpg),  # Front cover (must be preferred!)
                ]
            )
        )
        extracted = extract_embedded_audio_picture_sync(opus_file)
        assert extracted == large_front_jpg

    def test_extracts_native_flac_picture_block(self, tmp_path: Path) -> None:
        flac_file = tmp_path / "Track.flac"
        flac_file.write_bytes(_build_native_flac_with_picture(FAKE_PNG))
        extracted = extract_embedded_audio_picture_sync(flac_file)
        assert extracted == FAKE_PNG

    def test_malformed_or_truncated_audio_files_fail_safely(self, tmp_path: Path) -> None:
        corrupt_opus = tmp_path / "Corrupt.opus"
        corrupt_opus.write_bytes(b"OggS\x00\x02truncated")
        assert extract_embedded_audio_picture_sync(corrupt_opus) is None

        bad_b64_opus = tmp_path / "BadB64.opus"
        opus_head = b"OpusHead" + b"\x01\x02" + b"\x00" * 10
        bad_comment = b"METADATA_BLOCK_PICTURE=!!!not-base64!!!"
        opus_tags = (
            b"OpusTags"
            + struct.pack("<I", 0)
            + struct.pack("<I", 1)
            + struct.pack("<I", len(bad_comment))
            + bad_comment
        )
        bad_b64_opus.write_bytes(_build_ogg_pages_for_packets([opus_head, opus_tags]))
        assert extract_embedded_audio_picture_sync(bad_b64_opus) is None

        corrupt_flac = tmp_path / "Corrupt.flac"
        corrupt_flac.write_bytes(b"fLaC\x86\x00\x00\x10short")
        assert extract_embedded_audio_picture_sync(corrupt_flac) is None

    async def test_other_file_thumbnail_caching_negative_cache_and_ffmpeg_fallback(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        data = tmp_path / "data"
        (storage / "songs").mkdir(parents=True)
        data.mkdir()

        # 1. Opus with embedded art -> extracted without ffmpeg & cached to disk
        opus_path = storage / "songs" / "Hit.opus"
        opus_path.write_bytes(_build_opus_with_pictures([(3, FAKE_JPEG)]))
        with patch("app.services.thumbnail_service._run_extract_cmd", new_callable=AsyncMock) as mock_ff:
            res = await get_or_extract_other_file_thumbnail(
                "songs/Hit.opus", storage_dir=storage, data_dir=data
            )
            assert res == (FAKE_JPEG, "image/jpeg")
            mock_ff.assert_not_awaited()

        # 2. Direct small image -> served & cached directly
        img_path = storage / "songs" / "cover.png"
        img_path.write_bytes(FAKE_PNG)
        img_res = await get_or_extract_other_file_thumbnail(
            "songs/cover.png", storage_dir=storage, data_dir=data
        )
        assert img_res == (FAKE_PNG, "image/png")

        # 3. MP3 without art -> runs ffmpeg once, writes negative sentinel, second call skips ffmpeg!
        mp3_path = storage / "songs" / "NoArt.mp3"
        mp3_path.write_bytes(b"ID3-audio-without-art")
        with patch(
            "app.services.thumbnail_service._run_extract_cmd",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_ff:
            first = await get_or_extract_other_file_thumbnail(
                "songs/NoArt.mp3", storage_dir=storage, data_dir=data
            )
            second = await get_or_extract_other_file_thumbnail(
                "songs/NoArt.mp3", storage_dir=storage, data_dir=data
            )
            assert first is None
            assert second is None
            assert mock_ff.await_count == 1

            cache_file = get_other_thumbnail_cache_path(
                data, "songs/NoArt.mp3", float(mp3_path.stat().st_mtime)
            )
            assert cache_file.read_bytes() == AppConstants.THUMBNAIL_NEGATIVE_MARKER_BYTES

        # 4. Path traversal raises PathSafetyError
        with pytest.raises(PathSafetyError):
            await get_or_extract_other_file_thumbnail(
                "../escape.opus", storage_dir=storage, data_dir=data
            )

        # 5. Unsupported extension returns None without spawning ffmpeg
        txt_path = storage / "songs" / "notes.txt"
        txt_path.write_text("hello", encoding="utf-8")
        assert (
            await get_or_extract_other_file_thumbnail(
                "songs/notes.txt", storage_dir=storage, data_dir=data
            )
            is None
        )


class TestOtherFilesGridApiAndFrontend:
    """Verify /api/files other_files metadata, /api/files/other/thumbnail endpoint, and Grid View UI."""

    async def test_api_lists_thumbnail_candidates_and_serves_opus_album_art(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        songs = storage_dir / "downloads" / "songs"
        songs.mkdir(parents=True, exist_ok=True)
        (songs / "Sadi Gali.opus").write_bytes(_build_opus_with_pictures([(3, FAKE_JPEG)]))
        (songs / "lyrics.txt").write_text("lyrics", encoding="utf-8")

        await api_client.patch("/api/settings", json={"show_media_files_only": False})
        resp = await api_client.get("/api/files", params={"folder": "/downloads/songs"})
        assert resp.status_code == AppConstants.HTTP_OK
        others = {item["name"]: item for item in resp.json()["other_files"]}
        assert others["Sadi Gali.opus"]["has_thumbnail_candidate"] is True
        assert others["lyrics.txt"]["has_thumbnail_candidate"] is False

        # Thumbnail endpoint returns embedded Opus album art with immutable cache header
        thumb = await api_client.get(
            "/api/files/other/thumbnail",
            params={"filepath": "downloads/songs/Sadi Gali.opus"},
        )
        assert thumb.status_code == AppConstants.HTTP_OK
        assert thumb.headers["content-type"] == "image/jpeg"
        assert thumb.headers["cache-control"] == AppConstants.THUMBNAIL_CACHE_CONTROL_HEADER
        assert thumb.content == FAKE_JPEG

        # Non-thumbnail file -> 404; path traversal -> 400
        assert (
            await api_client.get(
                "/api/files/other/thumbnail",
                params={"filepath": "downloads/songs/lyrics.txt"},
            )
        ).status_code == AppConstants.HTTP_NOT_FOUND
        assert (
            await api_client.get(
                "/api/files/other/thumbnail",
                params={"filepath": "../../etc/passwd"},
            )
        ).status_code == AppConstants.HTTP_BAD_REQUEST

    def test_html_and_app_js_support_other_files_grid_and_album_art(self) -> None:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        assert 'x-for="of in otherFiles" :key="`tile-other-${of.filepath}`"' in html
        assert "shouldShowOtherFileThumb(of)" in html
        assert "otherFileThumbnailUrl(of)" in html
        assert "markOtherThumbError(of)" in html
        assert "isOtherAudioFile(of)" in html

        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
          fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const app = ctx.mkvForgeApp();
        const opusFile = {{
          name: "Sadi Gali.opus",
          filepath: "downloads/songs/Sadi Gali.opus",
          ext: ".opus",
          mtime: 1700000100,
          size_bytes: 3800000,
          has_thumbnail_candidate: true,
        }};
        const txtFile = {{
          name: "notes.txt",
          filepath: "downloads/songs/notes.txt",
          ext: ".txt",
          mtime: 1700000100,
          size_bytes: 128,
          has_thumbnail_candidate: false,
        }};

        const showOpusDefault = app.shouldShowOtherFileThumb(opusFile);
        const showTxtDefault = app.shouldShowOtherFileThumb(txtFile);
        const urlOpus = app.otherFileThumbnailUrl(opusFile);
        const isOpusAudio = app.isOtherAudioFile(opusFile);
        const isTxtAudio = app.isOtherAudioFile(txtFile);

        // Marking error suppresses thumb for current mtime, but newer mtime retries automatically!
        app.markOtherThumbError(opusFile);
        const showAfterError = app.shouldShowOtherFileThumb(opusFile);
        const showAfterNewMtime = app.shouldShowOtherFileThumb({{ ...opusFile, mtime: 1700000200 }});

        // Disabling Grid View Cover Art suppresses all other-file thumbs
        app.userSettings.show_grid_thumbnails = false;
        const showWhenSettingOff = app.shouldShowOtherFileThumb({{ ...opusFile, mtime: 1700000200 }});

        // Null/empty safety
        const showNull = app.shouldShowOtherFileThumb(null);
        const urlNull = app.otherFileThumbnailUrl(null);

        console.log(JSON.stringify({{
          showOpusDefault,
          showTxtDefault,
          urlOpus,
          isOpusAudio,
          isTxtAudio,
          showAfterError,
          showAfterNewMtime,
          showWhenSettingOff,
          showNull,
          urlNull,
        }}));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout.strip())
        assert out["showOpusDefault"] is True
        assert out["showTxtDefault"] is False
        assert out["urlOpus"] == "/api/files/other/thumbnail?filepath=downloads%2Fsongs%2FSadi%20Gali.opus&v=1700000100"
        assert out["isOpusAudio"] is True
        assert out["isTxtAudio"] is False
        assert out["showAfterError"] is False
        assert out["showAfterNewMtime"] is True
        assert out["showWhenSettingOff"] is False
        assert out["showNull"] is False
        assert out["urlNull"] == ""
