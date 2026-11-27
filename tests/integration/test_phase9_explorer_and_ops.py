"""Phase 9 tests: Explorer directory view, file/folder rename/move/delete, and :ro 403 guards."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso


def _seed_media(
    repo: MediaFileRepository,
    storage: Path,
    rel_filepath: str,
    content: bytes = b"media-bytes",
) -> int:
    full = storage / rel_filepath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    parts = rel_filepath.split("/")
    folder = "/" if len(parts) == 1 else f"/{'/'.join(parts[:-1])}"
    return repo.upsert(
        MediaFileUpsert(
            filepath=rel_filepath,
            folder=folder,
            filename=full.name,
            size_bytes=len(content),
            mtime=1700000000.0,
            duration_us=1_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json='[{"codec":"AAC","channels":6,"channel_layout":"5.1","language":"eng"}]',
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=True,
            has_ac3_eac3=False,
            raw_json='{"tracks":[]}',
            scanned_at=utc_now_iso(),
        )
    )


class TestExplorerDirectoryListing:
    """Verify GET /api/files returns immediate subfolders and cumulative folder_counts."""

    async def test_directory_view_returns_direct_files_subfolders_and_counts(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        _seed_media(repo, storage, "RootClip.mkv")
        _seed_media(repo, storage, "movies/Inception.mkv")
        _seed_media(repo, storage, "movies/4K/Dune.mkv")
        (storage / "movies" / "EmptySub").mkdir(parents=True, exist_ok=True)

        # Directory view at "/" returns RootClip.mkv + immediate subfolder "movies"
        root_resp = await api_client.get("/api/files", params={"folder": "/"})
        assert root_resp.status_code == AppConstants.HTTP_OK
        root_data = root_resp.json()
        assert [item["filename"] for item in root_data["items"]] == ["RootClip.mkv"]
        assert [sub["name"] for sub in root_data["subfolders"]] == ["movies"]
        assert root_data["folder_counts"]["/"] == 3
        assert root_data["folder_counts"]["/movies"] == 2
        assert root_data["folder_counts"]["/movies/4K"] == 1

        # Directory view at "/movies" returns Inception.mkv + subfolders 4K & EmptySub
        mov_resp = await api_client.get("/api/files", params={"folder": "/movies"})
        assert mov_resp.status_code == AppConstants.HTTP_OK
        mov_data = mov_resp.json()
        assert [item["filename"] for item in mov_data["items"]] == ["Inception.mkv"]
        assert [sub["name"] for sub in mov_data["subfolders"]] == ["4K", "EmptySub"]


class TestFileOperationsAndReadOnlyGuards:
    """Verify file rename, single/multi move, single/multi delete, job locks, and :ro 403."""

    async def test_rename_file_happy_path_guards_and_ro_403(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        job_repo = api_app.state.job_repo

        fid1 = _seed_media(repo, storage, "movies/OldName.mkv", b"movie-1")
        fid2 = _seed_media(repo, storage, "movies/Taken.mkv", b"movie-2")

        # 1. Happy path rename (omitting .mkv auto-preserves .mkv and keeps same file_id)
        resp = await api_client.post(
            f"/api/files/{fid1}/rename", json={"new_name": "NewName (2024)"}
        )
        assert resp.status_code == AppConstants.HTTP_OK
        body = resp.json()
        assert body["id"] == fid1
        assert body["filename"] == "NewName (2024).mkv"
        assert body["filepath"] == "movies/NewName (2024).mkv"
        assert (storage / "movies" / "NewName (2024).mkv").is_file()
        assert not (storage / "movies" / "OldName.mkv").exists()

        # 2. Rejects switching container extension (.mkv -> .mp4) -> 400
        bad_ext = await api_client.post(
            f"/api/files/{fid1}/rename", json={"new_name": "Wrong.mp4"}
        )
        assert bad_ext.status_code == AppConstants.HTTP_BAD_REQUEST

        # 3. Rejects traversal & temp marker -> 400
        bad_trav = await api_client.post(
            f"/api/files/{fid1}/rename", json={"new_name": "../escape.mkv"}
        )
        assert bad_trav.status_code == AppConstants.HTTP_BAD_REQUEST
        bad_tmp = await api_client.post(
            f"/api/files/{fid1}/rename", json={"new_name": "X.mkvforge.tmp.mkv"}
        )
        assert bad_tmp.status_code == AppConstants.HTTP_BAD_REQUEST

        # 4. Rejects overwrite collision -> 409
        col = await api_client.post(
            f"/api/files/{fid1}/rename", json={"new_name": "Taken.mkv"}
        )
        assert col.status_code == AppConstants.HTTP_CONFLICT

        # 5. Rejects rename while job is queued/running -> 409
        job_repo.create_job(
            job_id="active-rename-lock",
            filepath="movies/Taken.mkv",
            config_json="{}",
        )
        locked = await api_client.post(
            f"/api/files/{fid2}/rename", json={"new_name": "Locked.mkv"}
        )
        assert locked.status_code == AppConstants.HTTP_CONFLICT

        # 6. Simulated :ro mount (EROFS) -> 403 Forbidden, SQLite untouched
        with patch(
            "app.services.file_ops_service.os.link",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_resp = await api_client.post(
                f"/api/files/{fid1}/rename", json={"new_name": "ReadOnlyTry.mkv"}
            )
        assert ro_resp.status_code == AppConstants.HTTP_FORBIDDEN
        assert "read-only" in ro_resp.json()["detail"].lower()
        assert repo.get_by_id(fid1)["filename"] == "NewName (2024).mkv"

    async def test_duplicate_file_incrementing_suffix_job_lock_and_ro_guards(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        job_repo = api_app.state.job_repo

        fid = _seed_media(repo, storage, "movies/Sample.mkv", b"sample-bytes")

        # 1. First duplicate creates Sample (1).mkv on disk & in SQLite
        dup1 = await api_client.post(f"/api/files/{fid}/duplicate")
        assert dup1.status_code == AppConstants.HTTP_OK
        dup1_body = dup1.json()
        assert dup1_body["id"] != fid
        assert dup1_body["filename"] == "Sample (1).mkv"
        assert dup1_body["filepath"] == "movies/Sample (1).mkv"
        assert dup1_body["folder"] == "/movies"
        assert (storage / "movies" / "Sample (1).mkv").read_bytes() == b"sample-bytes"

        # 2. Duplicating the duplicate ("Sample (1).mkv") increments cleanly to "Sample (2).mkv"
        dup2 = await api_client.post(f"/api/files/{dup1_body['id']}/duplicate")
        assert dup2.status_code == AppConstants.HTTP_OK
        dup2_body = dup2.json()
        assert dup2_body["filename"] == "Sample (2).mkv"
        assert dup2_body["filepath"] == "movies/Sample (2).mkv"
        assert (storage / "movies" / "Sample (2).mkv").read_bytes() == b"sample-bytes"

        # 3. Missing file ID -> 404 Not Found
        not_found = await api_client.post("/api/files/99999/duplicate")
        assert not_found.status_code == AppConstants.HTTP_NOT_FOUND

        # 4. Active job lock -> 409 Conflict
        job_repo.create_job(job_id="dup-lock", filepath="movies/Sample.mkv", config_json="{}")
        locked = await api_client.post(f"/api/files/{fid}/duplicate")
        assert locked.status_code == AppConstants.HTTP_CONFLICT
        job_repo.mark_cancelled("dup-lock")

        # 5. Simulated :ro volume (EROFS) -> 403 Forbidden, no temp or duplicate left on disk
        with patch(
            "app.services.file_ops_service.shutil.copy2",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_dup = await api_client.post(f"/api/files/{fid}/duplicate")
        assert ro_dup.status_code == AppConstants.HTTP_FORBIDDEN
        assert "read-only" in ro_dup.json()["detail"].lower()
        assert not (storage / "movies" / "Sample (3).mkv").exists()

    async def test_move_files_batch_partial_failure_and_ro_403(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        job_repo = api_app.state.job_repo

        (storage / "archive").mkdir(parents=True, exist_ok=True)
        fid1 = _seed_media(repo, storage, "movies/One.mkv", b"one")
        fid2 = _seed_media(repo, storage, "movies/Two.mkv", b"two")
        fid3 = _seed_media(repo, storage, "movies/Busy.mkv", b"busy")
        job_repo.create_job(job_id="busy-job", filepath="movies/Busy.mkv", config_json="{}")

        # Batch move: One & Two succeed, Busy fails with 409 active job lock
        resp = await api_client.post(
            "/api/files/move",
            json={"file_ids": [fid1, fid2, fid3], "destination_folder": "/archive"},
        )
        assert resp.status_code == AppConstants.HTTP_OK
        data = resp.json()
        assert data["succeeded"] == 2
        assert data["failed"] == 1
        assert (storage / "archive" / "One.mkv").is_file()
        assert (storage / "archive" / "Two.mkv").is_file()
        assert (storage / "movies" / "Busy.mkv").is_file()
        assert repo.get_by_id(fid1)["filepath"] == "archive/One.mkv"
        assert repo.get_by_id(fid1)["folder"] == "/archive"

        # Moving on a read-only mount -> 403 Forbidden and DB stays at archive/One.mkv
        (storage / "dest_ro").mkdir(parents=True, exist_ok=True)
        with patch(
            "app.services.file_ops_service.os.link",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_move = await api_client.post(
                "/api/files/move",
                json={"file_ids": [fid1], "destination_folder": "/dest_ro"},
            )
        assert ro_move.status_code == AppConstants.HTTP_FORBIDDEN
        assert repo.get_by_id(fid1)["filepath"] == "archive/One.mkv"

    async def test_delete_files_happy_path_job_lock_and_ro_permission_denied(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        job_repo = api_app.state.job_repo

        fid1 = _seed_media(repo, storage, "movies/DelA.mkv", b"a")
        fid2 = _seed_media(repo, storage, "movies/DelB.mkv", b"b")
        fid3 = _seed_media(repo, storage, "movies/Protected.mkv", b"c")
        job_repo.create_job(job_id="lock-del", filepath="movies/Protected.mkv", config_json="{}")

        # Simulated :ro volume (EROFS) on delete -> 403 Forbidden, DB row untouched!
        with patch(
            "pathlib.Path.unlink",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_del = await api_client.post("/api/files/delete", json={"file_ids": [fid1]})
        assert ro_del.status_code == AppConstants.HTTP_FORBIDDEN
        assert "read-only" in ro_del.json()["detail"].lower()
        assert repo.get_by_id(fid1) is not None
        assert (storage / "movies" / "DelA.mkv").is_file()

        # Simulated PermissionError (EACCES) -> 403 Forbidden, DB row untouched!
        with patch(
            "pathlib.Path.unlink",
            side_effect=PermissionError(errno.EACCES, "Permission denied"),
        ):
            perm_del = await api_client.post("/api/files/delete", json={"file_ids": [fid1]})
        assert perm_del.status_code == AppConstants.HTTP_FORBIDDEN
        assert repo.get_by_id(fid1) is not None

        # Active job lock -> 409 Conflict
        lock_del = await api_client.post("/api/files/delete", json={"file_ids": [fid3]})
        assert lock_del.status_code == AppConstants.HTTP_CONFLICT
        assert (storage / "movies" / "Protected.mkv").is_file()

        # Multi-delete happy path -> removes both files from disk & SQLite
        ok_del = await api_client.post(
            "/api/files/delete", json={"file_ids": [fid1, fid2]}
        )
        assert ok_del.status_code == AppConstants.HTTP_OK
        assert ok_del.json()["succeeded"] == 2
        assert ok_del.json()["failed"] == 0
        assert not (storage / "movies" / "DelA.mkv").exists()
        assert not (storage / "movies" / "DelB.mkv").exists()
        assert repo.get_by_id(fid1) is None
        assert repo.get_by_id(fid2) is None


class TestFolderOperationsAndReadOnlyGuards:
    """Verify folder create/rename/move/delete, descendant DB sync, and :ro 403 guards."""

    async def test_folder_rename_move_delete_and_guards(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        storage = test_settings.storage_dir
        repo = api_app.state.media_repo
        job_repo = api_app.state.job_repo

        fid_nested = _seed_media(repo, storage, "shows/OldShow/S01/Ep1.mkv", b"ep1")
        (storage / "archive").mkdir(parents=True, exist_ok=True)

        # 1. Rename folder rewrites nested DB rows atomically
        ren = await api_client.post(
            "/api/folders/rename",
            json={"path": "/shows/OldShow", "new_name": "NewShow"},
        )
        assert ren.status_code == AppConstants.HTTP_OK
        assert ren.json()["new_path"] == "/shows/NewShow"
        assert ren.json()["affected_files"] == 1
        assert repo.get_by_id(fid_nested)["filepath"] == "shows/NewShow/S01/Ep1.mkv"
        assert repo.get_by_id(fid_nested)["folder"] == "/shows/NewShow/S01"

        # 2. Cannot move folder into itself or its own descendant -> 400
        self_move = await api_client.post(
            "/api/folders/move",
            json={"path": "/shows/NewShow", "destination_folder": "/shows/NewShow/S01"},
        )
        assert self_move.status_code == AppConstants.HTTP_BAD_REQUEST

        # 3. Move folder into /archive rewrites nested DB rows
        mov = await api_client.post(
            "/api/folders/move",
            json={"path": "/shows/NewShow", "destination_folder": "/archive"},
        )
        assert mov.status_code == AppConstants.HTTP_OK
        assert mov.json()["new_path"] == "/archive/NewShow"
        assert repo.get_by_id(fid_nested)["filepath"] == "archive/NewShow/S01/Ep1.mkv"

        # 4. Cannot delete root '/' -> 400
        del_root = await api_client.post(
            "/api/folders/delete", json={"path": "/", "recursive": True}
        )
        assert del_root.status_code == AppConstants.HTTP_BAD_REQUEST

        # 5. Non-empty folder without recursive=True -> 409 Conflict
        del_non_empty = await api_client.post(
            "/api/folders/delete", json={"path": "/archive/NewShow", "recursive": False}
        )
        assert del_non_empty.status_code == AppConstants.HTTP_CONFLICT

        # 6. Folder containing active job cannot be deleted -> 409 Conflict
        job_repo.create_job(
            job_id="folder-lock",
            filepath="archive/NewShow/S01/Ep1.mkv",
            config_json="{}",
        )
        del_locked = await api_client.post(
            "/api/folders/delete", json={"path": "/archive/NewShow", "recursive": True}
        )
        assert del_locked.status_code == AppConstants.HTTP_CONFLICT
        job_repo.mark_cancelled("folder-lock")

        # 7. Read-only volume (:ro -> EROFS) on folder delete & create -> 403 Forbidden, DB intact!
        with patch(
            "app.services.file_ops_service.shutil.rmtree",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_del = await api_client.post(
                "/api/folders/delete",
                json={"path": "/archive/NewShow", "recursive": True},
            )
        assert ro_del.status_code == AppConstants.HTTP_FORBIDDEN
        assert repo.get_by_id(fid_nested) is not None

        with patch(
            "pathlib.Path.mkdir",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            ro_mkdir = await api_client.post(
                "/api/folders", json={"parent": "/", "name": "ReadOnlyDir"}
            )
        assert ro_mkdir.status_code == AppConstants.HTTP_FORBIDDEN

        # 8. Recursive delete happy path removes directory + descendant SQLite rows
        ok_del = await api_client.post(
            "/api/folders/delete",
            json={"path": "/archive/NewShow", "recursive": True},
        )
        assert ok_del.status_code == AppConstants.HTTP_OK
        assert ok_del.json()["affected_files"] == 1
        assert not (storage / "archive" / "NewShow").exists()
        assert repo.get_by_id(fid_nested) is None

    async def test_recursive_root_search_finds_deeply_nested_files(
        self, api_client: httpx.AsyncClient, api_app
    ) -> None:
        """Omitting folder searches recursively from / across all nested directories."""
        storage: Path = api_app.state.settings.storage_dir
        repo: MediaFileRepository = api_app.state.media_repo

        _seed_media(repo, storage, "downloads/Good_news/Good_Newwz_1080p.mkv")
        _seed_media(repo, storage, "movies/Action/Good_Fellas_1990.mkv")
        _seed_media(repo, storage, "shows/Drama/Other_Episode.mp4")

        resp = await api_client.get("/api/files", params={"q": "Good"})
        assert resp.status_code == AppConstants.HTTP_OK
        payload = resp.json()
        names = [item["filename"] for item in payload["items"]]
        assert names == ["Good_Fellas_1990.mkv", "Good_Newwz_1080p.mkv"]
        assert payload["total"] == 2
        assert payload["subfolders"] == []

    async def test_frontend_explorer_and_ops_landmarks(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        constants_js = (await api_client.get("/static/js/constants.js")).text

        assert 'DIR: "dir"' in constants_js
        assert "FOLDER_LAYOUTS" in constants_js
        assert "CONTEXT_MENU" in constants_js
        # Left explorer sidebar & All Files folder dropdown are removed
        assert "Directory Explorer Tree" not in html
        assert "All Folders</option>" not in html
        # Material icons are 100% inline offline (no external font/CDN URLs)
        assert "fonts.googleapis.com" not in html
        assert "cdn.jsdelivr.net" not in html
        # Core Library views, Grid/List switcher, and Context Menu exist
        assert "Folders" in html
        assert "All Files" in html
        assert "setFolderLayout(C.FOLDER_LAYOUTS.LIST)" in html
        assert "setFolderLayout(C.FOLDER_LAYOUTS.GRID)" in html
        assert html.index("setFolderLayout(C.FOLDER_LAYOUTS.LIST)") < html.index(
            "setFolderLayout(C.FOLDER_LAYOUTS.GRID)"
        )
        app_js = (await api_client.get("/static/js/app.js")).text
        assert "return C.FOLDER_LAYOUTS.LIST;" in app_js
        assert "_libraryRequestSeq" in app_js
        assert "clearStale" in app_js
        assert "openFileContextMenu" in html
        assert "openFolderContextMenu" in html
        assert "runContextMenuAction('rescan')" in html
        assert "runContextMenuAction('copy')" in html
        assert "runContextMenuAction('rename')" in html
        assert "runContextMenuAction('move')" in html
        assert "runContextMenuAction('delete')" in html
        assert "Read-only mounts will fail safely" in html
        # Up button is removed from folder breadcrumb bar; mobile uses 3-dot folder options menu
        assert 'aria-label="Go up one folder"' not in html
        assert 'aria-label="Folder view options"' in html
        assert "folderBarMenuOpen" in html
        # Sidebar footer displays app version & name; top-right uses unified scan refresh dropdown
        assert "Pinned Sidebar Footer (" in html
        assert "Pinned bottom Scan Library controls" not in html
        assert "Scan library options" in html

        dash = (await api_client.get("/api/dashboard")).json()
        assert dash["app_name"] == AppConstants.APP_NAME
        assert dash["app_version"] == AppConstants.APP_VERSION

    async def test_formatters_file_extension_label_edge_cases(self) -> None:
        """Verify fileExtensionLabel handles normal, malformed, and edge-case names safely."""
        import subprocess

        script = (
            "global.window = global;"
            "require('./app/static/js/constants.js');"
            "require('./app/static/js/formatters.js');"
            "const f = global.MKVForgeFormatters.fileExtensionLabel;"
            "const assert = require('assert');"
            "assert.strictEqual(f('Good_Newwz.mkv'), 'MKV');"
            "assert.strictEqual(f('/movies/Clip.MP4'), 'MP4');"
            "assert.strictEqual(f('archive.tar.webm'), 'WEBM');"
            "assert.strictEqual(f('noext'), 'FILE');"
            "assert.strictEqual(f('.hidden'), 'FILE');"
            "assert.strictEqual(f('trailing.'), 'FILE');"
            "assert.strictEqual(f(''), 'FILE');"
            "assert.strictEqual(f(null), 'FILE');"
            "assert.strictEqual(f('movie.superlongext'), 'SUPERL');"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
