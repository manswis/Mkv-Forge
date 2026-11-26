"""Exhaustive tests for Instant Header Edit (mkvpropedit), Modern Track Flags, and Audio Dynamics."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from pydantic import ValidationError
import pytest

from app.config import AppConstants
from app.models.schemas import (
    AudioConversionConfig,
    ContainerMetadataConfig,
    ExternalTrackConfig,
    JobConfig,
    ParsedTrackInfo,
    TrackEditConfig,
)
from app.services.ffmpeg_runner import (
    build_audio_dynamics_filter,
    build_ffmpeg_command,
    run_ffmpeg_transcode,
)
from app.services.mkv_builder import build_mkvmerge_command
from app.services.mkv_runner import execute_two_stage_pipeline
from app.services.mkvpropedit_runner import (
    MkvPropEditError,
    MkvPropEditResult,
    build_mkvpropedit_command,
    can_use_instant_header_edit,
    run_mkvpropedit_edit,
)
from app.services.probe_parser import parse_mkvmerge_json

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"


def _sample_source_tracks() -> list[ParsedTrackInfo]:
    return [
        ParsedTrackInfo(id=0, type="video", codec="HEVC", language="und", default_track=True),
        ParsedTrackInfo(
            id=1,
            type="audio",
            codec="EAC3",
            language="eng",
            default_track=True,
            audio_channels=6,
            audio_index=0,
        ),
        ParsedTrackInfo(
            id=2,
            type="audio",
            codec="AAC",
            language="eng",
            default_track=False,
            audio_channels=2,
            audio_index=1,
        ),
        ParsedTrackInfo(id=3, type="subtitles", codec="SRT", language="eng"),
    ]


def _sample_header_edit_config(**overrides) -> JobConfig:
    base = dict(
        container=ContainerMetadataConfig(title="Clean Movie Title"),
        tracks=[
            TrackEditConfig(id=0, type="video", enabled=True, default_track=True, flag_original=True),
            TrackEditConfig(id=1, type="audio", enabled=True, language="eng", default_track=True),
            TrackEditConfig(
                id=2,
                type="audio",
                enabled=True,
                track_name="Director Commentary",
                language="eng",
                default_track=False,
                flag_commentary=True,
            ),
            TrackEditConfig(
                id=3,
                type="subtitles",
                enabled=True,
                language="eng",
                forced_track=False,
                flag_hearing_impaired=True,
            ),
        ],
        output_mode="replace",
    )
    base.update(overrides)
    return JobConfig(**base)


# ---------------------------------------------------------------------------
# Feature #1: Instant Header Edit (mkvpropedit)
# ---------------------------------------------------------------------------


class TestInstantHeaderEditEligibilityAndCommand:
    """Verify strict eligibility matrix and deterministic `mkvpropedit` argv generation."""

    def test_eligible_when_only_mkv_headers_and_flags_change(self, tmp_path: Path) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = _sample_header_edit_config()
        eligible, reason = can_use_instant_header_edit(
            mkv,
            cfg,
            source_tracks=_sample_source_tracks(),
            binary_available=True,
        )
        assert eligible is True
        assert reason is None

    def test_ineligible_negative_matrix(self, tmp_path: Path) -> None:
        mkv = tmp_path / "Movie.mkv"
        mp4 = tmp_path / "Movie.mp4"
        tracks = _sample_source_tracks()
        base = _sample_header_edit_config()

        # 1. Opt-out flag
        assert can_use_instant_header_edit(
            mkv, base.model_copy(update={"prefer_instant_edit": False}), source_tracks=tracks, binary_available=True
        )[0] is False

        # 2. Non-MKV container
        assert can_use_instant_header_edit(
            mp4, base, source_tracks=tracks, binary_available=True
        )[0] is False

        # 3. Non-Replace output modes
        assert can_use_instant_header_edit(
            mkv, base.model_copy(update={"output_mode": "keep_both"}), source_tracks=tracks, binary_available=True
        )[0] is False
        assert can_use_instant_header_edit(
            mkv,
            base.model_copy(update={"output_mode": "save_as", "output_path": "Out/Movie.mkv"}),
            source_tracks=tracks,
            binary_available=True,
        )[0] is False

        # 4. Audio conversion enabled
        assert can_use_instant_header_edit(
            mkv,
            base.model_copy(
                update={"audio_conversion": AudioConversionConfig(enabled=True, target_codec="eac3", bitrate="640k")}
            ),
            source_tracks=tracks,
            binary_available=True,
        )[0] is False

        # 5. External tracks imported
        ext = ExternalTrackConfig(id="e1", kind="subtitles", source="storage", path="subs/en.srt")
        assert can_use_instant_header_edit(
            mkv, base.model_copy(update={"external_tracks": [ext]}), source_tracks=tracks, binary_available=True
        )[0] is False

        # 6. Any strip flag or partial attachment keep list
        for field_name, val in (
            ("strip_global_tags", True),
            ("strip_track_tags", True),
            ("strip_attachments", True),
            ("keep_attachment_ids", [1]),
            ("strip_chapters", True),
        ):
            assert can_use_instant_header_edit(
                mkv,
                base.model_copy(update={"container": ContainerMetadataConfig(**{field_name: val})}),
                source_tracks=tracks,
                binary_available=True,
            )[0] is False

        # 7. Dropped track
        dropped = list(base.tracks)
        dropped[2] = dropped[2].model_copy(update={"enabled": False})
        assert can_use_instant_header_edit(
            mkv, base.model_copy(update={"tracks": dropped}), source_tracks=tracks, binary_available=True
        )[0] is False

        # 8. Reordered tracks
        reordered = [base.tracks[0], base.tracks[2], base.tracks[1], base.tracks[3]]
        assert can_use_instant_header_edit(
            mkv, base.model_copy(update={"tracks": reordered}), source_tracks=tracks, binary_available=True
        )[0] is False

        # 9. Missing source_tracks manifest or missing mkvpropedit binary
        assert can_use_instant_header_edit(mkv, base, source_tracks=None, binary_available=True)[0] is False
        assert can_use_instant_header_edit(mkv, base, source_tracks=tracks, binary_available=False)[0] is False

    def test_build_mkvpropedit_command_selectors_delete_and_flags(self, tmp_path: Path) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = _sample_header_edit_config(
            container=ContainerMetadataConfig(title=""),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, track_name="", default_track=True, flag_original=True),
                TrackEditConfig(
                    id=1,
                    type="audio",
                    enabled=True,
                    track_name="Main Audio",
                    language="eng",
                    default_track=True,
                    flag_visual_impaired=False,
                ),
                TrackEditConfig(
                    id=2,
                    type="audio",
                    enabled=True,
                    track_name="Commentary",
                    language="eng",
                    default_track=False,
                    flag_commentary=True,
                ),
                TrackEditConfig(
                    id=3,
                    type="subtitles",
                    enabled=True,
                    language="eng",
                    forced_track=True,
                    flag_hearing_impaired=True,
                ),
            ],
        )

        argv = build_mkvpropedit_command(mkv, cfg)
        assert argv[:4] == [AppConstants.TOOL_MKVPROPEDIT, str(mkv), "--edit", "info"]
        assert argv[4:6] == ["--delete", "title"]

        # Verify 1-based per-type track selectors (v1, a1, a2, s1)
        assert "track:v1" in argv
        assert "track:a1" in argv
        assert "track:a2" in argv
        assert "track:s1" in argv
        assert "name=Main Audio" in argv
        assert "--delete" in argv and "name" in argv
        assert "flag-original=1" in argv
        assert "flag-commentary=1" in argv
        assert "flag-hearing-impaired=1" in argv
        assert "flag-visual-impaired=0" in argv

    def test_build_mkvpropedit_command_diff_only_preserves_untouched_tracks_and_ietf_languages(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        source_tracks = [
            ParsedTrackInfo(id=0, type="video", codec="HEVC", language="und", default_track=True),
            ParsedTrackInfo(
                id=1,
                type="audio",
                codec="EAC3",
                language="eng",
                language_ietf="en-US",
                track_name="English 5.1",
                default_track=True,
                audio_channels=6,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=2,
                type="audio",
                codec="AAC",
                language="por",
                language_ietf="pt-BR",
                track_name="",
                default_track=False,
                audio_channels=2,
                audio_index=1,
            ),
            ParsedTrackInfo(
                id=3,
                type="subtitles",
                codec="SRT",
                language="eng",
                track_name="",
                default_track=False,
                forced_track=False,
                flag_hearing_impaired=False,
            ),
        ]

        # 1. User ONLY toggles SDH / HI on Subtitle #3 -> untouched tracks & pt-BR/en-US stay untouched
        sdh_only_cfg = JobConfig(
            container=ContainerMetadataConfig(title=None),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, track_name="", language="und", default_track=True),
                TrackEditConfig(
                    id=1, type="audio", enabled=True, track_name="English 5.1", language="eng", default_track=True
                ),
                TrackEditConfig(
                    id=2, type="audio", enabled=True, track_name="", language="por", default_track=False
                ),
                TrackEditConfig(
                    id=3,
                    type="subtitles",
                    enabled=True,
                    track_name="",
                    language="eng",
                    default_track=False,
                    forced_track=False,
                    flag_hearing_impaired=True,
                ),
            ],
            output_mode="replace",
        )
        argv_sdh = build_mkvpropedit_command(mkv, sdh_only_cfg, source_tracks=source_tracks)
        assert argv_sdh == [
            AppConstants.TOOL_MKVPROPEDIT,
            str(mkv),
            "--edit",
            "track:s1",
            "--set",
            "flag-hearing-impaired=1",
        ]

        # 2. User ONLY changes Audio #1 language eng -> spa
        lang_only_cfg = JobConfig(
            container=ContainerMetadataConfig(title=None),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, track_name="", language="und", default_track=True),
                TrackEditConfig(
                    id=1, type="audio", enabled=True, track_name="English 5.1", language="spa", default_track=True
                ),
                TrackEditConfig(
                    id=2, type="audio", enabled=True, track_name="", language="por", default_track=False
                ),
                TrackEditConfig(
                    id=3, type="subtitles", enabled=True, track_name="", language="eng", default_track=False
                ),
            ],
            output_mode="replace",
        )
        argv_lang = build_mkvpropedit_command(mkv, lang_only_cfg, source_tracks=source_tracks)
        assert argv_lang == [
            AppConstants.TOOL_MKVPROPEDIT,
            str(mkv),
            "--edit",
            "track:a1",
            "--set",
            "language=spa",
        ]

    async def test_run_mkvpropedit_edit_rejects_missing_and_readonly_files(
        self, tmp_path: Path
    ) -> None:
        cfg = _sample_header_edit_config()
        with pytest.raises(MkvPropEditError, match="not found"):
            await run_mkvpropedit_edit(tmp_path / "missing.mkv", cfg)

        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            ro_mkv = tmp_path / "readonly.mkv"
            ro_mkv.write_bytes(b"fake-mkv")
            ro_mkv.chmod(stat.S_IRUSR)
            try:
                with pytest.raises(MkvPropEditError, match="read-only"):
                    await run_mkvpropedit_edit(ro_mkv, cfg)
            finally:
                ro_mkv.chmod(stat.S_IRWXU)

    async def test_pipeline_routes_eligible_job_to_mkvpropedit_without_remux(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()
        mkv = storage / "Movie.mkv"
        mkv.write_bytes(b"fake-mkv-content")

        stages: list[tuple[str, str]] = []
        probed: list[str] = []
        propedit_called: list[Path] = []
        remux_called: list[Path] = []

        monkeypatch.setattr("app.services.mkvpropedit_runner.is_mkvpropedit_available", lambda: True)

        async def _fake_propedit(source_path: Path, config: JobConfig, **_kwargs) -> MkvPropEditResult:
            propedit_called.append(source_path)
            return MkvPropEditResult(target_path=source_path, exit_code=0, warnings=[])

        async def _fake_remux(source_path: Path, *_args, **_kwargs):
            remux_called.append(source_path)
            raise AssertionError("run_mkvmerge_remux should not be called for instant header edit")

        class _FakeScanner:
            async def probe_single_file(self, rel_path: str) -> None:
                probed.append(rel_path)

        monkeypatch.setattr("app.services.mkv_runner.run_mkvpropedit_edit", _fake_propedit)
        monkeypatch.setattr("app.services.mkv_runner.run_mkvmerge_remux", _fake_remux)

        result = await execute_two_stage_pipeline(
            mkv,
            _sample_header_edit_config(),
            storage_root=storage,
            scanner=_FakeScanner(),  # type: ignore[arg-type]
            source_tracks=_sample_source_tracks(),
            on_stage=lambda stage, label: stages.append((stage, label)),
        )

        assert propedit_called == [mkv]
        assert remux_called == []
        assert stages == [("header_edit", "Instant Header Edit (mkvpropedit)")]
        assert probed == ["Movie.mkv"]
        assert result.output_path == mkv
        assert result.output_rel_filepath == "Movie.mkv"

    async def test_pipeline_logs_instant_edit_skip_reason_only_for_mkv_replace_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.mkv_runner import MkvRemuxResult

        storage = tmp_path / "storage"
        storage.mkdir()
        mkv = storage / "Movie.mkv"
        mkv.write_bytes(b"source-mkv-bytes")
        mp4 = storage / "Clip.mp4"
        mp4.write_bytes(b"source-mp4-bytes")

        async def _fake_remux(_source: Path, tmp_out: Path, *_args, **_kwargs) -> MkvRemuxResult:
            tmp_out.write_bytes(b"remuxed-output")
            return MkvRemuxResult(
                output_tmp_path=tmp_out, exit_code=0, warnings=[], log_lines=[]
            )

        monkeypatch.setattr("app.services.mkv_runner.run_preflight_checks", lambda *_a, **_k: None)
        monkeypatch.setattr("app.services.mkv_runner.run_mkvmerge_remux", _fake_remux)

        # 1. MKV Replace job dropping a track -> logs exact skip reason to on_log (Watch Live Logs)
        drop_cfg = _sample_header_edit_config(
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, language="und", default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, language="eng", default_track=True),
                TrackEditConfig(id=2, type="audio", enabled=False, language="eng"),
                TrackEditConfig(id=3, type="subtitles", enabled=True, language="eng"),
            ]
        )
        replace_logs: list[str] = []
        await execute_two_stage_pipeline(
            mkv,
            drop_cfg,
            storage_root=storage,
            source_tracks=_sample_source_tracks(),
            on_log=replace_logs.append,
        )
        assert any(
            "Instant header edit (mkvpropedit) skipped (dropping tracks requires full remux); running full mkvmerge remux."
            in line
            for line in replace_logs
        )

        # 2. Keep-Both MKV job -> does NOT emit instant-edit skip noise
        keep_both_logs: list[str] = []
        await execute_two_stage_pipeline(
            mkv,
            _sample_header_edit_config(output_mode="keep_both"),
            storage_root=storage,
            source_tracks=_sample_source_tracks(),
            on_log=keep_both_logs.append,
        )
        assert not any("Instant header edit (mkvpropedit) skipped" in line for line in keep_both_logs)

        # 3. MP4 Replace job -> does NOT emit instant-edit skip noise
        mp4_logs: list[str] = []
        await execute_two_stage_pipeline(
            mp4,
            _sample_header_edit_config(output_mode="replace"),
            storage_root=storage,
            source_tracks=_sample_source_tracks(),
            on_log=mp4_logs.append,
        )
        assert not any("Instant header edit (mkvpropedit) skipped" in line for line in mp4_logs)


# ---------------------------------------------------------------------------
# Feature #2: Modern Matroska Track Flags
# ---------------------------------------------------------------------------


class TestModernMatroskaTrackFlags:
    """Verify probe parsing and conditional mkvmerge flag emission."""

    def test_probe_parser_reads_modern_track_flags_with_safe_defaults(self) -> None:
        payload = {
            "container": {"properties": {"duration": 60_000_000_000}},
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC",
                    "properties": {"flag_original": True, "default_track": True},
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "EAC3",
                    "properties": {
                        "audio_channels": 2,
                        "flag_commentary": True,
                        "flag_visual_impaired": True,
                    },
                },
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "SRT",
                    "properties": {"flag_hearing_impaired": True},
                },
            ],
        }

        parsed = parse_mkvmerge_json(payload)
        assert parsed.tracks[0].flag_original is True
        assert parsed.tracks[0].flag_commentary is False
        assert parsed.tracks[1].flag_commentary is True
        assert parsed.tracks[1].flag_visual_impaired is True
        assert parsed.tracks[1].flag_hearing_impaired is False
        assert parsed.tracks[2].flag_hearing_impaired is True

    def test_mkvmerge_emits_modern_flags_only_when_specified(self, tmp_path: Path) -> None:
        src = tmp_path / "in.mkv"
        out = tmp_path / "out.mkv"

        # Legacy TrackEditConfig omitting modern flags emits zero extra flags
        legacy_cfg = JobConfig(
            tracks=[TrackEditConfig(id=0, type="video", enabled=True, default_track=True)]
        )
        legacy_argv = build_mkvmerge_command(src, out, legacy_cfg)
        assert "--original-flag" not in legacy_argv
        assert "--commentary-flag" not in legacy_argv
        assert "--hearing-impaired-flag" not in legacy_argv
        assert "--visual-impaired-flag" not in legacy_argv

        # Explicit modern flags emit exact yes/no options before source path
        modern_cfg = JobConfig(
            tracks=[
                TrackEditConfig(
                    id=0,
                    type="video",
                    enabled=True,
                    default_track=True,
                    flag_original=True,
                ),
                TrackEditConfig(
                    id=1,
                    type="audio",
                    enabled=True,
                    flag_commentary=True,
                    flag_visual_impaired=False,
                ),
                TrackEditConfig(
                    id=2,
                    type="subtitles",
                    enabled=True,
                    flag_hearing_impaired=True,
                ),
            ]
        )
        modern_argv = build_mkvmerge_command(src, out, modern_cfg)
        src_idx = modern_argv.index(str(src))
        assert modern_argv.index("--original-flag") < src_idx
        assert "0:yes" in modern_argv
        assert "--commentary-flag" in modern_argv and "1:yes" in modern_argv
        assert "--visual-impaired-flag" in modern_argv and "1:no" in modern_argv
        assert "--hearing-impaired-flag" in modern_argv and "2:yes" in modern_argv


# ---------------------------------------------------------------------------
# Feature #3: Audio Dynamics (Dialogue Boost & Night Mode)
# ---------------------------------------------------------------------------


class TestAudioDynamicsPresetsAndRealFfmpeg:
    """Verify schema validation, FFmpeg filter chains, and real FFmpeg execution."""

    def test_schema_blocks_dynamics_for_lossless_flac_and_invalid_values(self) -> None:
        # Allowed on lossy codecs
        ok = AudioConversionConfig(
            enabled=True,
            target_codec="eac3",
            bitrate="256k",
            target_channels=2,
            audio_dynamics="night_mode",
        )
        assert ok.audio_dynamics == "night_mode"

        # Blocked on lossless FLAC
        with pytest.raises(ValidationError, match="lossless"):
            AudioConversionConfig(
                enabled=True,
                target_codec="flac",
                bitrate=None,
                audio_dynamics="dialogue_boost",
            )

        # Unknown mode rejected
        with pytest.raises(ValidationError):
            AudioConversionConfig(
                enabled=True,
                target_codec="aac",
                bitrate="192k",
                audio_dynamics="mega_bass",  # type: ignore[arg-type]
            )

    def test_build_audio_dynamics_filter_and_ffmpeg_argv(self, tmp_path: Path) -> None:
        # Original -> None (no -af flag)
        assert (
            build_audio_dynamics_filter(
                "original", source_channels=6, target_channels=2, target_codec="eac3"
            )
            is None
        )
        # FLAC -> None even if passed
        assert (
            build_audio_dynamics_filter(
                "night_mode", source_channels=6, target_channels=6, target_codec="flac"
            )
            is None
        )
        # Dialogue Boost on 5.1 -> 5.1 (no stereo downmix prefix)
        assert (
            build_audio_dynamics_filter(
                "dialogue_boost", source_channels=6, target_channels=6, target_codec="eac3"
            )
            == AppConstants.AUDIO_DYNAMICS_DIALOGUE_FILTER
        )
        # Dialogue Boost on 5.1 -> 2.0 (includes +3 dB center-channel downmix prefix)
        assert (
            build_audio_dynamics_filter(
                "dialogue_boost", source_channels=6, target_channels=2, target_codec="eac3"
            )
            == f"{AppConstants.AUDIO_DYNAMICS_DOWNMIX_PREFIX},{AppConstants.AUDIO_DYNAMICS_DIALOGUE_FILTER}"
        )
        # Night Mode on 7.1 -> 2.0
        assert (
            build_audio_dynamics_filter(
                "night_mode", source_channels=8, target_channels=2, target_codec="aac"
            )
            == f"{AppConstants.AUDIO_DYNAMICS_DOWNMIX_PREFIX},{AppConstants.AUDIO_DYNAMICS_NIGHT_FILTER}"
        )

        # Invalid mode raises ValueError
        with pytest.raises(ValueError, match="Unsupported audio_dynamics"):
            build_audio_dynamics_filter(
                "invalid", source_channels=2, target_channels=2, target_codec="aac"
            )

        # Verify build_ffmpeg_command omits -af for original and includes -af for night_mode
        src = tmp_path / "in.mkv"
        out = tmp_path / "out.eac3"
        orig_cmd = build_ffmpeg_command(
            src, out, audio_index=0, target_codec="eac3", bitrate="256k", channels=6, requested_channels=2
        )
        assert "-af" not in orig_cmd

        night_cmd = build_ffmpeg_command(
            src,
            out,
            audio_index=0,
            target_codec="eac3",
            bitrate="256k",
            channels=6,
            requested_channels=2,
            audio_dynamics="night_mode",
        )
        af_idx = night_cmd.index("-af")
        assert night_cmd[af_idx + 1] == (
            f"{AppConstants.AUDIO_DYNAMICS_DOWNMIX_PREFIX},{AppConstants.AUDIO_DYNAMICS_NIGHT_FILTER}"
        )

        # Dialogue Boost on AAC MUST pin -ar 48000 so loudnorm's internal 192 kHz never upsamples AAC to 96 kHz
        aac_db_cmd = build_ffmpeg_command(
            src,
            tmp_path / "out.mka",
            audio_index=0,
            target_codec="aac",
            bitrate="256k",
            channels=6,
            sample_rate=48_000,
            requested_channels=2,
            audio_dynamics="dialogue_boost",
        )
        ar_idx = aac_db_cmd.index("-ar")
        assert aac_db_cmd[ar_idx + 1] == "48000"

    async def test_real_ffmpeg_transcode_with_dialogue_boost_and_night_mode(
        self, tmp_path: Path
    ) -> None:
        if shutil.which(AppConstants.TOOL_FFMPEG) is None or shutil.which(AppConstants.TOOL_FFPROBE) is None:
            pytest.skip("ffmpeg/ffprobe not installed locally")

        # Synthesize a real 5.1 AAC input
        src_51 = tmp_path / "source_51.mka"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:duration=0.4:seed=1",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:duration=0.4:seed=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=500:duration=0.4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=80:duration=0.4",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:duration=0.4:seed=5",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:duration=0.4:seed=6",
                "-filter_complex",
                "[0][1][2][3][4][5]join=inputs=6:channel_layout=5.1[a]",
                "-map",
                "[a]",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-b:a",
                "256k",
                str(src_51),
            ],
            check=True,
        )

        for mode in ("dialogue_boost", "night_mode"):
            out_eac3 = tmp_path / f"out_{mode}.eac3"
            res = await run_ffmpeg_transcode(
                src_51,
                out_eac3,
                audio_index=0,
                target_codec="eac3",
                bitrate="256k",
                target_channels=2,
                audio_dynamics=mode,
            )
            assert res.channels == 2
            assert out_eac3.is_file() and out_eac3.stat().st_size > 0

        # Regression lock: AAC + dialogue_boost (loudnorm) must stay 48000 Hz (never upsample to 96000 Hz)
        for ch_target, br in ((2, "256k"), (6, "448k")):
            out_aac = tmp_path / f"out_aac_{ch_target}ch_db.mka"
            res_aac = await run_ffmpeg_transcode(
                src_51,
                out_aac,
                audio_index=0,
                target_codec="aac",
                bitrate=br,
                target_channels=ch_target,
                audio_dynamics="dialogue_boost",
            )
            assert res_aac.channels == ch_target
            probe = subprocess.run(
                [
                    AppConstants.TOOL_FFPROBE,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels",
                    "-print_format",
                    "json",
                    str(out_aac),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            assert stream["codec_name"] == "aac"
            assert int(stream["sample_rate"]) == 48_000
            assert int(stream["channels"]) == ch_target


# ---------------------------------------------------------------------------
# Frontend JS (Node VM) & HTML Landmarks for #1, #2, and #3
# ---------------------------------------------------------------------------


def test_frontend_editor_helpers_and_html_landmarks() -> None:
    """Verify JS editor state, instant-edit eligibility, track flags, audio dynamics, and HTML."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "editorIsInstantHeaderEdit" in html
    assert "Apply Instant Edit" in html
    assert "SDH / HI" in html
    assert "toggleTrackCommentaryFlag" in html
    assert "flag_visual_impaired" in html
    assert "Volume / Dynamics" in html
    assert "onDraftAudioDynamicsChange" in html

    js_dir = str(STATIC_DIR / "js")
    script = f"""
    const fs = require("fs");
    const vm = require("vm");
    const path = require("path");
    const jsDir = {json.dumps(js_dir)};
    const ctx = {{ window: null, globalThis: null }};
    ctx.window = ctx;
    ctx.globalThis = ctx;
    vm.createContext(ctx);
    for (const f of ["constants.js", "formatters.js", "folder_tree.js", "editor.js"]) {{
      vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
    }}
    const Ed = ctx.MKVForgeEditor;
    const file = {{
      id: 9,
      filename: "Movie.mkv",
      filepath: "Movies/Movie.mkv",
      folder: "/Movies",
      container_title: "Old Title",
      has_global_tags: false,
      has_track_tags: false,
      attachments_count: 0,
      chapters_count: 0,
      tracks: [
        {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
        {{ id: 1, type: "audio", codec: "EAC3", audio_channels: 6, audio_index: 0, language: "eng", default_track: true }},
        {{ id: 2, type: "subtitles", codec: "SRT", language: "eng", default_track: false, forced_track: false }}
      ]
    }};

    const state = Ed.createEditorState(file, {{
      default_output_mode: "replace",
      default_remove_all_metadata: false
    }});

    // Initially no changes -> not eligible
    if (Ed.hasPendingChanges(state, file)) throw new Error("Expected no pending changes initially");
    if (Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Expected not instant-eligible without changes");

    // Toggle modern SDH flag on subtitle -> pending changes + instant-edit eligible!
    state.tracks[2].flag_hearing_impaired = true;
    if (!Ed.hasPendingChanges(state, file)) throw new Error("Expected pending changes after SDH toggle");
    if (!Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Expected instant-edit eligible for MKV Replace flag edit");

    const payload1 = Ed.buildJobRequestPayload(state, file);
    if (payload1.config.tracks[2].flag_hearing_impaired !== true) throw new Error("Expected flag_hearing_impaired=true in payload");
    if (payload1.config.tracks[1].flag_commentary !== undefined) throw new Error("Unchanged false flags should stay omitted");

    // Switching to Keep Both disables instant edit
    state.outputMode = "keep_both";
    if (Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Keep Both must not be instant-eligible");
    state.outputMode = "replace";

    // Dropping a track disables instant edit
    state.tracks[2].enabled = false;
    if (Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Dropping track must not be instant-eligible");
    state.tracks[2].enabled = true;

    // Audio Dynamics title formatting & payload serialization
    const nightTitle = Ed.buildDefaultAudioTitle("eng", "eac3", 2, "256k", "detailed", "night_mode");
    if (!nightTitle.includes("Night Mode")) throw new Error("Expected Night Mode in title: " + nightTitle);
    const origTitle = Ed.buildDefaultAudioTitle("eng", "eac3", 6, "640k", "detailed", "original");
    if (origTitle !== "EAC3 5.1 • 640k • eng • muxed") throw new Error("Original title changed unexpectedly: " + origTitle);

    state.audioFix.enabled = true;
    state.audioFix.sourceTrackId = 1;
    state.audioFix.targetCodec = "eac3";
    state.audioFix.targetChannels = 2;
    state.audioFix.bitrate = "256k";
    state.audioFix.audioDynamics = "dialogue_boost";
    const payload2 = Ed.buildJobRequestPayload(state, file);
    if (payload2.config.audio_conversion.audio_dynamics !== "dialogue_boost") {{
      throw new Error("Expected dialogue_boost in audio_conversion payload");
    }}
    if (Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Audio conversion must not be instant-eligible");

    // Language options helper: preserves ISO 639-2/B (fre, dut, rum), BCP-47, and unknown codes without fallback to 'eng'
    const baseLen = ctx.MKVForgeConstants.EDITOR.LANGUAGE_OPTIONS.length;
    const freOpts = Ed.getLanguageOptionsFor("fre", "fre");
    if (freOpts[0].code !== "fre" || freOpts[0].label !== "French (fre)") {{
      throw new Error("Expected French (fre) prepended, got: " + JSON.stringify(freOpts[0]));
    }}
    if (freOpts.length !== baseLen + 1) throw new Error("Duplicate fre option generated");

    const dutOpts = Ed.getLanguageOptionsFor("dut");
    if (dutOpts[0].code !== "dut" || dutOpts[0].label !== "Dutch (dut)") {{
      throw new Error("Expected Dutch (dut) prepended, got: " + JSON.stringify(dutOpts[0]));
    }}

    const rumOpts = Ed.getLanguageOptionsFor("rum");
    if (rumOpts[0].code !== "rum" || rumOpts[0].label !== "Romanian (rum)") {{
      throw new Error("Expected Romanian (rum) prepended, got: " + JSON.stringify(rumOpts[0]));
    }}

    // Changing track.language from 'fre' to 'eng' keeps 'fre' (original_language) selectable to revert cleanly
    const revertedOpts = Ed.getLanguageOptionsFor("eng", "fre");
    if (!revertedOpts.some((o) => o.code === "fre") || !revertedOpts.some((o) => o.code === "eng")) {{
      throw new Error("Expected both eng and original fre in options");
    }}
    if (revertedOpts.filter((o) => o.code === "eng").length !== 1) {{
      throw new Error("Standard code eng must not be duplicated");
    }}

    // BCP-47 & unknown custom codes + negative/null/empty inputs
    const bcpOpts = Ed.getLanguageOptionsFor("pt-BR", null, undefined, "", "   ", "tlh");
    if (bcpOpts[0].code !== "pt-BR" || bcpOpts[1].code !== "tlh" || bcpOpts.length !== baseLen + 2) {{
      throw new Error("Unexpected BCP-47/unknown options: " + JSON.stringify(bcpOpts.slice(0, 3)));
    }}
    const standardOnly = Ed.getLanguageOptionsFor("eng", "und", null, "");
    if (standardOnly.length !== baseLen) {{
      throw new Error("Standard/empty codes must not alter base option list length");
    }}
    if (ctx.MKVForgeConstants.EDITOR.LANGUAGE_OPTIONS.length !== baseLen) {{
      throw new Error("LANGUAGE_OPTIONS constant was mutated");
    }}

    // Track state preserves fre/dut/rum verbatim with zero phantom pending changes
    const intlFile = {{
      ...file,
      tracks: [
        {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
        {{ id: 4, type: "subtitles", codec: "SubRip/SRT", language: "fre", default_track: false }},
        {{ id: 5, type: "subtitles", codec: "SubRip/SRT", language: "dut", default_track: false }},
        {{ id: 6, type: "subtitles", codec: "SubRip/SRT", language: "rum", default_track: false }},
      ]
    }};
    const intlState = Ed.createEditorState(intlFile, {{ default_output_mode: "replace", default_remove_all_metadata: false }});
    if (intlState.tracks[1].language !== "fre" || intlState.tracks[2].language !== "dut" || intlState.tracks[3].language !== "rum") {{
      throw new Error("Track languages were altered on load");
    }}
    if (Ed.hasPendingChanges(intlState, intlFile)) {{
      throw new Error("Untouched fre/dut/rum tracks must not report pending changes");
    }}

    console.log("FRONTEND_VM_OK");
    """
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FRONTEND_VM_OK" in proc.stdout
