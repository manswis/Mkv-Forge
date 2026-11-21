"""Unit tests for app.services.job_summary human-readable job change extraction."""

from __future__ import annotations

import json

import pytest

from app.services.job_summary import summarize_job_changes


class TestSummarizeJobChanges:
    """Verify added, removed, changed, output, and malformed-JSON edge cases."""

    def test_full_audio_metadata_tracks_and_save_as_summary(self) -> None:
        cfg = {
            "container": {
                "title": "",
                "strip_global_tags": True,
                "strip_track_tags": True,
                "strip_attachments": True,
                "strip_chapters": False,
            },
            "audio_conversion": {
                "enabled": True,
                "source_track_id": 1,
                "source_audio_index": 0,
                "source_channels": 6,
                "target_codec": "eac3",
                "bitrate": "640k",
                "language": "eng",
                "set_as_default": True,
                "drop_source_track": True,
            },
            "tracks": [
                {"id": 0, "type": "video", "enabled": True},
                {"id": 1, "type": "audio", "enabled": True},
                {"id": 2, "type": "subtitles", "enabled": False},
                {"id": 3, "type": "subtitles", "enabled": False},
            ],
            "output_mode": "save_as",
            "output_path": "exports/Movie (Dolby).mkv",
        }
        mode, changes = summarize_job_changes(
            json.dumps(cfg),
            filepath="movies/Movie.mkv",
            output_filepath="exports/Movie (Dolby).mkv",
        )
        assert mode == "save_as"
        pairs = [(c.kind, c.label) for c in changes]
        assert ("added", "+ EAC3 5.1 (640k • eng) • Default") in pairs
        assert ("removed", "Dropped base audio (#1)") in pairs
        assert ("removed", "Cleared title") in pairs
        assert ("removed", "Stripped global tags, track tags, attachments") in pairs
        assert ("removed", "Removed 2 subtitle tracks") in pairs
        assert ("output", "Saved as /exports/Movie (Dolby).mkv") in pairs

    def test_mp4_replace_updated_title_and_stereo_aac_output(self) -> None:
        cfg = {
            "container": {"title": "Clean Movie Title", "strip_global_tags": False},
            "audio_conversion": {
                "enabled": True,
                "source_channels": 2,
                "target_codec": "aac",
                "bitrate": "256k",
                "language": "und",
                "set_as_default": False,
                "drop_source_track": False,
            },
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        mode, changes = summarize_job_changes(
            json.dumps(cfg),
            filepath="movies/Clip.mp4",
            output_filepath="movies/Clip.mkv",
        )
        assert mode == "replace"
        pairs = [(c.kind, c.label) for c in changes]
        assert ("added", "+ AAC 2.0 (256k)") in pairs
        assert ("changed", 'Title → "Clean Movie Title"') in pairs
        assert ("output", ".mp4 → Clip.mkv") in pairs

    def test_keep_both_and_metadata_order_only_fallback(self) -> None:
        keep_cfg = {
            "container": {"title": None, "strip_global_tags": False},
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "keep_both",
        }
        mode, changes = summarize_job_changes(
            json.dumps(keep_cfg),
            filepath="movies/A.mkv",
            output_filepath="movies/A.remux.2.mkv",
        )
        assert mode == "keep_both"
        assert [(c.kind, c.label) for c in changes] == [
            ("output", "Keep Both → A.remux.2.mkv")
        ]

        # Reorder-only replace on .mkv -> fallback summary
        order_cfg = {
            "container": {"title": None, "strip_global_tags": False},
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        mode2, changes2 = summarize_job_changes(
            json.dumps(order_cfg), filepath="movies/A.mkv"
        )
        assert mode2 == "replace"
        assert [(c.kind, c.label) for c in changes2] == [
            ("changed", "Updated track metadata / order")
        ]

    @pytest.mark.parametrize("bad_json", [None, "", "{not-json", "[]", "123"])
    def test_degrades_safely_on_malformed_or_missing_config(
        self, bad_json: str | None
    ) -> None:
        mode, changes = summarize_job_changes(bad_json, filepath="movies/X.mkv")
        assert mode is None
        assert changes == []

    @pytest.mark.parametrize(
        ("audio_cfg", "expected_label"),
        [
            (
                {
                    "enabled": True,
                    "source_channels": 8,
                    "target_channels": 8,
                    "target_codec": "flac",
                    "bitrate": None,
                    "language": "eng",
                    "set_as_default": True,
                },
                "+ FLAC 7.1 (Lossless • eng) • Default",
            ),
            (
                {
                    "enabled": True,
                    "source_channels": 8,
                    "target_channels": 8,
                    "target_codec": "opus",
                    "bitrate": "448k",
                    "language": "eng",
                    "set_as_default": False,
                },
                "+ Opus 7.1 (448k • eng)",
            ),
            (
                {
                    "enabled": True,
                    "source_channels": 8,
                    "target_channels": None,
                    "target_codec": "eac3",
                    "bitrate": "768k",
                    "language": "eng",
                    "set_as_default": True,
                },
                "+ EAC3 5.1 (768k • eng) • Default",
            ),
            (
                {
                    "enabled": True,
                    "source_channels": 6,
                    "target_channels": 2,
                    "target_codec": "aac",
                    "bitrate": "256k",
                    "language": "jpn",
                    "set_as_default": False,
                },
                "+ AAC 2.0 (256k • jpn)",
            ),
            (
                {
                    "enabled": True,
                    "source_channels": 7,
                    "target_channels": 7,
                    "target_codec": "opus",
                    "bitrate": "384k",
                    "language": "und",
                    "set_as_default": False,
                },
                "+ Opus 6.1 (384k)",
            ),
            (
                {
                    "enabled": True,
                    "source_channels": 4,
                    "target_channels": 1,
                    "target_codec": "ac3",
                    "bitrate": "192k",
                    "language": "eng",
                    "set_as_default": False,
                },
                "+ AC3 1.0 (192k • eng)",
            ),
        ],
    )
    def test_phase14_codecs_lossless_and_target_channel_labels(
        self, audio_cfg: dict, expected_label: str
    ) -> None:
        cfg = {
            "container": {"title": None, "strip_global_tags": False},
            "audio_conversion": audio_cfg,
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes = summarize_job_changes(json.dumps(cfg), filepath="movies/M.mkv")
        pairs = [(c.kind, c.label) for c in changes]
        assert ("added", expected_label) in pairs
