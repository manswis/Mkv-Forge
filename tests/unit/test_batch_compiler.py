"""Exhaustive unit tests for `app/services/batch_compiler.py` (Phase 18 Batch & Templates)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.config import AppConstants
from app.models.schemas import (
    BatchAudioRule,
    BatchContainerRule,
    BatchFileOverride,
    BatchRenameRule,
    BatchRuleSpec,
    BatchTrackFilterRule,
    ParsedTrackInfo,
)
from app.services.batch_compiler import (
    build_batch_preview_response,
    compile_batch_plans,
    compile_batch_rule_for_file,
    detect_majority_audio_language,
    estimate_batch_disk_space,
    extract_season_episode,
    format_channel_label,
    format_smart_track_badge,
    is_commentary_or_description_track,
    natural_sort_key,
    normalize_lang_code,
    sanitize_batch_output_filename,
    select_primary_audio_track,
    summarize_batch_inventory,
)
from app.services.ffmpeg_runner import build_ffmpeg_command
from app.services.mkv_builder import build_mkvmerge_command


def _make_raw_probe(
    *,
    title: str = "",
    global_tags: int = 0,
    tracks: list[dict[str, Any]] | None = None,
) -> str:
    """Construct a synthetic `mkvmerge -J` JSON string for unit testing."""
    default_tracks = [
        {
            "id": 0,
            "type": "video",
            "codec": "HEVC",
            "properties": {
                "language": "und",
                "default_track": True,
                "forced_track": False,
                "pixel_dimensions": "1920x1080",
            },
        },
        {
            "id": 1,
            "type": "audio",
            "codec": "AAC",
            "properties": {
                "language": "eng",
                "default_track": True,
                "forced_track": False,
                "audio_channels": 6,
            },
        },
        {
            "id": 2,
            "type": "subtitles",
            "codec": "SubRip/SRT",
            "properties": {
                "language": "eng",
                "default_track": False,
                "forced_track": False,
            },
        },
    ]
    payload: dict[str, Any] = {
        "container": {"properties": {"title": title} if title else {}},
        "tracks": tracks if tracks is not None else default_tracks,
        "global_tags": [{"num_entries": global_tags}] if global_tags > 0 else [],
        "track_tags": [],
        "attachments": [],
        "chapters": [],
    }
    return json.dumps(payload)


def _make_media_row(
    *,
    file_id: int = 1,
    filepath: str = "Season 1/S01E01.mkv",
    folder: str = "/Season 1",
    filename: str = "S01E01.mkv",
    size_bytes: int = 1_000_000_000,
    container_title: str = "",
    has_global_tags: bool = False,
    has_aac_51: bool = True,
    has_ac3_eac3: bool = False,
    scan_status: str = "ready",
    audio_summary: list[dict[str, Any]] | None = None,
    raw_json: str | None = None,
) -> dict[str, Any]:
    summary = audio_summary if audio_summary is not None else [
        {"codec": "AAC", "channels": 6, "channel_layout": "5.1", "language": "eng"}
    ]
    return {
        "id": file_id,
        "filepath": filepath,
        "folder": folder,
        "filename": filename,
        "size_bytes": size_bytes,
        "container_title": container_title,
        "has_global_tags": int(has_global_tags),
        "has_aac_51": int(has_aac_51),
        "has_ac3_eac3": int(has_ac3_eac3),
        "scan_status": scan_status,
        "audio_summary_json": json.dumps(summary),
        "raw_json": raw_json if raw_json is not None else _make_raw_probe(
            title=container_title,
            global_tags=4 if has_global_tags else 0,
        ),
    }


class TestGuardrail1AudioTieBreaker:
    """Guardrail #1: Deprioritize commentary/AD tracks, then rank by channels -> default -> index."""

    def test_is_commentary_or_description_track_matches_variants(self) -> None:
        assert is_commentary_or_description_track("Director's Commentary") is True
        assert is_commentary_or_description_track("English Audio Description (AD)") is True
        assert is_commentary_or_description_track("Visually Impaired") is True
        assert is_commentary_or_description_track("English SDH") is True
        assert is_commentary_or_description_track("Cast & Crew Comments") is True
        assert is_commentary_or_description_track("Surround 5.1") is False
        assert is_commentary_or_description_track("") is False
        assert is_commentary_or_description_track(None) is False

    def test_deprioritizes_commentary_even_if_default_or_first(self) -> None:
        tracks = [
            ParsedTrackInfo(
                id=1,
                type="audio",
                codec="AAC",
                language="eng",
                track_name="Director's Commentary",
                default_track=True,
                forced_track=False,
                audio_channels=6,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=2,
                type="audio",
                codec="AAC",
                language="eng",
                track_name="Main Audio",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=1,
            ),
        ]
        selected = select_primary_audio_track(
            tracks,
            rule=BatchAudioRule(),
            user_preferred_language="eng",
        )
        assert selected is not None
        assert selected.id == 2

    def test_ranks_by_highest_channels_then_default_then_lowest_index(self) -> None:
        # 5.1 non-default beats 2.0 default in the same preferred language
        tracks = [
            ParsedTrackInfo(
                id=1,
                type="audio",
                codec="AAC",
                language="eng",
                track_name="Stereo",
                default_track=True,
                forced_track=False,
                audio_channels=2,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=2,
                type="audio",
                codec="AAC",
                language="eng",
                track_name="Surround",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=1,
            ),
        ]
        selected = select_primary_audio_track(
            tracks,
            rule=BatchAudioRule(),
            user_preferred_language="eng",
        )
        assert selected is not None
        assert selected.id == 2

        # When channel counts tie (both 6ch), default_track=True wins
        tied_ch = [
            ParsedTrackInfo(
                id=10,
                type="audio",
                codec="AAC",
                language="eng",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=11,
                type="audio",
                codec="AAC",
                language="eng",
                default_track=True,
                forced_track=False,
                audio_channels=6,
                audio_index=1,
            ),
        ]
        selected_default = select_primary_audio_track(
            tied_ch,
            rule=BatchAudioRule(),
            user_preferred_language="eng",
        )
        assert selected_default is not None
        assert selected_default.id == 11

        # When channels and default_track both tie, lowest track index wins
        tied_all = [
            ParsedTrackInfo(
                id=20,
                type="audio",
                codec="AAC",
                language="eng",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=21,
                type="audio",
                codec="AAC",
                language="eng",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=1,
            ),
        ]
        selected_idx = select_primary_audio_track(
            tied_all,
            rule=BatchAudioRule(),
            user_preferred_language="eng",
        )
        assert selected_idx is not None
        assert selected_idx.id == 20

    def test_falls_back_from_preferred_to_und_then_other_language(self) -> None:
        tracks = [
            ParsedTrackInfo(
                id=1,
                type="audio",
                codec="AAC",
                language="jpn",
                default_track=True,
                forced_track=False,
                audio_channels=6,
                audio_index=0,
            ),
            ParsedTrackInfo(
                id=2,
                type="audio",
                codec="AAC",
                language="und",
                default_track=False,
                forced_track=False,
                audio_channels=6,
                audio_index=1,
            ),
        ]
        # No 'eng' track -> falls back to 'und' (Tier 1) ahead of 'jpn' (Tier 2)
        selected = select_primary_audio_track(
            tracks,
            rule=BatchAudioRule(),
            user_preferred_language="eng",
        )
        assert selected is not None
        assert selected.id == 2


