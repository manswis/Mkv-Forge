"""Unit tests for app.services.ffmpeg_runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ffmpeg_runner import (
    FFmpegError,
    FFprobeAudioStreamInfo,
    StreamVerificationError,
    build_ffmpeg_command,
    canonicalize_audio_codec,
    compute_target_channels,
    is_lossless_source_codec,
    parse_ffmpeg_progress_kv,
    resolve_effective_bitrate,
    resolve_target_sample_rate,
    run_ffmpeg_transcode,
    verify_audio_stream_with_ffprobe,
)


def _fake_stream_reader(lines: list[bytes]) -> MagicMock:
    reader = MagicMock()
    iterator = iter([*lines, b""])
    reader.readline = AsyncMock(side_effect=lambda: next(iterator, b""))
    return reader


def _make_ffprobe_proc(returncode: int = 0, stdout: bytes = b"{}", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestHelpers:
    """Verify codec normalization, channel capping, progress parsing, and argv builder."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("AAC", "aac"),
            ("E-AC-3", "eac3"),
            ("eac3", "eac3"),
            ("AC-3", "ac3"),
            ("DTS-HD Master Audio", "dts"),
            ("TrueHD", "truehd"),
            ("MPEG-1/2 layer 3", "mp3"),
            (None, None),
            ("   ", None),
        ],
    )
    def test_canonicalize_audio_codec(self, raw: str | None, expected: str | None) -> None:
        assert canonicalize_audio_codec(raw) == expected

    @pytest.mark.parametrize(
        ("source_ch", "expected_ch"),
        [(1, 1), (2, 2), (6, 6), (8, 6)],
    )
    def test_compute_target_channels_preserves_and_caps_at_51(
        self, source_ch: int, expected_ch: int
    ) -> None:
        assert compute_target_channels(source_ch) == expected_ch

    def test_compute_target_channels_rejects_zero_or_negative(self) -> None:
        with pytest.raises(ValueError):
            compute_target_channels(0)
        with pytest.raises(ValueError):
            compute_target_channels(-2)

    def test_parse_ffmpeg_progress_kv(self) -> None:
        duration_us = 10_000_000  # 10s
        assert parse_ffmpeg_progress_kv("out_time_us=5000000", duration_us) == ("percent", 50)
        assert parse_ffmpeg_progress_kv("out_time_us=12000000", duration_us) == ("percent", 99)
        assert parse_ffmpeg_progress_kv("out_time_us=N/A", duration_us) is None
        assert parse_ffmpeg_progress_kv("out_time_us=-500", duration_us) is None
        assert parse_ffmpeg_progress_kv("out_time_us=5000000", 0) is None
        assert parse_ffmpeg_progress_kv("speed=21.4x", duration_us) == ("speed", "21.4x")
        assert parse_ffmpeg_progress_kv("speed=N/A", duration_us) is None
        assert parse_ffmpeg_progress_kv("progress=continue", duration_us) is None
        assert parse_ffmpeg_progress_kv("malformed", duration_us) is None

    @pytest.mark.parametrize(
        ("source_sr", "codec", "expected_sr"),
        [
            (48_000, "eac3", None),
            (44_100, "eac3", None),
            (32_000, "ac3", None),
            (None, "eac3", None),
            (24_000, "eac3", 48_000),
            (96_000, "ac3", 48_000),
            (24_000, "aac", None),
        ],
    )
    def test_resolve_target_sample_rate(
        self, source_sr: int | None, codec: str, expected_sr: int | None
    ) -> None:
        assert resolve_target_sample_rate(source_sr, codec) == expected_sr

    def test_build_ffmpeg_command_valid_and_invalid(self, tmp_path: Path) -> None:
        cmd = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.eac3",
            audio_index=1,
            target_codec="eac3",
            bitrate="640k",
            channels=8,  # 7.1 capped to 6 -> emits -ac 6 -channel_layout 5.1
            sample_rate=48_000,  # Supported rate -> omitted (zero resampling)
        )
        assert cmd == [
            "ffmpeg",
            "-y",
            "-i",
            str(tmp_path / "in.mkv"),
            "-map",
            "0:a:1",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:a",
            "eac3",
            "-b:a",
            "640k",
            "-ac",
            "6",
            "-channel_layout",
            "5.1",
            "-progress",
            "pipe:1",
            "-nostats",
            str(tmp_path / "in.mkv.mkvforge.eac3"),
        ]

        # Non-standard sample rate (e.g. 24000 Hz) auto-clamps to -ar 48000 for Dolby
        cmd_resampled = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.ac3",
            audio_index=0,
            target_codec="ac3",
            bitrate="448k",
            channels=2,
            sample_rate=24_000,
        )
        assert "-channel_layout" not in cmd_resampled
        assert cmd_resampled[cmd_resampled.index("-ar") + 1] == "48000"

        with pytest.raises(ValueError):
            build_ffmpeg_command(
                tmp_path / "in.mkv",
                tmp_path / "out.eac3",
                audio_index=-1,
                target_codec="eac3",
                bitrate="640k",
                channels=6,
            )
        with pytest.raises(ValueError):
            build_ffmpeg_command(
                tmp_path / "in.mkv",
                tmp_path / "out.opus",
                audio_index=0,
                target_codec="opus",
                bitrate="640k",
                channels=6,
            )
        with pytest.raises(ValueError, match="Unsupported target codec"):
            build_ffmpeg_command(
                tmp_path / "in.mkv",
                tmp_path / "out.mp3",
                audio_index=0,
                target_codec="mp3",
                bitrate="320k",
                channels=2,
            )

    @pytest.mark.parametrize(
        ("raw_codec", "expected"),
        [
            ("TrueHD", True),
            ("TrueHD Atmos", True),
            ("FLAC", True),
            ("PCM", True),
            ("pcm_s24le", True),
            ("ALAC", True),
            ("DTS-HD Master Audio", True),
            ("DTS-HD MA", True),
            ("DTS-HD High Resolution Audio", False),
            ("DTS", False),
            ("AAC", False),
            ("E-AC-3", False),
            ("AC-3", False),
            ("Opus", False),
            (None, False),
            ("   ", False),
        ],
    )
    def test_is_lossless_source_codec(
        self, raw_codec: str | None, expected: bool
    ) -> None:
        assert is_lossless_source_codec(raw_codec) is expected

    @pytest.mark.parametrize(
        ("source_ch", "target_codec", "requested_ch", "expected_ch"),
        [
            (8, "eac3", None, 6),
            (8, "ac3", None, 6),
            (8, "aac", None, 8),
            (8, "opus", None, 8),
            (8, "flac", None, 8),
            (7, "opus", None, 7),
            (8, "opus", 6, 6),
            (8, "aac", 2, 2),
            (6, "eac3", 2, 2),
            (6, "flac", 6, 6),
            (2, "opus", 1, 1),
        ],
    )
    def test_compute_target_channels_codec_matrix_and_downmix(
        self,
        source_ch: int,
        target_codec: str,
        requested_ch: int | None,
        expected_ch: int,
    ) -> None:
        assert (
            compute_target_channels(
                source_ch,
                target_codec=target_codec,
                requested_channels=requested_ch,
            )
            == expected_ch
        )

    def test_compute_target_channels_rejects_upmix_and_exceeding_codec_cap(self) -> None:
        # Never allow upmixing
        with pytest.raises(ValueError, match="Cannot upmix"):
            compute_target_channels(2, target_codec="aac", requested_channels=6)
        with pytest.raises(ValueError, match="Cannot upmix"):
            compute_target_channels(6, target_codec="opus", requested_channels=8)
        # Never allow exceeding codec channel ceiling
        with pytest.raises(ValueError, match="supports at most 6"):
            compute_target_channels(8, target_codec="eac3", requested_channels=8)
        with pytest.raises(ValueError, match="supports at most 6"):
            compute_target_channels(8, target_codec="ac3", requested_channels=7)
        # Reject invalid requested channels
        with pytest.raises(ValueError, match="Invalid target audio channel count"):
            compute_target_channels(6, target_codec="aac", requested_channels=0)
        with pytest.raises(ValueError, match="Unsupported target codec"):
            compute_target_channels(6, target_codec="mp3")

    @pytest.mark.parametrize(
        ("source_sr", "codec", "expected_sr"),
        [
            (44_100, "opus", 48_000),
            (48_000, "opus", None),
            (96_000, "opus", 48_000),
            (None, "opus", 48_000),
            (96_000, "flac", None),
            (44_100, "flac", None),
            (96_000, "aac", None),
        ],
    )
    def test_resolve_target_sample_rate_opus_and_flac(
        self, source_sr: int | None, codec: str, expected_sr: int | None
    ) -> None:
        assert resolve_target_sample_rate(source_sr, codec) == expected_sr

    @pytest.mark.parametrize(
        ("codec", "bitrate", "channels", "expected"),
        [
            ("opus", "320k", 1, "256k"),
            ("opus", "512k", 1, "256k"),
            ("opus", "128k", 1, "128k"),
            ("opus", "320k", 2, "320k"),
            ("opus", "448k", 8, "448k"),
            ("flac", "640k", 8, None),
            ("eac3", "640k", 1, "640k"),
        ],
    )
    def test_resolve_effective_bitrate_clamps_opus_mono_ceiling(
        self, codec: str, bitrate: str | None, channels: int, expected: str | None
    ) -> None:
        assert resolve_effective_bitrate(codec, bitrate, channels) == expected

    def test_build_ffmpeg_command_opus_flac_eac3_768k_and_drc_scale(
        self, tmp_path: Path
    ) -> None:
        # 1. Opus 7.1 -> libopus, -channel_layout 7.1, -mapping_family 1, -ar 48000 (when sr != 48k)
        opus_71 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.mka",
            audio_index=0,
            target_codec="opus",
            bitrate="448k",
            channels=8,
            sample_rate=96_000,
            source_codec="TrueHD",
        )
        assert "-drc_scale" not in opus_71
        assert opus_71[opus_71.index("-c:a") + 1] == "libopus"
        assert opus_71[opus_71.index("-b:a") + 1] == "448k"
        assert opus_71[opus_71.index("-ac") + 1] == "8"
        assert opus_71[opus_71.index("-channel_layout") + 1] == "7.1"
        assert opus_71[opus_71.index("-mapping_family") + 1] == "1"
        assert opus_71[opus_71.index("-ar") + 1] == "48000"

        # 2. Opus 2.0 -> no -channel_layout and no -mapping_family
        opus_20 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.mka",
            audio_index=0,
            target_codec="opus",
            bitrate="128k",
            channels=2,
            sample_rate=48_000,
        )
        assert "-mapping_family" not in opus_20
        assert "-channel_layout" not in opus_20
        assert "-ar" not in opus_20

        # 3. FLAC 7.1 -> omits -b:a, preserves native sample rate, emits -channel_layout 7.1
        flac_71 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.mka",
            audio_index=0,
            target_codec="flac",
            bitrate=None,
            channels=8,
            sample_rate=96_000,
        )
        assert flac_71[flac_71.index("-c:a") + 1] == "flac"
        assert "-b:a" not in flac_71
        assert "-ar" not in flac_71
        assert flac_71[flac_71.index("-ac") + 1] == "8"
        assert flac_71[flac_71.index("-channel_layout") + 1] == "7.1"

        # 4. E-AC-3 5.1 @ 768k allowed; AC-3 @ 768k rejected
        eac3_768 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.eac3",
            audio_index=0,
            target_codec="eac3",
            bitrate="768k",
            channels=6,
            sample_rate=48_000,
        )
        assert eac3_768[eac3_768.index("-b:a") + 1] == "768k"
        with pytest.raises(ValueError, match="Unsupported bitrate"):
            build_ffmpeg_command(
                tmp_path / "in.mkv",
                tmp_path / "in.mkv.mkvforge.ac3",
                audio_index=0,
                target_codec="ac3",
                bitrate="768k",
                channels=6,
            )

        # 5. -drc_scale 0 placed BEFORE -i for AC-3 and E-AC-3 sources only
        for dolby_src in ("AC-3", "E-AC-3", "ac3", "eac3"):
            cmd = build_ffmpeg_command(
                tmp_path / "in.mkv",
                tmp_path / "in.mkv.mkvforge.mka",
                audio_index=0,
                target_codec="aac",
                bitrate="384k",
                channels=6,
                source_codec=dolby_src,
            )
            assert "-drc_scale" in cmd
            drc_idx = cmd.index("-drc_scale")
            in_idx = cmd.index("-i")
            assert drc_idx < in_idx
            assert cmd[drc_idx + 1] == "0"

        # Non-Dolby source must NOT receive -drc_scale
        aac_src = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.eac3",
            audio_index=0,
            target_codec="eac3",
            bitrate="640k",
            channels=6,
            source_codec="AAC",
        )
        assert "-drc_scale" not in aac_src

        # 6. 4-channel uses canonical 'quad' (RFC 7845 / FLAC spec) and 5-channel uses '5.0'
        opus_4ch = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.mka",
            audio_index=0,
            target_codec="opus",
            bitrate="256k",
            channels=4,
            sample_rate=48_000,
        )
        assert opus_4ch[opus_4ch.index("-ac") + 1] == "4"
        assert opus_4ch[opus_4ch.index("-channel_layout") + 1] == "quad"
        assert opus_4ch[opus_4ch.index("-mapping_family") + 1] == "1"

        opus_5ch = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / "in.mkv.mkvforge.mka",
            audio_index=0,
            target_codec="opus",
            bitrate="256k",
            channels=5,
            sample_rate=48_000,
        )
        assert opus_5ch[opus_5ch.index("-ac") + 1] == "5"
        assert opus_5ch[opus_5ch.index("-channel_layout") + 1] == "5.0"
        assert opus_5ch[opus_5ch.index("-mapping_family") + 1] == "1"


