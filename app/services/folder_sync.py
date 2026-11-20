"""Single-folder disk reconciliation for instant ghost pruning and background probing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import AppConstants
from app.db.repositories import MediaFileRepository
from app.services.folder_service import (
    DirectoryChild,
    DirectoryOtherFile,
    FolderNotFoundError,
    normalize_storage_folder_path,
    scan_storage_folder_snapshot,
)
from app.utils.paths import PathSafetyError


@dataclass(frozen=True, slots=True)
class FolderReconcileResult:
    """Result of reconciling one storage folder against SQLite without extra disk walks."""

    folder_exists: bool
    subfolders: list[DirectoryChild]
    pruned_count: int
    probe_candidates: list[str]
    other_files: list[DirectoryOtherFile] = ()  # type: ignore[assignment]


def reconcile_folder_snapshot(
    raw_folder: str,
    *,
    storage_dir: Path,
    media_repo: MediaFileRepository,
    check_grand_children: bool = True,
    show_media_files_only: bool = True,
    visible_extensions: frozenset[str] = AppConstants.SUPPORTED_MEDIA_EXTENSIONS,
) -> FolderReconcileResult:
    """Reconcile immediate files/subfolders of `raw_folder` in ONE `os.scandir` pass.

    - Never mutates SQLite if `storage_dir` itself is unmounted/missing.
    - Prunes vanished rows immediately so refreshed folders never show ghost files.
    - Returns new or modified filepaths for non-blocking background probing.
    """
    try:
        normalized = normalize_storage_folder_path(raw_folder)
    except PathSafetyError:
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    if not storage_dir.is_dir():
        return FolderReconcileResult(
            folder_exists=True,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    try:
        snapshot = scan_storage_folder_snapshot(
            normalized,
            storage_dir,
            check_grand_children=check_grand_children,
            show_media_files_only=show_media_files_only,
            visible_extensions=visible_extensions,
        )
    except FolderNotFoundError:
        pruned = 0
        if normalized != "/":
            pruned = len(media_repo.delete_by_folder_prefix(normalized))
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=pruned,
            probe_candidates=[],
        )
    except PathSafetyError:
        return FolderReconcileResult(
            folder_exists=False,
            subfolders=[],
            pruned_count=0,
            probe_candidates=[],
        )

    db_states = media_repo.list_direct_file_states(snapshot.path)
    disk_map = {entry.rel_filepath: entry for entry in snapshot.media_files}

    vanished_paths = [path for path in db_states if path not in disk_map]
    pruned_count = (
        media_repo.delete_by_filepaths(vanished_paths) if vanished_paths else 0
    )

    probe_candidates: list[str] = []
    for rel_path, entry in disk_map.items():
        if entry.size_bytes <= 0:
            continue
        existing = db_states.get(rel_path)
        if existing is None:
            # Phase A: Insert instant 'scanning' placeholder so the file appears immediately
            media_repo.upsert_scanning_placeholder(
                filepath=rel_path,
                folder=snapshot.path,
                filename=Path(rel_path).name,
                size_bytes=entry.size_bytes,
                mtime=entry.mtime,
            )
            probe_candidates.append(rel_path)
            continue
        _file_id, db_size, db_mtime, db_status = existing
        if (
            db_status == AppConstants.FILE_SCAN_STATUS_SCANNING
            or db_size != entry.size_bytes
            or abs(db_mtime - entry.mtime) > AppConstants.MTIME_TOLERANCE_SEC
        ):
            probe_candidates.append(rel_path)

    return FolderReconcileResult(
        folder_exists=True,
        subfolders=snapshot.subfolders,
        pruned_count=pruned_count,
        probe_candidates=probe_candidates,
        other_files=snapshot.other_files,
    )
