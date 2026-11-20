"""Path safety guards, media filtering, and atomic output naming helpers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.config import AppConstants


class PathSafetyError(ValueError):
    """Raised when a path attempts to escape the configured storage root."""


def safe_resolve_path(
    target_path: str | Path,
    storage_root: Path,
    *,
    allow_root: bool = False,
) -> Path:
    """Resolve `target_path` and verify it strictly stays inside `storage_root`.

    Blocks `../` traversal, absolute paths outside `storage_root`, null bytes,
    and symlink escapes pointing outside `storage_root`.
    """
    raw_str = str(target_path)
    if not raw_str or raw_str.strip() == "":
        raise PathSafetyError("Path must not be empty")
    if "\x00" in raw_str:
        raise PathSafetyError("Path contains null byte")

    resolved_root = storage_root.resolve(strict=False)
    candidate_input = Path(raw_str)

    if candidate_input.is_absolute():
        resolved_candidate = candidate_input.resolve(strict=False)
    else:
        resolved_candidate = (resolved_root / candidate_input).resolve(strict=False)

    if not resolved_candidate.is_relative_to(resolved_root):
        raise PathSafetyError(
            f"Path {raw_str!r} resolves outside storage root {resolved_root}"
        )

    if not allow_root and resolved_candidate == resolved_root:
        raise PathSafetyError("Path must reference a file inside storage root")

    return resolved_candidate


def to_relative_storage_path(file_path: Path, storage_root: Path) -> str:
    """Return normalized POSIX relative filepath inside `storage_root`."""
    resolved_root = storage_root.resolve(strict=False)
    resolved_file = safe_resolve_path(file_path, resolved_root)
    return resolved_file.relative_to(resolved_root).as_posix()


def extract_folder(rel_filepath: str) -> str:
    """Return display folder path (e.g., '/Movies/English' or '/')."""
    parent = PurePosixPath(rel_filepath).parent.as_posix()
    if parent in ("", "."):
        return "/"
    return f"/{parent.lstrip('/')}"


def is_mkvforge_temp_file(path: Path) -> bool:
    """Return True if path is a temporary artifact created by MKV Forge."""
    return AppConstants.TEMP_MARKER in path.name.lower()


def is_supported_media_file(path: Path) -> bool:
    """Return True if path is an indexable `.mkv` or `.mp4` media file."""
    name = path.name
    if not name or name.startswith(AppConstants.MACOS_APPLEDOUBLE_PREFIX):
        return False
    if is_mkvforge_temp_file(path):
        return False
    return path.suffix.lower() in AppConstants.SUPPORTED_MEDIA_EXTENSIONS


def build_temp_remux_path(source_path: Path) -> Path:
    """Return same-directory temp output path for Stage 2 mkvmerge remux.

    For `.mp4` inputs, writes `<stem>.mkv.mkvforge.tmp` so the output clearly
    tracks the `.mkv` target container.
    """
    if source_path.suffix.lower() == AppConstants.EXT_MP4:
        filename = f"{source_path.stem}{AppConstants.EXT_MKV}{AppConstants.TEMP_REMUX_SUFFIX}"
    else:
        filename = f"{source_path.name}{AppConstants.TEMP_REMUX_SUFFIX}"
    return source_path.parent / filename


def build_temp_audio_path(source_path: Path, codec_ext: str) -> Path:
    """Return same-directory temp audio path for Stage 1 FFmpeg transcode."""
    cleaned = codec_ext.strip().lstrip(".").lower()
    if not cleaned or not cleaned.isalnum():
        raise ValueError(f"Invalid audio codec extension: {codec_ext!r}")
    return source_path.parent / f"{source_path.name}{AppConstants.TEMP_MARKER}{cleaned}"


def resolve_mp4_replace_target(source_path: Path) -> Path:
    """Return target `<stem>.mkv` path when replacing an `.mp4` source file."""
    if source_path.suffix.lower() != AppConstants.EXT_MP4:
        raise ValueError(f"Expected .mp4 source path, got {source_path.name!r}")
    return source_path.parent / f"{source_path.stem}{AppConstants.EXT_MKV}"


def ensure_mp4_replace_target_available(source_path: Path) -> Path:
    """Return target `<stem>.mkv` path or raise FileExistsError if it already exists."""
    target = resolve_mp4_replace_target(source_path)
    if target.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing MKV file: {target.name}"
        )
    return target


def resolve_keep_both_path(source_path: Path) -> Path:
    """Return non-colliding Keep-Both output path (`<stem>.remux[.N].mkv`)."""
    first_candidate = (
        source_path.parent
        / f"{source_path.stem}{AppConstants.KEEP_BOTH_INFIX}{AppConstants.EXT_MKV}"
    )
    if not first_candidate.exists():
        return first_candidate

    for counter in range(2, AppConstants.MAX_KEEP_BOTH_COLLISIONS + 2):
        candidate = (
            source_path.parent
            / f"{source_path.stem}{AppConstants.KEEP_BOTH_INFIX}.{counter}{AppConstants.EXT_MKV}"
        )
        if not candidate.exists():
            return candidate

    raise FileExistsError(
        f"Exceeded {AppConstants.MAX_KEEP_BOTH_COLLISIONS} Keep-Both remux files for {source_path.name}"
    )


def resolve_save_as_target(
    raw_output_path: str,
    storage_root: Path,
    *,
    source_path: Path | None = None,
) -> Path:
    """Resolve and validate a Save-As `.mkv` destination strictly inside `storage_root`.

    Rejects path traversal, absolute paths outside `storage_root`, non-`.mkv`
    filenames, MKV Forge temp filenames, hidden AppleDouble prefixes, and
    destinations identical to `source_path`.
    """
    cleaned = raw_output_path.strip()
    if not cleaned:
        raise PathSafetyError("Save As output path must not be empty")
    if "\\" in cleaned:
        raise PathSafetyError("Save As output path must use forward slashes")

    # Treat leading slash as storage-root-relative only if no traversal segments
    normalized = cleaned.lstrip("/")
    if not normalized:
        raise PathSafetyError("Save As output path must include a filename")

    parts = PurePosixPath(normalized).parts
    if any(part in ("..", ".") for part in parts):
        raise PathSafetyError(
            f"Save As output path must not contain relative segments: {raw_output_path!r}"
        )

    resolved_target = safe_resolve_path(normalized, storage_root)
    if resolved_target.suffix.lower() != AppConstants.EXT_MKV:
        raise PathSafetyError(
            f"Save As destination must end with {AppConstants.EXT_MKV}: {resolved_target.name!r}"
        )
    if is_mkvforge_temp_file(resolved_target):
        raise PathSafetyError(
            f"Save As destination must not use reserved temp marker: {resolved_target.name!r}"
        )
    if resolved_target.name.startswith(AppConstants.MACOS_APPLEDOUBLE_PREFIX):
        raise PathSafetyError(
            f"Save As destination filename is invalid: {resolved_target.name!r}"
        )
    if source_path is not None and resolved_target == source_path.resolve(strict=False):
        raise PathSafetyError(
            "Save As destination must differ from the original file; use Replace Original instead"
        )

    return resolved_target


def ensure_save_as_target_available(
    raw_output_path: str,
    storage_root: Path,
    *,
    source_path: Path | None = None,
) -> Path:
    """Return validated Save-As target path or raise FileExistsError if it already exists."""
    target = resolve_save_as_target(
        raw_output_path,
        storage_root,
        source_path=source_path,
    )
    if target.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing file at destination: {target.name}"
        )
    return target
