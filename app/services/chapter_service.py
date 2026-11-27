"""Pure chapter parsing, Matroska XML serialization, and on-demand chapter extraction.

Single-responsibility domain service for:
- Forgiving timestamp parsing and Matroska/OGG time formatting.
- Safe Matroska XML parsing (with DTD/XXE guards and complex-chapter detection) and serialization.
- `ffprobe -show_chapters` JSON parsing for `.mp4` (non-Matroska) containers.
- Unified auto-detecting text parser (Matroska XML, OGG simple chapters, YouTube/plain text/CSV).
- On-demand async chapter extraction (`mkvextract` for `.mkv`, `ffprobe` for `.mp4`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal, Sequence
from xml.sax.saxutils import escape as xml_escape

from app.config import AppConstants
from app.models.schemas import ChapterEntry

DetectedChapterFormat = Literal["matroska_xml", "ogg_text", "plain_text"]

_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2})(?:\.(?P<frac>\d{1,9}))?$"
)
_INLINE_TIMESTAMP_PATTERN = r"(?:\d+:)?\d{1,2}:\d{2}(?:\.\d{1,9})?"
_LEADING_LINE_RE = re.compile(
    rf"^\s*(?:[-*•]\s*|\d+[.)]\s*)?(?P<ts>{_INLINE_TIMESTAMP_PATTERN})\s*(?:[-–—:|,]\s*|\s+)?(?P<title>.*)$"
)
_TRAILING_LINE_RE = re.compile(
    rf"^\s*(?:[-*•]\s*|\d+[.)]\s*)?(?P<title>.+?)\s*(?:[-–—:|,]\s*|\s+)?[\[(]?(?P<ts>{_INLINE_TIMESTAMP_PATTERN})[\])]?\s*$"
)
_OGG_TIME_RE = re.compile(r"^CHAPTER(?P<num>\d+)=(?P<ts>.+)$", re.IGNORECASE)
_OGG_NAME_RE = re.compile(r"^CHAPTER(?P<num>\d+)NAME=(?P<title>.*)$", re.IGNORECASE)

_BENIGN_MKV_DOCTYPE_RE = re.compile(
    r'<!DOCTYPE\s+Chapters\s+SYSTEM\s+["\']matroskachapters\.dtd["\']\s*>',
    re.IGNORECASE,
)
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_UNSAFE_XML_ENTITY_RE = re.compile(r"<!\s*ENTITY\b", re.IGNORECASE)
_UNSAFE_XML_DOCTYPE_RE = re.compile(r"<!\s*DOCTYPE\b", re.IGNORECASE)
_MATROSKA_LANG_RE = re.compile(r"^[a-z]{3}$")

_FORMAT_XML: DetectedChapterFormat = "matroska_xml"
_FORMAT_OGG: DetectedChapterFormat = "ogg_text"
_FORMAT_PLAIN: DetectedChapterFormat = "plain_text"
_FRAC_NANO_DIGITS: int = 9
_MAX_MINUTES_WITH_HOURS: int = 59
_MAX_SECONDS: int = 59


class ChapterError(RuntimeError):
    """Raised when chapter parsing, validation, or extraction fails."""


def _strip_xml_namespace(tag: str) -> str:
    """Return local element name stripped of any `{namespace}` prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _truncate_title(raw_title: str) -> str:
    """Trim whitespace and clamp title length to `AppConstants.CHAPTER_MAX_TITLE_LEN`."""
    cleaned = " ".join(str(raw_title or "").strip().splitlines()).strip()
    return cleaned[: AppConstants.CHAPTER_MAX_TITLE_LEN]


def _default_chapter_title(one_based_index: int) -> str:
    """Return canonical fallback title (`Chapter 01`, `Chapter 02`, ...)."""
    return f"Chapter {one_based_index:02d}"


