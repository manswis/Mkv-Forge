"""Synthetic real-media fixture generators for Phase 7 E2E verification.

Generates tiny real `.mkv` and `.mp4` test clips via `ffmpeg` and `mkvmerge`
so every audio layout, metadata tag, sync delay, attachment, and cancellation
scenario can be verified deterministically without committing binary blobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import struct
import zlib
from pathlib import Path

from app.config import AppConstants
from app.utils.paths import is_mkvforge_temp_file


class MediaFixtureConstants:
    """Centralized parameters for synthetic Phase 7 media generation."""

    SHORT_DURATION_SEC: int = 2
    LONG_DURATION_SEC: int = 90
    VIDEO_SIZE: str = "160x120"
    VIDEO_FPS: str = "10"
    SAMPLE_RATE: int = 48_000
    SYNC_DELAY_MS: int = 120
    SYNC_DELAY_TOLERANCE_MS: int = 25
    JUNK_CONTAINER_TITLE: str = "Release.Group.Junk.Title.1080p"
    JUNK_TAG_NAME: str = "RELEASE_GROUP"
    JUNK_TAG_VALUE: str = "TorrentSpam"
    SUBTITLE_SRT_BODY: str = (
        "1\n00:00:00,000 --> 00:00:01,500\nHello MKV Forge\n"
    )
    COVER_MIME_TYPE: str = "image/png"
    COVER_FILENAME: str = "cover.png"


def has_ffmpeg_tools() -> bool:
    """Return True if both `ffmpeg` and `ffprobe` are available in PATH."""
    return (
        shutil.which(AppConstants.TOOL_FFMPEG) is not None
        and shutil.which(AppConstants.TOOL_FFPROBE) is not None
    )


def has_full_media_tools() -> bool:
    """Return True if `ffmpeg`, `ffprobe`, and `mkvmerge` are all available."""
    return has_ffmpeg_tools() and shutil.which(AppConstants.TOOL_MKVMERGE) is not None


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest for a file on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_temp_artifacts(directory: Path) -> list[Path]:
    """Return any lingering `*.mkvforge.*` temp files beneath `directory`."""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and is_mkvforge_temp_file(path)
    )


def write_minimal_png(path: Path) -> Path:
    """Write a valid 1x1 RGB PNG image without external image libraries."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", crc)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x63\x66\xf1")
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png_bytes)
    return path


def write_global_tags_xml(
    path: Path,
    *,
    tag_name: str = MediaFixtureConstants.JUNK_TAG_NAME,
    tag_value: str = MediaFixtureConstants.JUNK_TAG_VALUE,
) -> Path:
    """Write a valid Matroska global tags XML file for `mkvmerge --global-tags`."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Tags>\n"
        "  <Tag>\n"
        "    <Targets />\n"
        "    <Simple>\n"
        f"      <Name>{tag_name}</Name>\n"
        f"      <String>{tag_value}</String>\n"
        "    </Simple>\n"
        "  </Tag>\n"
        "</Tags>\n"
    )
    path.write_text(xml, encoding=AppConstants.UTF8_ENCODING)
    return path


async def _run_tool(*args: str) -> tuple[bytes, bytes]:
    """Execute a media CLI tool and raise AssertionError on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != AppConstants.EXIT_CODE_SUCCESS:
        err = stderr.decode(AppConstants.UTF8_ENCODING, errors="replace")
        out = stdout.decode(AppConstants.UTF8_ENCODING, errors="replace")
        raise RuntimeError(
            f"Command failed (rc={proc.returncode}): {' '.join(args)}\n"
            f"stdout: {out}\nstderr: {err}"
        )
    return stdout, stderr


async def ffprobe_json(path: Path) -> dict:
    """Run `ffprobe -show_format -show_streams` and return parsed JSON."""
    stdout, _ = await _run_tool(
        AppConstants.TOOL_FFPROBE,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    )
    return json.loads(stdout.decode(AppConstants.UTF8_ENCODING, errors="replace"))


async def mkvmerge_probe_json(path: Path) -> dict:
    """Run `mkvmerge -J` and return parsed JSON."""
    stdout, _ = await _run_tool(
        AppConstants.TOOL_MKVMERGE,
        AppConstants.MKVMERGE_IDENTIFY_FLAG,
        str(path),
    )
    return json.loads(stdout.decode(AppConstants.UTF8_ENCODING, errors="replace"))


