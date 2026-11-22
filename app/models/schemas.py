"""Pydantic schemas for MKV Forge API responses and requests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import AppConstants


class ToolCheckStatus(BaseModel):
    """Availability and version report for an external CLI binary."""

    model_config = ConfigDict(frozen=True)

    available: bool
    version: str | None = None
    error: str | None = None


class DataDirCheckStatus(BaseModel):
    """Write-access verification report for the application data directory."""

    model_config = ConfigDict(frozen=True)

    path: str
    writable: bool
    error: str | None = None


class HealthToolsReport(BaseModel):
    """Grouped health status for required media engines."""

    model_config = ConfigDict(frozen=True)

    mkvmerge: ToolCheckStatus
    ffmpeg: ToolCheckStatus
    ffprobe: ToolCheckStatus


class HealthResponse(BaseModel):
    """Payload returned by GET /api/health."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    app: str
    version: str
    tools: HealthToolsReport
    data_dir: DataDirCheckStatus


class AudioTrackSummary(BaseModel):
    """Compact audio badge summary stored denormalized in media_files."""

    model_config = ConfigDict(frozen=True)

    codec: str
    channels: int | None = None
    channel_layout: str | None = None
    language: str = "und"


class ParsedTrackInfo(BaseModel):
    """Detailed per-track metadata parsed from mkvmerge -J for the editor."""

    model_config = ConfigDict(frozen=True)

    id: int
    type: str
    codec: str
    codec_id: str | None = None
    language: str = "und"
    language_ietf: str | None = None
    track_name: str = ""
    default_track: bool = False
    forced_track: bool = False
    audio_channels: int | None = None
    audio_sampling_frequency: int | None = None
    audio_index: int | None = None
    pixel_dimensions: str | None = None
    minimum_timestamp_ns: int = 0
    sync_delay_ms: int = 0
    size_bytes: int | None = None


class MediaFileListItem(BaseModel):
    """Fast denormalized item returned by GET /api/files."""

    model_config = ConfigDict(frozen=True)

    id: int
    filepath: str
    folder: str
    filename: str
    size_bytes: int
    mtime: float
    duration_us: int
    resolution: str | None = None
    video_codec: str | None = None
    video_dynamic_range: str | None = None
    audio_summary: list[AudioTrackSummary] = Field(default_factory=list)
    subtitle_count: int = 0
    container_title: str = ""
    has_global_tags: bool = False
    has_metadata: bool = False
    has_aac_51: bool = False
    has_ac3_eac3: bool = False
    needs_conversion: bool = False
    scan_status: Literal["ready", "scanning", "error"] = AppConstants.FILE_SCAN_STATUS_READY  # type: ignore[assignment]
    scanned_at: str


class AudioTypeFacet(BaseModel):
    """Cascading sound-type filter chip with matching file count."""

    model_config = ConfigDict(frozen=True)

    label: str
    count: int


class LibraryFacets(BaseModel):
    """Cascading filter counts matching the active search/filter view."""

    model_config = ConfigDict(frozen=True)

    aac51_only: int = 0
    has_metadata: int = 0
    no_dolby: int = 0
    needs_conversion: int = 0
    audio_types: list[AudioTypeFacet] = Field(default_factory=list)


class FolderTreeEntry(BaseModel):
    """One directory entry returned by GET/POST /api/folders."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    has_children: bool = False


class FolderTrackFileEntry(BaseModel):
    """One importable external audio or subtitle file inside a storage folder."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    size_bytes: int
    kind: Literal["audio", "subtitles"]


class FolderListResponse(BaseModel):
    """Immediate subdirectories (and optional external track files) inside STORAGE_DIR."""

    model_config = ConfigDict(frozen=True)

    path: str
    entries: list[FolderTreeEntry] = Field(default_factory=list)
    files: list[FolderTrackFileEntry] = Field(default_factory=list)


class StagedUploadResponse(BaseModel):
    """Response returned by POST /api/uploads/track."""

    model_config = ConfigDict(frozen=True)

    upload_id: str
    kind: Literal["audio", "subtitles"]
    original_filename: str
    size_bytes: int


class FolderCreateRequest(BaseModel):
    """Request payload for POST /api/folders."""

    model_config = ConfigDict(frozen=True)

    parent: str = "/"
    name: str


class OtherFileEntry(BaseModel):
    """Lightweight non-indexed file discovered via os.scandir in Folder View."""

    model_config = ConfigDict(frozen=True)

    name: str
    filepath: str
    folder: str
    size_bytes: int
    mtime: float
    ext: str


