"""Stage 2 mkvmerge runner, exit-code handling, atomic swap, and full pipeline executor."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import AppConstants
from app.db.repositories import MediaFileRepository
from app.models.schemas import ExternalTrackConfig, JobConfig, ParsedTrackInfo
from app.services.ffmpeg_runner import (
    LogCallback,
    ProcessCallback,
    ProgressCallback,
    run_ffmpeg_transcode,
)
from app.services.folder_sync import FolderReconcileCache
from app.services.mkv_builder import build_mkvmerge_command
from app.services.mkvpropedit_runner import (
    can_use_instant_header_edit,
    run_mkvpropedit_edit,
)
from app.services.preflight import TargetFileCollisionError, run_preflight_checks
from app.services.processing_policy import (
    ProcessingRuntimePolicy,
    build_nice_preexec_fn,
    wrap_command_with_io_priority,
)
from app.services.scanner import LibraryScanner
from app.services.upload_service import (
    UploadService,
    extract_upload_ids_from_tracks,
)
from app.utils.paths import (
    build_temp_audio_path,
    build_temp_remux_path,
    ensure_mp4_replace_target_available,
    ensure_save_as_target_available,
    extract_folder,
    resolve_keep_both_path,
    to_relative_storage_path,
)

logger = logging.getLogger(__name__)

# Matches both `--gui-mode` (`#GUI#progress 45%`) and standard (`Progress: 45%`)
_PROGRESS_RE = re.compile(r"(?:#GUI#progress|Progress:)\s*(\d+)%")


class MkvMergeError(RuntimeError):
    """Raised when Stage 2 mkvmerge exits with an error code (>= 2)."""


class JobCancelledError(RuntimeError):
    """Raised when a running job is cancelled before or between pipeline stages."""


CancelCheckCallback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class MkvRemuxResult:
    """Raw output from Stage 2 mkvmerge execution before atomic swap."""

    output_tmp_path: Path
    exit_code: int
    warnings: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PipelineExecutionResult:
    """End-to-end result of running the two-stage audio + remux pipeline."""

    output_path: Path
    output_rel_filepath: str
    mkvmerge_exit_code: int
    warnings: list[str] = field(default_factory=list)
    replaced_mp4_deleted: bool = False


StageCallback = Callable[[str, str], None]


def parse_mkvmerge_progress_line(line: str) -> int | None:
    """Extract integer percentage (0..100) from mkvmerge stdout progress lines."""
    match = _PROGRESS_RE.search(line)
    if match is None:
        return None
    return max(0, min(100, int(match.group(1))))


def _is_warning_line(line: str) -> bool:
    """Return True if an mkvmerge log line represents a warning."""
    stripped = line.strip()
    return stripped.startswith("#GUI#warning") or stripped.startswith("Warning:")


def _safe_unlink(path: Path | None) -> None:
    """Best-effort temp file removal that never masks upstream exceptions."""
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("Failed to clean up temp file %s: %s", path, exc)


def _move_without_overwrite(src: Path, dst: Path) -> None:
    """Place `src` at `dst` without overwriting an existing file.

    Uses atomic hardlink + unlink on POSIX filesystems (raises FileExistsError
    atomically if `dst` already exists). Falls back cleanly on filesystems
    without hardlink support (e.g., exFAT/FAT32 USB drives).
    """
    try:
        os.link(src, dst)
        src.unlink()
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
            raise

    if dst.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {dst.name}")
    os.replace(src, dst)


async def run_mkvmerge_remux(
    source_path: Path,
    output_tmp_path: Path,
    config: JobConfig,
    *,
    temp_audio_path: Path | None = None,
    resolved_external_tracks: list[tuple[ExternalTrackConfig, Path]] | None = None,
    nice_increment: int = 0,
    use_ionice_idle: bool = False,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    on_process_started: ProcessCallback | None = None,
) -> MkvRemuxResult:
    """Execute Stage 2 `mkvmerge` asynchronously and enforce exit code semantics.

    Exit code 0 = success, 1 = success with warnings, >= 2 = failure.
    """
    argv = build_mkvmerge_command(
        source_path,
        output_tmp_path,
        config,
        temp_audio_path=temp_audio_path,
        resolved_external_tracks=resolved_external_tracks,
    )
    launch_argv = wrap_command_with_io_priority(
        argv, use_ionice_idle=use_ionice_idle
    )
    preexec_fn = build_nice_preexec_fn(nice_increment)
    exec_kwargs: dict[str, object] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if preexec_fn is not None:
        exec_kwargs["preexec_fn"] = preexec_fn

    try:
        proc = await asyncio.create_subprocess_exec(
            *launch_argv,
            **exec_kwargs,  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        raise MkvMergeError("mkvmerge executable not found in PATH") from exc
    except OSError as exc:
        raise MkvMergeError(f"Failed to launch mkvmerge: {exc}") from exc

    if on_process_started is not None:
        on_process_started(proc)

    warnings: list[str] = []
    log_lines: list[str] = []

    async def _read_stream(stream: asyncio.StreamReader) -> None:
        while True:
            raw_line = await stream.readline()
            if not raw_line:
                break
            line = raw_line.decode(AppConstants.UTF8_ENCODING, errors="replace").rstrip()
            if not line:
                continue
            log_lines.append(line)
            if _is_warning_line(line):
                warnings.append(line)
            if on_log is not None:
                on_log(line)

            percent = parse_mkvmerge_progress_line(line)
            if percent is not None and on_progress is not None:
                on_progress(percent, None)

    assert proc.stdout is not None and proc.stderr is not None
    await asyncio.gather(_read_stream(proc.stdout), _read_stream(proc.stderr))
    exit_code = await proc.wait()

    if exit_code not in (
        AppConstants.MKVMERGE_EXIT_OK,
        AppConstants.MKVMERGE_EXIT_WARNINGS,
    ):
        tail = " | ".join(log_lines[-3:])[: AppConstants.MAX_ERROR_EXCERPT_LEN]
        raise MkvMergeError(
            f"mkvmerge failed with exit code {exit_code}"
            + (f": {tail}" if tail else "")
        )

    return MkvRemuxResult(
        output_tmp_path=output_tmp_path,
        exit_code=exit_code,
        warnings=warnings,
        log_lines=log_lines,
    )


def _commit_remux_files_sync(
    source_path: Path,
    output_tmp_path: Path,
    *,
    output_mode: str,
    storage_root: Path,
    output_path: str | None = None,
    delete_original: bool = False,
) -> tuple[Path, str, str, bool, str | None]:
    """Execute blocking filesystem commit/swap steps inside a worker thread.

    Returns `(final_path, final_rel, source_rel, replaced_deleted, deleted_old_rel)`.
    """
    is_mp4 = source_path.suffix.lower() == AppConstants.EXT_MP4
    replaced_mp4_deleted = False
    deleted_old_rel: str | None = None
    source_rel = to_relative_storage_path(source_path, storage_root)

    if output_mode == AppConstants.OUTPUT_MODE_SAVE_AS:
        if not output_path:
            raise MkvMergeError("output_path is required for save_as output mode")
        try:
            final_path = ensure_save_as_target_available(
                output_path,
                storage_root,
                source_path=source_path,
            )
            final_path.parent.mkdir(parents=True, exist_ok=True)
            _move_without_overwrite(output_tmp_path, final_path)
        except FileExistsError as exc:
            raise TargetFileCollisionError(str(exc)) from exc

        if delete_original:
            if not final_path.is_file():
                raise MkvMergeError(
                    f"Expected Save As output missing after rename: {final_path}"
                )
            if final_path.resolve() != source_path.resolve():
                source_path.unlink(missing_ok=True)
                replaced_mp4_deleted = True
                deleted_old_rel = source_rel
    elif output_mode == AppConstants.OUTPUT_MODE_KEEP_BOTH:
        final_path = resolve_keep_both_path(source_path)
        _move_without_overwrite(output_tmp_path, final_path)
    elif is_mp4:
        try:
            final_path = ensure_mp4_replace_target_available(source_path)
            _move_without_overwrite(output_tmp_path, final_path)
        except FileExistsError as exc:
            raise TargetFileCollisionError(str(exc)) from exc

        if not final_path.is_file():
            raise MkvMergeError(
                f"Expected output MKV missing after rename: {final_path}"
            )

        # Delete original .mp4 ONLY after <stem>.mkv is safely in place
        source_path.unlink()
        replaced_mp4_deleted = True
        deleted_old_rel = source_rel
    else:
        final_path = source_path
        os.replace(output_tmp_path, final_path)

    final_rel = to_relative_storage_path(final_path, storage_root)
    return final_path, final_rel, source_rel, replaced_mp4_deleted, deleted_old_rel


def _prepare_save_as_temp_path_sync(
    output_path: str,
    storage_root: Path,
    source_path: Path,
) -> Path:
    """Validate Save As destination, ensure parent directory exists, and build temp path."""
    save_as_target = ensure_save_as_target_available(
        output_path,
        storage_root,
        source_path=source_path,
    )
    save_as_target.parent.mkdir(parents=True, exist_ok=True)
    return build_temp_remux_path(save_as_target)


async def finalize_remux_output(
    source_path: Path,
    output_tmp_path: Path,
    *,
    output_mode: str,
    storage_root: Path,
    output_path: str | None = None,
    delete_original: bool = False,
    media_repo: MediaFileRepository | None = None,
    scanner: LibraryScanner | None = None,
    folder_cache: FolderReconcileCache | None = None,
) -> tuple[Path, str, bool]:
    """Commit `output_tmp_path` atomically off the event loop and refresh SQLite cache.

    Handles:
    - `.mkv` + `replace`: `os.replace(tmp, source)`
    - `.mp4` + `replace`: move `tmp` -> `<stem>.mkv` (failing safely if it exists),
      delete original `.mp4` ONLY after `<stem>.mkv` is in place, delete old `.mp4`
      row from `media_files`, and call `probe_single_file()` on `<stem>.mkv`.
    - `keep_both`: move `tmp` -> auto-incremented `<stem>.remux[.N].mkv`.
    - `save_as`: move `tmp` -> validated custom destination path inside
      `storage_root` (failing safely if destination exists); deletes original
      source ONLY when `delete_original=True` and new output is verified on disk.
    """
    (
        final_path,
        final_rel,
        source_rel,
        replaced_mp4_deleted,
        deleted_old_rel,
    ) = await asyncio.to_thread(
        _commit_remux_files_sync,
        source_path,
        output_tmp_path,
        output_mode=output_mode,
        storage_root=storage_root,
        output_path=output_path,
        delete_original=delete_original,
    )

    if deleted_old_rel is not None and media_repo is not None:
        await asyncio.to_thread(media_repo.delete_by_filepaths, [deleted_old_rel])

    if folder_cache is not None:
        folder_cache.invalidate_folder(extract_folder(source_rel), include_parent=True)
        folder_cache.invalidate_folder(extract_folder(final_rel), include_parent=True)

    if scanner is not None:
        await scanner.probe_single_file(final_rel)

    return final_path, final_rel, replaced_mp4_deleted


async def execute_two_stage_pipeline(
    source_path: Path,
    config: JobConfig,
    *,
    storage_root: Path,
    duration_us: int = 0,
    media_repo: MediaFileRepository | None = None,
    scanner: LibraryScanner | None = None,
    upload_service: UploadService | None = None,
    folder_cache: FolderReconcileCache | None = None,
    processing_policy: ProcessingRuntimePolicy | None = None,
    source_tracks: list[ParsedTrackInfo] | None = None,
    on_stage: StageCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    on_process_started: ProcessCallback | None = None,
    check_cancelled: CancelCheckCallback | None = None,
) -> PipelineExecutionResult:
    """Run Instant Header Edit (`mkvpropedit`) or Pre-Flight -> Stage 1 -> Stage 2 -> Swap.

    Enforces cancel-between-stages checks before Stage 1, between Stage 1 and
    Stage 2, and immediately before the atomic file swap so a cancelled job can
    never advance or overwrite a file. Guarantees cleanup of `<temp_audio_path>`,
    job-owned staged uploads, and `<output_tmp_path>` whenever finalization does
    not move it into place.
    """
    policy = processing_policy or ProcessingRuntimePolicy()
    job_upload_ids = extract_upload_ids_from_tracks(config.external_tracks)
    temp_audio_path: Path | None = None
    output_tmp_path: Path | None = None
    finalized = False

    try:
        if check_cancelled is not None:
            check_cancelled()

        instant_ok, instant_reason = can_use_instant_header_edit(
            source_path,
            config,
            source_tracks=source_tracks,
        )
        if instant_ok:
            if on_stage is not None:
                on_stage("header_edit", "Instant Header Edit (mkvpropedit)")
            propedit_res = await run_mkvpropedit_edit(
                source_path,
                config,
                source_tracks=source_tracks,
                nice_increment=policy.nice_increment,
                use_ionice_idle=policy.use_ionice_idle,
                on_log=on_log,
                on_process_started=on_process_started,
                check_cancelled=check_cancelled,
            )
            if on_progress is not None:
                on_progress(100, None)
            rel_path = to_relative_storage_path(source_path, storage_root)
            if scanner is not None:
                await scanner.probe_single_file(rel_path)
            if folder_cache is not None:
                folder_cache.invalidate_for_filepath(rel_path)
            finalized = True
            return PipelineExecutionResult(
                output_path=source_path,
                output_rel_filepath=rel_path,
                mkvmerge_exit_code=propedit_res.exit_code,
                warnings=propedit_res.warnings,
                replaced_mp4_deleted=False,
            )

        if (
            source_path.suffix.lower() == AppConstants.EXT_MKV
            and config.output_mode == AppConstants.OUTPUT_MODE_REPLACE
            and on_log is not None
            and instant_reason
        ):
            on_log(
                f"Instant header edit (mkvpropedit) skipped ({instant_reason}); "
                "running full mkvmerge remux."
            )

        await asyncio.to_thread(
            run_preflight_checks,
            source_path,
            config,
            duration_us=duration_us,
            storage_root=storage_root,
            scratch_dir=policy.scratch_dir,
        )

        resolved_external_tracks: list[tuple[ExternalTrackConfig, Path]] = []
        if config.external_tracks:
            if upload_service is None:
                raise MkvMergeError("UploadService is required to resolve external tracks")
            resolved_external_tracks = await asyncio.to_thread(
                upload_service.resolve_external_tracks,
                config.external_tracks,
            )

        if config.output_mode == AppConstants.OUTPUT_MODE_SAVE_AS:
            assert config.output_path is not None
            output_tmp_path = await asyncio.to_thread(
                _prepare_save_as_temp_path_sync,
                config.output_path,
                storage_root,
                source_path,
            )
        else:
            output_tmp_path = build_temp_remux_path(source_path)

        if config.audio_conversion.enabled:
            if check_cancelled is not None:
                check_cancelled()
            audio_cfg = config.audio_conversion
            temp_ext = AppConstants.TEMP_AUDIO_EXT_MAP[audio_cfg.target_codec]
            if policy.scratch_dir is not None:
                await asyncio.to_thread(
                    policy.scratch_dir.mkdir, parents=True, exist_ok=True
                )
            temp_audio_path = build_temp_audio_path(
                source_path,
                temp_ext,
                scratch_dir=policy.scratch_dir,
            )
            if on_stage is not None:
                on_stage(
                    "transcode",
                    f"Stage 1 of 2: Encoding {audio_cfg.target_codec.upper()}",
                )
            try:
                await run_ffmpeg_transcode(
                    source_path,
                    temp_audio_path,
                    audio_index=audio_cfg.source_audio_index,
                    target_codec=audio_cfg.target_codec,
                    bitrate=audio_cfg.bitrate,
                    target_channels=audio_cfg.target_channels,
                    audio_dynamics=audio_cfg.audio_dynamics,
                    duration_us=duration_us,
                    expected_codec=audio_cfg.source_codec,
                    expected_channels=audio_cfg.source_channels,
                    threads=policy.ffmpeg_threads,
                    nice_increment=policy.nice_increment,
                    use_ionice_idle=policy.use_ionice_idle,
                    on_progress=on_progress,
                    on_log=on_log,
                    on_process_started=on_process_started,
                    check_cancelled=check_cancelled,
                )
            except Exception:
                if check_cancelled is not None:
                    check_cancelled()
                raise

        # Guardrail #1: check cancellation between Stage 1 and Stage 2
        if check_cancelled is not None:
            check_cancelled()

        if on_stage is not None:
            stage_label = (
                "Stage 2 of 2: Remuxing MKV"
                if config.audio_conversion.enabled
                else "Remuxing MKV"
            )
            on_stage("remux", stage_label)

        try:
            remux_result = await run_mkvmerge_remux(
                source_path,
                output_tmp_path,
                config,
                temp_audio_path=temp_audio_path,
                resolved_external_tracks=resolved_external_tracks,
                nice_increment=policy.nice_increment,
                use_ionice_idle=policy.use_ionice_idle,
                on_progress=on_progress,
                on_log=on_log,
                on_process_started=on_process_started,
            )
        except Exception:
            if check_cancelled is not None:
                check_cancelled()
            raise

        # Guardrail #1: check cancellation immediately BEFORE atomic file swap
        if check_cancelled is not None:
            check_cancelled()

        final_path, final_rel, replaced_mp4_deleted = await finalize_remux_output(
            source_path,
            output_tmp_path,
            output_mode=config.output_mode,
            storage_root=storage_root,
            output_path=config.output_path,
            delete_original=bool(config.delete_original),
            media_repo=media_repo,
            scanner=scanner,
            folder_cache=folder_cache,
        )
        finalized = True

        return PipelineExecutionResult(
            output_path=final_path,
            output_rel_filepath=final_rel,
            mkvmerge_exit_code=remux_result.exit_code,
            warnings=remux_result.warnings,
            replaced_mp4_deleted=replaced_mp4_deleted,
        )
    finally:
        if temp_audio_path is not None:
            await asyncio.to_thread(_safe_unlink, temp_audio_path)
        if not finalized and output_tmp_path is not None:
            await asyncio.to_thread(_safe_unlink, output_tmp_path)
        if upload_service is not None and job_upload_ids:
            await asyncio.to_thread(upload_service.cleanup_uploads, job_upload_ids)
