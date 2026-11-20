"""Exhaustive tests for Folder View Other Files (Show Media Files Only, Extension Normalization & File Ops)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.services.folder_service import (
    normalize_visible_extensions_csv,
    parse_visible_extensions_set,
)


class TestExtensionNormalization:
    """Verify extension list normalization and mandatory .mkv/.mp4 preservation."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ".mkv, .mp4"),
            ("   ", ".mkv, .mp4"),
            ("srt, ASS, .nfo", ".mkv, .mp4, .srt, .ass, .nfo"),
            (".MKV, mp4, .srt; .jpg", ".mkv, .mp4, .srt, .jpg"),
            ("bad/ext, .ok, ..", ".mkv, .mp4, .ok, .."),
        ],
    )
    def test_normalize_visible_extensions_csv(self, raw: str, expected: str) -> None:
        assert normalize_visible_extensions_csv(raw) == expected
        parsed = parse_visible_extensions_set(raw)
        assert ".mkv" in parsed
        assert ".mp4" in parsed


class TestOtherFilesListingAndOps:
    """Verify two-layer Folder View other_files listing, junk filtering, and Rename/Move/Delete."""

    async def test_settings_filter_other_files_and_skip_hidden_junk(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        movies = storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        (movies / "Film.mkv").write_bytes(b"video-bytes")
        (movies / "Film.srt").write_bytes(b"sub-bytes")
        (movies / "poster.jpg").write_bytes(b"img-bytes")
        (movies / "notes.nfo").write_bytes(b"nfo-bytes")
        # System junk that must ALWAYS be skipped in both modes
        (movies / ".DS_Store").write_bytes(b"junk")
        (movies / "._Film.mkv").write_bytes(b"appledouble")
        (movies / "Film.mkv.mkvforge.tmp").write_bytes(b"temp")
        (movies / "Downloading.mkv.part").write_bytes(b"part")

        # 1. Default setting (show_media_files_only=True, ".mkv, .mp4") -> other_files is empty
        r1 = await api_client.get("/api/files", params={"folder": "/movies"})
        assert r1.status_code == AppConstants.HTTP_OK
        assert r1.json()["other_files"] == []

        # 2. Add "srt, NFO" (unnormalized) -> normalized to ".mkv, .mp4, .srt, .nfo"
        patch_ext = await api_client.patch(
            "/api/settings", json={"visible_media_extensions": "srt, NFO"}
        )
        assert patch_ext.status_code == AppConstants.HTTP_OK
        assert patch_ext.json()["visible_media_extensions"] == ".mkv, .mp4, .srt, .nfo"

        r2 = await api_client.get("/api/files", params={"folder": "/movies"})
        other_names_2 = [f["name"] for f in r2.json()["other_files"]]
        assert other_names_2 == ["Film.srt", "notes.nfo"]

        # 3. Clearing the box still preserves .mkv, .mp4
        patch_empty = await api_client.patch(
            "/api/settings", json={"visible_media_extensions": "   "}
        )
        assert patch_empty.json()["visible_media_extensions"] == ".mkv, .mp4"

        # 4. Turn OFF show_media_files_only ("Show All Files") -> shows ALL non-hidden files
        #    (including .part / .uploading), while still hiding .DS_Store, ._*, and *.mkvforge.*
        (movies / "Movie.mkv.abc123.uploading").write_bytes(b"uploading-chunk")
        patch_all = await api_client.patch(
            "/api/settings", json={"show_media_files_only": False}
        )
        assert patch_all.status_code == AppConstants.HTTP_OK
        r3 = await api_client.get("/api/files", params={"folder": "/movies"})
        other_names_3 = [f["name"] for f in r3.json()["other_files"]]
        assert other_names_3 == [
            "Downloading.mkv.part",
            "Film.srt",
            "Movie.mkv.abc123.uploading",
            "notes.nfo",
            "poster.jpg",
        ]

        # SQLite media_files table remains strictly .mkv/.mp4 only (never bloated with other files)
        all_indexed = [fp for _id, fp in api_app.state.media_repo.list_all_filepaths()]
        assert all_indexed == ["movies/Film.mkv"]

    async def test_watcher_emits_live_sse_for_non_media_added_and_deleted_not_modified(
        self, api_app
    ) -> None:
        from watchfiles import Change

        storage_dir: Path = api_app.state.settings.storage_dir
        movies = storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        uploading = movies / "NewFilm.mkv.44f10dd8.uploading"
        uploading.write_bytes(b"chunk1")

        watcher = api_app.state.library_watcher
        events: list[tuple[str, str]] = []
        orig_record = api_app.state.library_broker.record_change

        def _capture(*, action: str, filepath: str, file_id: int | None = None) -> None:
            events.append((action, filepath))
            orig_record(action=action, filepath=filepath, file_id=file_id)

        api_app.state.library_broker.record_change = _capture

        # 1. Added -> emits once immediately so Show All Files shows it right away
        watcher.handle_raw_fs_event(Change.added, uploading)
        # 2. Modified bursts while uploading -> emits ZERO SSE events (no spam!)
        watcher.handle_raw_fs_event(Change.modified, uploading)
        watcher.handle_raw_fs_event(Change.modified, uploading)
        assert events == [("updated", "movies/NewFilm.mkv.44f10dd8.uploading")]

        # 3. Deleted/renamed away -> emits deleted SSE
        uploading.unlink()
        watcher.handle_raw_fs_event(Change.deleted, uploading)
        import asyncio
        await asyncio.sleep(0.05)
        assert ("deleted", "movies/NewFilm.mkv.44f10dd8.uploading") in events

    async def test_other_file_rename_move_delete_and_promotion_to_media(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        movies = storage_dir / "movies"
        archive = storage_dir / "archive"
        movies.mkdir(parents=True, exist_ok=True)
        archive.mkdir(parents=True, exist_ok=True)
        (movies / "Sub.srt").write_bytes(b"sub-content")
        (movies / "Existing.srt").write_bytes(b"existing-sub")
        (movies / "RawVideo.dat").write_bytes(b"renamed-to-mkv")

        # 1. Rename collision -> 409 Conflict
        col = await api_client.post(
            "/api/files/other/rename",
            json={"filepath": "movies/Sub.srt", "new_name": "Existing.srt"},
        )
        assert col.status_code == AppConstants.HTTP_CONFLICT

        # 2. Rename success
        ren = await api_client.post(
            "/api/files/other/rename",
            json={"filepath": "movies/Sub.srt", "new_name": "English.srt"},
        )
        assert ren.status_code == AppConstants.HTTP_OK
        assert ren.json()["new_filepath"] == "movies/English.srt"
        assert ren.json()["promoted_to_media"] is False
        assert (movies / "English.srt").is_file()

        # 3. Move to /archive
        mv = await api_client.post(
            "/api/files/other/move",
            json={"filepath": "movies/English.srt", "destination_folder": "/archive"},
        )
        assert mv.status_code == AppConstants.HTTP_OK
        assert mv.json()["new_filepath"] == "archive/English.srt"
        assert (archive / "English.srt").is_file()

        # 4. Delete
        dl = await api_client.post(
            "/api/files/other/delete",
            json={"filepath": "archive/English.srt"},
        )
        assert dl.status_code == AppConstants.HTTP_OK
        assert not (archive / "English.srt").exists()

        # 5. Path traversal rejected -> 400 Bad Request
        bad = await api_client.post(
            "/api/files/other/delete",
            json={"filepath": "../outside.txt"},
        )
        assert bad.status_code == AppConstants.HTTP_BAD_REQUEST

        # 6. Renaming a non-media file to .mkv promotes it into two-phase indexing ('scanning')
        promo = await api_client.post(
            "/api/files/other/rename",
            json={"filepath": "movies/RawVideo.dat", "new_name": "Promoted.mkv"},
        )
        assert promo.status_code == AppConstants.HTTP_OK
        assert promo.json()["promoted_to_media"] is True
        promoted_row = api_app.state.media_repo.get_by_filepath("movies/Promoted.mkv")
        assert promoted_row is not None
        assert promoted_row["scan_status"] == AppConstants.FILE_SCAN_STATUS_SCANNING

    async def test_ui_landmarks_for_media_filter_and_other_files(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert "Show Media Files Only" in html
        assert 'id="visible-media-extensions"' in html
        assert "Other Files" in html
        assert "openOtherFileContextMenu(of, $event)" in html
