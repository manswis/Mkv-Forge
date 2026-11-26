"""Lazy on-demand container cover-art thumbnail extraction and persistent disk cache."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

from app.config import AppConstants
from app.models.schemas import AttachmentInfo
from app.services.probe_parser import extract_attachments_from_probe
from app.utils.paths import PathSafetyError, safe_resolve_path

logger = logging.getLogger(__name__)

_EXTRACT_SEMAPHORE = asyncio.Semaphore(AppConstants.THUMBNAIL_MAX_CONCURRENCY)

_PREFERRED_COVER_STEMS: tuple[str, ...] = (
    "small_cover_land",
    "small_cover",
    "cover_land",
    "cover",
    "poster",
    "folder",
    "album",
    "front",
    "thumb",
    "thumbnail",
)

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF87_MAGIC = b"GIF87a"
_GIF89_MAGIC = b"GIF89a"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


def is_image_attachment(att: AttachmentInfo | dict[str, Any]) -> bool:
    """Return True if an attachment entry represents an embedded image (cover/poster/art)."""
    if isinstance(att, AttachmentInfo):
        ctype = att.content_type.strip().lower()
        fname = att.file_name.strip().lower()
    elif isinstance(att, dict):
        ctype = str(att.get("content_type") or "").strip().lower()
        fname = str(att.get("file_name") or "").strip().lower()
    else:
        return False

    if ctype.startswith("image/"):
        return True
    return any(fname.endswith(ext) for ext in AppConstants.IMAGE_ATTACHMENT_EXTENSIONS)


def has_image_attachment_in_raw_json(raw_json: str | dict[str, Any] | None) -> bool:
    """Fast inspection of `mkvmerge -J` payload to check if any embedded image attachment exists."""
    if not raw_json:
        return False
    if isinstance(raw_json, str):
        if "attachments" not in raw_json:
            return False
        try:
            payload = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            return False
    elif isinstance(raw_json, dict):
        payload = raw_json
    else:
        return False

    if not isinstance(payload, dict):
        return False
    raw_atts = payload.get("attachments")
    if not isinstance(raw_atts, list) or not raw_atts:
        return False
    return any(is_image_attachment(item) for item in raw_atts if isinstance(item, dict))


def select_best_cover_attachment(payload: dict[str, Any]) -> AttachmentInfo | None:
    """Pick the most appropriate embedded cover-art attachment from a parsed probe dictionary."""
    attachments = [
        att for att in extract_attachments_from_probe(payload) if is_image_attachment(att)
    ]
    if not attachments:
        return None

    def _rank_key(att: AttachmentInfo) -> tuple[int, int, int]:
        stem = Path(att.file_name.lower()).stem
        for idx, pref in enumerate(_PREFERRED_COVER_STEMS):
            if stem == pref or stem.startswith(pref):
                return (0, idx, att.size_bytes if att.size_bytes > 0 else 10**9)
        return (1, 99, att.size_bytes if att.size_bytes > 0 else 10**9)

    return min(attachments, key=_rank_key)


def detect_image_media_type(data: bytes) -> str:
    """Detect image MIME type from magic header bytes."""
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_GIF87_MAGIC) or data.startswith(_GIF89_MAGIC):
        return "image/gif"
    if len(data) >= 12 and data[:4] == _RIFF_MAGIC and data[8:12] == _WEBP_MAGIC:
        return "image/webp"
    return "image/jpeg"


def is_valid_image_bytes(data: bytes) -> bool:
    """Return True if `data` begins with a recognized image magic header."""
    if not data or len(data) < 8:
        return False
    if data.startswith((_JPEG_MAGIC, _PNG_MAGIC, _GIF87_MAGIC, _GIF89_MAGIC)):
        return True
    if len(data) >= 12 and data[:4] == _RIFF_MAGIC and data[8:12] == _WEBP_MAGIC:
        return True
    return False


def get_thumbnail_cache_path(data_dir: Path, file_id: int, mtime: float) -> Path:
    """Return deterministic content-versioned disk cache path for a file's thumbnail."""
    thumbs_dir = data_dir / AppConstants.THUMBNAILS_SUBDIR_NAME
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    mtime_tag = max(0, int(mtime))
    return thumbs_dir / f"thumb_{int(file_id)}_{mtime_tag}.img"


