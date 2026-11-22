"""Exhaustive unit tests for Speaker & Audio System Test Generator (Python + test_media.js)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.config import AppConstants
from app.models.schemas import TestMediaGenerateRequest
from app.services.test_media_generator import (
    TestMediaTimeoutError,
    TestMediaValidationError,
    build_audio_filter_complex,
    build_speaker_timeline,
    build_test_ffmpeg_command,
    build_test_mkvmerge_command,
    build_test_srt_content,
    build_video_filter_graph,
    generate_test_media,
    validate_test_media_spec,
)

JS_DIR = Path(__file__).resolve().parents[2] / "app" / "static" / "js"


def _run_node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestSpeakerTestValidationAndBuilders:
    """Verify validation hard blocks, soft warnings, pop-free fades, and CLI builders."""

    @pytest.mark.parametrize(
        ("codec", "layout", "expected_channels"),
        [
            ("eac3", "1.0", 1),
            ("eac3", "2.0", 2),
            ("eac3", "2.1", 3),
            ("eac3", "5.1", 6),
            ("ac3", "2.1", 3),
            ("ac3", "5.1", 6),
            ("aac", "2.1", 3),
            ("aac", "7.1", 8),
            ("opus", "1.0", 1),
            ("opus", "5.1", 6),
            ("opus", "7.1", 8),
            ("flac", "2.1", 3),
            ("flac", "7.1", 8),
            ("pcm", "2.1", 3),
            ("pcm", "5.1", 6),
            ("pcm", "7.1", 8),
        ],
    )
    def test_valid_codec_and_layout_combinations_pass(
        self,
        tmp_path: Path,
        codec: str,
        layout: str,
        expected_channels: int,
    ) -> None:
        req = TestMediaGenerateRequest(
            output_mode="video",
            codec=codec,
            layout=layout,
            destination_dir="/",
        )
        spec = validate_test_media_spec(req, storage_root=tmp_path)
        assert spec.channels == expected_channels
        assert spec.container == "mkv"
        assert spec.destination_dir == tmp_path.resolve()
        if codec in ("flac", "pcm"):
            assert spec.bitrate is None
        else:
            assert spec.bitrate is not None

    def test_hard_blocks_reject_invalid_combinations_and_paths(
        self, tmp_path: Path
    ) -> None:
        # Dolby E-AC-3 and AC-3 capped at 5.1
        for dolby in ("eac3", "ac3"):
            with pytest.raises(TestMediaValidationError, match="up to 5.1"):
                validate_test_media_spec(
                    TestMediaGenerateRequest(codec=dolby, layout="7.1"),
                    storage_root=tmp_path,
                )

        # Opus RFC 7845 rejects 2.1
        with pytest.raises(TestMediaValidationError, match="3.0, not 2.1"):
            validate_test_media_spec(
                TestMediaGenerateRequest(codec="opus", layout="2.1"),
                storage_root=tmp_path,
            )

        # Illegal audio-only container for codec
        with pytest.raises(TestMediaValidationError, match="not compatible"):
            validate_test_media_spec(
                TestMediaGenerateRequest(
                    output_mode="audio", codec="flac", layout="5.1", container="m4a"
                ),
                storage_root=tmp_path,
            )

        with pytest.raises(TestMediaValidationError, match="not compatible"):
            validate_test_media_spec(
                TestMediaGenerateRequest(
                    output_mode="audio", codec="pcm", layout="2.0", container="ac3"
                ),
                storage_root=tmp_path,
            )

        # Video mode rejects non-mkv container
        with pytest.raises(TestMediaValidationError, match="Matroska"):
            validate_test_media_spec(
                TestMediaGenerateRequest(
                    output_mode="video", codec="eac3", layout="5.1", container="mp4"
                ),
                storage_root=tmp_path,
            )

        # Path traversal & nonexistent folder blocked (never auto-created)
        with pytest.raises(TestMediaValidationError):
            validate_test_media_spec(
                TestMediaGenerateRequest(destination_dir="../../etc"),
                storage_root=tmp_path,
            )

        with pytest.raises(TestMediaValidationError, match="does not exist"):
            validate_test_media_spec(
                TestMediaGenerateRequest(destination_dir="/NonExistentFolder"),
                storage_root=tmp_path,
            )

        # Unsafe filename characters / reserved marker
        for bad_name in ("../evil.mkv", "bad/name.mkv", "a.mkvforge.mkv", "bad$name.mkv"):
            with pytest.raises(TestMediaValidationError):
                validate_test_media_spec(
                    TestMediaGenerateRequest(filename=bad_name),
                    storage_root=tmp_path,
                )

    def test_custom_tone_and_gap_durations_scale_timeline_and_enforce_bounds(
        self, tmp_path: Path
    ) -> None:
        spec = validate_test_media_spec(
            TestMediaGenerateRequest(
                output_mode="video",
                codec="eac3",
                layout="2.0",
                tone_duration_sec=5.0,
                gap_duration_sec=1.0,
            ),
            storage_root=tmp_path,
        )
        assert spec.tone_duration_sec == 5.0
        assert spec.gap_duration_sec == 1.0
        slots, total_sec = build_speaker_timeline(
            "2.0",
            tone_duration_sec=spec.tone_duration_sec,
            gap_duration_sec=spec.gap_duration_sec,
        )
        # 2 speakers + 1 ALL balance slot = 3 * (5.0 + 1.0) = 18.0s
        assert len(slots) == 3
        assert slots[0].start_sec == 0.0 and slots[0].end_sec == 5.0
        assert slots[1].start_sec == 6.0 and slots[1].end_sec == 11.0
        assert slots[2].start_sec == 12.0 and slots[2].end_sec == 17.0
        assert total_sec == 18.0

        # Out-of-bounds values rejected by Pydantic validation
        with pytest.raises(ValueError):
            TestMediaGenerateRequest(tone_duration_sec=0.1)
        with pytest.raises(ValueError):
            TestMediaGenerateRequest(tone_duration_sec=25.0)
        with pytest.raises(ValueError):
            TestMediaGenerateRequest(gap_duration_sec=-0.5)
        with pytest.raises(ValueError):
            TestMediaGenerateRequest(gap_duration_sec=5.0)

    def test_soft_warnings_generated_for_hardware_caveats(self, tmp_path: Path) -> None:
        spec_pcm = validate_test_media_spec(
            TestMediaGenerateRequest(output_mode="audio", codec="pcm", layout="5.1", container="wav"),
            storage_root=tmp_path,
        )
        assert any("Optical SPDIF" in w for w in spec_pcm.warnings)
        assert any("Video + Audio" in w for w in spec_pcm.warnings)

        spec_aac = validate_test_media_spec(
            TestMediaGenerateRequest(output_mode="video", codec="aac", layout="5.1"),
            storage_root=tmp_path,
        )
        assert any("downmix" in w for w in spec_aac.warnings)

    def test_audio_and_video_filter_graphs_include_anti_pop_fades_and_zero_fonts(
        self, tmp_path: Path
    ) -> None:
        spec = validate_test_media_spec(
            TestMediaGenerateRequest(
                output_mode="video",
                codec="eac3",
                layout="5.1",
                signal_type="tones",
            ),
            storage_root=tmp_path,
        )
        slots, total_sec = build_speaker_timeline("5.1")
        # 6 speaker slots + 1 all-channel balance slot
        assert len(slots) == 7
        assert total_sec > 15.0

        audio_fc = build_audio_filter_complex(slots, total_sec, spec=spec)
        # Anti-pop 25ms fade-in and fade-out on every solo & all-channel segment
        assert audio_fc.count(f"afade=t=in:st=0:d={AppConstants.TEST_MEDIA_FADE_DURATION_SEC}") == 12
        assert f"d={AppConstants.TEST_MEDIA_FADE_DURATION_SEC}" in audio_fc
        assert f"volume={AppConstants.TEST_MEDIA_TONE_VOLUME_DB}dB" in audio_fc
        assert f"volume={AppConstants.TEST_MEDIA_LFE_VOLUME_DB}dB" in audio_fc
        assert "concat=n=" in audio_fc
        assert "join=inputs=6:channel_layout=5.1" in audio_fc
        assert (
            f"asetnsamples=n={AppConstants.TEST_MEDIA_ENCODER_FRAME_SIZE_MAP['eac3']}:p=1,asetpts=N/SR/TB[aout]"
            in audio_fc
        )

        video_fc = build_video_filter_graph(slots, total_sec, layout="5.1")
        assert "drawbox=" in video_fc
        assert "drawtext" not in video_fc  # zero font dependency

        srt = build_test_srt_content(slots, codec="eac3", layout="5.1", signal_type="tones")
        assert "Front Left (FL)" in srt
        assert "Subwoofer (LFE" in srt
        assert "ALL CHANNELS (5.1)" in srt

        ff_cmd = build_test_ffmpeg_command(
            tmp_path / "raw.mkv", spec=spec, slots=slots, total_sec=total_sec
        )
        assert ff_cmd[0] == "ffmpeg"
        assert "-c:v" in ff_cmd and "libx264" in ff_cmd
        assert "-c:a" in ff_cmd and "eac3" in ff_cmd
        assert "-c:s" not in ff_cmd
        assert "0:s:0" not in ff_cmd

        # Video + subtitles enabled includes forced SRT stream
        ff_cmd_with_srt = build_test_ffmpeg_command(
            tmp_path / "with_srt.mkv",
            spec=spec,
            slots=slots,
            total_sec=total_sec,
            srt_path=tmp_path / "labels.srt",
        )
        assert "-c:s" in ff_cmd_with_srt and "srt" in ff_cmd_with_srt
        assert "default+forced" in ff_cmd_with_srt

        # Validating include_subtitles default True for video, False when disabled, False for audio
        assert spec.include_subtitles is True
        spec_no_sub = validate_test_media_spec(
            TestMediaGenerateRequest(
                output_mode="video",
                codec="eac3",
                layout="5.1",
                include_subtitles=False,
            ),
            storage_root=tmp_path,
        )
        assert spec_no_sub.include_subtitles is False
        spec_audio = validate_test_media_spec(
            TestMediaGenerateRequest(
                output_mode="audio",
                codec="flac",
                layout="2.0",
                include_subtitles=True,
            ),
            storage_root=tmp_path,
        )
        assert spec_audio.include_subtitles is False

        mkv_cmd = build_test_mkvmerge_command(
            tmp_path / "raw.mkv", tmp_path / "labels.srt", tmp_path / "out.tmp", spec=spec
        )
        assert mkv_cmd[0] == "mkvmerge"
        assert "--forced-display-flag" in mkv_cmd
        assert "0:yes" in mkv_cmd

    def test_pink_noise_lfe_applies_lowpass_and_conservative_level(
        self, tmp_path: Path
    ) -> None:
        spec = validate_test_media_spec(
            TestMediaGenerateRequest(
                output_mode="audio",
                codec="pcm",
                layout="2.1",
                container="wav",
                signal_type="pink_noise",
            ),
            storage_root=tmp_path,
        )
        slots, total_sec = build_speaker_timeline("2.1")
        audio_fc = build_audio_filter_complex(slots, total_sec, spec=spec)
        assert "anoisesrc=color=pink" in audio_fc
        assert f"lowpass=f={AppConstants.TEST_MEDIA_LFE_LOWPASS_HZ}" in audio_fc
        assert f"volume={AppConstants.TEST_MEDIA_PINK_VOLUME_DB}dB" in audio_fc

    async def test_timeout_enforced_and_temp_files_cleaned_up(
        self, tmp_path: Path
    ) -> None:
        req = TestMediaGenerateRequest(
            output_mode="video",
            codec="eac3",
            layout="5.1",
            destination_dir="/",
        )
        with pytest.raises(TestMediaTimeoutError, match="timed out"):
            await generate_test_media(
                req,
                storage_root=tmp_path,
                timeout_sec=0.001,
            )
        # No leftover temp artifacts or partial output
        assert list(tmp_path.iterdir()) == []


class TestFrontendTestMediaModule:
    """Verify pure frontend test_media.js logic, hard blocks, soft warnings, and presets."""

    def test_frontend_validation_matrix_presets_and_profile_prefill(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{}};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "test_media.js"]) {{
          vm.runInContext(
            fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"),
            ctx,
            {{ filename: f }}
          );
        }}
        const TM = ctx.MKVForgeTestMedia;

        // 1. Default destination is "/" (never auto-creates "/Test Audio")
        const defaultDraft = TM.createTestMediaDraft();

        // 2. Profile pre-fill for Optical Legacy -> AC-3 5.1
        const opticalProfile = {{
          preset: "optical_legacy",
          supportedCodecs: ["ac3", "aac_20"],
          maxChannels: 6,
          preferredCodec: "ac3",
        }};
        const opticalDraft = TM.createTestMediaDraft({{
          playbackProfile: opticalProfile,
          fromProfile: true,
        }});

        // 3. Hard blocks: E-AC-3 + 7.1 and Opus + 2.1
        const blockDolby71 = TM.evaluateTestMediaConfig({{ ...defaultDraft, codec: "eac3", layout: "7.1" }});
        const blockOpus21 = TM.evaluateTestMediaConfig({{ ...defaultDraft, codec: "opus", layout: "2.1" }});
        const blockBadName = TM.evaluateTestMediaConfig({{ ...defaultDraft, filename: "../hack.mkv" }});

        // 4. Soft warning when chosen codec mismatches user's Playback Profile
        const mismatchEval = TM.evaluateTestMediaConfig(
          {{ ...defaultDraft, codec: "eac3", layout: "5.1" }},
          opticalProfile
        );

        // 5. PCM preset produces lossless payload with null bitrate & highlights active preset
        const pcmDraft = TM.applyTestMediaPreset(defaultDraft, "lpcm_51");
        const pcmPayload = TM.buildTestMediaPayload(pcmDraft);
        const customTimingDraft = {{ ...defaultDraft, layout: "2.0", toneDurationSec: 5, gapDurationSec: 1, includeSubtitles: false }};
        const customTimingEval = TM.evaluateTestMediaConfig(customTimingDraft);
        const customTimingPayload = TM.buildTestMediaPayload(customTimingDraft);
        const audioModePayload = TM.buildTestMediaPayload({{ ...defaultDraft, outputMode: "audio", includeSubtitles: true }});
        const badToneEval = TM.evaluateTestMediaConfig({{ ...defaultDraft, toneDurationSec: 99 }});
        const badGapEval = TM.evaluateTestMediaConfig({{ ...defaultDraft, gapDurationSec: -1 }});
        const activeWhenStereoTapped = TM.detectMatchingPresetId(
          TM.applyTestMediaPreset(opticalDraft, "stereo_20", opticalProfile),
          opticalProfile,
          "stereo_20"
        );
        const activeWhenManualCustom = TM.detectMatchingPresetId(
          {{ ...defaultDraft, codec: "opus", layout: "5.1", bitrate: "320k" }},
          opticalProfile,
          null
        );
        const neutralSpec = TM.resolveProfileRecommendedSpec({{ preset: "neutral" }});
        const neutralDefaultPreset = defaultDraft.activePresetId;

        // 6. Null draft fails safely
        const nullEval = TM.evaluateTestMediaConfig(null);

        console.log(JSON.stringify({{
          defaultDest: defaultDraft.destinationDir,
          defaultTone: defaultDraft.toneDurationSec,
          defaultGap: defaultDraft.gapDurationSec,
          defaultIncludeSubtitles: defaultDraft.includeSubtitles,
          customEstSec: customTimingEval.estimatedDurationSec,
          customPayloadTone: customTimingPayload.tone_duration_sec,
          customPayloadGap: customTimingPayload.gap_duration_sec,
          customPayloadSubtitles: customTimingPayload.include_subtitles,
          audioPayloadSubtitles: audioModePayload.include_subtitles,
          badToneValid: badToneEval.valid,
          badGapValid: badGapEval.valid,
          opticalCodec: opticalDraft.codec,
          opticalLayout: opticalDraft.layout,
          opticalActivePreset: opticalDraft.activePresetId,
          activeWhenStereoTapped,
          activeWhenManualCustom,
          neutralSpecIsNull: neutralSpec === null,
          neutralDefaultPreset,
          blockDolby71Valid: blockDolby71.valid,
          blockOpus21Valid: blockOpus21.valid,
          blockBadNameValid: blockBadName.valid,
          mismatchValid: mismatchEval.valid,
          mismatchWarnCount: mismatchEval.warnings.length,
          pcmCodec: pcmPayload.codec,
          pcmBitrate: pcmPayload.bitrate,
          nullValid: nullEval.valid,
        }}));
        """
        out = _run_node_json(script)
        assert out["defaultDest"] == "/"
        assert out["defaultTone"] == 2
        assert out["defaultGap"] == 0.35
        assert out["defaultIncludeSubtitles"] is True
        assert out["customEstSec"] == 18
        assert out["customPayloadTone"] == 5
        assert out["customPayloadGap"] == 1
        assert out["customPayloadSubtitles"] is False
        assert out["audioPayloadSubtitles"] is False
        assert out["badToneValid"] is False
        assert out["badGapValid"] is False
        assert out["opticalCodec"] == "ac3"
        assert out["opticalLayout"] == "5.1"
        assert out["opticalActivePreset"] == "your_system"
        assert out["activeWhenStereoTapped"] == "stereo_20"
        assert out["activeWhenManualCustom"] is None
        assert out["neutralSpecIsNull"] is True
        assert out["neutralDefaultPreset"] == "dolby_arc_51"
        assert out["blockDolby71Valid"] is False
        assert out["blockOpus21Valid"] is False
        assert out["blockBadNameValid"] is False
        assert out["mismatchValid"] is True
        assert out["mismatchWarnCount"] >= 1
        assert out["pcmCodec"] == "pcm"
        assert out["pcmBitrate"] is None
        assert out["nullValid"] is False

    def test_view_generated_test_in_library_routes_to_target_folder(self) -> None:
        app_js = (JS_DIR / "app.js").read_text(encoding="utf-8")
        index_html = (JS_DIR.parent / "index.html").read_text(encoding="utf-8")

        # Verify View in Library opens Library Directory View to target folder (never openEditor)
        start = app_js.index("viewGeneratedTestInLibrary() {")
        end = app_js.index("\n      },", start) + 9
        method_body = app_js[start:end]
        assert "this.openFolderInLibrary(targetFolder)" in method_body
        assert "this.openEditor(" not in method_body
        assert "this.testMediaModalOpen = false;" in method_body
        assert "this.settingsModalOpen = false;" in method_body

        # Verify subtitle toggle switch exists in Advanced options
        assert 'aria-label="Include speaker label subtitles"' in index_html
        assert "testMediaDraft.includeSubtitles" in index_html