def parse_timestamp_to_ms(raw: str) -> int:
    """Parse a human, OGG, or Matroska timestamp string into non-negative integer milliseconds.

    Accepted formats:
    - `MM:SS` (e.g. `4:30`, `04:30`, `95:12`)
    - `H:MM:SS` / `HH:MM:SS` (e.g. `1:05:12`, `01:05:12`)
    - Optional 1..9 fractional second digits (`.5`, `.250`, `.123456789`)
    """
    cleaned = str(raw or "").strip()
    if not cleaned:
        raise ValueError("Timestamp cannot be empty")

    match = _TIMESTAMP_RE.match(cleaned)
    if match is None:
        raise ValueError(f"Invalid chapter timestamp: {cleaned!r}")

    hours_raw = match.group("hours")
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    frac_raw = match.group("frac")

    if seconds > _MAX_SECONDS:
        raise ValueError(f"Seconds out of range (0-59) in timestamp: {cleaned!r}")
    if hours_raw is not None and minutes > _MAX_MINUTES_WITH_HOURS:
        raise ValueError(f"Minutes out of range (0-59) in timestamp: {cleaned!r}")

    hours = int(hours_raw) if hours_raw is not None else 0
    total_sec = (
        hours * AppConstants.SEC_PER_HOUR
        + minutes * AppConstants.SEC_PER_MIN
        + seconds
    )
    frac_ms = 0
    if frac_raw is not None:
        nanos = int(frac_raw.ljust(_FRAC_NANO_DIGITS, "0")[:_FRAC_NANO_DIGITS])
        frac_ms = round(nanos / AppConstants.NS_PER_MS)

    return total_sec * AppConstants.MS_PER_SEC + frac_ms


def format_ms_to_matroska_time(start_ms: int) -> str:
    """Format non-negative milliseconds as Matroska `HH:MM:SS.mmm000000`."""
    if start_ms < 0:
        raise ValueError(f"Chapter timestamp must be non-negative (got {start_ms})")
    total_sec, rem_ms = divmod(int(start_ms), AppConstants.MS_PER_SEC)
    hours, rem_sec = divmod(total_sec, AppConstants.SEC_PER_HOUR)
    minutes, seconds = divmod(rem_sec, AppConstants.SEC_PER_MIN)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{rem_ms:03d}000000"


def format_ms_to_ogg_time(start_ms: int) -> str:
    """Format non-negative milliseconds as OGG Simple Chapters `HH:MM:SS.mmm`."""
    if start_ms < 0:
        raise ValueError(f"Chapter timestamp must be non-negative (got {start_ms})")
    total_sec, rem_ms = divmod(int(start_ms), AppConstants.MS_PER_SEC)
    hours, rem_sec = divmod(total_sec, AppConstants.SEC_PER_HOUR)
    minutes, seconds = divmod(rem_sec, AppConstants.SEC_PER_MIN)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{rem_ms:03d}"


def _sanitize_matroska_xml_input(xml_text: str) -> str:
    """Reject DTD/entity injection while tolerating standard MKVToolNix DOCTYPE/comments."""
    if len(xml_text.encode(AppConstants.UTF8_ENCODING, errors="replace")) > AppConstants.CHAPTER_PARSE_MAX_BYTES:
        raise ChapterError(
            f"Chapter XML exceeds maximum allowed size ({AppConstants.CHAPTER_PARSE_MAX_BYTES} bytes)"
        )
    without_comments = _XML_COMMENT_RE.sub("", xml_text)
    if _UNSAFE_XML_ENTITY_RE.search(without_comments):
        raise ChapterError("XML entity declarations are not permitted in chapter files")
    stripped_benign = _BENIGN_MKV_DOCTYPE_RE.sub("", without_comments)
    if _UNSAFE_XML_DOCTYPE_RE.search(stripped_benign):
        raise ChapterError("Unsupported XML DOCTYPE declaration in chapter file")
    return stripped_benign.strip()


def _extract_atom_title(atom: ET.Element) -> str:
    """Return the first non-empty `<ChapterDisplay>/<ChapterString>` text on a `<ChapterAtom>`."""
    for child in atom:
        if _strip_xml_namespace(child.tag) != "ChapterDisplay":
            continue
        for display_child in child:
            if _strip_xml_namespace(display_child.tag) == "ChapterString" and display_child.text:
                title = _truncate_title(display_child.text)
                if title:
                    return title
    return ""


