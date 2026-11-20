"""Unit tests for app.services.health_service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConstants, Settings
from app.models.schemas import ToolCheckStatus
from app.services.health_service import (
    check_binary_version,
    check_data_dir_writable,
    run_health_check,
)


def _make_fake_process(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> MagicMock:
    """Create a mock asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestCheckBinaryVersion:
    """Verify subprocess execution and error handling for binary probes."""

    async def test_returns_available_and_first_line_on_success(self) -> None:
        fake_proc = _make_fake_process(
            returncode=0,
            stdout=b"mkvmerge v74.0.0 ('You Oughta Know') 64-bit\nmore details\n",
        )
        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            result = await check_binary_version("mkvmerge", "--version", 5.0)

        assert result.available is True
        assert result.version == "mkvmerge v74.0.0 ('You Oughta Know') 64-bit"
        assert result.error is None

    async def test_falls_back_to_stderr_when_stdout_is_empty(self) -> None:
        fake_proc = _make_fake_process(
            returncode=0,
            stdout=b"\n  \n",
            stderr=b"ffmpeg version 6.1\n",
        )
        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            result = await check_binary_version("ffmpeg", "-version", 5.0)

        assert result.available is True
        assert result.version == "ffmpeg version 6.1"

    async def test_handles_missing_binary(self) -> None:
        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("not found")),
        ):
            result = await check_binary_version("mkvmerge", "--version", 5.0)

        assert result.available is False
        assert result.version is None
        assert "not found" in (result.error or "")

    async def test_handles_os_error_on_spawn(self) -> None:
        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=PermissionError("permission denied")),
        ):
            result = await check_binary_version("ffmpeg", "-version", 5.0)

        assert result.available is False
        assert "Failed to launch ffmpeg" in (result.error or "")

    async def test_handles_non_zero_exit_code(self) -> None:
        fake_proc = _make_fake_process(
            returncode=2,
            stderr=b"fatal: broken library\n",
        )
        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            result = await check_binary_version("ffprobe", "-version", 5.0)

        assert result.available is False
        assert "exited with code 2" in (result.error or "")
        assert "fatal: broken library" in (result.error or "")

    async def test_handles_timeout_and_kills_process(self) -> None:
        fake_proc = _make_fake_process()

        async def _hang() -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        fake_proc.communicate = AsyncMock(side_effect=_hang)

        with patch(
            "app.services.health_service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            result = await check_binary_version("mkvmerge", "--version", 0.05)

        assert result.available is False
        assert "timed out" in (result.error or "")
        fake_proc.kill.assert_called_once()
        fake_proc.wait.assert_awaited_once()


class TestCheckDataDirWritable:
    """Verify write-access probe and cleanup behavior."""

    def test_succeeds_and_cleans_up_probe_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "data"

        result = check_data_dir_writable(target)

        assert result.writable is True
        assert result.error is None
        assert result.path == str(target)
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_fails_when_path_is_a_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not_a_dir"
        file_path.write_text("content", encoding="utf-8")

        result = check_data_dir_writable(file_path)

        assert result.writable is False
        assert "not a directory" in (result.error or "")

    def test_fails_when_mkdir_raises_os_error(self, tmp_path: Path) -> None:
        target = tmp_path / "blocked"
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            result = check_data_dir_writable(target)

        assert result.writable is False
        assert "Cannot create data directory" in (result.error or "")

    def test_fails_when_write_raises_os_error(self, tmp_path: Path) -> None:
        target = tmp_path / "readonly"
        target.mkdir()
        with patch.object(Path, "write_bytes", side_effect=PermissionError("ro fs")):
            result = check_data_dir_writable(target)

        assert result.writable is False
        assert "not writable" in (result.error or "")


class TestRunHealthCheck:
    """Verify overall health aggregation logic."""

    async def test_returns_ok_when_all_tools_and_dir_are_ready(
        self, test_settings: Settings
    ) -> None:
        ok_status = ToolCheckStatus(available=True, version="v1.0")
        with patch(
            "app.services.health_service.check_binary_version",
            new=AsyncMock(return_value=ok_status),
        ):
            report = await run_health_check(test_settings)

        assert report.status == AppConstants.STATUS_OK
        assert report.tools.mkvmerge.available is True
        assert report.tools.ffmpeg.available is True
        assert report.tools.ffprobe.available is True
        assert report.data_dir.writable is True

    async def test_returns_degraded_when_any_tool_is_missing(
        self, test_settings: Settings
    ) -> None:
        async def _fake_check(
            binary: str, _flag: str, _timeout: float
        ) -> ToolCheckStatus:
            if binary == AppConstants.TOOL_MKVMERGE:
                return ToolCheckStatus(available=False, error="missing")
            return ToolCheckStatus(available=True, version="v6.1")

        with patch(
            "app.services.health_service.check_binary_version",
            new=AsyncMock(side_effect=_fake_check),
        ):
            report = await run_health_check(test_settings)

        assert report.status == AppConstants.STATUS_DEGRADED
        assert report.tools.mkvmerge.available is False
        assert report.tools.ffmpeg.available is True

    async def test_returns_degraded_when_data_dir_is_not_writable(
        self, tmp_path: Path, project_root: Path
    ) -> None:
        file_as_dir = tmp_path / "file_blocking_dir"
        file_as_dir.write_text("x", encoding="utf-8")
        settings = Settings(
            storage_dir=tmp_path,
            data_dir=file_as_dir,
            static_dir=project_root / "app" / "static",
            health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
            scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
            log_level=AppConstants.DEFAULT_LOG_LEVEL,
        )
        ok_status = ToolCheckStatus(available=True, version="v1.0")
        with patch(
            "app.services.health_service.check_binary_version",
            new=AsyncMock(return_value=ok_status),
        ):
            report = await run_health_check(settings)

        assert report.status == AppConstants.STATUS_DEGRADED
        assert report.data_dir.writable is False
