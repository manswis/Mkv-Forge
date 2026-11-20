"""Staged external track upload service backed by `<DATA_DIR>/uploads/`."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable

from app.config import AppConstants, Settings
from app.models.schemas import ExternalTrackConfig
from app.utils.paths import PathSafetyError, safe_resolve_path

logger = logging.getLogger(__name__)

_SAFE_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")


class UploadValidationError(ValueError):
    """Raised when an uploaded or referenced external track fails validation (HTTP 400)."""


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds configured per-kind size limits (HTTP 413)."""


class UploadNotFoundError(FileNotFoundError):
    """Raised when a staged upload ID does not exist on disk (HTTP 404)."""


@dataclass(frozen=True, slots=True)
class StagedUploadInfo:
    """Metadata for a staged external track stored inside `<DATA_DIR>/uploads/`."""

    upload_id: str
    kind: str
    original_filename: str
    size_bytes: int
    staged_path: Path


def allowed_extensions_for_kind(kind: str) -> frozenset[str]:
    """Return the allowlisted lowercase file extensions for `audio` or `subtitles`."""
    if kind == AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES:
        return AppConstants.EXTERNAL_SUBTITLE_EXTENSIONS
    if kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO:
        return AppConstants.EXTERNAL_AUDIO_EXTENSIONS
    raise UploadValidationError(
        f"Unsupported external track kind {kind!r}; expected 'audio' or 'subtitles'"
    )


def validate_external_filename(filename: str, kind: str) -> tuple[str, str]:
    """Validate an external track filename and return `(clean_name, lowercase_ext)`."""
    cleaned = str(filename or "").strip()
    if not cleaned:
        raise UploadValidationError("Filename must not be empty")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise UploadValidationError("Filename must not contain path separators")
    if cleaned in (".", "..") or cleaned.startswith("."):
        raise UploadValidationError(f"Invalid filename: {filename!r}")

    ext = Path(cleaned).suffix.lower()
    allowed = allowed_extensions_for_kind(kind)
    if not ext or ext not in allowed:
        supported = ", ".join(sorted(allowed))
        raise UploadValidationError(
            f"Unsupported {kind} file extension {ext or '(none)'!r}. Allowed: {supported}"
        )
    return cleaned, ext


def validate_storage_external_track(
    raw_rel_path: str,
    kind: str,
    storage_root: Path,
) -> Path:
    """Resolve and validate an external track stored inside `/storage` (read-only safe)."""
    cleaned_rel = str(raw_rel_path or "").strip().lstrip("/")
    if not cleaned_rel:
        raise UploadValidationError("Storage track path must not be empty")
    try:
        resolved = safe_resolve_path(cleaned_rel, storage_root)
    except PathSafetyError as exc:
        raise UploadValidationError(str(exc)) from exc

    validate_external_filename(resolved.name, kind)
    if not resolved.is_file():
        raise UploadNotFoundError(f"External {kind} file not found in storage: /{cleaned_rel}")
    return resolved