class TestMixedSeasonCompilationAndGuardrails:
    """Verify mixed episodes (AAC 5.1 vs EAC3/AC3 5.1), active-job skip, and disk-space checks."""

    def test_mixed_season_aac51_vs_existing_dolby_episodes(self) -> None:
        rule = BatchRuleSpec(
            container=BatchContainerRule(
                title_action="clear",
                strip_global_tags=True,
                strip_track_tags=True,
            ),
            audio=BatchAudioRule(
                enabled=True,
                target_codec="eac3",
                bitrate="640k",
                accept_existing_dolby=True,
            ),
            output_mode="keep_both",
        )

        # Episode 1: AAC 5.1 + messy release title -> Stage 1 (FFmpeg) + Stage 2 (mkvmerge)
        ep1 = _make_media_row(
            file_id=1,
            filepath="S01/E01.mkv",
            filename="E01.mkv",
            container_title="Show.S01E01.1080p.WEB-DL-GRP",
        )
        plan1 = compile_batch_rule_for_file(ep1, rule)
        assert plan1.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert plan1.stage_plan == AppConstants.BATCH_STAGE_TRANSCODE_AND_REMUX
        assert plan1.job_config is not None
        assert plan1.job_config.audio_conversion.enabled is True
        assert plan1.job_config.audio_conversion.target_codec == "eac3"
        assert plan1.job_config.container.title == ""

        # Episode 2: Already AC-3 (Dolby Digital 5.1) + messy release title -> Skip FFmpeg, Fast Remux Only!
        ep2_raw = _make_raw_probe(
            title="Show.S01E02.1080p.WEB-DL-GRP",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        ep2 = _make_media_row(
            file_id=2,
            filepath="S01/E02.mkv",
            filename="E02.mkv",
            container_title="Show.S01E02.1080p.WEB-DL-GRP",
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=ep2_raw,
        )
        plan2 = compile_batch_rule_for_file(ep2, rule)
        assert plan2.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert plan2.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
        assert plan2.job_config is not None
        assert plan2.job_config.audio_conversion.enabled is False
        assert plan2.job_config.container.title == ""

        # Episode 3: Already E-AC-3 5.1 AND no release title/tags -> Auto-Skip (Already Compliant)!
        ep3_raw = _make_raw_probe(
            title="",
            global_tags=0,
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        ep3 = _make_media_row(
            file_id=3,
            filepath="S01/E03.mkv",
            filename="E03.mkv",
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=ep3_raw,
        )
        plan3 = compile_batch_rule_for_file(ep3, rule)
        assert plan3.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert plan3.stage_plan == AppConstants.BATCH_STAGE_NONE
        assert plan3.job_config is None

    def test_guardrail3_active_job_duplicate_guard(self) -> None:
        ep = _make_media_row(file_id=5, filepath="S01/E05.mkv", filename="E05.mkv")
        plan = compile_batch_rule_for_file(
            ep,
            BatchRuleSpec(),
            active_job_filepaths={"S01/E05.mkv"},
        )
        assert plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB
        assert plan.stage_plan == AppConstants.BATCH_STAGE_NONE
        assert plan.reason == "Skip (Already in Queue)"
        assert plan.job_config is None

    def test_guardrail2_disk_space_check_keep_both_vs_replace(self, tmp_path: Path) -> None:
        ep1 = _make_media_row(file_id=1, filepath="S01/E01.mkv", size_bytes=10 * 1024**3)
        ep2 = _make_media_row(file_id=2, filepath="S01/E02.mkv", size_bytes=10 * 1024**3)
        plans = [
            compile_batch_rule_for_file(ep1, BatchRuleSpec(output_mode="keep_both")),
            compile_batch_rule_for_file(ep2, BatchRuleSpec(output_mode="keep_both")),
        ]

        # Simulate 15 GB free disk space:
        # - For keep_both (2 x 10 GB = ~22.1 GB required) -> disk_space_ok = False with warning!
        # - For replace (max 10 GB = ~11.1 GB required) -> disk_space_ok = True!
        fake_usage = type("Usage", (), {"total": 100 * 1024**3, "used": 85 * 1024**3, "free": 15 * 1024**3})()
        with patch("app.services.batch_compiler.shutil.disk_usage", return_value=fake_usage):
            free_kb, req_kb, ok_kb, warn_kb = estimate_batch_disk_space(
                plans,
                output_mode="keep_both",
                storage_root=tmp_path,
            )
            assert ok_kb is False
            assert req_kb > free_kb
            assert warn_kb is not None
            assert "Replace Original" in warn_kb

            free_rep, req_rep, ok_rep, warn_rep = estimate_batch_disk_space(
                plans,
                output_mode="replace",
                storage_root=tmp_path,
            )
            assert ok_rep is True
            assert req_rep < free_rep
            assert warn_rep is None

    def test_zero_audio_safety_guardrail_never_drops_last_audio_track(self) -> None:
        # Japanese-only episode when rule asks to keep only 'eng' audio without audio conversion
        jpn_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "jpn", "default_track": True, "audio_channels": 2}},
            ],
        )
        row = _make_media_row(
            file_id=9,
            filepath="Anime/E01.mkv",
            filename="E01.mkv",
            raw_json=jpn_raw,
        )
        rule = BatchRuleSpec(
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(
                preferred_language="eng",
                keep_audio_mode="preferred_only",
            ),
        )
        plan = compile_batch_rule_for_file(row, rule)
        assert any("Kept primary audio track #1" in w for w in plan.warnings)

    def test_retag_und_and_smart_fill_and_subtitle_filter(self) -> None:
        raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True, "pixel_dimensions": "1920x1080"}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "und", "default_track": True, "audio_channels": 6}},
                {"id": 2, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "eng", "default_track": False}},
                {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "fra", "default_track": False}},
            ],
        )
        row = _make_media_row(file_id=12, filepath="S01/E12.mkv", filename="E12.mkv", raw_json=raw)
        rule = BatchRuleSpec(
            container=BatchContainerRule(
                title_action="keep",
                strip_global_tags=False,
                strip_track_tags=False,
                smart_fill_track_names=True,
            ),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(
                retag_und_audio_to_preferred=True,
                retag_und_target_language="eng",
                preferred_language="eng",
                keep_subtitle_mode="preferred_only",
            ),
        )
        plan = compile_batch_rule_for_file(row, rule)
        assert plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert plan.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
        assert plan.job_config is not None
        track_by_id = {t.id: t for t in plan.job_config.tracks}
        assert track_by_id[1].language == "eng"
        assert track_by_id[1].track_name == "Audio • E-AC-3 • 5.1 • eng"
        assert track_by_id[2].enabled is True
        assert track_by_id[3].enabled is False

    def test_negative_cases_scanning_error_and_lossy_to_flac(self, tmp_path: Path) -> None:
        scanning_row = _make_media_row(file_id=20, scan_status="scanning")
        p_scan = compile_batch_rule_for_file(scanning_row, BatchRuleSpec())
        assert p_scan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_NOT_READY

        empty_tracks_row = _make_media_row(file_id=21, raw_json=json.dumps({"tracks": []}))
        p_empty = compile_batch_rule_for_file(empty_tracks_row, BatchRuleSpec())
        assert p_empty.action == AppConstants.BATCH_PLAN_ACTION_SKIP_WARNING

        # Lossy AAC source with lossless FLAC target skips audio conversion with warning
        aac_row = _make_media_row(file_id=22, container_title="")
        flac_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=True, target_codec="flac"),
        )
        p_flac = compile_batch_rule_for_file(aac_row, flac_rule)
        assert p_flac.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert any("lossy" in w for w in p_flac.warnings)

        # Helper coverage
        assert normalize_lang_code("en") == "eng"
        assert normalize_lang_code(None) == "und"
        assert format_channel_label(8) == "7.1"
        assert format_channel_label(10) == "10ch"
        inv = summarize_batch_inventory([aac_row])
        assert inv.total_files == 1
        assert inv.detected_majority_audio_language == "eng"
        preview = build_batch_preview_response(
            [aac_row],
            BatchRuleSpec(),
            batch_scope_label="1 file",
            active_job_filepaths=set(),
            storage_root=tmp_path,
        )
        assert preview.enqueue_count == 1