class MediaFileListResponse(BaseModel):
    """Paginated response returned by GET /api/files."""

    model_config = ConfigDict(frozen=True)

    items: list[MediaFileListItem]
    total: int
    page: int
    limit: int
    total_pages: int
    folders: list[str]
    audio_types: list[str] = Field(default_factory=list)
    facets: LibraryFacets = Field(default_factory=LibraryFacets)
    subfolders: list[FolderTreeEntry] = Field(default_factory=list)
    other_files: list[OtherFileEntry] = Field(default_factory=list)
    folder_counts: dict[str, int] = Field(default_factory=dict)
    folder_exists: bool = True


class AttachmentInfo(BaseModel):
    """Embedded container attachment (fonts, cover art, logos, XML, etc.)."""

    model_config = ConfigDict(frozen=True)

    id: int
    file_name: str
    content_type: str
    size_bytes: int = 0
    description: str = ""
    uid: str | None = None


class TagKeyValueInfo(BaseModel):
    """Single key-value tag entry extracted from container or track metadata."""

    model_config = ConfigDict(frozen=True)

    scope: str
    key: str
    value: str


class MediaFileDetailResponse(MediaFileListItem):
    """Full parsed track and container metadata returned by GET /api/files/{id}."""

    has_track_tags: bool = False
    global_tags_count: int = 0
    track_tags_count: int = 0
    attachments_count: int = 0
    chapters_count: int = 0
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    tag_entries: list[TagKeyValueInfo] = Field(default_factory=list)
    tracks: list[ParsedTrackInfo] = Field(default_factory=list)


class ContainerInfoSummary(BaseModel):
    """Comprehensive container-level metadata extracted from mkvmerge -J."""

    model_config = ConfigDict(frozen=True)

    container_type: str = "Unknown"
    title: str = ""
    duration_us: int = 0
    size_bytes: int = 0
    overall_bitrate_bps: int | None = None
    muxing_application: str | None = None
    writing_application: str | None = None
    date_utc: str | None = None
    segment_uid: str | None = None
    chapters_count: int = 0
    attachments_count: int = 0
    global_tags_count: int = 0
    track_tags_count: int = 0


class DetailedTrackInfo(BaseModel):
    """Complete per-track inspection metadata for the File Info popup modal."""

    model_config = ConfigDict(frozen=True)

    id: int
    type: str
    codec: str
    codec_id: str | None = None
    language: str = "und"
    language_ietf: str | None = None
    track_name: str = ""
    uid: str | None = None
    default_track: bool = False
    forced_track: bool = False
    enabled_track: bool = True
    flag_original: bool = False
    flag_commentary: bool = False
    flag_hearing_impaired: bool = False
    flag_visual_impaired: bool = False
    bitrate_bps: int | None = None
    frame_count: int | None = None
    sync_delay_ms: int = 0
    size_bytes: int | None = None
    # Video-specific
    pixel_dimensions: str | None = None
    display_dimensions: str | None = None
    frame_rate: str | None = None
    dynamic_range: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_matrix: str | None = None
    bit_depth: int | None = None
    # Audio-specific
    audio_index: int | None = None
    audio_channels: int | None = None
    channel_layout: str | None = None
    audio_sampling_frequency: int | None = None
    audio_bits_per_sample: int | None = None
    # Subtitle-specific
    text_subtitles: bool | None = None
    encoding: str | None = None


