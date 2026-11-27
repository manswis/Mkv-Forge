"""Pure, deterministic Stage 2 `mkvmerge` argument builder."""

from __future__ import annotations

from pathlib import Path

from app.config import AppConstants
from app.models.schemas import (
    AudioConversionConfig,
    ExternalTrackConfig,
    JobConfig,
    TrackEditConfig,
)


def ns_to_sync_delay_ms(minimum_timestamp_ns: int | None) -> int:
    """Convert `properties.minimum_timestamp` from nanoseconds to integer milliseconds."""
    if not minimum_timestamp_ns:
        return 0
    return round(minimum_timestamp_ns / AppConstants.NS_PER_MS)


def _bool_flag(value: bool) -> str:
    """Convert boolean track flag to mkvmerge 'yes'/'no'."""
    return "yes" if value else "no"


def _resolve_dropped_source_track_id(
    audio_cfg: AudioConversionConfig,
    tracks: list[TrackEditConfig],
) -> int | None:
    """Resolve which source audio track ID to drop when `drop_source_track` is enabled."""
    if not (audio_cfg.enabled and audio_cfg.drop_source_track):
        return None
    if audio_cfg.source_track_id is not None:
        return audio_cfg.source_track_id

    audio_tracks = [track for track in tracks if track.type == "audio"]
    if 0 <= audio_cfg.source_audio_index < len(audio_tracks):
        return audio_tracks[audio_cfg.source_audio_index].id
    return None


def _build_track_order_spec(
    kept_source_tracks: list[TrackEditConfig],
    *,
    source_file_index: int,
    has_new_audio: bool,
    external_order_entries: list[tuple[int, int | None]] | None = None,
) -> str:
    """Build `--track-order` `<file_id>:<track_id>` pairs for kept & imported tracks.

    When Stage 1 produced a new Dolby audio track (file index `0`, track `0`),
    it is inserted at Audio Index 0 (immediately before the first kept source
    audio track, or immediately after the last kept video track if no source
    audio tracks remain). External tracks are placed at their user-specified
    `order_index` (ascending) or appended at the end if unspecified.
    """
    order_pairs: list[str] = []
    inserted_new_audio = False

    for track in kept_source_tracks:
        if has_new_audio and not inserted_new_audio and track.type == "audio":
            order_pairs.append("0:0")
            inserted_new_audio = True
        order_pairs.append(f"{source_file_index}:{track.id}")

    if has_new_audio and not inserted_new_audio:
        last_video_idx = -1
        for idx, track in enumerate(kept_source_tracks):
            if track.type == "video":
                last_video_idx = idx
        order_pairs.insert(last_video_idx + 1, "0:0")

    if external_order_entries:
        indexed = [
            (file_idx, pos)
            for file_idx, pos in external_order_entries
            if pos is not None
        ]
        unindexed = [
            file_idx for file_idx, pos in external_order_entries if pos is None
        ]
        indexed.sort(key=lambda item: (item[1], item[0]))
        for file_idx, pos in indexed:
            clamped = max(0, min(pos, len(order_pairs)))
            order_pairs.insert(clamped, f"{file_idx}:0")
        for file_idx in unindexed:
            order_pairs.append(f"{file_idx}:0")

    return ",".join(order_pairs)


