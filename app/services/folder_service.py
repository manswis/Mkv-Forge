"""Safe lazy directory browsing and subfolder creation inside STORAGE_DIR."""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import AppConstants
from app.utils.paths import (
    PathSafetyError,
    is_supported_media_file,
    safe_resolve_path,
    to_relative_storage_path,
)


class FolderNotFoundError(FileNotFoundError):
    """Raised when a requested storage directory does not exist."""


class FolderAlreadyExistsError(FileExistsError):
    """Raised when attempting to create a subfolder that already exists."""


class FolderPermissionError(PermissionError):
    """Raised when creating a folder fails due to :ro mount or permissions."""


@dataclass(frozen=True, slots=True)
class DirectoryChild:
    """One immediate child directory inside a storage folder."""

    name: str
    path: str
    has_children: bool


@dataclass(frozen=True, slots=True)
class DirectoryMediaFile:
    """One immediate watchable media file discovered in a single `os.scandir` pass."""

    rel_filepath: str
    size_bytes: int
    mtime: float


@dataclass(frozen=True, slots=True)
class DirectoryExternalTrackFile:
    """One immediate importable subtitle or audio file inside a storage folder."""

    name: str
    path: str
    size_bytes: int
    kind: str


@dataclass(frozen=True, slots=True)
class DirectoryOtherFile:
    """One immediate non-indexed file discovered in the same `os.scandir` pass."""

    name: str
    rel_filepath: str
    folder: str
    size_bytes: int
    mtime: float
    ext: str


@dataclass(frozen=True, slots=True)
class FolderDiskSnapshot:
    """Single-pass snapshot of immediate subfolders, media files, and optional other files."""

    path: str
    subfolders: list[DirectoryChild]
    media_files: list[DirectoryMediaFile]
    other_files: list[DirectoryOtherFile] = ()  # type: ignore[assignment]


def normalize_visible_extensions_csv(raw_value: str | None) -> str:
    """Normalize comma-separated extensions (lowercase, trimmed, leading dot) always keeping .mkv, .mp4."""
    seen: list[str] = [".mkv", ".mp4"]
    seen_set: set[str] = set(seen)
    if raw_value:
        for token in raw_value.replace(";", ",").split(","):
            cleaned = token.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            # Strip stray whitespace/path separators
            if len(cleaned) <= 1 or "/" in cleaned or "\\" in cleaned or " " in cleaned:
                continue
            if cleaned not in seen_set:
                seen_set.add(cleaned)
                seen.append(cleaned)
    return ", ".join(seen)


def parse_visible_extensions_set(raw_value: str | None) -> frozenset[str]:
    """Return normalized frozenset of visible extensions (always includes .mkv and .mp4)."""
    csv = normalize_visible_extensions_csv(raw_value)
    return frozenset(part.strip() for part in csv.split(",") if part.strip())


def normalize_storage_folder_path(raw_path: str | None) -> str:
    """Normalize a display folder path (e.g. 'movies/' -> '/movies', '' -> '/')."""
    if raw_path is None:
        return "/"
    cleaned = raw_path.strip()
    if not cleaned or cleaned == "/":
        return "/"
    if "\\" in cleaned or "\x00" in cleaned:
        raise PathSafetyError(f"Invalid folder path: {raw_path!r}")

    rel = cleaned.lstrip("/")
    parts = PurePosixPath(rel).parts
    if any(part in ("..", ".") for part in parts):
        raise PathSafetyError(f"Folder path must not contain relative segments: {raw_path!r}")
    return f"/{'/'.join(parts)}"


def _resolve_folder_dir(display_path: str, storage_root: Path) -> tuple[Path, str]:
    """Resolve a normalized display folder path strictly inside `storage_root`."""
    normalized = normalize_storage_folder_path(display_path)
    if normalized == "/":
        resolved = storage_root.resolve(strict=False)
        return resolved, "/"
    rel = normalized.lstrip("/")
    resolved = safe_resolve_path(rel, storage_root, allow_root=True)
    return resolved, normalized


def _is_visible_safe_subdir(entry: os.DirEntry[str], resolved_root: Path) -> bool:
    """Return True if `entry` is a non-hidden directory strictly inside `resolved_root`."""
    if not entry.name or entry.name.startswith("."):
        return False
    try:
        if not entry.is_dir(follow_symlinks=True):
            return False
        resolved = Path(entry.path).resolve(strict=False)
        return resolved.is_relative_to(resolved_root)
    except OSError:
        return False


def _dir_has_visible_subdirs(directory: Path, resolved_root: Path) -> bool:
    """Check whether `directory` contains at least one visible safe subdirectory."""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if _is_visible_safe_subdir(entry, resolved_root):
                    return True
    except OSError:
        return False
    return False