async def generate_single_audio_media(
    output_path: Path,
    *,
    audio_codec: str = "aac",
    channels: int = AppConstants.CHANNELS_51,
    duration_sec: int = MediaFixtureConstants.SHORT_DURATION_SEC,
    container_title: str | None = None,
    global_tag_comment: str | None = None,
) -> Path:
    """Create a synthetic `.mkv` or `.mp4` with 1 video + 1 audio track via FFmpeg."""
    layout = "5.1" if channels == AppConstants.CHANNELS_51 else "stereo"
    cmd: list[str] = [
        AppConstants.TOOL_FFMPEG,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={MediaFixtureConstants.VIDEO_SIZE}:r={MediaFixtureConstants.VIDEO_FPS}:d={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate={MediaFixtureConstants.SAMPLE_RATE}:duration={duration_sec}",
        "-filter_complex",
        f"[1:a]aformat=channel_layouts={layout}[aout]",
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "mpeg4",
        "-c:a",
        audio_codec,
        "-ac",
        str(channels),
    ]
    if container_title is not None:
        cmd.extend(["-metadata", f"title={container_title}"])
    if global_tag_comment is not None:
        cmd.extend(["-metadata", f"comment={global_tag_comment}"])
    cmd.extend(["-shortest", str(output_path)])
    await _run_tool(*cmd)
    return output_path


async def generate_multi_audio_mkv(
    output_path: Path,
    *,
    duration_sec: int = MediaFixtureConstants.SHORT_DURATION_SEC,
) -> Path:
    """Create a real `.mkv` with Audio #0 Hindi Stereo 2.0 + Audio #1 English AAC 5.1."""
    cmd: list[str] = [
        AppConstants.TOOL_FFMPEG,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={MediaFixtureConstants.VIDEO_SIZE}:r={MediaFixtureConstants.VIDEO_FPS}:d={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate={MediaFixtureConstants.SAMPLE_RATE}:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=660:sample_rate={MediaFixtureConstants.SAMPLE_RATE}:duration={duration_sec}",
        "-filter_complex",
        "[1:a]aformat=channel_layouts=stereo[a0];[2:a]aformat=channel_layouts=5.1[a1]",
        "-map",
        "0:v:0",
        "-map",
        "[a0]",
        "-map",
        "[a1]",
        "-c:v",
        "mpeg4",
        "-c:a:0",
        "aac",
        "-ac:a:0",
        str(AppConstants.CHANNELS_STEREO),
        "-metadata:s:a:0",
        "language=hin",
        "-metadata:s:a:0",
        "title=Hindi Stereo",
        "-c:a:1",
        "aac",
        "-ac:a:1",
        str(AppConstants.CHANNELS_51),
        "-metadata:s:a:1",
        "language=eng",
        "-metadata:s:a:1",
        "title=English AAC 5.1",
        "-shortest",
        str(output_path),
    ]
    await _run_tool(*cmd)
    return output_path


async def enrich_mkv_with_mkvmerge(
    raw_mkv: Path,
    output_mkv: Path,
    *,
    work_dir: Path,
    container_title: str | None = None,
    add_global_tags: bool = False,
    add_subtitle: bool = False,
    add_attachment: bool = False,
    audio_sync_track_id: int | None = None,
    audio_sync_ms: int = 0,
) -> Path:
    """Remux a raw FFmpeg `.mkv` with `mkvmerge` to add real global tags, subs, art, or sync."""
    argv: list[str] = [AppConstants.TOOL_MKVMERGE, "-o", str(output_mkv)]
    if container_title is not None:
        argv.extend(["--title", container_title])
    if add_global_tags:
        tags_xml = write_global_tags_xml(work_dir / f"{output_mkv.stem}.tags.xml")
        argv.extend(["--global-tags", str(tags_xml)])
    if add_attachment:
        cover_png = write_minimal_png(work_dir / MediaFixtureConstants.COVER_FILENAME)
        argv.extend(
            [
                "--attachment-name",
                MediaFixtureConstants.COVER_FILENAME,
                "--attachment-mime-type",
                MediaFixtureConstants.COVER_MIME_TYPE,
                "--attach-file",
                str(cover_png),
            ]
        )
    if audio_sync_track_id is not None and audio_sync_ms != 0:
        argv.extend(["--sync", f"{audio_sync_track_id}:{audio_sync_ms}"])
    argv.append(str(raw_mkv))

    if add_subtitle:
        srt_path = work_dir / f"{output_mkv.stem}.srt"
        srt_path.write_text(
            MediaFixtureConstants.SUBTITLE_SRT_BODY,
            encoding=AppConstants.UTF8_ENCODING,
        )
        argv.extend(["--language", "0:eng", str(srt_path)])

    await _run_tool(*argv)
    return output_mkv