class TestMajorityLanguageAndPerFileChipOverrides:
    """Verify majority audio language detection (with und fallback) and interactive per-file chip overrides."""

    def test_majority_language_detection_and_und_fallback(self) -> None:
        # 2x Japanese + 1x untagged -> majority is 'jpn'
        jpn_raw = _make_raw_probe(
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "AAC", "properties": {"language": "jpn", "default_track": True, "audio_channels": 6}},
                {"id": 2, "type": "audio", "codec": "AAC", "properties": {"language": "eng", "track_name": "Director Commentary", "default_track": False, "audio_channels": 2}},
            ]
        )
        und_raw = _make_raw_probe(
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "AAC", "properties": {"language": "und", "default_track": True, "audio_channels": 6}},
            ]
        )
        rows = [
            _make_media_row(file_id=1, raw_json=jpn_raw),
            _make_media_row(file_id=2, raw_json=jpn_raw),
            _make_media_row(file_id=3, raw_json=und_raw),
        ]
        majority, detected = detect_majority_audio_language(rows)
        assert majority == "jpn"
        assert "jpn" in detected

        # All untagged files -> MUST return 'und' (never assume 'eng')
        all_und_majority, all_und_detected = detect_majority_audio_language(
            [_make_media_row(file_id=4, raw_json=und_raw)]
        )
        assert all_und_majority == "und"
        assert all_und_detected == []

        # Empty selection -> safe 'und'
        assert detect_majority_audio_language([]) == ("und", [])

    def test_retag_und_to_und_is_noop_and_retag_to_jpn_applies_cleanly(self) -> None:
        und_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "und", "default_track": True, "audio_channels": 6}},
            ],
        )
        row = _make_media_row(file_id=30, container_title="", raw_json=und_raw, has_aac_51=False, has_ac3_eac3=True)

        # Target = 'und' -> clean no-op (Skip Compliant, no fake retag chip)
        noop_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(retag_und_audio_to_preferred=True, retag_und_target_language="und"),
        )
        noop_plan = compile_batch_rule_for_file(row, noop_rule)
        assert noop_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert all(c.chip_id != "retag_und" for c in noop_plan.chips)

        # Target = 'jpn' -> enqueues remux and retags primary audio to 'jpn' with diff_from/diff_to
        jpn_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(retag_und_audio_to_preferred=True, retag_und_target_language="jpn"),
        )
        jpn_plan = compile_batch_rule_for_file(row, jpn_rule)
        assert jpn_plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert jpn_plan.job_config is not None
        aud = [t for t in jpn_plan.job_config.tracks if t.id == 1][0]
        assert aud.language == "jpn"
        aud_chip = [c for c in jpn_plan.chips if c.chip_id == "audio_track:1"][0]
        assert aud_chip.diff_from == "und"
        assert aud_chip.diff_to == "jpn"

        # Retagging existing 'hin' primary audio -> 'eng' when checkbox is ON shows hin -> eng diff;
        # when checkbox is OFF, original 'hin' is preserved with no diff and no retag chip.
        hin_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "hin", "default_track": True, "audio_channels": 6}},
            ],
        )
        hin_row = _make_media_row(file_id=31, container_title="", raw_json=hin_raw, has_aac_51=False, has_ac3_eac3=True)

        off_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(retag_und_audio_to_preferred=False, retag_und_target_language="eng"),
        )
        off_plan = compile_batch_rule_for_file(hin_row, off_rule)
        assert off_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        off_aud_chip = [c for c in off_plan.chips if c.chip_id == "audio_track:1"][0]
        assert off_aud_chip.diff_from is None
        assert off_aud_chip.diff_to is None
        assert all(c.chip_id != "retag_und" for c in off_plan.chips)

        on_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(retag_und_audio_to_preferred=True, retag_und_target_language="eng"),
        )
        on_plan = compile_batch_rule_for_file(hin_row, on_rule)
        assert on_plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        on_aud_chip = [c for c in on_plan.chips if c.chip_id == "audio_track:1"][0]
        assert on_aud_chip.diff_from == "hin"
        assert on_aud_chip.diff_to == "eng"
        retag_chip = [c for c in on_plan.chips if c.chip_id == "retag_und"][0]
        assert retag_chip.diff_from == "hin"
        assert retag_chip.diff_to == "eng"
        assert retag_chip.selected is True

        # Same language (hin -> hin) with checkbox ON stays a clean no-op
        same_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            track_filter=BatchTrackFilterRule(retag_und_audio_to_preferred=True, retag_und_target_language="hin"),
        )
        same_plan = compile_batch_rule_for_file(hin_row, same_rule)
        assert same_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        assert all(c.chip_id != "retag_und" for c in same_plan.chips)

    def test_compliant_file_exposes_selectable_chips_and_opt_in_overrides_flip_to_enqueue(self) -> None:
        raw_payload = {
            "container": {"properties": {}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
                {"id": 2, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "eng", "default_track": False}},
                {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "fra", "default_track": False}},
            ],
            "global_tags": [],
            "track_tags": [],
            "attachments": [{"id": 1, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 1024}],
            "chapters": [],
        }
        row = _make_media_row(
            file_id=40,
            container_title="",
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=json.dumps(raw_payload),
        )
        rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="clear", strip_global_tags=True, strip_track_tags=True, strip_attachments=False),
            audio=BatchAudioRule(enabled=True, target_codec="eac3", accept_existing_dolby=True),
        )

        # Default: Skip (Compliant), yet selectable Audio/Subtitle/Tag chips are visible
        # (and redundant audio_convert chip is hidden since source is already EAC3 5.1 == target eac3!)
        base_plan = compile_batch_rule_for_file(row, rule)
        assert base_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        chip_ids = {c.chip_id for c in base_plan.chips}
        assert "audio_track:1" in chip_ids
        assert "audio_convert" not in chip_ids
        assert "sub_track:2" in chip_ids
        assert "sub_track:3" in chip_ids
        assert "tag:attachments" in chip_ids

        # User opts in per-file to drop French subtitle #3 and strip attachments -> flips to Fast Remux Only!
        override = BatchFileOverride(track_enabled={3: False}, strip_attachments=True)
        opt_in_plan = compile_batch_rule_for_file(row, rule, file_override=override)
        assert opt_in_plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert opt_in_plan.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
        assert opt_in_plan.has_overrides is True
        assert opt_in_plan.job_config is not None
        assert opt_in_plan.job_config.container.strip_attachments is True
        sub_map = {t.id: t.enabled for t in opt_in_plan.job_config.tracks if t.type == "subtitles"}
        assert sub_map == {2: True, 3: False}

    def test_explicit_audio_drop_allowed_on_video_files_and_guarded_on_audio_only_and_active_jobs(self) -> None:
        row = _make_media_row(file_id=50, container_title="Messy.Title")
        rule = BatchRuleSpec(audio=BatchAudioRule(enabled=False))

        # Audio chip on a normal video+audio file is clickable (not grayed/disabled)
        base_plan = compile_batch_rule_for_file(row, rule)
        base_audio_chip = [c for c in base_plan.chips if c.chip_id == "audio_track:1"][0]
        assert base_audio_chip.disabled is False

        # Explicitly unchecking the only audio track (#1) on a video file IS allowed (creates silent MKV + warning)
        # while unknown track #999 is safely ignored
        silent_override = BatchFileOverride(track_enabled={1: False, 999: False})
        plan = compile_batch_rule_for_file(row, rule, file_override=silent_override)
        assert plan.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert plan.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
        assert plan.job_config is not None
        aud_tracks = [t for t in plan.job_config.tracks if t.type == "audio"]
        assert aud_tracks[0].enabled is False
        assert any("no audio" in w.lower() for w in plan.warnings)
        audio_chip = [c for c in plan.chips if c.chip_id == "audio_track:1"][0]
        assert audio_chip.selected is False
        assert audio_chip.overridden is True
        assert audio_chip.disabled is False

        # Audio-only file (no video track) still prevents dropping the only track
        audio_only_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        audio_only_row = _make_media_row(file_id=51, container_title="", raw_json=audio_only_raw)
        ao_plan = compile_batch_rule_for_file(
            audio_only_row,
            rule,
            file_override=BatchFileOverride(track_enabled={1: False}),
        )
        ao_chip = [c for c in ao_plan.chips if c.chip_id == "audio_track:1"][0]
        assert ao_chip.selected is True
        assert ao_chip.disabled is True
        assert ao_chip.disabled_reason is not None

        # Active-job Guardrail #3 cannot be bypassed by per-file overrides
        busy_plan = compile_batch_rule_for_file(
            row,
            rule,
            active_job_filepaths={row["filepath"]},
            file_override=BatchFileOverride(clear_title=True, audio_convert_enabled=True),
        )
        assert busy_plan.action == AppConstants.BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB
        assert busy_plan.job_config is None

    def test_convert_chip_visibility_flac_guardrail_apply_to_and_legacy_template_compat(self) -> None:
        aac_row = _make_media_row(file_id=60, container_title="Messy.Title")
        eac3_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        eac3_row = _make_media_row(file_id=61, container_title="", raw_json=eac3_raw, has_aac_51=False, has_ac3_eac3=True)

        # 1. Global audio conversion OFF -> hide audio_convert chip completely
        off_plan = compile_batch_rule_for_file(aac_row, BatchRuleSpec(audio=BatchAudioRule(enabled=False)))
        assert all(c.chip_id != "audio_convert" for c in off_plan.chips)

        # 2. E-AC-3 source with target=ac3 under 'incompatible_or_non_dolby' -> skipped by default, opt-in chip shown after existing tracks
        dolby_skip_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=True, target_codec="ac3", bitrate="448k", match_condition="incompatible_or_non_dolby", accept_existing_dolby=True),
        )
        p_eac3_skip = compile_batch_rule_for_file(eac3_row, dolby_skip_rule)
        assert p_eac3_skip.action == AppConstants.BATCH_PLAN_ACTION_SKIP_COMPLIANT
        skip_audio_chips = [c for c in p_eac3_skip.chips if c.category == AppConstants.BATCH_CHIP_CAT_AUDIO]
        assert skip_audio_chips[0].chip_id == "audio_track:1"
        assert skip_audio_chips[-1].chip_id == "audio_convert"
        assert skip_audio_chips[-1].selected is False
        assert skip_audio_chips[-1].label.startswith("Add AC3 5.1")

        # 3. E-AC-3 source with target=ac3 under 'any_non_target' -> converts E-AC-3 -> AC3 AND places new audio chip at Index 0!
        all_non_target_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=True, target_codec="ac3", bitrate="448k", match_condition="any_non_target", accept_existing_dolby=True),
        )
        p_eac3_convert = compile_batch_rule_for_file(eac3_row, all_non_target_rule)
        assert p_eac3_convert.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert p_eac3_convert.stage_plan == AppConstants.BATCH_STAGE_TRANSCODE_AND_REMUX
        upgrade_audio_chips = [c for c in p_eac3_convert.chips if c.category == AppConstants.BATCH_CHIP_CAT_AUDIO]
        assert upgrade_audio_chips[0].chip_id == "audio_convert"
        assert upgrade_audio_chips[0].selected is True
        assert upgrade_audio_chips[0].label.startswith("#0 New: AC3 5.1")
        assert upgrade_audio_chips[1].chip_id == "audio_track:1"

        # 4. Guardrail #1: Target=FLAC on lossy sources (AAC/E-AC-3) hides audio_convert chip & blocks opt-in
        flac_rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=True, target_codec="flac", match_condition="any_non_target"),
        )
        for lossy_row in (aac_row, eac3_row):
            p_flac = compile_batch_rule_for_file(
                lossy_row,
                flac_rule,
                file_override=BatchFileOverride(audio_convert_enabled=True),
            )
            assert all(c.chip_id != "audio_convert" for c in p_flac.chips)
            assert p_flac.stage_plan != AppConstants.BATCH_STAGE_TRANSCODE_AND_REMUX

        # 5. Guardrail #2: Legacy saved template JSON with accept_existing_dolby deserializes cleanly
        legacy_json = json.dumps(
            {
                "container": {"title_action": "clear", "strip_global_tags": True, "strip_track_tags": True},
                "audio": {"enabled": True, "target_codec": "eac3", "accept_existing_dolby": True, "bitrate": "640k"},
                "track_filter": {"retag_und_audio_to_preferred": False, "preferred_language": "eng"},
                "output_mode": "keep_both",
            }
        )
        parsed_legacy = BatchRuleSpec.model_validate(json.loads(legacy_json))
        assert parsed_legacy.audio.accept_existing_dolby is True
        assert parsed_legacy.audio.target_codec == "eac3"


