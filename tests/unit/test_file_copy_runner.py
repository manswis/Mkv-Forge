"""Exhaustive unit tests for chunked file copy runner, schemas, and job summaries."""

from __future__ import annotations

import errno
import json
import os
import shutil
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.models.schemas import JobConfig
from app.services.file_copy_runner import (
    check_copy_disk_space,
    ensure_copy_destination_writable,
    execute_file_copy,
    format_eta_seconds,
    format_transfer_speed,
    resolve_copy_destination_dir,
    resolve_copy_destination_path,
)
from app.services.file_ops_service import (
    FileOpConflictError,
    FileOpError,
    FileOpNotFoundError,
    FileOpPermissionError,
)
from app.services.folder_sync import FolderReconcileCache
from app.services.job_summary import summarize_job_changes
from app.services.library_events import LibraryEventBroker
from app.services.mkv_runner import JobCancelledError
from app.services.preflight import InsufficientDiskSpaceError
from app.utils.paths import PathSafetyError

_FakeUsage = namedtuple("_FakeUsage", ["total", "used", "free"])


def _seed_media_row(
    media_repo: MediaFileRepository,
    rel_filepath: str,
    size_bytes: int,
    mtime: float,
) -> dict:
    p = Path(rel_filepath)
    folder = f"/{p.parent.as_posix()}" if p.parent.as_posix() != "." else "/"
    fid = media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_filepath,
            folder=folder,
            filename=p.name,
            size_bytes=size_bytes,
            mtime=mtime,
            duration_us=7_200_000_000,
            resolution="1080p",
            video_codec="HEVC",
            video_dynamic_range="HDR10",
            audio_summary_json='[{"codec":"EAC3","channels":6,"language":"eng"}]',
            subtitle_count=2,
            container_title="Sample Movie",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json='{"tracks": []}',
            scanned_at=utc_now_iso(),
        )
    )
    row = media_repo.get_by_id(fid)
    assert row is not None
    return row


