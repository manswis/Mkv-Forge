"""Integration tests for app.services.scanner and run_mkvmerge_probe."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, MediaFileRepository
from app.services.scanner import LibraryScanner, ProbeError, run_mkvmerge_probe
from app.utils.paths import PathSafetyError


def _fake_probe_dict(title: str = "", has_eac3: bool = False) -> dict:
    audio_tracks = [
        {
            "id": 1,
            "type": "audio",
            "codec": "AAC",
            "properties": {"audio_channels": 6, "language": "eng"},
        }
    ]
    if has_eac3:
        audio_tracks.append(
            {
                "id": 2,
                "type": "audio",
                "codec": "E-AC-3",
                "properties": {"audio_channels": 6, "language": "eng"},
            }
        )
    return {
        "container": {
            "properties": {
                "duration": 5_000_000_000_000,
                "title": title,
            }
        },
        "global_tags": [],
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "HEVC",
                "properties": {"pixel_dimensions": "1920x1080"},
            },
            *audio_tracks,
        ],
    }


def _make_fake_proc(
    returncode: int = 0,
    stdout: bytes = b"{}",
    stderr: bytes = b"",
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestRunMkvmergeProbe:
    """Verify subprocess execution, timeout kill, and JSON validation."""

    async def test_returns_parsed_dict_and_raw_json(self, tmp_path: Path) -> None:
        payload = _fake_probe_dict(title="Test")
        raw = json.dumps(payload).encode("utf-8")
        with patch(
            "app.services.scanner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_fake_proc(0, stdout=raw)),
        ):
            parsed, raw_text = await run_mkvmerge_probe(tmp_path / "a.mkv", 5.0)

        assert parsed["container"]["properties"]["title"] == "Test"
        assert json.loads(raw_text) == payload

    async def test_raises_probe_error_on_missing_binary(self, tmp_path: Path) -> None:
        with patch(
            "app.services.scanner.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError),
        ):
            with pytest.raises(ProbeError, match="not found"):
                await run_mkvmerge_probe(tmp_path / "a.mkv", 5.0)

    async def test_raises_probe_error_on_nonzero_exit(self, tmp_path: Path) -> None:
        with patch(
            "app.services.scanner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_fake_proc(2, stderr=b"corrupt mkv")),
        ):
            with pytest.raises(ProbeError, match="exit code 2"):
                await run_mkvmerge_probe(tmp_path / "a.mkv", 5.0)

    async def test_raises_probe_error_on_timeout_and_kills_proc(
        self, tmp_path: Path
    ) -> None:
        fake_proc = _make_fake_proc()

        async def _hang() -> tuple[bytes, bytes]:
            await asyncio.sleep(5)
            return b"", b""

        fake_proc.communicate = AsyncMock(side_effect=_hang)
        with patch(
            "app.services.scanner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            with pytest.raises(ProbeError, match="timed out"):
                await run_mkvmerge_probe(tmp_path / "a.mkv", 0.05)

        fake_proc.kill.assert_called_once()
        fake_proc.wait.assert_awaited_once()

    async def test_raises_probe_error_on_invalid_json(self, tmp_path: Path) -> None:
        with patch(
            "app.services.scanner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_fake_proc(0, stdout=b"{bad json")),
        ):
            with pytest.raises(ProbeError, match="Invalid JSON"):
                await run_mkvmerge_probe(tmp_path / "a.mkv", 5.0)


class TestLibraryScanner:
    """Verify incremental skip, full rescan, deleted cleanup, corrupt skip, and single-file probe."""

    async def test_incremental_skip_full_rescan_and_deleted_cleanup(
        self, test_settings: Settings, db_manager: DatabaseManager, tmp_path: Path
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        mkv_file = movies / "Dune.mkv"
        mp4_file = movies / "Clip.mp4"
        mkv_file.write_bytes(b"mkv-content")
        mp4_file.write_bytes(b"mp4-content")

        # Noise files that must be ignored
        (movies / "Dune.mkv.mkvforge.tmp").write_bytes(b"tmp")
        (movies / "Dune.mkv.mkvforge.eac3").write_bytes(b"eac3")
        (movies / "._Dune.mkv").write_bytes(b"appledouble")
        (movies / "Dune.srt").write_text("sub", encoding="utf-8")

        # Symlink escaping storage_dir must be ignored
        outside_file = tmp_path / "outside.mkv"
        outside_file.write_bytes(b"secret")
        (movies / "escape.mkv").symlink_to(outside_file)

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)

        probe_mock = AsyncMock(
            side_effect=lambda path, _t: (_fake_probe_dict(title=path.stem), json.dumps(_fake_probe_dict(title=path.stem)))
        )

        with patch("app.services.scanner.run_mkvmerge_probe", new=probe_mock):
            # 1. Initial scan indexes 2 valid media files
            started, _ = scanner.start_scan(full=False)
            assert started is True
            await scanner.wait_for_scan()

            assert scanner.current_progress.status == AppConstants.SCAN_STATUS_COMPLETED
            assert scanner.current_progress.total == 2
            assert scanner.current_progress.scanned == 2
            assert scanner.current_progress.skipped == 0
            assert probe_mock.await_count == 2
            first_scan_at = state_repo.get(AppConstants.STATE_KEY_LAST_SCAN_AT)
            assert first_scan_at is not None

            # 2. Second incremental scan skips both unchanged files and STILL updates last_scan_at
            probe_mock.reset_mock()
            started, _ = scanner.start_scan(full=False)
            assert started is True
            await scanner.wait_for_scan()

            assert scanner.current_progress.total == 2
            assert scanner.current_progress.skipped == 2
            assert probe_mock.await_count == 0
            second_scan_at = state_repo.get(AppConstants.STATE_KEY_LAST_SCAN_AT)
            assert second_scan_at is not None
            assert second_scan_at >= first_scan_at

            # 3. Full rescan re-probes all files regardless of unchanged mtime/size
            probe_mock.reset_mock()
            scanner.start_scan(full=True)
            await scanner.wait_for_scan()
            assert scanner.current_progress.skipped == 0
            assert probe_mock.await_count == 2

            # 4. Delete mp4_file from disk -> next scan removes its DB row
            mp4_file.unlink()
            probe_mock.reset_mock()
            scanner.start_scan(full=False)
            await scanner.wait_for_scan()

            assert scanner.current_progress.total == 1
            assert scanner.current_progress.skipped == 1
            assert media_repo.get_by_filepath("movies/Clip.mp4") is None
            assert media_repo.get_by_filepath("movies/Dune.mkv") is not None

    async def test_corrupt_file_is_skipped_without_aborting_scan(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        bad = test_settings.storage_dir / "bad.mkv"
        good = test_settings.storage_dir / "good.mkv"
        bad.write_bytes(b"corrupt")
        good.write_bytes(b"valid")

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)

        async def _flaky_probe(path: Path, _timeout: float):
            if path.name == "bad.mkv":
                raise ProbeError("corrupt file")
            data = _fake_probe_dict(title="Good")
            return data, json.dumps(data)

        with patch("app.services.scanner.run_mkvmerge_probe", new=AsyncMock(side_effect=_flaky_probe)):
            scanner.start_scan(full=False)
            await scanner.wait_for_scan()

        assert scanner.current_progress.status == AppConstants.SCAN_STATUS_COMPLETED
        assert scanner.current_progress.total == 2
        assert scanner.current_progress.scanned == 2
        assert scanner.current_progress.errors == 1
        bad_row = media_repo.get_by_filepath("bad.mkv")
        assert bad_row is not None
        assert bad_row["scan_status"] == AppConstants.FILE_SCAN_STATUS_ERROR
        assert media_repo.get_by_filepath("good.mkv") is not None

    async def test_missing_storage_dir_fails_without_wiping_cache(
        self, tmp_path: Path, project_root: Path, db_manager: DatabaseManager
    ) -> None:
        missing_storage = tmp_path / "unmounted_usb"
        settings = Settings(
            storage_dir=missing_storage,
            data_dir=db_manager.db_path.parent,
            static_dir=project_root / "app" / "static",
            health_timeout_sec=5.0,
            scan_timeout_sec=5.0,
            log_level="INFO",
        )
        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(settings, media_repo, state_repo)

        scanner.start_scan(full=False)
        await scanner.wait_for_scan()

        assert scanner.current_progress.status == AppConstants.SCAN_STATUS_FAILED
        assert "does not exist" in (scanner.current_progress.error_message or "")

    async def test_probe_single_file_refreshes_and_handles_removal(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        target = test_settings.storage_dir / "movies" / "Solo.mkv"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"v1")

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)

        data_v1 = _fake_probe_dict(title="Old", has_eac3=False)
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(return_value=(data_v1, json.dumps(data_v1))),
        ):
            row = await scanner.probe_single_file("movies/Solo.mkv")
        assert row is not None
        assert bool(row["has_ac3_eac3"]) is False

        # Modify file and re-probe with EAC3 present
        target.write_bytes(b"v2-updated")
        os.utime(target, None)
        data_v2 = _fake_probe_dict(title="", has_eac3=True)
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(return_value=(data_v2, json.dumps(data_v2))),
        ):
            updated = await scanner.probe_single_file("movies/Solo.mkv")
        assert updated is not None
        assert updated["id"] == row["id"]
        assert bool(updated["has_ac3_eac3"]) is True
        assert updated["container_title"] == ""

        # Delete file from disk -> probe_single_file removes DB entry and returns None
        target.unlink()
        removed = await scanner.probe_single_file("movies/Solo.mkv")
        assert removed is None
        assert media_repo.get_by_id(row["id"]) is None

        # Path traversal is blocked
        with pytest.raises(PathSafetyError):
            await scanner.probe_single_file("../../etc/passwd")
