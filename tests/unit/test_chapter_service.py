"""Exhaustive Phase C1 unit tests for chapter schemas, parsing, XML serialization, and extraction."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
import pytest

from app.config import AppConstants
from app.models.schemas import (
    ChapterEntry,
    ChaptersParseRequest,
    ChaptersParseResponse,
    ChaptersReadResponse,
    ContainerMetadataConfig,
)
from app.services.chapter_service import (
    ChapterError,
    extract_chapters_from_file,
    format_ms_to_matroska_time,
    format_ms_to_ogg_time,
    parse_chapters_text,
    parse_ffprobe_chapters_json,
    parse_matroska_chapters_xml,
    parse_timestamp_to_ms,
    serialize_chapters_to_matroska_xml,
)


class TestChapterSchemasAndBackwardCompatibility:
    """Verify Pydantic models and legacy `strip_chapters` compatibility."""

    def test_chapter_entry_normalization_and_validation(self) -> None:
        entry = ChapterEntry(start_ms=0, title="  Opening Credits  ")
        assert entry.start_ms == 0
        assert entry.title == "Opening Credits"

        none_title = ChapterEntry(start_ms=1000, title=None)  # type: ignore[arg-type]
        assert none_title.title == ""

        with pytest.raises(ValidationError):
            ChapterEntry(start_ms=-1, title="Negative")
        with pytest.raises(ValidationError):
            ChapterEntry(start_ms=0, title="x" * (AppConstants.CHAPTER_MAX_TITLE_LEN + 1))

    def test_container_metadata_config_chapter_actions_and_legacy_strip(self) -> None:
        # Default: keep untouched
        default_cfg = ContainerMetadataConfig()
        assert default_cfg.chapters_action == "keep"
        assert default_cfg.strip_chapters is False
        assert default_cfg.chapters is None

        # Legacy strip_chapters=True maps cleanly to chapters_action='clear'
        legacy_strip = ContainerMetadataConfig(strip_chapters=True)
        assert legacy_strip.chapters_action == "clear"
        assert legacy_strip.strip_chapters is True

        # Explicit chapters_action='clear' syncs strip_chapters=True
        explicit_clear = ContainerMetadataConfig(chapters_action="clear")
        assert explicit_clear.chapters_action == "clear"
        assert explicit_clear.strip_chapters is True

        # Explicit chapters_action='replace' with valid chapters
        replace_cfg = ContainerMetadataConfig(
            chapters_action="replace",
            chapters=[ChapterEntry(start_ms=0, title="Intro")],
        )
        assert replace_cfg.chapters_action == "replace"
        assert replace_cfg.strip_chapters is False
        assert replace_cfg.chapters is not None and len(replace_cfg.chapters) == 1

        # API response/request containers instantiate cleanly
        read_resp = ChaptersReadResponse(
            file_id=1, duration_us=60_000_000, chapters=replace_cfg.chapters, has_complex_chapters=True
        )
        assert read_resp.has_complex_chapters is True
        parse_req = ChaptersParseRequest(raw_text="00:00 Intro", filename="ch.txt")
        assert parse_req.filename == "ch.txt"
        parse_resp = ChaptersParseResponse(
            detected_format="plain_text", chapters=replace_cfg.chapters, warnings=[]
        )
        assert parse_resp.detected_format == "plain_text"

    def test_container_metadata_config_rejects_invalid_and_contradictory_states(self) -> None:
        # Contradictory replace + strip_chapters=True
        with pytest.raises(ValidationError, match="strip_chapters cannot be True"):
            ContainerMetadataConfig(
                strip_chapters=True,
                chapters_action="replace",
                chapters=[ChapterEntry(start_ms=0, title="Intro")],
            )
        # Replace without chapters
        with pytest.raises(ValidationError, match="at least one entry"):
            ContainerMetadataConfig(chapters_action="replace", chapters=[])
        # Replace with >500 chapters
        too_many = [
            ChapterEntry(start_ms=i * 1000, title=f"C{i}")
            for i in range(AppConstants.CHAPTER_MAX_COUNT + 1)
        ]
        with pytest.raises(ValidationError, match="At most"):
            ContainerMetadataConfig(chapters_action="replace", chapters=too_many)
        # Clear with non-empty chapters
        with pytest.raises(ValidationError, match="empty or omitted"):
            ContainerMetadataConfig(
                chapters_action="clear",
                chapters=[ChapterEntry(start_ms=0, title="Intro")],
            )
        # Keep with strip_chapters=True explicitly passed
        with pytest.raises(ValidationError, match="strip_chapters cannot be True"):
            ContainerMetadataConfig(strip_chapters=True, chapters_action="keep")
        # Keep with chapters list
        with pytest.raises(ValidationError, match="must be omitted"):
            ContainerMetadataConfig(
                chapters_action="keep",
                chapters=[ChapterEntry(start_ms=0, title="Intro")],
            )
        # Non-dict passed to model_validate
        with pytest.raises(ValidationError):
            ContainerMetadataConfig.model_validate("not-a-dict")


class TestTimestampParsingAndFormatting:
    """Verify forgiving timestamp parsing and Matroska/OGG formatters."""

    @pytest.mark.parametrize(
        ("raw", "expected_ms"),
        [
            ("00:00", 0),
            ("0:05", 5_000),
            ("4:30", 270_000),
            ("95:10", 5_710_000),
            ("1:05:12", 3_912_000),
            ("01:05:12.5", 3_912_500),
            ("01:05:12.25", 3_912_250),
            ("01:05:12.125", 3_912_125),
            ("00:00:01.123456789", 1_123),
            ("00:00:01.999900000", 2_000),
        ],
    )
    def test_parse_timestamp_to_ms_positive(self, raw: str, expected_ms: int) -> None:
        assert parse_timestamp_to_ms(raw) == expected_ms

    @pytest.mark.parametrize(
        "bad_raw",
        [
            "",
            "   ",
            "abc",
            "-00:05",
            "00:60",
            "01:60:00",
            "1:2:3:4",
            "00:05.1234567890",
        ],
    )
    def test_parse_timestamp_to_ms_rejects_invalid(self, bad_raw: str) -> None:
        with pytest.raises(ValueError):
            parse_timestamp_to_ms(bad_raw)

    def test_format_ms_helpers_and_negative_rejection(self) -> None:
        assert format_ms_to_matroska_time(0) == "00:00:00.000000000"
        assert format_ms_to_matroska_time(3_912_045) == "01:05:12.045000000"
        assert format_ms_to_ogg_time(3_912_045) == "01:05:12.045"

        with pytest.raises(ValueError):
            format_ms_to_matroska_time(-1)
        with pytest.raises(ValueError):
            format_ms_to_ogg_time(-5)


class TestMatroskaXmlParsingAndSerialization:
    """Verify XML round-trips, complex-chapter detection, and XXE/DTD defense."""

    def test_serialize_and_parse_round_trip_unicode_and_escaping(self) -> None:
        chapters = [
            ChapterEntry(start_ms=2_537_500, title='Act 2: "Fire & Ice" <Live>'),
            ChapterEntry(start_ms=0, title=""),
            ChapterEntry(start_ms=872_000, title="इंटरमिशन · 🎬"),
        ]
        xml_out = serialize_chapters_to_matroska_xml(chapters, language="hin")
        assert "<ChapterLanguage>hin</ChapterLanguage>" in xml_out
        assert "&amp;" in xml_out and "&lt;Live&gt;" in xml_out

        parsed, has_complex = parse_matroska_chapters_xml(xml_out)
        assert has_complex is False
        assert parsed == [
            ChapterEntry(start_ms=0, title="Chapter 01"),
            ChapterEntry(start_ms=872_000, title="इंटरमिशन · 🎬"),
            ChapterEntry(start_ms=2_537_500, title='Act 2: "Fire & Ice" <Live>'),
        ]

        # Invalid language code falls back to 'und'
        xml_fallback = serialize_chapters_to_matroska_xml(
            [ChapterEntry(start_ms=0, title="Start")], language="en-US"
        )
        assert f"<ChapterLanguage>{AppConstants.CHAPTER_DEFAULT_LANGUAGE}</ChapterLanguage>" in xml_fallback

    def test_serialize_validation_errors(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            serialize_chapters_to_matroska_xml([])
        too_many = [
            ChapterEntry(start_ms=i * 1000, title=f"Ch {i}")
            for i in range(AppConstants.CHAPTER_MAX_COUNT + 1)
        ]
        with pytest.raises(ValueError, match="At most"):
            serialize_chapters_to_matroska_xml(too_many)

    def test_parse_xml_handles_mkvtoolnix_doctype_namespaces_and_blank_xml(self) -> None:
        assert parse_matroska_chapters_xml("   \n ") == ([], False)
        assert parse_matroska_chapters_xml("<Chapters></Chapters>") == ([], False)

        # Benign MKVToolNix DOCTYPE + XML comment + namespace + invalid atom timestamp skipped
        xml_ns = """<?xml version="1.0"?>
        <!-- <!DOCTYPE Chapters SYSTEM "matroskachapters.dtd"> -->
        <!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">
        <Chapters xmlns="urn:matroska:chapters">
          <EditionEntry>
            <ChapterAtom>
              <ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
              <ChapterDisplay><ChapterString>  Intro  </ChapterString></ChapterDisplay>
            </ChapterAtom>
            <ChapterAtom>
              <ChapterTimeStart>not-a-time</ChapterTimeStart>
              <ChapterDisplay><ChapterString>Broken</ChapterString></ChapterDisplay>
            </ChapterAtom>
            <ChapterAtom>
              <ChapterTimeStart>00:05:00.000000000</ChapterTimeStart>
              <ChapterDisplay></ChapterDisplay>
            </ChapterAtom>
          </EditionEntry>
        </Chapters>
        """
        parsed, has_complex = parse_matroska_chapters_xml(xml_ns)
        assert has_complex is False
        assert parsed == [
            ChapterEntry(start_ms=0, title="Intro"),
            ChapterEntry(start_ms=300_000, title="Chapter 02"),
        ]

    def test_detects_complex_chapters_multi_edition_ordered_nested_and_segment_uid(self) -> None:
        # 1. Multiple editions
        multi_ed = """<Chapters>
          <EditionEntry><ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>
            <ChapterDisplay><ChapterString>Ed1</ChapterString></ChapterDisplay></ChapterAtom></EditionEntry>
          <EditionEntry><ChapterAtom><ChapterTimeStart>00:01:00.000</ChapterTimeStart>
            <ChapterDisplay><ChapterString>Ed2</ChapterString></ChapterDisplay></ChapterAtom></EditionEntry>
        </Chapters>"""
        parsed, has_complex = parse_matroska_chapters_xml(multi_ed)
        assert has_complex is True
        assert [c.title for c in parsed] == ["Ed1"]

        # 2. Ordered edition
        ordered_ed = """<Chapters><EditionEntry>
          <EditionFlagOrdered>1</EditionFlagOrdered>
          <ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>
            <ChapterDisplay><ChapterString>Ordered</ChapterString></ChapterDisplay></ChapterAtom>
        </EditionEntry></Chapters>"""
        _, has_complex = parse_matroska_chapters_xml(ordered_ed)
        assert has_complex is True

        # 3. Nested sub-chapter atom
        nested_ed = """<Chapters><EditionEntry>
          <ChapterAtom>
            <ChapterTimeStart>00:00:00.000</ChapterTimeStart>
            <ChapterDisplay><ChapterString>Part 1</ChapterString></ChapterDisplay>
            <ChapterAtom>
              <ChapterTimeStart>00:02:00.000</ChapterTimeStart>
              <ChapterDisplay><ChapterString>Sub 1A</ChapterString></ChapterDisplay>
            </ChapterAtom>
          </ChapterAtom>
        </EditionEntry></Chapters>"""
        parsed_nested, has_complex = parse_matroska_chapters_xml(nested_ed)
        assert has_complex is True
        assert [c.title for c in parsed_nested] == ["Part 1", "Sub 1A"]

        # 4. Segment-linked atom
        seg_ed = """<Chapters><EditionEntry>
          <ChapterAtom>
            <ChapterTimeStart>00:00:00.000</ChapterTimeStart>
            <ChapterSegmentUID>0x010203</ChapterSegmentUID>
            <ChapterDisplay><ChapterString>Linked</ChapterString></ChapterDisplay>
          </ChapterAtom>
        </EditionEntry></Chapters>"""
        _, has_complex = parse_matroska_chapters_xml(seg_ed)
        assert has_complex is True

    def test_xml_security_and_malformed_rejections(self) -> None:
        # XXE / entity expansion blocked
        xxe = '<!DOCTYPE Chapters [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Chapters>&xxe;</Chapters>'
        with pytest.raises(ChapterError, match="entity declarations"):
            parse_matroska_chapters_xml(xxe)

        # Arbitrary DOCTYPE blocked
        bad_doctype = '<!DOCTYPE Chapters SYSTEM "http://evil.invalid/x.dtd"><Chapters/>'
        with pytest.raises(ChapterError, match="DOCTYPE"):
            parse_matroska_chapters_xml(bad_doctype)

        # Oversized XML blocked
        huge = "<Chapters>" + (" " * (AppConstants.CHAPTER_PARSE_MAX_BYTES + 10)) + "</Chapters>"
        with pytest.raises(ChapterError, match="maximum allowed size"):
            parse_matroska_chapters_xml(huge)

        # Malformed XML
        with pytest.raises(ChapterError, match="Malformed"):
            parse_matroska_chapters_xml("<Chapters><EditionEntry></Chapters>")

        # Wrong root tag
        with pytest.raises(ChapterError, match="Expected <Chapters>"):
            parse_matroska_chapters_xml("<WrongRoot/>")


class TestFfprobeMp4AndUnifiedTextParsing:
    """Verify `.mp4` ffprobe JSON parsing and multi-format auto-detecting text parser."""

    def test_parse_ffprobe_chapters_json_positive_and_edge_cases(self) -> None:
        assert parse_ffprobe_chapters_json({}) == []
        assert parse_ffprobe_chapters_json({"chapters": "not-a-list"}) == []

        payload = {
            "chapters": [
                "bad-item",
                {"start_time": "120.5", "tags": {"TITLE": "Second Scene"}},
                {"start_time": "0.000000", "tags": {"title": "Opening"}},
                {"start": 300000, "time_base": "1/1000", "tags": {}},
                {"start_time": "bad", "start": "bad", "time_base": "1/1000"},
                {"start_time": "-5.0"},
                {"start": 100, "time_base": "1/0"},
            ]
        }
        parsed = parse_ffprobe_chapters_json(payload)
        assert parsed == [
            ChapterEntry(start_ms=0, title="Opening"),
            ChapterEntry(start_ms=120_500, title="Second Scene"),
            ChapterEntry(start_ms=300_000, title="Chapter 03"),
        ]

    def test_parse_chapters_text_auto_detects_xml_ogg_and_plain_youtube_csv(self) -> None:
        # 1. Matroska XML (complex warning surfaced)
        complex_xml = """<Chapters><EditionEntry><EditionFlagOrdered>1</EditionFlagOrdered>
          <ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>
            <ChapterDisplay><ChapterString>Start</ChapterString></ChapterDisplay></ChapterAtom>
        </EditionEntry></Chapters>"""
        fmt, chs, warns = parse_chapters_text(complex_xml, duration_ms=600_000)
        assert fmt == "matroska_xml"
        assert chs == [ChapterEntry(start_ms=0, title="Start")]
        assert any("flattened" in w for w in warns)

        # 2. OGG Simple Chapters (including internal blank lines and malformed timestamp ignored)
        ogg = (
            "CHAPTER01=00:00:00.000\n"
            "CHAPTER01NAME=Intro\n"
            "\n"
            "CHAPTER02=not-valid\n"
            "CHAPTER03=00:10:00.250\n"
            "CHAPTER03NAME=Finale\n"
        )
        fmt, chs, warns = parse_chapters_text(ogg, duration_ms=900_000)
        assert fmt == "ogg_text"
        assert chs == [
            ChapterEntry(start_ms=0, title="Intro"),
            ChapterEntry(start_ms=600_250, title="Finale"),
        ]
        assert warns == []

        # 3. Plain text / YouTube / CSV / internal blank lines / title mentioning '<Chapters>'
        plain = (
            "Here is my tracklist:\n"
            "00:00 Intro & <Chapters> overview\n"
            "\n"
            "1. 04:12 - Opening Credits\n"
            "- 00:04:12,\n"
            "00:12:30, The Heist\n"
            "Intermission (42:17)\n"
            "Bad Leading 00:99\n"
            "00:99 Bad Leading Timestamp\n"
            "03:00:00 Past End\n"
        )
        fmt, chs, warns = parse_chapters_text(plain, duration_ms=5_400_000)
        assert fmt == "plain_text"
        assert chs == [
            ChapterEntry(start_ms=0, title="Intro & <Chapters> overview"),
            ChapterEntry(start_ms=252_000, title="Opening Credits"),
            ChapterEntry(start_ms=750_000, title="The Heist"),
            ChapterEntry(start_ms=2_537_000, title="Intermission"),
        ]
        assert any("past movie duration" in w for w in warns)
        assert any("duplicate timestamp" in w for w in warns)
        assert any("non-timestamp" in w for w in warns)

    def test_parse_chapters_text_edge_cases_dedup_fill_truncation_and_limits(self) -> None:
        # Empty / whitespace / no timestamps
        fmt, chs, warns = parse_chapters_text("   ")
        assert fmt == "plain_text" and chs == [] and warns
        fmt, chs, warns = parse_chapters_text("just random text\nno times here")
        assert fmt == "plain_text" and chs == [] and any("No valid" in w for w in warns)

        # Duplicate where first has empty title and second has title -> keeps second's title
        fmt, chs, warns = parse_chapters_text("00:00\n00:00 Real Title\n00:00 Another")
        assert chs == [ChapterEntry(start_ms=0, title="Real Title")]
        assert any("2 duplicate timestamps" in w for w in warns)

        # >500 chapters truncated with warning
        many_lines = "\n".join(
            f"{i // 60:02d}:{i % 60:02d} Ch {i}" for i in range(AppConstants.CHAPTER_MAX_COUNT + 3)
        )
        fmt, chs, warns = parse_chapters_text(many_lines)
        assert len(chs) == AppConstants.CHAPTER_MAX_COUNT
        assert any("Truncated" in w for w in warns)

        # Non-string and oversized input rejected
        with pytest.raises(ChapterError, match="must be text"):
            parse_chapters_text(None)  # type: ignore[arg-type]
        with pytest.raises(ChapterError, match="maximum allowed size"):
            parse_chapters_text("0" * (AppConstants.CHAPTER_PARSE_MAX_BYTES + 1))


class TestExtractChaptersFromFile:
    """Verify async `mkvextract` (.mkv) and `ffprobe` (.mp4) extraction + error paths."""

    async def test_extract_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ChapterError, match="does not exist"):
            await extract_chapters_from_file(tmp_path / "missing.mkv")

    async def test_extract_mkv_success_empty_and_errors(self, tmp_path: Path) -> None:
        mkv = tmp_path / "movie.mkv"
        mkv.write_bytes(b"fake-mkv")
        xml_sample = serialize_chapters_to_matroska_xml(
            [ChapterEntry(start_ms=0, title="Start"), ChapterEntry(start_ms=60_000, title="Next")]
        )

        # 1. Success writing valid XML into temp file
        async def _ok_mkvextract(*argv, **_kwargs):
            Path(argv[-1]).write_text(xml_sample, encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_ok_mkvextract):
            chapters, is_complex = await extract_chapters_from_file(mkv)
        assert is_complex is False
        assert [c.title for c in chapters] == ["Start", "Next"]

        # 2. Empty XML output (file has 0 chapters)
        async def _empty_mkvextract(*argv, **_kwargs):
            Path(argv[-1]).write_text("", encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_empty_mkvextract):
            assert await extract_chapters_from_file(mkv) == ([], False)

        # 3. Output temp file removed / absent after exit 0
        async def _deleted_tmp_mkvextract(*argv, **_kwargs):
            Path(argv[-1]).unlink(missing_ok=True)
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_deleted_tmp_mkvextract):
            assert await extract_chapters_from_file(mkv) == ([], False)

        # 4. Non-zero exit code raises ChapterError with excerpt
        async def _fail_mkvextract(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=2,
                communicate=AsyncMock(return_value=(b"Error: bad mkv", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_fail_mkvextract),
            pytest.raises(ChapterError, match="exit code 2"),
        ):
            await extract_chapters_from_file(mkv)

        # 5. Missing binary & OSError
        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no bin")),
            pytest.raises(ChapterError, match="not found"),
        ):
            await extract_chapters_from_file(mkv)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=OSError("perm denied")),
            pytest.raises(ChapterError, match="Failed to launch"),
        ):
            await extract_chapters_from_file(mkv)

        # 6. Timeout kills subprocess (tolerating ProcessLookupError)
        def _kill_gone():
            raise ProcessLookupError()

        async def _hang():
            await asyncio.sleep(5)
            return b"", b""

        async def _slow_mkvextract(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=None,
                communicate=AsyncMock(side_effect=_hang),
                kill=_kill_gone,
                wait=AsyncMock(),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_slow_mkvextract),
            pytest.raises(ChapterError, match="timed out"),
        ):
            await extract_chapters_from_file(mkv, timeout_sec=0.05)

        # 7. Temp unlink OSError in finally is swallowed cleanly without masking success
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_ok_mkvextract),
            patch.object(Path, "unlink", side_effect=OSError("busy tmp")),
        ):
            chapters_after_unlink_err, _ = await extract_chapters_from_file(mkv)
        assert [c.title for c in chapters_after_unlink_err] == ["Start", "Next"]

    async def test_extract_mp4_via_ffprobe_success_and_errors(self, tmp_path: Path) -> None:
        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"fake-mp4")
        ffprobe_out = json.dumps(
            {
                "chapters": [
                    {"start_time": "0.0", "tags": {"title": "MP4 Intro"}},
                    {"start_time": "45.5", "tags": {"title": "MP4 Scene 2"}},
                ]
            }
        ).encode()

        async def _ok_ffprobe(*argv, **_kwargs):
            assert argv[0] == AppConstants.TOOL_FFPROBE
            assert "-show_chapters" in argv
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(ffprobe_out, b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_ok_ffprobe):
            chapters, is_complex = await extract_chapters_from_file(mp4)
        assert is_complex is False
        assert chapters == [
            ChapterEntry(start_ms=0, title="MP4 Intro"),
            ChapterEntry(start_ms=45_500, title="MP4 Scene 2"),
        ]

        # Non-dict JSON returns empty safely
        async def _list_json_ffprobe(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"[]", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_list_json_ffprobe):
            assert await extract_chapters_from_file(mp4) == ([], False)

        # Invalid JSON raises ChapterError
        async def _bad_json_ffprobe(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"{bad", b"")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_bad_json_ffprobe),
            pytest.raises(ChapterError, match="Invalid JSON"),
        ):
            await extract_chapters_from_file(mp4)

        # Non-zero exit raises ChapterError
        async def _err_ffprobe(*_argv, **_kwargs):
            return SimpleNamespace(
                returncode=1,
                communicate=AsyncMock(return_value=(b"", b"corrupt mp4")),
                kill=lambda: None,
                wait=AsyncMock(),
            )

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_err_ffprobe),
            pytest.raises(ChapterError, match="ffprobe failed"),
        ):
            await extract_chapters_from_file(mp4)

    async def test_real_mkvextract_and_ffprobe_round_trip(self, tmp_path: Path) -> None:
        if not all(
            shutil.which(tool)
            for tool in (
                AppConstants.TOOL_FFMPEG,
                AppConstants.TOOL_FFPROBE,
                AppConstants.TOOL_MKVMERGE,
                AppConstants.TOOL_MKVEXTRACT,
            )
        ):
            pytest.skip("ffmpeg/mkvtoolnix not installed locally")

        # Build a tiny 2-second MP4 with FFMETADATA chapters, then mux to MKV with our XML
        meta_path = tmp_path / "ffmeta.txt"
        meta_path.write_text(
            ";FFMETADATA1\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=MP4 Start\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=MP4 Second\n",
            encoding="utf-8",
        )
        mp4_path = tmp_path / "sample.mp4"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:d=2:r=10",
                "-i",
                str(meta_path),
                "-map_metadata",
                "1",
                "-c:v",
                "libx264",
                str(mp4_path),
            ],
            check=True,
        )
        mp4_chapters, mp4_complex = await extract_chapters_from_file(mp4_path)
        assert mp4_complex is False
        assert mp4_chapters == [
            ChapterEntry(start_ms=0, title="MP4 Start"),
            ChapterEntry(start_ms=1_000, title="MP4 Second"),
        ]

        # Now write our serialized Matroska XML into an MKV via mkvmerge and read back via mkvextract
        xml_path = tmp_path / "chapters.xml"
        expected = [
            ChapterEntry(start_ms=0, title="MKV Opening & Intro"),
            ChapterEntry(start_ms=1_250, title="MKV Finale"),
        ]
        xml_path.write_text(
            serialize_chapters_to_matroska_xml(expected, language="eng"),
            encoding="utf-8",
        )
        mkv_path = tmp_path / "sample.mkv"
        subprocess.run(
            [
                AppConstants.TOOL_MKVMERGE,
                "-o",
                str(mkv_path),
                "--no-chapters",
                "--chapters",
                str(xml_path),
                str(mp4_path),
            ],
            check=True,
            capture_output=True,
        )
        mkv_chapters, mkv_complex = await extract_chapters_from_file(mkv_path)
        assert mkv_complex is False
        assert mkv_chapters == expected