def build_mkvextract_attachment_command(
    media_path: Path,
    attachment_id: int,
    output_path: Path,
) -> list[str]:
    """Build canonical MKVToolNix v17+ `mkvextract <file> attachments <id>:<out>` argv."""
    return [
        AppConstants.TOOL_MKVEXTRACT,
        str(media_path),
        "attachments",
        f"{attachment_id}:{output_path}",
    ]


def build_ffmpeg_thumbnail_command(
    source_path: Path,
    output_path: Path,
    *,
    map_first_video: bool = False,
) -> list[str]:
    """Build FFmpeg 7+ safe JPEG thumbnail argv normalizing 10-bit HDR / RGBA to yuvj420p."""
    vf_chain = (
        f"scale='min({AppConstants.THUMBNAIL_MAX_DIMENSION_PX},iw)':-2,"
        f"format={AppConstants.THUMBNAIL_JPEG_PIXEL_FORMAT}"
    )
    argv: list[str] = [
        AppConstants.TOOL_FFMPEG,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source_path),
    ]
    if map_first_video:
        argv.extend(["-map", "0:v:0"])
    argv.extend(
        [
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            vf_chain,
            "-f",
            "image2",
            "-c:v",
            "mjpeg",
            str(output_path),
        ]
    )
    return argv


