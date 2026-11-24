"""End-to-end audit tests proving unique per-file ffmpeg & mkvmerge command translation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.config import AppConstants
from app.models.schemas import (
    BatchAudioRule,
    BatchContainerRule,
    BatchFileOverride,
    BatchRenameRule,
    BatchRuleSpec,
    BatchTrackFilterRule,
    JobConfig,
)
from app.services.batch_compiler import (
    compile_batch_plans,
    compile_batch_rule_for_file,
)
from app.services.ffmpeg_runner import build_ffmpeg_command
from app.services.mkv_builder import build_mkvmerge_command
from app.services.preflight import run_preflight_checks
from app.services.processing_policy import wrap_command_with_io_priority

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_JS_DIR = REPO_ROOT / "app" / "static" / "js"


def _make_row(
    *,
    file_id: int,
    filepath: str,
    tracks: list[dict],
    title: str = "Release.Title",
    global_tags: int = 1,
    attachments: int = 2,
    chapters: int = 1,
) -> dict:
    raw_probe = {
        "container": {
            "properties": {
                "title": title,
                "duration": 60_000_000_000,
            }
        },
        "tracks": tracks,
        "global_tags": [{"num_entries": 1}] * global_tags,
        "track_tags": [{"track_id": 1, "num_entries": 1}],
        "attachments": [{"id": i + 1, "file_name": f"font{i}.ttf"} for i in range(attachments)],
        "chapters": [{"num_entries": 2}] * chapters,
    }
    p = Path(filepath)
    folder = f"/{p.parent.as_posix()}" if p.parent.as_posix() != "." else "/"
    return {
        "id": file_id,
        "filepath": filepath,
        "filename": p.name,
        "folder": folder,
        "size_bytes": 100_000_000,
        "scan_status": AppConstants.FILE_SCAN_STATUS_READY,
        "container_title": title,
        "has_global_tags": global_tags > 0,
        "raw_json": json.dumps(raw_probe),
    }


def _video_track(tid: int = 0, codec: str = "MPEG-H/HEVC/H.265") -> dict:
    return {
        "id": tid,
        "type": "video",
        "codec": codec,
        "properties": {"pixel_dimensions": "1920x1080", "default_track": True},
    }


def _audio_track(
    tid: int,
    codec: str,
    channels: int = 6,
    lang: str = "eng",
    *,
    default: bool = True,
    name: str = "",
) -> dict:
    return {
        "id": tid,
        "type": "audio",
        "codec": codec,
        "properties": {
            "audio_channels": channels,
            "language": lang,
            "default_track": default,
            "track_name": name,
        },
    }


def _sub_track(tid: int, lang: str = "eng", *, default: bool = False) -> dict:
    return {
        "id": tid,
        "type": "subtitles",
        "codec": "SubRip/SRT",
        "properties": {"language": lang, "default_track": default},
    }


class TestCommandTranslationAudit:
    """Verify every per-file UI override maps uniquely to ffmpeg/mkvmerge argv & passes preflight."""

    def test_heterogeneous_batch_produces_unique_ffmpeg_and_mkvmerge_commands_per_file(
        self, tmp_path: Path
    ) -> None:
        show_dir = tmp_path / "Show"
        show_dir.mkdir()
        for name in ("Ep01.mkv", "Ep02.mkv", "Ep03.mkv"):
            (show_dir / name).write_bytes(b"0" * 1024)

        row_a = _make_row(
            file_id=1,
            filepath="Show/Ep01.mkv",
            tracks=[
                _video_track(0),
                _audio_track(1, "AAC", 6, "und", default=True),
                _sub_track(2, "eng"),
            ],
        )
        row_b = _make_row(
            file_id=2,
            filepath="Show/Ep02.mkv",
            tracks=[
                _video_track(0),
                _audio_track(1, "AAC", 6, "eng", default=True),
                _audio_track(2, "AC-3", 6, "hin", default=False),
                _sub_track(3, "eng"),
            ],
        )
        row_c = _make_row(
            file_id=3,
            filepath="Show/Ep03.mkv",
            tracks=[
                _video_track(0),
                _audio_track(1, "AAC", 6, "eng", default=True),
                _sub_track(2, "eng"),
            ],
        )

        rule = BatchRuleSpec(
            container=BatchContainerRule(
                title_action="clear",
                strip_global_tags=True,
                strip_track_tags=True,
                strip_attachments=True,
                smart_fill_track_names=True,
            ),
            audio=BatchAudioRule(
                enabled=True,
                target_codec="eac3",
                bitrate="640k",
                match_condition="incompatible_or_non_dolby",
                drop_source_track=False,
            ),
            track_filter=BatchTrackFilterRule(
                retag_und_audio_to_preferred=True,
                retag_und_target_language="hin",
            ),
            output_mode="replace",
        )

        # User customizes EACH file differently in the Dry-Run Preview:
        # - File 1 (Ep01): Keep default conversion + retag und->hin + drop video #0 (--no-video!) + custom rename
        # - File 2 (Ep02): Turn OFF conversion + drop default audio #1 (keep #2 -> promoted to default!) + keep attachments
        # - File 3 (Ep03): Keep conversion + keep title + drop subtitle #2 (--no-subtitles!) + disable smart fill
        overrides = {
            1: BatchFileOverride(
                track_enabled={0: False},
                output_filename="AudioOnly_Ep01.mkv",
            ),
            2: BatchFileOverride(
                audio_convert_enabled=False,
                track_enabled={1: False},
                strip_attachments=False,
            ),
            3: BatchFileOverride(
                clear_title=False,
                track_enabled={2: False},
                smart_fill_track_names=False,
            ),
        }

        plans = compile_batch_plans(
            [row_a, row_b, row_c],
            rule,
            active_job_filepaths=set(),
            storage_root=tmp_path,
            file_overrides=overrides,
        )
        by_id = {p.file_id: p for p in plans}

        # Every compiled JobConfig MUST pass preflight checks without crashing
        for fid, plan in by_id.items():
            assert plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE, f"file {fid}: {plan.reason}"
            assert plan.job_config is not None
            run_preflight_checks(
                tmp_path / plan.filepath,
                plan.job_config,
                duration_us=60_000_000,
                storage_root=tmp_path,
            )

        # --- File 1: Stage 1 FFmpeg + Stage 2 mkvmerge (--no-video, retag hin, rename+replace delete_original) ---
        cfg1 = by_id[1].job_config
        assert cfg1 is not None
        assert cfg1.audio_conversion.enabled is True
        assert cfg1.audio_conversion.language == "hin"
        assert cfg1.output_mode == AppConstants.OUTPUT_MODE_SAVE_AS
        assert cfg1.output_path == "Show/AudioOnly_Ep01.mkv"
        assert cfg1.delete_original is True
        ff1 = build_ffmpeg_command(
            show_dir / "Ep01.mkv",
            show_dir / "Ep01.tmp.eac3",
            audio_index=cfg1.audio_conversion.source_audio_index,
            target_codec=cfg1.audio_conversion.target_codec,
            bitrate=cfg1.audio_conversion.bitrate,
            channels=cfg1.audio_conversion.source_channels or 6,
            source_codec=cfg1.audio_conversion.source_codec,
        )
        assert "-c:a" in ff1 and "eac3" in ff1 and "640k" in ff1
        mkv1 = build_mkvmerge_command(
            show_dir / "Ep01.mkv",
            show_dir / "Ep01.tmp.mkv",
            cfg1,
            temp_audio_path=show_dir / "Ep01.tmp.eac3",
        )
        assert "--no-video" in mkv1
        assert "--no-attachments" in mkv1
        assert "--language" in mkv1 and "0:hin" in mkv1 and "1:hin" in mkv1
        assert "--track-order" in mkv1
        assert mkv1[mkv1.index("--track-order") + 1] == "0:0,1:1,1:2"

        # --- File 2: NO Stage 1 FFmpeg; Stage 2 drops #1, promotes #2 to default:yes, keeps attachments ---
        cfg2 = by_id[2].job_config
        assert cfg2 is not None
        assert cfg2.audio_conversion.enabled is False
        assert cfg2.container.strip_attachments is False
        mkv2 = build_mkvmerge_command(
            show_dir / "Ep02.mkv",
            show_dir / "Ep02.tmp.mkv",
            cfg2,
            temp_audio_path=None,
        )
        assert "--video-tracks" in mkv2 and "0" in mkv2
        assert "--audio-tracks" in mkv2 and mkv2[mkv2.index("--audio-tracks") + 1] == "2"
        assert "2:yes" in mkv2  # Bug #3 fix: surviving audio track promoted to default!
        assert "--no-attachments" not in mkv2
        assert mkv2[mkv2.index("--track-order") + 1] == "0:0,0:2,0:3"

        # --- File 3: Stage 1 FFmpeg + Stage 2 keeps title, drops subtitles (--no-subtitles), no smart fill ---
        cfg3 = by_id[3].job_config
        assert cfg3 is not None
        assert cfg3.audio_conversion.enabled is True
        assert cfg3.container.title is None
        mkv3 = build_mkvmerge_command(
            show_dir / "Ep03.mkv",
            show_dir / "Ep03.tmp.mkv",
            cfg3,
            temp_audio_path=show_dir / "Ep03.tmp.eac3",
        )
        assert "--title" not in mkv3
        assert "--no-subtitles" in mkv3
        assert "--no-attachments" in mkv3
        assert mkv3[mkv3.index("--track-order") + 1] == "1:0,0:0,1:1"

    def test_ac3_to_eac3_upsample_is_blocked_in_compiler_and_never_crashes_preflight(
        self, tmp_path: Path
    ) -> None:
        """Bug #1 regression: AC-3 -> E-AC-3 without downmix must never enqueue a preflight-doomed job."""
        (tmp_path / "DolbyAC3.mkv").write_bytes(b"0" * 1024)
        row = _make_row(
            file_id=11,
            filepath="DolbyAC3.mkv",
            tracks=[_video_track(0), _audio_track(1, "AC-3", 6, "eng")],
            title="",
            global_tags=0,
            attachments=0,
            chapters=0,
        )
        rule = BatchRuleSpec(
            container=BatchContainerRule(
                title_action="keep",
                strip_global_tags=False,
                strip_track_tags=False,
                strip_attachments=False,
                smart_fill_track_names=False,
            ),
            audio=BatchAudioRule(
                enabled=True,
                target_codec="eac3",
                bitrate="640k",
                match_condition="any_non_target",
                accept_existing_dolby=False,
            ),
        )
        plan = compile_batch_rule_for_file(row, rule)
        assert plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert not any(c.chip_id == "audio_convert" for c in plan.chips)

        # Even if a stale client sends audio_convert_enabled=True override, compiler blocks it safely
        forced_plan = compile_batch_rule_for_file(
            row,
            rule,
            file_override=BatchFileOverride(audio_convert_enabled=True),
        )
        assert forced_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert any("AC-3 to E-AC-3" in w for w in forced_plan.warnings)

    def test_excluded_file_ids_preserve_preview_sequential_numbering(
        self, tmp_path: Path
    ) -> None:
        """Bug #2 regression: unchecking File 1 in Preview must NOT renumber File 2/3 on enqueue."""
        for name in ("BonusA.mkv", "BonusB.mkv", "BonusC.mkv"):
            (tmp_path / name).write_bytes(b"0" * 1024)
        rows = [
            _make_row(file_id=1, filepath="BonusA.mkv", tracks=[_video_track(0), _audio_track(1, "AAC", 6)]),
            _make_row(file_id=2, filepath="BonusB.mkv", tracks=[_video_track(0), _audio_track(1, "AAC", 6)]),
            _make_row(file_id=3, filepath="BonusC.mkv", tracks=[_video_track(0), _audio_track(1, "AAC", 6)]),
        ]
        rule = BatchRuleSpec(
            audio=BatchAudioRule(enabled=False),
            rename=BatchRenameRule(enabled=True, pattern="Show_S01E{n}", start_number=1),
            output_mode="replace",
        )
        preview_plans = compile_batch_plans(
            rows, rule, active_job_filepaths=set(), storage_root=tmp_path
        )
        assert [p.output_filename for p in preview_plans] == [
            "Show_S01E01.mkv",
            "Show_S01E02.mkv",
            "Show_S01E03.mkv",
        ]

        # User unchecks file_id=1 -> file 2 & 3 MUST keep 02 and 03 (matching preview!)
        enqueue_plans = compile_batch_plans(
            rows,
            rule,
            active_job_filepaths=set(),
            storage_root=tmp_path,
            excluded_file_ids={1},
        )
        assert [p.file_id for p in enqueue_plans] == [2, 3]
        assert [p.output_filename for p in enqueue_plans] == [
            "Show_S01E02.mkv",
            "Show_S01E03.mkv",
        ]

    def test_save_as_same_folder_and_same_filename_flagged_as_collision_upfront(
        self, tmp_path: Path
    ) -> None:
        """Bug #4 regression: Save As pointing at the original file itself must flag collision in preview/enqueue."""
        movies = tmp_path / "Movies"
        movies.mkdir()
        (movies / "Film.mkv").write_bytes(b"0" * 1024)
        row = _make_row(
            file_id=9,
            filepath="Movies/Film.mkv",
            tracks=[_video_track(0), _audio_track(1, "AAC", 6)],
        )
        rule = BatchRuleSpec(
            output_mode=AppConstants.OUTPUT_MODE_SAVE_AS,
            destination_folder="/Movies",
            rename=BatchRenameRule(enabled=False),
        )
        plans = compile_batch_plans(
            [row], rule, active_job_filepaths=set(), storage_root=tmp_path
        )
        assert len(plans) == 1
        assert plans[0].action == AppConstants.BATCH_PLAN_ACTION_SKIP_WARNING
        assert plans[0].collision_error is not None
        assert "Destination matches original file" in plans[0].collision_error
        assert plans[0].job_config is None


