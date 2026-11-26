"""Exhaustive unit and endpoint tests for Grid View Cover Art thumbnails and settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.models.schemas import UserSettings, UserSettingsUpdate
from app.services.thumbnail_service import (
    detect_image_media_type,
    get_or_extract_thumbnail,
    get_thumbnail_cache_path,
    has_image_attachment_in_raw_json,
    is_image_attachment,
    is_valid_image_bytes,
    select_best_cover_attachment,
)

_FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01"
_FAKE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_FAKE_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
_FAKE_GIF = b"GIF89a\x01\x00\x01\x00"


class TestThumbnailDetectionAndRanking:
    """Verify attachment detection, cover ranking, magic-byte validation, and negative inputs."""

    def test_is_image_attachment_and_raw_json_detection(self) -> None:
        assert is_image_attachment({"file_name": "cover.jpg", "content_type": "image/jpeg"}) is True
        assert is_image_attachment({"file_name": "poster.WEBP", "content_type": "application/octet-stream"}) is True
        assert is_image_attachment({"file_name": "Arial.ttf", "content_type": "application/x-truetype-font"}) is False
        assert is_image_attachment(None) is False  # type: ignore[arg-type]

        raw_with_cover = json.dumps(
            {
                "attachments": [
                    {"id": 1, "file_name": "font.ttf", "content_type": "font/ttf", "size": 50000},
                    {"id": 2, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 82000},
                ]
            }
        )
        raw_fonts_only = json.dumps(
            {
                "attachments": [
                    {"id": 1, "file_name": "font.ttf", "content_type": "font/ttf", "size": 50000},
                ]
            }
        )
        assert has_image_attachment_in_raw_json(raw_with_cover) is True
        assert has_image_attachment_in_raw_json(raw_fonts_only) is False
        assert has_image_attachment_in_raw_json("{}") is False
        assert has_image_attachment_in_raw_json("not-valid-json-attachments") is False
        assert has_image_attachment_in_raw_json(None) is False

    def test_select_best_cover_attachment_prefers_small_cover_then_cover(self) -> None:
        payload = {
            "attachments": [
                {"id": 1, "file_name": "font.otf", "content_type": "font/otf", "size": 120000},
                {"id": 2, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 2_400_000},
                {"id": 3, "file_name": "small_cover.jpg", "content_type": "image/jpeg", "size": 48_000},
            ]
        }
        best = select_best_cover_attachment(payload)
        assert best is not None
        assert best.id == 3
        assert best.file_name == "small_cover.jpg"

        assert select_best_cover_attachment({"attachments": []}) is None
        assert select_best_cover_attachment({}) is None

    def test_magic_bytes_validation_and_mime_detection(self) -> None:
        assert is_valid_image_bytes(_FAKE_JPEG) is True
        assert detect_image_media_type(_FAKE_JPEG) == "image/jpeg"

        assert is_valid_image_bytes(_FAKE_PNG) is True
        assert detect_image_media_type(_FAKE_PNG) == "image/png"

        assert is_valid_image_bytes(_FAKE_WEBP) is True
        assert detect_image_media_type(_FAKE_WEBP) == "image/webp"

        assert is_valid_image_bytes(_FAKE_GIF) is True
        assert detect_image_media_type(_FAKE_GIF) == "image/gif"

        assert is_valid_image_bytes(b"") is False
        assert is_valid_image_bytes(b"short") is False
        assert is_valid_image_bytes(b"not-an-image-payload-at-all") is False


class TestThumbnailExtractionAndCaching:
    """Verify disk cache hits, mocked mkvextract/ffmpeg extraction, and negative failure modes."""

    async def test_disk_cache_hit_and_lazy_extraction_with_mtime_invalidation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage_dir = tmp_path / "storage"
        data_dir = tmp_path / "data"
        storage_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        mkv_file = storage_dir / "Movie.mkv"
        mkv_file.write_bytes(b"fake-mkv-container")

        raw_json = json.dumps(
            {
                "attachments": [
                    {"id": 2, "file_name": "cover.png", "content_type": "image/png", "size": 4096}
                ]
            }
        )
        row: dict[str, Any] = {
            "id": 7,
            "filepath": "Movie.mkv",
            "mtime": 1700000500.0,
            "raw_json": raw_json,
        }

        calls: list[list[str]] = []

        async def _fake_run_extract(cmd: list[str], timeout_sec: float) -> bool:
            calls.append(cmd)
            # Extract destination path from `<id>:<tmp_path>` argument
            spec = cmd[-1]
            _, out_str = spec.split(":", 1)
            Path(out_str).write_bytes(_FAKE_PNG)
            return True

        monkeypatch.setattr(
            "app.services.thumbnail_service._run_extract_cmd", _fake_run_extract
        )

        # 1. Cache miss -> extracts once and caches on disk
        res1 = await get_or_extract_thumbnail(
            row, storage_dir=storage_dir, data_dir=data_dir
        )
        assert res1 == (_FAKE_PNG, "image/png")
        assert len(calls) == 1
        assert calls[0][:3] == [AppConstants.TOOL_MKVEXTRACT, str(mkv_file.resolve()), "attachments"]
        assert calls[0][3].startswith("2:")
        cache_file = get_thumbnail_cache_path(data_dir, 7, 1700000500.0)
        assert cache_file.is_file()

        # 2. Second request with same mtime -> hits disk cache with 0 subprocess calls
        res2 = await get_or_extract_thumbnail(
            row, storage_dir=storage_dir, data_dir=data_dir
        )
        assert res2 == (_FAKE_PNG, "image/png")
        assert len(calls) == 1

        # 3. File mtime changes -> invalidates cache and extracts fresh thumbnail
        row_updated = {**row, "mtime": 1700000999.0}
        res3 = await get_or_extract_thumbnail(
            row_updated, storage_dir=storage_dir, data_dir=data_dir
        )
        assert res3 == (_FAKE_PNG, "image/png")
        assert len(calls) == 2

    async def test_negative_cases_missing_attachment_corrupt_output_and_path_traversal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage_dir = tmp_path / "storage"
        data_dir = tmp_path / "data"
        storage_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        mkv_file = storage_dir / "NoArt.mkv"
        mkv_file.write_bytes(b"fake-mkv")

        # 1. No image attachment in raw_json -> returns None without spawning subprocess
        assert (
            await get_or_extract_thumbnail(
                {"id": 1, "filepath": "NoArt.mkv", "mtime": 100.0, "raw_json": "{}"},
                storage_dir=storage_dir,
                data_dir=data_dir,
            )
            is None
        )

        # 2. Path traversal attempt -> rejected safely
        raw_with_cover = json.dumps(
            {"attachments": [{"id": 1, "file_name": "cover.jpg", "content_type": "image/jpeg"}]}
        )
        assert (
            await get_or_extract_thumbnail(
                {"id": 2, "filepath": "../outside.mkv", "mtime": 100.0, "raw_json": raw_with_cover},
                storage_dir=storage_dir,
                data_dir=data_dir,
            )
            is None
        )

        # 3. Subprocess produces non-image garbage -> rejected and temp file cleaned up
        async def _corrupt_extract(cmd: list[str], timeout_sec: float) -> bool:
            if ":" in cmd[-1]:
                _, out_str = cmd[-1].split(":", 1)
                Path(out_str).write_bytes(b"corrupt-non-image-bytes")
            else:
                Path(cmd[-1]).write_bytes(b"corrupt-non-image-bytes")
            return True

        monkeypatch.setattr(
            "app.services.thumbnail_service._run_extract_cmd", _corrupt_extract
        )
        assert (
            await get_or_extract_thumbnail(
                {"id": 3, "filepath": "NoArt.mkv", "mtime": 100.0, "raw_json": raw_with_cover},
                storage_dir=storage_dir,
                data_dir=data_dir,
            )
            is None
        )
        thumbs_dir = data_dir / AppConstants.THUMBNAILS_SUBDIR_NAME
        assert list(thumbs_dir.glob(".thumb_*")) == []


class TestSqliteHasCoverArtDenormalizationAndSetting:
    """Verify SQLite has_cover_art column auto-detection, migration backfill, and UserSettings."""

    def test_upsert_and_settings_default_enabled(self, tmp_path: Path) -> None:
        assert UserSettings().show_grid_thumbnails is True
        assert UserSettingsUpdate(show_grid_thumbnails=False).show_grid_thumbnails is False

        db = DatabaseManager(tmp_path / "test.db")
        db.initialize()
        repo = MediaFileRepository(db)

        id_with = repo.upsert(
            MediaFileUpsert(
                filepath="Movies/WithCover.mkv",
                folder="/Movies",
                filename="WithCover.mkv",
                size_bytes=1024,
                mtime=100.0,
                duration_us=1000,
                resolution="1080p",
                video_codec="HEVC",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=False,
                raw_json=json.dumps(
                    {
                        "attachments": [
                            {"id": 1, "file_name": "cover.jpg", "content_type": "image/jpeg"}
                        ]
                    }
                ),
                scanned_at=utc_now_iso(),
            )
        )
        id_without = repo.upsert(
            MediaFileUpsert(
                filepath="Movies/NoCover.mkv",
                folder="/Movies",
                filename="NoCover.mkv",
                size_bytes=1024,
                mtime=100.0,
                duration_us=1000,
                resolution="1080p",
                video_codec="HEVC",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=False,
                raw_json="{}",
                scanned_at=utc_now_iso(),
            )
        )

        rows, total = repo.list_files()
        assert total == 2
        by_id = {int(r["id"]): r for r in rows}
        assert bool(by_id[id_with]["has_cover_art"]) is True
        assert bool(by_id[id_without]["has_cover_art"]) is False


class TestThumbnailCommandBuildersAndFfmpeg7Compatibility:
    """Verify MKVToolNix v17+/v104 mkvextract ordering and FFmpeg 7+ HDR/RGBA JPEG safety."""

    def test_mkvextract_places_source_file_before_attachments_mode(
        self, tmp_path: Path
    ) -> None:
        from app.services.thumbnail_service import build_mkvextract_attachment_command

        src = tmp_path / "-leading-dash-movie.mkv"
        out = tmp_path / "cover.img"
        cmd = build_mkvextract_attachment_command(src, 4, out)

        assert cmd == [
            AppConstants.TOOL_MKVEXTRACT,
            str(src),
            "attachments",
            f"4:{out}",
        ]
        assert cmd.index(str(src)) < cmd.index("attachments")

    def test_ffmpeg_thumbnail_command_normalizes_pixel_format_for_mjpeg(
        self, tmp_path: Path
    ) -> None:
        from app.services.thumbnail_service import build_ffmpeg_thumbnail_command

        src = tmp_path / "hdr10_clip.mkv"
        out = tmp_path / "thumb.img"

        cmd_default = build_ffmpeg_thumbnail_command(src, out)
        assert "-map" not in cmd_default
        vf_idx = cmd_default.index("-vf")
        assert (
            cmd_default[vf_idx + 1]
            == f"scale='min({AppConstants.THUMBNAIL_MAX_DIMENSION_PX},iw)':-2,format={AppConstants.THUMBNAIL_JPEG_PIXEL_FORMAT}"
        )

        cmd_mapped = build_ffmpeg_thumbnail_command(src, out, map_first_video=True)
        map_idx = cmd_mapped.index("-map")
        assert cmd_mapped[map_idx + 1] == "0:v:0"
        assert cmd_mapped[-1] == str(out)

    async def test_real_ffmpeg_generates_jpeg_from_10bit_hdr_and_rgba_sources(
        self, tmp_path: Path
    ) -> None:
        import shutil
        import subprocess
        from app.services.thumbnail_service import (
            _run_extract_cmd,
            build_ffmpeg_thumbnail_command,
        )

        if shutil.which(AppConstants.TOOL_FFMPEG) is None:
            pytest.skip("ffmpeg not installed locally")

        # 1. Generate a real 10-bit limited-range YUV (yuv420p10le) MKV frame
        hdr_src = tmp_path / "hdr10_10bit.mkv"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:duration=0.2:rate=10",
                "-pix_fmt",
                "yuv420p10le",
                "-c:v",
                "libx264",
                str(hdr_src),
            ],
            check=True,
        )

        hdr_out = tmp_path / "hdr_thumb.jpg"
        ok_hdr = await _run_extract_cmd(
            build_ffmpeg_thumbnail_command(hdr_src, hdr_out, map_first_video=True),
            timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
        )
        assert ok_hdr is True
        hdr_bytes = hdr_out.read_bytes()
        assert is_valid_image_bytes(hdr_bytes) is True
        assert detect_image_media_type(hdr_bytes) == "image/jpeg"

        # 2. Generate a real RGBA PNG image (with alpha channel)
        rgba_src = tmp_path / "alpha_cover.png"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue@0.5:s=512x512:d=0.1",
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgba",
                str(rgba_src),
            ],
            check=True,
        )

        rgba_out = tmp_path / "rgba_thumb.jpg"
        ok_rgba = await _run_extract_cmd(
            build_ffmpeg_thumbnail_command(rgba_src, rgba_out, map_first_video=True),
            timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
        )
        assert ok_rgba is True
        rgba_bytes = rgba_out.read_bytes()
        assert is_valid_image_bytes(rgba_bytes) is True
        assert detect_image_media_type(rgba_bytes) == "image/jpeg"

    async def test_logs_warning_when_mkvextract_falls_back_to_ffmpeg_and_not_when_primary_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        from unittest.mock import patch
        from app.services.thumbnail_service import get_or_extract_thumbnail

        storage = tmp_path / "storage"
        cache_dir = tmp_path / "cache"
        storage.mkdir()
        media = storage / "PosterMovie.mkv"
        media.write_bytes(b"fake-mkv")
        raw_json = json.dumps(
            {
                "attachments": [
                    {"id": 7, "file_name": "cover.jpg", "content_type": "image/jpeg"}
                ]
            }
        )
        fake_jpeg = b"\xff\xd8\xff\xe0" + (b"\x00" * 32)

        # Case 1: mkvextract fails -> logs WARNING and falls back to ffmpeg
        async def _fail_mkvextract_succeed_ffmpeg(argv: list[str], *, timeout_sec: float) -> bool:
            if argv[0] == AppConstants.TOOL_MKVEXTRACT:
                return False
            Path(argv[-1]).write_bytes(fake_jpeg)
            return True

        with (
            caplog.at_level(logging.WARNING, logger="app.services.thumbnail_service"),
            patch("app.services.thumbnail_service._run_extract_cmd", side_effect=_fail_mkvextract_succeed_ffmpeg),
        ):
            res = await get_or_extract_thumbnail(
                {
                    "id": 101,
                    "filepath": "PosterMovie.mkv",
                    "mtime": 123.0,
                    "raw_json": raw_json,
                },
                storage_dir=storage,
                data_dir=cache_dir,
            )

        assert res is not None and res[1] == "image/jpeg"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "PosterMovie.mkv" in warnings[0].getMessage()
        assert "attachment id=7" in warnings[0].getMessage()
        assert "falling back to ffmpeg" in warnings[0].getMessage()

        # Case 2: mkvextract succeeds directly -> zero fallback warnings
        caplog.clear()

        async def _succeed_mkvextract(argv: list[str], *, timeout_sec: float) -> bool:
            assert argv[0] == AppConstants.TOOL_MKVEXTRACT
            # attachments spec is "<id>:<out_path>"
            out_path = argv[-1].split(":", 1)[1]
            Path(out_path).write_bytes(fake_jpeg)
            return True

        with (
            caplog.at_level(logging.WARNING, logger="app.services.thumbnail_service"),
            patch("app.services.thumbnail_service._run_extract_cmd", side_effect=_succeed_mkvextract),
        ):
            res2 = await get_or_extract_thumbnail(
                {
                    "id": 102,
                    "filepath": "PosterMovie.mkv",
                    "mtime": 124.0,
                    "raw_json": raw_json,
                },
                storage_dir=storage,
                data_dir=cache_dir,
            )

        assert res2 is not None and res2[1] == "image/jpeg"
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