def _is_watchable_safe_media_entry(
    entry: os.DirEntry[str], resolved_root: Path
) -> tuple[Path, os.stat_result] | None:
    """Return `(resolved_path, stat)` if `entry` is a safe supported media file inside root."""
    name = entry.name
    if not name or name.startswith("."):
        return None
    lower_name = name.lower()
    for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES:
        if lower_name.endswith(suffix):
            return None
    entry_path = Path(entry.path)
    if not is_supported_media_file(entry_path):
        return None
    try:
        if not entry.is_file(follow_symlinks=True):
            return None
        resolved = entry_path.resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            return None
        return resolved, entry.stat(follow_symlinks=True)
    except OSError:
        return None


def _is_ignored_junk_filename(
    name: str, *, skip_partial_suffixes: bool = True
) -> bool:
    """Return True for hidden files, AppleDouble, .DS_Store, MKV Forge temp, and optionally partial downloads."""
    if not name or name.startswith("."):
        return True
    lower_name = name.lower()
    if lower_name == ".ds_store" or lower_name.startswith(AppConstants.MACOS_APPLEDOUBLE_PREFIX):
        return True
    if AppConstants.TEMP_MARKER in lower_name:
        return True
    if skip_partial_suffixes:
        return any(
            lower_name.endswith(suffix)
            for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES
        )
    return False


def _is_visible_safe_other_file(
    entry: os.DirEntry[str],
    resolved_root: Path,
    *,
    show_media_files_only: bool,
    allowed_extensions: frozenset[str],
) -> tuple[Path, os.stat_result, str] | None:
    """Return `(resolved_path, stat, ext)` if `entry` is a safe visible non-media file.

    - When `show_media_files_only` is True: hides partial download suffixes and filters
      strictly by `allowed_extensions`.
    - When `show_media_files_only` is False ("Show All Files"): shows all non-hidden,
      non-mkvforge files on disk (including `.uploading` / `.part` / `.tmp`).
    """
    name = entry.name
    if _is_ignored_junk_filename(name, skip_partial_suffixes=show_media_files_only):
        return None
    entry_path = Path(entry.path)
    if is_supported_media_file(entry_path):
        return None
    ext = entry_path.suffix.lower()
    if show_media_files_only and (not ext or ext not in allowed_extensions):
        return None
    try:
        if not entry.is_file(follow_symlinks=True):
            return None
        resolved = entry_path.resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            return None
        return resolved, entry.stat(follow_symlinks=True), ext
    except OSError:
        return None


def scan_storage_folder_snapshot(
    raw_path: str | None,
    storage_root: Path,
    *,
    check_grand_children: bool = True,
    show_media_files_only: bool = True,
    visible_extensions: frozenset[str] = AppConstants.SUPPORTED_MEDIA_EXTENSIONS,
) -> FolderDiskSnapshot:
    """Collect immediate subfolders, media files, and matching other files in ONE `os.scandir` pass.

    When `check_grand_children` is False, skips scanning inside each child directory
    (ideal for fast Library folder listings on spinning HDDs).
    """
    resolved_root = storage_root.resolve(strict=False)
    target_dir, normalized = _resolve_folder_dir(raw_path or "/", resolved_root)
    if not target_dir.is_dir():
        raise FolderNotFoundError(f"Folder does not exist: {normalized}")

    include_other_files = (not show_media_files_only) or bool(
        visible_extensions - AppConstants.SUPPORTED_MEDIA_EXTENSIONS
    )
    children: list[DirectoryChild] = []
    media_files: list[DirectoryMediaFile] = []
    other_files: list[DirectoryOtherFile] = []
    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                if _is_visible_safe_subdir(entry, resolved_root):
                    child_display = (
                        f"/{entry.name}"
                        if normalized == "/"
                        else f"{normalized}/{entry.name}"
                    )
                    children.append(
                        DirectoryChild(
                            name=entry.name,
                            path=child_display,
                            has_children=(
                                _dir_has_visible_subdirs(
                                    Path(entry.path), resolved_root
                                )
                                if check_grand_children
                                else False
                            ),
                        )
                    )
                    continue

                media_match = _is_watchable_safe_media_entry(entry, resolved_root)
                if media_match is not None:
                    resolved_file, st = media_match
                    try:
                        rel = to_relative_storage_path(resolved_file, resolved_root)
                    except PathSafetyError:
                        continue
                    media_files.append(
                        DirectoryMediaFile(
                            rel_filepath=rel,
                            size_bytes=int(st.st_size),
                            mtime=float(st.st_mtime),
                        )
                    )
                    continue

                if include_other_files:
                    other_match = _is_visible_safe_other_file(
                        entry,
                        resolved_root,
                        show_media_files_only=show_media_files_only,
                        allowed_extensions=visible_extensions,
                    )
                    if other_match is not None:
                        resolved_other, st_other, ext_other = other_match
                        try:
                            rel_other = to_relative_storage_path(
                                resolved_other, resolved_root
                            )
                        except PathSafetyError:
                            continue
                        other_files.append(
                            DirectoryOtherFile(
                                name=entry.name,
                                rel_filepath=rel_other,
                                folder=normalized,
                                size_bytes=int(st_other.st_size),
                                mtime=float(st_other.st_mtime),
                                ext=ext_other,
                            )
                        )
    except OSError as exc:
        raise FolderNotFoundError(f"Cannot read folder {normalized}: {exc}") from exc

    children.sort(key=lambda item: item.name.lower())
    other_files.sort(key=lambda item: item.name.lower())
    return FolderDiskSnapshot(
        path=normalized,
        subfolders=children,
        media_files=media_files,
        other_files=other_files,
    )