class TestUiSettingsToCliTranslationAuditFixes:
    """Regression suite for Findings #1-#6 (UI Settings -> Payload -> FFmpeg/mkvmerge CLI)."""

    @pytest.mark.parametrize(
        ("codec", "bitrate", "channels", "expected_encoder"),
        [
            ("eac3", "640k", 6, "eac3"),
            ("ac3", "448k", 6, "ac3"),
            ("aac", "448k", 6, "aac"),
            ("opus", "320k", 6, "libopus"),
            ("flac", None, 8, "flac"),
        ],
    )
    def test_finding1_ffmpeg_threads_caps_decode_filter_and_encode(
        self,
        tmp_path: Path,
        codec: str,
        bitrate: str | None,
        channels: int,
        expected_encoder: str,
    ) -> None:
        """Finding #1: `-threads N` must be emitted for BOTH input decode and output encode (+ `-filter_threads N`)."""
        src = tmp_path / "movie.mkv"
        out = tmp_path / f"movie.mkvforge.{codec}"
        cmd = build_ffmpeg_command(
            src,
            out,
            audio_index=0,
            target_codec=codec,
            bitrate=bitrate,
            channels=channels,
            threads=2,
        )
        i_idx = cmd.index("-i")
        ca_idx = cmd.index("-c:a")
        before_input = cmd[:i_idx]
        after_input = cmd[i_idx:]

        assert "-threads" in before_input
        assert before_input[before_input.index("-threads") + 1] == "2"
        assert "-filter_threads" in before_input
        assert before_input[before_input.index("-filter_threads") + 1] == "2"

        assert cmd[ca_idx + 1] == expected_encoder
        assert "-threads" in after_input
        assert after_input[after_input.index("-threads") + 1] == "2"
        assert cmd[ca_idx + 2 : ca_idx + 4] == ["-threads", "2"]

    def test_finding1_negative_threads_none_or_zero_omits_thread_flags(
        self, tmp_path: Path
    ) -> None:
        """Edge case: threads=None or threads=0 must emit zero thread flags."""
        src = tmp_path / "movie.mkv"
        out = tmp_path / "movie.mkvforge.eac3"
        for val in (None, 0, -1):
            cmd = build_ffmpeg_command(
                src,
                out,
                audio_index=0,
                target_codec="eac3",
                bitrate="640k",
                channels=6,
                threads=val,
            )
            assert "-threads" not in cmd
            assert "-filter_threads" not in cmd

    def test_finding2_ionice_includes_ignore_flag_for_docker_safety(self) -> None:
        """Finding #2: `ionice` must include `-t` (`--ignore`) so EPERM in unprivileged Docker never fails jobs."""
        base_cmd = ["mkvmerge", "--gui-mode", "-o", "/tmp/out.mkv", "/storage/in.mkv"]
        wrapped = wrap_command_with_io_priority(base_cmd, use_ionice_idle=True)
        assert wrapped == ["ionice", "-t", "-c", "3", *base_cmd]
        assert wrap_command_with_io_priority(base_cmd, use_ionice_idle=False) == base_cmd

    def test_findings3_4_5_6_frontend_settings_translate_to_exact_cli_flags(
        self, tmp_path: Path
    ) -> None:
        """Findings #3-#6: Verify Editor & Batch JS state factories -> JobConfig -> mkvmerge/ffmpeg CLI."""
        js_script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(str(STATIC_JS_DIR))};

        const ctx = {{
          window: null,
          console,
          Math,
          Date,
          JSON,
          Number,
          String,
          Boolean,
          Array,
          Object,
          Map,
          Set,
          RegExp,
          Error,
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        vm.runInContext(fs.readFileSync(path.join(jsDir, "constants.js"), "utf8"), ctx);
        vm.runInContext(fs.readFileSync(path.join(jsDir, "editor.js"), "utf8"), ctx);
        vm.runInContext(fs.readFileSync(path.join(jsDir, "batch.js"), "utf8"), ctx);

        const Ed = ctx.MKVForgeEditor;
        const B = ctx.MKVForgeBatch;

        const sampleFile = {{
          id: 42,
          filename: "Anime.S01E01.mkv",
          filepath: "Shows/Anime.S01E01.mkv",
          folder: "/Shows",
          container_title: "Release Title",
          has_global_tags: false,
          global_tags_count: 0,
          has_metadata: false,
          attachments: [{{ id: 1, file_name: "font.ttf", size_bytes: 2048 }}],
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true, forced_track: false, pixel_dimensions: "1920x1080" }},
            {{ id: 1, type: "audio", codec: "AAC", audio_channels: 6, audio_index: 0, language: "jpn", default_track: true, forced_track: false, track_name: "Japanese Main" }},
            {{ id: 2, type: "audio", codec: "AAC", audio_channels: 6, audio_index: 1, language: "eng", default_track: false, forced_track: false, track_name: "English Main" }},
          ],
        }};

        // Finding #3: default_strip_global_tags=true with default_remove_all_metadata=false
        const edStripTagsTrue = Ed.createEditorState(sampleFile, {{
          default_remove_all_metadata: false,
          default_strip_global_tags: true,
        }});
        const payloadStripTagsTrue = Ed.buildJobRequestPayload(edStripTagsTrue, sampleFile);

        const edStripTagsFalse = Ed.createEditorState(sampleFile, {{
          default_remove_all_metadata: false,
          default_strip_global_tags: false,
        }});
        const payloadStripTagsFalse = Ed.buildJobRequestPayload(edStripTagsFalse, sampleFile);

        // Finding #4: Batch Studio strip_attachments respects default_remove_all_metadata
        const batchKeepMeta = B.createDefaultBatchRule({{
          default_remove_all_metadata: false,
          default_strip_global_tags: true,
        }});
        const batchRemoveMeta = B.createDefaultBatchRule({{
          default_remove_all_metadata: true,
        }});

        // Finding #5 & #6: default_dolby_codec=ac3 + preferred_language=eng (neutral profile)
        const recsAc3Eng = Ed.getRecommendedAddAudioOptions(
          edStripTagsFalse,
          {{ preset: "neutral", preferredCodec: "eac3", maxChannels: 6 }},
          {{ default_dolby_codec: "ac3", preferred_language: "eng" }}
        );

        // Finding #5 precedence: non-neutral Playback Profile still overrides default_dolby_codec
        const recsProfileWins = Ed.getRecommendedAddAudioOptions(
          edStripTagsFalse,
          {{ preset: "dolby_arc", preferredCodec: "eac3", maxChannels: 6 }},
          {{ default_dolby_codec: "ac3", preferred_language: "eng" }}
        );

        // Finding #5 negative/edge: AC-3 source must NOT recommend blocked AC-3 -> AC-3
        const ac3OnlyFile = {{
          ...sampleFile,
          tracks: [
            sampleFile.tracks[0],
            {{ id: 1, type: "audio", codec: "AC-3", audio_channels: 6, audio_index: 0, language: "eng", default_track: true, forced_track: false }},
          ],
        }};
        const edAc3Source = Ed.createEditorState(ac3OnlyFile, {{ default_dolby_codec: "ac3" }});
        const recsOnAc3Source = Ed.getRecommendedAddAudioOptions(
          edAc3Source,
          {{ preset: "neutral" }},
          {{ default_dolby_codec: "ac3", preferred_language: "eng" }}
        );

        // Finding #6 commentary guardrail: English commentary must NOT beat Japanese main audio
        const commentaryFile = {{
          ...sampleFile,
          tracks: [
            sampleFile.tracks[0],
            {{ id: 1, type: "audio", codec: "AAC", audio_channels: 6, audio_index: 0, language: "jpn", default_track: true, forced_track: false, track_name: "Japanese 5.1" }},
            {{ id: 2, type: "audio", codec: "AAC", audio_channels: 2, audio_index: 1, language: "eng", default_track: false, forced_track: false, track_name: "Director Commentary" }},
          ],
        }};
        const edCommentary = Ed.createEditorState(commentaryFile, {{}});
        const recsCommentary = Ed.getRecommendedAddAudioOptions(
          edCommentary,
          {{ preset: "neutral" }},
          {{ preferred_language: "en" }}
        );

        // Finding #6 fallback when preferred language matches no track
        const recsNoLangMatch = Ed.getRecommendedAddAudioOptions(
          edStripTagsFalse,
          {{ preset: "neutral" }},
          {{ preferred_language: "fra" }}
        );

        console.log(JSON.stringify({{
          payloadStripTagsTrue,
          payloadStripTagsFalse,
          batchKeepMeta,
          batchRemoveMeta,
          recsAc3EngFirst: recsAc3Eng[0],
          recsProfileWinsFirst: recsProfileWins[0],
          recsOnAc3SourceCodecs: recsOnAc3Source.map((r) => r.codec),
          recsCommentaryTrackId: recsCommentary[0].preferredSourceTrackId,
          recsNoLangMatchTrackId: recsNoLangMatch[0].preferredSourceTrackId,
        }}));
        """
        proc = subprocess.run(
            ["node", "-e", js_script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        # Finding #3 end-to-end into mkvmerge CLI
        cfg_strip = JobConfig.model_validate(out["payloadStripTagsTrue"]["config"])
        assert cfg_strip.container.strip_global_tags is True
        assert cfg_strip.container.strip_attachments is False
        cmd_strip = build_mkvmerge_command(
            tmp_path / "in.mkv", tmp_path / "out.mkv", cfg_strip
        )
        assert "--no-global-tags" in cmd_strip
        assert "--no-attachments" not in cmd_strip

        cfg_no_strip = JobConfig.model_validate(out["payloadStripTagsFalse"]["config"])
        assert cfg_no_strip.container.strip_global_tags is False
        cmd_no_strip = build_mkvmerge_command(
            tmp_path / "in.mkv", tmp_path / "out.mkv", cfg_no_strip
        )
        assert "--no-global-tags" not in cmd_no_strip

        # Finding #4 end-to-end into Batch Compiler -> mkvmerge CLI
        assert out["batchKeepMeta"]["container"]["strip_attachments"] is False
        assert out["batchKeepMeta"]["container"]["strip_global_tags"] is True
        assert out["batchRemoveMeta"]["container"]["strip_attachments"] is True

        row = _make_row(
            file_id=10,
            filepath="Shows/Anime.S01E01.mkv",
            tracks=[_video_track(0), _audio_track(1, "AAC", 6, "eng")],
            attachments=2,
        )
        rule_keep = BatchRuleSpec.model_validate(out["batchKeepMeta"])
        plan_keep = compile_batch_rule_for_file(row, rule_keep)
        assert plan_keep.job_config is not None
        assert plan_keep.job_config.container.strip_attachments is False
        cmd_batch_keep = build_mkvmerge_command(
            tmp_path / "in.mkv",
            tmp_path / "out.mkv",
            plan_keep.job_config,
            temp_audio_path=tmp_path / "tmp.eac3",
        )
        src_idx = cmd_batch_keep.index(str(tmp_path / "in.mkv"))
        # Source file flags sit between temp_audio_path and source_path
        audio_idx = cmd_batch_keep.index(str(tmp_path / "tmp.eac3"))
        source_flags = cmd_batch_keep[audio_idx + 1 : src_idx]
        assert "--no-attachments" not in source_flags

        # Finding #5: Default Dolby Codec = AC-3 promoted under neutral profile, overridden by active profile, blocked on AC-3 source
        assert out["recsAc3EngFirst"]["codec"] == "ac3"
        assert out["recsAc3EngFirst"]["badge"] == "Default"
        assert out["recsProfileWinsFirst"]["codec"] == "eac3"
        assert out["recsProfileWinsFirst"]["badge"] == "Your System"
        assert "ac3" not in out["recsOnAc3SourceCodecs"]

        # Finding #6: Preferred language picks Track #2 (eng), skips English commentary, falls back to Track #1 when absent
        assert out["recsAc3EngFirst"]["preferredSourceTrackId"] == 2
        assert out["recsCommentaryTrackId"] == 1
        assert out["recsNoLangMatchTrackId"] == 1