class MediaFileInfoResponse(BaseModel):
    """Comprehensive read-only file metadata returned by GET /api/files/{id}/info."""

    model_config = ConfigDict(frozen=True)

    id: int
    filepath: str
    folder: str
    filename: str
    scanned_at: str
    resolution: str | None = None
    video_codec: str | None = None
    video_dynamic_range: str | None = None
    container: ContainerInfoSummary
    video_tracks: list[DetailedTrackInfo] = Field(default_factory=list)
    audio_tracks: list[DetailedTrackInfo] = Field(default_factory=list)
    subtitle_tracks: list[DetailedTrackInfo] = Field(default_factory=list)
    other_tracks: list[DetailedTrackInfo] = Field(default_factory=list)
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    tag_entries: list[TagKeyValueInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ScanProgressPayload(BaseModel):
    """Structured progress snapshot emitted by POST /api/scan and SSE stream."""

    model_config = ConfigDict(frozen=True)

    scanned: int = 0
    total: int = 0
    skipped: int = 0
    errors: int = 0
    current_file: str | None = None
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    error_message: str | None = None


class RecentJobItem(BaseModel):
    """Compact recent job item displayed on the Dashboard."""

    model_config = ConfigDict(frozen=True)

    id: str
    filepath: str
    output_filepath: str | None = None
    status: str
    stage: str | None = None
    progress: int = 0
    error_message: str | None = None
    created_at: str
    completed_at: str | None = None
    created_by_name: str = AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME


class DashboardStats(BaseModel):
    """Top-level aggregate counts for the Dashboard stat cards."""

    model_config = ConfigDict(frozen=True)

    total_files: int
    aac51_only: int
    has_metadata: int
    needs_conversion: int = 0
    playback_profile_configured: bool = False
    playback_profile_preset: str = AppConstants.DEFAULT_PLAYBACK_PROFILE_PRESET
    playback_profile_label: str = "Not Configured"
    last_scan_at: str | None = None
    last_scan_status: str | None = None


class WatcherStatus(BaseModel):
    """Live filesystem watcher configuration and active state."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["off", "watch", "poll"]
    active: bool
    debounce_sec: float
    scan_interval_min: int


class LibraryChangedEvent(BaseModel):
    """Coalesced SSE notification emitted when the watcher updates SQLite."""

    model_config = ConfigDict(frozen=True)

    action: Literal["added", "updated", "deleted", "batch"]
    count: int = 1
    filepaths: list[str] = Field(default_factory=list)
    file_ids: list[int] = Field(default_factory=list)


class UserSettings(BaseModel):
    """Persistent user-configurable UI, workflow, and maintenance preferences."""

    model_config = ConfigDict(frozen=True)

    # Appearance & Localization (Phase 12)
    theme_mode: Literal["dark", "light", "system"] = AppConstants.DEFAULT_THEME_MODE  # type: ignore[assignment]
    accent_color: Literal["blue", "indigo", "emerald", "violet", "rose", "amber"] = AppConstants.DEFAULT_ACCENT_COLOR  # type: ignore[assignment]
    font_scale: int = Field(
        default=AppConstants.DEFAULT_FONT_SCALE,
        ge=AppConstants.MIN_FONT_SCALE,
        le=AppConstants.MAX_FONT_SCALE,
    )
    language: Literal["en"] = AppConstants.DEFAULT_UI_LANGUAGE  # type: ignore[assignment]

    # Library & Display
    show_video_details: bool = AppConstants.DEFAULT_SHOW_VIDEO_DETAILS
    show_media_files_only: bool = AppConstants.DEFAULT_SHOW_MEDIA_FILES_ONLY
    visible_media_extensions: str = Field(
        default=AppConstants.DEFAULT_VISIBLE_MEDIA_EXTENSIONS,
        max_length=AppConstants.MAX_VISIBLE_EXTENSIONS_LEN,
    )
    default_library_view: Literal["dir", "list"] = AppConstants.DEFAULT_LIBRARY_VIEW  # type: ignore[assignment]
    default_folder_layout: Literal["list", "grid"] = AppConstants.DEFAULT_FOLDER_LAYOUT  # type: ignore[assignment]
    page_limit: Literal[25, 50, 100] = AppConstants.DEFAULT_SETTINGS_PAGE_LIMIT  # type: ignore[assignment]

    # Processing & Editor Defaults
    default_output_mode: Literal["keep_both", "replace", "save_as"] = AppConstants.DEFAULT_SETTINGS_OUTPUT_MODE  # type: ignore[assignment]
    default_dolby_codec: Literal["eac3", "ac3"] = AppConstants.DEFAULT_SETTINGS_DOLBY_CODEC  # type: ignore[assignment]
    default_dolby_bitrate: Literal["640k", "448k", "384k"] = AppConstants.DEFAULT_SETTINGS_DOLBY_BITRATE  # type: ignore[assignment]
    default_strip_global_tags: bool = AppConstants.DEFAULT_STRIP_GLOBAL_TAGS
    default_remove_all_metadata: bool = AppConstants.DEFAULT_REMOVE_ALL_METADATA
    confirm_cancel_job: bool = AppConstants.DEFAULT_CONFIRM_CANCEL_JOB
    confirm_delete_file: bool = AppConstants.DEFAULT_CONFIRM_DELETE_FILE
    preferred_language: str = Field(
        default=AppConstants.DEFAULT_PREFERRED_LANGUAGE,
        min_length=2,
        max_length=16,
    )

    # Scheduled Auto-Cleanup
    auto_cleanup_enabled: bool = AppConstants.DEFAULT_AUTO_CLEANUP_ENABLED
    auto_cleanup_interval_days: Literal[1, 7, 30] = AppConstants.DEFAULT_AUTO_CLEANUP_INTERVAL_DAYS  # type: ignore[assignment]
    job_history_retention_days: Literal[0, 7, 30, 90] = AppConstants.DEFAULT_JOB_HISTORY_RETENTION_DAYS  # type: ignore[assignment]
    auto_cleanup_temp: bool = AppConstants.DEFAULT_AUTO_CLEANUP_TEMP
    auto_cleanup_jobs: bool = AppConstants.DEFAULT_AUTO_CLEANUP_JOBS
    auto_cleanup_optimize_db: bool = AppConstants.DEFAULT_AUTO_CLEANUP_OPTIMIZE_DB

    # Global Playback Profile (Phase 15 — household-wide in app_state)
    playback_profile_preset: Literal[
        "neutral",
        "dolby_arc",
        "optical_legacy",
        "earc_avr",
        "streaming_clients",
        "stereo_mobile",
        "custom",
    ] = AppConstants.DEFAULT_PLAYBACK_PROFILE_PRESET  # type: ignore[assignment]
    playback_supported_codecs: list[str] = Field(default_factory=list)
    playback_max_channels: Literal[2, 6, 8] = AppConstants.DEFAULT_PLAYBACK_MAX_CHANNELS  # type: ignore[assignment]
    playback_preferred_codec: Literal["eac3", "ac3", "aac"] = AppConstants.DEFAULT_PLAYBACK_PREFERRED_CODEC  # type: ignore[assignment]


class UserSettingsUpdate(BaseModel):
    """Partial update payload for PATCH /api/settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    theme_mode: Literal["dark", "light", "system"] | None = None
    accent_color: Literal["blue", "indigo", "emerald", "violet", "rose", "amber"] | None = None
    font_scale: int | None = Field(
        default=None,
        ge=AppConstants.MIN_FONT_SCALE,
        le=AppConstants.MAX_FONT_SCALE,
    )
    language: Literal["en"] | None = None
    show_video_details: bool | None = None
    show_media_files_only: bool | None = None
    visible_media_extensions: str | None = Field(
        default=None,
        max_length=AppConstants.MAX_VISIBLE_EXTENSIONS_LEN,
    )
    default_library_view: Literal["dir", "list"] | None = None
    default_folder_layout: Literal["list", "grid"] | None = None
    page_limit: Literal[25, 50, 100] | None = None
    default_output_mode: Literal["keep_both", "replace", "save_as"] | None = None
    default_dolby_codec: Literal["eac3", "ac3"] | None = None
    default_dolby_bitrate: Literal["640k", "448k", "384k"] | None = None
    default_strip_global_tags: bool | None = None
    default_remove_all_metadata: bool | None = None
    confirm_cancel_job: bool | None = None
    confirm_delete_file: bool | None = None
    preferred_language: str | None = Field(default=None, min_length=2, max_length=16)
    auto_cleanup_enabled: bool | None = None
    auto_cleanup_interval_days: Literal[1, 7, 30] | None = None
    job_history_retention_days: Literal[0, 7, 30, 90] | None = None
    auto_cleanup_temp: bool | None = None
    auto_cleanup_jobs: bool | None = None
    auto_cleanup_optimize_db: bool | None = None
    playback_profile_preset: (
        Literal[
            "neutral",
            "dolby_arc",
            "optical_legacy",
            "earc_avr",
            "streaming_clients",
            "stereo_mobile",
            "custom",
        ]
        | None
    ) = None
    playback_supported_codecs: list[str] | None = None
    playback_max_channels: Literal[2, 6, 8] | None = None
    playback_preferred_codec: Literal["eac3", "ac3", "aac"] | None = None


class MaintenanceStatusResponse(BaseModel):
    """Read-only diagnostics returned by GET /api/maintenance/status."""

    model_config = ConfigDict(frozen=True)

    db_size_bytes: int = 0
    wal_size_bytes: int = 0
    reclaimable_bytes: int = 0
    indexed_files_count: int = 0
    terminal_jobs_count: int = 0
    orphaned_uploads_count: int = 0
    orphaned_uploads_bytes: int = 0
    stray_temp_files_count: int = 0
    stray_temp_files_bytes: int = 0
    last_auto_cleanup_at: str | None = None
    busy: bool = False
    busy_reason: str | None = None


class MaintenanceActionResponse(BaseModel):
    """Outcome payload for one maintenance action."""

    model_config = ConfigDict(frozen=True)

    action: str
    removed_count: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    bytes_reclaimed: int = 0
    duration_ms: float = 0.0
    message: str


class MaintenanceRunAllResponse(BaseModel):
    """Aggregated outcome payload for POST /api/maintenance/run-all."""

    model_config = ConfigDict(frozen=True)

    clean_temp: MaintenanceActionResponse
    prune_missing: MaintenanceActionResponse
    prune_jobs: MaintenanceActionResponse
    optimize_db: MaintenanceActionResponse
    total_bytes_reclaimed: int = 0
    total_duration_ms: float = 0.0


class FactoryResetRequest(BaseModel):
    """Confirmation payload for POST /api/maintenance/factory-reset."""

    model_config = ConfigDict(frozen=True)

    password: str | None = None
    confirm_text: str | None = None


class FactoryResetResponse(BaseModel):
    """Outcome payload after physically rebuilding SQLite and clearing app state."""

    model_config = ConfigDict(frozen=True)

    status: Literal["reset"] = "reset"
    removed_db_files: int = 0
    removed_uploads: int = 0
    duration_ms: float = 0.0
    message: str


class StorageBreakdown(BaseModel):
    """Drive capacity and container size breakdown for the Dashboard storage donut."""

    model_config = ConfigDict(frozen=True)

    disk_total_bytes: int = 0
    disk_used_bytes: int = 0
    disk_free_bytes: int = 0
    mkv_bytes: int = 0
    mkv_count: int = 0
    mp4_bytes: int = 0
    mp4_count: int = 0
    media_bytes: int = 0
    other_used_bytes: int = 0


class AudioCodecSlice(BaseModel):
    """One audio codec family slice for the Dashboard audio donut (counts total audio tracks)."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    count: int = 0
    percent: float = 0.0


class ResolutionCountItem(BaseModel):
    """Compact resolution or dynamic-range count pill for the Dashboard."""

    model_config = ConfigDict(frozen=True)

    label: str
    count: int = 0


class DashboardAnalytics(BaseModel):
    """Storage, audio codec track distribution, and video resolution/HDR analytics."""

    model_config = ConfigDict(frozen=True)

    storage: StorageBreakdown = Field(default_factory=StorageBreakdown)
    total_audio_tracks: int = 0
    audio_codecs: list[AudioCodecSlice] = Field(default_factory=list)
    top_codec_label: str = ""
    top_codec_percent: float = 0.0
    resolutions: list[ResolutionCountItem] = Field(default_factory=list)
    dynamic_ranges: list[ResolutionCountItem] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    """Payload returned by GET /api/dashboard."""

    model_config = ConfigDict(frozen=True)

    app_name: str = AppConstants.APP_NAME
    app_version: str = AppConstants.APP_VERSION
    user_settings: UserSettings = Field(default_factory=UserSettings)
    stats: DashboardStats
    analytics: DashboardAnalytics = Field(default_factory=DashboardAnalytics)
    scan_running: bool
    watcher: WatcherStatus | None = None
    recent_activity: list[RecentJobItem] = Field(default_factory=list)


class ContainerMetadataConfig(BaseModel):
    """Container-level title and stripping options for Stage 2 mkvmerge."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    strip_global_tags: bool = False
    strip_track_tags: bool = False
    strip_attachments: bool = False
    keep_attachment_ids: list[int] | None = None
    strip_chapters: bool = False


class AudioConversionConfig(BaseModel):
    """Stage 1 FFmpeg capability-driven audio conversion settings."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    source_track_id: int | None = None
    source_audio_index: int = Field(default=0, ge=0)
    source_channels: int | None = Field(default=None, ge=1)
    source_codec: str | None = None
    source_minimum_timestamp_ns: int = 0
    target_codec: Literal["eac3", "ac3", "aac", "opus", "flac"] = "eac3"
    target_channels: int | None = Field(
        default=None,
        ge=AppConstants.MIN_AUDIO_CHANNELS,
        le=AppConstants.MAX_AUDIO_CHANNELS,
    )
    bitrate: str | None = "640k"
    track_title: str = "EAC3 5.1"
    language: str = "eng"
    set_as_default: bool = True
    drop_source_track: bool = False

    @model_validator(mode="after")
    def _validate_audio_matrix_constraints(self) -> "AudioConversionConfig":
        if not self.enabled:
            return self
        max_channels = AppConstants.AUDIO_CODEC_MAX_CHANNELS.get(
            self.target_codec, AppConstants.MAX_DOLBY_CHANNELS
        )
        if self.target_channels is not None:
            if self.target_channels > max_channels:
                raise ValueError(
                    f"Codec {self.target_codec!r} supports at most {max_channels} channels "
                    f"(got {self.target_channels})"
                )
            if (
                self.source_channels is not None
                and self.target_channels > self.source_channels
            ):
                raise ValueError(
                    f"Cannot upmix audio from {self.source_channels} to {self.target_channels} channels"
                )
        if self.target_codec not in AppConstants.LOSSLESS_TARGET_CODECS:
            allowed_rates = AppConstants.AUDIO_CODEC_ALLOWED_BITRATES.get(
                self.target_codec, frozenset()
            )
            if not self.bitrate or self.bitrate not in allowed_rates:
                raise ValueError(
                    f"Unsupported bitrate {self.bitrate!r} for target codec {self.target_codec!r}"
                )
        return self


class TrackEditConfig(BaseModel):
    """Per-track selection, naming, language, and flag settings for Stage 2."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(ge=0)
    type: Literal["video", "audio", "subtitles"]
    enabled: bool = True
    track_name: str | None = None
    language: str | None = None
    default_track: bool = False
    forced_track: bool = False


class ExternalTrackConfig(BaseModel):
    """External audio or subtitle track imported from /storage or staged upload."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    kind: Literal["audio", "subtitles"]
    source: Literal["storage", "upload"]
    path: str | None = None
    upload_id: str | None = None
    original_filename: str = ""
    track_name: str | None = None
    language: str | None = AppConstants.DEFAULT_LANGUAGE
    default_track: bool = False
    forced_track: bool = False
    sync_delay_ms: int = Field(
        default=0,
        ge=AppConstants.MIN_SYNC_DELAY_MS,
        le=AppConstants.MAX_SYNC_DELAY_MS,
    )
    order_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_source_locator(self) -> "ExternalTrackConfig":
        if self.source == AppConstants.EXTERNAL_TRACK_SOURCE_STORAGE:
            if not self.path or not self.path.strip():
                raise ValueError("path is required when external track source is 'storage'")
        elif self.source == AppConstants.EXTERNAL_TRACK_SOURCE_UPLOAD:
            if not self.upload_id or not self.upload_id.strip():
                raise ValueError("upload_id is required when external track source is 'upload'")
        return self


class JobConfig(BaseModel):
    """Full remux and optional audio transcode specification."""

    model_config = ConfigDict(frozen=True)

    container: ContainerMetadataConfig = Field(default_factory=ContainerMetadataConfig)
    audio_conversion: AudioConversionConfig = Field(
        default_factory=AudioConversionConfig
    )
    tracks: list[TrackEditConfig] = Field(default_factory=list)
    external_tracks: list[ExternalTrackConfig] = Field(
        default_factory=list,
        max_length=AppConstants.MAX_EXTERNAL_TRACKS_PER_JOB,
    )
    output_mode: Literal["replace", "keep_both", "save_as"] = "replace"
    output_path: str | None = None
    delete_original: bool = False

    @model_validator(mode="after")
    def _validate_save_as_output_path(self) -> "JobConfig":
        if self.output_mode == "save_as":
            if self.output_path is None or not self.output_path.strip():
                raise ValueError(
                    "output_path is required when output_mode is 'save_as'"
                )
        return self


class JobCreateRequest(BaseModel):
    """Request payload for POST /api/jobs (accepts file_id or relative filepath)."""

    model_config = ConfigDict(frozen=True)

    file_id: int | None = Field(default=None, ge=1)
    filepath: str | None = None
    config: JobConfig

    @model_validator(mode="after")
    def _validate_target_present(self) -> "JobCreateRequest":
        has_filepath = self.filepath is not None and self.filepath.strip() != ""
        if self.file_id is None and not has_filepath:
            raise ValueError("Either file_id or filepath must be provided")
        return self


class JobChangeItem(BaseModel):
    """Structured human-readable summary chip for one change applied by a job."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["added", "removed", "changed", "output"]
    label: str


class JobItemResponse(BaseModel):
    """Serialized job record returned by GET/POST/DELETE /api/jobs."""

    model_config = ConfigDict(frozen=True)

    id: str
    filepath: str
    output_filepath: str | None = None
    output_mode: str | None = None
    status: str
    stage: str | None = None
    progress: int = 0
    changes: list[JobChangeItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None
    created_at: str
    completed_at: str | None = None
    created_by_name: str = AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME


class JobHistoryDeleteResponse(BaseModel):
    """Response returned when deleting one or all terminal job history entries."""

    model_config = ConfigDict(frozen=True)

    deleted: int
    id: str | None = None


class JobListResponse(BaseModel):
    """Structured active, queued, and historical job lists from GET /api/jobs."""

    model_config = ConfigDict(frozen=True)

    active: JobItemResponse | None = None
    queued: list[JobItemResponse] = Field(default_factory=list)
    history: list[JobItemResponse] = Field(default_factory=list)


class FileRenameRequest(BaseModel):
    """Request payload for POST /api/files/{id}/rename."""

    model_config = ConfigDict(frozen=True)

    new_name: str


class OtherFileRenameRequest(BaseModel):
    """Request payload for POST /api/files/other/rename."""

    model_config = ConfigDict(frozen=True)

    filepath: str
    new_name: str


class OtherFileMoveRequest(BaseModel):
    """Request payload for POST /api/files/other/move."""

    model_config = ConfigDict(frozen=True)

    filepath: str
    destination_folder: str


class OtherFileDeleteRequest(BaseModel):
    """Request payload for POST /api/files/other/delete."""

    model_config = ConfigDict(frozen=True)

    filepath: str


class OtherFileOpResponse(BaseModel):
    """Outcome payload for rename/move/delete on non-indexed files."""

    model_config = ConfigDict(frozen=True)

    old_filepath: str
    new_filepath: str | None = None
    promoted_to_media: bool = False


class FilesMoveRequest(BaseModel):
    """Request payload for POST /api/files/move (single or multi-select)."""

    model_config = ConfigDict(frozen=True)

    file_ids: list[int] = Field(min_length=1)
    destination_folder: str


class FilesDeleteRequest(BaseModel):
    """Request payload for POST /api/files/delete (single or multi-select)."""

    model_config = ConfigDict(frozen=True)

    file_ids: list[int] = Field(min_length=1)


class BatchFileOpItemResult(BaseModel):
    """Per-file outcome inside a batch move or delete operation."""

    model_config = ConfigDict(frozen=True)

    file_id: int
    filepath: str | None = None
    new_filepath: str | None = None
    ok: bool
    status_code: int
    error: str | None = None


class BatchFileOpResponse(BaseModel):
    """Aggregated outcome for batch file move or delete endpoints."""

    model_config = ConfigDict(frozen=True)

    succeeded: int
    failed: int
    results: list[BatchFileOpItemResult] = Field(default_factory=list)


class FolderRenameRequest(BaseModel):
    """Request payload for POST /api/folders/rename."""

    model_config = ConfigDict(frozen=True)

    path: str
    new_name: str


class FolderMoveRequest(BaseModel):
    """Request payload for POST /api/folders/move."""

    model_config = ConfigDict(frozen=True)

    path: str
    destination_folder: str


class FolderDeleteRequest(BaseModel):
    """Request payload for POST /api/folders/delete."""

    model_config = ConfigDict(frozen=True)

    path: str
    recursive: bool = False


class FolderOpResponse(BaseModel):
    """Outcome for folder rename, move, or delete operations."""

    model_config = ConfigDict(frozen=True)

    old_path: str
    new_path: str | None = None
    affected_files: int = 0


class JobProgressEvent(BaseModel):
    """Real-time SSE event broadcast on GET /api/jobs/stream."""

    model_config = ConfigDict(frozen=True)

    job_id: str | None = None
    filepath: str | None = None
    stage: str | None = None
    stage_label: str | None = None
    percent: int = 0
    speed: str | None = None
    eta: str | None = None
    log_line: str | None = None
    recent_logs: list[str] | None = None
    status: str = "idle"
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Phase 13: Local Accounts, Sessions & Onboarding Schemas
# ---------------------------------------------------------------------------


class AuthUserSummary(BaseModel):
    """Public user profile returned by auth endpoints (never exposes password hashes)."""

    model_config = ConfigDict(frozen=True)

    id: int
    username: str
    display_name: str
    role: str = AppConstants.AUTH_ROLE_ADMIN
    is_anonymous: bool = False
    created_at: str


class AuthStatusResponse(BaseModel):
    """Current authentication mode, onboarding flag, and resolved user."""

    model_config = ConfigDict(frozen=True)

    auth_mode: Literal["unconfigured", "anonymous", "local"]
    authenticated: bool
    needs_setup: bool
    user: AuthUserSummary | None = None
    storage_path: str | None = None
    storage_writable: bool = True


class AuthSetupRequest(BaseModel):
    """First-run onboarding or upgrade-from-anonymous payload."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["anonymous", "local"]
    username: str | None = None
    display_name: str | None = None
    password: str | None = None


class AuthLoginRequest(BaseModel):
    """Local username/password sign-in payload."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(min_length=1, max_length=AppConstants.AUTH_MAX_USERNAME_LEN)
    password: str = Field(min_length=1, max_length=AppConstants.AUTH_MAX_PASSWORD_LEN)


class AuthUserCreateRequest(BaseModel):
    """Create a peer local account."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(
        min_length=AppConstants.AUTH_MIN_USERNAME_LEN,
        max_length=AppConstants.AUTH_MAX_USERNAME_LEN,
    )
    display_name: str | None = Field(
        default=None, max_length=AppConstants.AUTH_MAX_DISPLAY_NAME_LEN
    )
    password: str = Field(
        min_length=AppConstants.AUTH_MIN_PASSWORD_LEN,
        max_length=AppConstants.AUTH_MAX_PASSWORD_LEN,
    )


class AuthPasswordChangeRequest(BaseModel):
    """Change current user's password."""

    model_config = ConfigDict(frozen=True)

    current_password: str = Field(
        min_length=1, max_length=AppConstants.AUTH_MAX_PASSWORD_LEN
    )
    new_password: str = Field(
        min_length=AppConstants.AUTH_MIN_PASSWORD_LEN,
        max_length=AppConstants.AUTH_MAX_PASSWORD_LEN,
    )


class AuthDisableRequest(BaseModel):
    """Switch from local auth back to anonymous passwordless mode."""

    model_config = ConfigDict(frozen=True)

    password: str = Field(min_length=1, max_length=AppConstants.AUTH_MAX_PASSWORD_LEN)


class AuthUsersListResponse(BaseModel):
    """List of local user accounts."""

    model_config = ConfigDict(frozen=True)

    users: list[AuthUserSummary] = Field(default_factory=list)


class ApiKeySummary(BaseModel):
    """Public metadata for an active API key (never exposes full key or hash)."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    key_prefix: str
    created_at: str
    creator_name: str = ""
    last_used_at: str | None = None


class ApiKeyCreateRequest(BaseModel):
    """Payload to create a new API key."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        default="Default",
        min_length=1,
        max_length=AppConstants.AUTH_API_KEY_MAX_NAME_LEN,
    )


class ApiKeyCreateResponse(BaseModel):
    """Response returned upon API key creation containing the raw token once."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    key: str
    key_prefix: str
    created_at: str
    creator_name: str = ""


class ApiKeysListResponse(BaseModel):
    """List of active API keys for the current user."""

    model_config = ConfigDict(frozen=True)

    api_keys: list[ApiKeySummary] = Field(default_factory=list)


class TestMediaGenerateRequest(BaseModel):
    """Request payload for POST /api/tools/generate-test-media."""

    __test__ = False
    model_config = ConfigDict(frozen=True)

    output_mode: Literal["video", "audio"] = AppConstants.TEST_MEDIA_OUTPUT_VIDEO
    codec: Literal["eac3", "ac3", "aac", "opus", "flac", "pcm"] = (
        AppConstants.TARGET_CODEC_EAC3
    )
    layout: Literal["1.0", "2.0", "2.1", "5.1", "7.1"] = "5.1"
    container: str | None = None
    bitrate: str | None = None
    signal_type: Literal["tones", "pink_noise"] = AppConstants.TEST_MEDIA_SIGNAL_TONES
    tone_duration_sec: float = Field(
        default=AppConstants.TEST_MEDIA_SLOT_DURATION_SEC,
        ge=AppConstants.TEST_MEDIA_MIN_SLOT_DURATION_SEC,
        le=AppConstants.TEST_MEDIA_MAX_SLOT_DURATION_SEC,
    )
    gap_duration_sec: float = Field(
        default=AppConstants.TEST_MEDIA_GAP_DURATION_SEC,
        ge=AppConstants.TEST_MEDIA_MIN_GAP_DURATION_SEC,
        le=AppConstants.TEST_MEDIA_MAX_GAP_DURATION_SEC,
    )
    include_subtitles: bool = AppConstants.TEST_MEDIA_DEFAULT_INCLUDE_SUBTITLES
    destination_dir: str = AppConstants.TEST_MEDIA_DEFAULT_DESTINATION_DIR
    filename: str | None = None


class TestMediaGenerateResponse(BaseModel):
    """Response payload returned after generating a speaker test file."""

    __test__ = False
    model_config = ConfigDict(frozen=True)

    filepath: str
    filename: str
    folder: str
    size_bytes: int
    duration_sec: float
    codec: str
    layout: str
    channels: int
    output_mode: str
    container: str
    file_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