def _collect_chapter_atoms(
    parent: ET.Element,
    *,
    is_nested: bool = False,
) -> tuple[list[ChapterEntry], bool]:
    """Extract `ChapterEntry` items from `<ChapterAtom>` children and flag nested/linked atoms."""
    entries: list[ChapterEntry] = []
    has_complex = False

    for child in parent:
        if _strip_xml_namespace(child.tag) != "ChapterAtom":
            continue
        if is_nested:
            has_complex = True

        start_text: str | None = None
        nested_children = False
        for prop in child:
            tag = _strip_xml_namespace(prop.tag)
            if tag == "ChapterTimeStart" and prop.text:
                start_text = prop.text.strip()
            elif tag == "ChapterSegmentUID" and (prop.text or "").strip():
                has_complex = True
            elif tag == "ChapterAtom":
                nested_children = True

        if start_text is not None:
            try:
                start_ms = parse_timestamp_to_ms(start_text)
            except ValueError:
                start_ms = None
            if start_ms is not None:
                entries.append(
                    ChapterEntry(start_ms=start_ms, title=_extract_atom_title(child))
                )

        if nested_children:
            nested_entries, nested_complex = _collect_chapter_atoms(
                child, is_nested=True
            )
            entries.extend(nested_entries)
            has_complex = has_complex or nested_complex

    return entries, has_complex


def parse_matroska_chapters_xml(xml_text: str) -> tuple[list[ChapterEntry], bool]:
    """Parse Matroska chapters XML into `(sorted_chapters, has_complex_chapters)`.

    `has_complex_chapters` is `True` when the file contains multiple `<EditionEntry>`
    blocks, ordered chapters (`<EditionFlagOrdered>1</EditionFlagOrdered>`), nested
    `<ChapterAtom>` sub-chapters, or segment-linked chapters (`<ChapterSegmentUID>`).
    """
    cleaned = _sanitize_matroska_xml_input(xml_text)
    if not cleaned:
        return [], False

    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise ChapterError(f"Malformed Matroska chapters XML: {exc}") from exc

    if _strip_xml_namespace(root.tag) != "Chapters":
        raise ChapterError(
            f"Expected <Chapters> root element in Matroska XML (got <{root.tag}>)"
        )

    editions = [
        child for child in root if _strip_xml_namespace(child.tag) == "EditionEntry"
    ]
    if not editions:
        return [], False

    has_complex = len(editions) > 1
    for edition in editions:
        for prop in edition:
            if (
                _strip_xml_namespace(prop.tag) == "EditionFlagOrdered"
                and (prop.text or "").strip() == "1"
            ):
                has_complex = True

    # Read chapters from the primary (first) edition so multi-edition files don't duplicate
    primary_entries, atoms_complex = _collect_chapter_atoms(editions[0])
    has_complex = has_complex or atoms_complex

    sorted_entries = sorted(primary_entries, key=lambda item: item.start_ms)
    normalized: list[ChapterEntry] = []
    for idx, entry in enumerate(sorted_entries, start=1):
        title = entry.title or _default_chapter_title(idx)
        normalized.append(ChapterEntry(start_ms=entry.start_ms, title=title))
    return normalized, has_complex


def serialize_chapters_to_matroska_xml(
    chapters: Sequence[ChapterEntry],
    *,
    language: str = AppConstants.CHAPTER_DEFAULT_LANGUAGE,
) -> str:
    """Serialize flat `ChapterEntry` items into valid MKVToolNix Matroska chapters XML."""
    if not chapters:
        raise ValueError("Cannot serialize an empty chapter list to Matroska XML")
    if len(chapters) > AppConstants.CHAPTER_MAX_COUNT:
        raise ValueError(
            f"At most {AppConstants.CHAPTER_MAX_COUNT} chapters are supported "
            f"(got {len(chapters)})"
        )

    lang_cleaned = str(language or "").strip().lower()
    lang_code = (
        lang_cleaned
        if _MATROSKA_LANG_RE.match(lang_cleaned)
        else AppConstants.CHAPTER_DEFAULT_LANGUAGE
    )

    sorted_chapters = sorted(chapters, key=lambda item: item.start_ms)
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Chapters>",
        "  <EditionEntry>",
        f"    <EditionUID>{AppConstants.CHAPTER_EDITION_UID}</EditionUID>",
        "    <EditionFlagHidden>0</EditionFlagHidden>",
        "    <EditionFlagDefault>1</EditionFlagDefault>",
    ]

    for idx, entry in enumerate(sorted_chapters, start=1):
        atom_uid = AppConstants.CHAPTER_ATOM_UID_BASE + idx - 1
        title = _truncate_title(entry.title) or _default_chapter_title(idx)
        escaped_title = xml_escape(title)
        start_str = format_ms_to_matroska_time(entry.start_ms)
        lines.extend(
            [
                "    <ChapterAtom>",
                f"      <ChapterUID>{atom_uid}</ChapterUID>",
                f"      <ChapterTimeStart>{start_str}</ChapterTimeStart>",
                "      <ChapterFlagHidden>0</ChapterFlagHidden>",
                "      <ChapterFlagEnabled>1</ChapterFlagEnabled>",
                "      <ChapterDisplay>",
                f"        <ChapterString>{escaped_title}</ChapterString>",
                f"        <ChapterLanguage>{lang_code}</ChapterLanguage>",
                "      </ChapterDisplay>",
                "    </ChapterAtom>",
            ]
        )

    lines.extend(["  </EditionEntry>", "</Chapters>", ""])
    return "\n".join(lines)


