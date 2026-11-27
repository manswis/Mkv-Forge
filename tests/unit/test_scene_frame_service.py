"""Exhaustive Phase C2 unit tests for stateless scene frame extraction and all 3 guardrails."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import AppConstants
import app.services.scene_frame_service as frame_mod
from app.services.scene_frame_service import (
    SceneFrameCancelledError,
    SceneFrameError,
    build_scene_frame_ffmpeg_command,
    clamp_frame_timestamp_ms,
    extract_scene_frame,
)

FAKE_JPEG = b"\xff\xd8\xff\xe0" + (b"\x00" * 64) + b"\xff\xd9"


class TestClampAndCommandBuilderGuardrail1:
    """Verify EOF clamping and dual-mode fast vs hybrid accurate FFmpeg argv (no tonemap, pipe:1)."""

    def test_clamp_frame_timestamp_ms_boundaries_and_negative_rejection(self) -> None:
        # Normal middle timestamp unchanged
        assert clamp_frame_timestamp_ms(30_000, 120_000_000) == 30_000
        # At or past EOF clamped to duration - 500ms
        assert clamp_frame_timestamp_ms(120_000, 120_000_000) == 119_500
        assert clamp_frame_timestamp_ms(999_999, 120_000_000) == 119_500
        # Very short clip (< 500ms) clamps cleanly to 0
        assert clamp_frame_timestamp_ms(400, 300_000) == 0
        assert clamp_frame_timestamp_ms(0, 0) == 0

        with pytest.raises(ValueError, match="non-negative"):
            clamp_frame_timestamp_ms(-1, 60_000_000)

    def test_build_command_fast_mode_uses_input_seek_320px_yuvj420p_and_pipe1(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "hdr10_movie.mkv"
        cmd = build_scene_frame_ffmpeg_command(src, ts_ms=125_500, mode="fast")

        assert cmd[0] == AppConstants.TOOL_FFMPEG
        ss_idx = cmd.index("-ss")
        i_idx = cmd.index("-i")
        assert ss_idx < i_idx
        assert cmd[ss_idx + 1] == "125.500"
        assert cmd[i_idx + 1] == str(src)
        assert cmd.count("-ss") == 1
        assert "-an" in cmd and "-sn" in cmd and "-dn" in cmd
        vf = cmd[cmd.index("-vf") + 1]
        assert vf == (
            f"scale='min({AppConstants.SCENE_FRAME_FAST_WIDTH_PX},iw)':-2,"
            f"format={AppConstants.THUMBNAIL_JPEG_PIXEL_FORMAT}"
        )
        assert "tonemap" not in vf and "zscale" not in vf
        assert cmd[cmd.index("-q:v") + 1] == str(AppConstants.SCENE_FRAME_FAST_QUALITY)
        assert cmd[-4:] == ["-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"][-4:]
        assert cmd[-1] == "pipe:1"

    def test_build_command_accurate_mode_uses_hybrid_preseek_and_handles_ts_under_10s(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "movie.mkv"

        # Case 1: ts >= 10s -> pre_seek = ts - 10s before -i, post_seek = 10.000 after -i
        cmd = build_scene_frame_ffmpeg_command(src, ts_ms=42_250, mode="accurate")
        ss_positions = [idx for idx, tok in enumerate(cmd) if tok == "-ss"]
        i_idx = cmd.index("-i")
        assert len(ss_positions) == 2
        assert ss_positions[0] < i_idx < ss_positions[1]
        assert cmd[ss_positions[0] + 1] == "32.250"
        assert cmd[ss_positions[1] + 1] == "10.000"
        vf = cmd[cmd.index("-vf") + 1]
        assert vf == (
            f"scale='min({AppConstants.SCENE_FRAME_ACCURATE_WIDTH_PX},iw)':-2,"
            f"format={AppConstants.THUMBNAIL_JPEG_PIXEL_FORMAT}"
        )
        assert cmd[cmd.index("-q:v") + 1] == str(AppConstants.SCENE_FRAME_ACCURATE_QUALITY)

        # Case 2: ts < 10s -> pre_seek = 0.000 before -i, post_seek = ts after -i
        cmd_early = build_scene_frame_ffmpeg_command(src, ts_ms=3_500, mode="accurate")
        ss_early = [idx for idx, tok in enumerate(cmd_early) if tok == "-ss"]
        assert cmd_early[ss_early[0] + 1] == "0.000"
        assert cmd_early[ss_early[1] + 1] == "3.500"

    def test_build_command_rejects_negative_ts_and_invalid_mode(self, tmp_path: Path) -> None:
        src = tmp_path / "movie.mkv"
        with pytest.raises(ValueError, match="non-negative"):
            build_scene_frame_ffmpeg_command(src, ts_ms=-10, mode="fast")
        with pytest.raises(ValueError, match="Unsupported scene frame mode"):
            build_scene_frame_ffmpeg_command(src, ts_ms=0, mode="ultra")


class TestExtractSceneFrameCancellationIoniceAndErrors:
    """Verify disconnect kill, timeout escalation, ionice latch, and error handling."""

    @pytest.fixture(autouse=True)
    def _reset_ionice_latch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(frame_mod, "_ionice_frame_disabled", False)

    async def test_missing_file_and_disconnect_before_spawn(self, tmp_path: Path) -> None:
        with pytest.raises(SceneFrameError, match="does not exist"):
            await extract_scene_frame(tmp_path / "missing.mkv", ts_ms=0)

        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")

        async def _already_gone() -> bool:
            return True

        with (
            patch("asyncio.create_subprocess_exec") as mock_exec,
            pytest.raises(SceneFrameCancelledError, match="before frame extraction"),
        ):
            await extract_scene_frame(
                sample, ts_ms=0, is_disconnected=_already_gone
            )
        mock_exec.assert_not_called()

    async def test_disconnect_mid_extraction_terminates_and_escalates_to_kill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(AppConstants, "SCENE_FRAME_TERMINATE_GRACE_SEC", 0.03)
        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")

        checks = 0

        async def _disconnect_on_second_poll() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        terminated = False
        killed = False

        async def _hang_comm():
            await asyncio.sleep(5)
            return b"", b""

        async def _stubborn_wait():
            # First wait (after SIGTERM) stalls past grace; second wait (after SIGKILL) returns
            if not killed:
                await asyncio.sleep(5)
            return -9

        def _term():
            nonlocal terminated
            terminated = True

        def _kill():
            nonlocal killed
            killed = True

        fake_proc = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang_comm),
            terminate=_term,
            kill=_kill,
            wait=AsyncMock(side_effect=_stubborn_wait),
        )

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            pytest.raises(SceneFrameCancelledError, match="during frame extraction"),
        ):
            await extract_scene_frame(
                sample,
                ts_ms=1000,
                is_disconnected=_disconnect_on_second_poll,
                disconnect_poll_sec=0.01,
            )

        assert terminated is True
        assert killed is True

    async def test_task_cancellation_and_timeout_terminate_subprocess(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")

        # 1. Timeout terminates subprocess cleanly (and tolerates ProcessLookupError)
        def _term_gone():
            raise ProcessLookupError()

        async def _hang():
            await asyncio.sleep(5)
            return b"", b""

        fake_timeout_proc = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang),
            terminate=_term_gone,
            kill=lambda: None,
            wait=AsyncMock(return_value=0),
        )

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_timeout_proc),
            pytest.raises(SceneFrameError, match="timed out"),
        ):
            await extract_scene_frame(sample, ts_ms=0, timeout_sec=0.03)

        # 2. Timeout under ionice also raises SceneFrameError without retrying
        with (
            patch("app.services.scene_frame_service.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", return_value=fake_timeout_proc),
            pytest.raises(SceneFrameError, match="timed out"),
        ):
            await extract_scene_frame(
                sample, ts_ms=0, low_io_priority=True, timeout_sec=0.03
            )

        # 3. External asyncio.CancelledError cleans up subprocess and re-raises
        terminated = False

        def _term_ok():
            nonlocal terminated
            terminated = True

        fake_cancel_proc = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang),
            terminate=_term_ok,
            kill=lambda: None,
            wait=AsyncMock(return_value=-15),
        )
        with patch("asyncio.create_subprocess_exec", return_value=fake_cancel_proc):
            task = asyncio.create_task(
                extract_scene_frame(sample, ts_ms=0, low_io_priority=False)
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert terminated is True

        # 4. External cancel under ionice propagates without fallback
        terminated_io = False

        def _term_io():
            nonlocal terminated_io
            terminated_io = True

        fake_io_cancel = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang),
            terminate=_term_io,
            kill=lambda: None,
            wait=AsyncMock(return_value=-15),
        )
        with (
            patch("app.services.scene_frame_service.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", return_value=fake_io_cancel),
        ):
            task_io = asyncio.create_task(
                extract_scene_frame(sample, ts_ms=0, low_io_priority=True)
            )
            await asyncio.sleep(0.02)
            task_io.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_io
        assert terminated_io is True

    async def test_ionice_success_and_latched_fallback_on_launch_and_ioprio_errors(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")
        captured: list[tuple[str, ...]] = []

        # 1. ionice available and succeeds (with live disconnect poller cancelled cleanly on finish)
        async def _never_disconnected() -> bool:
            return False

        async def _ok_exec(*argv, **_kwargs):
            captured.append(argv)
            async def _quick_comm():
                await asyncio.sleep(0.025)
                return (FAKE_JPEG, b"")

            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(side_effect=_quick_comm),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with (
            patch("app.services.scene_frame_service.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_ok_exec),
        ):
            out = await extract_scene_frame(
                sample,
                ts_ms=1000,
                low_io_priority=True,
                is_disconnected=_never_disconnected,
                disconnect_poll_sec=0.01,
            )
        assert out == FAKE_JPEG
        assert captured[0][0] == AppConstants.IONICE_TOOL

        # 2. ionice launch PermissionError latches off and warns once
        captured.clear()

        async def _perm_exec(*argv, **_kwargs):
            captured.append(argv)
            if argv[0] == AppConstants.IONICE_TOOL:
                raise PermissionError("blocked")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(FAKE_JPEG, b"")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with (
            caplog.at_level(logging.WARNING, logger="app.services.scene_frame_service"),
            patch("app.services.scene_frame_service.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_perm_exec),
        ):
            assert await extract_scene_frame(sample, ts_ms=1000, low_io_priority=True) == FAKE_JPEG
            assert await extract_scene_frame(sample, ts_ms=2000, low_io_priority=True) == FAKE_JPEG

        assert [a[0] for a in captured] == [
            AppConstants.IONICE_TOOL,
            AppConstants.TOOL_FFMPEG,
            AppConstants.TOOL_FFMPEG,
        ]
        assert len(caplog.records) == 1

        # 3. ionice ioprio_set stderr exit latches off and falls back cleanly
        frame_mod._ionice_frame_disabled = False
        captured.clear()

        async def _ioprio_exec(*argv, **_kwargs):
            captured.append(argv)
            if argv[0] == AppConstants.IONICE_TOOL:
                return SimpleNamespace(
                    returncode=1,
                    communicate=AsyncMock(return_value=(b"", b"ionice: ioprio_set failed")),
                    terminate=lambda: None,
                    kill=lambda: None,
                    wait=AsyncMock(return_value=1),
                )
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(FAKE_JPEG, b"")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with (
            patch("app.services.scene_frame_service.is_ionice_available", return_value=True),
            patch("asyncio.create_subprocess_exec", side_effect=_ioprio_exec),
        ):
            assert await extract_scene_frame(sample, ts_ms=1000, low_io_priority=True) == FAKE_JPEG
        assert frame_mod._ionice_frame_disabled is True
        assert [a[0] for a in captured] == [AppConstants.IONICE_TOOL, AppConstants.TOOL_FFMPEG]

    async def test_ffmpeg_launch_exit_and_invalid_jpeg_errors(
        self, tmp_path: Path
    ) -> None:
        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no ffmpeg")),
            pytest.raises(SceneFrameError, match="not found"),
        ):
            await extract_scene_frame(sample, ts_ms=0)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=OSError("bad exec")),
            pytest.raises(SceneFrameError, match="Failed to launch"),
        ):
            await extract_scene_frame(sample, ts_ms=0)

        async def _exit_err(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=1,
                communicate=AsyncMock(return_value=(b"", b"Invalid data found")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=1),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_exit_err),
            pytest.raises(SceneFrameError, match="exit code 1"),
        ):
            await extract_scene_frame(sample, ts_ms=0)

        async def _bad_jpeg(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"not-jpeg-bytes", b"")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_bad_jpeg),
            pytest.raises(SceneFrameError, match="valid JPEG"),
        ):
            await extract_scene_frame(sample, ts_ms=0)

    async def test_kill_process_lookup_error_tolerated_during_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(AppConstants, "SCENE_FRAME_TERMINATE_GRACE_SEC", 0.02)
        sample = tmp_path / "clip.mkv"
        sample.write_bytes(b"mkv")

        wait_calls = 0

        async def _wait_then_exit():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.sleep(5)
            return -9

        async def _hang():
            await asyncio.sleep(5)
            return b"", b""

        def _kill_gone():
            raise ProcessLookupError()

        fake_proc = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang),
            terminate=lambda: None,
            kill=_kill_gone,
            wait=AsyncMock(side_effect=_wait_then_exit),
        )
        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            pytest.raises(SceneFrameError, match="timed out"),
        ):
            await extract_scene_frame(sample, ts_ms=0, timeout_sec=0.02)

    async def test_real_ffmpeg_10bit_hdr_accurate_mode_distinguishes_1_second_apart(
        self, tmp_path: Path
    ) -> None:
        if shutil.which(AppConstants.TOOL_FFMPEG) is None:
            pytest.skip("ffmpeg not installed locally")

        # Generate a real 3-second 10-bit HDR (yuv420p10le) MKV with time-varying visual content
        hdr_mkv = tmp_path / "hdr10_clock.mkv"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:duration=3:rate=10",
                "-pix_fmt",
                "yuv420p10le",
                "-c:v",
                "libx264",
                "-g",
                "100",
                str(hdr_mkv),
            ],
            check=True,
        )

        # Even with a single GOP (-g 100), mode='accurate' MUST yield distinct frames 1s apart!
        f1 = await extract_scene_frame(hdr_mkv, ts_ms=500, mode="accurate")
        f2 = await extract_scene_frame(hdr_mkv, ts_ms=1_500, mode="accurate")
        f_fast = await extract_scene_frame(hdr_mkv, ts_ms=1_500, mode="fast")

        assert f1.startswith(b"\xff\xd8\xff")
        assert f2.startswith(b"\xff\xd8\xff")
        assert f_fast.startswith(b"\xff\xd8\xff")
        assert f1 != f2

    async def test_resilient_fallback_and_safe_disconnect_poll_exception_handling(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sample = tmp_path / "open_gop.mkv"
        sample.write_bytes(b"mkv")

        # 1. Primary returns 0 bytes -> logs WARNING and automatically succeeds via resilient fallback
        captured_argvs: list[tuple[str, ...]] = []

        async def _empty_then_ok(*argv, **_kwargs):
            captured_argvs.append(argv)
            if len(captured_argvs) == 1:
                return SimpleNamespace(
                    returncode=0,
                    communicate=AsyncMock(return_value=(b"", b"")),
                    terminate=lambda: None,
                    kill=lambda: None,
                    wait=AsyncMock(return_value=0),
                )
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(FAKE_JPEG, b"")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with (
            caplog.at_level(logging.WARNING, logger="app.services.scene_frame_service"),
            patch("asyncio.create_subprocess_exec", side_effect=_empty_then_ok),
        ):
            out = await extract_scene_frame(sample, ts_ms=25_000, mode="fast")

        assert out == FAKE_JPEG
        assert len(captured_argvs) == 2
        assert "-nostdin" in captured_argvs[0]
        assert "-map" in captured_argvs[0] and "0:v:0" in captured_argvs[0]
        assert "-fps_mode" in captured_argvs[0] and "vfr" in captured_argvs[0]
        assert "-strict" in captured_argvs[0] and "unofficial" in captured_argvs[0]
        assert "-err_detect" in captured_argvs[1] and "ignore_err" in captured_argvs[1]
        assert "+genpts+discardcorrupt" in captured_argvs[1]
        assert any("retrying with resilient fallback" in r.message for r in caplog.records)

        # 2. Middleware exception inside is_disconnected() MUST NOT kill a healthy ffmpeg subprocess!
        poll_calls = 0

        async def _flaky_poller() -> bool:
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                return False  # pre-spawn check
            raise RuntimeError("BaseHTTPMiddleware receive error")

        async def _slow_ok(*_argv, **_kwargs):
            async def _comm():
                await asyncio.sleep(0.03)
                return (FAKE_JPEG, b"")

            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(side_effect=_comm),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_slow_ok):
            out2 = await extract_scene_frame(
                sample,
                ts_ms=5_000,
                is_disconnected=_flaky_poller,
                disconnect_poll_sec=0.01,
            )
        assert out2 == FAKE_JPEG

        # 3. Timeout after flaky poller exited safely still cleans up subprocess
        poll_calls = 0

        async def _hang_comm():
            await asyncio.sleep(5)
            return b"", b""

        fake_hang = SimpleNamespace(
            returncode=None,
            communicate=AsyncMock(side_effect=_hang_comm),
            terminate=lambda: None,
            kill=lambda: None,
            wait=AsyncMock(return_value=-15),
        )
        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_hang),
            pytest.raises(SceneFrameError, match="timed out"),
        ):
            await extract_scene_frame(
                sample,
                ts_ms=5_000,
                is_disconnected=_flaky_poller,
                disconnect_poll_sec=0.005,
                timeout_sec=0.025,
            )

        # 4. Accurate mode resilient fallback argv preserves 10s preseek + ignore_err
        fb_acc = build_scene_frame_ffmpeg_command(
            sample, ts_ms=25_000, mode="accurate", resilient_fallback=True
        )
        assert "-err_detect" in fb_acc and "ignore_err" in fb_acc
        assert fb_acc[fb_acc.index("-ss") + 1] == "15.000"

    async def test_server_latest_wins_superseding_and_different_file_isolation(
        self, tmp_path: Path
    ) -> None:
        """Rapid bombardment on the same file immediately cancels obsolete ffmpeg without affecting other files."""
        movie_a = tmp_path / "movie_a.mkv"
        movie_b = tmp_path / "movie_b.mkv"
        movie_a.write_bytes(b"mkv-a")
        movie_b.write_bytes(b"mkv-b")

        from app.services import scene_frame_service as sfs

        # 1. In-flight superseding even when is_disconnected always returns False (lagging reverse proxy)
        req1_started = asyncio.Event()
        req1_terminated = False
        spawn_count = 0

        async def _fake_exec(*argv, **_kwargs):
            nonlocal req1_terminated, spawn_count
            spawn_count += 1
            call_num = spawn_count
            if call_num == 1:
                req1_started.set()

                async def _slow_comm():
                    await asyncio.sleep(5)
                    return FAKE_JPEG, b""

                def _term():
                    nonlocal req1_terminated
                    req1_terminated = True

                async def _wait_after_term():
                    await asyncio.sleep(0.02)
                    return -15

                return SimpleNamespace(
                    returncode=None,
                    communicate=AsyncMock(side_effect=_slow_comm),
                    terminate=_term,
                    kill=lambda: None,
                    wait=AsyncMock(side_effect=_wait_after_term),
                )
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(FAKE_JPEG, b"")),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        async def _never_disconnected() -> bool:
            return False

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            # Request #1 starts slow ffmpeg; Request #2 arrives mid-flight, then Request #3 immediately supersedes #2 while queued!
            t1 = asyncio.create_task(
                extract_scene_frame(
                    movie_a,
                    ts_ms=10_000,
                    is_disconnected=_never_disconnected,
                    disconnect_poll_sec=0.2,
                )
            )
            await asyncio.wait_for(req1_started.wait(), timeout=1.0)
            t2 = asyncio.create_task(
                extract_scene_frame(
                    movie_a,
                    ts_ms=20_000,
                    is_disconnected=_never_disconnected,
                    disconnect_poll_sec=0.2,
                )
            )
            await asyncio.sleep(0.005)
            t3 = asyncio.create_task(
                extract_scene_frame(
                    movie_a,
                    ts_ms=30_000,
                    is_disconnected=_never_disconnected,
                    disconnect_poll_sec=0.2,
                )
            )

            with pytest.raises(SceneFrameCancelledError):
                await t1
            with pytest.raises(SceneFrameCancelledError, match="Superseded"):
                await t2
            latest_bytes = await t3

        assert req1_terminated is True
        assert latest_bytes == FAKE_JPEG
        # Only Request #1 and Request #3 spawned ffmpeg; superseded queued Request #2 NEVER launched ffmpeg!
        assert spawn_count == 2
        assert sfs._ACTIVE_FILE_CANCEL_EVENTS == {}

        # 2. Different files do NOT supersede each other (both complete cleanly in serial order)
        async def _fast_exec(*_argv, **_kwargs):
            async def _comm():
                await asyncio.sleep(0.02)
                return FAKE_JPEG, b""

            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(side_effect=_comm),
                terminate=lambda: None,
                kill=lambda: None,
                wait=AsyncMock(return_value=0),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_fast_exec):
            res_a, res_b = await asyncio.gather(
                extract_scene_frame(movie_a, ts_ms=1_000, disconnect_poll_sec=0.005),
                extract_scene_frame(movie_b, ts_ms=2_000, disconnect_poll_sec=0.005),
            )
        assert res_a == FAKE_JPEG
        assert res_b == FAKE_JPEG
        assert sfs._ACTIVE_FILE_CANCEL_EVENTS == {}