class TestEndToEndCommandTranslationAudit:
    """Audit exact per-file FFmpeg and mkvmerge argv translation under heterogeneous per-file overrides."""

    def test_unique_ffmpeg_and_mkvmerge_commands_per_file_with_overrides(self, tmp_path: Path) -> None:
        # Shared global rule: Convert non-Dolby to EAC3 640k + Drop source audio + Clear title + Retag to eng
        shared_rule = BatchRuleSpec(
            container=BatchContainerRule(
                title_action="clear",
                strip_global_tags=True,
                strip_track_tags=True,
                strip_chapters=False,
                smart_fill_track_names=False,
            ),
            audio=BatchAudioRule(
                enabled=True,
                target_codec="eac3",
                bitrate="640k",
                match_condition="incompatible_or_non_dolby",
                drop_source_track=True,
            ),
            track_filter=BatchTrackFilterRule(
                retag_und_audio_to_preferred=True,
                retag_und_target_language="eng",
                keep_subtitle_mode="all",
            ),
            output_mode="keep_both",
        )

        # File 1: Commentary at audio_index=0 (id=1), Main AAC 5.1 hin at audio_index=1 (id=2), 2 subs (id=3 eng, id=4 fra), chapters
        raw_ep1 = {
            "container": {"properties": {"title": "Ep1.Release"}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "AAC", "properties": {"language": "eng", "track_name": "Director Commentary", "default_track": True, "audio_channels": 2}},
                {"id": 2, "type": "audio", "codec": "AAC", "properties": {"language": "hin", "track_name": "Main", "default_track": False, "audio_channels": 6, "minimum_timestamp": 25_000_000}},
                {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "eng", "default_track": False}},
                {"id": 4, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "fra", "default_track": False}},
            ],
            "global_tags": [{"num_entries": 2}],
            "track_tags": [],
            "attachments": [],
            "chapters": [{"num_entries": 5}],
        }
        ep1 = _make_media_row(file_id=101, filepath="S01/Ep1.mkv", filename="Ep1.mkv", container_title="Ep1.Release", has_global_tags=True, raw_json=json.dumps(raw_ep1))

        # File 2: Same AAC 5.1 hin at audio_index=0 (id=1), 2 subs (id=2 eng, id=3 fra) — user overrides to KEEP source audio #1, KEEP title, DROP sub #3, and SKIP retag!
        raw_ep2 = {
            "container": {"properties": {"title": "Ep2.KeepMe"}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "AAC", "properties": {"language": "hin", "default_track": True, "audio_channels": 6}},
                {"id": 2, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "eng", "default_track": False}},
                {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"language": "fra", "default_track": False}},
            ],
            "global_tags": [{"num_entries": 1}],
            "track_tags": [],
            "attachments": [],
            "chapters": [{"num_entries": 3}],
        }
        ep2 = _make_media_row(file_id=102, filepath="S01/Ep2.mkv", filename="Ep2.mkv", container_title="Ep2.KeepMe", has_global_tags=True, raw_json=json.dumps(raw_ep2))

        # File 3: User opts OUT of audio conversion & drops only audio track -> silent video-only remux!
        ep3 = _make_media_row(file_id=103, filepath="S01/Ep3.mkv", filename="Ep3.mkv", container_title="Ep3.Release", has_global_tags=False, raw_json=json.dumps(raw_ep2))

        overrides = {
            101: BatchFileOverride(strip_chapters=True),  # Ep1: also strip chapters, keep global drop_source_track=True
            102: BatchFileOverride(
                track_enabled={1: True, 3: False},  # Ep2: override drop_source_track to KEEP source #1, drop French sub #3
                clear_title=False,                  # Ep2: KEEP container title
                retag_und_enabled=False,            # Ep2: KEEP original 'hin' tag
            ),
            103: BatchFileOverride(
                audio_convert_enabled=False,        # Ep3: NO FFmpeg stage
                track_enabled={1: False},           # Ep3: explicit silent MKV (--no-audio)
            ),
        }

        preview = build_batch_preview_response(
            [ep1, ep2, ep3],
            shared_rule,
            batch_scope_label="3 files",
            active_job_filepaths=set(),
            storage_root=tmp_path,
            file_overrides=overrides,
        )
        plans = {p.file_id: p for p in preview.items}

        # --- Audit Ep1: Stage 1 FFmpeg maps 0:a:1 (skips commentary!), Stage 2 drops source #2, strips chapters, clears title, syncs 25ms, retags #0 to eng ---
        p1 = plans[101]
        assert p1.job_config is not None
        assert p1.job_config.audio_conversion.enabled is True
        assert p1.job_config.audio_conversion.drop_source_track is True
        # UI chip for dropped source track #2 MUST honestly show selected=False (matching mkvmerge!)
        ep1_src_chip = [c for c in p1.chips if c.chip_id == "audio_track:2"][0]
        assert ep1_src_chip.selected is False
        ep1_new_chip = [c for c in p1.chips if c.chip_id == "audio_convert"][0]
        assert ep1_new_chip.diff_from == "hin" and ep1_new_chip.diff_to == "eng"

        ff1 = build_ffmpeg_command(
            tmp_path / "Ep1.mkv",
            tmp_path / "Ep1.tmp.eac3",
            audio_index=p1.job_config.audio_conversion.source_audio_index,
            target_codec=p1.job_config.audio_conversion.target_codec,
            bitrate=p1.job_config.audio_conversion.bitrate,
            channels=p1.job_config.audio_conversion.source_channels,
            source_codec=p1.job_config.audio_conversion.source_codec,
        )
        assert ff1[ff1.index("-map") + 1] == "0:a:1"
        assert ff1[ff1.index("-c:a") + 1] == "eac3"
        assert ff1[ff1.index("-b:a") + 1] == "640k"

        mkv1 = build_mkvmerge_command(
            tmp_path / "Ep1.mkv",
            tmp_path / "Ep1.out.mkv",
            p1.job_config,
            temp_audio_path=tmp_path / "Ep1.tmp.eac3",
        )
        assert "--title" in mkv1 and mkv1[mkv1.index("--title") + 1] == ""
        assert "--no-chapters" in mkv1
        assert "--sync" in mkv1 and mkv1[mkv1.index("--sync") + 1] == "0:25"
        assert mkv1[mkv1.index("--language") + 1] == "0:eng"
        assert mkv1[mkv1.index("--audio-tracks") + 1] == "1"  # Commentary #1 kept, source #2 dropped!
        assert mkv1[mkv1.index("--subtitle-tracks") + 1] == "3,4"
        assert mkv1[mkv1.index("--track-order") + 1].startswith("1:0,0:0,")

        # --- Audit Ep2: Stage 1 maps 0:a:0, Stage 2 KEEPS source #1, KEEPS title, KEEPS chapters, DROPS sub #3, KEEPS 'hin' tag ---
        p2 = plans[102]
        assert p2.job_config is not None
        assert p2.job_config.audio_conversion.enabled is True
        assert p2.job_config.audio_conversion.drop_source_track is False
        assert p2.job_config.audio_conversion.language == "hin"
        ep2_src_chip = [c for c in p2.chips if c.chip_id == "audio_track:1"][0]
        assert ep2_src_chip.selected is True and ep2_src_chip.overridden is True

        ff2 = build_ffmpeg_command(
            tmp_path / "Ep2.mkv",
            tmp_path / "Ep2.tmp.eac3",
            audio_index=p2.job_config.audio_conversion.source_audio_index,
            target_codec=p2.job_config.audio_conversion.target_codec,
            bitrate=p2.job_config.audio_conversion.bitrate,
            channels=p2.job_config.audio_conversion.source_channels,
            source_codec=p2.job_config.audio_conversion.source_codec,
        )
        assert ff2[ff2.index("-map") + 1] == "0:a:0"

        mkv2 = build_mkvmerge_command(
            tmp_path / "Ep2.mkv",
            tmp_path / "Ep2.out.mkv",
            p2.job_config,
            temp_audio_path=tmp_path / "Ep2.tmp.eac3",
        )
        assert "--title" not in mkv2  # Preserves Ep2.KeepMe!
        assert "--no-chapters" not in mkv2[mkv2.index(str(tmp_path / "Ep2.tmp.eac3")) :]
        assert mkv2[mkv2.index("--language") + 1] == "0:hin"
        assert mkv2[mkv2.index("--audio-tracks") + 1] == "1"  # Source #1 KEPT alongside #0 New!
        assert mkv2[mkv2.index("--subtitle-tracks") + 1] == "2"  # French sub #3 dropped!
        assert mkv2[mkv2.index("--track-order") + 1] == "1:0,0:0,1:1,1:2"

        # --- Audit Ep3: No Stage 1 FFmpeg, Stage 2 emits --no-audio cleanly ---
        p3 = plans[103]
        assert p3.job_config is not None
        assert p3.job_config.audio_conversion.enabled is False
        mkv3 = build_mkvmerge_command(
            tmp_path / "Ep3.mkv",
            tmp_path / "Ep3.out.mkv",
            p3.job_config,
            temp_audio_path=None,
        )
        assert "--no-audio" in mkv3
        assert "--audio-tracks" not in mkv3
        assert mkv3[mkv3.index("--track-order") + 1] == "0:0,0:2,0:3"

    def test_video_track_chip_at_index_zero_removal_emits_no_video_and_guards_empty_container(
        self, tmp_path: Path
    ) -> None:
        raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True, "pixel_dimensions": "1920x1080"}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        row = _make_media_row(
            file_id=201,
            filepath="S01/VideoDrop.mkv",
            filename="VideoDrop.mkv",
            container_title="",
            has_aac_51=False,
            has_ac3_eac3=True,
            raw_json=raw,
        )
        rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
        )

        # 1. Default compliant file exposes Video #0 chip first (index 0) and clickable
        base = compile_batch_rule_for_file(row, rule)
        assert base.chips[0].chip_id == "video_track:0"
        assert base.chips[0].category == AppConstants.BATCH_CHIP_CAT_VIDEO
        assert base.chips[0].label == "#0 HEVC · 1920x1080"
        assert base.chips[0].selected is True
        assert base.chips[0].disabled is False

        # 2. Unchecking Video #0 flips file to Fast Remux Only and emits `--no-video` to mkvmerge!
        no_vid = compile_batch_rule_for_file(row, rule, file_override=BatchFileOverride(track_enabled={0: False}))
        assert no_vid.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE
        assert no_vid.stage_plan == AppConstants.BATCH_STAGE_REMUX_ONLY
        assert no_vid.job_config is not None
        assert no_vid.chips[0].selected is False
        assert no_vid.chips[0].overridden is True
        assert any("--no-video" in w for w in no_vid.warnings)

        # Audio #1 is now the last remaining track, so its chip locks to prevent a 0-track container
        aud_chip = [c for c in no_vid.chips if c.chip_id == "audio_track:1"][0]
        assert aud_chip.disabled is True

        mkv = build_mkvmerge_command(
            tmp_path / "VideoDrop.mkv",
            tmp_path / "VideoDrop.out.mkv",
            no_vid.job_config,
        )
        assert "--no-video" in mkv
        assert "--video-tracks" not in mkv
        assert mkv[mkv.index("--audio-tracks") + 1] == "1"
        assert mkv[mkv.index("--track-order") + 1] == "0:1"

        # 3. Negative case: trying to drop BOTH video #0 and audio #1 safely keeps Track #0
        empty_try = compile_batch_rule_for_file(
            row,
            rule,
            file_override=BatchFileOverride(track_enabled={0: False, 1: False}),
        )
        assert empty_try.job_config is not None
        kept = [t for t in empty_try.job_config.tracks if t.enabled]
        assert len(kept) == 1
        assert kept[0].id == 0
        assert any("at least one track" in w for w in empty_try.warnings)


