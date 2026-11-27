"""Stateless on-demand video frame extractor for interactive chapter picking.

Implements all three Phase C2 guardrails:
1. Dual-mode seeking: fast input-seeking (`-ss` before `-i`, 320px) for timeline peeks and
   Scan Scenes grids; hybrid accurate seeking (`-ss <ts - 10s> -i <file> -ss <delta>`, 480px)
   for frame-exact `±1s`/`±5s` nudges, with lightweight `format=yuvj420p` for 10-bit HDR.
2. Real instant cancellation: bounded by `Semaphore(1)` and races `proc.communicate()`
   against `request.is_disconnected()` polling, terminating/killing `ffmpeg` immediately.
3. Zero server disk cache: streams JPEG bytes directly from `ffmpeg` stdout (`pipe:1`)
   without writing any files into `<data_dir>` or `/storage`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from app.config import AppConstants
from app.services.processing_policy import (
    build_nice_preexec_fn,
    is_ionice_available,
    wrap_command_with_io_priority,
)

logger = logging.getLogger(__name__)

DisconnectChecker = Callable[[], Awaitable[bool]]

_JPEG_MAGIC: bytes = b"\xff\xd8\xff"
_MIN_JPEG_BYTES: int = 8
_IONICE_ERROR_MARKERS: tuple[str, ...] = ("ionice:", "ioprio_set")
_FRAME_SEMAPHORE = asyncio.Semaphore(AppConstants.SCENE_FRAME_MAX_CONCURRENCY)
_ionice_frame_disabled: bool = False


class SceneFrameError(RuntimeError):
    """Raised when on-demand frame extraction fails, times out, or produces invalid output."""


class SceneFrameCancelledError(RuntimeError):
    """Raised when the client disconnects before FFmpeg finishes extracting a frame."""


def clamp_frame_timestamp_ms(ts_ms: int, duration_us: int) -> int:
    """Clamp `ts_ms` into `[0, max(0, duration_ms - SCENE_FRAME_EOF_MARGIN_MS)]`.

    Prevents tapping the far end of the scrubber or nudging past EOF from yielding an
    empty 0-byte FFmpeg stream.
    """
    if ts_ms < 0:
        raise ValueError(f"Frame timestamp must be non-negative (got {ts_ms})")
    duration_ms = max(0, int(duration_us) // AppConstants.US_PER_MS)
    max_ms = max(0, duration_ms - AppConstants.SCENE_FRAME_EOF_MARGIN_MS)
    return min(int(ts_ms), max_ms)


def build_scene_frame_ffmpeg_command(
    abs_path: Path,
    *,
    ts_ms: int,
    mode: str = AppConstants.SCENE_FRAME_MODE_FAST,
) -> list[str]:
    """Build deterministic, shell-free `ffmpeg` argv streaming a single JPEG to `pipe:1`."""
    if ts_ms < 0:
        raise ValueError(f"Frame timestamp must be non-negative (got {ts_ms})")
    if mode not in AppConstants.ALLOWED_SCENE_FRAME_MODES:
        raise ValueError(
            f"Unsupported scene frame mode {mode!r}; expected one of "
            f"{AppConstants.ALLOWED_SCENE_FRAME_MODES}"
        )

    ts_sec = ts_ms / AppConstants.MS_PER_SEC
    if mode == AppConstants.SCENE_FRAME_MODE_ACCURATE:
        pre_seek_sec = max(0.0, ts_sec - AppConstants.SCENE_FRAME_ACCURATE_PRESEEK_SEC)
        post_seek_sec = max(0.0, ts_sec - pre_seek_sec)
        width_px = AppConstants.SCENE_FRAME_ACCURATE_WIDTH_PX
        quality = AppConstants.SCENE_FRAME_ACCURATE_QUALITY
        seek_args = [
            "-ss",
            f"{pre_seek_sec:.3f}",
            "-i",
            str(abs_path),
            "-ss",
            f"{post_seek_sec:.3f}",
        ]
    else:
        width_px = AppConstants.SCENE_FRAME_FAST_WIDTH_PX
        quality = AppConstants.SCENE_FRAME_FAST_QUALITY
        seek_args = [
            "-ss",
            f"{ts_sec:.3f}",
            "-i",
            str(abs_path),
        ]

    vf = (
        f"scale='min({width_px},iw)':-2,"
        f"format={AppConstants.THUMBNAIL_JPEG_PIXEL_FORMAT}"
    )
    return [
        AppConstants.TOOL_FFMPEG,
        "-v",
        "error",
        *seek_args,
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-q:v",
        str(quality),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]


def _is_ionice_wrapper_failure(stderr_bytes: bytes) -> bool:
    """Return True when stderr indicates `ionice` itself failed due to container restrictions."""
    lower = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").lower()
    return any(marker in lower for marker in _IONICE_ERROR_MARKERS)


def _disable_ionice_frame(reason: str) -> None:
    """Latch `ionice` off for future scene-frame calls and emit a single WARNING."""
    global _ionice_frame_disabled
    if not _ionice_frame_disabled:
        _ionice_frame_disabled = True
        logger.warning(
            "ionice frame wrapper unavailable (%s); disabling ionice for future scene frames",
            reason,
        )


async def _terminate_subprocess(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM with a short grace window, escalating to SIGKILL if still alive."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=AppConstants.SCENE_FRAME_TERMINATE_GRACE_SEC,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _poll_disconnect(
    is_disconnected: DisconnectChecker,
    poll_interval_sec: float,
) -> None:
    """Resolve as soon as `is_disconnected()` returns True."""
    while True:
        if await is_disconnected():
            return
        await asyncio.sleep(poll_interval_sec)


async def _run_frame_subprocess(
    argv: list[str],
    *,
    timeout_sec: float,
    preexec_fn: object | None,
    is_disconnected: DisconnectChecker | None,
    disconnect_poll_sec: float,
) -> tuple[int, bytes, bytes]:
    """Execute one FFmpeg frame command with disconnect-aware termination and timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec_fn,  # type: ignore[arg-type]
    )
    comm_task = asyncio.create_task(proc.communicate())
    disc_task: asyncio.Task[None] | None = (
        asyncio.create_task(_poll_disconnect(is_disconnected, disconnect_poll_sec))
        if is_disconnected is not None
        else None
    )
    tasks: set[asyncio.Task] = {comm_task}
    if disc_task is not None:
        tasks.add(disc_task)

    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _terminate_subprocess(proc)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise asyncio.TimeoutError

        if disc_task is not None and disc_task in done and comm_task not in done:
            await _terminate_subprocess(proc)
            comm_task.cancel()
            await asyncio.gather(comm_task, return_exceptions=True)
            raise SceneFrameCancelledError("Client disconnected during frame extraction")

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        stdout_bytes, stderr_bytes = comm_task.result()
        return int(proc.returncode or 0), stdout_bytes, stderr_bytes
    except asyncio.CancelledError:
        await _terminate_subprocess(proc)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def extract_scene_frame(
    abs_path: Path,
    *,
    ts_ms: int,
    mode: str = AppConstants.SCENE_FRAME_MODE_FAST,
    low_io_priority: bool = False,
    is_disconnected: DisconnectChecker | None = None,
    timeout_sec: float = AppConstants.SCENE_FRAME_TIMEOUT_SEC,
    disconnect_poll_sec: float = AppConstants.SCENE_FRAME_DISCONNECT_POLL_SEC,
) -> bytes:
    """Extract a single JPEG frame directly into memory (`pipe:1`) with strict guardrails."""
    if not await asyncio.to_thread(abs_path.is_file):
        raise SceneFrameError(f"Media file does not exist: {abs_path.name}")

    base_argv = build_scene_frame_ffmpeg_command(abs_path, ts_ms=ts_ms, mode=mode)
    preexec_fn = (
        build_nice_preexec_fn(AppConstants.LOW_PRIORITY_NICE_INCREMENT)
        if low_io_priority
        else None
    )

    async with _FRAME_SEMAPHORE:
        if is_disconnected is not None and await is_disconnected():
            raise SceneFrameCancelledError("Client disconnected before frame extraction")

        use_ionice = bool(
            low_io_priority and not _ionice_frame_disabled and is_ionice_available()
        )
        result: tuple[int, bytes, bytes] | None = None

        if use_ionice:
            wrapped_argv = wrap_command_with_io_priority(base_argv, use_ionice_idle=True)
            try:
                rc, out_b, err_b = await _run_frame_subprocess(
                    wrapped_argv,
                    timeout_sec=timeout_sec,
                    preexec_fn=preexec_fn,
                    is_disconnected=is_disconnected,
                    disconnect_poll_sec=disconnect_poll_sec,
                )
            except (SceneFrameCancelledError, asyncio.CancelledError):
                raise
            except asyncio.TimeoutError as exc:
                raise SceneFrameError(
                    f"Frame extraction timed out after {timeout_sec}s for {abs_path.name}"
                ) from exc
            except (FileNotFoundError, PermissionError, OSError) as exc:
                _disable_ionice_frame(str(exc))
            else:
                if rc == AppConstants.EXIT_CODE_SUCCESS or not _is_ionice_wrapper_failure(
                    err_b
                ):
                    result = (rc, out_b, err_b)
                else:
                    detail = err_b.decode(
                        AppConstants.UTF8_ENCODING, errors="replace"
                    ).strip()
                    _disable_ionice_frame(f"exit {rc}: {detail}")

        if result is None:
            try:
                result = await _run_frame_subprocess(
                    base_argv,
                    timeout_sec=timeout_sec,
                    preexec_fn=preexec_fn,
                    is_disconnected=is_disconnected,
                    disconnect_poll_sec=disconnect_poll_sec,
                )
            except (SceneFrameCancelledError, asyncio.CancelledError):
                raise
            except asyncio.TimeoutError as exc:
                raise SceneFrameError(
                    f"Frame extraction timed out after {timeout_sec}s for {abs_path.name}"
                ) from exc
            except FileNotFoundError as exc:
                raise SceneFrameError("ffmpeg executable not found in PATH") from exc
            except OSError as exc:
                raise SceneFrameError(f"Failed to launch ffmpeg: {exc}") from exc

    returncode, stdout_bytes, stderr_bytes = result
    if returncode != AppConstants.EXIT_CODE_SUCCESS:
        excerpt = stderr_bytes.decode(
            AppConstants.UTF8_ENCODING, errors="replace"
        ).strip()[: AppConstants.MAX_ERROR_EXCERPT_LEN]
        raise SceneFrameError(
            f"ffmpeg frame extraction failed with exit code {returncode}: {excerpt}"
        )

    if len(stdout_bytes) < _MIN_JPEG_BYTES or not stdout_bytes.startswith(_JPEG_MAGIC):
        raise SceneFrameError(
            f"ffmpeg did not produce a valid JPEG frame for {abs_path.name} at {ts_ms}ms"
        )

    return stdout_bytes