def _parse_ffprobe_chapter_start_ms(raw_chapter: dict[str, Any]) -> int | None:
    """Extract non-negative millisecond start time from one `ffprobe -show_chapters` entry."""
    start_time_raw = raw_chapter.get("start_time")
    if start_time_raw is not None:
        try:
            start_sec = float(start_time_raw)
        except (TypeError, ValueError):
            start_sec = -1.0
        if start_sec >= 0.0:
            return round(start_sec * AppConstants.MS_PER_SEC)

    start_val = raw_chapter.get("start")
    time_base = str(raw_chapter.get("time_base") or "").strip()
    if start_val is not None and "/" in time_base:
        num_str, den_str = time_base.split("/", 1)
        try:
            num = float(num_str)
            den = float(den_str)
            start_num = float(start_val)
        except (TypeError, ValueError):
            return None
        if den > 0.0 and start_num >= 0.0 and num >= 0.0:
            return round((start_num * num / den) * AppConstants.MS_PER_SEC)
    return None


def parse_ffprobe_chapters_json(payload: dict[str, Any]) -> list[ChapterEntry]:
    """Parse `ffprobe -print_format json -show_chapters` payload (used for `.mp4` inputs)."""
    raw_chapters = payload.get("chapters")
    if not isinstance(raw_chapters, list):
        return []

    extracted: list[ChapterEntry] = []
    for item in raw_chapters:
        if not isinstance(item, dict):
            continue
        start_ms = _parse_ffprobe_chapter_start_ms(item)
        if start_ms is None:
            continue
        title = ""
        tags = item.get("tags")
        if isinstance(tags, dict):
            for k, v in tags.items():
                if str(k).strip().lower() == "title" and v is not None:
                    title = _truncate_title(str(v))
                    break
        extracted.append(ChapterEntry(start_ms=start_ms, title=title))

    sorted_entries = sorted(extracted, key=lambda entry: entry.start_ms)
    return [
        ChapterEntry(
            start_ms=entry.start_ms,
            title=entry.title or _default_chapter_title(idx),
        )
        for idx, entry in enumerate(sorted_entries, start=1)
    ]


def _parse_ogg_chapters_text(raw_text: str) -> list[ChapterEntry]:
    """Parse OGG Simple Chapters (`CHAPTER01=HH:MM:SS.mmm` / `CHAPTER01NAME=...`)."""
    times_by_num: dict[str, int] = {}
    names_by_num: dict[str, str] = {}

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name_match = _OGG_NAME_RE.match(stripped)
        if name_match is not None:
            names_by_num[name_match.group("num")] = _truncate_title(
                name_match.group("title")
            )
            continue
        time_match = _OGG_TIME_RE.match(stripped)
        if time_match is not None:
            try:
                times_by_num[time_match.group("num")] = parse_timestamp_to_ms(
                    time_match.group("ts")
                )
            except ValueError:
                continue

    entries: list[ChapterEntry] = []
    for num, start_ms in times_by_num.items():
        entries.append(
            ChapterEntry(start_ms=start_ms, title=names_by_num.get(num, ""))
        )
    return entries