async def _run_extract_cmd(cmd: list[str], timeout_sec: float) -> bool:
    """Run an extraction subprocess with strict timeout and cleanup on failure."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        return proc.returncode == 0
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except OSError:
                pass
        return False


async def get_or_extract_thumbnail(
    row: dict[str, Any],
    *,
    storage_dir: Path,
    data_dir: Path,
) -> tuple[bytes, str] | None:
    """Return `(image_bytes, media_type)` from disk cache or lazy on-demand extraction."""
    file_id = int(row["id"])
    mtime = float(row.get("mtime") or 0.0)
    cache_path = get_thumbnail_cache_path(data_dir, file_id, mtime)

    # 1. Fast Path: Disk Cache Hit (<0.3ms, zero subprocesses)
    if cache_path.is_file():
        try:
            cached = await asyncio.to_thread(cache_path.read_bytes)
            if is_valid_image_bytes(cached):
                return cached, detect_image_media_type(cached)
        except OSError:
            pass

    raw_json_str = str(row.get("raw_json") or "{}")
    try:
        payload = json.loads(raw_json_str)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    best_att = select_best_cover_attachment(payload)
    if best_att is None:
        return None

    try:
        abs_media_path = safe_resolve_path(str(row["filepath"]), storage_dir)
    except PathSafetyError:
        return None
    if not abs_media_path.is_file():
        return None

    # 2. Throttled On-Demand Extraction (bounded by semaphore so CPU/disk never spikes)
    async with _EXTRACT_SEMAPHORE:
        if cache_path.is_file():
            try:
                cached = await asyncio.to_thread(cache_path.read_bytes)
                if is_valid_image_bytes(cached):
                    return cached, detect_image_media_type(cached)
            except OSError:
                pass

        fd, tmp_str = tempfile.mkstemp(
            prefix=f".thumb_{file_id}_",
            suffix=".img",
            dir=str(cache_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            # Strategy A: mkvextract seeks straight to the Matroska attachment table in <10ms
            extracted = await _run_extract_cmd(
                build_mkvextract_attachment_command(
                    abs_media_path, best_att.id, tmp_path
                ),
                timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
            )

            # Strategy B: Fallback to ffmpeg attached_pic / video stream downscaled thumbnail
            if not extracted or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                logger.warning(
                    "Thumbnail mkvextract failed for %s (attachment id=%s); falling back to ffmpeg",
                    abs_media_path.name,
                    best_att.id,
                )
                extracted = await _run_extract_cmd(
                    build_ffmpeg_thumbnail_command(abs_media_path, tmp_path),
                    timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
                )

            if not extracted or not tmp_path.is_file():
                return None

            raw_bytes = await asyncio.to_thread(tmp_path.read_bytes)
            if not is_valid_image_bytes(raw_bytes):
                return None

            await asyncio.to_thread(os.replace, tmp_path, cache_path)
            return raw_bytes, detect_image_media_type(raw_bytes)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def is_other_file_thumbnail_candidate(path_or_ext: str | None) -> bool:
    """Return True if a non-indexed file extension may contain album/cover art or be an image."""
    if not path_or_ext:
        return False
    cleaned = str(path_or_ext).strip().lower()
    if not cleaned:
        return False
    ext = cleaned if cleaned.startswith(".") and "/" not in cleaned else Path(cleaned).suffix.lower()
    return ext in AppConstants.OTHER_FILE_THUMBNAIL_EXTENSIONS


def get_other_thumbnail_cache_path(
    data_dir: Path,
    rel_filepath: str,
    mtime: float,
) -> Path:
    """Return deterministic disk cache path for a non-indexed file thumbnail."""
    thumbs_dir = data_dir / AppConstants.THUMBNAILS_SUBDIR_NAME
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(rel_filepath.encode(AppConstants.UTF8_ENCODING)).hexdigest()[
        : AppConstants.OTHER_THUMBNAIL_HASH_LEN
    ]
    mtime_tag = max(0, int(mtime))
    return thumbs_dir / f"other_{digest}_{mtime_tag}.img"


def _parse_flac_picture_block(block: bytes) -> tuple[int, bytes] | None:
    """Parse a FLAC METADATA_BLOCK_PICTURE binary block into `(picture_type, image_bytes)`."""
    if len(block) < 32:
        return None
    offset = 0
    picture_type, mime_len = struct.unpack_from(">II", block, offset)
    offset += 8
    if offset + mime_len + 4 > len(block):
        return None
    offset += mime_len
    (desc_len,) = struct.unpack_from(">I", block, offset)
    offset += 4
    if offset + desc_len + 20 > len(block):
        return None
    offset += desc_len
    # Skip width, height, color depth, indexed color count (4 x uint32 = 16 bytes)
    offset += 16
    (data_len,) = struct.unpack_from(">I", block, offset)
    offset += 4
    if data_len <= 0 or offset + data_len > len(block):
        return None
    image_bytes = block[offset : offset + data_len]
    if not is_valid_image_bytes(image_bytes):
        return None
    return picture_type, image_bytes


def _extract_vorbis_comment_picture(comment_payload: bytes) -> bytes | None:
    """Extract embedded `METADATA_BLOCK_PICTURE` image bytes from an OpusTags/VorbisComment packet."""
    if len(comment_payload) < 8:
        return None
    offset = 0
    (vendor_len,) = struct.unpack_from("<I", comment_payload, offset)
    offset += 4
    if offset + vendor_len + 4 > len(comment_payload):
        return None
    offset += vendor_len
    (comment_count,) = struct.unpack_from("<I", comment_payload, offset)
    offset += 4

    fallback_img: bytes | None = None
    prefix = b"METADATA_BLOCK_PICTURE="
    prefix_len = len(prefix)
    for _ in range(min(comment_count, 512)):
        if offset + 4 > len(comment_payload):
            break
        (entry_len,) = struct.unpack_from("<I", comment_payload, offset)
        offset += 4
        if entry_len < 0 or offset + entry_len > len(comment_payload):
            break
        entry = comment_payload[offset : offset + entry_len]
        offset += entry_len
        if len(entry) <= prefix_len or entry[:prefix_len].upper() != prefix:
            continue
        try:
            decoded = base64.b64decode(entry[prefix_len:], validate=False)
        except (ValueError, TypeError):
            continue
        parsed = _parse_flac_picture_block(decoded)
        if parsed is None:
            continue
        pic_type, img_bytes = parsed
        if pic_type == AppConstants.FLAC_PICTURE_TYPE_FRONT_COVER:
            return img_bytes
        if fallback_img is None:
            fallback_img = img_bytes
    return fallback_img


def _extract_ogg_embedded_picture_sync(file_path: Path) -> bytes | None:
    """Parse Ogg pages (Opus/Vorbis) to extract `METADATA_BLOCK_PICTURE` cover art."""
    try:
        with file_path.open("rb") as fh:
            data = fh.read(AppConstants.OTHER_THUMBNAIL_OGG_MAX_READ_BYTES)
    except OSError:
        return None

    if not data.startswith(b"OggS"):
        return None

    offset = 0
    total_len = len(data)
    packet_buf = bytearray()
    packets_seen = 0

    for _ in range(AppConstants.OTHER_THUMBNAIL_OGG_MAX_PAGES):
        if offset + 27 > total_len or data[offset : offset + 4] != b"OggS":
            break
        num_segments = data[offset + 26]
        seg_table_end = offset + 27 + num_segments
        if seg_table_end > total_len:
            break
        seg_table = data[offset + 27 : seg_table_end]
        body_offset = seg_table_end
        body_len = sum(seg_table)
        if body_offset + body_len > total_len:
            break

        cursor = body_offset
        for seg_len in seg_table:
            packet_buf.extend(data[cursor : cursor + seg_len])
            cursor += seg_len
            if seg_len < 255:
                packet = bytes(packet_buf)
                packet_buf.clear()
                packets_seen += 1
                if packet.startswith(b"OpusTags"):
                    return _extract_vorbis_comment_picture(packet[8:])
                if packet.startswith(b"\x03vorbis"):
                    return _extract_vorbis_comment_picture(packet[7:])
                # Header packets always appear within the first 3 logical packets
                if packets_seen >= 3:
                    return None
        offset = body_offset + body_len
    return None


def _extract_native_flac_picture_sync(file_path: Path) -> bytes | None:
    """Extract embedded PICTURE metadata block (type 6) directly from a native FLAC file."""
    try:
        with file_path.open("rb") as fh:
            magic = fh.read(4)
            if magic != b"fLaC":
                return None
            fallback_img: bytes | None = None
            bytes_scanned = 4
            while bytes_scanned < AppConstants.OTHER_THUMBNAIL_OGG_MAX_READ_BYTES:
                hdr = fh.read(4)
                if len(hdr) < 4:
                    break
                is_last = bool(hdr[0] & 0x80)
                block_type = hdr[0] & 0x7F
                block_len = (hdr[1] << 16) | (hdr[2] << 8) | hdr[3]
                bytes_scanned += 4 + block_len
                if block_type == AppConstants.FLAC_BLOCK_TYPE_PICTURE and block_len > 0:
                    if block_len <= AppConstants.OTHER_THUMBNAIL_DIRECT_IMAGE_MAX_BYTES:
                        block_data = fh.read(block_len)
                        parsed = _parse_flac_picture_block(block_data)
                        if parsed is not None:
                            pic_type, img_bytes = parsed
                            if pic_type == AppConstants.FLAC_PICTURE_TYPE_FRONT_COVER:
                                return img_bytes
                            if fallback_img is None:
                                fallback_img = img_bytes
                    else:
                        fh.seek(block_len, os.SEEK_CUR)
                else:
                    fh.seek(block_len, os.SEEK_CUR)
                if is_last:
                    break
            return fallback_img
    except OSError:
        return None


def extract_embedded_audio_picture_sync(file_path: Path) -> bytes | None:
    """Fast pure-Python album-art extraction for Ogg Opus/Vorbis and native FLAC files."""
    ext = file_path.suffix.lower()
    if ext in (".opus", ".ogg", ".oga"):
        return _extract_ogg_embedded_picture_sync(file_path)
    if ext == ".flac":
        return _extract_native_flac_picture_sync(file_path)
    return None


def _write_cache_atomic_sync(cache_path: Path, payload: bytes) -> None:
    """Atomically write thumbnail bytes or negative sentinel to disk cache."""
    fd, tmp_str = tempfile.mkstemp(
        prefix=".other_thumb_",
        suffix=".img",
        dir=str(cache_path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, cache_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


async def get_or_extract_other_file_thumbnail(
    rel_filepath: str,
    *,
    storage_dir: Path,
    data_dir: Path,
) -> tuple[bytes, str] | None:
    """Return `(image_bytes, media_type)` for a non-indexed audio/image/media file.

    Raises `PathSafetyError` if `rel_filepath` escapes `storage_dir`.
    Caches both positive thumbnails and negative misses keyed by `(filepath_hash, mtime)`.
    """
    abs_path = safe_resolve_path(rel_filepath, storage_dir)
    if not abs_path.is_file():
        return None
    ext = abs_path.suffix.lower()
    if ext not in AppConstants.OTHER_FILE_THUMBNAIL_EXTENSIONS:
        return None

    try:
        stat = await asyncio.to_thread(abs_path.stat)
    except OSError:
        return None

    cache_path = get_other_thumbnail_cache_path(data_dir, rel_filepath, float(stat.st_mtime))

    # 1. Fast Path: Disk Cache Hit (positive image or negative miss sentinel)
    if cache_path.is_file():
        try:
            cached = await asyncio.to_thread(cache_path.read_bytes)
            if cached == AppConstants.THUMBNAIL_NEGATIVE_MARKER_BYTES:
                return None
            if is_valid_image_bytes(cached):
                return cached, detect_image_media_type(cached)
        except OSError:
            pass

    # 2. Direct Image Files (.jpg, .png, .webp, .gif)
    if ext in AppConstants.IMAGE_ATTACHMENT_EXTENSIONS:
        if stat.st_size <= 0 or stat.st_size > AppConstants.OTHER_THUMBNAIL_DIRECT_IMAGE_MAX_BYTES:
            return None
        if stat.st_size <= AppConstants.OTHER_THUMBNAIL_DOWNSCALE_THRESHOLD_BYTES:
            raw_bytes = await asyncio.to_thread(abs_path.read_bytes)
            if is_valid_image_bytes(raw_bytes):
                await asyncio.to_thread(_write_cache_atomic_sync, cache_path, raw_bytes)
                return raw_bytes, detect_image_media_type(raw_bytes)
            await asyncio.to_thread(
                _write_cache_atomic_sync, cache_path, AppConstants.THUMBNAIL_NEGATIVE_MARKER_BYTES
            )
            return None

    # 3. Fast Pure-Python Album Art Path (.opus, .ogg, .flac) — zero subprocesses
    direct_art = await asyncio.to_thread(extract_embedded_audio_picture_sync, abs_path)
    if direct_art is not None and is_valid_image_bytes(direct_art):
        await asyncio.to_thread(_write_cache_atomic_sync, cache_path, direct_art)
        return direct_art, detect_image_media_type(direct_art)

    # 4. Throttled FFmpeg Fallback (MP3 ID3 APIC, M4A covr, MKA, large images, MOV/WebM)
    async with _EXTRACT_SEMAPHORE:
        if cache_path.is_file():
            try:
                cached = await asyncio.to_thread(cache_path.read_bytes)
                if cached == AppConstants.THUMBNAIL_NEGATIVE_MARKER_BYTES:
                    return None
                if is_valid_image_bytes(cached):
                    return cached, detect_image_media_type(cached)
            except OSError:
                pass

        fd, tmp_str = tempfile.mkstemp(
            prefix=".other_thumb_ff_",
            suffix=".img",
            dir=str(cache_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            extracted = await _run_extract_cmd(
                build_ffmpeg_thumbnail_command(
                    abs_path, tmp_path, map_first_video=True
                ),
                timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
            )
            if extracted and tmp_path.is_file():
                raw_bytes = await asyncio.to_thread(tmp_path.read_bytes)
                if is_valid_image_bytes(raw_bytes):
                    await asyncio.to_thread(os.replace, tmp_path, cache_path)
                    return raw_bytes, detect_image_media_type(raw_bytes)

            # Fallback for large direct image files if ffmpeg is unavailable
            if ext in AppConstants.IMAGE_ATTACHMENT_EXTENSIONS:
                raw_img = await asyncio.to_thread(abs_path.read_bytes)
                if is_valid_image_bytes(raw_img):
                    await asyncio.to_thread(_write_cache_atomic_sync, cache_path, raw_img)
                    return raw_img, detect_image_media_type(raw_img)

            await asyncio.to_thread(
                _write_cache_atomic_sync,
                cache_path,
                AppConstants.THUMBNAIL_NEGATIVE_MARKER_BYTES,
            )
            return None
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
