"""Safe file and folder operations (rename, move, delete) with :ro and job locks."""

from __future__ import annotations

import errno
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.config import AppConstants, Settings
from app.db.repositories import (
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
    utc_now_iso,
)
from app.models.schemas import (
    BatchFileOpItemResult,
    BatchFileOpResponse,
    FolderOpResponse,
    OtherFileOpResponse,
)
from app.services.folder_service import normalize_storage_folder_path
from app.services.library_events import LibraryEventBroker
from app.utils.paths import (
    PathSafetyError,
    extract_folder,
    is_mkvforge_temp_file,
    is_supported_media_file,
    safe_resolve_path,
    to_relative_storage_path,
)


class FileOpError(RuntimeError):
    """Base error for file/folder management operations."""

    status_code: int = AppConstants.HTTP_BAD_REQUEST


class FileOpNotFoundError(FileOpError):
    """Raised when a target file or folder does not exist."""

    status_code: int = AppConstants.HTTP_NOT_FOUND


class FileOpConflictError(FileOpError):
    """Raised on name collisions, active job locks, or non-empty folder guard."""

    status_code: int = AppConstants.HTTP_CONFLICT


class FileOpPermissionError(FileOpError):
    """Raised when the storage mount is read-only (:ro) or denies write access."""

    status_code: int = AppConstants.HTTP_FORBIDDEN


def _raise_if_permission_os_error(exc: OSError, action: str) -> None:
    """Translate read-only filesystem / permission OSErrors into HTTP 403 errors."""
    if isinstance(exc, PermissionError) or exc.errno in (
        errno.EROFS,
        errno.EACCES,
        errno.EPERM,
    ):
        if exc.errno == errno.EROFS:
            raise FileOpPermissionError(
                f"Permission denied: storage volume is read-only (cannot {action})"
            ) from exc
        raise FileOpPermissionError(
            f"Permission denied: cannot {action} ({exc.strerror or exc})"
        ) from exc


def _validate_single_name(raw_name: str, *, kind: str) -> str:
    """Validate a single filename or folder name segment (no slashes or traversal)."""
    cleaned = raw_name.strip()
    if not cleaned:
        raise PathSafetyError(f"{kind} name must not be empty")
    if (
        "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in (".", "..")
        or cleaned.startswith(".")
    ):
        raise PathSafetyError(f"Invalid {kind.lower()} name: {raw_name!r}")
    return cleaned


