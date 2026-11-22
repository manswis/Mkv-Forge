"""Unit tests for SQLite WAL connection and repositories."""

from __future__ import annotations

import json

from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
    decode_audio_summary_json,
    utc_now_iso,
)


def _sample_record(
    filepath: str,
    *,
    folder: str = "/movies",
    filename: str | None = None,
    size_bytes: int = 1000,
    mtime: float = 1700000000.0,
    duration_us: int = 7_200_000_000,
    resolution: str | None = "1080p",
    video_codec: str | None = "HEVC",
    subtitle_count: int = 1,
    container_title: str = "",
    has_global_tags: bool = False,
    has_aac_51: bool = False,
    has_ac3_eac3: bool = False,
) -> MediaFileUpsert:
    return MediaFileUpsert(
        filepath=filepath,
        folder=folder,
        filename=filename or filepath.split("/")[-1],
        size_bytes=size_bytes,
        mtime=mtime,
        duration_us=duration_us,
        resolution=resolution,
        video_codec=video_codec,
        audio_summary_json=json.dumps(
            [{"codec": "AAC", "channels": 6, "channel_layout": "5.1", "language": "eng"}]
        ),
        subtitle_count=subtitle_count,
        container_title=container_title,
        has_global_tags=has_global_tags,
        has_aac_51=has_aac_51,
        has_ac3_eac3=has_ac3_eac3,
        raw_json="{}",
        scanned_at=utc_now_iso(),
    )


class TestDatabaseInitialization:
    """Verify WAL mode, tables, and indexes."""

    def test_initializes_wal_mode_and_tables(self, db_manager: DatabaseManager) -> None:
        with db_manager.connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }

        assert journal_mode.lower() == "wal"
        assert {"app_state", "media_files", "jobs"}.issubset(tables)
        assert {
            "idx_media_files_folder",
            "idx_media_files_has_aac_51",
            "idx_media_files_has_ac3_eac3",
            "idx_jobs_filepath",
            "idx_jobs_status",
        }.issubset(indexes)


class TestAppStateRepository:
    """Verify key-value state persistence."""

    def test_get_set_overwrite_and_get_all(self, db_manager: DatabaseManager) -> None:
        repo = AppStateRepository(db_manager)

        assert repo.get("last_scan_at") is None
        repo.set("last_scan_at", "2026-11-19T14:00:00+00:00")
        assert repo.get("last_scan_at") == "2026-11-19T14:00:00+00:00"

        repo.set("last_scan_at", "2026-11-19T15:00:00+00:00")
        repo.set("last_scan_status", "completed")
        assert repo.get_all() == {
            "last_scan_at": "2026-11-19T15:00:00+00:00",
            "last_scan_status": "completed",
        }


