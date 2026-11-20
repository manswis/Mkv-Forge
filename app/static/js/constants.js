/* Centralized frontend constants (zero magic numbers). */

window.MKVForgeConstants = Object.freeze({
  APP: Object.freeze({
    NAME: "MKV Forge",
    VERSION: "1.0.0",
    AUTHOR: "Manoj Biswas",
    GITHUB_URL: "https://github.com/manswis/Mkv-Forge",
    RELEASES_URL: "https://github.com/manswis/Mkv-Forge/releases",
    LICENSE: "MIT",
  }),

  ROUTES: Object.freeze({
    DASHBOARD: "dashboard",
    LIBRARY: "library",
    JOBS: "jobs",
    EDITOR: "files",
    SETTINGS: "settings",
  }),

  API: Object.freeze({
    HEALTH: "/api/health",
    AUTH: "/api/auth",
    DASHBOARD: "/api/dashboard",
    SETTINGS: "/api/settings",
    MAINTENANCE: "/api/maintenance",
    FILES: "/api/files",
    FOLDERS: "/api/folders",
    UPLOADS: "/api/uploads",
    UPLOADS_TRACK: "/api/uploads/track",
    SCAN: "/api/scan",
    SCAN_STREAM: "/api/scan/stream",
    JOBS: "/api/jobs",
    JOBS_STREAM: "/api/jobs/stream",
    EVENTS_STREAM: "/api/events/stream",
  }),

  AUTH_MODES: Object.freeze({
    UNCONFIGURED: "unconfigured",
    ANONYMOUS: "anonymous",
    LOCAL: "local",
  }),

  FILE_SCAN_STATUSES: Object.freeze({
    READY: "ready",
    SCANNING: "scanning",
    ERROR: "error",
  }),

  EXTERNAL_TRACKS: Object.freeze({
    KIND_AUDIO: "audio",
    KIND_SUBTITLES: "subtitles",
    SOURCE_STORAGE: "storage",
    SOURCE_UPLOAD: "upload",
    SUBTITLE_ACCEPT: ".srt,.ass,.ssa,.vtt,.sup",
    AUDIO_ACCEPT: ".ac3,.eac3,.aac,.mka,.dts,.flac,.mp3,.opus,.m4a",
    MAX_PER_JOB: 16,
    MIN_SYNC_DELAY_MS: -600000,
    MAX_SYNC_DELAY_MS: 600000,
  }),

  DEFAULT_USER_SETTINGS: Object.freeze({
    theme_mode: "dark",
    accent_color: "blue",
    font_scale: 100,
    language: "en",
    show_video_details: true,
    show_media_files_only: true,
    visible_media_extensions: ".mkv, .mp4",
    default_library_view: "dir",
    default_folder_layout: "list",
    page_limit: 50,
    default_output_mode: "keep_both",
    default_dolby_codec: "eac3",
    default_dolby_bitrate: "640k",
    default_strip_global_tags: false,
    default_remove_all_metadata: true,
    confirm_cancel_job: true,
    confirm_delete_file: true,
    preferred_language: "eng",
    auto_cleanup_enabled: true,
    auto_cleanup_interval_days: 7,
    job_history_retention_days: 30,
    auto_cleanup_temp: true,
    auto_cleanup_jobs: true,
    auto_cleanup_optimize_db: true,
  }),

  THEMES: Object.freeze({
    MODE_DARK: "dark",
    MODE_LIGHT: "light",
    MODE_SYSTEM: "system",
    DEFAULT_MODE: "dark",
    DEFAULT_ACCENT: "blue",
    DEFAULT_FONT_SCALE: 100,
    MIN_FONT_SCALE: 85,
    MAX_FONT_SCALE: 125,
    FONT_SCALE_STEP: 5,
    DEFAULT_LANGUAGE: "en",
    DARK_META_COLOR: "#09090B",
    LIGHT_META_COLOR: "#F4F4F5",
    MODES: Object.freeze([
      Object.freeze({ id: "dark", label: "Dark" }),
      Object.freeze({ id: "light", label: "Light" }),
      Object.freeze({ id: "system", label: "System" }),
    ]),
    ACCENTS: Object.freeze([
      Object.freeze({ id: "blue", label: "Blue", hex: "#3B82F6" }),
      Object.freeze({ id: "indigo", label: "Indigo", hex: "#6366F1" }),
      Object.freeze({ id: "emerald", label: "Emerald", hex: "#10B981" }),
      Object.freeze({ id: "violet", label: "Violet", hex: "#8B5CF6" }),
      Object.freeze({ id: "rose", label: "Rose", hex: "#F43F5E" }),
      Object.freeze({ id: "amber", label: "Amber", hex: "#F59E0B" }),
    ]),
    LANGUAGES: Object.freeze([
      Object.freeze({ code: "en", label: "English (Default)" }),
    ]),
  }),

  SETTINGS_SECTIONS: Object.freeze({
    APPEARANCE: "appearance",
    ACCOUNT: "account",
    AUDIO_TRACKS: "audio_tracks",
    OUTPUT_METADATA: "output_metadata",
    MAINTENANCE: "maintenance",
    AUTO_CLEANUP: "auto_cleanup",
    ABOUT: "about",
  }),

  SETTINGS_CATEGORIES: Object.freeze([
    Object.freeze({
      id: "preferences",
      title: "General & Display",
      sections: Object.freeze([
        Object.freeze({
          id: "appearance",
          title: "Appearance & Display",
          subtitle: "Theme, accent color, font scale & library layout",
          badgeClass: "border-indigo-500/35 bg-indigo-500/15 text-indigo-300",
        }),
        Object.freeze({
          id: "account",
          title: "Account & Sign-In",
          subtitle: "Local accounts, password & anonymous mode",
          badgeClass: "border-rose-500/35 bg-rose-500/15 text-rose-300",
        }),
      ]),
    }),
    Object.freeze({
      id: "remuxing",
      title: "Media & Remuxing",
      sections: Object.freeze([
        Object.freeze({
          id: "audio_tracks",
          title: "Audio & Track Defaults",
          subtitle: "Default Dolby codec, bitrate & track language",
          badgeClass: "border-emerald-500/35 bg-emerald-500/15 text-emerald-300",
        }),
        Object.freeze({
          id: "output_metadata",
          title: "Output & Metadata",
          subtitle: "Default output file behavior & global tag stripping",
          badgeClass: "border-sky-500/35 bg-sky-500/15 text-sky-300",
        }),
      ]),
    }),
    Object.freeze({
      id: "storage",
      title: "Storage & System",
      sections: Object.freeze([
        Object.freeze({
          id: "maintenance",
          title: "Storage & DB Cleanup",
          subtitle: "SQLite vacuum, temp files & one-tap cleanup",
          badgeClass: "border-amber-500/35 bg-amber-500/15 text-amber-300",
        }),
        Object.freeze({
          id: "auto_cleanup",
          title: "Scheduled Auto-Cleanup",
          subtitle: "Idle cleanup cadence & job history retention",
          badgeClass: "border-purple-500/35 bg-purple-500/15 text-purple-300",
        }),
      ]),
    }),
    Object.freeze({
      id: "legal",
      title: "About & Legal",
      sections: Object.freeze([
        Object.freeze({
          id: "about",
          title: "About & Legal Terms",
          subtitle: "Developer attribution, zero-liability & offline terms",
          badgeClass: "border-blue-500/35 bg-blue-500/15 text-blue-300",
        }),
      ]),
    }),
  ]),

  SETTINGS_OPTIONS: Object.freeze({
    PAGE_LIMITS: Object.freeze([25, 50, 100]),
    DOLBY_BITRATES: Object.freeze(["640k", "448k", "384k"]),
    CLEANUP_INTERVALS: Object.freeze([
      { days: 1, label: "Daily (1 day)" },
      { days: 7, label: "Weekly (7 days)" },
      { days: 30, label: "Monthly (30 days)" },
    ]),
    JOB_RETENTIONS: Object.freeze([
      { days: 0, label: "Never (keep all)" },
      { days: 7, label: "7 days" },
      { days: 30, label: "30 days" },
      { days: 90, label: "90 days" },
    ]),
  }),

  VIDEO_CODEC_TOOLTIPS: Object.freeze({
    HEVC: "HEVC / H.265",
    "H.264": "H.264 / AVC",
    AV1: "AV1",
    VP9: "VP9",
    "MPEG-2": "MPEG-2",
    "VC-1": "VC-1",
  }),

  FOLDER_TREE: Object.freeze({
    ROOT_PATH: "/",
    ROOT_LABEL: "/ (Storage Root)",
    BREADCRUMB_ROOT_LABEL: "Storage",
    BASE_INDENT_REM: 0.75,
    INDENT_STEP_REM: 1.15,
  }),

  SSE_EVENTS: Object.freeze({
    SCAN_PROGRESS: "scan_progress",
    JOB_PROGRESS: "job_progress",
    LIBRARY_CHANGED: "library_changed",
  }),

  UI: Object.freeze({
    SCROLL_TOP_THRESHOLD_PX: 80,
  }),

  PULL_REFRESH: Object.freeze({
    MOBILE_MAX_WIDTH_PX: 767,
    THRESHOLD_PX: 68,
    MAX_PULL_PX: 108,
    HOLD_HEIGHT_PX: 52,
    RESISTANCE: 0.48,
    MIN_START_DELTA_PX: 8,
    MIN_REFRESH_DISPLAY_MS: 350,
    MAX_ROTATION_DEG: 270,
  }),

  CONTEXT_MENU: Object.freeze({
    MENU_WIDTH_PX: 196,
    FILE_MENU_HEIGHT_PX: 260,
    FOLDER_MENU_HEIGHT_PX: 144,
    VIEWPORT_MARGIN_PX: 12,
    TRIGGER_GAP_PX: 6,
  }),

  LIBRARY_VIEWS: Object.freeze({
    DIR: "dir",
    LIST: "list",
  }),

  FOLDER_LAYOUTS: Object.freeze({
    GRID: "grid",
    LIST: "list",
  }),

  STORAGE_KEYS: Object.freeze({
    LIBRARY_VIEW: "mkvforge.libraryView",
    FOLDER_LAYOUT: "mkvforge.folderLayout",
    THEME_MODE: "mkvforge.themeMode",
    ACCENT_COLOR: "mkvforge.accentColor",
    FONT_SCALE: "mkvforge.fontScale",
    UI_LANGUAGE: "mkvforge.language",
  }),

  FILTERS: Object.freeze({
    ALL: "all",
    AAC51_ONLY: "aac51_only",
    AAC51: "aac51",
    HAS_METADATA: "has_metadata",
    NO_DOLBY: "no_dolby",
  }),

  SORT_FIELDS: Object.freeze({
    FILENAME: "filename",
    FOLDER: "folder",
    SIZE_BYTES: "size_bytes",
    DURATION_US: "duration_us",
  }),

  SORT_ORDERS: Object.freeze({
    ASC: "asc",
    DESC: "desc",
  }),

  EDITOR: Object.freeze({
    CODEC_EAC3: "eac3",
    CODEC_AC3: "ac3",
    CODEC_AAC: "aac",
    TARGET_CODEC_META: Object.freeze({
      eac3: Object.freeze({
        codec: "eac3",
        label: "EAC3",
        subtitle: "Dolby Digital Plus",
        bitrates: Object.freeze(["640k", "448k", "384k"]),
        defaultBitrate: "640k",
      }),
      ac3: Object.freeze({
        codec: "ac3",
        label: "AC3",
        subtitle: "Dolby Digital (Legacy Compatible)",
        bitrates: Object.freeze(["640k", "448k", "384k"]),
        defaultBitrate: "448k",
      }),
      aac: Object.freeze({
        codec: "aac",
        label: "AAC",
        subtitle: "Universal Mobile & Web",
        bitrates: Object.freeze(["320k", "256k", "192k", "128k"]),
        defaultBitrate: "320k",
      }),
    }),
    LOGICAL_TARGET_MAP: Object.freeze({
      aac: Object.freeze(["eac3", "ac3"]),
      eac3: Object.freeze(["ac3", "aac"]),
      ac3: Object.freeze(["aac"]),
      default: Object.freeze(["eac3", "ac3", "aac"]),
    }),
    BITRATES: Object.freeze(["640k", "448k", "384k", "320k", "256k", "192k", "128k"]),
    DEFAULT_BITRATE: "640k",
    OUTPUT_REPLACE: "replace",
    OUTPUT_KEEP_BOTH: "keep_both",
    OUTPUT_SAVE_AS: "save_as",
    CUSTOM_FOLDER_SENTINEL: "__custom__",
    TEMP_FILE_MARKER: ".mkvforge.",
    MAX_DOLBY_CHANNELS: 6,
    DEFAULT_LANGUAGE: "und",
    LOG_AUTOSCROLL_THRESHOLD_PX: 24,
    MAX_CLIENT_LOG_LINES: 200,
    SUPPORTED_TRACK_TYPES: Object.freeze(["video", "audio", "subtitles"]),
    LANGUAGE_OPTIONS: Object.freeze([
      { code: "eng", label: "English (eng)" },
      { code: "hin", label: "Hindi (hin)" },
      { code: "ben", label: "Bengali (ben)" },
      { code: "tam", label: "Tamil (tam)" },
      { code: "tel", label: "Telugu (tel)" },
      { code: "mal", label: "Malayalam (mal)" },
      { code: "kan", label: "Kannada (kan)" },
      { code: "mar", label: "Marathi (mar)" },
      { code: "pan", label: "Punjabi (pan)" },
      { code: "jpn", label: "Japanese (jpn)" },
      { code: "kor", label: "Korean (kor)" },
      { code: "spa", label: "Spanish (spa)" },
      { code: "fra", label: "French (fra)" },
      { code: "deu", label: "German (deu)" },
      { code: "ita", label: "Italian (ita)" },
      { code: "rus", label: "Russian (rus)" },
      { code: "zho", label: "Chinese (zho)" },
      { code: "ara", label: "Arabic (ara)" },
      { code: "por", label: "Portuguese (por)" },
      { code: "und", label: "Undetermined (und)" },
    ]),
    LANGUAGE_DISPLAY_NAMES: Object.freeze({
      eng: "English",
      hin: "Hindi",
      ben: "Bengali",
      tam: "Tamil",
      tel: "Telugu",
      mal: "Malayalam",
      kan: "Kannada",
      mar: "Marathi",
      pan: "Punjabi",
      jpn: "Japanese",
      kor: "Korean",
      spa: "Spanish",
      fra: "French",
      deu: "German",
      ita: "Italian",
      rus: "Russian",
      zho: "Chinese",
      ara: "Arabic",
      por: "Portuguese",
    }),
  }),

  TIMINGS_MS: Object.freeze({
    SEARCH_DEBOUNCE: 300,
    TOAST_AUTO_DISMISS: 3500,
    SSE_RECONNECT_BASE: 2000,
    SSE_RECONNECT_MAX: 15000,
    SSE_CONNECT_DELAY: 150,
    SSE_HIDDEN_CLOSE_DELAY: 3000,
    REQUEST_TIMEOUT: 15000,
  }),

  TOAST: Object.freeze({
    TYPE_SUCCESS: "success",
    TYPE_ERROR: "error",
    TYPE_INFO: "info",
    TYPES: Object.freeze(["success", "error", "info"]),
    MAX_VISIBLE: 4,
    DURATION_MS: Object.freeze({
      success: 3500,
      info: 4000,
      error: 6500,
    }),
  }),

  JOB_UI: Object.freeze({
    MIN_PROGRESS_PCT: 0,
    MAX_PROGRESS_PCT: 100,
    INDETERMINATE_FLOOR_PCT: 4,
  }),

  PAGINATION: Object.freeze({
    DEFAULT_PAGE: 1,
    DEFAULT_LIMIT: 50,
  }),

  UNITS: Object.freeze({
    BYTES_PER_KB: 1024,
    BPS_PER_KBPS: 1000,
    BPS_PER_MBPS: 1_000_000,
    HZ_PER_KHZ: 1000,
    US_PER_SEC: 1_000_000,
    SEC_PER_MIN: 60,
    SEC_PER_HOUR: 3600,
    SEC_PER_DAY: 86400,
    MS_PER_SEC: 1000,
    HTTP_NOT_FOUND: 404,
    HTTP_CONFLICT: 409,
  }),

  STATUSES: Object.freeze({
    IDLE: "idle",
    QUEUED: "queued",
    RUNNING: "running",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
  }),
});
