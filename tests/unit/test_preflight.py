"""Unit tests for app.services.preflight."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import AppConstants
from app.models.schemas import AudioConversionConfig, JobConfig
from app.services.preflight import (
    InsufficientDiskSpaceError,
    InvalidAudioConversionError,
    PreflightError,
    TargetFileCollisionError,
    calculate_required_disk_bytes,
    check_disk_space,
    estimate_audio_size_bytes,
    run_preflight_checks,
    validate_audio_conversion_pair,
)

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


class TestDiskSpaceCalculations:
    """Verify audio size estimation and required disk space formula."""

    def test_estimate_audio_size_bytes_for_known_and_unknown_duration(self) -> None:
        # 1000 seconds at 640,000 bps = 80,000,000 bytes
        assert estimate_audio_size_bytes(1_000_000_000, "640k") == 80_000_000
        assert estimate_audio_size_bytes(1_000_000_000, "448k") == 56_000_000
        assert estimate_audio_size_bytes(1_000_000_000, "384k") == 48_000_000
        assert (
            estimate_audio_size_bytes(0, "640k")
            == AppConstants.UNKNOWN_DURATION_AUDIO_ESTIMATE_BYTES
        )

    def test_estimate_audio_size_rejects_unsupported_bitrate(self) -> None:
        with pytest.raises(ValueError, match="Unsupported audio bitrate"):
            estimate_audio_size_bytes(1_000_000, "999k")

    def test_estimate_audio_size_supports_768k_and_lossless_flac(self) -> None:
        # 1000s at 768,000 bps = 96,000,000 bytes
        assert estimate_audio_size_bytes(1_000_000_000, "768k", target_codec="eac3") == 96_000_000

        # FLAC lossless ceiling: duration_sec * channels * 48000 * 3
        flac_71_bytes = estimate_audio_size_bytes(
            10_000_000,  # 10 seconds
            None,
            target_codec="flac",
            channels=8,
        )
        assert (
            flac_71_bytes
            == 10 * 8 * AppConstants.FLAC_ESTIMATED_SAMPLE_RATE * AppConstants.FLAC_ESTIMATED_BYTES_PER_SAMPLE
        )

        # FLAC with unknown duration uses conservative fallback
        flac_unknown = estimate_audio_size_bytes(0, None, target_codec="flac", channels=8)
        assert flac_unknown == AppConstants.UNKNOWN_DURATION_AUDIO_ESTIMATE_BYTES

        # FLAC calculate_required_disk_bytes integrates cleanly with None bitrate
        req, est = calculate_required_disk_bytes(
            input_size_bytes=1_000_000,
            audio_conversion_enabled=True,
            duration_us=10_000_000,
            bitrate=None,
            target_codec="flac",
            channels=8,
        )
        assert est == flac_71_bytes
        assert req == 1_100_000 + flac_71_bytes + AppConstants.DISK_SPACE_BUFFER_BYTES

    def test_calculate_required_disk_bytes_with_and_without_conversion(self) -> None:
        req_no_conv, est_no_conv = calculate_required_disk_bytes(
            input_size_bytes=1_000_000,
            audio_conversion_enabled=False,
            duration_us=1_000_000_000,
            bitrate="640k",
        )
        assert est_no_conv == 0
        assert req_no_conv == 1_100_000 + AppConstants.DISK_SPACE_BUFFER_BYTES

        req_conv, est_conv = calculate_required_disk_bytes(
            input_size_bytes=1_000_000,
            audio_conversion_enabled=True,
            duration_us=1_000_000_000,
            bitrate="640k",
        )
        assert est_conv == 80_000_000
        assert (
            req_conv
            == 1_100_000 + 80_000_000 + AppConstants.DISK_SPACE_BUFFER_BYTES
        )

    def test_negative_input_size_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            calculate_required_disk_bytes(
                input_size_bytes=-1,
                audio_conversion_enabled=False,
                duration_us=0,
                bitrate="640k",
            )


class TestPreflightGuards:
    """Verify disk-space boundary checks and .mp4 replace collision guard."""

    def test_disk_space_exact_boundary_passes_and_one_byte_short_fails(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"x" * 10_000)

        required, _ = calculate_required_disk_bytes(
            input_size_bytes=10_000,
            audio_conversion_enabled=True,
            duration_us=100_000_000,
            bitrate="640k",
        )

        # Exact required bytes -> passes
        with patch(
            "app.services.preflight.shutil.disk_usage",
            return_value=_DiskUsage(total=required * 2, used=required, free=required),
        ):
            res = check_disk_space(
                src,
                audio_conversion_enabled=True,
                duration_us=100_000_000,
                bitrate="640k",
            )
        assert res.free_bytes == required
        assert res.required_bytes == required

        # 1 byte short -> fails cleanly
        with patch(
            "app.services.preflight.shutil.disk_usage",
            return_value=_DiskUsage(
                total=required * 2, used=required + 1, free=required - 1
            ),
        ):
            with pytest.raises(InsufficientDiskSpaceError, match="Insufficient disk space"):
                check_disk_space(
                    src,
                    audio_conversion_enabled=True,
                    duration_us=100_000_000,
                    bitrate="640k",
                )

    def test_missing_source_file_raises_preflight_error(self, tmp_path: Path) -> None:
        with pytest.raises(PreflightError, match="does not exist"):
            run_preflight_checks(tmp_path / "missing.mkv", JobConfig())

    def test_mp4_replace_collision_blocks_before_start(self, tmp_path: Path) -> None:
        mp4 = tmp_path / "Film.mp4"
        existing_mkv = tmp_path / "Film.mkv"
        mp4.write_bytes(b"mp4-data")
        existing_mkv.write_bytes(b"existing-mkv")

        replace_cfg = JobConfig(
            audio_conversion=AudioConversionConfig(enabled=False),
            output_mode="replace",
        )
        with pytest.raises(TargetFileCollisionError, match="Film.mkv"):
            run_preflight_checks(mp4, replace_cfg)

        # Keep Both mode is allowed even if <stem>.mkv exists
        keep_both_cfg = JobConfig(
            audio_conversion=AudioConversionConfig(enabled=False),
            output_mode="keep_both",
        )
        with patch(
            "app.services.preflight.shutil.disk_usage",
            return_value=_DiskUsage(
                total=10**12, used=0, free=10**12
            ),
        ):
            result = run_preflight_checks(mp4, keep_both_cfg)
        assert result.input_size_bytes == len(b"mp4-data")


class TestAudioConversionCodecMatrix:
    """Verify backend rejects same-codec and lossy AC3->EAC3 conversions."""

    @pytest.mark.parametrize(
        ("source_codec", "target_codec"),
        [
            ("AAC", "eac3"),
            ("AAC", "ac3"),
            ("E-AC-3", "ac3"),
            ("E-AC-3", "aac"),
            ("AC-3", "aac"),
            ("DTS-HD Master Audio", "eac3"),
            ("TrueHD", "ac3"),
            ("FLAC", "aac"),
            (None, "eac3"),
        ],
    )
    def test_allows_logical_conversion_pairs(
        self, source_codec: str | None, target_codec: str
    ) -> None:
        validate_audio_conversion_pair(source_codec, target_codec)

    @pytest.mark.parametrize(
        ("source_codec", "target_codec"),
        [
            ("E-AC-3", "eac3"),
            ("AC-3", "ac3"),
            ("AAC", "aac"),
            ("AC-3", "eac3"),
        ],
    )
    def test_rejects_same_codec_and_lossy_upsample(
        self, source_codec: str, target_codec: str
    ) -> None:
        with pytest.raises(InvalidAudioConversionError):
            validate_audio_conversion_pair(source_codec, target_codec)

    @pytest.mark.parametrize(
        ("source_codec", "target_codec", "source_ch", "target_ch"),
        [
            ("TrueHD", "flac", 8, 8),
            ("TrueHD Atmos", "flac", 8, 6),
            ("DTS-HD Master Audio", "flac", 8, 8),
            ("PCM", "flac", 6, 6),
            ("AAC", "opus", 8, 8),
            ("AAC", "aac", 8, 2),      # Explicit same-codec downmix allowed
            ("E-AC-3", "eac3", 6, 2),   # Explicit same-codec downmix allowed
            ("E-AC-3", "eac3", 8, None),  # 7.1 -> 5.1 Dolby cap downmix allowed
            ("AC-3", "eac3", 6, 2),     # Explicit downmix allowed
            ("FLAC", "flac", 8, 2),    # Lossless downmix allowed
        ],
    )
    def test_phase14_allows_lossless_flac_opus_and_explicit_downmix(
        self,
        source_codec: str,
        target_codec: str,
        source_ch: int,
        target_ch: int,
    ) -> None:
        validate_audio_conversion_pair(
            source_codec,
            target_codec,
            source_channels=source_ch,
            target_channels=target_ch,
        )

    @pytest.mark.parametrize(
        ("source_codec", "target_codec", "source_ch", "target_ch", "match"),
        [
            ("AAC", "flac", 6, 6, "lossless"),
            ("E-AC-3", "flac", 6, 6, "lossless"),
            ("DTS", "flac", 6, 6, "lossless"),
            ("DTS-HD High Resolution Audio", "flac", 8, 8, "lossless"),
            ("AAC", "aac", 6, 6, "already AAC"),
            ("Opus", "opus", 6, 6, "already OPUS"),
            ("AAC", "opus", 2, 6, "Cannot upmix"),
            ("TrueHD", "eac3", 8, 8, "supports at most 6"),
            ("TrueHD", "ac3", 8, 7, "supports at most 6"),
        ],
    )
    def test_phase14_rejects_lossy_to_flac_upmix_and_codec_cap_violations(
        self,
        source_codec: str,
        target_codec: str,
        source_ch: int,
        target_ch: int,
        match: str,
    ) -> None:
        with pytest.raises(InvalidAudioConversionError, match=match):
            validate_audio_conversion_pair(
                source_codec,
                target_codec,
                source_channels=source_ch,
                target_channels=target_ch,
            )
