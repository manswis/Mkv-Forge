"""Health check service verifying media binaries and data directory access."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.config import AppConstants, Settings
from app.models.schemas import (
    DataDirCheckStatus,
    HealthResponse,
    HealthToolsReport,
    ToolCheckStatus,
)


def _extract_first_line(raw_bytes: bytes) -> str | None:
    """Decode subprocess output and return the first non-empty trimmed line."""
    decoded = raw_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace")
    for line in decoded.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[: AppConstants.MAX_ERROR_EXCERPT_LEN]
    return None


async def check_binary_version(
    binary: str,
    version_flag: str,
    timeout_sec: float,
) -> ToolCheckStatus:
    """Run `<binary> <version_flag>` asynchronously and report availability."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            version_flag,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ToolCheckStatus(
            available=False,
            error=f"{binary} executable not found in PATH",
        )
    except OSError as exc:
        return ToolCheckStatus(
            available=False,
            error=f"Failed to launch {binary}: {exc}",
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return ToolCheckStatus(
            available=False,
            error=f"{binary} timed out after {timeout_sec}s",
        )

    if proc.returncode != AppConstants.EXIT_CODE_SUCCESS:
        detail = _extract_first_line(stderr_bytes) or _extract_first_line(stdout_bytes)
        error_msg = f"{binary} exited with code {proc.returncode}"
        if detail:
            error_msg = f"{error_msg}: {detail}"
        return ToolCheckStatus(available=False, error=error_msg)

    version_line = _extract_first_line(stdout_bytes) or _extract_first_line(
        stderr_bytes
    )
    return ToolCheckStatus(available=True, version=version_line)


def check_data_dir_writable(data_dir: Path) -> DataDirCheckStatus:
    """Verify the data directory exists (or can be created) and accepts writes."""
    if data_dir.exists() and not data_dir.is_dir():
        return DataDirCheckStatus(
            path=str(data_dir),
            writable=False,
            error="Data directory path exists but is not a directory",
        )

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DataDirCheckStatus(
            path=str(data_dir),
            writable=False,
            error=f"Cannot create data directory: {exc}",
        )

    probe_file = data_dir / f"{AppConstants.HEALTH_PROBE_PREFIX}{uuid.uuid4().hex}"
    try:
        probe_file.write_bytes(AppConstants.HEALTH_PROBE_PAYLOAD)
        return DataDirCheckStatus(path=str(data_dir), writable=True)
    except OSError as exc:
        return DataDirCheckStatus(
            path=str(data_dir),
            writable=False,
            error=f"Data directory is not writable: {exc}",
        )
    finally:
        try:
            if probe_file.exists():
                probe_file.unlink()
        except OSError:
            pass


async def run_health_check(settings: Settings) -> HealthResponse:
    """Execute all runtime readiness checks concurrently."""
    mkvmerge_status, ffmpeg_status, ffprobe_status = await asyncio.gather(
        check_binary_version(
            AppConstants.TOOL_MKVMERGE,
            AppConstants.VERSION_FLAG_DOUBLE_DASH,
            settings.health_timeout_sec,
        ),
        check_binary_version(
            AppConstants.TOOL_FFMPEG,
            AppConstants.VERSION_FLAG_SINGLE_DASH,
            settings.health_timeout_sec,
        ),
        check_binary_version(
            AppConstants.TOOL_FFPROBE,
            AppConstants.VERSION_FLAG_SINGLE_DASH,
            settings.health_timeout_sec,
        ),
    )

    data_dir_status = check_data_dir_writable(settings.data_dir)
    all_healthy = (
        mkvmerge_status.available
        and ffmpeg_status.available
        and ffprobe_status.available
        and data_dir_status.writable
    )

    return HealthResponse(
        status=(
            AppConstants.STATUS_OK
            if all_healthy
            else AppConstants.STATUS_DEGRADED
        ),
        app=AppConstants.APP_NAME,
        version=AppConstants.APP_VERSION,
        tools=HealthToolsReport(
            mkvmerge=mkvmerge_status,
            ffmpeg=ffmpeg_status,
            ffprobe=ffprobe_status,
        ),
        data_dir=data_dir_status,
    )
