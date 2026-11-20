"""Unit tests for app.services.probe_parser against real-world mkvmerge -J shapes."""

from __future__ import annotations

from app.services.probe_parser import parse_mkvmerge_json


class TestParseMkvmergeJson:
    """Verify ns->us duration conversion, badges, tags, audio indexing, and sync delay."""

    def test_aac_51_cinemascope_with_global_tags_attachments_and_chapters(self) -> None:
        payload = {
            "container": {
                "type": "Matroska",
                "properties": {
                    # 2 hours in nanoseconds -> 7,200,000,000 us
                    "duration": 7_200_000_000_000,
                    "title": "  Release.Group.Junk.Title  ",
                },
            },
            "global_tags": [{"num_entries": 3}],
            "track_tags": [{"num_entries": 5, "track_id": 0}],
            "attachments": [{"id": 1, "file_name": "cover.jpg"}, {"id": 2, "file_name": "font.ttf"}],
            "chapters": [{"num_entries": 12}],
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "MPEG-H/HEVC/H.265",
                    "properties": {
                        "codec_id": "V_MPEGH/ISO/HEVC",
                        "pixel_dimensions": "1920x800",
                        "language": "und",
                        "default_track": True,
                    },
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "AAC",
                    "properties": {
                        "codec_id": "A_AAC",
                        "audio_channels": 6,
                        "audio_sampling_frequency": 48000,
                        "language": "eng",
                        "language_ietf": "en",
                        "track_name": "English AAC 5.1",
                        "default_track": True,
                        "minimum_timestamp": 0,
                    },
                },
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {"language": "eng", "forced_track": False},
                },
                {
                    "id": 3,
                    "type": "subtitles",
                    "codec": "HDMV PGS",
                    "properties": {"language": "eng", "forced_track": True},
                },
            ],
        }

        probe = parse_mkvmerge_json(payload)

        assert probe.duration_us == 7_200_000_000
        assert probe.container_title == "Release.Group.Junk.Title"
        assert probe.has_global_tags is True
        assert probe.has_track_tags is True
        assert probe.global_tags_count == 3
        assert probe.track_tags_count == 5
        assert probe.attachments_count == 2
        assert [a.file_name for a in probe.attachments] == ["cover.jpg", "font.ttf"]
        assert probe.chapters_count == 12
        assert probe.has_aac_51 is True
        assert probe.has_ac3_eac3 is False
        assert probe.resolution == "1080p"  # 1920x800 CinemaScope is 1080p
        assert probe.video_codec == "HEVC"
        assert probe.subtitle_count == 2
        assert len(probe.audio_summary) == 1
        assert probe.audio_summary[0].codec == "AAC"
        assert probe.audio_summary[0].channel_layout == "5.1"
        assert probe.tracks[1].audio_index == 0
        assert probe.tracks[1].sync_delay_ms == 0

    def test_multi_audio_relative_index_and_minimum_timestamp_delay(self) -> None:
        payload = {
            "container": {"properties": {"duration": 3_600_000_000_000}},
            "global_tags": [],
            "track_tags": [{"num_entries": 4, "track_id": 0}],
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "AVC/H.264/MPEG-4p10",
                    "properties": {"pixel_dimensions": "3840x1600"},
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "AAC",
                    "properties": {
                        "audio_channels": 6,
                        "language": "hin",
                        "minimum_timestamp": 0,
                    },
                },
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {"language": "eng"},
                },
                {
                    "id": 3,
                    "type": "audio",
                    "codec": "E-AC-3",
                    "properties": {
                        "codec_id": "A_EAC3",
                        "audio_channels": 6,
                        "language": "eng",
                        "default_track": True,
                        # +42ms delay in nanoseconds
                        "minimum_timestamp": 42_000_000,
                    },
                },
            ],
        }

        probe = parse_mkvmerge_json(payload)

        # track_tags must NOT flip has_global_tags
        assert probe.has_global_tags is False
        assert probe.has_track_tags is True
        assert probe.container_title == ""
        assert probe.resolution == "2160p"
        assert probe.video_codec == "H.264"
        assert probe.has_aac_51 is True
        assert probe.has_ac3_eac3 is True

        audio_tracks = [t for t in probe.tracks if t.type == "audio"]
        assert [(t.id, t.audio_index, t.sync_delay_ms) for t in audio_tracks] == [
            (1, 0, 0),
            (3, 1, 42),
        ]

    def test_mp4_probe_with_missing_duration_and_ac3_stereo(self) -> None:
        payload = {
            "container": {"type": "QuickTime/MP4", "properties": {}},
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "AV1",
                    "properties": {"pixel_dimensions": "1280x720"},
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "AC-3",
                    "properties": {"codec_id": "A_AC3", "audio_channels": 2, "language": ""},
                },
            ],
        }

        probe = parse_mkvmerge_json(payload)

        assert probe.duration_us == 0
        assert probe.resolution == "720p"
        assert probe.video_codec == "AV1"
        assert probe.has_aac_51 is False
        assert probe.has_ac3_eac3 is True
        assert probe.audio_summary[0].codec == "AC3"
        assert probe.audio_summary[0].channel_layout == "2.0"
        assert probe.audio_summary[0].language == "und"

    def test_sd_resolution_and_malformed_payload_degrade_safely(self) -> None:
        sd_probe = parse_mkvmerge_json(
            {
                "container": {"properties": {"duration": -500}},
                "tracks": [
                    {
                        "id": 0,
                        "type": "video",
                        "codec": "MPEG-2",
                        "properties": {"pixel_dimensions": "720x480"},
                    },
                    "not-a-dict",
                    {"type": "audio"},  # missing id
                ],
            }
        )
        assert sd_probe.duration_us == 0
        assert sd_probe.resolution == "SD"
        assert sd_probe.video_codec == "MPEG-2"
        assert len(sd_probe.tracks) == 1

        empty_probe = parse_mkvmerge_json({})
        assert empty_probe.duration_us == 0
        assert empty_probe.tracks == []
        assert empty_probe.has_global_tags is False
        assert empty_probe.has_aac_51 is False
        assert empty_probe.has_ac3_eac3 is False

    def test_extracts_attachments_with_uid_and_tag_entries_excluding_statistics(
        self,
    ) -> None:
        payload = {
            "container": {"type": "Matroska", "properties": {"tag_comment": "Promo Comment"}},
            "container_tags": {
                "TITLE": "Downloaded From ExampleSite.org",
                "ENCODER": "libebml v1.4",
            },
            "global_tags": [{"num_entries": 1}],
            "track_tags": [{"num_entries": 4, "track_id": 0}],
            "attachments": [
                {
                    "id": 1,
                    "file_name": "Logo-Attachment.png",
                    "content_type": "image/png",
                    "size": 25923,
                    "description": "Promo Logo",
                    "properties": {"uid": 1645021483571234},
                }
            ],
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC",
                    "properties": {
                        "pixel_dimensions": "1920x1080",
                        "tag_bps": "5000000",
                        "tag_duration": "01:00:00.000000000",
                        "tag_number_of_frames": "86400",
                        "tag_title": "Track Spam Title",
                    },
                }
            ],
        }
        probe = parse_mkvmerge_json(payload)
        assert len(probe.attachments) == 1
        att = probe.attachments[0]
        assert att.id == 1
        assert att.file_name == "Logo-Attachment.png"
        assert att.content_type == "image/png"
        assert att.size_bytes == 25923
        assert att.uid == "1645021483571234"

        tags = [(t.scope, t.key, t.value) for t in probe.tag_entries]
        assert ("Global", "TITLE", "Downloaded From ExampleSite.org") in tags
        assert ("Global", "COMMENT", "Promo Comment") in tags
        assert ("Track #0", "TITLE", "Track Spam Title") in tags
        # Statistics tags and format encoder are filtered out
        assert all(t.key not in ("BPS", "DURATION", "NUMBER_OF_FRAMES", "ENCODER") for t in probe.tag_entries)
