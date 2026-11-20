"""Unit tests for app.services.mkv_builder enforcing all mkvmerge guardrails."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.schemas import (
    AudioConversionConfig,
    ContainerMetadataConfig,
    JobConfig,
    TrackEditConfig,
)
from app.services.mkv_builder import build_mkvmerge_command, ns_to_sync_delay_ms


class TestNsToSyncDelayMs:
    """Guardrail #2: verify nanosecond-to-millisecond rounding."""

    @pytest.mark.parametrize(
        ("ns", "expected_ms"),
        [
            (None, 0),
            (0, 0),
            (42_000_000, 42),
            (41_600_000, 42),
            (-15_400_000, -15),
            (-15_600_000, -16),
        ],
    )
    def test_conversion(self, ns: int | None, expected_ms: int) -> None:
        assert ns_to_sync_delay_ms(ns) == expected_ms


class TestBuildMkvmergeCommand:
    """Verify argument ordering (Guardrail #1), double-default prevention, empty tracks, and track-order."""

    def test_stage1_plus_stage2_argument_order_sync_and_double_default_fix(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Movie.mkv"
        tmp_out = tmp_path / "Movie.mkv.mkvforge.tmp"
        tmp_audio = tmp_path / "Movie.mkv.mkvforge.eac3"

        config = JobConfig(
            container=ContainerMetadataConfig(
                title="",  # explicitly clear container title
                strip_global_tags=True,
                strip_track_tags=True,
                strip_attachments=True,
                strip_chapters=True,
            ),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                source_channels=6,
                source_minimum_timestamp_ns=42_000_000,  # +42ms
                target_codec="eac3",
                bitrate="640k",
                track_title="English EAC3 5.1",
                language="eng",
                set_as_default=True,
                drop_source_track=False,
            ),
            tracks=[
                TrackEditConfig(
                    id=0,
                    type="video",
                    enabled=True,
                    track_name="Main Video",
                    language="und",
                    default_track=True,
                ),
                TrackEditConfig(
                    id=1,
                    type="audio",
                    enabled=True,
                    track_name="Original AAC 5.1",
                    language="eng",
                    default_track=True,  # MUST be demoted to no!
                ),
                TrackEditConfig(
                    id=2,
                    type="subtitles",
                    enabled=True,
                    language="eng",
                    default_track=False,
                    forced_track=True,
                ),
            ],
        )

        argv = build_mkvmerge_command(src, tmp_out, config, temp_audio_path=tmp_audio)

        # Basic global prefix
        assert argv[:4] == ["mkvmerge", "--gui-mode", "-o", str(tmp_out)]
        assert argv[4:6] == ["--title", ""]

        audio_pos = argv.index(str(tmp_audio))
        source_pos = argv.index(str(src))
        assert audio_pos < source_pos

        # Guardrail #1: ALL temp audio options strictly precede str(tmp_audio),
        # including metadata strip flags to prevent .mka muxer tags leaking.
        audio_section = argv[6:audio_pos]
        assert "--no-global-tags" in audio_section
        assert "--no-track-tags" in audio_section
        assert "--no-attachments" in audio_section
        assert "--no-chapters" in audio_section
        assert "--track-name" in audio_section
        assert "0:English EAC3 5.1" in audio_section
        assert "--language" in audio_section
        assert "0:eng" in audio_section
        assert "--default-track-flag" in audio_section
        assert "0:yes" in audio_section
        assert "--forced-display-flag" in audio_section
        assert "0:no" in audio_section
        # Guardrail #2: --sync 0:42 precedes str(tmp_audio)
        sync_idx = audio_section.index("--sync")
        assert audio_section[sync_idx + 1] == "0:42"

        # Guardrail #1: ALL source options strictly between tmp_audio and str(src)
        source_section = argv[audio_pos + 1 : source_pos]
        assert "--no-global-tags" in source_section
        assert "--no-track-tags" in source_section
        assert "--no-attachments" in source_section
        assert "--no-chapters" in source_section
        assert source_section[source_section.index("--video-tracks") + 1] == "0"
        assert source_section[source_section.index("--audio-tracks") + 1] == "1"
        assert source_section[source_section.index("--subtitle-tracks") + 1] == "2"

        # Double-default prevention: original AAC (id=1) MUST have default=no
        assert "1:no" in source_section
        assert "1:yes" not in source_section
        assert "2:yes" in source_section  # forced flag on sub id=2

        # Global --track-order at end: Video (1:0), New EAC3 (0:0), Original AAC (1:1), Sub (1:2)
        order_idx = argv.index("--track-order")
        assert order_idx > source_pos
        assert argv[order_idx + 1] == "1:0,0:0,1:1,1:2"

    def test_empty_track_types_emit_no_flags_and_drop_original_aac(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Solo.mkv"
        tmp_out = tmp_path / "Solo.mkv.mkvforge.tmp"
        tmp_audio = tmp_path / "Solo.mkv.mkvforge.ac3"

        config = JobConfig(
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                target_codec="ac3",
                bitrate="448k",
                track_title="Hindi AC3 5.1",
                language="hin",
                drop_source_track=True,  # drops only source audio!
            ),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
                TrackEditConfig(id=2, type="subtitles", enabled=False),  # all subs unchecked!
            ],
        )

        argv = build_mkvmerge_command(src, tmp_out, config, temp_audio_path=tmp_audio)
        audio_pos = argv.index(str(tmp_audio))
        source_pos = argv.index(str(src))
        source_section = argv[audio_pos + 1 : source_pos]

        assert "--no-audio" in source_section
        assert "--audio-tracks" not in source_section
        assert "--no-subtitles" in source_section
        assert "--subtitle-tracks" not in source_section
        assert "--sync" not in argv[:audio_pos]  # 0ms delay -> no --sync flag
        # Track order places 0:0 immediately after Video (1:0)
        assert argv[argv.index("--track-order") + 1] == "1:0,0:0"

    def test_remux_only_without_stage1_uses_file_index_zero_and_preserves_order(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Clean.mkv"
        tmp_out = tmp_path / "Clean.mkv.mkvforge.tmp"

        config = JobConfig(
            container=ContainerMetadataConfig(title=None, strip_global_tags=True),
            audio_conversion=AudioConversionConfig(enabled=False),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                # User reordered audio id=3 ahead of audio id=1
                TrackEditConfig(id=3, type="audio", enabled=True, language="eng", default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, language="hin", default_track=False),
            ],
        )

        argv = build_mkvmerge_command(src, tmp_out, config, temp_audio_path=None)

        assert "--title" not in argv
        source_pos = argv.index(str(src))
        source_section = argv[:source_pos]
        assert "--no-global-tags" in source_section
        assert "--no-subtitles" in source_section
        assert source_section[source_section.index("--audio-tracks") + 1] == "3,1"
        # Default flag preserved on track 3 since no Stage 1 audio was added
        assert "3:yes" in source_section
        assert "1:no" in source_section
        assert argv[argv.index("--track-order") + 1] == "0:0,0:3,0:1"

    def test_error_conditions(self, tmp_path: Path) -> None:
        src = tmp_path / "a.mkv"
        out = tmp_path / "a.mkv.mkvforge.tmp"

        # Audio enabled without temp_audio_path
        with pytest.raises(ValueError, match="temp_audio_path"):
            build_mkvmerge_command(
                src,
                out,
                JobConfig(audio_conversion=AudioConversionConfig(enabled=True)),
                temp_audio_path=None,
            )

        # Zero kept tracks
        with pytest.raises(ValueError, match="zero kept tracks"):
            build_mkvmerge_command(
                src,
                out,
                JobConfig(
                    audio_conversion=AudioConversionConfig(enabled=False),
                    tracks=[TrackEditConfig(id=0, type="video", enabled=False)],
                ),
            )

    def test_selective_keep_attachment_ids_and_empty_keep_list(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "WithAttachments.mkv"
        out = tmp_path / "WithAttachments.mkv.mkvforge.tmp"

        # 1. Keeping subset [2, 3] emits --attachments 2,3 before source_path
        partial_cfg = JobConfig(
            container=ContainerMetadataConfig(
                strip_attachments=False,
                keep_attachment_ids=[2, 3],
            ),
            tracks=[TrackEditConfig(id=0, type="video", enabled=True)],
        )
        partial_argv = build_mkvmerge_command(src, out, partial_cfg)
        source_pos = partial_argv.index(str(src))
        source_section = partial_argv[:source_pos]
        assert "--no-attachments" not in source_section
        att_idx = source_section.index("--attachments")
        assert source_section[att_idx + 1] == "2,3"

        # 2. Empty keep_attachment_ids=[] emits --no-attachments
        none_cfg = JobConfig(
            container=ContainerMetadataConfig(
                strip_attachments=False,
                keep_attachment_ids=[],
            ),
            tracks=[TrackEditConfig(id=0, type="video", enabled=True)],
        )
        none_argv = build_mkvmerge_command(src, out, none_cfg)
        assert "--no-attachments" in none_argv[: none_argv.index(str(src))]
