"""Lazy on-demand container cover-art thumbnail extraction and persistent disk cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
                [
                    AppConstants.TOOL_MKVEXTRACT,
                    "attachments",
                    str(abs_media_path),
                    f"{best_att.id}:{tmp_path}",
                ],
                timeout_sec=AppConstants.THUMBNAIL_EXTRACT_TIMEOUT_SEC,
            )

            # Strategy B: Fallback to ffmpeg attached_pic / video stream downscaled thumbnail
            if not extracted or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                extracted = await _run_extract_cmd(
                    [
                        AppConstants.TOOL_FFMPEG,
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(abs_media_path),
                        "-an",
                        "-sn",
                        "-dn",
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale='min({AppConstants.THUMBNAIL_MAX_DIMENSION_PX},iw)':-2",
                        "-f",
                        "image2",
                        "-c:v",
                        "mjpeg",
                        str(tmp_path),
                    ],
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
