"""Exhaustive unit and API tests for Stage 1 scratch mount telemetry, storage folder picker, and exclusion."""

from __future__ import annotations

import os
from pathlib import Path
import stat

import httpx
import pytest
from watchfiles import Change

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, JobRepository, MediaFileRepository
from app.main import create_app
from app.models.schemas import UserSettings, UserSettingsUpdate
from app.services.library_events import LibraryEventBroker
from app.services.maintenance_service import MaintenanceService
from app.services.processing_policy import (
    check_directory_writable,
    get_excluded_storage_scratch_rel_prefix,
    inspect_scratch_location,
    is_path_on_tmpfs,
    is_rel_path_within_excluded_prefix,
    list_managed_scratch_dirs,
    normalize_storage_scratch_folder,
    resolve_processing_policy,
    resolve_stage1_scratch_dir,
)
from app.services.scanner import LibraryScanner, _discover_media_files
from app.services.user_settings import UserSettingsService
from app.services.watcher import LibraryWatcher

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"


def _make_settings(tmp_path: Path, *, scratch_dir: Path | None = None) -> Settings:
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    storage.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return Settings(
        storage_dir=storage,
        data_dir=data,
        static_dir=STATIC_DIR,
        health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
        scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
        log_level=AppConstants.DEFAULT_LOG_LEVEL,
        scratch_dir=scratch_dir,
        watch_mode=AppConstants.WATCH_MODE_OFF,
    )