class TestBatchRenameSaveAsAndCollisions:
    """Verify Guardrails #1 (natural sort), #2 (rename on compliant), #3 (collisions), & Save As."""

    def test_natural_sort_episode_extraction_and_prefix_auto_append(self, tmp_path: Path) -> None:
        assert extract_season_episode("Show.S02E09.1080p.HEVC.mkv") == (2, 9)
        assert extract_season_episode("Show 1x04.mkv") == (1, 4)
        assert extract_season_episode("Show Ep 12.mkv") == (None, 12)
        assert extract_season_episode("Movie.2024.1080p.x265.mkv") == (None, None)
        assert sanitize_batch_output_filename("../../evil:name*.mp4") == "evilname.mkv"
        assert natural_sort_key("Ep2.mkv") < natural_sort_key("Ep10.mkv")

        compliant_raw = _make_raw_probe(
            title="",
            tracks=[
                {"id": 0, "type": "video", "codec": "HEVC", "properties": {"language": "und", "default_track": True}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"language": "eng", "default_track": True, "audio_channels": 6}},
            ],
        )
        # Deliberately pass out-of-order rows: Ep10, Ep2, Bonus (no ep #)
        rows = [
            _make_media_row(file_id=10, filepath="S1/Ep10.mkv", filename="Ep10.mkv", container_title="", has_aac_51=False, has_ac3_eac3=True, raw_json=compliant_raw),
            _make_media_row(file_id=2, filepath="S1/Ep2.mkv", filename="Ep2.mkv", container_title="", has_aac_51=False, has_ac3_eac3=True, raw_json=compliant_raw),
            _make_media_row(file_id=1, filepath="S1/Bonus.mkv", filename="Bonus.mkv", container_title="", has_aac_51=False, has_ac3_eac3=True, raw_json=compliant_raw),
        ]
        # Plain prefix 'MY_SHOW_S1_' (no tokens) auto-appends {ep}, Replace Original -> atomic save_as + delete_original=True
        rule = BatchRuleSpec(
            container=BatchContainerRule(title_action="keep", strip_global_tags=False, strip_track_tags=False),
            audio=BatchAudioRule(enabled=False),
            rename=BatchRenameRule(enabled=True, pattern="MY_SHOW_S1_", start_number=1),
            output_mode="replace",
        )
        plans = compile_batch_plans(
            rows,
            rule,
            active_job_filepaths=set(),
            storage_root=tmp_path,
        )
        # Guardrail #1: Natural sort places Bonus, Ep2, Ep10 (Ep2 strictly before Ep10!)
        assert [p.filename for p in plans] == ["Bonus.mkv", "Ep2.mkv", "Ep10.mkv"]
        # Guardrail #2: All 3 compliant files flip to actionable Fast Remux Only because rename is active!
        assert all(p.action == AppConstants.BATCH_PLAN_ACTION_ENQUEUE for p in plans)
        assert [p.output_filename for p in plans] == [
            "MY_SHOW_S1_01.mkv",  # fallback to natural seq #1
            "MY_SHOW_S1_02.mkv",  # extracted ep 2
            "MY_SHOW_S1_10.mkv",  # extracted ep 10
        ]
        assert all(p.job_config is not None and p.job_config.output_mode == "save_as" for p in plans)
        assert all(p.job_config is not None and p.job_config.delete_original is True for p in plans)

    def test_save_as_destination_folder_and_inline_override_and_collisions(self, tmp_path: Path) -> None:
        raw = _make_raw_probe(title="")
        ep1 = _make_media_row(file_id=1, filepath="S1/Ep1.mkv", filename="Ep1.mkv", container_title="", raw_json=raw)
        ep2 = _make_media_row(file_id=2, filepath="S1/Ep2.mkv", filename="Ep2.mkv", container_title="", raw_json=raw)

        # Create pre-existing file on disk to test on-disk collision
        dest_dir = tmp_path / "Clean"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "Existing.mkv").write_bytes(b"already-there")

        rule = BatchRuleSpec(
            audio=BatchAudioRule(enabled=False),
            output_mode="save_as",
            destination_folder="/Clean",
            delete_original=False,
        )

        # 1. Intra-batch duplicate target collision flags BOTH files
        dup_preview = build_batch_preview_response(
            [ep1, ep2],
            rule,
            batch_scope_label="2 files",
            active_job_filepaths=set(),
            storage_root=tmp_path,
            file_overrides={
                1: BatchFileOverride(output_filename="Duplicate.mkv"),
                2: BatchFileOverride(output_filename="Duplicate.mkv"),
            },
        )
        assert dup_preview.collision_count == 2
        assert all(p.collision_error and "Duplicate" in p.collision_error for p in dup_preview.items)

        # 2. On-disk collision flags Ep1 while Ep2 succeeds to /Clean/Ep2_Custom.mkv
        disk_preview = build_batch_preview_response(
            [ep1, ep2],
            rule,
            batch_scope_label="2 files",
            active_job_filepaths=set(),
            storage_root=tmp_path,
            file_overrides={
                1: BatchFileOverride(output_filename="Existing.mkv"),
                2: BatchFileOverride(output_filename="Ep2_Custom.mkv"),
            },
        )
        assert disk_preview.collision_count == 1
        by_id = {p.file_id: p for p in disk_preview.items}
        assert by_id[1].collision_error and "already exists on disk" in by_id[1].collision_error
        assert by_id[2].collision_error is None
        assert by_id[2].output_rel_path == "Clean/Ep2_Custom.mkv"
        assert by_id[2].job_config is not None
        assert by_id[2].job_config.output_mode == "save_as"
        assert by_id[2].job_config.output_path == "Clean/Ep2_Custom.mkv"
        assert by_id[2].job_config.delete_original is False