class TestMediaFileRepository:
    """Verify CRUD, filtering, sorting, pagination, and dashboard counts."""

    def test_upsert_updates_in_place_preserving_id(
        self, db_manager: DatabaseManager
    ) -> None:
        repo = MediaFileRepository(db_manager)
        first_id = repo.upsert(
            _sample_record("movies/Dune.mkv", size_bytes=100, has_aac_51=True)
        )
        second_id = repo.upsert(
            _sample_record(
                "movies/Dune.mkv",
                size_bytes=200,
                has_aac_51=True,
                has_ac3_eac3=True,
            )
        )

        assert first_id == second_id
        row = repo.get_by_id(first_id)
        assert row is not None
        assert row["size_bytes"] == 200
        assert bool(row["has_ac3_eac3"]) is True
        assert repo.get_by_filepath("movies/Dune.mkv")["id"] == first_id

    def test_filters_search_sorting_pagination_and_counts(
        self, db_manager: DatabaseManager
    ) -> None:
        repo = MediaFileRepository(db_manager)
        # 1. AAC 5.1 only + container title metadata
        repo.upsert(
            _sample_record(
                "movies/Alpha_100%.mkv",
                folder="/movies",
                size_bytes=5000,
                has_aac_51=True,
                has_ac3_eac3=False,
                container_title="Junk Title",
            )
        )
        # 2. Has BOTH AAC 5.1 and EAC3 (already fixed -> NOT aac51_only!)
        repo.upsert(
            _sample_record(
                "movies/Beta.mkv",
                folder="/movies",
                size_bytes=8000,
                has_aac_51=True,
                has_ac3_eac3=True,
            )
        )
        # 3. No Dolby, no AAC 5.1, has global tags
        repo.upsert(
            _sample_record(
                "shows/Gamma.mp4",
                folder="/shows",
                size_bytes=2000,
                has_aac_51=False,
                has_ac3_eac3=False,
                has_global_tags=True,
            )
        )

        counts = repo.get_dashboard_counts()
        assert counts == {
            "total_files": 3,
            "aac51_only": 1,
            "has_metadata": 2,
            "needs_conversion": 0,
        }
        assert repo.list_folders() == ["/movies", "/shows"]

        # aac51_only vs aac51 vs no_dolby vs has_metadata
        items, total = repo.list_files(aac51_only=True)
        assert total == 1
        assert items[0]["filename"] == "Alpha_100%.mkv"
        assert "raw_json" not in items[0]

        items, total = repo.list_files(aac51=True)
        assert total == 2

        items, total = repo.list_files(no_dolby=True)
        assert total == 2

        items, total = repo.list_files(has_metadata=True)
        assert total == 2

        # Folder filter
        items, total = repo.list_files(folder="/shows")
        assert total == 1
        assert items[0]["filename"] == "Gamma.mp4"

        # Literal LIKE escaping for '%' and '_'
        items, total = repo.list_files(q="100%")
        assert total == 1
        assert items[0]["filename"] == "Alpha_100%.mkv"

        # Sort & pagination + invalid sort fallback
        items, total = repo.list_files(sort="size_bytes", order="desc", page=1, limit=2)
        assert total == 3
        assert [row["filename"] for row in items] == ["Beta.mkv", "Alpha_100%.mkv"]

        page2, _ = repo.list_files(sort="size_bytes", order="desc", page=2, limit=2)
        assert [row["filename"] for row in page2] == ["Gamma.mp4"]

        fallback, _ = repo.list_files(sort="DROP TABLE media_files;", order="invalid")
        assert [row["filename"] for row in fallback] == [
            "Alpha_100%.mkv",
            "Beta.mkv",
            "Gamma.mp4",
        ]

    def test_scan_index_and_deletions(self, db_manager: DatabaseManager) -> None:
        repo = MediaFileRepository(db_manager)
        id1 = repo.upsert(_sample_record("a.mkv", folder="/", mtime=10.5, size_bytes=123))
        repo.upsert(_sample_record("b.mkv", folder="/", mtime=20.5, size_bytes=456))

        assert repo.get_scan_index() == {
            "a.mkv": (10.5, 123, "ready"),
            "b.mkv": (20.5, 456, "ready"),
        }
        assert repo.delete_by_filepaths(["b.mkv"]) == 1
        assert repo.delete_by_filepaths([]) == 0
        assert repo.delete_by_id(id1) is True
        assert repo.delete_by_id(9999) is False
        assert repo.get_scan_index() == {}

    def test_cascading_audio_type_and_folder_facets(
        self, db_manager: DatabaseManager
    ) -> None:
        repo = MediaFileRepository(db_manager)
        # Coco in /movies -> EAC3 5.1 only, clean metadata
        repo.upsert(
            MediaFileUpsert(
                filepath="movies/Coco 2017.mkv",
                folder="/movies",
                filename="Coco 2017.mkv",
                size_bytes=5000,
                mtime=1.0,
                duration_us=1000,
                resolution="1080p",
                video_codec="HEVC",
                audio_summary_json=json.dumps(
                    [{"codec": "EAC3", "channels": 6, "channel_layout": "5.1", "language": "eng"}]
                ),
                subtitle_count=1,
                container_title="",
                has_global_tags=False,
                has_aac_51=False,
                has_ac3_eac3=True,
                raw_json="{}",
                scanned_at=utc_now_iso(),
            )
        )
        # Baishe Srabon in /bengali -> MP3 2.0 + AAC 2.0, has metadata
        repo.upsert(
            MediaFileUpsert(
                filepath="bengali/Baishe Srabon.mkv",
                folder="/bengali",
                filename="Baishe Srabon.mkv",
                size_bytes=2000,
                mtime=2.0,
                duration_us=1000,
                resolution="720p",
                video_codec="H.264",
                audio_summary_json=json.dumps(
                    [
                        {"codec": "MP3", "channels": 2, "channel_layout": "2.0", "language": "ben"},
                        {"codec": "AAC", "channels": 2, "channel_layout": "2.0", "language": "ben"},
                    ]
                ),
                subtitle_count=0,
                container_title="Baishe Srabon",
                has_global_tags=True,
                has_aac_51=False,
                has_ac3_eac3=False,
                raw_json="{}",
                scanned_at=utc_now_iso(),
            )
        )

        # Unfiltered: all 3 distinct sound types present
        all_audio = repo.list_audio_types()
        assert all_audio == [
            {"label": "AAC 2.0", "count": 1},
            {"label": "EAC3 5.1", "count": 1},
            {"label": "MP3 2.0", "count": 1},
        ]

        # Cascading on search q="coco": ONLY EAC3 5.1, ONLY /movies, 0 metadata / 0 no_dolby
        assert repo.list_audio_types(q="coco") == [{"label": "EAC3 5.1", "count": 1}]
        assert repo.list_folders(q="coco") == ["/movies"]
        assert repo.get_workflow_facet_counts(q="coco") == {
            "aac51_only": 0,
            "has_metadata": 0,
            "no_dolby": 0,
            "needs_conversion": 0,
        }

        # Filtering by audio_type is case-insensitive and works on multi-audio files
        mp3_items, mp3_total = repo.list_files(audio_type="mp3 2.0")
        assert mp3_total == 1
        assert mp3_items[0]["filename"] == "Baishe Srabon.mkv"


class TestJobRepositoryAndHelpers:
    """Verify recent job query and audio summary JSON decoder."""

    def test_list_recent_orders_descending(self, db_manager: DatabaseManager) -> None:
        with db_manager.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, filepath, status, config_json, progress, created_at)
                VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-1",
                    "movies/Old.mkv",
                    "completed",
                    "{}",
                    100,
                    "2026-11-19T10:00:00+00:00",
                    "job-2",
                    "movies/New.mkv",
                    "completed",
                    "{}",
                    100,
                    "2026-11-19T12:00:00+00:00",
                ),
            )

        repo = JobRepository(db_manager)
        recent = repo.list_recent(limit=1)
        assert len(recent) == 1
        assert recent[0]["id"] == "job-2"

    def test_decode_audio_summary_handles_malformed_json(self) -> None:
        assert decode_audio_summary_json(None) == []
        assert decode_audio_summary_json("{not-json") == []
        assert decode_audio_summary_json('{"not": "a list"}') == []
        assert decode_audio_summary_json('[{"codec":"EAC3"}]') == [{"codec": "EAC3"}]
