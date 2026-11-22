"""Integration tests for /api/settings, video_dynamic_range backfill, and UI badges."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.services.probe_parser import parse_mkvmerge_json


class TestSettingsApiAndVideoDetails:
    """Verify GET/PATCH /api/settings, dashboard hydration, backfill, and frontend badges."""

    async def test_settings_get_patch_and_validation_errors(
        self, api_client: httpx.AsyncClient
    ) -> None:
        # Default is show_video_details=True on both /api/settings and /api/dashboard
        get_resp = await api_client.get("/api/settings")
        assert get_resp.status_code == AppConstants.HTTP_OK
        assert get_resp.json()["show_video_details"] is True

        dash_resp = await api_client.get("/api/dashboard")
        assert dash_resp.status_code == AppConstants.HTTP_OK
        assert dash_resp.json()["user_settings"]["show_video_details"] is True

        # Toggle to False via PATCH
        patch_resp = await api_client.patch(
            "/api/settings", json={"show_video_details": False}
        )
        assert patch_resp.status_code == AppConstants.HTTP_OK
        assert patch_resp.json()["show_video_details"] is False

        assert (await api_client.get("/api/settings")).json()["show_video_details"] is False
        assert (await api_client.get("/api/dashboard")).json()["user_settings"][
            "show_video_details"
        ] is False

        # Unknown setting key rejected with 422; existing setting stays intact
        bad_key = await api_client.patch(
            "/api/settings", json={"unknown_flag": True}
        )
        assert bad_key.status_code == 422

        bad_type = await api_client.patch(
            "/api/settings", json={"show_video_details": "not-a-bool"}
        )
        assert bad_type.status_code == 422

        assert (await api_client.get("/api/settings")).json()["show_video_details"] is False

    async def test_legacy_db_migration_and_zero_io_backfill_populates_hdr_and_sdr(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "legacy.db"
        # Build a legacy media_files table WITHOUT video_dynamic_range column
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT NOT NULL UNIQUE,
                folder TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime REAL NOT NULL,
                duration_us INTEGER NOT NULL DEFAULT 0,
                resolution TEXT,
                video_codec TEXT,
                audio_summary_json TEXT NOT NULL DEFAULT '[]',
                subtitle_count INTEGER NOT NULL DEFAULT 0,
                container_title TEXT NOT NULL DEFAULT '',
                has_global_tags INTEGER NOT NULL DEFAULT 0,
                has_aac_51 INTEGER NOT NULL DEFAULT 0,
                has_ac3_eac3 INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                scanned_at TEXT NOT NULL
            );
            """
        )
        hdr_raw = json.dumps(
            {
                "container": {"properties": {"duration": 1_000_000_000}},
                "tracks": [
                    {
                        "id": 0,
                        "type": "video",
                        "codec": "HEVC/H.265/MPEG-H",
                        "properties": {
                            "pixel_dimensions": "3840x2160",
                            "color_transfer_characteristics": 16,
                        },
                    }
                ],
            }
        )
        sdr_raw = json.dumps(
            {
                "container": {"properties": {"duration": 1_000_000_000}},
                "tracks": [
                    {
                        "id": 0,
                        "type": "video",
                        "codec": "AVC/H.264/MPEG-4p10",
                        "properties": {"pixel_dimensions": "1920x1080"},
                    }
                ],
            }
        )
        conn.execute(
            """
            INSERT INTO media_files (
                filepath, folder, filename, size_bytes, mtime, duration_us,
                resolution, video_codec, audio_summary_json, subtitle_count,
                container_title, has_global_tags, has_aac_51, has_ac3_eac3,
                raw_json, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, '', 0, 0, 1, ?, ?)
            """,
            ("movies/Dune.mkv", "/movies", "Dune.mkv", 100, 1.0, 1000, "2160p", "HEVC", hdr_raw, utc_now_iso()),
        )
        conn.execute(
            """
            INSERT INTO media_files (
                filepath, folder, filename, size_bytes, mtime, duration_us,
                resolution, video_codec, audio_summary_json, subtitle_count,
                container_title, has_global_tags, has_aac_51, has_ac3_eac3,
                raw_json, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, '', 0, 0, 1, ?, ?)
            """,
            ("movies/Classic.mkv", "/movies", "Classic.mkv", 100, 1.0, 1000, "1080p", "H.264", sdr_raw, utc_now_iso()),
        )
        conn.commit()
        conn.close()

        # Run DatabaseManager.initialize() + zero-I/O backfill
        db = DatabaseManager(db_path)
        db.initialize()
        repo = MediaFileRepository(db)
        updated_count = repo.backfill_video_dynamic_range(
            lambda payload: parse_mkvmerge_json(payload).video_dynamic_range
        )
        assert updated_count == 2

        dune = repo.get_by_filepath("movies/Dune.mkv")
        classic = repo.get_by_filepath("movies/Classic.mkv")
        assert dune is not None and dune["video_dynamic_range"] == "HDR10"
        assert classic is not None and classic["video_dynamic_range"] == "SDR"

        # Second backfill run is a no-op
        assert (
            repo.backfill_video_dynamic_range(
                lambda payload: parse_mkvmerge_json(payload).video_dynamic_range
            )
            == 0
        )

    async def test_files_api_exposes_video_dynamic_range_and_frontend_formatters(
        self, api_client: httpx.AsyncClient, api_app, test_settings
    ) -> None:
        repo: MediaFileRepository = api_app.state.media_repo
        media = test_settings.storage_dir / "Movie.mkv"
        media.write_bytes(b"video-bytes")
        repo.upsert(
            MediaFileUpsert(
                filepath="Movie.mkv",
                folder="/",
                filename="Movie.mkv",
                size_bytes=11,
                mtime=1700000000.0,
                duration_us=1_000_000,
                resolution="2160p",
                video_codec="HEVC",
                video_dynamic_range="DV HDR",
                audio_summary_json="[]",
                subtitle_count=0,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=True,
                raw_json='{"tracks":[]}',
                scanned_at=utc_now_iso(),
            )
        )

        resp = await api_client.get("/api/files")
        assert resp.status_code == AppConstants.HTTP_OK
        item = resp.json()["items"][0]
        assert item["resolution"] == "2160p"
        assert item["video_codec"] == "HEVC"
        assert item["video_dynamic_range"] == "DV HDR"

        html = (await api_client.get("/")).text
        assert "userSettings.show_video_details && file.video_codec" in html
        assert "userSettings.show_video_details && file.video_dynamic_range" in html

        # Verify frontend video badge formatters safely handle normal & edge-case inputs
        script = (
            "global.window = global;"
            "require('./app/static/js/constants.js');"
            "require('./app/static/js/formatters.js');"
            "const F = global.MKVForgeFormatters;"
            "const assert = require('assert');"
            "assert.strictEqual(F.videoCodecTooltip('HEVC'), 'HEVC / H.265');"
            "assert.strictEqual(F.videoCodecTooltip('H.264'), 'H.264 / AVC');"
            "assert.strictEqual(F.videoCodecTooltip('CustomCodec'), 'CustomCodec');"
            "assert.strictEqual(F.videoCodecTooltip(null), '');"
            "assert.ok(F.videoCodecBadgeClass('HEVC').includes('ui-status-info'));"
            "assert.ok(F.videoCodecBadgeClass('H.264').includes('ui-tag'));"
            "assert.ok(F.dynamicRangeBadgeClass('DV HDR').includes('text-accent'));"
            "assert.ok(F.dynamicRangeBadgeClass('HDR10').includes('ui-status-warn'));"
            "assert.ok(F.dynamicRangeBadgeClass('HLG').includes('ui-status-warn'));"
            "assert.ok(F.dynamicRangeBadgeClass('SDR').includes('ui-tag'));"
            "assert.ok(F.dynamicRangeBadgeClass(null).includes('ui-tag'));"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