class TestVerifyAudioStreamWithFfprobe:
    """Verify stream mapping checks and format.duration fallback."""

    async def test_verifies_matching_stream_and_parses_format_duration(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "streams": [
                {"index": 1, "codec_name": "aac", "channels": 2, "sample_rate": "44100"},
                {
                    "index": 3,
                    "codec_name": "aac",
                    "channels": 6,
                    "sample_rate": "48000",
                    "channel_layout": "5.1",
                },
            ],
            "format": {"duration": "12.500000"},
        }
        with patch(
            "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(
                return_value=_make_ffprobe_proc(0, json.dumps(payload).encode())
            ),
        ):
            info = await verify_audio_stream_with_ffprobe(
                tmp_path / "movie.mp4",
                audio_index=1,
                expected_codec="AAC",
                expected_channels=6,
            )

        assert info.audio_index == 1
        assert info.stream_index == 3
        assert info.codec_name == "aac"
        assert info.channels == 6
        assert info.sample_rate == 48_000
        assert info.channel_layout == "5.1"
        assert info.format_duration_us == 12_500_000

    async def test_raises_on_out_of_range_codec_or_channel_mismatch(
        self, tmp_path: Path
    ) -> None:
        payload = {"streams": [{"index": 1, "codec_name": "ac3", "channels": 2}]}
        raw = json.dumps(payload).encode()

        with patch(
            "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_ffprobe_proc(0, raw)),
        ):
            with pytest.raises(StreamVerificationError, match="not found"):
                await verify_audio_stream_with_ffprobe(tmp_path / "a.mkv", 2)

            with pytest.raises(StreamVerificationError, match="codec mismatch"):
                await verify_audio_stream_with_ffprobe(
                    tmp_path / "a.mkv", 0, expected_codec="AAC"
                )

            with pytest.raises(StreamVerificationError, match="channel mismatch"):
                await verify_audio_stream_with_ffprobe(
                    tmp_path / "a.mkv", 0, expected_codec="AC-3", expected_channels=6
                )

    async def test_handles_missing_binary_and_timeout(self, tmp_path: Path) -> None:
        with patch(
            "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError),
        ):
            with pytest.raises(FFmpegError, match="not found"):
                await verify_audio_stream_with_ffprobe(tmp_path / "a.mkv", 0)

        hanging = _make_ffprobe_proc()

        async def _hang():
            await asyncio.sleep(5)
            return b"", b""

        hanging.communicate = AsyncMock(side_effect=_hang)
        with patch(
            "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=hanging),
        ):
            with pytest.raises(FFmpegError, match="timed out"):
                await verify_audio_stream_with_ffprobe(
                    tmp_path / "a.mkv", 0, timeout_sec=0.05
                )
        hanging.kill.assert_called_once()