class UploadService:
    """Manages streaming staged uploads inside `<DATA_DIR>/uploads/` with safe cleanup."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def uploads_dir(self) -> Path:
        return self._settings.uploads_dir

    def max_bytes_for_kind(self, kind: str) -> int:
        """Return configured maximum byte size for `subtitles` or `audio` uploads."""
        if kind == AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES:
            return self._settings.max_sub_upload_mb * AppConstants.BYTES_PER_MIB
        if kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO:
            return self._settings.max_audio_upload_mb * AppConstants.BYTES_PER_MIB
        raise UploadValidationError(f"Unsupported upload kind: {kind!r}")

    def _max_mb_for_kind(self, kind: str) -> int:
        if kind == AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES:
            return self._settings.max_sub_upload_mb
        return self._settings.max_audio_upload_mb

    def _ensure_uploads_dir(self) -> Path:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        return self.uploads_dir.resolve(strict=False)

    async def stage_upload_stream(
        self,
        *,
        kind: str,
        filename: str,
        chunks: AsyncIterator[bytes],
        content_length: int | None = None,
    ) -> StagedUploadInfo:
        """Stream an external track into `<DATA_DIR>/uploads/<uuid>.<ext>` enforcing limits."""
        clean_name, ext = validate_external_filename(filename, kind)
        max_bytes = self.max_bytes_for_kind(kind)
        max_mb = self._max_mb_for_kind(kind)

        if content_length is not None and content_length > max_bytes:
            raise UploadTooLargeError(
                f"Upload exceeds {max_mb} MB limit for {kind} files "
                "(if using Nginx Proxy Manager, also verify client_max_body_size)."
            )

        base_dir = self._ensure_uploads_dir()
        upload_id = f"{uuid.uuid4().hex}{ext}"
        staged_path = base_dir / upload_id
        total_bytes = 0

        try:
            with staged_path.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise UploadTooLargeError(
                            f"Upload exceeds {max_mb} MB limit for {kind} files "
                            "(if using Nginx Proxy Manager, also verify client_max_body_size)."
                        )
                    handle.write(chunk)
        except Exception:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        if total_bytes <= 0:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise UploadValidationError("Uploaded file is empty (0 bytes)")

        return StagedUploadInfo(
            upload_id=upload_id,
            kind=kind,
            original_filename=clean_name,
            size_bytes=total_bytes,
            staged_path=staged_path,
        )

    def resolve_upload_path(self, upload_id: str, *, kind: str | None = None) -> Path:
        """Resolve an existing staged upload strictly inside `<DATA_DIR>/uploads/`."""
        cleaned = str(upload_id or "").strip()
        if not _SAFE_UPLOAD_ID_RE.match(cleaned):
            raise UploadValidationError(f"Invalid upload_id format: {upload_id!r}")
        if kind is not None:
            ext = Path(cleaned).suffix.lower()
            allowed = allowed_extensions_for_kind(kind)
            if ext not in allowed:
                raise UploadValidationError(
                    f"Staged upload {cleaned!r} extension {ext!r} does not match kind {kind!r}"
                )

        base_dir = self.uploads_dir.resolve(strict=False)
        candidate = (base_dir / cleaned).resolve(strict=False)
        if not candidate.is_relative_to(base_dir) or candidate.parent != base_dir:
            raise UploadValidationError(f"Upload path escapes staging directory: {upload_id!r}")
        if not candidate.is_file():
            raise UploadNotFoundError(f"Staged upload not found: {cleaned}")
        return candidate

    def delete_upload(self, upload_id: str) -> bool:
        """Delete one unsubmitted staged upload by ID; raises on invalid/missing ID."""
        path = self.resolve_upload_path(upload_id)
        path.unlink()
        return True

    def cleanup_uploads(self, upload_ids: Iterable[str]) -> int:
        """Best-effort unlink of job-owned staged upload IDs (safe to call in `finally`)."""
        removed = 0
        for upload_id in set(upload_ids):
            if not upload_id:
                continue
            try:
                path = self.resolve_upload_path(upload_id)
                path.unlink(missing_ok=True)
                removed += 1
            except (UploadValidationError, UploadNotFoundError, OSError):
                continue
        return removed

    def sweep_orphaned_uploads(self, active_upload_ids: set[str]) -> int:
        """Remove files in `<DATA_DIR>/uploads/` not referenced by any queued/running job."""
        if not self.uploads_dir.is_dir():
            return 0
        removed = 0
        for entry in self.uploads_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name in active_upload_ids:
                continue
            try:
                entry.unlink()
                removed += 1
                logger.info("Removed orphaned staged upload: %s", entry.name)
            except OSError as exc:
                logger.warning("Failed to remove orphaned staged upload %s: %s", entry, exc)
        return removed

    def resolve_external_tracks(
        self, external_tracks: list[ExternalTrackConfig]
    ) -> list[tuple[ExternalTrackConfig, Path]]:
        """Validate and resolve all external tracks for a job into `(config, Path)` pairs."""
        resolved: list[tuple[ExternalTrackConfig, Path]] = []
        for ext in external_tracks:
            if ext.source == AppConstants.EXTERNAL_TRACK_SOURCE_STORAGE:
                assert ext.path is not None
                path = validate_storage_external_track(
                    ext.path,
                    ext.kind,
                    self._settings.storage_dir,
                )
            else:
                assert ext.upload_id is not None
                path = self.resolve_upload_path(ext.upload_id, kind=ext.kind)
            resolved.append((ext, path))
        return resolved


def extract_upload_ids_from_tracks(
    external_tracks: Iterable[ExternalTrackConfig],
) -> list[str]:
    """Extract all staged `upload_id` values referenced by `external_tracks`."""
    return [
        ext.upload_id
        for ext in external_tracks
        if ext.source == AppConstants.EXTERNAL_TRACK_SOURCE_UPLOAD and ext.upload_id
    ]
