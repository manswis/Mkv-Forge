"""Application configuration and centralized constants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class AppConstants:
    """Centralized immutable constants to eliminate magic values."""

    APP_NAME: str = "MKV Forge"
    DEFAULT_APP_VERSION: str = "1.0.2"
    ENV_APP_VERSION: str = "MKVFORGE_APP_VERSION"
    APP_VERSION: str = (
        os.environ.get("MKVFORGE_APP_VERSION", "").strip() or DEFAULT_APP_VERSION
    )

    DEFAULT_HOST: str = "0.0.0.0"
    DEFAULT_PORT: int = 7788
    UVICORN_WORKERS: int = 1

    DEFAULT_STORAGE_DIR: str = "/storage"
    DEFAULT_DATA_DIR: str = "/app/data"
    DEFAULT_LOG_LEVEL: str = "INFO"

    ENV_STORAGE_DIR: str = "MKVFORGE_STORAGE_DIR"
    ENV_DATA_DIR: str = "MKVFORGE_DATA_DIR"
    ENV_HEALTH_TIMEOUT_SEC: str = "MKVFORGE_HEALTH_TIMEOUT_SEC"
    ENV_SCAN_TIMEOUT_SEC: str = "MKVFORGE_SCAN_TIMEOUT_SEC"
    ENV_LOG_LEVEL: str = "MKVFORGE_LOG_LEVEL"
    ENV_WATCH_MODE: str = "MKVFORGE_WATCH_MODE"
    ENV_WATCH_DEBOUNCE_SEC: str = "MKVFORGE_WATCH_DEBOUNCE_SEC"
    ENV_SCAN_INTERVAL_MIN: str = "MKVFORGE_SCAN_INTERVAL_MIN"
    ENV_MAX_SUB_UPLOAD_MB: str = "MKVFORGE_MAX_SUB_UPLOAD_MB"
    ENV_MAX_AUDIO_UPLOAD_MB: str = "MKVFORGE_MAX_AUDIO_UPLOAD_MB"

    # Phase 10: External track import & staged uploads
    UPLOADS_SUBDIR_NAME: str = "uploads"
    UPLOAD_STREAM_CHUNK_BYTES: int = 64 * 1024
    BYTES_PER_MIB: int = 1024 * 1024
    DEFAULT_MAX_SUB_UPLOAD_MB: int = 25
    MIN_MAX_SUB_UPLOAD_MB: int = 1
    MAX_MAX_SUB_UPLOAD_MB: int = 512
    DEFAULT_MAX_AUDIO_UPLOAD_MB: int = 1024
    MIN_MAX_AUDIO_UPLOAD_MB: int = 1
    MAX_MAX_AUDIO_UPLOAD_MB: int = 8192
    EXTERNAL_TRACK_KIND_AUDIO: str = "audio"
    EXTERNAL_TRACK_KIND_SUBTITLES: str = "subtitles"
    EXTERNAL_TRACK_SOURCE_STORAGE: str = "storage"
    EXTERNAL_TRACK_SOURCE_UPLOAD: str = "upload"
    EXTERNAL_SUBTITLE_EXTENSIONS: frozenset[str] = frozenset(
        {".srt", ".ass", ".ssa", ".vtt", ".sup"}
    )
    EXTERNAL_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
        {".ac3", ".eac3", ".aac", ".mka", ".dts", ".flac", ".mp3", ".opus", ".m4a"}
    )
    MAX_EXTERNAL_TRACKS_PER_JOB: int = 16
    MIN_SYNC_DELAY_MS: int = -600_000
    MAX_SYNC_DELAY_MS: int = 600_000

    # Phase 8: Live filesystem watcher
    WATCH_MODE_OFF: str = "off"
    WATCH_MODE_WATCH: str = "watch"
    WATCH_MODE_POLL: str = "poll"
    VALID_WATCH_MODES: frozenset[str] = frozenset({"off", "watch", "poll"})
    DEFAULT_WATCH_MODE: str = "watch"
    DEFAULT_WATCH_DEBOUNCE_SEC: float = 8.0
    MIN_WATCH_DEBOUNCE_SEC: float = 0.05
    MAX_WATCH_DEBOUNCE_SEC: float = 300.0
    DEFAULT_SCAN_INTERVAL_MIN: int = 0
    MIN_SCAN_INTERVAL_MIN: int = 0
    MAX_SCAN_INTERVAL_MIN: int = 10_080  # 7 days
    WATCH_POLL_INTERVAL_MS: int = 2000
    WATCH_JOB_DEFER_POLL_SEC: float = 1.0
    WATCH_EVENT_COALESCE_SEC: float = 0.75
    WATCH_MAX_EVENT_PATHS: int = 20
    WATCH_MAX_STABILITY_CHECKS: int = 360  # ~48m at 8s default
    IGNORED_PARTIAL_SUFFIXES: frozenset[str] = frozenset(
        {".part", ".!qb", ".tmp", ".crdownload", ".partial"}
    )

    DEFAULT_HEALTH_TIMEOUT_SEC: float = 5.0
    MIN_HEALTH_TIMEOUT_SEC: float = 0.1
    MAX_HEALTH_TIMEOUT_SEC: float = 60.0

    DEFAULT_SCAN_TIMEOUT_SEC: float = 30.0
    MIN_SCAN_TIMEOUT_SEC: float = 1.0
    MAX_SCAN_TIMEOUT_SEC: float = 300.0

    HEALTH_PROBE_PREFIX: str = ".mkvforge_health_probe_"
    HEALTH_PROBE_PAYLOAD: bytes = b"ok"
    MAX_ERROR_EXCERPT_LEN: int = 240

    TOOL_MKVMERGE: str = "mkvmerge"
    TOOL_MKVEXTRACT: str = "mkvextract"
    TOOL_FFMPEG: str = "ffmpeg"
    TOOL_FFPROBE: str = "ffprobe"
    VERSION_FLAG_DOUBLE_DASH: str = "--version"
    VERSION_FLAG_SINGLE_DASH: str = "-version"
    MKVMERGE_IDENTIFY_FLAG: str = "-J"

    # Database
    DB_FILENAME: str = "mkvforge.db"
    SQLITE_BUSY_TIMEOUT_MS: int = 5000
    STATE_KEY_LAST_SCAN_AT: str = "last_scan_at"
    STATE_KEY_LAST_SCAN_STATUS: str = "last_scan_status"

    # File extensions & temp naming
    EXT_MKV: str = ".mkv"
    EXT_MP4: str = ".mp4"
    SUPPORTED_MEDIA_EXTENSIONS: frozenset[str] = frozenset({".mkv", ".mp4"})
    TEMP_MARKER: str = ".mkvforge."
    TEMP_REMUX_SUFFIX: str = ".mkvforge.tmp"
    KEEP_BOTH_INFIX: str = ".remux"
    MACOS_APPLEDOUBLE_PREFIX: str = "._"
    MAX_KEEP_BOTH_COLLISIONS: int = 10_000
    MAX_DUPLICATE_COLLISIONS: int = 10_000
    MTIME_TOLERANCE_SEC: float = 1e-6

    # Time & media parsing constants
    NS_PER_US: int = 1_000
    NS_PER_MS: int = 1_000_000
    US_PER_SEC: int = 1_000_000
    BITS_PER_BYTE: int = 8
    BYTES_PER_MIB: int = 1024 * 1024
    CHANNELS_71: int = 8
    CHANNELS_61: int = 7
    CHANNELS_51: int = 6
    CHANNELS_50: int = 5
    CHANNELS_40: int = 4
    CHANNELS_30: int = 3
    CHANNELS_STEREO: int = 2
    CHANNELS_MONO: int = 1
    MIN_AUDIO_CHANNELS: int = 1
    MAX_DOLBY_CHANNELS: int = 6
    MAX_AUDIO_CHANNELS: int = 8
    CHANNEL_LAYOUT_51: str = "5.1"
    CHANNEL_LAYOUT_MAP: Mapping[int, str] = {
        1: "mono",
        2: "stereo",
        3: "3.0",
        4: "quad",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
    }
    DOLBY_SUPPORTED_SAMPLE_RATES: frozenset[int] = frozenset({48_000, 44_100, 32_000})
    DOLBY_FALLBACK_SAMPLE_RATE: int = 48_000
    OPUS_SAMPLE_RATE: int = 48_000
    OPUS_MULTICHANNEL_MAPPING_FAMILY: int = 1
    OPUS_MAX_MONO_BITRATE: str = "256k"
    OPUS_MAX_MONO_BPS: int = 256_000
    DOLBY_DECODE_DRC_SCALE_OFF: str = "0"
    RES_2160P_MIN_HEIGHT: int = 2000
    RES_2160P_MIN_WIDTH: int = 3500
    RES_1080P_MIN_HEIGHT: int = 1000
    RES_1080P_MIN_WIDTH: int = 1800
    RES_720P_MIN_HEIGHT: int = 680
    RES_720P_MIN_WIDTH: int = 1200
    DEFAULT_LANGUAGE: str = "und"

    # Video dynamic range classification (ITU-T H.273 & Matroska block additions)
    COLOR_TRANSFER_PQ: int = 16  # SMPTE ST 2084 (HDR10 / PQ)
    COLOR_TRANSFER_HLG: int = 18  # ARIB STD-B67 (HLG)
    COLOR_PRIMARIES_BT2020: int = 9
    COLOR_MATRIX_BT2020_TYPES: frozenset[int] = frozenset({9, 10})
    DV_BLOCK_ADDITION_TYPES: frozenset[int] = frozenset({1685480259, 1685485123})
    COLOR_TRANSFER_KEYS: tuple[str, ...] = (
        "color_transfer",
        "color_transfer_characteristics",
        "colour_transfer",
        "colour_transfer_characteristics",
    )
    COLOR_PRIMARIES_KEYS: tuple[str, ...] = (
        "color_primaries",
        "colour_primaries",
    )
    COLOR_MATRIX_KEYS: tuple[str, ...] = (
        "color_matrix",
        "color_matrix_coefficients",
        "colour_matrix",
        "colour_matrix_coefficients",
    )
    HDR_MASTERING_KEYS: tuple[str, ...] = (
        "max_luminance",
        "min_luminance",
        "max_content_light",
        "max_frame_light",
        "maximum_content_light",
        "maximum_frame_average_light",
        "color_max_cll",
        "color_max_fall",
        "color_luminance_max",
        "max_cll",
        "max_fall",
        "chromaticity_coordinates",
        "white_color_coordinates",
    )
    STATE_DYNAMIC_RANGE_CLASSIFIER_VERSION: str = "dynamic_range_classifier_version"
    DYNAMIC_RANGE_CLASSIFIER_VERSION: str = "2"
    DYNAMIC_RANGE_DV: str = "DV"
    DYNAMIC_RANGE_DV_HDR: str = "DV HDR"
    DYNAMIC_RANGE_HDR10: str = "HDR10"
    DYNAMIC_RANGE_HLG: str = "HLG"
    DYNAMIC_RANGE_HDR: str = "HDR"
    DYNAMIC_RANGE_SDR: str = "SDR"

    # Dashboard analytics lens ordering & language normalization constants
    DASHBOARD_TOP_AUDIO_LANGUAGES_LIMIT: int = 5
    KNOWN_VIDEO_CODECS: tuple[str, ...] = (
        "HEVC",
        "H.264",
        "AV1",
        "VP9",
        "MPEG-2",
        "VC-1",
    )
    AUDIO_CHANNEL_TIER_ORDER: tuple[tuple[str, str], ...] = (
        ("7.1", "7.1 Surround"),
        ("5.1", "5.1 Surround"),
        ("2.0", "2.0 Stereo"),
        ("1.0", "1.0 Mono"),
        ("Other", "Other Channels"),
    )
    LANGUAGE_DISPLAY_LABELS: Mapping[str, str] = {
        "und": "und (Untagged)",
        "eng": "English (eng)",
        "en": "English (en)",
        "hin": "Hindi (hin)",
        "hi": "Hindi (hi)",
        "ben": "Bengali (ben)",
        "bn": "Bengali (bn)",
        "jpn": "Japanese (jpn)",
        "ja": "Japanese (ja)",
        "spa": "Spanish (spa)",
        "es": "Spanish (es)",
        "fra": "French (fra)",
        "fre": "French (fre)",
        "fr": "French (fr)",
        "deu": "German (deu)",
        "ger": "German (ger)",
        "de": "German (de)",
        "kor": "Korean (kor)",
        "ko": "Korean (ko)",
        "zho": "Chinese (zho)",
        "chi": "Chinese (chi)",
        "zh": "Chinese (zh)",
        "ita": "Italian (ita)",
        "it": "Italian (it)",
        "rus": "Russian (rus)",
        "ru": "Russian (ru)",
        "por": "Portuguese (por)",
        "pt": "Portuguese (pt)",
        "tam": "Tamil (tam)",
        "tel": "Telugu (tel)",
        "mal": "Malayalam (mal)",
        "kan": "Kannada (kan)",
        "mar": "Marathi (mar)",
        "ara": "Arabic (ara)",
        "other": "Other Languages",
    }

    # Persistent user settings defaults (stored in SQLite app_state)
    STATE_KEY_USER_SETTINGS: str = "user_settings"
    STATE_KEY_LAST_AUTO_CLEANUP_AT: str = "last_auto_cleanup_at"
    STATE_KEY_AUTH_MODE: str = "auth_mode"

    # Phase 13 Local Authentication & Hybrid Settings constants
    AUTH_MODE_UNCONFIGURED: str = "unconfigured"
    AUTH_MODE_ANONYMOUS: str = "anonymous"
    AUTH_MODE_LOCAL: str = "local"
    ALLOWED_AUTH_MODES: frozenset[str] = frozenset(
        {"unconfigured", "anonymous", "local"}
    )
    AUTH_ROLE_ADMIN: str = "admin"
    AUTH_ANONYMOUS_USERNAME: str = "anonymous"
    AUTH_ANONYMOUS_DISPLAY_NAME: str = "Anonymous"
    AUTH_SESSION_COOKIE_NAME: str = "mkvforge_session"
    AUTH_SESSION_TTL_DAYS: int = 30
    AUTH_SESSION_TTL_SEC: int = 30 * 86400
    AUTH_SESSION_TOKEN_BYTES: int = 32
    AUTH_SCRYPT_N: int = 16384
    AUTH_SCRYPT_R: int = 8
    AUTH_SCRYPT_P: int = 1
    AUTH_SCRYPT_DKLEN: int = 32
    AUTH_SCRYPT_SALT_BYTES: int = 16
    AUTH_MIN_USERNAME_LEN: int = 2
    AUTH_MAX_USERNAME_LEN: int = 32
    AUTH_MIN_PASSWORD_LEN: int = 6
    AUTH_MAX_PASSWORD_LEN: int = 128
    AUTH_MAX_DISPLAY_NAME_LEN: int = 48
    AUTH_API_KEY_PREFIX: str = "mkv_"
    AUTH_API_KEY_BYTES: int = 32
    AUTH_API_KEY_MAX_NAME_LEN: int = 64
    AUTH_API_KEY_HEADER: str = "x-api-key"
    AUTH_BEARER_PREFIX: str = "bearer "
    AUTH_PUBLIC_PATHS: frozenset[str] = frozenset(
        {
            "/",
            "/api/health",
            "/api/auth/status",
            "/api/auth/login",
            "/api/auth/setup",
        }
    )
    SYSTEM_SETTING_KEYS: frozenset[str] = frozenset(
        {
            "auto_cleanup_enabled",
            "auto_cleanup_interval_days",
            "job_history_retention_days",
            "auto_cleanup_temp",
            "auto_cleanup_jobs",
            "auto_cleanup_optimize_db",
            "playback_profile_preset",
            "playback_supported_codecs",
            "playback_max_channels",
            "playback_preferred_codec",
        }
    )
    USER_SETTING_KEYS: frozenset[str] = frozenset(
        {
            "theme_mode",
            "accent_color",
            "font_scale",
            "language",
            "show_video_details",
            "show_grid_thumbnails",
            "show_media_files_only",
            "visible_media_extensions",
            "default_library_view",
            "default_folder_layout",
            "page_limit",
            "default_output_mode",
            "default_dolby_codec",
            "default_dolby_bitrate",
            "default_strip_global_tags",
            "default_remove_all_metadata",
            "confirm_cancel_job",
            "confirm_delete_file",
            "preferred_language",
        }
    )
    DEFAULT_THEME_MODE: str = "dark"
    ALLOWED_THEME_MODES: frozenset[str] = frozenset({"dark", "light", "system"})
    DEFAULT_ACCENT_COLOR: str = "blue"
    ALLOWED_ACCENT_COLORS: frozenset[str] = frozenset(
        {"blue", "indigo", "emerald", "violet", "rose", "amber"}
    )
    DEFAULT_FONT_SCALE: int = 100
    MIN_FONT_SCALE: int = 85
    MAX_FONT_SCALE: int = 125
    DEFAULT_UI_LANGUAGE: str = "en"
    ALLOWED_UI_LANGUAGES: frozenset[str] = frozenset({"en"})
    DEFAULT_SHOW_VIDEO_DETAILS: bool = True
    DEFAULT_SHOW_GRID_THUMBNAILS: bool = True
    THUMBNAILS_SUBDIR_NAME: str = "thumbnails"
    THUMBNAIL_MAX_CONCURRENCY: int = 2
    THUMBNAIL_EXTRACT_TIMEOUT_SEC: float = 8.0
    THUMBNAIL_MAX_DIMENSION_PX: int = 256
    THUMBNAIL_CACHE_CONTROL_HEADER: str = "public, max-age=31536000, immutable"
    IMAGE_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
        {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    DEFAULT_SHOW_MEDIA_FILES_ONLY: bool = True
    DEFAULT_VISIBLE_MEDIA_EXTENSIONS: str = ".mkv, .mp4"
    MAX_VISIBLE_EXTENSIONS_LEN: int = 256
    DEFAULT_LIBRARY_VIEW: str = "dir"
    DEFAULT_FOLDER_LAYOUT: str = "list"
    DEFAULT_SETTINGS_PAGE_LIMIT: int = 50
    ALLOWED_SETTINGS_PAGE_LIMITS: frozenset[int] = frozenset({25, 50, 100})
    DEFAULT_SETTINGS_OUTPUT_MODE: str = "keep_both"
    DEFAULT_SETTINGS_DOLBY_CODEC: str = "eac3"
    DEFAULT_SETTINGS_DOLBY_BITRATE: str = "640k"
    ALLOWED_DOLBY_BITRATES: frozenset[str] = frozenset({"640k", "448k", "384k"})
    DEFAULT_STRIP_GLOBAL_TAGS: bool = False
    DEFAULT_REMOVE_ALL_METADATA: bool = True
    DEFAULT_CONFIRM_CANCEL_JOB: bool = True
    DEFAULT_CONFIRM_DELETE_FILE: bool = True
    DEFAULT_PREFERRED_LANGUAGE: str = "eng"
    DEFAULT_AUTO_CLEANUP_ENABLED: bool = True
    DEFAULT_AUTO_CLEANUP_INTERVAL_DAYS: int = 7
    ALLOWED_CLEANUP_INTERVAL_DAYS: frozenset[int] = frozenset({1, 7, 30})
    DEFAULT_JOB_HISTORY_RETENTION_DAYS: int = 30
    ALLOWED_JOB_RETENTION_DAYS: frozenset[int] = frozenset({0, 7, 30, 90})
    DEFAULT_AUTO_CLEANUP_TEMP: bool = True
    DEFAULT_AUTO_CLEANUP_JOBS: bool = True
    DEFAULT_AUTO_CLEANUP_OPTIMIZE_DB: bool = True

    # Phase 15 Global Playback Profile & 3-Step Onboarding constants
    PLAYBACK_PRESET_NEUTRAL: str = "neutral"
    PLAYBACK_PRESET_DOLBY_ARC: str = "dolby_arc"
    PLAYBACK_PRESET_OPTICAL_LEGACY: str = "optical_legacy"
    PLAYBACK_PRESET_EARC_AVR: str = "earc_avr"
    PLAYBACK_PRESET_STREAMING: str = "streaming_clients"
    PLAYBACK_PRESET_STEREO_MOBILE: str = "stereo_mobile"
    PLAYBACK_PRESET_CUSTOM: str = "custom"
    ALLOWED_PLAYBACK_PRESETS: frozenset[str] = frozenset(
        {
            "neutral",
            "dolby_arc",
            "optical_legacy",
            "earc_avr",
            "streaming_clients",
            "stereo_mobile",
            "custom",
        }
    )
    PLAYBACK_CODEC_KEYS: tuple[str, ...] = (
        "eac3",
        "ac3",
        "dts",
        "dts_hd_ma",
        "truehd",
        "aac_51",
        "aac_20",
        "opus",
        "flac",
        "pcm",
        "mp3",
        "alac",
    )
    ALLOWED_PLAYBACK_CODEC_KEYS: frozenset[str] = frozenset(PLAYBACK_CODEC_KEYS)
    ALLOWED_PLAYBACK_MAX_CHANNELS: frozenset[int] = frozenset({2, 6, 8})
    ALLOWED_PLAYBACK_PREFERRED_CODECS: frozenset[str] = frozenset(
        {"eac3", "ac3", "aac"}
    )
    DEFAULT_PLAYBACK_PROFILE_PRESET: str = "neutral"
    DEFAULT_PLAYBACK_MAX_CHANNELS: int = 6
    DEFAULT_PLAYBACK_PREFERRED_CODEC: str = "eac3"

    # Phase 11 maintenance & scheduled cleanup constants
    ORPHAN_UPLOAD_MIN_AGE_SEC: int = 3600
    MAINTENANCE_SCHEDULER_POLL_SEC: float = 300.0
    SECONDS_PER_DAY: int = 86400
    FACTORY_RESET_CONFIRM_TOKEN: str = "RESET"

    # Phase 3 processing constants
    DISK_SPACE_SIZE_MULTIPLIER: float = 1.1
    DISK_SPACE_BUFFER_BYTES: int = 100 * 1024 * 1024  # 100 MiB
    UNKNOWN_DURATION_AUDIO_ESTIMATE_BYTES: int = 500 * 1024 * 1024  # 500 MiB
    FFPROBE_TIMEOUT_SEC: float = 15.0
    FFMPEG_MAX_PROGRESS_PERCENT: int = 99
    MKVMERGE_EXIT_OK: int = 0
    MKVMERGE_EXIT_WARNINGS: int = 1
    MKVMERGE_EXIT_ERROR: int = 2
    TARGET_CODEC_EAC3: str = "eac3"
    TARGET_CODEC_AC3: str = "ac3"
    TARGET_CODEC_AAC: str = "aac"
    TARGET_CODEC_OPUS: str = "opus"
    TARGET_CODEC_FLAC: str = "flac"
    ALLOWED_TARGET_CODECS: frozenset[str] = frozenset(
        {"eac3", "ac3", "aac", "opus", "flac"}
    )
    LOSSLESS_TARGET_CODECS: frozenset[str] = frozenset({"flac"})
    LOSSLESS_SOURCE_CODECS: frozenset[str] = frozenset({"truehd", "flac", "pcm", "alac"})
    AUDIO_CODEC_FFMPEG_ENCODER_MAP: Mapping[str, str] = {
        "eac3": "eac3",
        "ac3": "ac3",
        "aac": "aac",
        "opus": "libopus",
        "flac": "flac",
    }
    AUDIO_CODEC_MAX_CHANNELS: Mapping[str, int] = {
        "eac3": 6,
        "ac3": 6,
        "aac": 8,
        "opus": 8,
        "flac": 8,
    }
    TEMP_AUDIO_EXT_MAP: Mapping[str, str] = {
        "eac3": "eac3",
        "ac3": "ac3",
        "aac": "mka",
        "opus": "mka",
        "flac": "mka",
    }
    DEFAULT_TARGET_CODEC: str = "eac3"
    DEFAULT_BITRATE: str = "640k"
    OUTPUT_MODE_REPLACE: str = "replace"
    OUTPUT_MODE_KEEP_BOTH: str = "keep_both"
    OUTPUT_MODE_SAVE_AS: str = "save_as"
    BITRATE_BPS_MAP: Mapping[str, int] = {
        "768k": 768_000,
        "640k": 640_000,
        "512k": 512_000,
        "448k": 448_000,
        "384k": 384_000,
        "320k": 320_000,
        "256k": 256_000,
        "192k": 192_000,
        "160k": 160_000,
        "128k": 128_000,
        "96k": 96_000,
        "64k": 64_000,
    }
    AUDIO_CODEC_ALLOWED_BITRATES: Mapping[str, frozenset[str]] = {
        "eac3": frozenset(
            {"96k", "128k", "192k", "256k", "320k", "384k", "448k", "640k", "768k"}
        ),
        "ac3": frozenset(
            {"96k", "128k", "192k", "256k", "320k", "384k", "448k", "640k"}
        ),
        "aac": frozenset(
            {"96k", "128k", "192k", "256k", "320k", "384k", "448k", "512k", "640k", "768k"}
        ),
        "opus": frozenset(
            {"64k", "96k", "128k", "160k", "192k", "256k", "320k", "384k", "448k", "512k"}
        ),
        "flac": frozenset(),
    }
    FLAC_ESTIMATED_SAMPLE_RATE: int = 48_000
    FLAC_ESTIMATED_BYTES_PER_SAMPLE: int = 3

    # Pagination, sorting & dashboard
    DEFAULT_PAGE: int = 1
    DEFAULT_PAGE_LIMIT: int = 50
    MIN_PAGE_LIMIT: int = 1
    MAX_PAGE_LIMIT: int = 200
    DEFAULT_RECENT_JOBS_LIMIT: int = 10
    DEFAULT_SORT_FIELD: str = "filename"
    DEFAULT_SORT_ORDER: str = "asc"
    ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
        {"filename", "folder", "size_bytes", "mtime", "scanned_at", "duration_us"}
    )
    ALLOWED_SORT_ORDERS: frozenset[str] = frozenset({"asc", "desc"})

    # SSE
    SSE_PING_INTERVAL_SEC: int = 15
    SSE_ACCEL_BUFFERING_HEADER: str = "X-Accel-Buffering"
    SSE_ACCEL_BUFFERING_VALUE: str = "no"
    SSE_SCAN_EVENT_NAME: str = "scan_progress"
    SSE_JOB_EVENT_NAME: str = "job_progress"
    SSE_LIBRARY_CHANGED_EVENT_NAME: str = "library_changed"
    LIBRARY_SUBSCRIBER_QUEUE_MAXSIZE: int = 64

    # Job queue & worker
    JOB_STATUS_QUEUED: str = "queued"
    JOB_STATUS_RUNNING: str = "running"
    JOB_STATUS_COMPLETED: str = "completed"
    JOB_STATUS_FAILED: str = "failed"
    JOB_STATUS_CANCELLED: str = "cancelled"
    JOB_STATUS_IDLE: str = "idle"
    JOB_LOG_BUFFER_MAX_LINES: int = 100
    JOB_SUBSCRIBER_QUEUE_MAXSIZE: int = 256
    JOB_HISTORY_LIMIT: int = 50
    SUBPROCESS_CANCEL_GRACE_SEC: float = 5.0
    CRASH_RECOVERY_ERROR_MESSAGE: str = "Interrupted by container restart"
    CANCELLED_BY_USER_MESSAGE: str = "Cancelled by user"

    # Scan statuses
    SCAN_STATUS_IDLE: str = "idle"
    SCAN_STATUS_RUNNING: str = "running"
    SCAN_STATUS_COMPLETED: str = "completed"
    SCAN_STATUS_FAILED: str = "failed"

    # Per-file two-phase indexing statuses (stored in media_files.scan_status)
    FILE_SCAN_STATUS_READY: str = "ready"
    FILE_SCAN_STATUS_SCANNING: str = "scanning"
    FILE_SCAN_STATUS_ERROR: str = "error"
    ALLOWED_FILE_SCAN_STATUSES: frozenset[str] = frozenset(
        {"ready", "scanning", "error"}
    )

    # Health statuses
    STATUS_OK: str = "ok"
    STATUS_DEGRADED: str = "degraded"

    # HTTP status codes
    HTTP_OK: int = 200
    HTTP_CREATED: int = 201
    HTTP_ACCEPTED: int = 202
    HTTP_BAD_REQUEST: int = 400
    HTTP_UNAUTHORIZED: int = 401
    HTTP_FORBIDDEN: int = 403
    HTTP_NOT_FOUND: int = 404
    HTTP_CONFLICT: int = 409
    HTTP_BAD_GATEWAY: int = 502
    HTTP_PAYLOAD_TOO_LARGE: int = 413
    HTTP_SERVICE_UNAVAILABLE: int = 503
    HTTP_GATEWAY_TIMEOUT: int = 504
    HTTP_INSUFFICIENT_STORAGE: int = 507
    EXIT_CODE_SUCCESS: int = 0

    # Phase 16: Speaker & Audio System Test Generator constants (zero magic numbers)
    TEST_MEDIA_OUTPUT_VIDEO: str = "video"
    TEST_MEDIA_OUTPUT_AUDIO: str = "audio"
    TEST_MEDIA_OUTPUT_MODES: frozenset[str] = frozenset({"video", "audio"})
    TEST_MEDIA_DEFAULT_INCLUDE_SUBTITLES: bool = True
    TEST_MEDIA_SIGNAL_TONES: str = "tones"
    TEST_MEDIA_SIGNAL_PINK: str = "pink_noise"
    TEST_MEDIA_SIGNAL_TYPES: frozenset[str] = frozenset({"tones", "pink_noise"})
    TEST_MEDIA_CODEC_PCM: str = "pcm"
    TEST_MEDIA_ALLOWED_CODECS: frozenset[str] = frozenset(
        {"eac3", "ac3", "aac", "opus", "flac", "pcm"}
    )
    TEST_MEDIA_LOSSLESS_CODECS: frozenset[str] = frozenset({"flac", "pcm"})
    TEST_MEDIA_ENCODER_MAP: Mapping[str, str] = {
        "eac3": "eac3",
        "ac3": "ac3",
        "aac": "aac",
        "opus": "libopus",
        "flac": "flac",
        "pcm": "pcm_s16le",
    }
    # Fixed per-encoder audio frame sizes for asetnsamples (prevents FFmpeg 5.x non-last-frame errors)
    TEST_MEDIA_ENCODER_FRAME_SIZE_MAP: Mapping[str, int] = {
        "eac3": 1536,
        "ac3": 1536,
        "aac": 1024,
        "opus": 960,
        "flac": 4096,
        "pcm": 1024,
    }
    TEST_MEDIA_CODEC_LABELS: Mapping[str, str] = {
        "eac3": "E-AC-3",
        "ac3": "AC-3",
        "aac": "AAC",
        "opus": "Opus",
        "flac": "FLAC",
        "pcm": "PCM (LPCM 16-bit)",
    }
    TEST_MEDIA_CODEC_MAX_CHANNELS: Mapping[str, int] = {
        "eac3": 6,
        "ac3": 6,
        "aac": 8,
        "opus": 8,
        "flac": 8,
        "pcm": 8,
    }
    TEST_MEDIA_LAYOUT_CHANNELS: Mapping[str, int] = {
        "1.0": 1,
        "2.0": 2,
        "2.1": 3,
        "5.1": 6,
        "7.1": 8,
    }
    TEST_MEDIA_ALLOWED_LAYOUTS: frozenset[str] = frozenset(
        {"1.0", "2.0", "2.1", "5.1", "7.1"}
    )
    TEST_MEDIA_FFMPEG_LAYOUT_MAP: Mapping[str, str] = {
        "1.0": "mono",
        "2.0": "stereo",
        "2.1": "2.1",
        "5.1": "5.1",
        "7.1": "7.1",
    }
    # Encoders that prefer canonical "5.1" vs "5.1(side)" explicitly documented here
    TEST_MEDIA_51_ENCODER_LAYOUT_MAP: Mapping[str, str] = {
        "eac3": "5.1",
        "ac3": "5.1",
        "aac": "5.1",
        "opus": "5.1",
        "flac": "5.1",
        "pcm": "5.1",
    }
    TEST_MEDIA_AUDIO_CONTAINERS_BY_CODEC: Mapping[str, tuple[str, ...]] = {
        "eac3": ("mka", "eac3"),
        "ac3": ("mka", "ac3"),
        "aac": ("mka", "m4a"),
        "opus": ("mka",),
        "flac": ("mka", "flac"),
        "pcm": ("mka", "wav"),
    }
    TEST_MEDIA_VIDEO_CONTAINER: str = "mkv"
    TEST_MEDIA_DEFAULT_DESTINATION_DIR: str = "/"
    TEST_MEDIA_TIMEOUT_SEC: float = 30.0
    TEST_MEDIA_SAMPLE_RATE: int = 48_000
    TEST_MEDIA_SLOT_DURATION_SEC: float = 2.0
    TEST_MEDIA_MIN_SLOT_DURATION_SEC: float = 0.5
    TEST_MEDIA_MAX_SLOT_DURATION_SEC: float = 10.0
    TEST_MEDIA_GAP_DURATION_SEC: float = 0.35
    TEST_MEDIA_MIN_GAP_DURATION_SEC: float = 0.0
    TEST_MEDIA_MAX_GAP_DURATION_SEC: float = 3.0
    TEST_MEDIA_ALL_CHANNEL_DURATION_SEC: float = 2.0
    TEST_MEDIA_FADE_DURATION_SEC: float = 0.025
    TEST_MEDIA_TONE_VOLUME_DB: int = -14
    TEST_MEDIA_LFE_VOLUME_DB: int = -18
    TEST_MEDIA_PINK_VOLUME_DB: int = -20
    TEST_MEDIA_ALL_CHANNEL_VOLUME_DB: int = -18
    TEST_MEDIA_LFE_FREQ_HZ: int = 60
    TEST_MEDIA_LFE_LOWPASS_HZ: int = 120
    TEST_MEDIA_ALL_CHANNEL_FREQ_HZ: int = 440
    TEST_MEDIA_VIDEO_WIDTH: int = 1280
    TEST_MEDIA_VIDEO_HEIGHT: int = 720
    TEST_MEDIA_VIDEO_FPS: int = 24
    TEST_MEDIA_VIDEO_CRF: str = "28"
    TEST_MEDIA_VIDEO_PRESET: str = "ultrafast"
    TEST_MEDIA_VIDEO_BG_COLOR: str = "0x09090b"
    TEST_MEDIA_BOX_W: int = 180
    TEST_MEDIA_BOX_H: int = 96
    TEST_MEDIA_BOX_DIM_COLOR: str = "0x27272a"
    TEST_MEDIA_BOX_ACTIVE_COLOR: str = "0x3b82f6"
    TEST_MEDIA_BOX_LFE_ACTIVE_COLOR: str = "0x10b981"
    TEST_MEDIA_MIN_FREE_BYTES: int = 50 * 1024 * 1024  # 50 MiB safety floor
    TEST_MEDIA_MAX_FILENAME_LEN: int = 120

    STATIC_ROUTE_PREFIX: str = "/static"
    STATIC_REVALIDATE_PREFIXES: tuple[str, ...] = ("/static/js/", "/static/css/")
    CACHE_CONTROL_HEADER: str = "Cache-Control"
    CACHE_CONTROL_NO_CACHE: str = "no-cache"
    INDEX_FILENAME: str = "index.html"
    APP_ICON_REL_PATH: str = "img/app-icon.svg"
    SVG_MEDIA_TYPE: str = "image/svg+xml"
    UTF8_ENCODING: str = "utf-8"

    VALID_LOG_LEVELS: frozenset[str] = frozenset(
        {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    )
    LOG_DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"
    LOG_CLI_TIME_FORMAT: str = "%H:%M:%S"
    LOG_SLOW_REQUEST_MS: float = 1000.0
    LOG_SLOW_FILES_QUERY_MS: float = 500.0
    LOG_REQUEST_ID_LENGTH: int = 8
    LOG_QUIET_PATHS: frozenset[str] = frozenset({"/api/health"})
    LOG_QUIET_PREFIXES: tuple[str, ...] = ("/static/",)
    LOG_UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")
    LOG_QUIET_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
        "watchfiles",
        "watchfiles.main",
        "watchfiles.watcher",
    )
    LOG_QUIET_THIRD_PARTY_LEVEL: str = "WARNING"


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings for MKV Forge."""

    storage_dir: Path
    data_dir: Path
    static_dir: Path
    health_timeout_sec: float
    scan_timeout_sec: float
    log_level: str
    watch_mode: str = AppConstants.DEFAULT_WATCH_MODE
    watch_debounce_sec: float = AppConstants.DEFAULT_WATCH_DEBOUNCE_SEC
    scan_interval_min: int = AppConstants.DEFAULT_SCAN_INTERVAL_MIN
    max_sub_upload_mb: int = AppConstants.DEFAULT_MAX_SUB_UPLOAD_MB
    max_audio_upload_mb: int = AppConstants.DEFAULT_MAX_AUDIO_UPLOAD_MB

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file inside data_dir."""
        return self.data_dir / AppConstants.DB_FILENAME

    @property
    def uploads_dir(self) -> Path:
        """Path to the staged uploads directory inside data_dir."""
        return self.data_dir / AppConstants.UPLOADS_SUBDIR_NAME


def resolve_app_version(env: Mapping[str, str] | None = None) -> str:
    """Return runtime application version from `MKVFORGE_APP_VERSION` or default."""
    source = os.environ if env is None else env
    raw = source.get(AppConstants.ENV_APP_VERSION)
    if raw is not None and raw.strip():
        return raw.strip()
    return AppConstants.DEFAULT_APP_VERSION


def _parse_bounded_float(
    raw_value: str | None,
    env_name: str,
    default_value: float,
    min_value: float,
    max_value: float,
) -> float:
    """Parse and validate a bounded float from an environment variable."""
    if raw_value is None or raw_value.strip() == "":
        return default_value

    try:
        parsed = float(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {env_name}: {raw_value!r}") from exc

    if not (min_value <= parsed <= max_value):
        raise ValueError(
            f"{env_name} must be between {min_value} and {max_value}, got {parsed}"
        )

    return parsed


def _parse_bounded_int(
    raw_value: str | None,
    env_name: str,
    default_value: int,
    min_value: int,
    max_value: int,
) -> int:
    """Parse and validate a bounded integer from an environment variable."""
    if raw_value is None or raw_value.strip() == "":
        return default_value
    try:
        parsed = int(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {env_name}: {raw_value!r}") from exc
    if not (min_value <= parsed <= max_value):
        raise ValueError(
            f"{env_name} must be between {min_value} and {max_value}, got {parsed}"
        )
    return parsed


def _parse_watch_mode(raw_value: str | None) -> str:
    """Parse and validate MKVFORGE_WATCH_MODE (`off` | `watch` | `poll`)."""
    if raw_value is None or raw_value.strip() == "":
        return AppConstants.DEFAULT_WATCH_MODE
    normalized = raw_value.strip().lower()
    if normalized not in AppConstants.VALID_WATCH_MODES:
        raise ValueError(
            f"Invalid {AppConstants.ENV_WATCH_MODE}: {raw_value!r}. "
            f"Expected one of {sorted(AppConstants.VALID_WATCH_MODES)}"
        )
    return normalized


def _parse_log_level(raw_value: str | None) -> str:
    """Parse and validate log level string."""
    if raw_value is None or raw_value.strip() == "":
        return AppConstants.DEFAULT_LOG_LEVEL

    normalized = raw_value.strip().upper()
    if normalized not in AppConstants.VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid {AppConstants.ENV_LOG_LEVEL}: {raw_value!r}. "
            f"Expected one of {sorted(AppConstants.VALID_LOG_LEVELS)}"
        )

    return normalized


def _parse_path(raw_value: str | None, default_path: str) -> Path:
    """Resolve a configured directory path without requiring it to exist yet."""
    candidate = raw_value.strip() if raw_value is not None else ""
    target = candidate if candidate else default_path
    return Path(target).expanduser()


def load_settings(
    env: Mapping[str, str] | None = None,
    static_dir: Path | None = None,
) -> Settings:
    """Load validated settings from environment variables."""
    source = os.environ if env is None else env
    resolved_static = (
        static_dir
        if static_dir is not None
        else Path(__file__).resolve().parent / "static"
    )

    return Settings(
        storage_dir=_parse_path(
            source.get(AppConstants.ENV_STORAGE_DIR),
            AppConstants.DEFAULT_STORAGE_DIR,
        ),
        data_dir=_parse_path(
            source.get(AppConstants.ENV_DATA_DIR),
            AppConstants.DEFAULT_DATA_DIR,
        ),
        static_dir=resolved_static,
        health_timeout_sec=_parse_bounded_float(
            source.get(AppConstants.ENV_HEALTH_TIMEOUT_SEC),
            AppConstants.ENV_HEALTH_TIMEOUT_SEC,
            AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
            AppConstants.MIN_HEALTH_TIMEOUT_SEC,
            AppConstants.MAX_HEALTH_TIMEOUT_SEC,
        ),
        scan_timeout_sec=_parse_bounded_float(
            source.get(AppConstants.ENV_SCAN_TIMEOUT_SEC),
            AppConstants.ENV_SCAN_TIMEOUT_SEC,
            AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
            AppConstants.MIN_SCAN_TIMEOUT_SEC,
            AppConstants.MAX_SCAN_TIMEOUT_SEC,
        ),
        log_level=_parse_log_level(source.get(AppConstants.ENV_LOG_LEVEL)),
        watch_mode=_parse_watch_mode(source.get(AppConstants.ENV_WATCH_MODE)),
        watch_debounce_sec=_parse_bounded_float(
            source.get(AppConstants.ENV_WATCH_DEBOUNCE_SEC),
            AppConstants.ENV_WATCH_DEBOUNCE_SEC,
            AppConstants.DEFAULT_WATCH_DEBOUNCE_SEC,
            AppConstants.MIN_WATCH_DEBOUNCE_SEC,
            AppConstants.MAX_WATCH_DEBOUNCE_SEC,
        ),
        scan_interval_min=_parse_bounded_int(
            source.get(AppConstants.ENV_SCAN_INTERVAL_MIN),
            AppConstants.ENV_SCAN_INTERVAL_MIN,
            AppConstants.DEFAULT_SCAN_INTERVAL_MIN,
            AppConstants.MIN_SCAN_INTERVAL_MIN,
            AppConstants.MAX_SCAN_INTERVAL_MIN,
        ),
        max_sub_upload_mb=_parse_bounded_int(
            source.get(AppConstants.ENV_MAX_SUB_UPLOAD_MB),
            AppConstants.ENV_MAX_SUB_UPLOAD_MB,
            AppConstants.DEFAULT_MAX_SUB_UPLOAD_MB,
            AppConstants.MIN_MAX_SUB_UPLOAD_MB,
            AppConstants.MAX_MAX_SUB_UPLOAD_MB,
        ),
        max_audio_upload_mb=_parse_bounded_int(
            source.get(AppConstants.ENV_MAX_AUDIO_UPLOAD_MB),
            AppConstants.ENV_MAX_AUDIO_UPLOAD_MB,
            AppConstants.DEFAULT_MAX_AUDIO_UPLOAD_MB,
            AppConstants.MIN_MAX_AUDIO_UPLOAD_MB,
            AppConstants.MAX_MAX_AUDIO_UPLOAD_MB,
        ),
    )