def _parse_plain_chapters_text(raw_text: str) -> tuple[list[ChapterEntry], int]:
    """Parse YouTube / plain-text / CSV chapter lines into `(entries, skipped_lines)`."""
    entries: list[ChapterEntry] = []
    skipped = 0

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lead = _LEADING_LINE_RE.match(line)
        if lead is not None:
            try:
                start_ms = parse_timestamp_to_ms(lead.group("ts"))
            except ValueError:
                skipped += 1
                continue
            title = _truncate_title(lead.group("title").strip(" -–—:|,\t"))
            entries.append(ChapterEntry(start_ms=start_ms, title=title))
            continue

        trail = _TRAILING_LINE_RE.match(line)
        if trail is not None:
            try:
                start_ms = parse_timestamp_to_ms(trail.group("ts"))
            except ValueError:
                skipped += 1
                continue
            title = _truncate_title(trail.group("title").strip(" -–—:|,\t"))
            entries.append(ChapterEntry(start_ms=start_ms, title=title))
            continue

        skipped += 1

    return entries, skipped


def _finalize_parsed_chapters(
    raw_entries: Sequence[ChapterEntry],
    *,
    duration_ms: int | None,
) -> tuple[list[ChapterEntry], list[str]]:
    """Sort, filter out-of-range timestamps, deduplicate, cap count, and fill blank titles."""
    warnings: list[str] = []
    in_range: list[ChapterEntry] = []
    past_eof_count = 0

    for entry in raw_entries:
        if duration_ms is not None and duration_ms > 0 and entry.start_ms > duration_ms:
            past_eof_count += 1
            continue
        in_range.append(entry)

    if past_eof_count > 0:
        noun = "chapter" if past_eof_count == 1 else "chapters"
        warnings.append(
            f"Skipped {past_eof_count} {noun} past movie duration ({format_ms_to_ogg_time(duration_ms or 0)})"
        )

    sorted_entries = sorted(in_range, key=lambda item: item.start_ms)

    deduped: list[ChapterEntry] = []
    dup_count = 0
    for entry in sorted_entries:
        if deduped and deduped[-1].start_ms == entry.start_ms:
            dup_count += 1
            if not deduped[-1].title and entry.title:
                deduped[-1] = entry
            continue
        deduped.append(entry)

    if dup_count > 0:
        noun = "duplicate timestamp" if dup_count == 1 else "duplicate timestamps"
        warnings.append(f"Merged {dup_count} {noun}")

    if len(deduped) > AppConstants.CHAPTER_MAX_COUNT:
        dropped = len(deduped) - AppConstants.CHAPTER_MAX_COUNT
        deduped = deduped[: AppConstants.CHAPTER_MAX_COUNT]
        warnings.append(
            f"Truncated to maximum of {AppConstants.CHAPTER_MAX_COUNT} chapters ({dropped} omitted)"
        )

    finalized = [
        ChapterEntry(
            start_ms=entry.start_ms,
            title=entry.title or _default_chapter_title(idx),
        )
        for idx, entry in enumerate(deduped, start=1)
    ]
    return finalized, warnings


def parse_chapters_text(
    raw_text: str,
    *,
    duration_ms: int | None = None,
) -> tuple[DetectedChapterFormat, list[ChapterEntry], list[str]]:
    """Auto-detect Matroska XML, OGG simple text, or YouTube/plain-text/CSV chapters.

    Returns `(detected_format, finalized_chapters, warnings)`.
    """
    if not isinstance(raw_text, str):
        raise ChapterError("Chapter input must be text")
    if len(raw_text.encode(AppConstants.UTF8_ENCODING, errors="replace")) > AppConstants.CHAPTER_PARSE_MAX_BYTES:
        raise ChapterError(
            f"Chapter text exceeds maximum allowed size ({AppConstants.CHAPTER_PARSE_MAX_BYTES} bytes)"
        )

    stripped = raw_text.strip()
    if not stripped:
        return _FORMAT_PLAIN, [], ["No chapters found in input"]

    lower_sample = stripped.lower()
    if stripped.startswith("<") and (
        "<chapters" in lower_sample or "<chapteratom" in lower_sample
    ):
        xml_entries, has_complex = parse_matroska_chapters_xml(stripped)
        finalized, warnings = _finalize_parsed_chapters(
            xml_entries, duration_ms=duration_ms
        )
        if has_complex:
            warnings.insert(
                0,
                "Source XML contains multiple editions, nested sub-chapters, or ordered chapters and was flattened",
            )
        return _FORMAT_XML, finalized, warnings

    if any(_OGG_TIME_RE.match(line.strip()) for line in stripped.splitlines()):
        ogg_entries = _parse_ogg_chapters_text(stripped)
        finalized, warnings = _finalize_parsed_chapters(
            ogg_entries, duration_ms=duration_ms
        )
        return _FORMAT_OGG, finalized, warnings

    plain_entries, skipped_lines = _parse_plain_chapters_text(stripped)
    finalized, warnings = _finalize_parsed_chapters(
        plain_entries, duration_ms=duration_ms
    )
    if skipped_lines > 0 and finalized:
        noun = "line" if skipped_lines == 1 else "lines"
        warnings.append(f"Ignored {skipped_lines} non-timestamp {noun}")
    elif not finalized:
        warnings.append("No valid chapter timestamps detected")
    return _FORMAT_PLAIN, finalized, warnings