class TestCopyFormattingAndHelpers:
    """Test pure speed/ETA formatting, path resolution, writeability, and disk checks."""

    def test_format_transfer_speed_tiers_and_non_positive(self) -> None:
        assert format_transfer_speed(0) is None
        assert format_transfer_speed(-10.0) is None
        assert format_transfer_speed(512.0) == "512 B/s"
        assert format_transfer_speed(2048.0) == "2 KB/s"
        assert format_transfer_speed(50.5 * 1024 * 1024) == "50.5 MB/s"
        assert format_transfer_speed(1.25 * 1024 * 1024 * 1024) == "1.25 GB/s"

    def test_format_eta_seconds_all_ranges(self) -> None:
        assert format_eta_seconds(None) is None
        assert format_eta_seconds(-5.0) is None
        assert format_eta_seconds(0.0) == "0s"
        assert format_eta_seconds(42.4) == "42s"
        assert format_eta_seconds(125.0) == "2m 5s"
        assert format_eta_seconds(3720.0) == "1h 2m"

    def test_resolve_copy_destination_dir_valid_missing_and_traversal(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        sub = storage / "Backup"
        sub.mkdir(parents=True)

        root_dir, root_norm = resolve_copy_destination_dir("/", storage)
        assert root_dir == storage.resolve()
        assert root_norm == "/"

        sub_dir, sub_norm = resolve_copy_destination_dir("/Backup", storage)
        assert sub_dir == sub.resolve()
        assert sub_norm == "/Backup"

        with pytest.raises(FileOpNotFoundError):
            resolve_copy_destination_dir("/DoesNotExist", storage)

        with pytest.raises(PathSafetyError):
            resolve_copy_destination_dir("/../escape", storage)

    def test_resolve_copy_destination_path_cross_folder_same_folder_and_exhausted(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        src_dir = storage / "Movies"
        dst_dir = storage / "Backup"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)

        src = src_dir / "Film.mkv"
        src.write_bytes(b"data")

        # 1. Cross-folder with no collision preserves exact filename
        assert resolve_copy_destination_path(src, dst_dir, storage) == (
            dst_dir / "Film.mkv"
        ).resolve()

        # 2. Cross-folder WITH collision allocates (1), then (2)
        (dst_dir / "Film.mkv").write_bytes(b"existing")
        assert resolve_copy_destination_path(src, dst_dir, storage) == (
            dst_dir / "Film (1).mkv"
        ).resolve()
        (dst_dir / "Film (1).mkv").write_bytes(b"existing-1")
        assert resolve_copy_destination_path(src, dst_dir, storage) == (
            dst_dir / "Film (2).mkv"
        ).resolve()

        # 3. Same folder allocates (1), and copying Film (1).mkv increments cleanly to (2)
        dup1 = resolve_copy_destination_path(src, src_dir, storage)
        assert dup1 == (src_dir / "Film (1).mkv").resolve()
        dup1.write_bytes(b"dup1")
        dup2 = resolve_copy_destination_path(dup1, src_dir, storage)
        assert dup2 == (src_dir / "Film (2).mkv").resolve()

        # 4. Exhausted collision counter raises FileOpConflictError
        with patch.object(AppConstants, "MAX_DUPLICATE_COLLISIONS", 1):
            with pytest.raises(FileOpConflictError):
                resolve_copy_destination_path(src, src_dir, storage)

    def test_ensure_copy_destination_writable_success_and_errors(
        self, tmp_path: Path
    ) -> None:
        ensure_copy_destination_writable(tmp_path)
        assert list(tmp_path.iterdir()) == []

        with patch(
            "builtins.open",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            with pytest.raises(FileOpPermissionError):
                ensure_copy_destination_writable(tmp_path)

        with patch(
            "builtins.open",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            with pytest.raises(FileOpError):
                ensure_copy_destination_writable(tmp_path)

        # Cleanup failure in finally block is safely swallowed
        real_open = open
        with (
            patch("builtins.open", side_effect=real_open),
            patch.object(Path, "unlink", side_effect=OSError(errno.EIO, "io")),
        ):
            ensure_copy_destination_writable(tmp_path)

    def test_check_copy_disk_space_bounds_and_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            check_copy_disk_space(-1, tmp_path)

        with patch.object(
            shutil,
            "disk_usage",
            return_value=_FakeUsage(10_000, 1_000, 9_000),
        ):
            assert check_copy_disk_space(5_000, tmp_path, margin_bytes=1_000) == 9_000
            with pytest.raises(InsufficientDiskSpaceError):
                check_copy_disk_space(8_500, tmp_path, margin_bytes=1_000)

        with patch.object(shutil, "disk_usage", side_effect=OSError("statvfs failed")):
            with pytest.raises(FileOpError):
                check_copy_disk_space(100, tmp_path, margin_bytes=0)


class TestJobConfigAndSummaryForCopy:
    """Test copy operation validation in JobConfig and human-readable job summary."""

    def test_job_config_copy_validation(self) -> None:
        cfg = JobConfig(operation="copy", copy_destination_folder="/Backup")
        assert cfg.operation == "copy"
        assert cfg.copy_destination_folder == "/Backup"

        with pytest.raises(ValidationError):
            JobConfig(operation="copy", copy_destination_folder=None)

        with pytest.raises(ValidationError):
            JobConfig(operation="copy", copy_destination_folder="   ")

        # Existing remux defaults remain completely unaffected
        remux_cfg = JobConfig()
        assert remux_cfg.operation == "remux"

    def test_summarize_job_changes_for_copy_jobs(self) -> None:
        cfg_json = json.dumps(
            {"operation": "copy", "copy_destination_folder": "/Backup"}
        )
        mode_queued, changes_queued = summarize_job_changes(
            cfg_json, filepath="Movies/Film.mkv", output_filepath=None
        )
        assert mode_queued == "copy"
        assert len(changes_queued) == 1
        assert changes_queued[0].label == "Copy → /Backup"

        mode_done, changes_done = summarize_job_changes(
            cfg_json,
            filepath="Movies/Film.mkv",
            output_filepath="Backup/Film.mkv",
        )
        assert mode_done == "copy"
        assert changes_done[0].label == "Copied → /Backup/Film.mkv"

        root_json = json.dumps({"operation": "copy", "copy_destination_folder": "/"})
        _, changes_root = summarize_job_changes(
            root_json, filepath="Movies/Film.mkv", output_filepath=None
        )
        assert changes_root[0].label == "Copy → /"

        empty_dest_json = json.dumps({"operation": "copy"})
        _, changes_empty = summarize_job_changes(
            empty_dest_json, filepath="Movies/Film.mkv", output_filepath=None
        )
        assert changes_empty[0].label == "Copy → /"


class TestExecuteFileCopy:
    """Test end-to-end chunked execution, progress, cancellation, and edge cases."""

    @pytest.fixture()
    def copy_env(self, tmp_path: Path):
        storage = tmp_path / "storage"
        (storage / "Movies").mkdir(parents=True)
        (storage / "Backup").mkdir(parents=True)
        db = DatabaseManager(tmp_path / "test.db")
        db.initialize()
        media_repo = MediaFileRepository(db)
        return storage, media_repo

    @pytest.mark.asyncio
    async def test_cross_folder_chunked_copy_progress_and_indexing(
        self, copy_env
    ) -> None:
        storage, media_repo = copy_env
        src = storage / "Movies" / "Demo.mkv"
        payload = b"A" * (AppConstants.FILE_COPY_MIN_CHUNK_BYTES * 3 + 1234)
        src.write_bytes(payload)
        st = src.stat()
        media_row = _seed_media_row(media_repo, "Movies/Demo.mkv", st.st_size, st.st_mtime)

        stages: list[tuple[str, str]] = []
        progress_ticks: list[tuple[int, str | None, str | None]] = []
        logs: list[str] = []
        broker_paths: list[str] = []
        broker = LibraryEventBroker(on_change=broker_paths.append)
        cache = FolderReconcileCache()

        result = await execute_file_copy(
            src,
            destination_folder="/Backup",
            storage_root=storage,
            media_repo=media_repo,
            media_row=media_row,
            folder_cache=cache,
            library_broker=broker,
            on_stage=lambda s, lbl: stages.append((s, lbl)),
            on_progress=lambda pct, spd, eta: progress_ticks.append((pct, spd, eta)),
            on_log=lambda line: logs.append(line),
            chunk_bytes=AppConstants.FILE_COPY_MIN_CHUNK_BYTES,
            emit_interval_sec=0.0,
            disk_margin_bytes=0,
        )

        # Allow any loop.call_soon_threadsafe progress callbacks to flush
        await __import__("asyncio").sleep(0.01)

        assert result.output_rel_filepath == "Backup/Demo.mkv"
        assert result.output_path.read_bytes() == payload
        assert stages == [(AppConstants.JOB_STAGE_COPYING, "Copying to /Backup")]
        assert len(progress_ticks) >= 2
        assert progress_ticks[-1][0] == 100
        assert any("Copying Demo.mkv" in line for line in logs)
        assert any("Copied Demo.mkv -> /Backup/Demo.mkv" in line for line in logs)
        assert not any(
            ".mkvforge." in p.name for p in (storage / "Backup").iterdir()
        )

        # Cloned SQLite row preserves source metadata without re-probing
        indexed = media_repo.get_by_filepath("Backup/Demo.mkv")
        assert indexed is not None
        assert indexed["video_codec"] == "HEVC"
        assert indexed["resolution"] == "1080p"
        assert indexed["duration_us"] == 7_200_000_000
        assert indexed["folder"] == "/Backup"
        assert broker_paths == ["Backup/Demo.mkv"]

    @pytest.mark.asyncio
    async def test_zero_byte_file_copy_and_scanner_fallback(self, copy_env) -> None:
        storage, media_repo = copy_env
        src = storage / "Movies" / "Empty.mkv"
        src.write_bytes(b"")

        progress_ticks: list[tuple[int, str | None, str | None]] = []

        async def _fake_scan_single(path: Path) -> None:
            st = path.stat()
            _seed_media_row(media_repo, "Backup/Empty.mkv", st.st_size, st.st_mtime)

        scanner = MagicMock()
        scanner.scan_single_file = AsyncMock(side_effect=_fake_scan_single)

        result = await execute_file_copy(
            src,
            destination_folder="/Backup",
            storage_root=storage,
            media_repo=media_repo,
            media_row=None,
            scanner=scanner,
            on_progress=lambda pct, spd, eta: progress_ticks.append((pct, spd, eta)),
            disk_margin_bytes=0,
        )
        await __import__("asyncio").sleep(0.01)

        assert result.output_rel_filepath == "Backup/Empty.mkv"
        assert result.output_path.read_bytes() == b""
        assert progress_ticks == [(100, None, "0s")]
        scanner.scan_single_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancellation_before_start_zero_byte_and_mid_copy_cleans_temp(
        self, copy_env
    ) -> None:
        storage, media_repo = copy_env
        src = storage / "Movies" / "Big.mkv"
        src.write_bytes(b"X" * (AppConstants.FILE_COPY_MIN_CHUNK_BYTES * 3))

        # 1. Pre-start cancel
        with pytest.raises(JobCancelledError):
            await execute_file_copy(
                src,
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                is_cancelled=lambda: True,
                disk_margin_bytes=0,
            )

        # 2. Mid-copy cancel on chunk 2 removes partial temp file
        calls = 0

        def _cancel_after_first_chunk() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 3

        logs: list[str] = []
        with pytest.raises(JobCancelledError):
            await execute_file_copy(
                src,
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                on_log=logs.append,
                is_cancelled=_cancel_after_first_chunk,
                chunk_bytes=AppConstants.FILE_COPY_MIN_CHUNK_BYTES,
                emit_interval_sec=0.0,
                disk_margin_bytes=0,
            )
        assert list((storage / "Backup").iterdir()) == []
        assert any("cancelled" in line.lower() for line in logs)

        # 3. Cancel on zero-byte file inside sync helper
        zero_src = storage / "Movies" / "Zero.mkv"
        zero_src.write_bytes(b"")
        zero_calls = 0

        def _cancel_inside_zero() -> bool:
            nonlocal zero_calls
            zero_calls += 1
            return zero_calls >= 2

        with pytest.raises(JobCancelledError):
            await execute_file_copy(
                zero_src,
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                is_cancelled=_cancel_inside_zero,
                disk_margin_bytes=0,
            )
        assert list((storage / "Backup").iterdir()) == []

        # 4. Cancel immediately after final read before finalize
        small = storage / "Movies" / "Small.mkv"
        small.write_bytes(b"hello")
        post_calls = 0

        def _cancel_post_loop() -> bool:
            nonlocal post_calls
            post_calls += 1
            return post_calls >= 4

        with pytest.raises(JobCancelledError):
            await execute_file_copy(
                small,
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                is_cancelled=_cancel_post_loop,
                disk_margin_bytes=0,
            )
        assert list((storage / "Backup").iterdir()) == []

    @pytest.mark.asyncio
    async def test_missing_source_mid_copy_collision_copystat_and_os_error(
        self, copy_env
    ) -> None:
        storage, media_repo = copy_env

        # 1. Missing source file
        with pytest.raises(FileOpNotFoundError):
            await execute_file_copy(
                storage / "Movies" / "Ghost.mkv",
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                disk_margin_bytes=0,
            )

        # 2. Collision appearing mid-copy + copystat OSError swallowed cleanly
        src = storage / "Movies" / "Race.mkv"
        src.write_bytes(b"payload-bytes")
        raced_target = storage / "Backup" / "Race.mkv"

        def _inject_collision_on_copystat(_s: str, _d: str) -> None:
            raced_target.write_bytes(b"concurrently-arrived")
            raise OSError(errno.EPERM, "SMB copystat unsupported")

        with patch.object(shutil, "copystat", side_effect=_inject_collision_on_copystat):
            result = await execute_file_copy(
                src,
                destination_folder="/Backup",
                storage_root=storage,
                media_repo=media_repo,
                disk_margin_bytes=0,
            )

        assert result.output_rel_filepath == "Backup/Race (1).mkv"
        assert raced_target.read_bytes() == b"concurrently-arrived"
        assert result.output_path.read_bytes() == b"payload-bytes"

        # 3. Unexpected OSError during chunk write cleans up temp and raises FileOpError
        with patch(
            "app.services.file_copy_runner._copy_file_chunked_sync",
            side_effect=OSError(errno.EIO, "disk I/O failure"),
        ):
            with pytest.raises(FileOpError, match="Failed to copy"):
                await execute_file_copy(
                    src,
                    destination_folder="/Backup",
                    storage_root=storage,
                    media_repo=media_repo,
                    disk_margin_bytes=0,
                )

        # 4. EROFS during copy maps to FileOpPermissionError
        with patch(
            "app.services.file_copy_runner._copy_file_chunked_sync",
            side_effect=OSError(errno.EROFS, "read-only"),
        ):
            with pytest.raises(FileOpPermissionError):
                await execute_file_copy(
                    src,
                    destination_folder="/Backup",
                    storage_root=storage,
                    media_repo=media_repo,
                    disk_margin_bytes=0,
                )

        # 5. Temp unlink OSError in finally is swallowed safely
        def _create_tmp_then_boom(_src: Path, tmp_p: Path, **_kw) -> None:
            tmp_p.write_bytes(b"partial")
            raise OSError(errno.EIO, "boom")

        real_unlink = Path.unlink

        def _fail_copy_tmp_unlink(self: Path, *args, **kwargs) -> None:
            if ".mkvforge.copy-" in self.name:
                raise OSError(errno.EIO, "unlink fail")
            real_unlink(self, *args, **kwargs)

        with (
            patch(
                "app.services.file_copy_runner._copy_file_chunked_sync",
                side_effect=_create_tmp_then_boom,
            ),
            patch.object(Path, "unlink", _fail_copy_tmp_unlink),
        ):
            with pytest.raises(FileOpError):
                await execute_file_copy(
                    src,
                    destination_folder="/Backup",
                    storage_root=storage,
                    media_repo=media_repo,
                    disk_margin_bytes=0,
                )