def build_mkvmerge_command(
    source_path: Path,
    output_tmp_path: Path,
    config: JobConfig,
    *,
    temp_audio_path: Path | None = None,
    resolved_external_tracks: list[tuple[ExternalTrackConfig, Path]] | None = None,
    chapters_xml_path: Path | None = None,
) -> list[str]:
    """Construct a complete, shell-free `mkvmerge` argument list.

    Guardrails enforced:
    1. All per-file and per-track options (`--track-name`, `--language`,
       `--default-track-flag`, `--forced-display-flag`, `--sync`, `--*-tracks`,
       `--no-*`) strictly precede the input filename they apply to.
    2. `source_minimum_timestamp_ns` is converted ns -> ms (`round(ns / 1_000_000)`)
       before emitting `--sync 0:<delay_ms>`.
    3. Double-default audio/subtitle tracks are prevented across converted,
       source, and imported tracks.
    4. Empty track types emit `--no-video` / `--no-audio` / `--no-subtitles`
       instead of invalid empty `--*-tracks ""` arguments.
    5. Each external input strips global/track tags, attachments, chapters, and
       unrelated track types before its path.
    """
    audio_cfg = config.audio_conversion
    has_new_audio = temp_audio_path is not None
    external_list = list(resolved_external_tracks or [])

    if audio_cfg.enabled and temp_audio_path is None:
        raise ValueError(
            "temp_audio_path is required when audio_conversion.enabled is True"
        )

    if config.external_tracks and not external_list:
        raise ValueError(
            "resolved_external_tracks is required when config.external_tracks is non-empty"
        )

    chapters_action = config.container.chapters_action
    if (
        chapters_action == AppConstants.CHAPTER_ACTION_REPLACE
        and chapters_xml_path is None
    ):
        raise ValueError(
            "chapters_xml_path is required when chapters_action is 'replace'"
        )

    dropped_audio_id = _resolve_dropped_source_track_id(audio_cfg, config.tracks)
    kept_source_tracks = [
        track
        for track in config.tracks
        if track.enabled and track.id != dropped_audio_id
    ]

    if not has_new_audio and not kept_source_tracks and not external_list:
        raise ValueError("Cannot build mkvmerge command with zero kept tracks")

    argv: list[str] = [
        AppConstants.TOOL_MKVMERGE,
        "--gui-mode",
        "-o",
        str(output_tmp_path),
    ]

    # Global container title & replacement chapters options
    if config.container.title is not None:
        argv.extend(["--title", config.container.title])
    if chapters_action == AppConstants.CHAPTER_ACTION_REPLACE:
        assert chapters_xml_path is not None
        argv.extend(["--chapters", str(chapters_xml_path)])

    # Determine single-default ownership across converted, source, and external tracks
    converted_owns_audio_default = has_new_audio and audio_cfg.set_as_default
    default_ext_audio_id: str | None = None
    default_ext_sub_id: str | None = None
    for ext_cfg, _ext_path in external_list:
        if (
            ext_cfg.kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO
            and ext_cfg.default_track
            and not converted_owns_audio_default
            and default_ext_audio_id is None
        ):
            default_ext_audio_id = ext_cfg.id
        elif (
            ext_cfg.kind == AppConstants.EXTERNAL_TRACK_KIND_SUBTITLES
            and ext_cfg.default_track
            and default_ext_sub_id is None
        ):
            default_ext_sub_id = ext_cfg.id

    # ------------------------------------------------------------------
    # Input File 0 (optional): Stage 1 converted audio (EAC3 / AC3 / MKA)
    # ------------------------------------------------------------------
    if temp_audio_path is not None:
        argv.extend(
            [
                "--no-global-tags",
                "--no-track-tags",
                "--no-attachments",
                "--no-chapters",
            ]
        )
        if audio_cfg.track_title:
            argv.extend(["--track-name", f"0:{audio_cfg.track_title}"])
        if audio_cfg.language and audio_cfg.language.strip():
            argv.extend(["--language", f"0:{audio_cfg.language.strip()}"])
        argv.extend(
            ["--default-track-flag", f"0:{_bool_flag(audio_cfg.set_as_default)}"]
        )
        argv.extend(["--forced-display-flag", "0:no"])

        sync_delay_ms = ns_to_sync_delay_ms(audio_cfg.source_minimum_timestamp_ns)
        if sync_delay_ms != 0:
            argv.extend(["--sync", f"0:{sync_delay_ms}"])

        argv.append(str(temp_audio_path))

    # ------------------------------------------------------------------
    # Source Input File (File 1 when Stage 1 ran, else File 0)
    # All per-file & per-track options MUST precede str(source_path)
    # ------------------------------------------------------------------
    source_file_index = 1 if has_new_audio else 0

    if config.container.strip_global_tags:
        argv.append("--no-global-tags")
    if config.container.strip_track_tags:
        argv.append("--no-track-tags")
    if config.container.strip_attachments or (
        config.container.keep_attachment_ids is not None
        and len(config.container.keep_attachment_ids) == 0
    ):
        argv.append("--no-attachments")
    elif config.container.keep_attachment_ids:
        kept_att_ids = ",".join(
            str(att_id) for att_id in config.container.keep_attachment_ids
        )
        argv.extend(["--attachments", kept_att_ids])
    if (
        chapters_action
        in (AppConstants.CHAPTER_ACTION_REPLACE, AppConstants.CHAPTER_ACTION_CLEAR)
        or config.container.strip_chapters
    ):
        argv.append("--no-chapters")

    kept_video_ids = [str(t.id) for t in kept_source_tracks if t.type == "video"]
    kept_audio_ids = [str(t.id) for t in kept_source_tracks if t.type == "audio"]
    kept_sub_ids = [str(t.id) for t in kept_source_tracks if t.type == "subtitles"]

    if kept_video_ids:
        argv.extend(["--video-tracks", ",".join(kept_video_ids)])
    else:
        argv.append("--no-video")

    if kept_audio_ids:
        argv.extend(["--audio-tracks", ",".join(kept_audio_ids)])
    else:
        argv.append("--no-audio")

    if kept_sub_ids:
        argv.extend(["--subtitle-tracks", ",".join(kept_sub_ids)])
    else:
        argv.append("--no-subtitles")

    demote_source_audio_default = converted_owns_audio_default or (
        default_ext_audio_id is not None
    )
    demote_source_sub_default = default_ext_sub_id is not None

    for track in kept_source_tracks:
        if track.track_name is not None:
            argv.extend(["--track-name", f"{track.id}:{track.track_name}"])
        if track.language is not None and track.language.strip():
            argv.extend(["--language", f"{track.id}:{track.language.strip()}"])

        if demote_source_audio_default and track.type == "audio":
            effective_default = False
        elif demote_source_sub_default and track.type == "subtitles":
            effective_default = False
        else:
            effective_default = track.default_track

        argv.extend(
            ["--default-track-flag", f"{track.id}:{_bool_flag(effective_default)}"]
        )
        argv.extend(
            ["--forced-display-flag", f"{track.id}:{_bool_flag(track.forced_track)}"]
        )
        if track.flag_original is not None:
            argv.extend(
                ["--original-flag", f"{track.id}:{_bool_flag(track.flag_original)}"]
            )
        if track.flag_commentary is not None:
            argv.extend(
                ["--commentary-flag", f"{track.id}:{_bool_flag(track.flag_commentary)}"]
            )
        if track.flag_hearing_impaired is not None:
            argv.extend(
                [
                    "--hearing-impaired-flag",
                    f"{track.id}:{_bool_flag(track.flag_hearing_impaired)}",
                ]
            )
        if track.flag_visual_impaired is not None:
            argv.extend(
                [
                    "--visual-impaired-flag",
                    f"{track.id}:{_bool_flag(track.flag_visual_impaired)}",
                ]
            )

    argv.append(str(source_path))

    # ------------------------------------------------------------------
    # External Imported Track Inputs (File indices source_file_index + 1 ...)
    # ------------------------------------------------------------------
    external_order_entries: list[tuple[int, int | None]] = []
    next_file_index = source_file_index + 1

    for ext_cfg, ext_path in external_list:
        argv.extend(
            [
                "--no-global-tags",
                "--no-track-tags",
                "--no-attachments",
                "--no-chapters",
            ]
        )
        if ext_cfg.kind == AppConstants.EXTERNAL_TRACK_KIND_AUDIO:
            argv.extend(["--no-video", "--no-subtitles"])
            ext_default = ext_cfg.id == default_ext_audio_id
        else:
            argv.extend(["--no-video", "--no-audio"])
            ext_default = ext_cfg.id == default_ext_sub_id

        if ext_cfg.track_name is not None and ext_cfg.track_name.strip():
            argv.extend(["--track-name", f"0:{ext_cfg.track_name.strip()}"])
        if ext_cfg.language is not None and ext_cfg.language.strip():
            argv.extend(["--language", f"0:{ext_cfg.language.strip()}"])
        argv.extend(["--default-track-flag", f"0:{_bool_flag(ext_default)}"])
        argv.extend(
            ["--forced-display-flag", f"0:{_bool_flag(ext_cfg.forced_track)}"]
        )
        if ext_cfg.sync_delay_ms != 0:
            argv.extend(["--sync", f"0:{ext_cfg.sync_delay_ms}"])

        argv.append(str(ext_path))
        external_order_entries.append((next_file_index, ext_cfg.order_index))
        next_file_index += 1

    # Global --track-order referencing kept & imported tracks only
    track_order = _build_track_order_spec(
        kept_source_tracks,
        source_file_index=source_file_index,
        has_new_audio=has_new_audio,
        external_order_entries=external_order_entries,
    )
    argv.extend(["--track-order", track_order])

    return argv
