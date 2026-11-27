"""Application configuration and centralized constants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class AppConstants:
    """Centralized immutable constants to eliminate magic values."""

    APP_NAME: str = "MKV Forge"
    DEFAULT_APP_VERSION: str = "1.0.8"
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
    ENV_SCRATCH_DIR: str = "MKVFORGE_SCRATCH_DIR"
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
    WATCH_PROBE_CONCURRENCY: int = 1
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
    TOOL_MKVPROPEDIT: str = "mkvpropedit"
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
    TEMP_CHAPTERS_SUFFIX: str = ".mkvforge.chapters.xml"
    KEEP_BOTH_INFIX: str = ".remux"
    MACOS_APPLEDOUBLE_PREFIX: str = "._"
    MAX_KEEP_BOTH_COLLISIONS: int = 10_000
    MAX_DUPLICATE_COLLISIONS: int = 10_000
    MTIME_TOLERANCE_SEC: float = 1e-6

    # Chapters Editor & On-Demand Scene Frame Extraction
    CHAPTER_ACTION_KEEP: str = "keep"
    CHAPTER_ACTION_REPLACE: str = "replace"
    CHAPTER_ACTION_CLEAR: str = "clear"
    ALLOWED_CHAPTER_ACTIONS: tuple[str, ...] = ("keep", "replace", "clear")
    CHAPTER_MAX_COUNT: int = 500
    CHAPTER_MAX_TITLE_LEN: int = 200
    CHAPTER_PARSE_MAX_BYTES: int = 512 * 1024
    CHAPTER_EXTRACT_TIMEOUT_SEC: float = 10.0
    CHAPTER_QUICK_INTERVALS_MIN: tuple[int, ...] = (5, 10, 15)
    CHAPTER_DEFAULT_LANGUAGE: str = "und"
    CHAPTER_EDITION_UID: int = 1
    CHAPTER_ATOM_UID_BASE: int = 1_000_001

    SCENE_FRAME_MODE_FAST: str = "fast"
    SCENE_FRAME_MODE_ACCURATE: str = "accurate"
    ALLOWED_SCENE_FRAME_MODES: tuple[str, ...] = ("fast", "accurate")
    SCENE_FRAME_SCAN_DENSITY_PRESETS: tuple[int, ...] = (12, 24, 40)
    SCENE_FRAME_SCAN_DEFAULT_DENSITY: int = 24
    SCENE_FRAME_PEEK_OFFSET_SEC: int = 15
    SCENE_FRAME_FAST_WIDTH_PX: int = 320
    SCENE_FRAME_ACCURATE_WIDTH_PX: int = 480
    SCENE_FRAME_ACCURATE_PRESEEK_SEC: float = 10.0
    SCENE_FRAME_EOF_MARGIN_MS: int = 500
    SCENE_FRAME_FAST_QUALITY: int = 5
    SCENE_FRAME_ACCURATE_QUALITY: int = 4
    SCENE_FRAME_TIMEOUT_SEC: float = 10.0
    SCENE_FRAME_DISCONNECT_POLL_SEC: float = 0.15
    SCENE_FRAME_TERMINATE_GRACE_SEC: float = 0.5
    SCENE_FRAME_MAX_CONCURRENCY: int = 1
    SCENE_FRAME_HTTP_CACHE_MAX_AGE_SEC: int = 86_400

    # Time & media parsing constants
    NS_PER_US: int = 1_000
    NS_PER_MS: int = 1_000_000
    US_PER_MS: int = 1_000
    US_PER_SEC: int = 1_000_000
    MS_PER_SEC: int = 1_000
    SEC_PER_MIN: int = 60
    SEC_PER_HOUR: int = 3_600
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
    AUDIO_DYNAMICS_ORIGINAL: str = "original"
    AUDIO_DYNAMICS_DIALOGUE_BOOST: str = "dialogue_boost"
    AUDIO_DYNAMICS_NIGHT_MODE: str = "night_mode"
    DEFAULT_AUDIO_DYNAMICS: str = "original"
    ALLOWED_AUDIO_DYNAMICS: frozenset[str] = frozenset(
        {"original", "dialogue_boost", "night_mode"}
    )
    AUDIO_DYNAMICS_DOWNMIX_PREFIX: str = (
        "aresample=center_mix_level=1.414:ochl=stereo"
    )
    AUDIO_DYNAMICS_DIALOGUE_FILTER: str = "loudnorm=I=-16:TP=-1.5:LRA=11"
    AUDIO_DYNAMICS_NIGHT_FILTER: str = "dynaudnorm=f=150:g=15:p=0.9:m=10:s=12"
    MKVPROPEDIT_TRACK_TYPE_PREFIX: Mapping[str, str] = {
        "video": "v",
        "audio": "a",
        "subtitles": "s",
    }
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
            "processing_priority_mode",
            "stage1_scratch_mode",
            "stage1_scratch_storage_folder",
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
            "default_audio_codec",
            "default_audio_quality",
            "default_dolby_codec",
            "default_dolby_bitrate",
            "default_strip_global_tags",
            "default_remove_all_metadata",
            "confirm_cancel_job",
            "confirm_delete_file",
            "preferred_language",
            "smart_fill_track_format",
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
    THUMBNAIL_JPEG_PIXEL_FORMAT: str = "yuvj420p"
    THUMBNAIL_CACHE_CONTROL_HEADER: str = "public, max-age=31536000, immutable"
    DOWNLOAD_ACCEL_BUFFERING_HEADER: str = "X-Accel-Buffering"
    DOWNLOAD_ACCEL_BUFFERING_VALUE: str = "no"
    DOWNLOAD_CACHE_CONTROL_HEADER: str = "no-store"
    IMAGE_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
        {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    OTHER_FILE_AUDIO_ART_EXTENSIONS: frozenset[str] = frozenset(
        {
            ".opus",
            ".mp3",
            ".flac",
            ".m4a",
            ".mp4a",
            ".ogg",
            ".oga",
            ".mka",
            ".wma",
            ".wav",
            ".aif",
            ".aiff",
            ".wv",
            ".ape",
            ".alac",
        }
    )
    OTHER_FILE_VIDEO_ART_EXTENSIONS: frozenset[str] = frozenset(
        {".webm", ".mov", ".avi", ".ts", ".m4v"}
    )
    OTHER_FILE_THUMBNAIL_EXTENSIONS: frozenset[str] = (
        IMAGE_ATTACHMENT_EXTENSIONS
        | OTHER_FILE_AUDIO_ART_EXTENSIONS
        | OTHER_FILE_VIDEO_ART_EXTENSIONS
    )
    OTHER_THUMBNAIL_HASH_LEN: int = 16
    OTHER_THUMBNAIL_DIRECT_IMAGE_MAX_BYTES: int = 10 * 1024 * 1024
    OTHER_THUMBNAIL_DOWNSCALE_THRESHOLD_BYTES: int = 256 * 1024
    OTHER_THUMBNAIL_OGG_MAX_READ_BYTES: int = 4 * 1024 * 1024
    OTHER_THUMBNAIL_OGG_MAX_PAGES: int = 1024
    THUMBNAIL_NEGATIVE_MARKER_BYTES: bytes = b"NONE"
    FLAC_PICTURE_TYPE_FRONT_COVER: int = 3
    FLAC_BLOCK_TYPE_PICTURE: int = 6
    DEFAULT_SHOW_MEDIA_FILES_ONLY: bool = True
    DEFAULT_VISIBLE_MEDIA_EXTENSIONS: str = ".mkv, .mp4"
    MAX_VISIBLE_EXTENSIONS_LEN: int = 256
    DEFAULT_LIBRARY_VIEW: str = "dir"
    DEFAULT_FOLDER_LAYOUT: str = "list"
    DEFAULT_SETTINGS_PAGE_LIMIT: int = 50
    ALLOWED_SETTINGS_PAGE_LIMITS: frozenset[int] = frozenset({25, 50, 100})
    DEFAULT_SETTINGS_OUTPUT_MODE: str = "keep_both"
    AUDIO_CODEC_AUTO: str = "auto"
    DEFAULT_AUDIO_CODEC: str = "auto"
    ALLOWED_DEFAULT_AUDIO_CODECS: frozenset[str] = frozenset(
        {"auto", "eac3", "ac3", "aac", "opus", "flac"}
    )
    AUDIO_QUALITY_HIGH: str = "high"
    AUDIO_QUALITY_BALANCED: str = "balanced"
    AUDIO_QUALITY_COMPACT: str = "compact"
    DEFAULT_AUDIO_QUALITY: str = "high"
    ALLOWED_AUDIO_QUALITIES: frozenset[str] = frozenset(
        {"high", "balanced", "compact"}
    )
    LEGACY_DOLBY_BITRATE_TO_QUALITY: Mapping[str, str] = {
        "640k": "high",
        "448k": "balanced",
        "384k": "compact",
    }
    AUDIO_QUALITY_TO_LEGACY_DOLBY_BITRATE: Mapping[str, str] = {
        "high": "640k",
        "balanced": "448k",
        "compact": "384k",
    }
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

    # Smart Fill Track Format presets (Per-User; 'detailed' preserves original behavior)
    SMART_FILL_FORMAT_DETAILED: str = "detailed"
    SMART_FILL_FORMAT_COMPACT: str = "compact"
    SMART_FILL_FORMAT_LANG_CODEC: str = "lang_codec"
    DEFAULT_SMART_FILL_TRACK_FORMAT: str = "detailed"
    ALLOWED_SMART_FILL_TRACK_FORMATS: frozenset[str] = frozenset(
        {"detailed", "compact", "lang_codec"}
    )

    # Processing CPU / I/O Priority & Stage 1 Scratch Directory (System-Wide)
    PROCESSING_PRIORITY_AUTO: str = "auto"
    PROCESSING_PRIORITY_HALF_CORES: str = "half_cores"
    PROCESSING_PRIORITY_LOW: str = "low_priority"
    DEFAULT_PROCESSING_PRIORITY_MODE: str = "auto"
    ALLOWED_PROCESSING_PRIORITY_MODES: frozenset[str] = frozenset(
        {"auto", "half_cores", "low_priority"}
    )
    HALF_CORES_NICE_INCREMENT: int = 10
    LOW_PRIORITY_NICE_INCREMENT: int = 15
    LOW_PRIORITY_MAX_THREADS: int = 2
    MIN_FFMPEG_THREADS: int = 1
    IONICE_TOOL: str = "ionice"
    IONICE_IDLE_CLASS_ARGS: tuple[str, ...] = ("-t", "-c", "3")

    STAGE1_SCRATCH_SOURCE: str = "source"
    STAGE1_SCRATCH_TMP: str = "tmp"
    STAGE1_SCRATCH_DATA: str = "data"
    STAGE1_SCRATCH_STORAGE_FOLDER: str = "storage_folder"
    STAGE1_SCRATCH_CUSTOM: str = "custom"
    DEFAULT_STAGE1_SCRATCH_MODE: str = "source"
    ALLOWED_STAGE1_SCRATCH_MODES: frozenset[str] = frozenset(
        {"source", "tmp", "data", "storage_folder", "custom"}
    )
    SCRATCH_SUBDIR_NAME: str = "scratch"
    TMP_SCRATCH_DIR_NAME: str = "mkvforge_scratch"
    SCRATCH_PATH_HASH_LEN: int = 8
    MAX_SCRATCH_STORAGE_FOLDER_LEN: int = 512
    PROC_MOUNTS_PATH: str = "/proc/mounts"
    TMPFS_FS_TYPES: frozenset[str] = frozenset({"tmpfs", "ramfs"})
    SCRATCH_WRITE_PROBE_PREFIX: str = ".mkvforge_scratch_probe_"

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
        {"eac3", "ac3", "aac", "opus", "flac"}
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
    # Full-channel quality-tier bitrate matrix (codec -> channel bucket -> quality tier -> bitrate)
    # Every value is strictly within AUDIO_CODEC_ALLOWED_BITRATES and obeys codec channel/mono caps.
    AUDIO_QUALITY_BITRATE_MATRIX: Mapping[str, Mapping[int, Mapping[str, str]]] = {
        "eac3": {
            1: {"high": "192k", "balanced": "128k", "compact": "96k"},
            2: {"high": "256k", "balanced": "192k", "compact": "128k"},
            3: {"high": "448k", "balanced": "384k", "compact": "256k"},
            4: {"high": "448k", "balanced": "384k", "compact": "256k"},
            6: {"high": "640k", "balanced": "448k", "compact": "384k"},
        },
        "ac3": {
            1: {"high": "192k", "balanced": "128k", "compact": "96k"},
            2: {"high": "256k", "balanced": "192k", "compact": "128k"},
            3: {"high": "448k", "balanced": "384k", "compact": "256k"},
            4: {"high": "448k", "balanced": "384k", "compact": "256k"},
            6: {"high": "640k", "balanced": "448k", "compact": "384k"},
        },
        "aac": {
            1: {"high": "192k", "balanced": "128k", "compact": "96k"},
            2: {"high": "256k", "balanced": "192k", "compact": "128k"},
            3: {"high": "384k", "balanced": "320k", "compact": "256k"},
            4: {"high": "384k", "balanced": "320k", "compact": "256k"},
            6: {"high": "448k", "balanced": "384k", "compact": "256k"},
            7: {"high": "512k", "balanced": "448k", "compact": "320k"},
            8: {"high": "640k", "balanced": "512k", "compact": "384k"},
        },
        "opus": {
            1: {"high": "128k", "balanced": "96k", "compact": "64k"},
            2: {"high": "192k", "balanced": "128k", "compact": "96k"},
            3: {"high": "320k", "balanced": "256k", "compact": "192k"},
            4: {"high": "320k", "balanced": "256k", "compact": "192k"},
            6: {"high": "320k", "balanced": "256k", "compact": "192k"},
            7: {"high": "384k", "balanced": "320k", "compact": "256k"},
            8: {"high": "448k", "balanced": "320k", "compact": "256k"},
        },
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
    FOLDER_RECONCILE_CACHE_TTL_SEC: float = 2.0
    FOLDER_RECONCILE_CACHE_MAX_ENTRIES: int = 128

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
    JOB_PROGRESS_DB_THROTTLE_SEC: float = 0.75
    JOB_PROGRESS_DB_MIN_DELTA_PCT: int = 5
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

    # Phase 18: Batch Processing & Reusable Templates constants (zero magic numbers)
    BATCH_MAX_FILES_PER_REQUEST: int = 500
    BATCH_ID_PREFIX: str = "batch_"
    BATCH_ID_HEX_LEN: int = 10
    TEMPLATE_MIN_NAME_LEN: int = 1
    TEMPLATE_MAX_NAME_LEN: int = 64
    TEMPLATE_MAX_DESCRIPTION_LEN: int = 200
    BATCH_TITLE_ACTION_CLEAR: str = "clear"
    BATCH_TITLE_ACTION_KEEP: str = "keep"
    BATCH_AUDIO_LANG_MODE_PREFERRED: str = "preferred"
    BATCH_AUDIO_LANG_MODE_FIRST: str = "first_audio"
    BATCH_AUDIO_LANG_MODE_CUSTOM: str = "custom"
    BATCH_MATCH_INCOMPATIBLE_OR_NON_DOLBY: str = "incompatible_or_non_dolby"
    BATCH_MATCH_AAC51_ONLY: str = "aac_51_only"
    BATCH_MATCH_ANY_NON_TARGET: str = "any_non_target"
    BATCH_KEEP_TRACKS_ALL: str = "all"
    BATCH_KEEP_TRACKS_PREFERRED: str = "preferred_only"
    BATCH_KEEP_SUBS_NONE: str = "none"
    BATCH_PLAN_ACTION_ENQUEUE: str = "enqueue"
    BATCH_PLAN_ACTION_SKIP_COMPLIANT: str = "skip_already_compliant"
    BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB: str = "skip_active_job"
    BATCH_PLAN_ACTION_SKIP_NOT_READY: str = "skip_not_ready"
    BATCH_PLAN_ACTION_SKIP_WARNING: str = "skip_warning"
    BATCH_STAGE_TRANSCODE_AND_REMUX: str = "transcode_and_remux"
    BATCH_STAGE_REMUX_ONLY: str = "remux_only"
    BATCH_STAGE_NONE: str = "none"
    BATCH_CHIP_CAT_VIDEO: str = "video"
    BATCH_CHIP_CAT_AUDIO: str = "audio"
    BATCH_CHIP_CAT_SUBTITLE: str = "subtitle"
    BATCH_CHIP_CAT_TAG: str = "tag"
    BATCH_OVERRIDE_TRACK_ENABLED: str = "track_enabled"
    BATCH_OVERRIDE_AUDIO_CONVERT: str = "audio_convert_enabled"
    BATCH_OVERRIDE_RETAG_UND: str = "retag_und_enabled"
    BATCH_OVERRIDE_CLEAR_TITLE: str = "clear_title"
    BATCH_OVERRIDE_STRIP_GLOBAL_TAGS: str = "strip_global_tags"
    BATCH_OVERRIDE_STRIP_TRACK_TAGS: str = "strip_track_tags"
    BATCH_OVERRIDE_STRIP_ATTACHMENTS: str = "strip_attachments"
    BATCH_OVERRIDE_STRIP_CHAPTERS: str = "strip_chapters"
    BATCH_OVERRIDE_SMART_FILL_NAMES: str = "smart_fill_track_names"
    BATCH_OVERRIDE_OUTPUT_FILENAME: str = "output_filename"
    BATCH_RENAME_START_DEFAULT: int = 1
    BATCH_RENAME_MIN_START: int = 0
    BATCH_RENAME_MAX_START: int = 9999
    BATCH_RENAME_MAX_PATTERN_LEN: int = 180
    BATCH_RENAME_MAX_FILENAME_LEN: int = 220
    BATCH_RENAME_PAD_DIGITS_2: int = 2
    BATCH_RENAME_PAD_DIGITS_3: int = 3
    BATCH_RENAME_PAD_THRESHOLD_3: int = 100
    ISO_LANG_ALIAS_MAP: Mapping[str, str] = {
        "en": "eng",
        "eng": "eng",
        "hi": "hin",
        "hin": "hin",
        "bn": "ben",
        "ben": "ben",
        "ja": "jpn",
        "jpn": "jpn",
        "es": "spa",
        "spa": "spa",
        "fr": "fra",
        "fra": "fra",
        "fre": "fra",
        "de": "deu",
        "deu": "deu",
        "ger": "deu",
        "ko": "kor",
        "kor": "kor",
        "zh": "zho",
        "zho": "zho",
        "chi": "zho",
        "it": "ita",
        "ita": "ita",
        "ru": "rus",
        "rus": "rus",
        "pt": "por",
        "por": "por",
        "ta": "tam",
        "tam": "tam",
        "te": "tel",
        "tel": "tel",
        "ml": "mal",
        "mal": "mal",
        "kn": "kan",
        "kan": "kan",
        "mr": "mar",
        "mar": "mar",
        "ar": "ara",
        "ara": "ara",
        "und": "und",
    }

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
    scratch_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file inside data_dir."""
        return self.data_dir / AppConstants.DB_FILENAME

    @property
    def uploads_dir(self) -> Path:
        """Path to the staged uploads directory inside data_dir."""
        return self.data_dir / AppConstants.UPLOADS_SUBDIR_NAME

    @property
    def data_scratch_dir(self) -> Path:
        """Path to the managed Stage 1 scratch directory inside data_dir."""
        return self.data_dir / AppConstants.SCRATCH_SUBDIR_NAME


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
        scratch_dir=(
            Path(raw_scratch).expanduser()
            if (raw_scratch := (source.get(AppConstants.ENV_SCRATCH_DIR) or "").strip())
            else None
        ),
    )