class TestStorageScratchFolderNormalization:
    """Positive, negative, and path-traversal security tests for storage scratch subfolders."""

    @pytest.mark.parametrize(
        ("raw", "expected_rel"),
        [
            ("hdd-temp", "hdd-temp"),
            ("/hdd-temp/", "hdd-temp"),
            ("temp/stage1", "temp/stage1"),
            ("hdd\\nested\\audio", "hdd/nested/audio"),
            ("  /my temp/audio/  ", "my temp/audio"),
        ],
    )
    def test_normalize_valid_subfolders(
        self, tmp_path: Path, raw: str, expected_rel: str
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()
        rel, resolved = normalize_storage_scratch_folder(raw, storage_root=storage)
        assert rel == expected_rel
        assert resolved == (storage / expected_rel).resolve(strict=False)

    @pytest.mark.parametrize(
        "bad_input",
        [
            None,
            "",
            "   ",
            "/",
            "///",
            ".",
            "./",
            "..",
            "../outside",
            "sub/../../outside",
            "sub/..",
            "hdd\x00temp",
        ],
    )
    def test_reject_invalid_root_and_traversal_inputs(
        self, tmp_path: Path, bad_input: str | None
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()
        with pytest.raises(ValueError):
            normalize_storage_scratch_folder(bad_input, storage_root=storage)

    def test_reject_symlink_escaping_storage_root(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        outside = tmp_path / "outside"
        storage.mkdir()
        outside.mkdir()
        escape_link = storage / "escape"
        escape_link.symlink_to(outside)

        with pytest.raises(ValueError):
            normalize_storage_scratch_folder("escape", storage_root=storage)


class TestMountTelemetryAndWritability:
    """Tests for Linux `/proc/mounts` tmpfs detection, writability probes, and location inspection."""

    def test_is_path_on_tmpfs_longest_prefix_and_escaped_spaces(
        self, tmp_path: Path
    ) -> None:
        mounts_file = tmp_path / "mounts"
        base = tmp_path.resolve()
        ram_dir = base / "ram_tmp"
        nested_disk = ram_dir / "disk_child"
        spaced_ram = base / "ram mount"
        ext4_dir = base / "media"

        ram_dir.mkdir()
        nested_disk.mkdir()
        spaced_ram.mkdir()
        ext4_dir.mkdir()

        escaped_spaced = str(spaced_ram).replace(" ", "\\040")
        mounts_file.write_text(
            "\n".join(
                [
                    "overlay / overlay rw 0 0",
                    f"tmpfs {ram_dir} tmpfs rw,nosuid,nodev 0 0",
                    f"/dev/sda1 {nested_disk} ext4 rw 0 0",
                    f"ramfs {escaped_spaced} ramfs rw 0 0",
                    f"/dev/sdb1 {ext4_dir} ext4 rw 0 0",
                    "malformed_short_line",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        assert is_path_on_tmpfs(ram_dir / "mkvforge_scratch", proc_mounts_path=mounts_file) is True
        assert is_path_on_tmpfs(nested_disk / "sub", proc_mounts_path=mounts_file) is False
        assert is_path_on_tmpfs(spaced_ram / "sub", proc_mounts_path=mounts_file) is True
        assert is_path_on_tmpfs(ext4_dir / "movie.mkv", proc_mounts_path=mounts_file) is False

        # Missing mounts file degrades gracefully to False
        assert is_path_on_tmpfs(ram_dir, proc_mounts_path=tmp_path / "no_such_mounts") is False

    def test_check_directory_writable_and_inspect_locations(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()

        # Existing writable directory
        existing_dir = storage / "hdd-temp"
        existing_dir.mkdir()
        assert check_directory_writable(existing_dir) == (True, True)

        # Non-existent child under writable parent (e.g., <DATA_DIR>/scratch before first job)
        pending_dir = tmp_path / "data" / "scratch"
        (tmp_path / "data").mkdir()
        assert check_directory_writable(pending_dir) == (False, True)

        # Regular file blocking target path
        blocking_file = storage / "not_a_dir.mkv"
        blocking_file.write_bytes(b"data")
        assert check_directory_writable(blocking_file) == (False, False)

        # Unconfigured location
        unconfigured = inspect_scratch_location("custom", None, storage_root=storage)
        assert unconfigured.configured is False
        assert unconfigured.exists is False
        assert unconfigured.writable is False
        assert unconfigured.free_bytes is None

        # Configured location returns live free bytes and device match
        info = inspect_scratch_location("storage_folder", existing_dir, storage_root=storage)
        assert info.configured is True
        assert info.exists is True
        assert info.writable is True
        assert info.free_bytes is not None and info.free_bytes > 0
        assert info.same_device_as_storage is True

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="Root ignores POSIX read-only directory permissions",
    )
    def test_check_directory_writable_detects_readonly_dir(self, tmp_path: Path) -> None:
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            assert check_directory_writable(ro_dir) == (True, False)
        finally:
            ro_dir.chmod(stat.S_IRWXU)


class TestPolicyScannerWatcherAndCleanupIntegration:
    """Verify storage temp folder resolution, scanner/watcher exclusion, and stray temp sweeping."""

    def test_resolve_policy_and_managed_dirs_for_storage_folder(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        hdd_temp = settings.storage_dir / "hdd-temp"
        hdd_temp.mkdir()

        resolved = resolve_stage1_scratch_dir(
            AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER,
            settings=settings,
            storage_folder="hdd-temp",
        )
        assert resolved == hdd_temp.resolve()

        # Invalid traversal or missing folder value safely falls back to None (source mode)
        assert (
            resolve_stage1_scratch_dir(
                AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER,
                settings=settings,
                storage_folder="../escape",
            )
            is None
        )
        assert (
            resolve_stage1_scratch_dir(
                AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER,
                settings=settings,
                storage_folder=None,
            )
            is None
        )

        # Regular file occupying storage_folder path also fails safe
        bad_file = settings.storage_dir / "file_not_dir"
        bad_file.write_bytes(b"x")
        assert (
            resolve_stage1_scratch_dir(
                AppConstants.STAGE1_SCRATCH_STORAGE_FOLDER,
                settings=settings,
                storage_folder="file_not_dir",
            )
            is None
        )

        policy = resolve_processing_policy(
            UserSettings(
                stage1_scratch_mode="storage_folder",
                stage1_scratch_storage_folder="hdd-temp",
            ),
            settings=settings,
        )
        assert policy.scratch_mode == "storage_folder"
        assert policy.scratch_dir == hdd_temp.resolve()

        managed = list_managed_scratch_dirs(settings, storage_folder="hdd-temp")
        assert hdd_temp.resolve() in managed

    def test_excluded_prefix_matching_and_scanner_discovery(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        storage = settings.storage_dir

        (storage / "temp").mkdir()
        (storage / "temp" / "nested").mkdir()
        (storage / "temp" / "in_temp.mkv").write_bytes(b"123")
        (storage / "temp" / "nested" / "deep.mkv").write_bytes(b"123")
        (storage / "temp_movies").mkdir()
        (storage / "temp_movies" / "real_movie.mkv").write_bytes(b"123")
        (storage / "Movies").mkdir()
        (storage / "Movies" / "visible.mkv").write_bytes(b"123")

        # Exclusion active ONLY when mode is storage_folder
        inactive_prefix = get_excluded_storage_scratch_rel_prefix(
            UserSettings(stage1_scratch_mode="source", stage1_scratch_storage_folder="temp"),
            storage_root=storage,
        )
        assert inactive_prefix is None

        active_prefix = get_excluded_storage_scratch_rel_prefix(
            UserSettings(
                stage1_scratch_mode="storage_folder",
                stage1_scratch_storage_folder="temp",
            ),
            storage_root=storage,
        )
        assert active_prefix == "temp"
        assert is_rel_path_within_excluded_prefix("temp/a.mkv", active_prefix) is True
        assert is_rel_path_within_excluded_prefix("temp", active_prefix) is True
        assert is_rel_path_within_excluded_prefix("temp_movies/real_movie.mkv", active_prefix) is False

        discovered, errors = _discover_media_files(
            storage, excluded_rel_prefix=active_prefix
        )
        assert errors == 0
        rel_paths = {item.rel_filepath for item in discovered}
        assert rel_paths == {"Movies/visible.mkv", "temp_movies/real_movie.mkv"}

    def test_watcher_ignores_events_inside_active_storage_scratch_folder(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        db = DatabaseManager(settings.db_path)
        db.initialize()
        state_repo = AppStateRepository(db)
        media_repo = MediaFileRepository(db)
        job_repo = JobRepository(db)
        user_settings = UserSettingsService(state_repo=state_repo)
        user_settings.update(
            UserSettingsUpdate(
                stage1_scratch_mode="storage_folder",
                stage1_scratch_storage_folder="hdd-temp",
            )
        )
        scanner = LibraryScanner(
            settings=settings,
            media_repo=media_repo,
            state_repo=state_repo,
            user_settings_service=user_settings,
        )
        broker = LibraryEventBroker()
        watcher = LibraryWatcher(
            settings=settings,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            library_broker=broker,
            user_settings_service=user_settings,
        )

        scratch_dir = settings.storage_dir / "hdd-temp"
        scratch_dir.mkdir()
        scratch_mkv = scratch_dir / "working.mkv"
        scratch_mkv.write_bytes(b"abc")

        # Event inside active storage scratch folder must schedule zero debounce tasks
        watcher.handle_raw_fs_event(Change.added, scratch_mkv)
        assert watcher._debounce_tasks == {}
        broker.close()

    def test_maintenance_finds_stray_temp_in_hidden_storage_scratch_folder(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        db = DatabaseManager(settings.db_path)
        db.initialize()
        state_repo = AppStateRepository(db)
        media_repo = MediaFileRepository(db)
        job_repo = JobRepository(db)
        user_settings = UserSettingsService(state_repo=state_repo)

        hidden_scratch = settings.storage_dir / ".hdd-scratch"
        hidden_scratch.mkdir()
        stray = hidden_scratch / "movie.mkvforge.audio.eac3"
        stray.write_bytes(b"12345")

        user_settings.update(
            UserSettingsUpdate(
                stage1_scratch_mode="storage_folder",
                stage1_scratch_storage_folder=".hdd-scratch",
            )
        )
        scanner = LibraryScanner(
            settings=settings,
            media_repo=media_repo,
            state_repo=state_repo,
            user_settings_service=user_settings,
        )
        broker = LibraryEventBroker()
        maint = MaintenanceService(
            settings=settings,
            db=db,
            state_repo=state_repo,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            library_broker=broker,
            user_settings_service=user_settings,
        )

        found = maint._find_stray_temp_files()
        assert any(path.resolve() == stray.resolve() for path, _ in found)
        broker.close()


class TestSettingsApiScratchEndpoints:
    """End-to-end FastAPI tests for `/api/settings/runtime` and `PATCH /api/settings`."""

    async def test_runtime_telemetry_and_storage_folder_validation(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        hdd_temp = settings.storage_dir / "hdd-temp"
        hdd_temp.mkdir()
        plain_file = settings.storage_dir / "clip.mkv"
        plain_file.write_bytes(b"mkv")

        app = create_app(settings)
        app.state.db.initialize()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Runtime endpoint returns all 5 scratch location telemetry objects
            rt = await client.get("/api/settings/runtime")
            assert rt.status_code == 200
            rt_body = rt.json()
            assert rt_body["storage_dir_path"] == str(settings.storage_dir)
            locs = rt_body["scratch_locations"]
            assert set(locs.keys()) == {"source", "tmp", "data", "storage_folder", "custom"}
            assert locs["source"]["configured"] is True
            assert locs["source"]["writable"] is True
            assert locs["source"]["free_bytes"] > 0
            assert locs["storage_folder"]["configured"] is False
            assert locs["custom"]["configured"] is False

            # 2. Reject enabling storage_folder without choosing a folder first
            res_missing_folder = await client.patch(
                "/api/settings", json={"stage1_scratch_mode": "storage_folder"}
            )
            assert res_missing_folder.status_code == 400

            # 3. Reject root, traversal, non-existent folder, and regular file
            for bad_folder in ("/", "../etc", "does-not-exist", "clip.mkv"):
                res_bad = await client.patch(
                    "/api/settings",
                    json={
                        "stage1_scratch_mode": "storage_folder",
                        "stage1_scratch_storage_folder": bad_folder,
                    },
                )
                assert res_bad.status_code == 400, f"Expected 400 for {bad_folder!r}"

            # 4. Accept valid writable subfolder and normalize slashes
            res_ok = await client.patch(
                "/api/settings",
                json={
                    "stage1_scratch_mode": "storage_folder",
                    "stage1_scratch_storage_folder": "/hdd-temp/",
                },
            )
            assert res_ok.status_code == 200
            saved = res_ok.json()
            assert saved["stage1_scratch_mode"] == "storage_folder"
            assert saved["stage1_scratch_storage_folder"] == "hdd-temp"

            # 5. Runtime endpoint reflects the configured storage_folder live
            rt2 = await client.get("/api/settings/runtime")
            assert rt2.status_code == 200
            rt2_body = rt2.json()
            assert rt2_body["storage_folder_scratch_path"] == str(hdd_temp.resolve())
            sf_loc = rt2_body["scratch_locations"]["storage_folder"]
            assert sf_loc["configured"] is True
            assert sf_loc["exists"] is True
            assert sf_loc["writable"] is True
            assert sf_loc["same_device_as_storage"] is True

        app.state.library_broker.close()

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="Root ignores POSIX read-only directory permissions",
    )
    async def test_reject_readonly_storage_folder_via_api(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        ro_folder = settings.storage_dir / "ro-temp"
        ro_folder.mkdir()
        ro_folder.chmod(stat.S_IRUSR | stat.S_IXUSR)

        app = create_app(settings)
        app.state.db.initialize()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.patch(
                    "/api/settings",
                    json={
                        "stage1_scratch_mode": "storage_folder",
                        "stage1_scratch_storage_folder": "ro-temp",
                    },
                )
                assert res.status_code == 400
                assert "read-only" in res.json()["detail"].lower()
        finally:
            ro_folder.chmod(stat.S_IRWXU)
            app.state.library_broker.close()


def test_frontend_scratch_ui_landmarks() -> None:
    """Ensure Settings UI wires live telemetry badges, SSD/tmpfs guidance, and folder picker."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    constants_js = (STATIC_DIR / "js" / "constants.js").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")

    assert "Stage 1 Scratch Directory" in html
    assert "selectStage1ScratchMode" in html
    assert "openScratchStorageFolderPicker" in html
    assert "formatScratchMountBadge" in html
    assert "formatScratchFreeSpace" in html
    assert "--tmpfs /tmp:size=4g" in html

    assert 'id: "storage_folder"' in constants_js
    assert "stage1_scratch_storage_folder: null" in constants_js
    assert 'this.folderPickerPurpose === "scratch_storage_folder"' in app_js
    assert "RAM (tmpfs) · Zero SSD wear" in app_js