def _safe_move_file_no_overwrite(src: Path, dst: Path, *, action: str) -> None:
    """Move a file without overwriting `dst`, translating :ro/permission errors."""
    if dst.exists():
        raise FileOpConflictError(
            f"Destination already exists: {dst.name}"
        )
    try:
        try:
            os.link(src, dst)
            src.unlink()
            return
        except FileExistsError as exc:
            raise FileOpConflictError(
                f"Destination already exists: {dst.name}"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.EROFS or exc.errno == errno.EACCES:
                _raise_if_permission_os_error(exc, action)
            if exc.errno not in (
                errno.EPERM,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
                errno.EXDEV,
            ):
                raise

        if dst.exists():
            raise FileOpConflictError(f"Destination already exists: {dst.name}")
        shutil.move(str(src), str(dst))
    except OSError as exc:
        _raise_if_permission_os_error(exc, action)
        raise FileOpError(f"Failed to {action}: {exc}") from exc


def _safe_move_dir_no_overwrite(src: Path, dst: Path, *, action: str) -> None:
    """Move or rename a directory without overwriting `dst`, translating :ro errors."""
    if dst.exists():
        raise FileOpConflictError(f"Destination folder already exists: {dst.name}")
    try:
        try:
            src.rename(dst)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                _raise_if_permission_os_error(exc, action)
                raise
        shutil.move(str(src), str(dst))
    except OSError as exc:
        _raise_if_permission_os_error(exc, action)
        raise FileOpError(f"Failed to {action}: {exc}") from exc


_DUPLICATE_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\s+\(\d+\)$")


def _next_duplicate_path(src_path: Path, storage_dir: Path) -> Path:
    """Resolve the next available `<base> (1)<ext>`, `<base> (2)<ext>`, ... sibling path."""
    stem = src_path.stem
    match = _DUPLICATE_SUFFIX_RE.match(stem)
    base_stem = match.group("base").strip() if match and match.group("base").strip() else stem
    ext = src_path.suffix

    for index in range(1, AppConstants.MAX_DUPLICATE_COLLISIONS + 1):
        candidate_name = f"{base_stem} ({index}){ext}"
        candidate_path = safe_resolve_path(
            src_path.parent / candidate_name,
            storage_dir,
        )
        if not candidate_path.exists():
            return candidate_path

    raise FileOpConflictError(
        f"Unable to allocate duplicate filename for {src_path.name}"
    )


def _dir_is_non_empty(directory: Path) -> bool:
    """Return True if `directory` contains any file or subdirectory."""
    try:
        with os.scandir(directory) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


class FileOpsService:
    """Coordinates safe disk + SQLite + SSE mutations for files and folders."""

    def __init__(
        self,
        *,
        settings: Settings,
        media_repo: MediaFileRepository,
        job_repo: JobRepository,
        library_broker: LibraryEventBroker | None = None,
    ) -> None:
        self._settings = settings
        self._media_repo = media_repo
        self._job_repo = job_repo
        self._library_broker = library_broker

    def _emit(self, *, action: str, filepath: str, file_id: int | None = None) -> None:
        if self._library_broker is not None:
            self._library_broker.record_change(
                action=action,
                filepath=filepath,
                file_id=file_id,
            )

    def _resolve_folder_dir(self, display_path: str) -> tuple[Path, str]:
        normalized = normalize_storage_folder_path(display_path)
        root = self._settings.storage_dir.resolve(strict=False)
        if normalized == "/":
            return root, "/"
        resolved = safe_resolve_path(normalized.lstrip("/"), root, allow_root=True)
        return resolved, normalized

    # ------------------------------------------------------------------
    # File Operations
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_not_scanning(row: dict[str, Any], *, action: str) -> None:
        if (
            str(row.get("scan_status") or AppConstants.FILE_SCAN_STATUS_READY)
            == AppConstants.FILE_SCAN_STATUS_SCANNING
        ):
            raise FileOpConflictError(
                f"Cannot {action} {row['filename']} while metadata scan is in progress"
            )

    def rename_file(self, file_id: int, new_name: str) -> dict[str, Any]:
        """Rename a single media file on disk and update SQLite without re-probing."""
        row = self._media_repo.get_by_id(file_id)
        if row is None:
            raise FileOpNotFoundError(f"Media file {file_id} not found")
        self._ensure_not_scanning(row, action="rename")

        rel_filepath = str(row["filepath"])
        if self._job_repo.get_active_job_for_filepath(rel_filepath) is not None:
            raise FileOpConflictError(
                f"Cannot rename {row['filename']} while a job is queued or running for it"
            )

        src_path = safe_resolve_path(rel_filepath, self._settings.storage_dir)
        if not src_path.is_file():
            raise FileOpNotFoundError(f"File not found on disk: {rel_filepath}")

        cleaned = _validate_single_name(new_name, kind="File")
        orig_ext = src_path.suffix.lower()
        candidate_path_name = cleaned
        if Path(cleaned).suffix == "":
            candidate_path_name = f"{cleaned}{orig_ext}"
        elif Path(cleaned).suffix.lower() != orig_ext:
            raise PathSafetyError(
                f"Rename must preserve container extension ({orig_ext}); use the Editor to remux formats"
            )

        dst_path = safe_resolve_path(
            src_path.parent / candidate_path_name,
            self._settings.storage_dir,
        )
        if is_mkvforge_temp_file(dst_path):
            raise PathSafetyError("Filename cannot contain reserved '.mkvforge.' marker")
        if dst_path == src_path:
            return row

        _safe_move_file_no_overwrite(src_path, dst_path, action=f"rename {src_path.name}")

        new_rel = to_relative_storage_path(dst_path, self._settings.storage_dir)
        new_folder = extract_folder(new_rel)
        updated = self._media_repo.update_file_location(
            file_id,
            filepath=new_rel,
            folder=new_folder,
            filename=dst_path.name,
        )
        assert updated is not None
        self._emit(action="updated", filepath=new_rel, file_id=file_id)
        return updated

    def duplicate_file(self, file_id: int) -> dict[str, Any]:
        """Duplicate a media file in the same folder (`<name> (1).ext`, `<name> (2).ext`, ...) and index it."""
        row = self._media_repo.get_by_id(file_id)
        if row is None:
            raise FileOpNotFoundError(f"Media file {file_id} not found")
        self._ensure_not_scanning(row, action="duplicate")

        rel_filepath = str(row["filepath"])
        if self._job_repo.get_active_job_for_filepath(rel_filepath) is not None:
            raise FileOpConflictError(
                f"Cannot duplicate {row['filename']} while a job is queued or running for it"
            )

        src_path = safe_resolve_path(rel_filepath, self._settings.storage_dir)
        if not src_path.is_file():
            raise FileOpNotFoundError(f"File not found on disk: {rel_filepath}")

        dst_path = _next_duplicate_path(src_path, self._settings.storage_dir)
        tmp_path = dst_path.with_name(
            f".{dst_path.name}{AppConstants.TEMP_MARKER}dup-{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            shutil.copy2(str(src_path), str(tmp_path))
            _safe_move_file_no_overwrite(
                tmp_path,
                dst_path,
                action=f"duplicate {src_path.name}",
            )
        except OSError as exc:
            _raise_if_permission_os_error(exc, f"duplicate {src_path.name}")
            raise FileOpError(f"Failed to duplicate {src_path.name}: {exc}") from exc
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        stat = dst_path.stat()
        new_rel = to_relative_storage_path(dst_path, self._settings.storage_dir)
        new_folder = extract_folder(new_rel)
        new_id = self._media_repo.upsert(
            MediaFileUpsert(
                filepath=new_rel,
                folder=new_folder,
                filename=dst_path.name,
                size_bytes=int(stat.st_size),
                mtime=float(stat.st_mtime),
                duration_us=int(row.get("duration_us") or 0),
                resolution=row.get("resolution"),
                video_codec=row.get("video_codec"),
                video_dynamic_range=row.get("video_dynamic_range"),
                audio_summary_json=str(row.get("audio_summary_json") or "[]"),
                subtitle_count=int(row.get("subtitle_count") or 0),
                container_title=str(row.get("container_title") or ""),
                has_global_tags=bool(row.get("has_global_tags")),
                has_aac_51=bool(row.get("has_aac_51")),
                has_ac3_eac3=bool(row.get("has_ac3_eac3")),
                raw_json=str(row.get("raw_json") or "{}"),
                scanned_at=utc_now_iso(),
            )
        )
        created = self._media_repo.get_by_id(new_id)
        assert created is not None
        self._emit(action="added", filepath=new_rel, file_id=new_id)
        return created

    def move_files(
        self,
        file_ids: list[int],
        destination_folder: str,
    ) -> BatchFileOpResponse:
        """Move one or more indexed media files into `destination_folder`."""
        dst_dir, normalized_dst = self._resolve_folder_dir(destination_folder)
        if not dst_dir.is_dir():
            raise FileOpNotFoundError(f"Destination folder does not exist: {normalized_dst}")

        results: list[BatchFileOpItemResult] = []
        succeeded = 0
        failed = 0

        for file_id in dict.fromkeys(file_ids):
            row = self._media_repo.get_by_id(file_id)
            if row is None:
                failed += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        ok=False,
                        status_code=AppConstants.HTTP_NOT_FOUND,
                        error=f"Media file {file_id} not found",
                    )
                )
                continue

            rel_filepath = str(row["filepath"])
            try:
                self._ensure_not_scanning(row, action="move")
                if self._job_repo.get_active_job_for_filepath(rel_filepath) is not None:
                    raise FileOpConflictError(
                        f"Active or queued job exists for {row['filename']}"
                    )

                src_path = safe_resolve_path(rel_filepath, self._settings.storage_dir)
                if not src_path.is_file():
                    raise FileOpNotFoundError(f"File missing on disk: {rel_filepath}")

                dst_path = safe_resolve_path(
                    dst_dir / src_path.name,
                    self._settings.storage_dir,
                )
                if dst_path == src_path:
                    succeeded += 1
                    results.append(
                        BatchFileOpItemResult(
                            file_id=file_id,
                            filepath=rel_filepath,
                            new_filepath=rel_filepath,
                            ok=True,
                            status_code=AppConstants.HTTP_OK,
                        )
                    )
                    continue

                _safe_move_file_no_overwrite(
                    src_path, dst_path, action=f"move {src_path.name}"
                )
                new_rel = to_relative_storage_path(dst_path, self._settings.storage_dir)
                new_folder = extract_folder(new_rel)
                self._media_repo.update_file_location(
                    file_id,
                    filepath=new_rel,
                    folder=new_folder,
                    filename=dst_path.name,
                )
                self._emit(action="updated", filepath=new_rel, file_id=file_id)
                succeeded += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        filepath=rel_filepath,
                        new_filepath=new_rel,
                        ok=True,
                        status_code=AppConstants.HTTP_OK,
                    )
                )
            except (FileOpError, PathSafetyError) as exc:
                status = (
                    exc.status_code
                    if isinstance(exc, FileOpError)
                    else AppConstants.HTTP_BAD_REQUEST
                )
                failed += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        filepath=rel_filepath,
                        ok=False,
                        status_code=status,
                        error=str(exc),
                    )
                )

        return BatchFileOpResponse(
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    def delete_files(self, file_ids: list[int]) -> BatchFileOpResponse:
        """Delete one or more media files from disk and SQLite (respects :ro & job locks)."""
        results: list[BatchFileOpItemResult] = []
        succeeded = 0
        failed = 0

        for file_id in dict.fromkeys(file_ids):
            row = self._media_repo.get_by_id(file_id)
            if row is None:
                failed += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        ok=False,
                        status_code=AppConstants.HTTP_NOT_FOUND,
                        error=f"Media file {file_id} not found",
                    )
                )
                continue

            rel_filepath = str(row["filepath"])
            try:
                self._ensure_not_scanning(row, action="delete")
                if self._job_repo.get_active_job_for_filepath(rel_filepath) is not None:
                    raise FileOpConflictError(
                        f"Cannot delete {row['filename']} while a job is queued or running for it"
                    )

                src_path = safe_resolve_path(rel_filepath, self._settings.storage_dir)
                if not src_path.is_file():
                    raise FileOpNotFoundError(f"File missing on disk: {rel_filepath}")

                try:
                    src_path.unlink()
                except OSError as exc:
                    _raise_if_permission_os_error(exc, f"delete {src_path.name}")
                    raise FileOpError(f"Failed to delete {src_path.name}: {exc}") from exc

                # Only remove SQLite row AFTER disk deletion succeeded!
                self._media_repo.delete_by_id(file_id)
                self._emit(action="deleted", filepath=rel_filepath, file_id=file_id)
                succeeded += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        filepath=rel_filepath,
                        ok=True,
                        status_code=AppConstants.HTTP_OK,
                    )
                )
            except (FileOpError, PathSafetyError) as exc:
                status = (
                    exc.status_code
                    if isinstance(exc, FileOpError)
                    else AppConstants.HTTP_BAD_REQUEST
                )
                failed += 1
                results.append(
                    BatchFileOpItemResult(
                        file_id=file_id,
                        filepath=rel_filepath,
                        ok=False,
                        status_code=status,
                        error=str(exc),
                    )
                )

        return BatchFileOpResponse(
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    # ------------------------------------------------------------------
    # Folder Operations
    # ------------------------------------------------------------------

    def rename_folder(self, path: str, new_name: str) -> FolderOpResponse:
        """Rename a folder inside STORAGE_DIR and rewrite all child SQLite rows."""
        src_dir, old_norm = self._resolve_folder_dir(path)
        if old_norm == "/":
            raise PathSafetyError("Cannot rename storage root '/'")
        if not src_dir.is_dir():
            raise FileOpNotFoundError(f"Folder does not exist: {old_norm}")

        if self._job_repo.get_active_job_in_folder(old_norm) is not None:
            raise FileOpConflictError(
                f"Cannot rename {old_norm} while a job is queued or running inside it"
            )

        cleaned = _validate_single_name(new_name, kind="Folder")
        dst_dir = safe_resolve_path(
            src_dir.parent / cleaned,
            self._settings.storage_dir,
        )
        new_norm = f"/{to_relative_storage_path(dst_dir, self._settings.storage_dir)}"
        if dst_dir == src_dir:
            return FolderOpResponse(old_path=old_norm, new_path=new_norm, affected_files=0)

        _safe_move_dir_no_overwrite(src_dir, dst_dir, action=f"rename folder {old_norm}")
        updated = self._media_repo.update_folder_prefix(old_norm, new_norm)
        self._emit(action="updated", filepath=new_norm.lstrip("/"))
        return FolderOpResponse(
            old_path=old_norm,
            new_path=new_norm,
            affected_files=len(updated),
        )

    def move_folder(self, path: str, destination_folder: str) -> FolderOpResponse:
        """Move a folder into `destination_folder` and rewrite all child SQLite rows."""
        src_dir, old_norm = self._resolve_folder_dir(path)
        if old_norm == "/":
            raise PathSafetyError("Cannot move storage root '/'")
        if not src_dir.is_dir():
            raise FileOpNotFoundError(f"Folder does not exist: {old_norm}")

        dst_parent, dst_parent_norm = self._resolve_folder_dir(destination_folder)
        if not dst_parent.is_dir():
            raise FileOpNotFoundError(
                f"Destination folder does not exist: {dst_parent_norm}"
            )

        dst_dir = safe_resolve_path(
            dst_parent / src_dir.name,
            self._settings.storage_dir,
        )
        if dst_dir == src_dir or dst_dir.is_relative_to(src_dir):
            raise PathSafetyError(
                f"Cannot move folder {old_norm} into itself or one of its subfolders"
            )

        if self._job_repo.get_active_job_in_folder(old_norm) is not None:
            raise FileOpConflictError(
                f"Cannot move {old_norm} while a job is queued or running inside it"
            )

        new_norm = f"/{to_relative_storage_path(dst_dir, self._settings.storage_dir)}"
        _safe_move_dir_no_overwrite(src_dir, dst_dir, action=f"move folder {old_norm}")
        updated = self._media_repo.update_folder_prefix(old_norm, new_norm)
        self._emit(action="updated", filepath=new_norm.lstrip("/"))
        return FolderOpResponse(
            old_path=old_norm,
            new_path=new_norm,
            affected_files=len(updated),
        )

    def delete_folder(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> FolderOpResponse:
        """Delete a folder on disk and remove descendant SQLite rows (respects :ro & job locks)."""
        src_dir, old_norm = self._resolve_folder_dir(path)
        if old_norm == "/":
            raise PathSafetyError("Cannot delete storage root '/'")
        if not src_dir.is_dir():
            raise FileOpNotFoundError(f"Folder does not exist: {old_norm}")

        if self._job_repo.get_active_job_in_folder(old_norm) is not None:
            raise FileOpConflictError(
                f"Cannot delete {old_norm} while a job is queued or running inside it"
            )

        non_empty = _dir_is_non_empty(src_dir)
        if non_empty and not recursive:
            raise FileOpConflictError(
                f"Folder {old_norm} is not empty; confirm recursive delete to remove its contents"
            )

        try:
            if non_empty:
                shutil.rmtree(src_dir)
            else:
                src_dir.rmdir()
        except OSError as exc:
            _raise_if_permission_os_error(exc, f"delete folder {old_norm}")
            raise FileOpError(f"Failed to delete folder {old_norm}: {exc}") from exc

        deleted_rows = self._media_repo.delete_by_folder_prefix(old_norm)
        self._emit(action="deleted", filepath=old_norm.lstrip("/"))
        return FolderOpResponse(
            old_path=old_norm,
            new_path=None,
            affected_files=len(deleted_rows),
        )

    # ------------------------------------------------------------------
    # Non-Indexed (Other) File Operations
    # ------------------------------------------------------------------

    def _resolve_other_file_path(self, rel_filepath: str) -> tuple[Path, str]:
        """Resolve a non-indexed file path safely inside storage_dir."""
        cleaned = (rel_filepath or "").strip().lstrip("/")
        if not cleaned:
            raise PathSafetyError("Filepath must not be empty")
        src_path = safe_resolve_path(cleaned, self._settings.storage_dir)
        if not src_path.is_file():
            raise FileOpNotFoundError(f"File not found on disk: {cleaned}")
        if is_mkvforge_temp_file(src_path) or src_path.name.startswith("."):
            raise PathSafetyError("Cannot modify hidden or temporary system files")
        if is_supported_media_file(src_path):
            raise FileOpConflictError(
                "Indexed media files (.mkv/.mp4) must use standard media file endpoints"
            )
        return src_path, to_relative_storage_path(src_path, self._settings.storage_dir)

    def rename_other_file(self, filepath: str, new_name: str) -> OtherFileOpResponse:
        """Rename a non-media file; if renamed to .mkv/.mp4, insert a scanning placeholder."""
        src_path, old_rel = self._resolve_other_file_path(filepath)
        cleaned_name = _validate_single_name(new_name, kind="File")
        dst_path = safe_resolve_path(
            src_path.parent / cleaned_name,
            self._settings.storage_dir,
        )
        if is_mkvforge_temp_file(dst_path):
            raise PathSafetyError("Filename cannot contain reserved '.mkvforge.' marker")
        if dst_path == src_path:
            return OtherFileOpResponse(
                old_filepath=old_rel,
                new_filepath=old_rel,
                promoted_to_media=False,
            )

        _safe_move_file_no_overwrite(
            src_path, dst_path, action=f"rename {src_path.name}"
        )
        new_rel = to_relative_storage_path(dst_path, self._settings.storage_dir)
        promoted = is_supported_media_file(dst_path)
        if promoted:
            st = dst_path.stat()
            file_id, _ = self._media_repo.upsert_scanning_placeholder(
                filepath=new_rel,
                folder=extract_folder(new_rel),
                filename=dst_path.name,
                size_bytes=int(st.st_size),
                mtime=float(st.st_mtime),
            )
            self._emit(action="added", filepath=new_rel, file_id=file_id)
        else:
            self._emit(action="updated", filepath=new_rel)
        return OtherFileOpResponse(
            old_filepath=old_rel,
            new_filepath=new_rel,
            promoted_to_media=promoted,
        )

    def move_other_file(
        self, filepath: str, destination_folder: str
    ) -> OtherFileOpResponse:
        """Move a non-media file into `destination_folder` with overwrite protection."""
        src_path, old_rel = self._resolve_other_file_path(filepath)
        dst_dir, normalized_dst = self._resolve_folder_dir(destination_folder)
        if not dst_dir.is_dir():
            raise FileOpNotFoundError(
                f"Destination folder does not exist: {normalized_dst}"
            )
        dst_path = safe_resolve_path(
            dst_dir / src_path.name,
            self._settings.storage_dir,
        )
        if dst_path == src_path:
            return OtherFileOpResponse(
                old_filepath=old_rel,
                new_filepath=old_rel,
                promoted_to_media=False,
            )

        _safe_move_file_no_overwrite(src_path, dst_path, action=f"move {src_path.name}")
        new_rel = to_relative_storage_path(dst_path, self._settings.storage_dir)
        self._emit(action="updated", filepath=new_rel)
        return OtherFileOpResponse(
            old_filepath=old_rel,
            new_filepath=new_rel,
            promoted_to_media=False,
        )

    def delete_other_file(self, filepath: str) -> OtherFileOpResponse:
        """Delete a non-media file from disk (respects :ro volumes)."""
        src_path, old_rel = self._resolve_other_file_path(filepath)
        try:
            src_path.unlink()
        except OSError as exc:
            _raise_if_permission_os_error(exc, f"delete {src_path.name}")
            raise FileOpError(f"Failed to delete {src_path.name}: {exc}") from exc
        self._emit(action="deleted", filepath=old_rel)
        return OtherFileOpResponse(
            old_filepath=old_rel,
            new_filepath=None,
            promoted_to_media=False,
        )