def list_storage_subfolders(
    raw_path: str | None,
    storage_root: Path,
) -> tuple[str, list[DirectoryChild]]:
    """List immediate non-hidden subdirectories of `raw_path` inside `storage_root`."""
    snapshot = scan_storage_folder_snapshot(raw_path, storage_root)
    return snapshot.path, snapshot.subfolders


def list_storage_folder_and_tracks(
    raw_path: str | None,
    storage_root: Path,
    *,
    kind: str | None = None,
) -> tuple[str, list[DirectoryChild], list[DirectoryExternalTrackFile]]:
    """List immediate subdirectories AND importable external subtitle/audio files (read-only safe)."""
    resolved_root = storage_root.resolve(strict=False)
    target_dir, normalized = _resolve_folder_dir(raw_path or "/", resolved_root)
    if not target_dir.is_dir():
        raise FolderNotFoundError(f"Folder does not exist: {normalized}")

    allowed_sub = AppConstants.EXTERNAL_SUBTITLE_EXTENSIONS
    allowed_aud = AppConstants.EXTERNAL_AUDIO_EXTENSIONS

    children: list[DirectoryChild] = []
    track_files: list[DirectoryExternalTrackFile] = []
    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                if _is_visible_safe_subdir(entry, resolved_root):
                    child_display = (
                        f"/{entry.name}"
                        if normalized == "/"
                        else f"{normalized}/{entry.name}"
                    )
                    children.append(
                        DirectoryChild(
                            name=entry.name,
                            path=child_display,
                            has_children=_dir_has_visible_subdirs(
                                Path(entry.path), resolved_root
                            ),
                        )
                    )
                    continue

                name = entry.name
                if not name or name.startswith("."):
                    continue
                lower_name = name.lower()
                if any(
                    lower_name.endswith(suffix)
                    for suffix in AppConstants.IGNORED_PARTIAL_SUFFIXES
                ):
                    continue
                ext = Path(name).suffix.lower()
                file_kind: str | None = None
                if ext in allowed_sub and (
                    kind is None or kind == AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES
                ):
                    file_kind = AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES
                elif ext in allowed_aud and (
                    kind is None or kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO
                ):
                    file_kind = AppConstants.EXTERNAL_TRACK_KIND_AUDIO

                if file_kind is None:
                    continue

                try:
                    if not entry.is_file(follow_symlinks=True):
                        continue
                    resolved_file = Path(entry.path).resolve(strict=False)
                    if not resolved_file.is_relative_to(resolved_root):
                        continue
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue

                rel_display = (
                    f"/{name}" if normalized == "/" else f"{normalized}/{name}"
                )
                track_files.append(
                    DirectoryExternalTrackFile(
                        name=name,
                        path=rel_display,
                        size_bytes=int(st.st_size),
                        kind=file_kind,
                    )
                )
    except OSError as exc:
        raise FolderNotFoundError(f"Cannot read folder {normalized}: {exc}") from exc

    children.sort(key=lambda item: item.name.lower())
    track_files.sort(key=lambda item: item.name.lower())
    return normalized, children, track_files


def create_storage_subfolder(
    parent_path: str | None,
    folder_name: str,
    storage_root: Path,
) -> DirectoryChild:
    """Create a single new subfolder directly inside `parent_path` within `storage_root`."""
    cleaned_name = folder_name.strip()
    if not cleaned_name:
        raise PathSafetyError("Folder name must not be empty")
    if (
        "/" in cleaned_name
        or "\\" in cleaned_name
        or "\x00" in cleaned_name
        or cleaned_name in (".", "..")
        or cleaned_name.startswith(".")
    ):
        raise PathSafetyError(f"Invalid folder name: {folder_name!r}")

    resolved_root = storage_root.resolve(strict=False)
    parent_dir, normalized_parent = _resolve_folder_dir(
        parent_path or "/", resolved_root
    )
    if not parent_dir.is_dir():
        raise FolderNotFoundError(f"Parent folder does not exist: {normalized_parent}")

    child_display = (
        f"/{cleaned_name}"
        if normalized_parent == "/"
        else f"{normalized_parent}/{cleaned_name}"
    )
    target_dir = safe_resolve_path(child_display.lstrip("/"), resolved_root)
    if target_dir.exists():
        raise FolderAlreadyExistsError(f"Folder already exists: {child_display}")

    try:
        target_dir.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (
            errno.EROFS,
            errno.EACCES,
            errno.EPERM,
        ):
            msg = (
                "Permission denied: storage volume is read-only"
                if exc.errno == errno.EROFS
                else f"Permission denied: cannot create folder {child_display}"
            )
            raise FolderPermissionError(msg) from exc
        raise
    return DirectoryChild(
        name=cleaned_name,
        path=child_display,
        has_children=False,
    )