class TestRunFfmpegTranscode:
    """Verify async Stage 1 progress callbacks, mp4 duration fallback, and errors."""

    async def test_uses_ffprobe_duration_fallback_and_emits_progress(
        self, tmp_path: Path
    ) -> None:
        probe_info = FFprobeAudioStreamInfo(
            audio_index=0,
            stream_index=1,
            codec_name="aac",
            channels=2,  # Stereo must stay 2 channels, NOT 6!
            format_duration_us=10_000_000,
        )
        fake_proc = MagicMock()
        fake_proc.stdout = _fake_stream_reader(
            [
                b"out_time_us=N/A\n",
                b"out_time_us=2500000\n",
                b"speed=18.4x\n",
                b"out_time_us=7500000\n",
                b"progress=end\n",
            ]
        )
        fake_proc.stderr = _fake_stream_reader([b"encoder line 1\n"])
        fake_proc.wait = AsyncMock(return_value=0)

        progress_events: list[tuple[int, str | None]] = []
        logs: list[str] = []
        started_procs: list = []

        with (
            patch(
                "app.services.ffmpeg_runner.verify_audio_stream_with_ffprobe",
                new=AsyncMock(return_value=probe_info),
            ),
            patch(
                "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_proc),
            ) as mock_exec,
        ):
            result = await run_ffmpeg_transcode(
                tmp_path / "clip.mp4",
                tmp_path / "clip.mp4.mkvforge.eac3",
                audio_index=0,
                target_codec="eac3",
                bitrate="384k",
                duration_us=0,  # Forces fallback to ffprobe format_duration_us
                on_progress=lambda pct, spd: progress_events.append((pct, spd)),
                on_log=logs.append,
                on_process_started=started_procs.append,
            )

        assert result.channels == 2
        assert result.effective_duration_us == 10_000_000
        assert (25, None) in progress_events
        assert (25, "18.4x") in progress_events
        assert (75, "18.4x") in progress_events
        assert logs == ["encoder line 1"]
        assert started_procs == [fake_proc]

        # Verify -ac 2 was passed (not hardcoded -ac 6)
        called_args = mock_exec.call_args.args
        ac_idx = called_args.index("-ac")
        assert called_args[ac_idx + 1] == "2"

    async def test_raises_ffmpeg_error_on_nonzero_exit(self, tmp_path: Path) -> None:
        probe_info = FFprobeAudioStreamInfo(
            audio_index=0,
            stream_index=1,
            codec_name="aac",
            channels=6,
            format_duration_us=10_000_000,
        )
        fake_proc = MagicMock()
        fake_proc.stdout = _fake_stream_reader([])
        fake_proc.stderr = _fake_stream_reader([b"Conversion failed!\n"])
        fake_proc.wait = AsyncMock(return_value=1)

        with (
            patch(
                "app.services.ffmpeg_runner.verify_audio_stream_with_ffprobe",
                new=AsyncMock(return_value=probe_info),
            ),
            patch(
                "app.services.ffmpeg_runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_proc),
            ),
        ):
            with pytest.raises(FFmpegError, match="exit code 1"):
                await run_ffmpeg_transcode(
                    tmp_path / "a.mkv",
                    tmp_path / "a.mkv.mkvforge.eac3",
                    audio_index=0,
                    target_codec="eac3",
                    bitrate="640k",
                )