async def _run_chapter_subprocess(
    argv: list[str],
    *,
    timeout_sec: float,
    tool_label: str,
) -> tuple[int, bytes, bytes]:
    """Execute a short-lived chapter reader subprocess with strict timeout cleanup."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ChapterError(f"{tool_label} executable not found in PATH") from exc
    except OSError as exc:
        raise ChapterError(f"Failed to launch {tool_label}: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise ChapterError(
            f"{tool_label} chapter extraction timed out after {timeout_sec}s"
        ) from exc

    return int(proc.returncode or 0), stdout_bytes, stderr_bytes


async def extract_chapters_from_file(
    abs_path: Path,
    *,
    timeout_sec: float = AppConstants.CHAPTER_EXTRACT_TIMEOUT_SEC,
) -> tuple[list[ChapterEntry], bool]:
    """Read existing chapters from an `.mkv` (`mkvextract`) or `.mp4` (`ffprobe`) file.

    Returns `(chapters, has_complex_chapters)`.
    """
    if not await asyncio.to_thread(abs_path.is_file):
        raise ChapterError(f"Media file does not exist: {abs_path.name}")

    if abs_path.suffix.lower() == AppConstants.EXT_MKV:
        fd, tmp_str = tempfile.mkstemp(
            prefix=".mkvforge_chapters_",
            suffix=".xml",
        )
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            returncode, stdout_bytes, stderr_bytes = await _run_chapter_subprocess(
                [
                    AppConstants.TOOL_MKVEXTRACT,
                    str(abs_path),
                    "chapters",
                    str(tmp_path),
                ],
                timeout_sec=timeout_sec,
                tool_label=AppConstants.TOOL_MKVEXTRACT,
            )
            if returncode != AppConstants.EXIT_CODE_SUCCESS:
                err_text = (
                    stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
                    or stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()
                )[: AppConstants.MAX_ERROR_EXCERPT_LEN]
                raise ChapterError(
                    f"mkvextract failed with exit code {returncode}: {err_text}"
                )

            if not await asyncio.to_thread(tmp_path.is_file):
                return [], False
            xml_text = await asyncio.to_thread(
                tmp_path.read_text, encoding=AppConstants.UTF8_ENCODING, errors="replace"
            )
            if not xml_text.strip():
                return [], False
            return parse_matroska_chapters_xml(xml_text)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # Non-Matroska containers (e.g. .mp4): read chapters via ffprobe JSON
    returncode, stdout_bytes, stderr_bytes = await _run_chapter_subprocess(
        [
            AppConstants.TOOL_FFPROBE,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_chapters",
            str(abs_path),
        ],
        timeout_sec=timeout_sec,
        tool_label=AppConstants.TOOL_FFPROBE,
    )
    if returncode != AppConstants.EXIT_CODE_SUCCESS:
        err_text = stderr_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace").strip()[
            : AppConstants.MAX_ERROR_EXCERPT_LEN
        ]
        raise ChapterError(f"ffprobe failed with exit code {returncode}: {err_text}")

    try:
        parsed_json = json.loads(
            stdout_bytes.decode(AppConstants.UTF8_ENCODING, errors="replace") or "{}"
        )
    except json.JSONDecodeError as exc:
        raise ChapterError(f"Invalid JSON returned by ffprobe: {exc}") from exc

    if not isinstance(parsed_json, dict):
        return [], False
    return parse_ffprobe_chapters_json(parsed_json), False
