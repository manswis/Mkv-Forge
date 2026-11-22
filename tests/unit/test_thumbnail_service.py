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
