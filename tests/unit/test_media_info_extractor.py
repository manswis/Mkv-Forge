"""Unit tests for app.services.media_info_extractor."""

from __future__ import annotations

from app.services.media_info_extractor import extract_media_file_info


def _sample_row(**overrides) -> dict:
    base = {
        "id": 42,
        "filepath": "shows/Sample/S01E01.mkv",
        "folder": "/shows/Sample",
        "filename": "S01E01.mkv",
        "size_bytes": 1_500_000_000,
        "duration_us": 2_000_000_000,  # 2000s -> 6,000,000 bps
        "resolution": "2160p",
        "video_codec": "HEVC",
        "video_dynamic_range": "HDR10",
        "scanned_at": "2026-11-19T20:00:00Z",
    }
    base.update(overrides)
    return base


class TestMediaInfoExtractor:
    """Verify full MediaInfo extraction and resilience against malformed payloads."""

    def test_extracts_full_container_tracks_attachments_chapters_and_tags(self) -> None:
        raw_payload = {
            "container": {
                "type": "Matroska",
                "properties": {
                    "title": "Sample Episode 1",
                    "duration": 2_000_000_000_000,
                    "muxing_application": "libebml v1.4.5 + libmatroska v1.7.1",
                    "writing_application": "mkvmerge v86.0",
                    "date_utc": "2026-10-01T12:00:00Z",
                    "segment_uid": "0xabcdef123456",
                },
            },
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC/H.265/MPEG-H",
                    "properties": {
                        "codec_id": "V_MPEGH/ISO/HEVC",
                        "language": "und",
                        "pixel_dimensions": "3840x2160",
                        "display_dimensions": "3840x2160",
                        "default_duration": 41_708_333,
                        "color_primaries": 9,
                        "color_transfer_characteristics": 16,
                        "color_matrix_coefficients": 9,
                        "color_bits_per_channel": 10,
                        "tag_bps": 12_500_000,
                        "tag_number_of_frames": 47952,
                        "default_track": True,
                        "enabled_track": True,
                        "uid": 101,
                    },
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "E-AC-3",
                    "properties": {
                        "codec_id": "A_EAC3",
                        "language": "hin",
                        "language_ietf": "hi-IN",
                        "track_name": "Hindi Dolby Digital Plus 5.1",
                        "audio_channels": 6,
                        "audio_sampling_frequency": 48000,
                        "audio_bits_per_sample": 24,
                        "tag_bps": 640000,
                        "default_track": True,
                        "flag_original": True,
                        "minimum_timestamp": 15_000_000,
                    },
                },
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {
                        "codec_id": "S_TEXT/UTF8",
                        "language": "eng",
                        "track_name": "English SDH",
                        "text_subtitles": True,
                        "encoding": "UTF-8",
                        "flag_hearing_impaired": True,
                        "tag_number_of_frames": 842,
                    },
                },
            ],
            "attachments": [
                {
                    "id": 1,
                    "file_name": "Roboto-Regular.ttf",
                    "content_type": "application/x-truetype-font",
                    "size": 168260,
                    "description": "Subtitle font",
                },
                {
                    "id": 2,
                    "file_name": "cover.jpg",
                    "content_type": "image/jpeg",
                    "size": 52400,
                },
            ],
            "chapters": [{"num_entries": 12}],
            "global_tags": [{"num_entries": 3}],
            "track_tags": [{"num_entries": 4}, {"num_entries": 5}],
            "warnings": ["Container timestamp minor drift"],
        }

        info = extract_media_file_info(_sample_row(), raw_payload)

        assert info.id == 42
        assert info.container.container_type == "Matroska"
        assert info.container.title == "Sample Episode 1"
        assert info.container.overall_bitrate_bps == 6_000_000
        assert info.container.muxing_application == "libebml v1.4.5 + libmatroska v1.7.1"
        assert info.container.writing_application == "mkvmerge v86.0"
        assert info.container.chapters_count == 12
        assert info.container.attachments_count == 2
        assert info.container.global_tags_count == 3
        assert info.container.track_tags_count == 9

        assert len(info.video_tracks) == 1
        v = info.video_tracks[0]
        assert v.pixel_dimensions == "3840x2160"
        assert v.frame_rate == "23.976 fps"
        assert v.dynamic_range == "HDR10"
        assert v.color_primaries == "BT.2020"
        assert v.color_transfer == "SMPTE ST 2084 (PQ)"
        assert v.color_matrix == "BT.2020 NCL"
        assert v.bit_depth == 10
        assert v.bitrate_bps == 12_500_000
        assert v.frame_count == 47952

        assert len(info.audio_tracks) == 1
        a = info.audio_tracks[0]
        assert a.audio_index == 0
        assert a.channel_layout == "5.1"
        assert a.audio_sampling_frequency == 48000
        assert a.audio_bits_per_sample == 24
        assert a.language_ietf == "hi-IN"
        assert a.flag_original is True
        assert a.sync_delay_ms == 15

        assert len(info.subtitle_tracks) == 1
        s = info.subtitle_tracks[0]
        assert s.text_subtitles is True
        assert s.encoding == "UTF-8"
        assert s.flag_hearing_impaired is True
        assert s.frame_count == 842

        assert len(info.attachments) == 2
        assert info.attachments[0].file_name == "Roboto-Regular.ttf"
        assert info.attachments[0].size_bytes == 168260
        assert info.attachments[0].description == "Subtitle font"
        assert info.attachments[1].file_name == "cover.jpg"
        assert info.warnings == ["Container timestamp minor drift"]

    def test_malformed_or_empty_payload_degrades_safely(self) -> None:
        info = extract_media_file_info(
            _sample_row(size_bytes=0, duration_us=0, resolution=None, video_codec=None, video_dynamic_range=None),
            {
                "container": "invalid-not-a-dict",
                "tracks": ["bad", {"id": "not-an-int"}, {"id": 9, "type": "buttons", "codec": "Menu", "properties": "bad"}],
                "attachments": "bad",
                "chapters": None,
                "global_tags": "bad",
                "warnings": "not-a-list",
            },
        )
        assert info.container.container_type == "Unknown"
        assert info.container.overall_bitrate_bps is None
        assert info.container.attachments_count == 0
        assert info.video_tracks == []
        assert info.audio_tracks == []
        assert info.subtitle_tracks == []
        assert len(info.other_tracks) == 1
        assert info.other_tracks[0].id == 9
        assert info.attachments == []
        assert info.warnings == []
