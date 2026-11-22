/* Alpine.js application controller for MKV Forge (Phases 5 & 6). */

(function attachAppController(global) {
  const C = global.MKVForgeConstants;
  const Api = global.MKVForgeApi;
  const Fmt = global.MKVForgeFormatters;
  const Ed = global.MKVForgeEditor;
  const FT = global.MKVForgeFolderTree;
  const TS = global.MKVForgeToastStack;
  const PP = global.MKVForgePlaybackProfile || {
    PRESETS: [],
    CODEC_OPTIONS: [],
    CHANNEL_CAP_OPTIONS: [],
    PREFERRED_TARGET_OPTIONS: [],
    getPresetById: () => null,
    createProfileDraftFromSettings: () => ({
      preset: "neutral",
      supportedCodecs: [],
      maxChannels: 6,
      preferredCodec: "eac3",
      customOpen: false,
    }),
    applyPresetToDraft: (d) => d,
    toggleCodecInDraft: (d) => d,
    formatChannelCapLabel: () => "5.1",
    formatProfileSummary: () => "Not configured",
    buildSettingsPatchFromDraft: () => ({
      playback_profile_preset: "neutral",
      playback_supported_codecs: [],
    }),
  };
  const Charts = global.MKVForgeCharts || {
    buildPieSlices: () => [],
    renderPieSvg: () => "",
    patchPieSvg: () => false,
  };
  const TM = global.MKVForgeTestMedia || {
    createTestMediaDraft: () => ({
      outputMode: "video",
      codec: "eac3",
      layout: "5.1",
      container: "mkv",
      bitrate: "640k",
      signalType: "tones",
      destinationDir: "/",
      filename: "MKVForge_Test_EAC3_5.1.mkv",
      filenameEdited: false,
      advancedOpen: false,
    }),
    applyTestMediaPreset: (d) => d,
    buildDefaultTestFilename: () => "MKVForge_Test_EAC3_5.1.mkv",
    getAllowedContainers: () => ["mkv"],
    getBitrateConfig: () => ({ options: ["640k"], defaultBitrate: "640k" }),
    resolveProfileRecommendedSpec: () => null,
    detectMatchingPresetId: () => null,
    evaluateTestMediaConfig: () => ({
      valid: true,
      blockReason: null,
      warnings: [],
      summary: "",
      effectiveContainer: "mkv",
      effectiveBitrate: "640k",
      channels: 6,
      estimatedDurationSec: 16.5,
    }),
    buildTestMediaPayload: (d) => d,
  };
  const Theme = global.MKVForgeTheme || {
    normalizeThemeMode: (m) => m || "dark",
    normalizeAccentColor: (a) => a || "blue",
    normalizeFontScale: (s) => Number(s) || 100,
    resolveEffectiveTheme: (m) => (m === "light" ? "light" : "dark"),
    loadStoredThemePrefs: () => ({ mode: "dark", accent: "blue", fontScale: 100 }),
    applyFontScale: (s) => Number(s) || 100,
    applyTheme: () => ({ mode: "dark", accent: "blue", effective: "dark" }),
    bindSystemThemeListener: () => null,
  };
  const I18n = global.MKVForgeI18n || {
    DEFAULT_LOCALE: "en",
    normalizeLocale: () => "en",
    t: (key, _loc, fallback) => fallback || key,
  };
  const PR = global.MKVForgePullRefresh || {
    createPullRefreshState: () => ({
      tracking: false,
      pulling: false,
      refreshing: false,
      startX: 0,
      startY: 0,
      distancePx: 0,
      progress: 0,
      ready: false,
      rotationDeg: 0,
    }),
    isInteractiveOrScrollableTarget: () => false,
    shouldStartPull: () => false,
    shouldCancelForHorizontal: () => false,
    computePullMetrics: () => ({
      distancePx: 0,
      progress: 0,
      ready: false,
      rotationDeg: 0,
    }),
  };
  const LR = global.MKVForgeLibraryReturn || {
    normalizeOffset: (v) => {
      const n = Number(v);
      return Number.isFinite(n) && n >= 0 ? Math.round(n) : 0;
    },
    normalizeHash: (h) => (typeof h === "string" ? h.trim() : ""),
    isLibraryHash: (h) =>
      typeof h === "string" &&
      (h.trim() === "#/library" || h.trim().startsWith("#/library?")),
    hashesMatch: (a, b) =>
      typeof a === "string" && typeof b === "string" && a.trim() === b.trim(),
    createOriginSnapshot: (input) =>
      input && input.hash
        ? {
            hash: String(input.hash).trim(),
            page: Math.max(1, parseInt(String(input.page || 1), 10) || 1),
            view:
              input.view === C.LIBRARY_VIEWS.LIST
                ? C.LIBRARY_VIEWS.LIST
                : C.LIBRARY_VIEWS.DIR,
            folder: input.folder || C.FOLDER_TREE.ROOT_PATH,
            scrollTop: Math.max(0, Math.round(Number(input.scrollTop) || 0)),
            windowScrollY: Math.max(
              0,
              Math.round(Number(input.windowScrollY) || 0)
            ),
            fileId: input.fileId || null,
          }
        : null,
    saveOrigin: (snapshot) => snapshot || null,
    loadOrigin: () => null,
    clearOrigin: () => {},
    applyScrollPosition: ({ container, win, scrollTop, windowScrollY } = {}) => {
      if (container) container.scrollTop = Math.max(0, Number(scrollTop) || 0);
      if (win && typeof win.scrollTo === "function") {
        win.scrollTo(0, Math.max(0, Number(windowScrollY) || 0));
      }
      return { appliedContainer: Boolean(container), appliedWindow: Boolean(win), needsRetry: false };
    },
  };

  const VALID_SORTS = new Set(Object.values(C.SORT_FIELDS));
  const VALID_ORDERS = new Set(Object.values(C.SORT_ORDERS));

  function parseRouteFromHash(rawHash) {
    const cleaned = String(rawHash || "").replace(/^#\/?/, "");
    const [pathPart, queryPart = ""] = cleaned.split("?");
    const parts = (pathPart || "").split("/").filter(Boolean);
    const segment = (parts[0] || "").toLowerCase();
    const params = new URLSearchParams(queryPart);

    let route = C.ROUTES.DASHBOARD;
    let editorFileId = null;

    if (segment === C.ROUTES.LIBRARY) {
      route = C.ROUTES.LIBRARY;
    } else if (segment === C.ROUTES.JOBS) {
      route = C.ROUTES.JOBS;
    } else if (segment === C.ROUTES.SETTINGS) {
      route = C.ROUTES.SETTINGS;
    } else if (segment === C.ROUTES.EDITOR && parts[1]) {
      const parsedId = parseInt(parts[1], 10);
      if (Number.isFinite(parsedId) && parsedId >= 1) {
        route = C.ROUTES.EDITOR;
        editorFileId = parsedId;
      }
    }

    const legacyFilter = params.get("filter") || "";
    const needsConversion =
      params.get("needs_conversion") === "1" ||
      legacyFilter === C.FILTERS.NEEDS_CONVERSION;
    let noDolby =
      params.get("no_dolby") === "1" || legacyFilter === C.FILTERS.NO_DOLBY;
    const hasMetadata =
      params.get("has_metadata") === "1" ||
      legacyFilter === C.FILTERS.HAS_METADATA;
    let audioType = params.get("audio") || "";
    const audioCodec = params.get("audio_codec") || "";
    const videoCodec = params.get("video_codec") || "";
    const resolution = params.get("resolution") || "";
    const dynamicRange = params.get("dynamic_range") || "";
    const audioChannels = params.get("audio_channels") || "";
    const audioLanguage = params.get("audio_language") || "";
    const noSubtitles = params.get("no_subtitles") === "1";

    if (legacyFilter === C.FILTERS.AAC51_ONLY) {
      noDolby = true;
      if (!audioType) audioType = "AAC 5.1";
    }

    const sortParam = params.get("sort") || C.SORT_FIELDS.FILENAME;
    const sort = VALID_SORTS.has(sortParam)
      ? sortParam
      : C.SORT_FIELDS.FILENAME;

    const orderParam = params.get("order") || C.SORT_ORDERS.ASC;
    const order = VALID_ORDERS.has(orderParam)
      ? orderParam
      : C.SORT_ORDERS.ASC;

    const parsedPage = parseInt(params.get("page") || "", 10);
    const page =
      Number.isFinite(parsedPage) && parsedPage >= C.PAGINATION.DEFAULT_PAGE
        ? parsedPage
        : C.PAGINATION.DEFAULT_PAGE;

    const rawView = (params.get("view") || "").toLowerCase();
    let view = null;
    if (rawView === C.LIBRARY_VIEWS.DIR || rawView === C.LIBRARY_VIEWS.LIST) {
      view = rawView;
    }

    return {
      route,
      editorFileId,
      view,
      needsConversion,
      noDolby,
      hasMetadata,
      audioType,
      audioCodec,
      videoCodec,
      resolution,
      dynamicRange,
      audioChannels,
      audioLanguage,
      noSubtitles,
      folder: params.get("folder") || "",
      q: params.get("q") || "",
      sort,
      order,
      page,
    };
  }

  function buildHash(state) {
    if (state.route === C.ROUTES.EDITOR && state.editorFileId) {
      return `#/${C.ROUTES.EDITOR}/${state.editorFileId}`;
    }
    if (state.route === C.ROUTES.LIBRARY) {
      const qs = new URLSearchParams();
      if (state.view && state.view !== C.LIBRARY_VIEWS.DIR) {
        qs.set("view", state.view);
      }
      if (state.needsConversion) qs.set("needs_conversion", "1");
      if (state.noDolby) qs.set("no_dolby", "1");
      if (state.hasMetadata) qs.set("has_metadata", "1");
      if (state.audioType && state.audioType.trim()) {
        qs.set("audio", state.audioType.trim());
      }
      if (state.audioCodec && state.audioCodec.trim()) {
        qs.set("audio_codec", state.audioCodec.trim());
      }
      if (state.videoCodec && state.videoCodec.trim()) {
        qs.set("video_codec", state.videoCodec.trim());
      }
      if (state.resolution && state.resolution.trim()) {
        qs.set("resolution", state.resolution.trim());
      }
      if (state.dynamicRange && state.dynamicRange.trim()) {
        qs.set("dynamic_range", state.dynamicRange.trim());
      }
      if (state.audioChannels && state.audioChannels.trim()) {
        qs.set("audio_channels", state.audioChannels.trim());
      }
      if (state.audioLanguage && state.audioLanguage.trim()) {
        qs.set("audio_language", state.audioLanguage.trim());
      }
      if (state.noSubtitles) {
        qs.set("no_subtitles", "1");
      }
      if (state.folder) qs.set("folder", state.folder);
      if (state.q && state.q.trim()) qs.set("q", state.q.trim());
      if (state.sort && state.sort !== C.SORT_FIELDS.FILENAME) {
        qs.set("sort", state.sort);
      }
      if (state.order && state.order !== C.SORT_ORDERS.ASC) {
        qs.set("order", state.order);
      }
      if (state.page && state.page > C.PAGINATION.DEFAULT_PAGE) {
        qs.set("page", String(state.page));
      }
      const query = qs.toString();
      return query ? `#/library?${query}` : "#/library";
    }
    if (state.route === C.ROUTES.JOBS) {
      return "#/jobs";
    }
    if (state.route === C.ROUTES.SETTINGS) {
      return "#/settings";
    }
    return "#/dashboard";
  }

  function loadStoredLibraryView() {
    try {
      const saved = window.localStorage.getItem(C.STORAGE_KEYS.LIBRARY_VIEW);
      if (saved === C.LIBRARY_VIEWS.LIST || saved === C.LIBRARY_VIEWS.DIR) {
        return saved;
      }
    } catch (_err) {
      // Ignore storage errors in restricted contexts
    }
    return C.LIBRARY_VIEWS.DIR;
  }

  function loadStoredFolderLayout() {
    try {
      const saved = window.localStorage.getItem(C.STORAGE_KEYS.FOLDER_LAYOUT);
      if (
        saved === C.FOLDER_LAYOUTS.LIST ||
        saved === C.FOLDER_LAYOUTS.GRID
      ) {
        return saved;
      }
    } catch (_err) {
      // Ignore storage errors in restricted contexts
    }
    return C.FOLDER_LAYOUTS.LIST;
  }

  function loadStoredDashStorageLens() {
    const lenses = C.DASHBOARD_LENSES || {};
    try {
      const saved = window.localStorage.getItem(C.STORAGE_KEYS.DASH_STORAGE_LENS);
      if (
        saved === lenses.STORAGE_CODEC ||
        saved === lenses.STORAGE_RESOLUTION ||
        saved === lenses.STORAGE_CONTAINER
      ) {
        return saved;
      }
    } catch (_err) {
      // Ignore storage errors in restricted contexts
    }
    return lenses.STORAGE_DEFAULT || "codec";
  }

  function loadStoredDashAudioLens() {
    const lenses = C.DASHBOARD_LENSES || {};
    try {
      const saved = window.localStorage.getItem(C.STORAGE_KEYS.DASH_AUDIO_LENS);
      if (
        saved === lenses.AUDIO_CODECS ||
        saved === lenses.AUDIO_CHANNELS ||
        saved === lenses.AUDIO_LANGUAGES
      ) {
        return saved;
      }
    } catch (_err) {
      // Ignore storage errors in restricted contexts
    }
    return lenses.AUDIO_DEFAULT || "codecs";
  }

  function computeContextMenuPosition(event, menuHeightPx) {
    const cfg = C.CONTEXT_MENU;
    const vw = window.innerWidth || 1024;
    const vh = window.innerHeight || 768;
    const isPointerContext = Boolean(event && event.type === "contextmenu");

    let left = cfg.VIEWPORT_MARGIN_PX;
    let top = cfg.VIEWPORT_MARGIN_PX;

    if (isPointerContext) {
      left = Number(event.clientX || 0);
      top = Number(event.clientY || 0);
    } else if (event && event.currentTarget && event.currentTarget.getBoundingClientRect) {
      const rect = event.currentTarget.getBoundingClientRect();
      left = rect.right - cfg.MENU_WIDTH_PX;
      top = rect.bottom + cfg.TRIGGER_GAP_PX;
      if (top + menuHeightPx + cfg.VIEWPORT_MARGIN_PX > vh) {
        top = rect.top - menuHeightPx - cfg.TRIGGER_GAP_PX;
      }
    }

    const maxLeft = Math.max(
      cfg.VIEWPORT_MARGIN_PX,
      vw - cfg.MENU_WIDTH_PX - cfg.VIEWPORT_MARGIN_PX
    );
    const maxTop = Math.max(
      cfg.VIEWPORT_MARGIN_PX,
      vh - menuHeightPx - cfg.VIEWPORT_MARGIN_PX
    );

    return {
      left: Math.round(Math.min(Math.max(left, cfg.VIEWPORT_MARGIN_PX), maxLeft)),
      top: Math.round(Math.min(Math.max(top, cfg.VIEWPORT_MARGIN_PX), maxTop)),
    };
  }

  global.mkvForgeApp = function mkvForgeApp() {
    return {
      // Exposed helpers for Alpine templates
      C,
      Fmt,
      Ed,
      FT,
      PP,
      TM,
      Theme,
      I18n,

      t(key, fallback = "") {
        return I18n.t(
          key,
          (this.userSettings && this.userSettings.language) || I18n.DEFAULT_LOCALE,
          fallback
        );
      },

      // Navigation & hash route state
      route: C.ROUTES.DASHBOARD,
      lastLibraryHash: "#/library",
      _libraryReturnState: LR.loadOrigin(),
      _pendingLibraryScrollRestore: null,
      _scrollRestoreTimer: null,
      _lastLoadedLibraryHash: null,
      scanMenuOpen: false,

      // Toast notification
      toasts: [],
      _toastSeq: 0,
      _toastTimers: {},

      // Persistent user settings (hydrated from /api/settings & /api/dashboard)
      userSettings: { ...C.DEFAULT_USER_SETTINGS },
      settingsSaving: false,
      settingsModalOpen: false,
      activeSettingsSection: C.SETTINGS_SECTIONS.APPEARANCE,
      mobileSettingsSection: C.SETTINGS_SECTIONS.APPEARANCE,
      _lastNonSettingsRoute: C.ROUTES.DASHBOARD,

      // Phase 13 Local Authentication & First-Run Onboarding state
      authReady: false,
      authMode: C.AUTH_MODES.UNCONFIGURED,
      authAuthenticated: true,
      authNeedsSetup: false,
      authUser: null,
      authUsers: [],
      authUsersLoading: false,
      apiKeys: [],
      apiKeysLoading: false,
      newApiKeyName: "",
      createdApiKeyModal: null,
      apiKeyCopied: false,
      authBusy: false,
      authError: null,
      authExpiredNotice: false,
      _authRecoveryPromise: null,
      onboardingStep: "choice", // Legacy compat
      onboardingActive: false,
      onboardingWizardStep: C.ONBOARDING.STEP_ACCESS,
      onboardingDirection: C.ONBOARDING.DIR_FORWARD,
      onboardingAuthChoice: C.ONBOARDING.AUTH_CHOICE_LOCAL,
      onboardingStoragePath: null,
      onboardingStorageWritable: true,
      onboardingProfileDraft: PP.createProfileDraftFromSettings(C.DEFAULT_USER_SETTINGS),
      settingsProfileDraft: PP.createProfileDraftFromSettings(C.DEFAULT_USER_SETTINGS),
      onboardingForm: { username: "", display_name: "", password: "", confirmPassword: "" },
      loginForm: { username: "", password: "" },
      upgradeAuthOpen: false,
      upgradeAuthForm: { username: "", display_name: "", password: "", confirmPassword: "" },
      newUserForm: { username: "", display_name: "", password: "" },
      changePasswordForm: { currentPassword: "", newPassword: "", confirmPassword: "" },
      disableAuthOpen: false,
      disableAuthPassword: "",
      logoutConfirmOpen: false,
      visiblePasswords: {},

      isPasswordVisible(fieldKey) {
        return Boolean(this.visiblePasswords[fieldKey]);
      },

      togglePasswordVisible(fieldKey) {
        this.visiblePasswords = {
          ...this.visiblePasswords,
          [fieldKey]: !this.visiblePasswords[fieldKey],
        };
      },

      // Storage, cache & database maintenance state (Screen 5: #/settings)
      maintenanceStatus: null,
      maintenanceLoading: false,
      maintenanceError: null,
      maintenanceBusyAction: null,
      factoryResetModalOpen: false,
      factoryResetBusy: false,
      factoryResetError: null,
      factoryResetPassword: "",
      factoryResetConfirmText: "",

      // Dashboard state
      dashboardLoading: true,
      dashboardError: null,
      dashStorageLens: loadStoredDashStorageLens(),
      dashAudioLens: loadStoredDashAudioLens(),
      dashboard: {
        app_name: C.APP.NAME,
        app_version: C.APP.VERSION,
        stats: {
          total_files: 0,
          aac51_only: 0,
          has_metadata: 0,
          has_metadata_bytes: 0,
          needs_conversion: 0,
          needs_conversion_bytes: 0,
          untagged_audio: 0,
          untagged_audio_bytes: 0,
          no_subtitles: 0,
          with_subtitles: 0,
          playback_profile_configured: false,
          playback_profile_preset: "neutral",
          playback_profile_label: "Not Configured",
          last_scan_at: null,
          last_scan_status: null,
        },
        analytics: {
          storage: {
            disk_total_bytes: 0,
            disk_used_bytes: 0,
            disk_free_bytes: 0,
            mkv_bytes: 0,
            mkv_count: 0,
            mp4_bytes: 0,
            mp4_count: 0,
            media_bytes: 0,
            other_used_bytes: 0,
          },
          storage_by_codec: [],
          storage_by_resolution: [],
          total_audio_tracks: 0,
          audio_codecs: [],
          audio_channels: [],
          audio_languages: [],
          resolutions: [],
          video_codecs: [],
          dynamic_ranges: [],
        },
        scan_running: false,
        recent_activity: [],
      },
      hoveredStorageKey: null,
      hoveredAudioKey: null,

      // Library browser & Explorer state
      libraryView: loadStoredLibraryView(),
      folderLayout: loadStoredFolderLayout(),
      currentDir: C.FOLDER_TREE.ROOT_PATH,
      subfolders: [],
      otherFiles: [],
      folderCounts: {},
      selectedFileIds: [],
      mobileSelectMode: false,
      folderBarMenuOpen: false,
      contextMenu: null,
      libraryLoading: false,
      libraryError: null,
      _pendingLibraryReload: false,
      files: [],
      folders: [],
      audioTypes: [],
      facets: {
        aac51_only: 0,
        has_metadata: 0,
        no_dolby: 0,
        needs_conversion: 0,
        audio_types: [],
      },
      totalFiles: 0,
      totalPages: 0,
      needsConversion: false,
      noDolby: false,
      hasMetadata: false,
      audioType: "",
      audioCodec: "",
      videoCodec: "",
      resolution: "",
      dynamicRange: "",
      audioChannels: "",
      audioLanguage: "",
      noSubtitles: false,
      searchQuery: "",
      appliedSearchQuery: "",
      sortField: C.SORT_FIELDS.FILENAME,
      sortOrder: C.SORT_ORDERS.ASC,
      page: C.PAGINATION.DEFAULT_PAGE,
      limit: C.PAGINATION.DEFAULT_LIMIT,
      rescanningIds: {},
      duplicatingIds: {},
      thumbLoadErrors: {},
      _searchTimer: null,
      _libraryRequestSeq: 0,

      // File Editor state (Screen 3: #/files/{id})
      editorFileId: null,
      editorLoading: false,
      editorError: null,
      editorConflictMessage: null,
      editorExternalChange: null,
      editorFile: null,
      editor: null,
      editorSubmitting: false,
      editorRescanning: false,

      // Unified + Add / Convert Track menu, Convert Audio modal & Import External Track modal
      addAudioMenuOpen: false,
      addAudioModalOpen: false,
      addAudioDraft: null,
      importTrackModal: null,

      // Unified Folder Tree Picker modal state (Save As / Move Files / Move Folder / Browse)
      folderPickerOpen: false,
      folderPickerPurpose: "save_as",
      folderPickerTitle: "Choose Destination Folder",
      folderPickerConfirmLabel: "Select Folder",
      folderPickerContext: null,
      folderPickerSubmitting: false,
      folderTree: null,

      // File/Folder Rename, Delete, New Folder, Cancel Job & Media Info modal state
      renameModal: null,
      deleteModal: null,
      newFolderModal: null,
      cancelJobModal: null,
      infoModal: null,

      // Mobile Pull-to-Refresh gesture state
      pullRefresh: PR.createPullRefreshState(),

      // Phase 16: Speaker & Audio System Test Generator modal state
      testMediaModalOpen: false,
      testMediaDraft: null,
      testMediaBusy: false,
      testMediaError: null,
      testMediaResult: null,

      // Scan SSE state
      scanTriggering: false,
      scanProgress: {
        scanned: 0,
        total: 0,
        skipped: 0,
        errors: 0,
        current_file: null,
        status: C.STATUSES.IDLE,
        error_message: null,
      },
      _lastScanStatus: C.STATUSES.IDLE,
      _scanLiveTimer: null,
      _scanLiveLastRefreshAt: 0,
      _scanLiveLastLibraryRefreshAt: 0,
      _scanLiveLastProbedCount: 0,
      _scanLiveInFlight: false,
      _scanPendingFinalSync: false,

      // Job queue, Live Drawer & CLI log viewer state
      jobsDrawerOpen: false,
      jobsLoading: false,
      jobsError: null,
      cancellingJobIds: {},
      deletingHistoryIds: {},
      clearingHistory: false,
      confirmClearHistory: false,
      expandedHistoryWarnings: {},
      showCliLogs: false,
      logsAutoScroll: true,
      liveLogs: [],
      _liveLogJobId: null,
      jobsState: {
        active: null,
        queued: [],
        history: [],
      },
      liveJob: {
        job_id: null,
        filepath: null,
        stage: null,
        stage_label: null,
        percent: 0,
        speed: null,
        eta: null,
        status: C.STATUSES.IDLE,
        warnings: [],
        error_message: null,
      },
      _lastJobStatus: C.STATUSES.IDLE,

      // Managed single multiplexed SSE stream & lifecycle guards
      _initialized: false,
      _eventStream: null,
      _hiddenCloseTimer: null,
      _libraryAbortController: null,

      // Lifecycle initialization (idempotent: Alpine v3 auto-invokes init())
      init() {
        if (this._initialized) return;
        this._initialized = true;
        this._applyHashState(window.location.hash, false);
        if (!window.location.hash) {
          this._syncUrlHash(true);
        }

        window.addEventListener("hashchange", () => {
          this._applyHashState(window.location.hash, true);
        });

        window.addEventListener("keydown", (event) => {
          if (event.key === "Escape") {
            if (this.scanMenuOpen) {
              this.scanMenuOpen = false;
            } else if (this.contextMenu) {
              this.closeContextMenu();
            } else if (this.infoModal) {
              this.closeFileInfoModal();
            } else if (this.cancelJobModal) {
              this.closeCancelJobModal();
            } else if (this.deleteModal) {
              this.deleteModal = null;
            } else if (this.renameModal) {
              this.renameModal = null;
            } else if (this.newFolderModal) {
              this.newFolderModal = null;
            } else if (this.folderPickerOpen) {
              this.closeFolderPicker();
            } else if (this.testMediaModalOpen) {
              this.closeTestMediaModal();
            } else if (this.importTrackModal) {
              this.closeImportTrackModal();
            } else if (this.addAudioModalOpen) {
              this.closeAddAudioModal();
            } else if (this.addAudioMenuOpen) {
              this.addAudioMenuOpen = false;
            } else if (this.jobsDrawerOpen) {
              this.closeJobsDrawer();
            }
          }
        });

        window.addEventListener(
          "scroll",
          () => {
            if (this.contextMenu) this.closeContextMenu();
          },
          true
        );

        window.addEventListener("resize", () => {
          if (this.contextMenu) this.closeContextMenu();
        });

        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "visible") {
            this.handleAppResume();
          } else {
            this.handleAppHidden();
          }
        });

        window.addEventListener("online", () => {
          this.handleAppResume({ forceSse: true });
        });

        window.addEventListener("beforeunload", () => {
          this._disconnectStreams();
        });

        window.addEventListener("pagehide", () => {
          this._disconnectStreams();
        });

        // Mobile Pull-to-Refresh touch gesture listeners
        window.addEventListener(
          "touchstart",
          (event) => this.onPullTouchStart(event),
          { passive: true }
        );
        window.addEventListener(
          "touchmove",
          (event) => this.onPullTouchMove(event),
          { passive: false }
        );
        window.addEventListener(
          "touchend",
          () => this.onPullTouchEnd(),
          { passive: true }
        );
        window.addEventListener(
          "touchcancel",
          () => this.resetPullRefresh(),
          { passive: true }
        );

        // Apply cached theme & bind live OS prefers-color-scheme listener
        const cachedTheme = Theme.loadStoredThemePrefs();
        this.userSettings = {
          ...this.userSettings,
          theme_mode: cachedTheme.mode || this.userSettings.theme_mode,
          accent_color: cachedTheme.accent || this.userSettings.accent_color,
          font_scale: cachedTheme.fontScale || this.userSettings.font_scale,
        };
        Theme.applyTheme(
          this.userSettings.theme_mode,
          this.userSettings.accent_color,
          { persist: false }
        );
        Theme.applyFontScale(this.userSettings.font_scale, { persist: false });
        Theme.bindSystemThemeListener(
          () => this.userSettings && this.userSettings.theme_mode,
          () => this.userSettings && this.userSettings.accent_color
        );

        // Register global 401 unauthorized recovery so expired/stale sessions smoothly prompt Sign-In
        if (typeof Api.setUnauthorizedHandler === "function") {
          Api.setUnauthorizedHandler(() => {
            this.handleUnauthorizedResponse();
          });
        }

        // Open persistent SSE streams after page load (skipped if login gate is active)
        const startStreams = () => {
          if (this.isLoginRequired) return;
          setTimeout(() => {
            if (!this.isLoginRequired) {
              this._connectStreams();
            }
          }, C.TIMINGS_MS.SSE_CONNECT_DELAY);
        };
        if (document.readyState === "complete") {
          startStreams();
        } else {
          window.addEventListener("load", startStreams, { once: true });
        }

        // Check auth status first so login screen blocks protected APIs & SSE when local auth is active
        this.bootstrapSessionAndData();
      },

      get isLoginRequired() {
        return (
          this.authReady &&
          this.authMode === C.AUTH_MODES.LOCAL &&
          !this.authAuthenticated
        );
      },

      get isOnboardingRequired() {
        return Boolean(this.authReady && (this.authNeedsSetup || this.onboardingActive));
      },

      get onboardingStepAnimClass() {
        return this.onboardingDirection === C.ONBOARDING.DIR_BACK
          ? "onboarding-step-back"
          : "onboarding-step-fwd";
      },

      get activePlaybackProfile() {
        return {
          preset: this.userSettings.playback_profile_preset || "neutral",
          supportedCodecs: Array.isArray(this.userSettings.playback_supported_codecs)
            ? this.userSettings.playback_supported_codecs
            : [],
          maxChannels: Number(this.userSettings.playback_max_channels || 6),
          preferredCodec: this.userSettings.playback_preferred_codec || "eac3",
        };
      },

      get isPlaybackProfileConfigured() {
        const p = this.activePlaybackProfile;
        return Boolean(
          p.preset &&
            p.preset !== "neutral" &&
            (p.preset !== "custom" || p.supportedCodecs.length > 0)
        );
      },

      get isLocalAuthEnabled() {
        return this.authMode === C.AUTH_MODES.LOCAL;
      },

      get canSubmitFactoryReset() {
        if (this.factoryResetBusy) return false;
        if (this.isLocalAuthEnabled) {
          return Boolean(
            this.factoryResetPassword && this.factoryResetPassword.length > 0
          );
        }
        return (
          String(this.factoryResetConfirmText || "").trim() ===
          C.MAINTENANCE.FACTORY_RESET_CONFIRM_TOKEN
        );
      },

      get currentUserDisplayName() {
        if (this.authUser && this.authUser.display_name) {
          return this.authUser.display_name;
        }
        return "Anonymous";
      },

      _applyAuthStatus(status) {
        if (!status || typeof status !== "object") return;
        this.authMode = status.auth_mode || C.AUTH_MODES.ANONYMOUS;
        this.authAuthenticated = Boolean(status.authenticated);
        this.authNeedsSetup = Boolean(status.needs_setup);
        this.authUser = status.user || null;
        if (status.storage_path !== undefined) {
          this.onboardingStoragePath = status.storage_path || null;
        }
        if (status.storage_writable !== undefined) {
          this.onboardingStorageWritable = Boolean(status.storage_writable);
        }
        if (this.authNeedsSetup && !this.onboardingActive) {
          this.onboardingActive = true;
          this.onboardingWizardStep = C.ONBOARDING.STEP_ACCESS;
          this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
        }
        this.authReady = true;
        if (this.authAuthenticated) {
          this.authExpiredNotice = false;
        }
      },

      _clearAuthErrorBanners() {
        this.dashboardError = null;
        this.libraryError = null;
        this.editorError = null;
        this.maintenanceError = null;
      },

      async handleUnauthorizedResponse() {
        if (this._authRecoveryPromise) {
          return this._authRecoveryPromise;
        }
        this._authRecoveryPromise = (async () => {
          try {
            const status = await Api.getAuthStatus();
            this._applyAuthStatus(status);
            if (this.isLoginRequired) {
              this._disconnectStreams();
              this._clearAuthErrorBanners();
              this.authExpiredNotice = true;
              this.authError = null;
              return false;
            }
            // Session is actually valid (e.g. mobile wake-up cookie race) -> recover silently
            this._clearAuthErrorBanners();
            this._startInitialDataAndStreams(true);
            return true;
          } catch (_err) {
            return false;
          } finally {
            this._authRecoveryPromise = null;
          }
        })();
        return this._authRecoveryPromise;
      },

      _startInitialDataAndStreams(connectStreamsNow = false) {
        this.loadDashboard();
        this.loadJobs();
        if (this.route === C.ROUTES.LIBRARY) {
          this.loadLibrary();
        } else if (this.route === C.ROUTES.SETTINGS) {
          this.loadUserSettings();
          this.loadMaintenanceStatus();
          if (this.isLocalAuthEnabled && this.authAuthenticated) {
            this.loadAuthUsers();
            this.loadApiKeys();
          }
        } else if (this.route === C.ROUTES.EDITOR && this.editorFileId) {
          this.loadEditor(this.editorFileId);
        }

        if (connectStreamsNow && !this.isLoginRequired) {
          setTimeout(() => {
            if (!this.isLoginRequired) {
              this._connectStreams();
            }
          }, C.TIMINGS_MS.SSE_CONNECT_DELAY);
        }
      },

      async bootstrapSessionAndData() {
        try {
          const status = await Api.getAuthStatus();
          this._applyAuthStatus(status);
        } catch (_err) {
          // If auth status fails on older mock/harness, degrade safely
          this.authReady = true;
          this.authAuthenticated = true;
          this.authNeedsSetup = false;
        }

        if (this.isLoginRequired) {
          this._disconnectStreams();
          return;
        }
        this._startInitialDataAndStreams();
      },

      async loadAuthUsers() {
        if (!this.isLocalAuthEnabled || !this.authAuthenticated) {
          this.authUsers = [];
          this.apiKeys = [];
          return;
        }
        this.authUsersLoading = true;
        try {
          const res = await Api.getAuthUsers();
          this.authUsers = Array.isArray(res && res.users) ? res.users : [];
        } catch (err) {
          this.showToast(err.message || "Failed to load local accounts", "error");
        } finally {
          this.authUsersLoading = false;
        }
      },

      async loadApiKeys() {
        if (!this.isLocalAuthEnabled || !this.authAuthenticated) {
          this.apiKeys = [];
          return;
        }
        this.apiKeysLoading = true;
        try {
          const res = await Api.getApiKeys();
          this.apiKeys = Array.isArray(res && res.api_keys) ? res.api_keys : [];
        } catch (err) {
          this.showToast(err.message || "Failed to load API keys", "error");
        } finally {
          this.apiKeysLoading = false;
        }
      },

      // Phase 15: 3-Step Apple-Style First-Run Onboarding Wizard
      _resetOnboardingScroll() {
        const body = this.$refs && this.$refs.onboardingBody;
        if (body && typeof body.scrollTop === "number") {
          body.scrollTop = C.SCROLL_RESTORE.MIN_OFFSET_PX;
        }
      },

      selectOnboardingAuthChoice(choice) {
        this.onboardingAuthChoice =
          choice === C.ONBOARDING.AUTH_CHOICE_ANONYMOUS
            ? C.ONBOARDING.AUTH_CHOICE_ANONYMOUS
            : C.ONBOARDING.AUTH_CHOICE_LOCAL;
        this.authError = null;
      },

      onboardingNextFromAccess() {
        this.authError = null;
        this.onboardingAuthChoice = C.ONBOARDING.AUTH_CHOICE_LOCAL;
        const form = this.onboardingForm;
        const uname = String(form.username || "").trim();
        if (uname.length < C.ONBOARDING.MIN_USERNAME_LEN) {
          this.authError = `Username must be at least ${C.ONBOARDING.MIN_USERNAME_LEN} characters.`;
          return;
        }
        if (!form.password || form.password.length < C.ONBOARDING.MIN_PASSWORD_LEN) {
          this.authError = `Password must be at least ${C.ONBOARDING.MIN_PASSWORD_LEN} characters.`;
          return;
        }
        if (form.password !== form.confirmPassword) {
          this.authError = "Passwords do not match.";
          return;
        }
        this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
        this.onboardingWizardStep = C.ONBOARDING.STEP_SOUND_SYSTEM;
        this._resetOnboardingScroll();
      },

      onboardingContinueAnonymous() {
        this.authError = null;
        this.onboardingAuthChoice = C.ONBOARDING.AUTH_CHOICE_ANONYMOUS;
        this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
        this.onboardingWizardStep = C.ONBOARDING.STEP_SOUND_SYSTEM;
        this._resetOnboardingScroll();
      },

      onboardingBack() {
        this.authError = null;
        this.onboardingDirection = C.ONBOARDING.DIR_BACK;
        if (this.onboardingWizardStep === C.ONBOARDING.STEP_INITIALIZE) {
          this.onboardingWizardStep = C.ONBOARDING.STEP_SOUND_SYSTEM;
        } else if (this.onboardingWizardStep === C.ONBOARDING.STEP_SOUND_SYSTEM) {
          this.onboardingWizardStep = C.ONBOARDING.STEP_ACCESS;
        }
        this._resetOnboardingScroll();
      },

      onboardingSelectPreset(presetId) {
        this.onboardingProfileDraft = PP.applyPresetToDraft(
          this.onboardingProfileDraft,
          presetId
        );
      },

      onboardingToggleCustomCodec(codecKey) {
        this.onboardingProfileDraft = PP.toggleCodecInDraft(
          this.onboardingProfileDraft,
          codecKey
        );
      },

      onboardingSetMaxChannels(ch) {
        this.onboardingProfileDraft = {
          ...this.onboardingProfileDraft,
          preset: "custom",
          maxChannels: Number(ch),
        };
      },

      onboardingSetPreferredCodec(codec) {
        this.onboardingProfileDraft = {
          ...this.onboardingProfileDraft,
          preset: "custom",
          preferredCodec: codec,
        };
      },

      onboardingNextFromSoundSystem() {
        this.authError = null;
        this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
        this.onboardingWizardStep = C.ONBOARDING.STEP_INITIALIZE;
        this._resetOnboardingScroll();
      },

      onboardingSkipSoundSystem() {
        this.onboardingProfileDraft = PP.applyPresetToDraft(
          this.onboardingProfileDraft,
          "neutral"
        );
        this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
        this.onboardingWizardStep = C.ONBOARDING.STEP_INITIALIZE;
        this._resetOnboardingScroll();
      },

      async finishOnboarding() {
        if (this.authBusy) return;
        this.authBusy = true;
        this.authError = null;
        try {
          let setupPayload;
          if (this.onboardingAuthChoice === C.ONBOARDING.AUTH_CHOICE_LOCAL) {
            const form = this.onboardingForm;
            setupPayload = {
              mode: C.AUTH_MODES.LOCAL,
              username: String(form.username || "").trim(),
              display_name:
                String(form.display_name || "").trim() ||
                String(form.username || "").trim(),
              password: form.password,
            };
          } else {
            setupPayload = { mode: C.AUTH_MODES.ANONYMOUS };
          }

          const status = await Api.setupAuth(setupPayload);
          this._applyAuthStatus(status);

          const profilePatch = PP.buildSettingsPatchFromDraft(
            this.onboardingProfileDraft
          );
          try {
            const updatedSettings = await Api.updateSettings(profilePatch);
            this._applyUserSettings(updatedSettings);
          } catch (_settingsErr) {
            // Non-fatal: user can adjust Playback Profile in Settings anytime
          }

          this.onboardingForm = {
            username: "",
            display_name: "",
            password: "",
            confirmPassword: "",
          };
          this.onboardingActive = false;
          this.authNeedsSetup = false;
          this.onboardingWizardStep = C.ONBOARDING.STEP_ACCESS;

          try {
            await Api.triggerScan(false);
          } catch (_scanErr) {
            // Non-fatal: Dashboard remains accessible with manual scan retry
          }

          this.showToast(
            this.isLocalAuthEnabled
              ? `Welcome, ${this.currentUserDisplayName}!`
              : "MKV Forge is ready.",
            "success"
          );
          this.navigate(C.ROUTES.DASHBOARD);
          this._startInitialDataAndStreams(true);
        } catch (err) {
          this.authError = err.message || "Failed to complete setup.";
          this.onboardingDirection = C.ONBOARDING.DIR_BACK;
          this.onboardingWizardStep = C.ONBOARDING.STEP_ACCESS;
        } finally {
          this.authBusy = false;
        }
      },

      async completeOnboardingAnonymous() {
        this.onboardingAuthChoice = C.ONBOARDING.AUTH_CHOICE_ANONYMOUS;
        return this.finishOnboarding();
      },

      async completeOnboardingLocal() {
        this.onboardingAuthChoice = C.ONBOARDING.AUTH_CHOICE_LOCAL;
        return this.finishOnboarding();
      },

      async submitLogin() {
        if (this.authBusy) return;
        this.authError = null;
        if (!this.loginForm.username.trim() || !this.loginForm.password) {
          this.authError = "Enter your username and password.";
          return;
        }
        this.authBusy = true;
        try {
          const status = await Api.login(
            this.loginForm.username.trim(),
            this.loginForm.password
          );
          this._applyAuthStatus(status);
          this.loginForm = { username: "", password: "" };
          this.authExpiredNotice = false;
          this._clearAuthErrorBanners();
          this.showToast(`Welcome back, ${this.currentUserDisplayName}`, "success");
          this._startInitialDataAndStreams(true);
        } catch (err) {
          this.authError = err.message || "Invalid username or password.";
        } finally {
          this.authBusy = false;
        }
      },

      openLogoutConfirm() {
        this.scanMenuOpen = false;
        this.logoutConfirmOpen = true;
      },

      closeLogoutConfirm() {
        if (!this.logoutConfirmOpen || this.authBusy) return;
        this.logoutConfirmOpen = false;
      },

      async signOut() {
        if (this.authBusy) return;
        this.authBusy = true;
        this.authError = null;
        try {
          this._disconnectStreams();
          const status = await Api.logout();
          this._applyAuthStatus(status);
          this.settingsModalOpen = false;
          this.logoutConfirmOpen = false;
          this.authUsers = [];
          this.apiKeys = [];
          this.showToast("Signed out", "info");
        } catch (err) {
          this.showToast(err.message || "Failed to sign out", "error");
        } finally {
          this.authBusy = false;
        }
      },

      async submitUpgradeToLocalAuth() {
        if (this.authBusy) return;
        const form = this.upgradeAuthForm;
        this.authError = null;
        if (!form.username.trim() || !form.password) {
          this.authError = "Username and password are required.";
          return;
        }
        if (form.password !== form.confirmPassword) {
          this.authError = "Passwords do not match.";
          return;
        }
        this.authBusy = true;
        try {
          const status = await Api.setupAuth({
            mode: C.AUTH_MODES.LOCAL,
            username: form.username.trim(),
            display_name: form.display_name.trim() || form.username.trim(),
            password: form.password,
          });
          this._applyAuthStatus(status);
          this.upgradeAuthOpen = false;
          this.upgradeAuthForm = {
            username: "",
            display_name: "",
            password: "",
            confirmPassword: "",
          };
          this.showToast(`Sign-in enabled (${this.currentUserDisplayName})`, "success");
          this.loadAuthUsers();
        } catch (err) {
          this.authError = err.message || "Failed to enable local sign-in";
        } finally {
          this.authBusy = false;
        }
      },

      async submitCreateLocalUser() {
        if (this.authBusy) return;
        const form = this.newUserForm;
        this.authError = null;
        if (!form.username.trim() || !form.password) {
          this.authError = "Username and password are required.";
          return;
        }
        this.authBusy = true;
        try {
          const created = await Api.createAuthUser({
            username: form.username.trim(),
            display_name: form.display_name.trim() || form.username.trim(),
            password: form.password,
          });
          this.newUserForm = { username: "", display_name: "", password: "" };
          this.showToast(`Added local account ${created.display_name}`, "success");
          await this.loadAuthUsers();
        } catch (err) {
          this.authError = err.message || "Failed to create local user";
        } finally {
          this.authBusy = false;
        }
      },

      async submitChangePassword() {
        if (this.authBusy) return;
        const form = this.changePasswordForm;
        this.authError = null;
        if (!form.currentPassword || !form.newPassword) {
          this.authError = "Enter your current and new password.";
          return;
        }
        if (form.newPassword !== form.confirmPassword) {
          this.authError = "New passwords do not match.";
          return;
        }
        this.authBusy = true;
        try {
          await Api.changePassword(form.currentPassword, form.newPassword);
          this.changePasswordForm = {
            currentPassword: "",
            newPassword: "",
            confirmPassword: "",
          };
          this.showToast("Password updated", "success");
        } catch (err) {
          this.authError = err.message || "Failed to update password";
        } finally {
          this.authBusy = false;
        }
      },

      async removeLocalUser(user) {
        if (!user || this.authBusy) return;
        this.authBusy = true;
        this.authError = null;
        try {
          await Api.deleteAuthUser(user.id);
          this.showToast(`Removed account ${user.display_name}`, "info");
          await this.loadAuthUsers();
        } catch (err) {
          this.authError = err.message || "Failed to remove account";
          this.showToast(this.authError, "error");
        } finally {
          this.authBusy = false;
        }
      },

      async submitCreateApiKey() {
        if (this.authBusy) return;
        const name = (this.newApiKeyName || "").trim() || "Default";
        this.authBusy = true;
        this.authError = null;
        try {
          const created = await Api.createApiKey(name);
          this.newApiKeyName = "";
          this.createdApiKeyModal = created;
          this.apiKeyCopied = false;
          await this.loadApiKeys();
          this.showToast(`API Key "${created.name}" created`, "success");
        } catch (err) {
          this.authError = err.message || "Failed to create API key";
          this.showToast(this.authError, "error");
        } finally {
          this.authBusy = false;
        }
      },

      async removeApiKey(key) {
        if (!key || this.authBusy) return;
        if (!confirm(`Revoke API key "${key.name}" (${key.key_prefix})? Any external services using it will lose access immediately.`)) {
          return;
        }
        this.authBusy = true;
        try {
          await Api.deleteApiKey(key.id);
          this.showToast(`Revoked key "${key.name}"`, "info");
          await this.loadApiKeys();
        } catch (err) {
          this.showToast(err.message || "Failed to revoke API key", "error");
        } finally {
          this.authBusy = false;
        }
      },

      async copyApiKeyToClipboard() {
        if (!this.createdApiKeyModal || !this.createdApiKeyModal.key) return;
        try {
          await navigator.clipboard.writeText(this.createdApiKeyModal.key);
          this.apiKeyCopied = true;
          this.showToast("API key copied to clipboard!", "success");
          setTimeout(() => {
            this.apiKeyCopied = false;
          }, 3000);
        } catch (err) {
          this.showToast("Could not copy to clipboard. Please copy manually.", "warning");
        }
      },

      async submitDisableLocalAuth() {
        if (this.authBusy) return;
        this.authError = null;
        if (!this.disableAuthPassword) {
          this.authError = "Enter your current password to switch to no-auth mode.";
          return;
        }
        this.authBusy = true;
        try {
          const status = await Api.disableAuth(this.disableAuthPassword);
          this._applyAuthStatus(status);
          this.disableAuthOpen = false;
          this.disableAuthPassword = "";
          this.authUsers = [];
          this.apiKeys = [];
          this.showToast("Switched to passwordless mode (Anonymous)", "info");
          this.loadUserSettings();
        } catch (err) {
          this.authError = err.message || "Password verification failed";
        } finally {
          this.authBusy = false;
        }
      },

      get dashboardStorage() {
        return (
          (this.dashboard &&
            this.dashboard.analytics &&
            this.dashboard.analytics.storage) || {
            disk_total_bytes: 0,
            disk_used_bytes: 0,
            disk_free_bytes: 0,
            mkv_bytes: 0,
            mkv_count: 0,
            mp4_bytes: 0,
            mp4_count: 0,
            media_bytes: 0,
            other_used_bytes: 0,
          }
        );
      },

      get dashboardDiskUsedPercent() {
        const s = this.dashboardStorage;
        if (!s.disk_total_bytes || s.disk_total_bytes <= 0) return 0;
        return Math.min(100, Math.round((s.disk_used_bytes / s.disk_total_bytes) * 100));
      },

      setDashStorageLens(lens) {
        const lenses = C.DASHBOARD_LENSES || {};
        if (
          lens !== lenses.STORAGE_CODEC &&
          lens !== lenses.STORAGE_RESOLUTION &&
          lens !== lenses.STORAGE_CONTAINER
        ) {
          return;
        }
        this.dashStorageLens = lens;
        this.hoveredStorageKey = null;
        try {
          window.localStorage.setItem(C.STORAGE_KEYS.DASH_STORAGE_LENS, lens);
        } catch (_err) {
          // Ignore storage errors in restricted contexts
        }
      },

      setDashAudioLens(lens) {
        const lenses = C.DASHBOARD_LENSES || {};
        if (
          lens !== lenses.AUDIO_CODECS &&
          lens !== lenses.AUDIO_CHANNELS &&
          lens !== lenses.AUDIO_LANGUAGES
        ) {
          return;
        }
        this.dashAudioLens = lens;
        this.hoveredAudioKey = null;
        try {
          window.localStorage.setItem(C.STORAGE_KEYS.DASH_AUDIO_LENS, lens);
        } catch (_err) {
          // Ignore storage errors in restricted contexts
        }
      },

      get dashboardStorageSegments() {
        const s = this.dashboardStorage;
        const analytics = (this.dashboard && this.dashboard.analytics) || {};
        const lenses = C.DASHBOARD_LENSES || {};

        if (
          this.dashStorageLens === lenses.STORAGE_CODEC &&
          Array.isArray(analytics.storage_by_codec) &&
          analytics.storage_by_codec.length > 0
        ) {
          const codecPalette = {
            HEVC: {
              color: "#10B981",
              colorStart: "#34D399",
              colorEnd: "#059669",
              dotClass: "bg-emerald-500",
            },
            "H.264": {
              color: "#F59E0B",
              colorStart: "#FBBF24",
              colorEnd: "#D97706",
              dotClass: "bg-amber-500",
            },
            AV1: {
              color: "#3B82F6",
              colorStart: "#60A5FA",
              colorEnd: "#2563EB",
              dotClass: "bg-blue-500",
            },
            VP9: {
              color: "#8B5CF6",
              colorStart: "#A78BFA",
              colorEnd: "#7C3AED",
              dotClass: "bg-violet-500",
            },
            "MPEG-2": {
              color: "#0EA5E9",
              colorStart: "#38BDF8",
              colorEnd: "#0284C7",
              dotClass: "bg-sky-500",
            },
            "VC-1": {
              color: "#F43F5E",
              colorStart: "#FB7185",
              colorEnd: "#E11D48",
              dotClass: "bg-rose-500",
            },
            Other: {
              color: "#71717A",
              colorStart: "#A1A1AA",
              colorEnd: "#52525B",
              dotClass: "bg-ink-muted",
            },
          };
          return Charts.buildPieSlices(
            analytics.storage_by_codec.map((slice) => {
              const style = codecPalette[slice.key] || codecPalette.Other;
              const fc = Number(slice.file_count || 0);
              return {
                key: slice.key,
                label: slice.label,
                value: Number(slice.size_bytes || 0),
                fileCount: fc,
                sublabel: `${fc} ${fc === 1 ? "file" : "files"}`,
                filterPreset: {
                  videoCodec: slice.key,
                  sort: C.SORT_FIELDS.SIZE_BYTES,
                  order: C.SORT_ORDERS.DESC,
                },
                color: style.color,
                colorStart: style.colorStart,
                colorEnd: style.colorEnd,
                dotClass: style.dotClass,
              };
            })
          );
        }

        if (
          this.dashStorageLens === lenses.STORAGE_RESOLUTION &&
          Array.isArray(analytics.storage_by_resolution) &&
          analytics.storage_by_resolution.length > 0
        ) {
          const resPalette = {
            "2160p": {
              color: "#8B5CF6",
              colorStart: "#A78BFA",
              colorEnd: "#7C3AED",
              dotClass: "bg-violet-500",
            },
            "1080p": {
              color: "#3B82F6",
              colorStart: "#60A5FA",
              colorEnd: "#2563EB",
              dotClass: "bg-blue-500",
            },
            "720p": {
              color: "#10B981",
              colorStart: "#34D399",
              colorEnd: "#059669",
              dotClass: "bg-emerald-500",
            },
            "480p": {
              color: "#F59E0B",
              colorStart: "#FBBF24",
              colorEnd: "#D97706",
              dotClass: "bg-amber-500",
            },
            SD: {
              color: "#0EA5E9",
              colorStart: "#38BDF8",
              colorEnd: "#0284C7",
              dotClass: "bg-sky-500",
            },
            Other: {
              color: "#71717A",
              colorStart: "#A1A1AA",
              colorEnd: "#52525B",
              dotClass: "bg-ink-muted",
            },
          };
          return Charts.buildPieSlices(
            analytics.storage_by_resolution.map((slice) => {
              const style = resPalette[slice.key] || resPalette.Other;
              const fc = Number(slice.file_count || 0);
              return {
                key: slice.key,
                label: slice.label,
                value: Number(slice.size_bytes || 0),
                fileCount: fc,
                sublabel: `${fc} ${fc === 1 ? "file" : "files"}`,
                filterPreset: {
                  resolution: slice.key,
                  sort: C.SORT_FIELDS.SIZE_BYTES,
                  order: C.SORT_ORDERS.DESC,
                },
                color: style.color,
                colorStart: style.colorStart,
                colorEnd: style.colorEnd,
                dotClass: style.dotClass,
              };
            })
          );
        }

        return Charts.buildPieSlices([
          {
            key: "mkv",
            label: "MKV Video",
            value: s.mkv_bytes || 0,
            fileCount: s.mkv_count || 0,
            sublabel: `${s.mkv_count || 0} files`,
            filterPreset: null,
            color: "#3B82F6",
            colorStart: "#60A5FA",
            colorEnd: "#2563EB",
            dotClass: "bg-blue-500",
          },
          {
            key: "mp4",
            label: "MP4 Video",
            value: s.mp4_bytes || 0,
            fileCount: s.mp4_count || 0,
            sublabel: `${s.mp4_count || 0} files`,
            filterPreset: null,
            color: "#10B981",
            colorStart: "#34D399",
            colorEnd: "#059669",
            dotClass: "bg-emerald-500",
          },
          {
            key: "other",
            label: "Other / Non-Indexed",
            value: s.other_used_bytes || 0,
            fileCount: 0,
            sublabel: "Disk used outside index",
            filterPreset: null,
            color: "#71717A",
            colorStart: "#A1A1AA",
            colorEnd: "#52525B",
            dotClass: "bg-ink-muted",
          },
        ]);
      },

      get activeStorageSegment() {
        if (!this.hoveredStorageKey) return null;
        return (
          this.dashboardStorageSegments.find(
            (seg) => seg.key === this.hoveredStorageKey
          ) || null
        );
      },

      get dashboardAudioSegments() {
        const analytics =
          (this.dashboard && this.dashboard.analytics) || {
            total_audio_tracks: 0,
            audio_codecs: [],
            audio_channels: [],
            audio_languages: [],
          };
        const lenses = C.DASHBOARD_LENSES || {};

        if (this.dashAudioLens === lenses.AUDIO_CHANNELS) {
          const chPalette = {
            "7.1": {
              color: "#F43F5E",
              colorStart: "#FB7185",
              colorEnd: "#E11D48",
              dotClass: "bg-rose-500",
            },
            "5.1": {
              color: "#10B981",
              colorStart: "#34D399",
              colorEnd: "#059669",
              dotClass: "bg-emerald-500",
            },
            "2.0": {
              color: "#0EA5E9",
              colorStart: "#38BDF8",
              colorEnd: "#0284C7",
              dotClass: "bg-sky-500",
            },
            "1.0": {
              color: "#F59E0B",
              colorStart: "#FBBF24",
              colorEnd: "#D97706",
              dotClass: "bg-amber-500",
            },
            Other: {
              color: "#71717A",
              colorStart: "#A1A1AA",
              colorEnd: "#52525B",
              dotClass: "bg-ink-muted",
            },
          };
          const items = (analytics.audio_channels || []).map((slice) => {
            const style = chPalette[slice.key] || chPalette.Other;
            return {
              key: slice.key,
              label: slice.label,
              value: slice.count || 0,
              count: slice.count || 0,
              color: style.color,
              colorStart: style.colorStart,
              colorEnd: style.colorEnd,
              dotClass: style.dotClass,
            };
          });
          return Charts.buildPieSlices(items);
        }

        if (this.dashAudioLens === lenses.AUDIO_LANGUAGES) {
          const langRanks = [
            {
              color: "#3B82F6",
              colorStart: "#60A5FA",
              colorEnd: "#2563EB",
              dotClass: "bg-blue-500",
            },
            {
              color: "#10B981",
              colorStart: "#34D399",
              colorEnd: "#059669",
              dotClass: "bg-emerald-500",
            },
            {
              color: "#8B5CF6",
              colorStart: "#A78BFA",
              colorEnd: "#7C3AED",
              dotClass: "bg-violet-500",
            },
            {
              color: "#0EA5E9",
              colorStart: "#38BDF8",
              colorEnd: "#0284C7",
              dotClass: "bg-sky-500",
            },
            {
              color: "#F43F5E",
              colorStart: "#FB7185",
              colorEnd: "#E11D48",
              dotClass: "bg-rose-500",
            },
          ];
          const undStyle = {
            color: "#F59E0B",
            colorStart: "#FBBF24",
            colorEnd: "#D97706",
            dotClass: "bg-amber-500",
          };
          const otherStyle = {
            color: "#71717A",
            colorStart: "#A1A1AA",
            colorEnd: "#52525B",
            dotClass: "bg-ink-muted",
          };
          let taggedIdx = 0;
          const items = (analytics.audio_languages || []).map((slice) => {
            let style = otherStyle;
            if (slice.key === "und") {
              style = undStyle;
            } else if (slice.key !== "other") {
              style = langRanks[taggedIdx % langRanks.length];
              taggedIdx += 1;
            }
            return {
              key: slice.key,
              label: slice.label,
              value: slice.count || 0,
              count: slice.count || 0,
              color: style.color,
              colorStart: style.colorStart,
              colorEnd: style.colorEnd,
              dotClass: style.dotClass,
            };
          });
          return Charts.buildPieSlices(items);
        }

        const palette = {
          EAC3: {
            color: "#10B981",
            colorStart: "#34D399",
            colorEnd: "#059669",
            dotClass: "bg-emerald-500",
          },
          AC3: {
            color: "#0EA5E9",
            colorStart: "#38BDF8",
            colorEnd: "#0284C7",
            dotClass: "bg-sky-500",
          },
          AAC: {
            color: "#F59E0B",
            colorStart: "#FBBF24",
            colorEnd: "#D97706",
            dotClass: "bg-amber-500",
          },
          DTS: {
            color: "#8B5CF6",
            colorStart: "#A78BFA",
            colorEnd: "#7C3AED",
            dotClass: "bg-violet-500",
          },
          TrueHD: {
            color: "#F43F5E",
            colorStart: "#FB7185",
            colorEnd: "#E11D48",
            dotClass: "bg-rose-500",
          },
          Other: {
            color: "#71717A",
            colorStart: "#A1A1AA",
            colorEnd: "#52525B",
            dotClass: "bg-ink-muted",
          },
        };
        const items = (analytics.audio_codecs || []).map((slice) => {
          const style = palette[slice.key] || palette.Other;
          return {
            key: slice.key,
            label: slice.label,
            value: slice.count || 0,
            count: slice.count || 0,
            color: style.color,
            colorStart: style.colorStart,
            colorEnd: style.colorEnd,
            dotClass: style.dotClass,
          };
        });
        return Charts.buildPieSlices(items);
      },

      get activeAudioSegment() {
        if (!this.hoveredAudioKey) return null;
        return (
          this.dashboardAudioSegments.find(
            (seg) => seg.key === this.hoveredAudioKey
          ) || null
        );
      },

      get dashboardStoragePieSvg() {
        return Charts.renderPieSvg(
          this.dashboardStorageSegments,
          this.hoveredStorageKey,
          { idPrefix: "st", ariaLabel: "Storage breakdown chart" }
        );
      },

      get dashboardAudioPieSvg() {
        return Charts.renderPieSvg(
          this.dashboardAudioSegments,
          this.hoveredAudioKey,
          { idPrefix: "ac", ariaLabel: "Audio codec distribution chart" }
        );
      },

      syncStoragePieChart(el) {
        const slices = this.dashboardStorageSegments;
        const activeKey = this.hoveredStorageKey;
        Charts.patchPieSvg(el, slices, activeKey, {
          idPrefix: "st",
          ariaLabel: "Storage breakdown chart",
        });
      },

      syncAudioPieChart(el) {
        const slices = this.dashboardAudioSegments;
        const activeKey = this.hoveredAudioKey;
        Charts.patchPieSvg(el, slices, activeKey, {
          idPrefix: "ac",
          ariaLabel: "Audio codec distribution chart",
        });
      },

      _sliceKeyFromEvent(event) {
        const el =
          event && event.target && typeof event.target.closest === "function"
            ? event.target.closest("[data-slice-key]")
            : null;
        return el ? el.getAttribute("data-slice-key") : null;
      },

      onStoragePieMove(event) {
        const key = this._sliceKeyFromEvent(event);
        if (this.hoveredStorageKey !== key) {
          this.hoveredStorageKey = key;
        }
      },

      onStorageSegmentClick(seg) {
        if (!seg) return;
        if (seg.filterPreset) {
          this.openLibraryPreset(seg.filterPreset);
          return;
        }
        this.hoveredStorageKey = this.hoveredStorageKey === seg.key ? null : seg.key;
      },

      onStoragePieClick(event) {
        const key = this._sliceKeyFromEvent(event);
        if (!key) return;
        const seg = this.dashboardStorageSegments.find((s) => s.key === key);
        if (seg && seg.filterPreset) {
          this.openLibraryPreset(seg.filterPreset);
          return;
        }
        this.hoveredStorageKey = this.hoveredStorageKey === key ? null : key;
      },

      onAudioPieMove(event) {
        const key = this._sliceKeyFromEvent(event);
        if (this.hoveredAudioKey !== key) {
          this.hoveredAudioKey = key;
        }
      },

      onAudioPieClick(event) {
        const key = this._sliceKeyFromEvent(event);
        if (!key) return;
        const lenses = C.DASHBOARD_LENSES || {};
        if (this.dashAudioLens === lenses.AUDIO_CHANNELS) {
          this.openLibraryPreset({ audioChannels: key });
          return;
        }
        if (this.dashAudioLens === lenses.AUDIO_LANGUAGES) {
          this.openLibraryPreset({ audioLanguage: key });
          return;
        }
        this.openLibraryPreset({ audioCodec: key });
      },

      get dashboardTotalAudioTracks() {
        return Number(
          (this.dashboard &&
            this.dashboard.analytics &&
            this.dashboard.analytics.total_audio_tracks) ||
            0
        );
      },

      get dashboardResolutions() {
        return (
          (this.dashboard &&
            this.dashboard.analytics &&
            this.dashboard.analytics.resolutions) ||
          []
        );
      },

      get dashboardVideoCodecs() {
        return (
          (this.dashboard &&
            this.dashboard.analytics &&
            this.dashboard.analytics.video_codecs) ||
          []
        );
      },

      get dashboardDynamicRanges() {
        return (
          (this.dashboard &&
            this.dashboard.analytics &&
            this.dashboard.analytics.dynamic_ranges) ||
          []
        );
      },

      // Computed view getters
      get isScanRunning() {
        return this.scanProgress.status === C.STATUSES.RUNNING;
      },

      get isJobRunning() {
        return (
          this.liveJob.status === C.STATUSES.RUNNING ||
          Boolean(this.jobsState.active)
        );
      },

      get activeJobId() {
        if (this.liveJob.status === C.STATUSES.RUNNING && this.liveJob.job_id) {
          return this.liveJob.job_id;
        }
        return this.jobsState.active ? this.jobsState.active.id : null;
      },

      get activeJobFilepath() {
        if (this.liveJob.status === C.STATUSES.RUNNING && this.liveJob.filepath) {
          return this.liveJob.filepath;
        }
        return this.jobsState.active ? this.jobsState.active.filepath : "";
      },

      get activeJobPercent() {
        if (this.liveJob.status === C.STATUSES.RUNNING) {
          return Number(this.liveJob.percent || 0);
        }
        return this.jobsState.active ? Number(this.jobsState.active.progress || 0) : 0;
      },

      get activeJobStageLabel() {
        if (this.liveJob.status === C.STATUSES.RUNNING && this.liveJob.stage_label) {
          return this.liveJob.stage_label;
        }
        return (
          (this.jobsState.active && this.jobsState.active.stage) ||
          "Processing job"
        );
      },

      shouldShowFileCoverThumb(file) {
        if (!file || !file.id || !file.has_cover_art) return false;
        if (!this.userSettings || this.userSettings.show_grid_thumbnails === false) {
          return false;
        }
        return !this.thumbLoadErrors[file.id];
      },

      fileThumbnailUrl(file) {
        if (!file || !file.id) return "";
        const mtimeTag = Math.max(0, Math.floor(Number(file.mtime) || 0));
        return `${C.API.FILES}/${file.id}/thumbnail?v=${mtimeTag}`;
      },

      markThumbError(fileId) {
        if (!fileId) return;
        this.thumbLoadErrors = { ...this.thumbLoadErrors, [fileId]: true };
      },

      // Per-file active/queued job lookup (used for per-file locks & inline progress)
      getFileJobStatus(file) {
        if (!file || !file.filepath) return null;
        const target = file.filepath;
        if (this.isJobRunning && this.activeJobFilepath === target) {
          return {
            state: C.STATUSES.RUNNING,
            jobId: this.activeJobId,
            percent: Math.max(
              C.JOB_UI.MIN_PROGRESS_PCT,
              Math.min(C.JOB_UI.MAX_PROGRESS_PCT, this.activeJobPercent)
            ),
            stageLabel: this.activeJobStageLabel,
            speed:
              this.liveJob.status === C.STATUSES.RUNNING && this.liveJob.speed
                ? this.liveJob.speed
                : null,
            eta:
              this.liveJob.status === C.STATUSES.RUNNING && this.liveJob.eta
                ? this.liveJob.eta
                : null,
          };
        }
        const queued = Array.isArray(this.jobsState.queued)
          ? this.jobsState.queued
          : [];
        const qIndex = queued.findIndex((item) => item && item.filepath === target);
        if (qIndex >= 0) {
          return {
            state: C.STATUSES.QUEUED,
            jobId: queued[qIndex].id,
            queuePos: qIndex + 1,
            percent: C.JOB_UI.MIN_PROGRESS_PCT,
            stageLabel: "Queued",
            speed: null,
            eta: null,
          };
        }
        return null;
      },

      isFileBusy(file) {
        return Boolean(this.getFileJobStatus(file));
      },

      _busyLockMessage(file) {
        const status = this.getFileJobStatus(file);
        const name = (file && file.filename) || "This file";
        if (status && status.state === C.STATUSES.QUEUED) {
          return `${name} is in the job queue (#${status.queuePos}). Remove it from the job queue to modify.`;
        }
        return `${name} is currently processing.`;
      },

      isFileScanning(file) {
        return Boolean(
          file && file.scan_status === C.FILE_SCAN_STATUSES.SCANNING
        );
      },

      isFileScanError(file) {
        return Boolean(file && file.scan_status === C.FILE_SCAN_STATUSES.ERROR);
      },

      isFileLocked(file) {
        return this.isFileBusy(file) || this.isFileScanning(file);
      },

      fileBusyProgressWidth(file) {
        const status = this.getFileJobStatus(file);
        if (!status || status.state !== C.STATUSES.RUNNING) {
          return C.JOB_UI.MIN_PROGRESS_PCT;
        }
        return Math.max(
          C.JOB_UI.INDETERMINATE_FLOOR_PCT,
          Math.min(C.JOB_UI.MAX_PROGRESS_PCT, Number(status.percent || 0))
        );
      },

      get editorActiveJob() {
        return this.getFileJobStatus(this.editorFile);
      },

      get isEditorFileBusy() {
        return Boolean(this.editorActiveJob);
      },

      get isContextMenuFileBusy() {
        return Boolean(
          this.contextMenu &&
            this.contextMenu.kind === "file" &&
            this.isFileBusy(this.contextMenu.file)
        );
      },

      get isContextMenuFileScanning() {
        return Boolean(
          this.contextMenu &&
            this.contextMenu.kind === "file" &&
            this.isFileScanning(this.contextMenu.file)
        );
      },

      get isContextMenuFileScanError() {
        return Boolean(
          this.contextMenu &&
            this.contextMenu.kind === "file" &&
            this.isFileScanError(this.contextMenu.file)
        );
      },

      get hasOpenOverlay() {
        return Boolean(
          this.scanMenuOpen ||
            this.contextMenu ||
            this.infoModal ||
            this.deleteModal ||
            this.renameModal ||
            this.newFolderModal ||
            this.folderPickerOpen ||
            this.importTrackModal ||
            this.addAudioModalOpen ||
            this.addAudioMenuOpen ||
            this.jobsDrawerOpen
        );
      },

      // Mobile Pull-to-Refresh handlers
      onPullTouchStart(event) {
        const touches = event && event.touches ? event.touches : [];
        const windowScrollY = window.scrollY || document.documentElement?.scrollTop || 0;
        const libraryScrollTop =
          this.route === C.ROUTES.LIBRARY && this.$refs?.libraryScrollContainer
            ? this.$refs.libraryScrollContainer.scrollTop || 0
            : 0;
        const scrollY = Math.max(windowScrollY, libraryScrollTop);
        const canStart = PR.shouldStartPull(
          {
            viewportWidth: window.innerWidth,
            scrollY,
            touchCount: touches.length,
            hasOpenOverlay: this.hasOpenOverlay,
            isBlockedTarget: PR.isInteractiveOrScrollableTarget(event?.target),
            isRefreshing: this.pullRefresh.refreshing,
          },
          C.PULL_REFRESH
        );
        if (!canStart) {
          this.pullRefresh.tracking = false;
          return;
        }
        this.pullRefresh.tracking = true;
        this.pullRefresh.pulling = false;
        this.pullRefresh.startX = touches[0].clientX;
        this.pullRefresh.startY = touches[0].clientY;
      },

      onPullTouchMove(event) {
        if (!this.pullRefresh.tracking || this.pullRefresh.refreshing) return;
        const touches = event && event.touches ? event.touches : [];
        if (touches.length !== 1) {
          this.resetPullRefresh();
          return;
        }
        const scrollY = window.scrollY || document.documentElement?.scrollTop || 0;
        if (scrollY > 0 && !this.pullRefresh.pulling) {
          this.pullRefresh.tracking = false;
          return;
        }

        const deltaX = touches[0].clientX - this.pullRefresh.startX;
        const deltaY = touches[0].clientY - this.pullRefresh.startY;
        if (!this.pullRefresh.pulling) {
          if (deltaY <= C.PULL_REFRESH.MIN_START_DELTA_PX) return;
          if (PR.shouldCancelForHorizontal(deltaX, deltaY)) {
            this.pullRefresh.tracking = false;
            return;
          }
          this.pullRefresh.pulling = true;
        }

        if (deltaY <= 0) {
          this.resetPullRefresh();
          return;
        }

        if (event.cancelable) {
          event.preventDefault();
        }
        const metrics = PR.computePullMetrics(deltaY, C.PULL_REFRESH);
        this.pullRefresh.distancePx = metrics.distancePx;
        this.pullRefresh.progress = metrics.progress;
        this.pullRefresh.ready = metrics.ready;
        this.pullRefresh.rotationDeg = metrics.rotationDeg;
      },

      onPullTouchEnd() {
        if (!this.pullRefresh.tracking && !this.pullRefresh.pulling) return;
        const shouldTrigger =
          this.pullRefresh.pulling &&
          this.pullRefresh.ready &&
          !this.pullRefresh.refreshing;
        this.pullRefresh.tracking = false;
        this.pullRefresh.pulling = false;

        if (shouldTrigger) {
          this.performPullRefresh();
        } else {
          this.resetPullRefresh();
        }
      },

      resetPullRefresh() {
        this.pullRefresh.tracking = false;
        this.pullRefresh.pulling = false;
        if (!this.pullRefresh.refreshing) {
          this.pullRefresh.distancePx = 0;
          this.pullRefresh.progress = 0;
          this.pullRefresh.ready = false;
          this.pullRefresh.rotationDeg = 0;
        }
      },

      async performPullRefresh() {
        if (this.pullRefresh.refreshing) return;
        this.pullRefresh.refreshing = true;
        this.pullRefresh.distancePx = C.PULL_REFRESH.HOLD_HEIGHT_PX;
        this.pullRefresh.ready = true;
        this._connectStreams();

        const minDelay = new Promise((resolve) =>
          setTimeout(resolve, C.PULL_REFRESH.MIN_REFRESH_DISPLAY_MS)
        );
        try {
          const tasks = [minDelay];
          if (this.route === C.ROUTES.LIBRARY) {
            tasks.push(
              this.loadLibrary({ preserveSelection: true }),
              this.loadDashboard(),
              this.loadJobs()
            );
          } else if (this.route === C.ROUTES.JOBS) {
            tasks.push(this.loadJobs(), this.loadDashboard());
          } else if (this.route === C.ROUTES.SETTINGS) {
            tasks.push(
              this.loadUserSettings(),
              this.loadMaintenanceStatus(),
              this.loadJobs()
            );
          } else if (this.route === C.ROUTES.EDITOR && this.editorFileId) {
            if (this.editorHasChanges || this.isEditorFileBusy) {
              tasks.push(this.loadJobs());
              if (this.editorHasChanges) {
                this.showToast(
                  "Synced live status (unsaved edits preserved).",
                  "info"
                );
              }
            } else {
              tasks.push(this.loadEditor(this.editorFileId), this.loadJobs());
            }
          } else {
            tasks.push(this.loadDashboard(), this.loadJobs());
          }
          await Promise.all(tasks);
        } finally {
          this.pullRefresh.refreshing = false;
          this.resetPullRefresh();
        }
      },

      get scanPercent() {
        if (!this.scanProgress.total || this.scanProgress.total <= 0) return 0;
        return Math.min(
          100,
          Math.round((this.scanProgress.scanned / this.scanProgress.total) * 100)
        );
      },

      get showGlobalPill() {
        return (this.isJobRunning || this.isScanRunning) && !this.jobsDrawerOpen;
      },

      get globalPillText() {
        if (this.isJobRunning) {
          const speedPart = this.liveJob.speed ? ` (${this.liveJob.speed})` : "";
          return `${this.activeJobStageLabel} — ${this.activeJobPercent}%${speedPart}`;
        }
        if (this.isScanRunning) {
          if (this.scanProgress.total > 0) {
            return `Scanning ${this.scanProgress.scanned} / ${this.scanProgress.total} files (${this.scanPercent}%)`;
          }
          return "Scanning library…";
        }
        return "";
      },

      get globalPillPercent() {
        if (this.isJobRunning) return this.activeJobPercent;
        if (this.isScanRunning) return this.scanPercent;
        return 0;
      },

      get hasLibraryEmptyState() {
        return (
          !this.dashboardLoading &&
          this.dashboard.stats.total_files === 0 &&
          !this.dashboard.stats.last_scan_at &&
          !this.isScanRunning
        );
      },

      get hasActiveFilters() {
        return Boolean(
          this.needsConversion ||
          this.noDolby ||
          this.hasMetadata ||
          this.audioType ||
          this.audioCodec ||
          this.videoCodec ||
          this.resolution ||
          this.dynamicRange ||
          this.audioChannels ||
          this.audioLanguage ||
          this.noSubtitles ||
          this.isGlobalSearchActive
        );
      },

      get visibleWorkflowChips() {
        const candidates = [
          {
            key: C.FILTERS.ALL,
            label: "All",
            count: null,
            active:
              !this.needsConversion &&
              !this.noDolby &&
              !this.hasMetadata &&
              !this.audioType &&
              !this.audioCodec &&
              !this.videoCodec &&
              !this.resolution &&
              !this.dynamicRange &&
              !this.audioChannels &&
              !this.audioLanguage &&
              !this.noSubtitles,
          },
          {
            key: C.FILTERS.NEEDS_CONVERSION,
            label: "Needs Conversion",
            count: this.facets.needs_conversion || 0,
            active: this.needsConversion,
          },
          {
            key: C.FILTERS.HAS_METADATA,
            label: "Has Metadata",
            count: this.facets.has_metadata || 0,
            active: this.hasMetadata,
          },
        ];
        return candidates.filter(
          (chip) =>
            chip.key === C.FILTERS.ALL || chip.count > 0 || chip.active
        );
      },

      get visibleAudioChips() {
        const list = Array.isArray(this.facets.audio_types)
          ? [...this.facets.audio_types]
          : [];
        if (
          this.audioType &&
          !list.some(
            (entry) =>
              entry.label.toLowerCase() === this.audioType.toLowerCase()
          )
        ) {
          list.unshift({ label: this.audioType, count: 0 });
        }
        return list;
      },

      // Editor computed getters
      get editorAudioTracks() {
        if (!this.editor) return [];
        return this.editor.tracks.filter((t) => t.type === "audio");
      },

      get editorAvailableAudioOptions() {
        return Ed.getAvailableAddAudioOptions(this.editor);
      },

      get editorRecommendedAudioOptions() {
        return Ed.getRecommendedAddAudioOptions(
          this.editor,
          this.activePlaybackProfile
        ).map((opt) => ({
          ...opt,
          defaultBitrate: this._resolveDefaultBitrateForDraft(
            opt.codec,
            opt.resolvedChannels
          ),
        }));
      },

      get editorDraftOption() {
        if (!this.addAudioDraft) return null;
        return (
          this.editorAvailableAudioOptions.find(
            (opt) => opt.codec === this.addAudioDraft.targetCodec
          ) || null
        );
      },

      get editorDraftCodecMeta() {
        if (!this.addAudioDraft) return null;
        return Ed.getCodecMeta(this.addAudioDraft.targetCodec);
      },

      get editorDraftSourceTrack() {
        if (!this.addAudioDraft || !this.editor) return null;
        return (
          this.editor.tracks.find(
            (t) =>
              t.type === "audio" &&
              t.id === Number(this.addAudioDraft.sourceTrackId)
          ) || null
        );
      },

      get editorDraftAvailableCodecs() {
        return Ed.getAvailableTargetCodecs(this.editorDraftSourceTrack);
      },

      get editorDraftChannelOptions() {
        if (!this.addAudioDraft) return [];
        return Ed.getAvailableChannelOptions(
          this.editorDraftSourceTrack,
          this.addAudioDraft.targetCodec
        );
      },

      get editorDraftResolvedChannels() {
        if (!this.addAudioDraft) return C.EDITOR.MAX_DOLBY_CHANNELS;
        const src = this.editorDraftSourceTrack;
        return Ed.resolveTargetChannels(
          src ? src.audio_channels : C.EDITOR.MAX_DOLBY_CHANNELS,
          this.addAudioDraft.targetCodec,
          this.addAudioDraft.targetChannels
        );
      },

      get editorDraftBitrateConfig() {
        if (!this.addAudioDraft) return { options: [], defaultBitrate: null };
        return Ed.getBitrateConfigFor(
          this.addAudioDraft.targetCodec,
          this.editorDraftResolvedChannels
        );
      },

      get editorDraftNotices() {
        if (!this.addAudioDraft) return [];
        return Ed.getConversionNotices(
          this.editorDraftSourceTrack,
          this.addAudioDraft.targetCodec,
          this.editorDraftResolvedChannels
        );
      },

      get editorDraftBlockStatus() {
        if (!this.addAudioDraft) return { blocked: false, reason: null };
        return Ed.isConversionCombinationBlocked(
          this.editorDraftSourceTrack,
          this.addAudioDraft.targetCodec,
          this.editorDraftResolvedChannels
        );
      },

      get editorDraftAdvancedSummary() {
        if (!this.addAudioDraft) return "";
        const meta = this.editorDraftCodecMeta;
        const codecName = (meta && (meta.displayLabel || meta.label)) || "";
        const resolvedCh = this.editorDraftResolvedChannels;
        const chLabel = Ed.formatChannelLabel(resolvedCh);
        const src = this.editorDraftSourceTrack;
        const srcCh = Number((src && src.audio_channels) || 0) || C.EDITOR.MAX_DOLBY_CHANNELS;
        const chText =
          this.addAudioDraft.targetChannels === null && resolvedCh === srcCh
            ? `Match (${chLabel})`
            : chLabel;
        const rateText =
          meta && meta.isLossless ? "Lossless" : this.addAudioDraft.bitrate || "";
        const langText = this.addAudioDraft.language || C.EDITOR.DEFAULT_LANGUAGE;
        return [codecName, chText, rateText, langText].filter(Boolean).join(" · ");
      },

      get editorDisplayTracks() {
        return Ed.buildDisplayTracks(this.editor);
      },

      get editorAllVideoUnchecked() {
        return Ed.hasUncheckedAllVideo(this.editor);
      },

      get editorKeptTrackCount() {
        return Ed.countEffectiveKeptTracks(this.editor);
      },

      get editorHasChanges() {
        return Ed.hasPendingChanges(this.editor, this.editorFile);
      },

      get editorIsMp4() {
        return Ed.isMp4File(this.editorFile);
      },

      get editorOutputPreview() {
        return Ed.buildOutputPreview(this.editor, this.editorFile);
      },

      get folderTreeRows() {
        return FT.flattenFolderTree(this.folderTree);
      },

      get isDirView() {
        return this.libraryView === C.LIBRARY_VIEWS.DIR;
      },

      get librarySearchPlaceholder() {
        const count =
          Number(this.totalFiles) > 0
            ? Number(this.totalFiles)
            : Number((this.dashboard && this.dashboard.stats && this.dashboard.stats.total_files) || 0);
        if (count > 0) {
          return `Search ${count.toLocaleString()} ${count === 1 ? "file" : "files"}…`;
        }
        return "Search files, movies, shows…";
      },

      get isGlobalSearchActive() {
        return Boolean(String(this.appliedSearchQuery || "").trim());
      },

      get showSubfolders() {
        return this.isDirView && !this.isGlobalSearchActive;
      },

      get isGridLayout() {
        return (
          this.showSubfolders &&
          this.folderLayout === C.FOLDER_LAYOUTS.GRID
        );
      },

      get currentDirBreadcrumbs() {
        return FT.buildBreadcrumbs(this.currentDir, { includeRoot: true });
      },

      get editorFolderBreadcrumbs() {
        if (!this.editorFile) return [];
        return FT.buildBreadcrumbs(this.editorFile.folder, { includeRoot: false });
      },

      get editorParentFolderPath() {
        if (!this.editorFile) return C.FOLDER_TREE.ROOT_PATH;
        return FT.normalizeFolderPath(this.editorFile.folder);
      },

      get editorBackLabel() {
        const origin = this._getLibraryReturnOrigin();
        if (origin && origin.hash) {
          const base =
            origin.view === C.LIBRARY_VIEWS.LIST
              ? "Back to Library"
              : `Back to ${FT.getParentFolderName(
                  origin.folder || (this.editorFile && this.editorFile.folder)
                )}`;
          return origin.page > C.PAGINATION.DEFAULT_PAGE
            ? `${base} (Page ${origin.page})`
            : base;
        }
        if (!this.editorFile) return "Back to Parent";
        return `Back to ${FT.getParentFolderName(this.editorFile.folder)}`;
      },

      get canGoUpDir() {
        return FT.normalizeFolderPath(this.currentDir) !== C.FOLDER_TREE.ROOT_PATH;
      },

      get selectedFileCount() {
        return this.selectedFileIds.length;
      },

      get allPageFilesSelected() {
        if (!this.files || this.files.length === 0) return false;
        const selectable = this.files.filter((file) => !this.isFileLocked(file));
        if (selectable.length === 0) return false;
        return selectable.every((file) => this.selectedFileIds.includes(file.id));
      },

      get singleSelectedFile() {
        if (this.selectedFileIds.length !== 1) return null;
        return this.files.find((f) => f.id === this.selectedFileIds[0]) || null;
      },

      get editorCanSubmit() {
        if (!this.editor || !this.editorFile) return false;
        if (this.isEditorFileBusy) return false;
        if (this.editorSubmitting || !this.editorHasChanges) return false;
        if (this.editorKeptTrackCount === 0) return false;
        if (this.editorAllVideoUnchecked && !this.editor.confirmNoVideo) return false;
        if (this.editorOutputPreview.isSaveAsInvalid) return false;
        return true;
      },

      get editorPendingSummary() {
        return Ed.buildPendingSummary(this.editor, this.editorFile);
      },

      // Routing helpers
      get isSettingsOpen() {
        return Boolean(this.settingsModalOpen || this.route === C.ROUTES.SETTINGS);
      },

      openSettings(sectionId = null) {
        this.scanMenuOpen = false;
        this.folderBarMenuOpen = false;
        this.closeContextMenu();
        const nextSection = sectionId || C.SETTINGS_SECTIONS.APPEARANCE;
        this.activeSettingsSection = nextSection;
        this.mobileSettingsSection = nextSection;
        this.settingsModalOpen = true;
        this.authError = null;
        this.loadUserSettings();
        this.loadMaintenanceStatus();
        if (this.isLocalAuthEnabled && this.authAuthenticated) {
          this.loadAuthUsers();
          this.loadApiKeys();
        }
      },

      closeSettings() {
        this.settingsModalOpen = false;
        if (this.route === C.ROUTES.SETTINGS) {
          this.navigate(this._lastNonSettingsRoute || C.ROUTES.DASHBOARD);
        }
      },

      selectWebSettingsSection(sectionId) {
        if (!sectionId) return;
        this.activeSettingsSection = sectionId;
        this.mobileSettingsSection = sectionId;
      },

      toggleMobileSettingsSection(sectionId) {
        if (!sectionId) return;
        if (this.mobileSettingsSection === sectionId) {
          this.mobileSettingsSection = null;
        } else {
          this.mobileSettingsSection = sectionId;
          this.activeSettingsSection = sectionId;
        }
      },

      _buildCurrentLibraryHash() {
        return buildHash({
          route: C.ROUTES.LIBRARY,
          view: this.libraryView,
          needsConversion: this.needsConversion,
          noDolby: this.noDolby,
          hasMetadata: this.hasMetadata,
          audioType: this.audioType,
          audioCodec: this.audioCodec,
          videoCodec: this.videoCodec,
          resolution: this.resolution,
          dynamicRange: this.dynamicRange,
          audioChannels: this.audioChannels,
          audioLanguage: this.audioLanguage,
          noSubtitles: this.noSubtitles,
          folder: this.isDirView
            ? this.currentDir === C.FOLDER_TREE.ROOT_PATH
              ? ""
              : this.currentDir
            : "",
          q: this.searchQuery,
          sort: this.sortField,
          order: this.sortOrder,
          page: this.page,
        });
      },

      _captureLibraryOrigin(fileId = null) {
        if (this.route !== C.ROUTES.LIBRARY) return null;
        const hash = this._buildCurrentLibraryHash();
        const container = this.$refs && this.$refs.libraryScrollContainer;
        const snapshot = LR.saveOrigin({
          hash,
          page: this.page,
          view: this.libraryView,
          folder: this.isDirView ? this.currentDir : C.FOLDER_TREE.ROOT_PATH,
          scrollTop: container ? container.scrollTop : 0,
          windowScrollY:
            window.scrollY ||
            (document.documentElement && document.documentElement.scrollTop) ||
            0,
          fileId,
        });
        this.lastLibraryHash = hash;
        this._libraryReturnState = snapshot;
        return snapshot;
      },

      _getLibraryReturnOrigin() {
        if (this._libraryReturnState && this._libraryReturnState.hash) {
          return this._libraryReturnState;
        }
        const loaded = LR.loadOrigin();
        if (loaded) {
          this._libraryReturnState = loaded;
        }
        return loaded;
      },

      _cancelPendingScrollRestore() {
        if (this._scrollRestoreTimer !== null) {
          clearTimeout(this._scrollRestoreTimer);
          this._scrollRestoreTimer = null;
        }
        this._pendingLibraryScrollRestore = null;
      },

      _clearLibraryReturnOrigin({ clearPendingScroll = true } = {}) {
        this._libraryReturnState = null;
        LR.clearOrigin();
        if (clearPendingScroll) {
          this._cancelPendingScrollRestore();
        }
      },

      _scheduleLibraryScrollRestore({ consume = true, attempt = 0 } = {}) {
        const pending = this._pendingLibraryScrollRestore;
        if (!pending || this.route !== C.ROUTES.LIBRARY) return;

        if (this._scrollRestoreTimer !== null) {
          clearTimeout(this._scrollRestoreTimer);
          this._scrollRestoreTimer = null;
        }

        const runRestore = () => {
          const currentPending = this._pendingLibraryScrollRestore;
          if (!currentPending || this.route !== C.ROUTES.LIBRARY) return;
          const container =
            (this.$refs && this.$refs.libraryScrollContainer) || null;
          const result = LR.applyScrollPosition({
            container,
            win: typeof window !== "undefined" ? window : null,
            scrollTop: currentPending.scrollTop,
            windowScrollY: currentPending.windowScrollY,
          });
          const maxAttempts =
            (C.SCROLL_RESTORE && C.SCROLL_RESTORE.MAX_RESTORE_ATTEMPTS) || 4;
          const retryDelayMs =
            (C.SCROLL_RESTORE && C.SCROLL_RESTORE.RETRY_DELAY_MS) || 50;
          if (result.needsRetry && attempt < maxAttempts) {
            this._scrollRestoreTimer = setTimeout(() => {
              this._scrollRestoreTimer = null;
              this._scheduleLibraryScrollRestore({
                consume,
                attempt: attempt + 1,
              });
            }, retryDelayMs);
            return;
          }
          if (consume) {
            this._pendingLibraryScrollRestore = null;
            this._libraryReturnState = null;
            LR.clearOrigin();
          }
        };

        const scheduleFrame = () => {
          if (typeof requestAnimationFrame === "function") {
            requestAnimationFrame(runRestore);
          } else {
            runRestore();
          }
        };

        if (typeof this.$nextTick === "function") {
          this.$nextTick(scheduleFrame);
        } else {
          scheduleFrame();
        }
      },

      returnToLibraryOrigin() {
        const origin = this._getLibraryReturnOrigin();
        if (!origin || !origin.hash) return false;
        this.scanMenuOpen = false;
        this.closeContextMenu();
        this.libraryView = origin.view;
        this.lastLibraryHash = origin.hash;
        this._pendingLibraryScrollRestore = { ...origin };
        if (window.location.hash !== origin.hash) {
          window.location.hash = origin.hash;
        } else {
          this._applyHashState(origin.hash, true);
        }
        return true;
      },

      _applyHashState(rawHash, shouldLoad) {
        this.closeContextMenu();
        const prevRoute = this.route;
        const parsed = parseRouteFromHash(rawHash);
        if (parsed.route !== C.ROUTES.SETTINGS) {
          this._lastNonSettingsRoute = parsed.route;
          this.settingsModalOpen = false;
        } else {
          this.settingsModalOpen = true;
        }
        this.route = parsed.route;

        if (parsed.route === C.ROUTES.LIBRARY) {
          const origin = this._getLibraryReturnOrigin();
          if (parsed.view) {
            this.libraryView = parsed.view;
          } else if (
            origin &&
            LR.hashesMatch(origin.hash, rawHash) &&
            origin.view
          ) {
            this.libraryView = origin.view;
          }
          this.needsConversion = Boolean(parsed.needsConversion);
          this.noDolby = parsed.noDolby;
          this.hasMetadata = parsed.hasMetadata;
          this.audioType = parsed.audioType;
          this.audioCodec = parsed.audioCodec || "";
          this.videoCodec = parsed.videoCodec || "";
          this.resolution = parsed.resolution || "";
          this.dynamicRange = parsed.dynamicRange || "";
          this.audioChannels = parsed.audioChannels || "";
          this.audioLanguage = parsed.audioLanguage || "";
          this.noSubtitles = Boolean(parsed.noSubtitles);
          this.currentDir = this.isDirView
            ? FT.normalizeFolderPath(parsed.folder || C.FOLDER_TREE.ROOT_PATH)
            : C.FOLDER_TREE.ROOT_PATH;
          this.searchQuery = parsed.q;
          this.appliedSearchQuery = String(parsed.q || "").trim();
          this.sortField = parsed.sort;
          this.sortOrder = parsed.order;
          this.page = parsed.page;
          this.lastLibraryHash = buildHash({
            ...parsed,
            view: this.libraryView,
            folder: this.isDirView
              ? this.currentDir === C.FOLDER_TREE.ROOT_PATH
                ? ""
                : this.currentDir
              : "",
          });
          if (
            !this._pendingLibraryScrollRestore &&
            prevRoute &&
            prevRoute !== C.ROUTES.LIBRARY &&
            origin &&
            LR.hashesMatch(origin.hash, this.lastLibraryHash)
          ) {
            this._pendingLibraryScrollRestore = { ...origin };
          }
        } else if (parsed.route === C.ROUTES.EDITOR) {
          this.editorFileId = parsed.editorFileId;
        }

        if (!shouldLoad) return;
        this._loadCurrentRoute();
      },

      _syncUrlHash(replace = false) {
        const nextHash = buildHash({
          route: this.route,
          editorFileId: this.editorFileId,
          view: this.libraryView,
          needsConversion: this.needsConversion,
          noDolby: this.noDolby,
          hasMetadata: this.hasMetadata,
          audioType: this.audioType,
          audioCodec: this.audioCodec,
          videoCodec: this.videoCodec,
          resolution: this.resolution,
          dynamicRange: this.dynamicRange,
          audioChannels: this.audioChannels,
          audioLanguage: this.audioLanguage,
          noSubtitles: this.noSubtitles,
          folder: this.isDirView
            ? this.currentDir === C.FOLDER_TREE.ROOT_PATH
              ? ""
              : this.currentDir
            : "",
          q: this.searchQuery,
          sort: this.sortField,
          order: this.sortOrder,
          page: this.page,
        });
        if (this.route === C.ROUTES.LIBRARY) {
          this.lastLibraryHash = nextHash;
        }
        if (window.location.hash === nextHash) return;
        if (replace) {
          window.history.replaceState(null, "", nextHash);
        } else {
          window.location.hash = nextHash;
        }
      },

      _loadCurrentRoute() {
        if (this.route === C.ROUTES.DASHBOARD) {
          this.loadDashboard();
        } else if (this.route === C.ROUTES.LIBRARY) {
          const canPreserveRenderedPage =
            Boolean(this._pendingLibraryScrollRestore) &&
            this.files.length > 0 &&
            LR.hashesMatch(this._lastLoadedLibraryHash, this.lastLibraryHash);
          if (canPreserveRenderedPage) {
            this._scheduleLibraryScrollRestore({ consume: false });
            this.loadLibrary({ clearStale: false, preserveSelection: true });
          } else {
            this.loadLibrary({ clearStale: true });
          }
        } else if (this.route === C.ROUTES.JOBS) {
          this.loadJobs();
        } else if (this.route === C.ROUTES.SETTINGS) {
          this.loadUserSettings();
          this.loadMaintenanceStatus();
          if (this.isLocalAuthEnabled && this.authAuthenticated) {
            this.loadAuthUsers();
            this.loadApiKeys();
          }
        } else if (this.route === C.ROUTES.EDITOR && this.editorFileId) {
          this.loadEditor(this.editorFileId);
        }
      },

      navigate(targetRoute) {
        this.scanMenuOpen = false;
        this.closeContextMenu();
        if (
          this.route === C.ROUTES.LIBRARY &&
          targetRoute !== C.ROUTES.LIBRARY
        ) {
          this._captureLibraryOrigin(null);
        }
        const nextHash = buildHash({
          route: targetRoute,
          view: this.libraryView,
          needsConversion: this.needsConversion,
          noDolby: this.noDolby,
          hasMetadata: this.hasMetadata,
          audioType: this.audioType,
          audioCodec: this.audioCodec,
          videoCodec: this.videoCodec,
          resolution: this.resolution,
          dynamicRange: this.dynamicRange,
          audioChannels: this.audioChannels,
          audioLanguage: this.audioLanguage,
          noSubtitles: this.noSubtitles,
          folder: this.isDirView
            ? this.currentDir === C.FOLDER_TREE.ROOT_PATH
              ? ""
              : this.currentDir
            : "",
          q: this.searchQuery,
          sort: this.sortField,
          order: this.sortOrder,
          page: this.page,
        });
        if (targetRoute === C.ROUTES.LIBRARY) {
          const origin = this._getLibraryReturnOrigin();
          if (origin && LR.hashesMatch(origin.hash, nextHash)) {
            this._pendingLibraryScrollRestore = { ...origin };
          }
        }
        if (window.location.hash !== nextHash) {
          window.location.hash = nextHash;
        } else {
          this.route = targetRoute;
          this._loadCurrentRoute();
        }
      },

      openLibraryPreset(preset = {}) {
        this.scanMenuOpen = false;
        this.closeContextMenu();
        this._clearLibraryReturnOrigin();
        const nextNeedsConversion = Boolean(preset.needsConversion);
        const nextNoDolby = Boolean(preset.noDolby);
        const nextHasMetadata = Boolean(preset.hasMetadata);
        const nextAudioType = preset.audioType || "";
        const nextAudioCodec = preset.audioCodec || "";
        const nextVideoCodec = preset.videoCodec || "";
        const nextResolution = preset.resolution || "";
        const nextDynamicRange = preset.dynamicRange || "";
        const nextAudioChannels = preset.audioChannels || "";
        const nextAudioLanguage = preset.audioLanguage || "";
        const nextNoSubtitles = Boolean(preset.noSubtitles);
        const nextSort = preset.sort || this.sortField;
        const nextOrder = preset.order || this.sortOrder;
        // Dashboard presets search across ALL folders -> switch cleanly to All Files list view
        const presetView =
          nextNeedsConversion ||
          nextNoDolby ||
          nextHasMetadata ||
          nextAudioType ||
          nextAudioCodec ||
          nextVideoCodec ||
          nextResolution ||
          nextDynamicRange ||
          nextAudioChannels ||
          nextAudioLanguage ||
          nextNoSubtitles
            ? C.LIBRARY_VIEWS.LIST
            : this.libraryView;
        const nextHash = buildHash({
          route: C.ROUTES.LIBRARY,
          view: presetView,
          needsConversion: nextNeedsConversion,
          noDolby: nextNoDolby,
          hasMetadata: nextHasMetadata,
          audioType: nextAudioType,
          audioCodec: nextAudioCodec,
          videoCodec: nextVideoCodec,
          resolution: nextResolution,
          dynamicRange: nextDynamicRange,
          audioChannels: nextAudioChannels,
          audioLanguage: nextAudioLanguage,
          noSubtitles: nextNoSubtitles,
          folder: "",
          q: "",
          sort: nextSort,
          order: nextOrder,
          page: C.PAGINATION.DEFAULT_PAGE,
        });
        if (window.location.hash !== nextHash) {
          window.location.hash = nextHash;
        } else {
          this.route = C.ROUTES.LIBRARY;
          this.libraryView = presetView;
          this.needsConversion = nextNeedsConversion;
          this.noDolby = nextNoDolby;
          this.hasMetadata = nextHasMetadata;
          this.audioType = nextAudioType;
          this.audioCodec = nextAudioCodec;
          this.videoCodec = nextVideoCodec;
          this.resolution = nextResolution;
          this.dynamicRange = nextDynamicRange;
          this.audioChannels = nextAudioChannels;
          this.audioLanguage = nextAudioLanguage;
          this.noSubtitles = nextNoSubtitles;
          this.sortField = nextSort;
          this.sortOrder = nextOrder;
          this.currentDir = C.FOLDER_TREE.ROOT_PATH;
          this.searchQuery = "";
          this.appliedSearchQuery = "";
          this.page = C.PAGINATION.DEFAULT_PAGE;
          this.lastLibraryHash = nextHash;
          this.loadLibrary({ clearStale: true });
        }
      },

      openEditor(file) {
        if (!file || !file.id) return;
        if (this.isFileScanning(file)) {
          this.showToast("Scanning metadata — ready in a moment.", "info");
          return;
        }
        if (this.isFileScanError(file)) {
          this.showToast(
            "Metadata scan failed — use Re-scan from the menu to retry.",
            "error"
          );
          return;
        }
        if (this.route === C.ROUTES.LIBRARY) {
          this._captureLibraryOrigin(file.id);
        }
        const targetHash = `#/${C.ROUTES.EDITOR}/${file.id}`;
        if (window.location.hash !== targetHash) {
          window.location.hash = targetHash;
        } else {
          this.loadEditor(file.id);
        }
      },

      openFolderInLibrary(folderPath) {
        this.scanMenuOpen = false;
        this.closeContextMenu();
        this._clearLibraryReturnOrigin();
        const normalized = FT.normalizeFolderPath(folderPath);
        this.libraryView = C.LIBRARY_VIEWS.DIR;
        try {
          window.localStorage.setItem(
            C.STORAGE_KEYS.LIBRARY_VIEW,
            C.LIBRARY_VIEWS.DIR
          );
        } catch (_err) {
          // Safe fallback
        }
        const nextHash = buildHash({
          route: C.ROUTES.LIBRARY,
          view: C.LIBRARY_VIEWS.DIR,
          needsConversion: false,
          noDolby: false,
          hasMetadata: false,
          audioType: "",
          audioCodec: "",
          videoCodec: "",
          resolution: "",
          dynamicRange: "",
          audioChannels: "",
          audioLanguage: "",
          noSubtitles: false,
          folder: normalized === C.FOLDER_TREE.ROOT_PATH ? "" : normalized,
          q: "",
          sort: this.sortField,
          order: this.sortOrder,
          page: C.PAGINATION.DEFAULT_PAGE,
        });
        this.lastLibraryHash = nextHash;
        if (window.location.hash !== nextHash) {
          window.location.hash = nextHash;
        } else {
          this.route = C.ROUTES.LIBRARY;
          this.needsConversion = false;
          this.noDolby = false;
          this.hasMetadata = false;
          this.audioType = "";
          this.audioCodec = "";
          this.videoCodec = "";
          this.resolution = "";
          this.dynamicRange = "";
          this.audioChannels = "";
          this.audioLanguage = "";
          this.noSubtitles = false;
          this.currentDir = normalized;
          this.searchQuery = "";
          this.appliedSearchQuery = "";
          this.page = C.PAGINATION.DEFAULT_PAGE;
          this.loadLibrary({ clearStale: true });
        }
      },

      backToParentFolder() {
        if (this.returnToLibraryOrigin()) {
          return;
        }
        if (this.editorFile) {
          this.openFolderInLibrary(this.editorFile.folder);
          return;
        }
        this.backToLibrary();
      },

      backToLibrary() {
        if (this.returnToLibraryOrigin()) {
          return;
        }
        const target = this.lastLibraryHash || "#/library";
        if (window.location.hash !== target) {
          window.location.hash = target;
        } else {
          this.navigate(C.ROUTES.LIBRARY);
        }
      },

      // Global Jobs Drawer controls
      openJobsDrawer() {
        this.jobsDrawerOpen = true;
        this.loadJobs();
        this.$nextTick(() => this.scrollLogsToBottom(true));
      },

      closeJobsDrawer() {
        this.jobsDrawerOpen = false;
      },

      handleGlobalPillClick() {
        if (this.isJobRunning) {
          this.openJobsDrawer();
        } else {
          this.navigate(C.ROUTES.LIBRARY);
        }
      },

      // Visibility wake-up & single multiplexed SSE lifecycle
      _connectStreams() {
        if (this._hiddenCloseTimer !== null) {
          clearTimeout(this._hiddenCloseTimer);
          this._hiddenCloseTimer = null;
        }
        if (this._eventStream) {
          this._eventStream.ensureConnected();
          return;
        }
        this._eventStream = new Api.ManagedEventSource(C.API.EVENTS_STREAM, {
          [C.SSE_EVENTS.SCAN_PROGRESS]: (payload) =>
            this._handleScanEvent(payload),
          [C.SSE_EVENTS.LIBRARY_CHANGED]: (payload) =>
            this._handleLibraryChangedEvent(payload),
          [C.SSE_EVENTS.JOB_PROGRESS]: (payload) =>
            this._handleJobEvent(payload),
        });
        this._eventStream.connect();
      },

      _disconnectStreams() {
        if (this._hiddenCloseTimer !== null) {
          clearTimeout(this._hiddenCloseTimer);
          this._hiddenCloseTimer = null;
        }
        this._clearScanLiveTimer();
        if (this._eventStream) {
          this._eventStream.close();
          this._eventStream = null;
        }
      },

      handleAppHidden() {
        if (this._hiddenCloseTimer !== null) return;
        // Release HTTP/1.1 SSE socket when tab stays backgrounded so other tabs stay instant
        this._hiddenCloseTimer = setTimeout(() => {
          this._hiddenCloseTimer = null;
          if (document.visibilityState === "hidden") {
            this._disconnectStreams();
          }
        }, C.TIMINGS_MS.SSE_HIDDEN_CLOSE_DELAY);
      },

      handleAppResume({ forceSse = false } = {}) {
        if (this._hiddenCloseTimer !== null) {
          clearTimeout(this._hiddenCloseTimer);
          this._hiddenCloseTimer = null;
        }
        if (this.isLoginRequired) {
          this._disconnectStreams();
          return;
        }
        if (forceSse && this._eventStream) {
          this._eventStream.forceReconnect();
        } else {
          this._connectStreams();
        }
        // In local auth mode, verify session quietly in parallel so expired overnight sessions
        // transition directly to the Sign-In modal with zero stale error banners.
        if (this.isLocalAuthEnabled) {
          Api.getAuthStatus()
            .then((status) => {
              this._applyAuthStatus(status);
              if (this.isLoginRequired) {
                this._disconnectStreams();
                this._clearAuthErrorBanners();
                this.authExpiredNotice = true;
              }
            })
            .catch(() => {});
        }
        this.loadDashboard();
        this.loadJobs();
        if (this.route === C.ROUTES.LIBRARY) {
          this.loadLibrary({ preserveSelection: true });
        }
      },

      _clearScanLiveTimer() {
        if (this._scanLiveTimer !== null) {
          clearTimeout(this._scanLiveTimer);
          this._scanLiveTimer = null;
        }
      },

      _hasBlockingOverlayOpen() {
        return Boolean(
          this.contextMenu ||
            this.folderBarMenuOpen ||
            this.scanMenuOpen ||
            this.infoModal ||
            this.cancelJobModal ||
            this.deleteModal ||
            this.renameModal ||
            this.newFolderModal ||
            this.folderPickerOpen ||
            this.importTrackModal ||
            this.addAudioModalOpen ||
            this.addAudioMenuOpen ||
            this.jobsDrawerOpen ||
            this.settingsModalOpen ||
            this.factoryResetModalOpen ||
            this.testMediaModalOpen ||
            this.logoutConfirmOpen ||
            this.upgradeAuthOpen ||
            this.disableAuthOpen ||
            this.createdApiKeyModal
        );
      },

      _canAutoRefreshLibraryDuringScan() {
        if (this.route !== C.ROUTES.LIBRARY) return false;
        if (Number(this.page) !== C.PAGINATION.DEFAULT_PAGE) return false;
        if (String(this.searchQuery || "").trim() !== "") return false;
        if (String(this.appliedSearchQuery || "").trim() !== "") return false;
        if (this.mobileSelectMode || this.selectedFileIds.length > 0) return false;
        if (this._hasBlockingOverlayOpen()) return false;
        if (this.libraryLoading) return false;
        return true;
      },

      _scheduleLiveScanRefresh(probedCount) {
        if (this._scanLiveInFlight) return;
        const now = Date.now();
        const isFirstPaint = this._scanLiveLastRefreshAt === 0;
        const elapsed = now - this._scanLiveLastRefreshAt;
        const intervalMs = C.TIMINGS_MS.SCAN_LIVE_DASHBOARD_MS;

        if (isFirstPaint || elapsed >= intervalMs) {
          this._clearScanLiveTimer();
          this._runLiveScanRefresh(probedCount);
          return;
        }

        if (this._scanLiveTimer === null) {
          const waitMs = Math.max(0, intervalMs - elapsed);
          this._scanLiveTimer = setTimeout(() => {
            this._scanLiveTimer = null;
            if (this._lastScanStatus !== C.STATUSES.RUNNING) return;
            const latestProbed = Math.max(
              0,
              Number(this.scanProgress.scanned || 0) -
                Number(this.scanProgress.skipped || 0)
            );
            if (latestProbed > this._scanLiveLastProbedCount) {
              this._runLiveScanRefresh(latestProbed);
            }
          }, waitMs);
        }
      },

      async _runLiveScanRefresh(probedCount) {
        if (this._scanLiveInFlight) return;
        this._scanLiveInFlight = true;
        this._scanLiveLastRefreshAt = Date.now();
        this._scanLiveLastProbedCount = probedCount;
        try {
          const tasks = [this.loadDashboard()];
          const libNow = Date.now();
          const libElapsed = libNow - this._scanLiveLastLibraryRefreshAt;
          if (
            this._canAutoRefreshLibraryDuringScan() &&
            (this._scanLiveLastLibraryRefreshAt === 0 ||
              libElapsed >= C.TIMINGS_MS.SCAN_LIVE_LIBRARY_MS)
          ) {
            this._scanLiveLastLibraryRefreshAt = libNow;
            tasks.push(
              this.loadLibrary({ preserveSelection: true, silent: true })
            );
          }
          await Promise.all(tasks);
        } finally {
          this._scanLiveInFlight = false;
          if (this._scanPendingFinalSync) {
            this._scanPendingFinalSync = false;
            this._executeFinalScanSync();
          }
        }
      },

      _flushFinalScanSync() {
        this._clearScanLiveTimer();
        if (this._scanLiveInFlight) {
          this._scanPendingFinalSync = true;
          return;
        }
        this._scanPendingFinalSync = false;
        this._executeFinalScanSync();
      },

      _executeFinalScanSync() {
        this.loadDashboard();
        if (this.route === C.ROUTES.LIBRARY) {
          this.loadLibrary({ preserveSelection: true });
        }
      },

      _handleScanEvent(payload) {
        const prev = this._lastScanStatus;
        this.scanProgress = { ...this.scanProgress, ...payload };
        const nextStatus = payload.status || C.STATUSES.IDLE;
        this._lastScanStatus = nextStatus;

        if (nextStatus === C.STATUSES.RUNNING) {
          if (prev !== C.STATUSES.RUNNING) {
            this._clearScanLiveTimer();
            this._scanLiveLastRefreshAt = 0;
            this._scanLiveLastLibraryRefreshAt = 0;
            this._scanLiveLastProbedCount = 0;
            this._scanPendingFinalSync = false;
          }
          const probedCount = Math.max(
            0,
            Number(payload.scanned || 0) - Number(payload.skipped || 0)
          );
          if (probedCount > 0 && probedCount > this._scanLiveLastProbedCount) {
            this._scheduleLiveScanRefresh(probedCount);
          }
          return;
        }

        if (
          prev === C.STATUSES.RUNNING &&
          nextStatus === C.STATUSES.COMPLETED
        ) {
          this.showToast(
            `Scan complete: ${payload.total} files indexed (${payload.skipped} skipped).`,
            "success"
          );
          this._flushFinalScanSync();
        } else if (
          prev === C.STATUSES.RUNNING &&
          nextStatus === C.STATUSES.FAILED
        ) {
          this.showToast(
            payload.error_message || "Library scan failed.",
            "error"
          );
          this._flushFinalScanSync();
        }
      },

      _handleLibraryChangedEvent(payload) {
        if (!payload) return;

        // 1. Dashboard: silently refresh stat cards in real time
        this.loadDashboard();

        // 2. Library: always auto-reload seamlessly in place (preserve scroll & selection)
        if (this.route === C.ROUTES.LIBRARY) {
          if (this.libraryLoading) {
            this._pendingLibraryReload = true;
          } else {
            this.loadLibrary({ preserveSelection: true });
          }
          return;
        }

        // 3. File Editor: never wipe unsaved edits; only warn if THIS open file changed
        if (this.route === C.ROUTES.EDITOR && this.editorFile) {
          const changedIds = Array.isArray(payload.file_ids)
            ? payload.file_ids
            : [];
          const changedPaths = Array.isArray(payload.filepaths)
            ? payload.filepaths
            : [];
          const matchesOpenFile =
            changedIds.includes(this.editorFile.id) ||
            changedPaths.includes(this.editorFile.filepath);
          if (matchesOpenFile) {
            this.editorExternalChange = {
              action: payload.action === "deleted" ? "deleted" : "updated",
              filename: this.editorFile.filename,
            };
          }
        }
      },

      reloadEditorAfterExternalChange() {
        if (!this.editorFileId) return;
        this.editorExternalChange = null;
        this.loadEditor(this.editorFileId);
      },

      _handleJobEvent(payload) {
        const prev = this._lastJobStatus;
        const incomingJobId = payload.job_id || null;

        // Hydrate rolling ring buffer on reconnect snapshot or clear on new job
        if (Array.isArray(payload.recent_logs)) {
          this._liveLogJobId = incomingJobId;
          this.liveLogs = payload.recent_logs.slice(-C.EDITOR.MAX_CLIENT_LOG_LINES);
          this.$nextTick(() => this.scrollLogsToBottom(false));
        } else if (incomingJobId && incomingJobId !== this._liveLogJobId) {
          this._liveLogJobId = incomingJobId;
          this.liveLogs = [];
        }

        if (payload.log_line) {
          this.liveLogs.push(payload.log_line);
          if (this.liveLogs.length > C.EDITOR.MAX_CLIENT_LOG_LINES) {
            this.liveLogs.splice(
              0,
              this.liveLogs.length - C.EDITOR.MAX_CLIENT_LOG_LINES
            );
          }
          this.$nextTick(() => this.scrollLogsToBottom(false));
        }

        this.liveJob = { ...this.liveJob, ...payload };
        this._lastJobStatus = payload.status || C.STATUSES.IDLE;
        this._pruneBusySelection();

        if (prev !== C.STATUSES.RUNNING && payload.status === C.STATUSES.RUNNING) {
          this.loadJobs();
        }

        const terminalStatuses = [
          C.STATUSES.COMPLETED,
          C.STATUSES.FAILED,
          C.STATUSES.CANCELLED,
        ];
        if (
          prev === C.STATUSES.RUNNING &&
          terminalStatuses.includes(payload.status)
        ) {
          this.jobsState = {
            ...this.jobsState,
            active: null,
          };
          this.loadJobs();
          this.loadDashboard();
          if (this.route === C.ROUTES.LIBRARY) {
            this.loadLibrary();
          } else if (
            this.route === C.ROUTES.EDITOR &&
            this.editorFile &&
            payload.filepath === this.editorFile.filepath &&
            payload.status === C.STATUSES.COMPLETED
          ) {
            if (!Ed.isMp4File(this.editorFile)) {
              this.loadEditor(this.editorFile.id);
            }
          }

          if (payload.status === C.STATUSES.COMPLETED) {
            this.showToast(
              `Finished ${Fmt.basenameOf(payload.filepath)}.`,
              "success"
            );
          } else if (payload.status === C.STATUSES.FAILED) {
            this.showToast(
              payload.error_message || `Job failed for ${Fmt.basenameOf(payload.filepath)}.`,
              "error"
            );
          } else if (payload.status === C.STATUSES.CANCELLED) {
            this.showToast(
              `Cancelled ${Fmt.basenameOf(payload.filepath)}.`,
              "info"
            );
          }
        }
      },

      // Live CLI Log auto-scroll management
      onLogScroll(el) {
        if (!el) return;
        const distanceFromBottom =
          el.scrollHeight - el.scrollTop - el.clientHeight;
        this.logsAutoScroll =
          distanceFromBottom <= C.EDITOR.LOG_AUTOSCROLL_THRESHOLD_PX;
      },

      scrollLogsToBottom(force = false) {
        if (!this.showCliLogs) return;
        if (!force && !this.logsAutoScroll) return;
        const targets = [this.$refs.drawerLogBox, this.$refs.pageLogBox].filter(
          Boolean
        );
        targets.forEach((el) => {
          el.scrollTop = el.scrollHeight;
        });
        if (force) {
          this.logsAutoScroll = true;
        }
      },

      toggleCliLogs() {
        this.showCliLogs = !this.showCliLogs;
        if (this.showCliLogs) {
          this.logsAutoScroll = true;
          this.$nextTick(() => this.scrollLogsToBottom(true));
        }
      },

      // Data loaders
      _applyUserSettings(settings) {
        if (!settings || typeof settings !== "object") return;
        const merged = { ...C.DEFAULT_USER_SETTINGS, ...settings };
        this.userSettings = merged;
        const wasCustomOpen = Boolean(
          this.settingsProfileDraft && this.settingsProfileDraft.customOpen
        );
        const nextDraft = PP.createProfileDraftFromSettings(merged);
        if (wasCustomOpen && nextDraft.preset !== "neutral") {
          nextDraft.customOpen = true;
        }
        this.settingsProfileDraft = nextDraft;
        Theme.applyTheme(merged.theme_mode, merged.accent_color);
        Theme.applyFontScale(merged.font_scale);
        if (
          Number.isFinite(Number(merged.page_limit)) &&
          C.SETTINGS_OPTIONS.PAGE_LIMITS.includes(Number(merged.page_limit))
        ) {
          this.limit = Number(merged.page_limit);
        }
      },

      // Phase 15: Global Household Playback Profile Settings Actions
      async _saveSettingsPlaybackDraft(nextDraft) {
        this.settingsProfileDraft = nextDraft;
        const patch = PP.buildSettingsPatchFromDraft(nextDraft);
        const saved = await this.updateUserSettings(patch);
        if (saved) {
          this.loadDashboard();
          if (this.route === C.ROUTES.LIBRARY) {
            this.loadLibrary({ preserveSelection: true });
          }
        }
        return saved;
      },

      selectSettingsPlaybackPreset(presetId) {
        const nextDraft = PP.applyPresetToDraft(this.settingsProfileDraft, presetId);
        return this._saveSettingsPlaybackDraft(nextDraft);
      },

      toggleSettingsPlaybackCodec(codecKey) {
        const nextDraft = PP.toggleCodecInDraft(this.settingsProfileDraft, codecKey);
        return this._saveSettingsPlaybackDraft(nextDraft);
      },

      setSettingsPlaybackMaxChannels(ch) {
        const nextDraft = {
          ...this.settingsProfileDraft,
          preset: "custom",
          maxChannels: Number(ch),
          customOpen: true,
        };
        return this._saveSettingsPlaybackDraft(nextDraft);
      },

      setSettingsPlaybackPreferredCodec(codec) {
        const nextDraft = {
          ...this.settingsProfileDraft,
          preset: "custom",
          preferredCodec: codec,
          customOpen: true,
        };
        return this._saveSettingsPlaybackDraft(nextDraft);
      },

      resetSettingsPlaybackProfile() {
        const nextDraft = PP.applyPresetToDraft(this.settingsProfileDraft, "neutral");
        return this._saveSettingsPlaybackDraft(nextDraft);
      },

      setThemeMode(mode) {
        const normalized = Theme.normalizeThemeMode(mode);
        this.userSettings = { ...this.userSettings, theme_mode: normalized };
        Theme.applyTheme(normalized, this.userSettings.accent_color);
        return this.updateUserSettings({ theme_mode: normalized });
      },

      setAccentColor(accent) {
        const normalized = Theme.normalizeAccentColor(accent);
        this.userSettings = { ...this.userSettings, accent_color: normalized };
        Theme.applyTheme(this.userSettings.theme_mode, normalized);
        return this.updateUserSettings({ accent_color: normalized });
      },

      previewFontScale(scale) {
        const normalized = Theme.normalizeFontScale(scale);
        this.userSettings = { ...this.userSettings, font_scale: normalized };
        Theme.applyFontScale(normalized);
        return normalized;
      },

      setFontScale(scale) {
        const normalized = this.previewFontScale(scale);
        return this.updateUserSettings({ font_scale: normalized });
      },

      setUiLanguage(lang) {
        const normalized = I18n.normalizeLocale(lang);
        this.userSettings = { ...this.userSettings, language: normalized };
        return this.updateUserSettings({ language: normalized });
      },

      async loadUserSettings() {
        try {
          const settings = await Api.getSettings();
          this._applyUserSettings(settings);
        } catch (_err) {
          // Keep defaults safely if offline or unavailable
        }
      },

      async updateUserSettings(patch) {
        this.settingsSaving = true;
        try {
          const updated = await Api.updateSettings(patch);
          this._applyUserSettings(updated);
          if (patch && patch.default_library_view) {
            this.libraryView = updated.default_library_view;
            try {
              window.localStorage.setItem(
                C.STORAGE_KEYS.LIBRARY_VIEW,
                updated.default_library_view
              );
            } catch (_err) {
              // Safe fallback
            }
          }
          if (patch && patch.default_folder_layout) {
            this.folderLayout = updated.default_folder_layout;
            try {
              window.localStorage.setItem(
                C.STORAGE_KEYS.FOLDER_LAYOUT,
                updated.default_folder_layout
              );
            } catch (_err) {
              // Safe fallback
            }
          }
          if (
            patch &&
            (patch.show_media_files_only !== undefined ||
              patch.visible_media_extensions !== undefined ||
              patch.page_limit !== undefined) &&
            this.route === C.ROUTES.LIBRARY
          ) {
            if (patch.page_limit !== undefined) {
              this.page = C.PAGINATION.DEFAULT_PAGE;
              this._syncUrlHash(true);
            }
            this.loadLibrary({ preserveSelection: true });
          }
          this.showToast("Settings saved.", "success");
          return this.userSettings;
        } catch (err) {
          this.showToast(err.message || "Failed to save setting.", "error");
          return null;
        } finally {
          this.settingsSaving = false;
        }
      },

      async loadMaintenanceStatus() {
        this.maintenanceLoading = true;
        this.maintenanceError = null;
        try {
          this.maintenanceStatus = await Api.getMaintenanceStatus();
        } catch (err) {
          if (err && err.status === 401) return;
          this.maintenanceError =
            err.message || "Failed to load maintenance diagnostics.";
        } finally {
          this.maintenanceLoading = false;
        }
      },

      openFactoryResetModal() {
        if (
          this.isJobRunning ||
          this.isScanRunning ||
          (this.maintenanceStatus && this.maintenanceStatus.busy)
        ) {
          this.showToast(
            "Wait for active jobs or scans to finish before resetting MKV Forge.",
            "info"
          );
          return;
        }
        this.factoryResetError = null;
        this.factoryResetPassword = "";
        this.factoryResetConfirmText = "";
        this.factoryResetModalOpen = true;
      },

      closeFactoryResetModal() {
        if (this.factoryResetBusy) return;
        this.factoryResetModalOpen = false;
        this.factoryResetError = null;
        this.factoryResetPassword = "";
        this.factoryResetConfirmText = "";
      },

      _clearClientStorageAndStateForReset() {
        try {
          Object.values(C.STORAGE_KEYS).forEach((key) => {
            window.localStorage.removeItem(key);
            window.sessionStorage.removeItem(key);
          });
        } catch (_err) {
          // Storage may be unavailable in restricted contexts
        }
        LR.clearOrigin();
        this._libraryReturnState = null;
        this._pendingLibraryScrollRestore = null;
        this.userSettings = { ...C.DEFAULT_USER_SETTINGS };
        this.settingsProfileDraft = PP.createProfileDraftFromSettings(
          C.DEFAULT_USER_SETTINGS
        );
        this.onboardingProfileDraft = PP.createProfileDraftFromSettings(
          C.DEFAULT_USER_SETTINGS
        );
        Theme.applyTheme(
          C.DEFAULT_USER_SETTINGS.theme_mode,
          C.DEFAULT_USER_SETTINGS.accent_color
        );
        Theme.applyFontScale(C.DEFAULT_USER_SETTINGS.font_scale);
        this.libraryView = C.DEFAULT_USER_SETTINGS.default_library_view;
        this.folderLayout = C.DEFAULT_USER_SETTINGS.default_folder_layout;
        this.limit = C.DEFAULT_USER_SETTINGS.page_limit;
        this.files = [];
        this.folders = [];
        this.subfolders = [];
        this.otherFiles = [];
        this.totalFiles = 0;
        this.totalPages = 0;
        this.needsConversion = false;
        this.noDolby = false;
        this.hasMetadata = false;
        this.audioType = "";
        this.audioCodec = "";
        this.videoCodec = "";
        this.resolution = "";
        this.dynamicRange = "";
        this.audioChannels = "";
        this.audioLanguage = "";
        this.noSubtitles = false;
        this.dashStorageLens = C.DASHBOARD_LENSES.STORAGE_DEFAULT;
        this.dashAudioLens = C.DASHBOARD_LENSES.AUDIO_DEFAULT;
        this.thumbLoadErrors = {};
        this.searchQuery = "";
        this.appliedSearchQuery = "";
        this.activeJob = null;
        this.queuedJobs = [];
        this.jobHistory = [];
        this.jobLogs = [];
        this.editorFile = null;
        this.editor = null;
        this.authUsers = [];
        this.apiKeys = [];
        this.maintenanceStatus = null;
        this.dashboard = {
          app_name: C.APP.NAME,
          app_version: C.APP.VERSION,
          stats: {
            total_files: 0,
            aac51_only: 0,
            has_metadata: 0,
            has_metadata_bytes: 0,
            needs_conversion: 0,
            needs_conversion_bytes: 0,
            untagged_audio: 0,
            untagged_audio_bytes: 0,
            no_subtitles: 0,
            with_subtitles: 0,
            playback_profile_configured: false,
            playback_profile_preset: "neutral",
            playback_profile_label: "Not Configured",
            last_scan_at: null,
            last_scan_status: null,
          },
          analytics: {
            storage: {
              disk_total_bytes: 0,
              disk_used_bytes: 0,
              disk_free_bytes: 0,
              mkv_bytes: 0,
              mkv_count: 0,
              mp4_bytes: 0,
              mp4_count: 0,
              media_bytes: 0,
              other_used_bytes: 0,
            },
            storage_by_codec: [],
            storage_by_resolution: [],
            total_audio_tracks: 0,
            audio_codecs: [],
            audio_channels: [],
            audio_languages: [],
            resolutions: [],
            video_codecs: [],
            dynamic_ranges: [],
          },
          scan_running: false,
          recent_activity: [],
        };
      },

      async submitFactoryReset() {
        if (!this.canSubmitFactoryReset) return;
        this.factoryResetBusy = true;
        this.factoryResetError = null;
        try {
          const payload = this.isLocalAuthEnabled
            ? { password: this.factoryResetPassword }
            : { confirm_text: String(this.factoryResetConfirmText || "").trim() };
          await Api.factoryReset(payload);

          this._disconnectStreams();
          this.factoryResetModalOpen = false;
          this.factoryResetPassword = "";
          this.factoryResetConfirmText = "";
          this.settingsModalOpen = false;
          this.jobsDrawerOpen = false;
          this._clearClientStorageAndStateForReset();

          this.onboardingAuthChoice = C.ONBOARDING.AUTH_CHOICE_LOCAL;
          this.onboardingForm = {
            username: "",
            display_name: "",
            password: "",
            confirmPassword: "",
          };
          this.onboardingWizardStep = C.ONBOARDING.STEP_ACCESS;
          this.onboardingDirection = C.ONBOARDING.DIR_FORWARD;
          this.onboardingActive = true;
          this.authNeedsSetup = true;
          this.authMode = C.AUTH_MODES.UNCONFIGURED;
          this.authUser = null;
          this.route = C.ROUTES.DASHBOARD;
          if (window.location.hash !== "#/") {
            window.history.replaceState(null, "", "#/");
          }
          await this.bootstrapSessionAndData();
          this.showToast(
            "MKV Forge was reset. Your media files were untouched.",
            "info"
          );
        } catch (err) {
          this.factoryResetError =
            err.message || "Factory reset failed. Please try again.";
        } finally {
          this.factoryResetBusy = false;
        }
      },

      async runMaintenance(action) {
        if (!action || this.maintenanceBusyAction) return;
        this.maintenanceBusyAction = action;
        this.maintenanceError = null;
        try {
          const result = await Api.runMaintenanceAction(action);
          if (action === "run-all") {
            const totalRemoved =
              Number(result.clean_temp?.removed_count || 0) +
              Number(result.prune_missing?.removed_count || 0) +
              Number(result.prune_jobs?.removed_count || 0);
            const reclaimedStr =
              result.total_bytes_reclaimed > 0
                ? ` • reclaimed ${Fmt.formatBytes(result.total_bytes_reclaimed)}`
                : "";
            this.showToast(
              `Cleanup complete (${totalRemoved} item(s) cleaned${reclaimedStr}).`,
              "success"
            );
          } else {
            const reclaimedStr =
              result.bytes_reclaimed > 0
                ? ` (${Fmt.formatBytes(result.bytes_reclaimed)} reclaimed)`
                : "";
            this.showToast(`${result.message}${reclaimedStr}`, "success");
          }
          await Promise.all([
            this.loadMaintenanceStatus(),
            this.loadDashboard(),
            this.loadJobs(),
          ]);
        } catch (err) {
          this.showToast(
            err.message || "Maintenance operation failed.",
            err.status === C.UNITS.HTTP_CONFLICT ? "info" : "error"
          );
          this.loadMaintenanceStatus();
        } finally {
          this.maintenanceBusyAction = null;
        }
      },

      async loadDashboard() {
        this.dashboardError = null;
        try {
          this.dashboard = await Api.getDashboard();
          if (this.dashboard && this.dashboard.user_settings) {
            this._applyUserSettings(this.dashboard.user_settings);
          }
        } catch (err) {
          if (err && err.status === 401) return;
          this.dashboardError = err.message || "Failed to load dashboard";
        } finally {
          this.dashboardLoading = false;
        }
      },

      async loadLibrary({
        clearStale = false,
        preserveSelection = false,
        silent = false,
      } = {}) {
        const reqSeq = ++this._libraryRequestSeq;
        if (this._libraryAbortController) {
          this._libraryAbortController.abort();
          this._libraryAbortController = null;
        }
        const abortController =
          typeof AbortController === "function" ? new AbortController() : null;
        this._libraryAbortController = abortController;

        const queryText = String(this.searchQuery || "").trim();
        const isSearching = Boolean(queryText);
        const includeFolderScope = this.isDirView && !isSearching;

        if (!silent) {
          this.libraryLoading = true;
        }
        this.libraryError = null;
        if (!preserveSelection) {
          this.selectedFileIds = [];
          this.closeContextMenu();
        }
        if (clearStale) {
          this.files = [];
          this.subfolders = [];
          this.otherFiles = [];
          this.totalPages = 0;
        }
        try {
          // Search always runs recursively from root (/), and All Files is always root (/).
          const activeFolder = includeFolderScope
            ? FT.normalizeFolderPath(this.currentDir)
            : "";
          const data = await Api.listFiles(
            {
              q: queryText,
              folder: activeFolder,
              audio_type: this.audioType,
              audio_codec: this.audioCodec,
              video_codec: this.videoCodec,
              resolution: this.resolution,
              dynamic_range: this.dynamicRange,
              audio_channels: this.audioChannels,
              audio_language: this.audioLanguage,
              no_subtitles: this.noSubtitles,
              has_metadata: this.hasMetadata,
              no_dolby: this.noDolby,
              needs_conversion: this.needsConversion,
              sort: this.sortField,
              order: this.sortOrder,
              page: this.page,
              limit: this.limit,
            },
            abortController ? { signal: abortController.signal } : {}
          );
          if (reqSeq !== this._libraryRequestSeq) return;

          // If the folder currently open in Folders view vanished on disk, step up cleanly
          if (
            includeFolderScope &&
            data.folder_exists === false &&
            FT.normalizeFolderPath(this.currentDir) !== C.FOLDER_TREE.ROOT_PATH
          ) {
            this._pendingLibraryReload = false;
            this.showToast(
              `Folder ${this.currentDir} no longer exists on disk.`,
              "info"
            );
            this.goUpDirectory();
            return;
          }

          this.appliedSearchQuery = queryText;
          this.files = data.items || [];
          if (preserveSelection && this.selectedFileIds.length > 0) {
            const visibleIds = new Set(this.files.map((item) => item.id));
            this.selectedFileIds = this.selectedFileIds.filter((id) =>
              visibleIds.has(id)
            );
          }
          this.folders = data.folders || [];
          this.subfolders = includeFolderScope ? data.subfolders || [] : [];
          this.otherFiles = includeFolderScope ? data.other_files || [] : [];
          this.folderCounts = data.folder_counts || {};
          this.audioTypes = data.audio_types || [];
          this.facets = data.facets || {
            aac51_only: 0,
            has_metadata: 0,
            no_dolby: 0,
            audio_types: [],
          };
          this.totalFiles = data.total || 0;
          this.totalPages = data.total_pages || 0;
          if (this.totalPages > 0 && this.page > this.totalPages) {
            this.page = this.totalPages;
            this._syncUrlHash(true);
            this.loadLibrary({ preserveSelection });
            return;
          }
          this._lastLoadedLibraryHash = this._buildCurrentLibraryHash();
        } catch (err) {
          if (
            reqSeq !== this._libraryRequestSeq ||
            (err && (err.detail === "aborted" || err.status === 401))
          ) {
            return;
          }
          this.libraryError = err.message || "Failed to load library";
        } finally {
          if (this._libraryAbortController === abortController) {
            this._libraryAbortController = null;
          }
          if (reqSeq === this._libraryRequestSeq) {
            this.libraryLoading = false;
            if (this._pendingLibraryReload && this.route === C.ROUTES.LIBRARY) {
              this._pendingLibraryReload = false;
              this.loadLibrary({ preserveSelection: true });
            } else if (
              this.route === C.ROUTES.LIBRARY &&
              this._pendingLibraryScrollRestore
            ) {
              this._scheduleLibraryScrollRestore({ consume: true });
            }
          }
        }
      },

      async loadJobs() {
        this.jobsLoading = true;
        this.jobsError = null;
        try {
          this.jobsState = await Api.getJobs();
          this._pruneBusySelection();
        } catch (err) {
          if (err && err.status === 401) return;
          this.jobsError = err.message || "Failed to load jobs";
        } finally {
          this.jobsLoading = false;
        }
      },

      _pruneBusySelection() {
        if (!Array.isArray(this.selectedFileIds) || this.selectedFileIds.length === 0) {
          return;
        }
        const byId = new Map((this.files || []).map((f) => [f.id, f]));
        const next = this.selectedFileIds.filter((id) => {
          const f = byId.get(id);
          return !f || !this.isFileLocked(f);
        });
        if (next.length !== this.selectedFileIds.length) {
          this.selectedFileIds = next;
        }
      },

      async loadEditor(fileId) {
        this.editorLoading = true;
        this.editorError = null;
        this.editorConflictMessage = null;
        this.editorExternalChange = null;
        this.addAudioMenuOpen = false;
        this.addAudioModalOpen = false;
        this.addAudioDraft = null;
        this.importTrackModal = null;
        this.folderPickerOpen = false;
        this.folderTree = null;
        try {
          const detail = await Api.getFileDetail(fileId);
          this.editorFile = detail;
          this.editor = Ed.createEditorState(detail, this.userSettings);
          const activeJob = this.getFileJobStatus(detail);
          if (activeJob && activeJob.state === C.STATUSES.QUEUED) {
            this.showToast(this._busyLockMessage(detail), "info");
          }
        } catch (err) {
          if (err && err.status === 401) return;
          this.editorFile = null;
          this.editor = null;
          this.editorError = err.message || "Failed to load media file details";
        } finally {
          this.editorLoading = false;
        }
      },

      // Editor actions (+ Add Audio dropdown -> Progressive-Disclosure modal -> Inline track)
      _resolveDefaultBitrateForDraft(targetCodec, resolvedChannels) {
        const cfg = Ed.getBitrateConfigFor(targetCodec, resolvedChannels);
        if (!cfg.options || cfg.options.length === 0) return null;
        const isDolby51 =
          (targetCodec === C.EDITOR.CODEC_EAC3 ||
            targetCodec === C.EDITOR.CODEC_AC3) &&
          resolvedChannels === C.EDITOR.MAX_DOLBY_CHANNELS;
        if (
          isDolby51 &&
          cfg.options.includes(this.userSettings.default_dolby_bitrate)
        ) {
          return this.userSettings.default_dolby_bitrate;
        }
        return cfg.defaultBitrate;
      },

      openAddAudioChoice(option, { expandAdvanced = false } = {}) {
        if (!this.editor || !option || this.isEditorFileBusy) return;
        this.addAudioMenuOpen = false;
        const tracks =
          Array.isArray(option.eligibleTracks) && option.eligibleTracks.length > 0
            ? option.eligibleTracks
            : this.editorAudioTracks;
        const preferred =
          tracks.find((t) => t.id === option.preferredSourceTrackId) || tracks[0];
        const sourceLang = preferred
          ? preferred.language || C.EDITOR.DEFAULT_LANGUAGE
          : C.EDITOR.DEFAULT_LANGUAGE;
        const sourceChannels = preferred
          ? Number(preferred.audio_channels || C.EDITOR.MAX_DOLBY_CHANNELS)
          : C.EDITOR.MAX_DOLBY_CHANNELS;
        const requestedChannels =
          option.targetChannels !== undefined ? option.targetChannels : null;
        const resolvedChannels = Ed.resolveTargetChannels(
          sourceChannels,
          option.codec,
          requestedChannels
        );
        const preferredBitrate = this._resolveDefaultBitrateForDraft(
          option.codec,
          resolvedChannels
        );

        this.addAudioDraft = {
          targetCodec: option.codec,
          targetChannels: requestedChannels,
          sourceTrackId: preferred ? preferred.id : null,
          bitrate: preferredBitrate,
          language: sourceLang,
          trackTitle: Ed.buildDefaultAudioTitle(
            sourceLang,
            option.codec,
            resolvedChannels,
            preferredBitrate
          ),
          titleManuallyEdited: false,
          setAsDefault: true,
          dropSourceTrack: false,
          advancedOpen: Boolean(expandAdvanced),
        };
        this.addAudioModalOpen = true;
      },

      openCustomAddAudio() {
        if (!this.editor || this.isEditorFileBusy) return;
        const recommended = this.editorRecommendedAudioOptions;
        if (recommended.length > 0) {
          this.openAddAudioChoice(recommended[0], { expandAdvanced: true });
          return;
        }
        const firstAudio = this.editorAudioTracks[0];
        if (!firstAudio) return;
        this.openAddAudioChoice(
          {
            codec: C.EDITOR.CODEC_EAC3,
            targetChannels: null,
            preferredSourceTrackId: firstAudio.id,
            eligibleTracks: this.editorAudioTracks,
          },
          { expandAdvanced: true }
        );
      },

      editAddedAudio() {
        if (!this.editor || !this.editor.audioFix.enabled || this.isEditorFileBusy) return;
        this.addAudioMenuOpen = false;
        this.addAudioDraft = {
          targetChannels: null,
          ...this.editor.audioFix,
          advancedOpen: false,
        };
        this.addAudioModalOpen = true;
      },

      closeAddAudioModal() {
        this.addAudioModalOpen = false;
        this.addAudioDraft = null;
      },

      _refreshDraftAudioTitle() {
        if (!this.addAudioDraft || this.addAudioDraft.titleManuallyEdited) return;
        const resolvedCh = this.editorDraftResolvedChannels;
        this.addAudioDraft.trackTitle = Ed.buildDefaultAudioTitle(
          this.addAudioDraft.language,
          this.addAudioDraft.targetCodec,
          resolvedCh,
          this.addAudioDraft.bitrate
        );
      },

      onDraftBaseAudioChange() {
        if (!this.addAudioDraft) return;
        const src = this.editorDraftSourceTrack;
        if (src) {
          this.addAudioDraft.language =
            src.language || C.EDITOR.DEFAULT_LANGUAGE;
          const meta = Ed.getCodecMeta(this.addAudioDraft.targetCodec);
          if (meta.requiresLosslessSource && !Ed.isLosslessAudioTrack(src)) {
            const fallback = Ed.getAvailableTargetCodecs(src).find(
              (c) =>
                !Ed.isConversionCombinationBlocked(
                  src,
                  c.codec,
                  this.addAudioDraft.targetChannels
                ).blocked
            );
            this.addAudioDraft.targetCodec = fallback
              ? fallback.codec
              : C.EDITOR.CODEC_EAC3;
          }
          const srcCh = Number(src.audio_channels || C.EDITOR.MAX_DOLBY_CHANNELS);
          const maxCh = Math.min(
            srcCh,
            Ed.getCodecMeta(this.addAudioDraft.targetCodec).maxChannels ||
              C.EDITOR.MAX_DOLBY_CHANNELS
          );
          if (
            this.addAudioDraft.targetChannels !== null &&
            this.addAudioDraft.targetChannels >= maxCh
          ) {
            this.addAudioDraft.targetChannels = null;
          }
          const resolvedCh = this.editorDraftResolvedChannels;
          const rateCfg = Ed.getBitrateConfigFor(
            this.addAudioDraft.targetCodec,
            resolvedCh
          );
          if (rateCfg.options.length === 0) {
            this.addAudioDraft.bitrate = null;
          } else if (!rateCfg.options.includes(this.addAudioDraft.bitrate)) {
            this.addAudioDraft.bitrate = this._resolveDefaultBitrateForDraft(
              this.addAudioDraft.targetCodec,
              resolvedCh
            );
          }
        }
        this._refreshDraftAudioTitle();
      },

      onDraftTargetCodecChange(nextCodec) {
        if (!this.addAudioDraft || !nextCodec) return;
        this.addAudioDraft.targetCodec = nextCodec;
        const src = this.editorDraftSourceTrack;
        const srcCh = Number((src && src.audio_channels) || C.EDITOR.MAX_DOLBY_CHANNELS);
        const maxCh = Math.min(
          srcCh,
          Ed.getCodecMeta(nextCodec).maxChannels || C.EDITOR.MAX_DOLBY_CHANNELS
        );
        if (
          this.addAudioDraft.targetChannels !== null &&
          this.addAudioDraft.targetChannels >= maxCh
        ) {
          this.addAudioDraft.targetChannels = null;
        }
        const resolvedCh = this.editorDraftResolvedChannels;
        this.addAudioDraft.bitrate = this._resolveDefaultBitrateForDraft(
          nextCodec,
          resolvedCh
        );
        this._refreshDraftAudioTitle();
      },

      onDraftChannelsChange(nextChannelValue) {
        if (!this.addAudioDraft) return;
        this.addAudioDraft.targetChannels =
          nextChannelValue === null || nextChannelValue === undefined
            ? null
            : Number(nextChannelValue);
        const resolvedCh = this.editorDraftResolvedChannels;
        this.addAudioDraft.bitrate = this._resolveDefaultBitrateForDraft(
          this.addAudioDraft.targetCodec,
          resolvedCh
        );
        this._refreshDraftAudioTitle();
      },

      onDraftLanguageChange() {
        this._refreshDraftAudioTitle();
      },

      onDraftBitrateChange(rate) {
        if (!this.addAudioDraft) return;
        this.addAudioDraft.bitrate = rate;
        this._refreshDraftAudioTitle();
      },

      clearDraftAudioTitle() {
        if (!this.addAudioDraft) return;
        this.addAudioDraft.trackTitle = "";
        this.addAudioDraft.titleManuallyEdited = true;
      },

      resetDraftAudioTitle() {
        if (!this.addAudioDraft) return;
        this.addAudioDraft.titleManuallyEdited = false;
        this._refreshDraftAudioTitle();
      },

      defaultAddedAudioTitle() {
        if (!this.editor || !this.editor.audioFix || !this.editor.audioFix.enabled) {
          return "";
        }
        const src = Ed.findSelectedSourceAudio(this.editor);
        const resolvedCh = Ed.resolveTargetChannels(
          src ? src.audio_channels : C.EDITOR.MAX_DOLBY_CHANNELS,
          this.editor.audioFix.targetCodec,
          this.editor.audioFix.targetChannels
        );
        return Ed.buildDefaultAudioTitle(
          this.editor.audioFix.language,
          this.editor.audioFix.targetCodec,
          resolvedCh,
          this.editor.audioFix.bitrate
        );
      },

      sanitizeAddedAudioMetadata() {
        if (!this.editor || !this.editor.audioFix || !this.editor.audioFix.enabled) return;
        this.editor.audioFix.trackTitle = this.defaultAddedAudioTitle();
        this.editor.audioFix.titleManuallyEdited = false;
      },

      onDraftTitleInput() {
        if (!this.addAudioDraft) return;
        this.addAudioDraft.titleManuallyEdited = true;
      },

      confirmAddAudio() {
        if (!this.editor || !this.addAudioDraft) return;
        if (this.addAudioDraft.sourceTrackId === null) {
          this.showToast("Please choose a base audio track.", "error");
          return;
        }
        const status = this.editorDraftBlockStatus;
        if (status && status.blocked) {
          this.showToast(status.reason || "Invalid conversion combination.", "error");
          return;
        }
        const { advancedOpen: _ignoreUiFlag, ...persistedDraft } = this.addAudioDraft;
        this.editor.audioFix = {
          ...persistedDraft,
          enabled: true,
        };
        this.closeAddAudioModal();
      },

      removeAddedAudio() {
        if (!this.editor) return;
        this.editor.audioFix.enabled = false;
        this.editor.audioFix.sourceTrackId = null;
        this.editor.audioFix.dropSourceTrack = false;
      },

      // Phase 10: Import External Audio & Subtitles (From Storage or Device Upload)
      async openImportTrackModal(kind, existingExt = null) {
        if (!this.editor || this.isEditorFileBusy) return;
        this.addAudioMenuOpen = false;
        const normalizedKind =
          kind === C.EXTERNAL_TRACKS.KIND_AUDIO
            ? C.EXTERNAL_TRACKS.KIND_AUDIO
            : C.EXTERNAL_TRACKS.KIND_SUBTITLES;
        const isEditing = Boolean(existingExt && existingExt.id);

        if (
          !isEditing &&
          (this.editor.externalTracks || []).length >= C.EXTERNAL_TRACKS.MAX_PER_JOB
        ) {
          this.showToast(
            `Maximum ${C.EXTERNAL_TRACKS.MAX_PER_JOB} imported tracks per job.`,
            "error"
          );
          return;
        }

        const initialDir =
          (existingExt &&
            existingExt.source === C.EXTERNAL_TRACKS.SOURCE_STORAGE &&
            existingExt.path &&
            FT.normalizeFolderPath(
              existingExt.path.slice(0, existingExt.path.lastIndexOf("/")) || "/"
            )) ||
          (this.editorFile && this.editorFile.folder
            ? FT.normalizeFolderPath(this.editorFile.folder)
            : C.FOLDER_TREE.ROOT_PATH);

        this.importTrackModal = {
          isEditing,
          id: existingExt ? existingExt.id : null,
          kind: normalizedKind,
          source: existingExt
            ? existingExt.source
            : C.EXTERNAL_TRACKS.SOURCE_STORAGE,
          path: existingExt ? existingExt.path : null,
          uploadId: existingExt ? existingExt.uploadId : null,
          originalUploadId: existingExt ? existingExt.uploadId : null,
          originalFilename: existingExt ? existingExt.originalFilename : "",
          sizeBytes: existingExt ? Number(existingExt.sizeBytes || 0) : 0,
          trackName: existingExt ? existingExt.trackName || "" : "",
          titleManuallyEdited: Boolean(existingExt && existingExt.trackName),
          language: existingExt
            ? existingExt.language || C.EDITOR.DEFAULT_LANGUAGE
            : this.userSettings.preferred_language || "eng",
          defaultTrack: existingExt ? Boolean(existingExt.defaultTrack) : false,
          forcedTrack: existingExt ? Boolean(existingExt.forcedTrack) : false,
          syncDelayMs: existingExt ? Number(existingExt.syncDelayMs || 0) : 0,
          storageDir: initialDir,
          storageSubfolders: [],
          storageFiles: [],
          storageLoading: false,
          uploading: false,
          uploadProgress: 0,
          error: null,
        };

        await this.browseImportStorageDir(initialDir);
      },

      async browseImportStorageDir(folderPath) {
        if (!this.importTrackModal) return;
        const normalized = FT.normalizeFolderPath(folderPath);
        this.importTrackModal.storageDir = normalized;
        this.importTrackModal.storageLoading = true;
        this.importTrackModal.error = null;
        try {
          const data = await Api.listFolders(normalized, {
            includeFiles: true,
            kind: this.importTrackModal.kind,
          });
          if (!this.importTrackModal) return;
          this.importTrackModal.storageDir = data.path || normalized;
          this.importTrackModal.storageSubfolders = data.entries || [];
          this.importTrackModal.storageFiles = data.files || [];
        } catch (err) {
          if (this.importTrackModal) {
            this.importTrackModal.error =
              err.message || "Failed to browse storage folder.";
          }
        } finally {
          if (this.importTrackModal) {
            this.importTrackModal.storageLoading = false;
          }
        }
      },

      goUpImportStorageDir() {
        if (!this.importTrackModal) return;
        const cur = FT.normalizeFolderPath(this.importTrackModal.storageDir);
        if (cur === C.FOLDER_TREE.ROOT_PATH) return;
        const idx = cur.lastIndexOf("/");
        const parent = idx <= 0 ? C.FOLDER_TREE.ROOT_PATH : cur.slice(0, idx);
        this.browseImportStorageDir(parent);
      },

      selectImportStorageFile(fileEntry) {
        if (!this.importTrackModal || !fileEntry) return;
        this.importTrackModal.source = C.EXTERNAL_TRACKS.SOURCE_STORAGE;
        this.importTrackModal.path = fileEntry.path;
        this.importTrackModal.originalFilename = fileEntry.name;
        this.importTrackModal.sizeBytes = Number(fileEntry.size_bytes || 0);
        this.importTrackModal.error = null;
        if (!this.importTrackModal.titleManuallyEdited) {
          this.importTrackModal.trackName = Ed.buildDefaultExternalTitle(
            this.importTrackModal.language,
            this.importTrackModal.kind,
            fileEntry.name
          );
        }
      },

      async handleImportDeviceFile(file) {
        if (!this.importTrackModal || !file || this.importTrackModal.uploading) return;
        const modal = this.importTrackModal;
        const prevTempUploadId =
          modal.uploadId && modal.uploadId !== modal.originalUploadId
            ? modal.uploadId
            : null;

        modal.uploading = true;
        modal.uploadProgress = 0;
        modal.error = null;
        try {
          const staged = await Api.uploadExternalTrack(
            modal.kind,
            file,
            (pct) => {
              if (this.importTrackModal) {
                this.importTrackModal.uploadProgress = pct;
              }
            }
          );
          if (!this.importTrackModal) {
            // Modal closed mid-upload: clean up orphaned staged file
            Api.deleteUploadedTrack(staged.upload_id).catch(() => {});
            return;
          }
          if (prevTempUploadId) {
            Api.deleteUploadedTrack(prevTempUploadId).catch(() => {});
          }
          this.importTrackModal.source = C.EXTERNAL_TRACKS.SOURCE_UPLOAD;
          this.importTrackModal.uploadId = staged.upload_id;
          this.importTrackModal.originalFilename = staged.original_filename;
          this.importTrackModal.sizeBytes = Number(staged.size_bytes || 0);
          this.importTrackModal.uploadProgress = 100;
          if (!this.importTrackModal.titleManuallyEdited) {
            this.importTrackModal.trackName = Ed.buildDefaultExternalTitle(
              this.importTrackModal.language,
              this.importTrackModal.kind,
              staged.original_filename
            );
          }
        } catch (err) {
          if (this.importTrackModal) {
            this.importTrackModal.error = err.message || "Upload failed.";
          }
        } finally {
          if (this.importTrackModal) {
            this.importTrackModal.uploading = false;
          }
        }
      },

      onImportLanguageChange() {
        if (!this.importTrackModal || this.importTrackModal.titleManuallyEdited) return;
        this.importTrackModal.trackName = Ed.buildDefaultExternalTitle(
          this.importTrackModal.language,
          this.importTrackModal.kind,
          this.importTrackModal.originalFilename
        );
      },

      closeImportTrackModal() {
        const modal = this.importTrackModal;
        this.importTrackModal = null;
        if (
          modal &&
          modal.uploadId &&
          modal.uploadId !== modal.originalUploadId
        ) {
          Api.deleteUploadedTrack(modal.uploadId).catch(() => {});
        }
      },

      confirmImportTrackModal() {
        const modal = this.importTrackModal;
        if (!this.editor || !modal || modal.uploading) return;

        if (modal.source === C.EXTERNAL_TRACKS.SOURCE_STORAGE && !modal.path) {
          modal.error = "Select a file from Storage or switch to Upload from Device.";
          return;
        }
        if (modal.source === C.EXTERNAL_TRACKS.SOURCE_UPLOAD && !modal.uploadId) {
          modal.error = "Upload a file from your device or switch to From Storage.";
          return;
        }

        // If replacing a previous staged upload on edit, clean up the old upload
        if (
          modal.originalUploadId &&
          (modal.source !== C.EXTERNAL_TRACKS.SOURCE_UPLOAD ||
            modal.uploadId !== modal.originalUploadId)
        ) {
          Api.deleteUploadedTrack(modal.originalUploadId).catch(() => {});
        }

        Ed.upsertExternalTrack(this.editor, {
          id: modal.id,
          kind: modal.kind,
          source: modal.source,
          path:
            modal.source === C.EXTERNAL_TRACKS.SOURCE_STORAGE ? modal.path : null,
          uploadId:
            modal.source === C.EXTERNAL_TRACKS.SOURCE_UPLOAD
              ? modal.uploadId
              : null,
          originalFilename: modal.originalFilename,
          sizeBytes: modal.sizeBytes,
          trackName: modal.trackName,
          language: modal.language,
          defaultTrack: modal.defaultTrack,
          forcedTrack: modal.forcedTrack,
          syncDelayMs: modal.syncDelayMs,
        });
        this.importTrackModal = null;
      },

      removeImportedTrack(extId) {
        if (!this.editor || !extId) return;
        const removed = Ed.removeExternalTrack(this.editor, extId);
        if (
          removed &&
          removed.source === C.EXTERNAL_TRACKS.SOURCE_UPLOAD &&
          removed.uploadId
        ) {
          Api.deleteUploadedTrack(removed.uploadId).catch(() => {});
        }
      },

      // Unified Folder Tree Picker modal actions (Save As / Move Files / Move Folder / Browse)
      async _openFolderPickerWithConfig({
        purpose,
        initialPath,
        title,
        confirmLabel,
        context = null,
      }) {
        this.folderPickerPurpose = purpose;
        this.folderPickerTitle = title;
        this.folderPickerConfirmLabel = confirmLabel;
        this.folderPickerContext = context;
        this.folderPickerSubmitting = false;
        this.folderTree = FT.createFolderTreeState(
          initialPath || C.FOLDER_TREE.ROOT_PATH
        );
        this.folderPickerOpen = true;

        const ancestors = FT.getAncestorPaths(this.folderTree.selectedPath);
        const toLoad = ancestors.length > 1 ? ancestors.slice(0, -1) : ancestors;
        for (const folderPath of toLoad) {
          await this._loadFolderChildren(folderPath);
        }
      },

      async openFolderPicker() {
        if (!this.editor || !this.editor.saveAs) return;
        const initialPath =
          this.editor.saveAs.folder ||
          (this.editorFile ? this.editorFile.folder : C.FOLDER_TREE.ROOT_PATH);
        await this._openFolderPickerWithConfig({
          purpose: "save_as",
          initialPath,
          title: "Choose Destination Folder",
          confirmLabel: "Select Folder",
        });
      },

      async openMoveFilesModal(fileIds) {
        const ids = Array.isArray(fileIds) ? fileIds.filter(Boolean) : [];
        if (ids.length === 0) return;
        const countLabel = ids.length === 1 ? "1 File" : `${ids.length} Files`;
        await this._openFolderPickerWithConfig({
          purpose: "move_files",
          initialPath: this.isDirView ? this.currentDir : C.FOLDER_TREE.ROOT_PATH,
          title: `Move ${countLabel} To…`,
          confirmLabel: `Move ${countLabel}`,
          context: { fileIds: ids },
        });
      },

      async openMoveOtherFileModal(otherFile) {
        if (!otherFile || !otherFile.filepath) return;
        await this._openFolderPickerWithConfig({
          purpose: "move_other_file",
          initialPath: this.isDirView ? this.currentDir : C.FOLDER_TREE.ROOT_PATH,
          title: `Move "${otherFile.name}" To…`,
          confirmLabel: "Move File Here",
          context: { filepath: otherFile.filepath, name: otherFile.name },
        });
      },

      async openMoveFolderModal(folderPath) {
        const normalized = FT.normalizeFolderPath(folderPath);
        if (normalized === C.FOLDER_TREE.ROOT_PATH) return;
        const parentIdx = normalized.lastIndexOf("/");
        const parentPath = parentIdx <= 0 ? C.FOLDER_TREE.ROOT_PATH : normalized.slice(0, parentIdx);
        await this._openFolderPickerWithConfig({
          purpose: "move_folder",
          initialPath: parentPath,
          title: `Move Folder "${Fmt.basenameOf(normalized)}" To…`,
          confirmLabel: "Move Folder Here",
          context: { path: normalized },
        });
      },

      closeFolderPicker() {
        this.folderPickerOpen = false;
        this.folderPickerSubmitting = false;
      },

      // Phase 16: Speaker & Audio System Test Generator modal lifecycle
      get testMediaEval() {
        return TM.evaluateTestMediaConfig(
          this.testMediaDraft,
          this.activePlaybackProfile
        );
      },

      get testMediaAllowedContainers() {
        if (!this.testMediaDraft) return [C.TEST_MEDIA.VIDEO_CONTAINER];
        return TM.getAllowedContainers(
          this.testMediaDraft.outputMode,
          this.testMediaDraft.codec
        );
      },

      get testMediaBitrateConfig() {
        if (!this.testMediaDraft) return { options: [], defaultBitrate: null };
        return TM.getBitrateConfig(
          this.testMediaDraft.codec,
          this.testMediaDraft.layout
        );
      },

      get testMediaProfilePresetSpec() {
        return TM.resolveProfileRecommendedSpec(this.activePlaybackProfile);
      },

      get testMediaActivePresetId() {
        if (!this.testMediaDraft) return null;
        return TM.detectMatchingPresetId(
          this.testMediaDraft,
          this.activePlaybackProfile,
          this.testMediaDraft.activePresetId || null
        );
      },

      openTestMediaModal({
        destinationDir = null,
        fromProfile = false,
      } = {}) {
        this.scanMenuOpen = false;
        this.folderBarMenuOpen = false;
        const resolvedDir =
          destinationDir ||
          (this.route === C.ROUTES.LIBRARY && this.isDirView
            ? this.currentDir
            : C.TEST_MEDIA.DEFAULT_DESTINATION_DIR);
        this.testMediaDraft = TM.createTestMediaDraft({
          playbackProfile: this.activePlaybackProfile,
          destinationDir: resolvedDir,
          fromProfile: Boolean(
            fromProfile ||
              (this.activePlaybackProfile &&
                this.activePlaybackProfile.preset !== "neutral")
          ),
        });
        this.testMediaBusy = false;
        this.testMediaError = null;
        this.testMediaResult = null;
        this.testMediaModalOpen = true;
      },

      closeTestMediaModal() {
        if (this.testMediaBusy) return;
        this.testMediaModalOpen = false;
        this.testMediaError = null;
      },

      async openTestMediaFolderPicker() {
        if (!this.testMediaDraft) return;
        await this._openFolderPickerWithConfig({
          purpose: "test_media",
          initialPath:
            this.testMediaDraft.destinationDir ||
            C.TEST_MEDIA.DEFAULT_DESTINATION_DIR,
          title: "Choose Destination Folder",
          confirmLabel: "Select Folder",
        });
      },

      _syncTestMediaDraftDefaults() {
        if (!this.testMediaDraft) return;
        const allowed = TM.getAllowedContainers(
          this.testMediaDraft.outputMode,
          this.testMediaDraft.codec
        );
        if (!allowed.includes(this.testMediaDraft.container)) {
          this.testMediaDraft.container = allowed[0];
        }
        const brCfg = TM.getBitrateConfig(
          this.testMediaDraft.codec,
          this.testMediaDraft.layout
        );
        if (
          brCfg.defaultBitrate === null ||
          !brCfg.options.includes(this.testMediaDraft.bitrate)
        ) {
          this.testMediaDraft.bitrate = brCfg.defaultBitrate;
        }
        if (!this.testMediaDraft.filenameEdited) {
          this.testMediaDraft.filename = TM.buildDefaultTestFilename(
            this.testMediaDraft
          );
        }
        this.testMediaDraft.activePresetId = TM.detectMatchingPresetId(
          this.testMediaDraft,
          this.activePlaybackProfile,
          null
        );
        this.testMediaError = null;
      },

      onTestMediaOutputModeChange(mode) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft.outputMode = mode;
        this._syncTestMediaDraftDefaults();
      },

      onTestMediaCodecChange(codec) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft.codec = codec;
        this._syncTestMediaDraftDefaults();
      },

      onTestMediaLayoutChange(layout) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft.layout = layout;
        this._syncTestMediaDraftDefaults();
      },

      onTestMediaSignalChange(signalType) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft.signalType = signalType;
        this._syncTestMediaDraftDefaults();
      },

      onTestMediaContainerChange(container) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft.container = container;
        if (!this.testMediaDraft.filenameEdited) {
          this.testMediaDraft.filename = TM.buildDefaultTestFilename(
            this.testMediaDraft
          );
        }
        this.testMediaDraft.activePresetId = TM.detectMatchingPresetId(
          this.testMediaDraft,
          this.activePlaybackProfile,
          null
        );
        this.testMediaError = null;
      },

      onTestMediaPresetClick(presetId) {
        if (!this.testMediaDraft) return;
        this.testMediaDraft = TM.applyTestMediaPreset(
          this.testMediaDraft,
          presetId,
          this.activePlaybackProfile
        );
        this.testMediaError = null;
      },

      async submitTestMediaGenerate() {
        if (!this.testMediaDraft || this.testMediaBusy) return;
        const evalRes = this.testMediaEval;
        if (!evalRes.valid) {
          this.testMediaError = evalRes.blockReason || "Invalid configuration.";
          return;
        }
        this.testMediaBusy = true;
        this.testMediaError = null;
        try {
          const payload = TM.buildTestMediaPayload(this.testMediaDraft);
          const result = await Api.generateTestMedia(payload);
          this.testMediaResult = result;
          this.showToast(`Generated ${result.filename}`, "success");
          this.loadDashboard();
          if (this.route === C.ROUTES.LIBRARY) {
            this.loadLibrary({ preserveSelection: true, silent: true });
          }
        } catch (err) {
          this.testMediaError =
            err.message || "Failed to generate speaker test file.";
        } finally {
          this.testMediaBusy = false;
        }
      },

      viewGeneratedTestInLibrary() {
        const res = this.testMediaResult;
        const targetFolder =
          (res && res.folder) ||
          (this.testMediaDraft && this.testMediaDraft.destinationDir) ||
          C.FOLDER_TREE.ROOT_PATH;
        this.testMediaModalOpen = false;
        this.settingsModalOpen = false;
        this.openFolderInLibrary(targetFolder);
      },

      async _loadFolderChildren(folderPath) {
        if (!this.folderTree) return;
        const normalized = FT.normalizeFolderPath(folderPath);
        this.folderTree.loading = { ...this.folderTree.loading, [normalized]: true };
        this.folderTree.error = null;
        try {
          const data = await Api.listFolders(normalized);
          if (this.folderTree) {
            FT.mergeFolderChildren(this.folderTree, data.path, data.entries || []);
          }
        } catch (err) {
          if (this.folderTree) {
            this.folderTree.error = err.message || "Failed to load folders";
          }
        } finally {
          if (this.folderTree) {
            const nextLoading = { ...this.folderTree.loading };
            delete nextLoading[normalized];
            this.folderTree.loading = nextLoading;
          }
        }
      },

      async toggleFolderNode(row) {
        if (!this.folderTree || !row) return;
        const path = row.path;
        if (this.folderTree.expanded[path]) {
          const nextExpanded = { ...this.folderTree.expanded };
          delete nextExpanded[path];
          this.folderTree.expanded = nextExpanded;
          return;
        }
        this.folderTree.expanded = { ...this.folderTree.expanded, [path]: true };
        if (!this.folderTree.loadedPaths[path]) {
          await this._loadFolderChildren(path);
        }
      },

      selectFolderNode(path) {
        if (!this.folderTree) return;
        this.folderTree.selectedPath = FT.normalizeFolderPath(path);
        this.folderTree.error = null;
      },

      async confirmFolderPicker() {
        if (!this.folderTree || this.folderPickerSubmitting) return;
        const chosenPath = this.folderTree.selectedPath;

        if (this.folderPickerPurpose === "save_as") {
          if (this.editor && this.editor.saveAs) {
            this.editor.saveAs.folder = chosenPath;
          }
          this.closeFolderPicker();
          return;
        }

        if (this.folderPickerPurpose === "test_media") {
          if (this.testMediaDraft) {
            this.testMediaDraft.destinationDir = chosenPath;
            this.testMediaError = null;
          }
          this.closeFolderPicker();
          return;
        }

        if (this.folderPickerPurpose === "move_files") {
          const fileIds =
            (this.folderPickerContext && this.folderPickerContext.fileIds) || [];
          if (fileIds.length === 0) return;
          this.folderPickerSubmitting = true;
          this.folderTree.error = null;
          try {
            const res = await Api.moveFiles(fileIds, chosenPath);
            this.closeFolderPicker();
            this.clearSelection();
            await this.loadLibrary();
            this.loadDashboard();
            if (res.failed > 0) {
              const firstErr =
                (res.results.find((r) => !r.ok) || {}).error || "some files failed";
              this.showToast(
                `Moved ${res.succeeded}, skipped ${res.failed}: ${firstErr}`,
                "info"
              );
            } else {
              this.showToast(
                `Moved ${res.succeeded} ${res.succeeded === 1 ? "file" : "files"} to ${chosenPath}.`,
                "success"
              );
            }
          } catch (err) {
            this.folderTree.error = err.message || "Failed to move files.";
          } finally {
            this.folderPickerSubmitting = false;
          }
          return;
        }

        if (this.folderPickerPurpose === "move_other_file") {
          const srcFilepath =
            this.folderPickerContext && this.folderPickerContext.filepath;
          const srcName =
            (this.folderPickerContext && this.folderPickerContext.name) ||
            Fmt.basenameOf(srcFilepath || "");
          if (!srcFilepath) return;
          this.folderPickerSubmitting = true;
          this.folderTree.error = null;
          try {
            await Api.moveOtherFile(srcFilepath, chosenPath);
            this.closeFolderPicker();
            await this.loadLibrary();
            this.showToast(`Moved ${srcName} to ${chosenPath}.`, "success");
          } catch (err) {
            this.folderTree.error = err.message || "Failed to move file.";
          } finally {
            this.folderPickerSubmitting = false;
          }
          return;
        }

        if (this.folderPickerPurpose === "move_folder") {
          const srcPath =
            this.folderPickerContext && this.folderPickerContext.path;
          if (!srcPath) return;
          this.folderPickerSubmitting = true;
          this.folderTree.error = null;
          try {
            const res = await Api.moveFolder(srcPath, chosenPath);
            this.closeFolderPicker();
            if (
              this.currentDir === srcPath ||
              this.currentDir.startsWith(`${srcPath}/`)
            ) {
              this.currentDir = res.new_path || chosenPath;
              this._syncUrlHash(true);
            }
            await this.loadLibrary();
            this.loadDashboard();
            this.showToast(
              `Moved folder to ${res.new_path} (${res.affected_files} indexed files updated).`,
              "success"
            );
          } catch (err) {
            this.folderTree.error = err.message || "Failed to move folder.";
          } finally {
            this.folderPickerSubmitting = false;
          }
        }
      },

      async submitCreateSubfolder() {
        if (!this.folderTree || this.folderTree.creatingFolder) return;
        const name = String(this.folderTree.newFolderName || "").trim();
        if (!name) {
          this.folderTree.error = "Enter a folder name.";
          return;
        }
        const parentPath = this.folderTree.selectedPath || C.FOLDER_TREE.ROOT_PATH;
        this.folderTree.creatingFolder = true;
        this.folderTree.error = null;
        try {
          const created = await Api.createFolder(parentPath, name);
          this.folderTree.expanded = { ...this.folderTree.expanded, [parentPath]: true };
          await this._loadFolderChildren(parentPath);
          this.folderTree.selectedPath = created.path;
          this.folderTree.newFolderName = "";
          this.folderTree.newFolderOpen = false;
          if (this.route === C.ROUTES.LIBRARY && this.showSubfolders && this.currentDir === parentPath) {
            this.loadLibrary();
          }
          this.showToast(`Created folder ${created.path}.`, "success");
        } catch (err) {
          this.folderTree.error = err.message || "Failed to create folder.";
        } finally {
          if (this.folderTree) {
            this.folderTree.creatingFolder = false;
          }
        }
      },

      // =================================================================
      // Phase 9: Library Explorer Views, Context Menu & File/Folder Ops
      // =================================================================
      setLibraryView(viewMode) {
        if (
          viewMode !== C.LIBRARY_VIEWS.DIR &&
          viewMode !== C.LIBRARY_VIEWS.LIST
        ) {
          return;
        }
        this.scanMenuOpen = false;
        const viewChanged = this.libraryView !== viewMode;
        this.libraryView = viewMode;
        try {
          window.localStorage.setItem(C.STORAGE_KEYS.LIBRARY_VIEW, viewMode);
        } catch (_err) {
          // Safe fallback
        }
        if (this.route !== C.ROUTES.LIBRARY) {
          this._clearLibraryReturnOrigin();
          this.page = C.PAGINATION.DEFAULT_PAGE;
          this.clearSelection();
          this.navigate(C.ROUTES.LIBRARY);
          return;
        }
        if (!viewChanged) return;
        this._clearLibraryReturnOrigin();
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this.clearSelection();
        this.closeContextMenu();
        this._syncUrlHash(true);
        this.loadLibrary({ clearStale: true });
      },

      toggleMobileSelectFromMenu() {
        this.scanMenuOpen = false;
        if (this.route !== C.ROUTES.LIBRARY) {
          this.mobileSelectMode = true;
          this.navigate(C.ROUTES.LIBRARY);
          return;
        }
        this.toggleMobileSelectMode();
      },

      setFolderLayout(layout) {
        this.folderBarMenuOpen = false;
        if (
          layout !== C.FOLDER_LAYOUTS.GRID &&
          layout !== C.FOLDER_LAYOUTS.LIST
        ) {
          return;
        }
        if (this.folderLayout === layout) return;
        this.folderLayout = layout;
        this.closeContextMenu();
        try {
          window.localStorage.setItem(C.STORAGE_KEYS.FOLDER_LAYOUT, layout);
        } catch (_err) {
          // Safe fallback
        }
      },

      openDirectory(folderPath) {
        this.folderBarMenuOpen = false;
        this._clearLibraryReturnOrigin();
        const normalized = FT.normalizeFolderPath(folderPath);
        this.currentDir = normalized;
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this.clearSelection();
        this.closeContextMenu();
        this._syncUrlHash(true);
        this.loadLibrary({ clearStale: true });
      },

      goUpDirectory() {
        const normalized = FT.normalizeFolderPath(this.currentDir);
        if (normalized === C.FOLDER_TREE.ROOT_PATH) return;
        const idx = normalized.lastIndexOf("/");
        const parent = idx <= 0 ? C.FOLDER_TREE.ROOT_PATH : normalized.slice(0, idx);
        this.openDirectory(parent);
      },

      // Unified Context Menu (File & Folder actions)
      openOtherFileContextMenu(otherFile, event) {
        if (!otherFile) return;
        const pos = computeContextMenuPosition(
          event,
          C.CONTEXT_MENU.FOLDER_MENU_HEIGHT_PX
        );
        this.contextMenu = {
          kind: "other_file",
          otherFile,
          title: otherFile.name,
          left: pos.left,
          top: pos.top,
        };
      },

      openOtherFileNotice(otherFile) {
        if (!otherFile) return;
        this.showToast(
          `${otherFile.name} is not an editable MKV/MP4 container. Use the menu to rename, move, or delete.`,
          "info"
        );
      },

      openFileContextMenu(file, event) {
        if (!file) return;
        const pos = computeContextMenuPosition(
          event,
          C.CONTEXT_MENU.FILE_MENU_HEIGHT_PX
        );
        this.contextMenu = {
          kind: "file",
          file,
          title: file.filename,
          left: pos.left,
          top: pos.top,
        };
      },

      openFolderContextMenu(sub, event) {
        if (!sub || !sub.path) return;
        const pos = computeContextMenuPosition(
          event,
          C.CONTEXT_MENU.FOLDER_MENU_HEIGHT_PX
        );
        this.contextMenu = {
          kind: "folder",
          folder: sub,
          title: sub.name || Fmt.basenameOf(sub.path),
          left: pos.left,
          top: pos.top,
        };
      },

      closeContextMenu() {
        this.contextMenu = null;
      },

      async openFileInfoModal(file) {
        if (!file || !file.id) return;
        this.infoModal = {
          fileId: file.id,
          filename: file.filename || Fmt.basenameOf(file.filepath),
          loading: true,
          error: null,
          data: null,
        };
        try {
          const data = await Api.getFileInfo(file.id);
          if (this.infoModal && this.infoModal.fileId === file.id) {
            this.infoModal.data = data;
          }
        } catch (err) {
          if (this.infoModal && this.infoModal.fileId === file.id) {
            this.infoModal.error = err.message || "Failed to load media info.";
          }
        } finally {
          if (this.infoModal && this.infoModal.fileId === file.id) {
            this.infoModal.loading = false;
          }
        }
      },

      closeFileInfoModal() {
        this.infoModal = null;
      },

      runContextMenuAction(action) {
        const menu = this.contextMenu;
        this.closeContextMenu();
        if (!menu) return;

        if (menu.kind === "file" && menu.file) {
          if (action === "view_progress") {
            this.openJobsDrawer();
            return;
          }
          if (this.isFileBusy(menu.file)) {
            if (action === "info") {
              this.openFileInfoModal(menu.file);
              return;
            }
            this.showToast(this._busyLockMessage(menu.file), "info");
            return;
          }
          if (this.isFileScanning(menu.file)) {
            this.showToast("Scanning metadata — ready in a moment.", "info");
            return;
          }
          if (action === "info") {
            this.openFileInfoModal(menu.file);
            return;
          }
          if (action === "rescan") {
            this.rescanSingleFile(menu.file);
          } else if (action === "duplicate") {
            this.duplicateSingleFile(menu.file);
          } else if (action === "rename") {
            this.openRenameFileModal(menu.file);
          } else if (action === "move") {
            this.openMoveFilesModal([menu.file.id]);
          } else if (action === "delete") {
            this.openDeleteFilesModal([menu.file]);
          }
          return;
        }

        if (menu.kind === "other_file" && menu.otherFile) {
          if (action === "rename") {
            this.openRenameOtherFileModal(menu.otherFile);
          } else if (action === "move") {
            this.openMoveOtherFileModal(menu.otherFile);
          } else if (action === "delete") {
            this.openDeleteOtherFileModal(menu.otherFile);
          }
          return;
        }

        if (menu.kind === "folder" && menu.folder) {
          if (action === "rename") {
            this.openRenameFolderModal(menu.folder.path);
          } else if (action === "move") {
            this.openMoveFolderModal(menu.folder.path);
          } else if (action === "delete") {
            this.openDeleteFolderModal(menu.folder.path);
          }
        }
      },

      // Multi-Select Helpers
      isFileSelected(fileId) {
        return this.selectedFileIds.includes(fileId);
      },

      toggleFileSelection(fileId) {
        const target = (this.files || []).find((f) => f.id === fileId);
        if (target && this.isFileLocked(target)) {
          this.selectedFileIds = this.selectedFileIds.filter((id) => id !== fileId);
          return;
        }
        if (this.isFileSelected(fileId)) {
          this.selectedFileIds = this.selectedFileIds.filter((id) => id !== fileId);
        } else {
          this.selectedFileIds = [...this.selectedFileIds, fileId];
        }
      },

      toggleSelectAllPage() {
        if (this.allPageFilesSelected) {
          this.selectedFileIds = [];
        } else {
          this.selectedFileIds = (this.files || [])
            .filter((f) => !this.isFileLocked(f))
            .map((f) => f.id);
        }
      },

      clearSelection() {
        this.selectedFileIds = [];
        this.mobileSelectMode = false;
      },

      toggleMobileSelectMode() {
        this.mobileSelectMode = !this.mobileSelectMode;
        if (!this.mobileSelectMode) {
          this.selectedFileIds = [];
        }
      },

      // New Folder Modal (in Library Directory View)
      openNewFolderModal(parentPath = null) {
        this.folderBarMenuOpen = false;
        this.newFolderModal = {
          parent: FT.normalizeFolderPath(parentPath || this.currentDir),
          name: "",
          submitting: false,
          error: null,
        };
      },

      async submitNewFolderModal() {
        if (!this.newFolderModal || this.newFolderModal.submitting) return;
        const name = String(this.newFolderModal.name || "").trim();
        if (!name) {
          this.newFolderModal.error = "Enter a folder name.";
          return;
        }
        this.newFolderModal.submitting = true;
        this.newFolderModal.error = null;
        try {
          const created = await Api.createFolder(this.newFolderModal.parent, name);
          this.newFolderModal = null;
          await this.loadLibrary();
          this.showToast(`Created folder ${created.path}.`, "success");
        } catch (err) {
          this.newFolderModal.error = err.message || "Failed to create folder.";
        } finally {
          if (this.newFolderModal) {
            this.newFolderModal.submitting = false;
          }
        }
      },

      // Rename Modal (File or Folder)
      openRenameFileModal(file) {
        if (!file) return;
        this.renameModal = {
          kind: "file",
          fileId: file.id,
          originalName: file.filename,
          newName: file.filename,
          submitting: false,
          error: null,
        };
      },

      openRenameOtherFileModal(otherFile) {
        if (!otherFile || !otherFile.filepath) return;
        this.renameModal = {
          kind: "other_file",
          filepath: otherFile.filepath,
          originalName: otherFile.name,
          newName: otherFile.name,
          submitting: false,
          error: null,
        };
      },

      openRenameFolderModal(folderPath) {
        const normalized = FT.normalizeFolderPath(folderPath);
        if (normalized === C.FOLDER_TREE.ROOT_PATH) return;
        const currentName = Fmt.basenameOf(normalized);
        this.renameModal = {
          kind: "folder",
          path: normalized,
          originalName: currentName,
          newName: currentName,
          submitting: false,
          error: null,
        };
      },

      async submitRenameModal() {
        if (!this.renameModal || this.renameModal.submitting) return;
        const newName = String(this.renameModal.newName || "").trim();
        if (!newName) {
          this.renameModal.error = "Enter a new name.";
          return;
        }
        this.renameModal.submitting = true;
        this.renameModal.error = null;
        try {
          if (this.renameModal.kind === "file") {
            const updated = await Api.renameFile(this.renameModal.fileId, newName);
            this.renameModal = null;
            this.clearSelection();
            await this.loadLibrary();
            this.showToast(`Renamed to ${updated.filename}.`, "success");
          } else if (this.renameModal.kind === "other_file") {
            const res = await Api.renameOtherFile(this.renameModal.filepath, newName);
            this.renameModal = null;
            await this.loadLibrary();
            this.showToast(
              res.promoted_to_media
                ? `Renamed to ${Fmt.basenameOf(res.new_filepath)} — scanning metadata…`
                : `Renamed to ${Fmt.basenameOf(res.new_filepath || newName)}.`,
              "success"
            );
          } else {
            const oldPath = this.renameModal.path;
            const res = await Api.renameFolder(oldPath, newName);
            this.renameModal = null;
            if (
              this.currentDir === oldPath ||
              this.currentDir.startsWith(`${oldPath}/`)
            ) {
              this.currentDir = res.new_path || C.FOLDER_TREE.ROOT_PATH;
              this._syncUrlHash(true);
            }
            await this.loadLibrary();
            this.showToast(`Renamed folder to ${res.new_path}.`, "success");
          }
        } catch (err) {
          this.renameModal.error = err.message || "Rename failed.";
        } finally {
          if (this.renameModal) {
            this.renameModal.submitting = false;
          }
        }
      },

      get shouldConfirmDeleteFile() {
        return (
          !this.userSettings ||
          this.userSettings.confirm_delete_file !== false
        );
      },

      // Delete Confirmation Modal (Files, Other Files, Folder, or Save As + Delete Original)
      openDeleteFilesModal(fileList) {
        const items = Array.isArray(fileList) ? fileList.filter(Boolean) : [];
        if (items.length === 0) return;
        this.deleteModal = {
          kind: "files",
          fileIds: items.map((f) => f.id),
          names: items.map((f) => f.filename),
          submitting: false,
          error: null,
        };
        if (!this.shouldConfirmDeleteFile) {
          this.submitDeleteModal();
        }
      },

      openDeleteSelectedFilesModal() {
        const selected = this.files.filter((f) => this.selectedFileIds.includes(f.id));
        this.openDeleteFilesModal(selected);
      },

      openDeleteOtherFileModal(otherFile) {
        if (!otherFile || !otherFile.filepath) return;
        this.deleteModal = {
          kind: "other_file",
          filepath: otherFile.filepath,
          names: [otherFile.name],
          submitting: false,
          error: null,
        };
        if (!this.shouldConfirmDeleteFile) {
          this.submitDeleteModal();
        }
      },

      openDeleteFolderModal(folderPath) {
        const normalized = FT.normalizeFolderPath(folderPath);
        if (normalized === C.FOLDER_TREE.ROOT_PATH) return;
        const count = Number(this.folderCounts[normalized] || 0);
        this.deleteModal = {
          kind: "folder",
          path: normalized,
          name: Fmt.basenameOf(normalized),
          indexedFileCount: count,
          recursive: false,
          submitting: false,
          error: null,
        };
        if (!this.shouldConfirmDeleteFile && count === 0) {
          this.submitDeleteModal();
        }
      },

      async submitDeleteModal() {
        if (!this.deleteModal || this.deleteModal.submitting) return;
        if (this.deleteModal.kind === "save_as_delete_original") {
          this.deleteModal = null;
          await this.submitEditorJob(true);
          return;
        }
        this.deleteModal.submitting = true;
        this.deleteModal.error = null;
        try {
          if (this.deleteModal.kind === "files") {
            const res = await Api.deleteFiles(this.deleteModal.fileIds);
            this.deleteModal = null;
            this.clearSelection();
            await this.loadLibrary();
            this.loadDashboard();
            if (res.failed > 0) {
              const firstErr =
                (res.results.find((r) => !r.ok) || {}).error || "some files failed";
              this.showToast(
                `Deleted ${res.succeeded}, failed ${res.failed}: ${firstErr}`,
                "error"
              );
            } else {
              this.showToast(
                `Deleted ${res.succeeded} ${res.succeeded === 1 ? "file" : "files"}.`,
                "success"
              );
            }
          } else if (this.deleteModal.kind === "other_file") {
            const name = (this.deleteModal.names && this.deleteModal.names[0]) || "file";
            await Api.deleteOtherFile(this.deleteModal.filepath);
            this.deleteModal = null;
            await this.loadLibrary();
            this.showToast(`Deleted ${name}.`, "success");
          } else {
            const targetPath = this.deleteModal.path;
            const res = await Api.deleteFolder(targetPath, this.deleteModal.recursive);
            this.deleteModal = null;
            if (
              this.currentDir === targetPath ||
              this.currentDir.startsWith(`${targetPath}/`)
            ) {
              const idx = targetPath.lastIndexOf("/");
              this.currentDir = idx <= 0 ? C.FOLDER_TREE.ROOT_PATH : targetPath.slice(0, idx);
              this._syncUrlHash(true);
            }
            await this.loadLibrary();
            this.loadDashboard();
            this.showToast(
              `Deleted folder ${targetPath}${res.affected_files > 0 ? ` (${res.affected_files} files removed)` : ""}.`,
              "success"
            );
          }
        } catch (err) {
          this.deleteModal.error = err.message || "Delete failed.";
        } finally {
          if (this.deleteModal) {
            this.deleteModal.submitting = false;
          }
        }
      },

      async rescanEditorFile() {
        if (!this.editorFile || this.editorRescanning || this.isEditorFileBusy) return;
        this.editorRescanning = true;
        this.editorConflictMessage = null;
        try {
          const updated = await Api.rescanFile(this.editorFile.id);
          this.editorFile = updated;
          this.editor = Ed.createEditorState(updated, this.userSettings);
          this.loadDashboard();
          this.showToast(`Re-scanned ${updated.filename}.`, "success");
        } catch (err) {
          if (err.status === C.UNITS.HTTP_NOT_FOUND) {
            this.showToast("File was removed from disk.", "info");
            this.backToLibrary();
          } else {
            this.showToast(err.message || "Failed to re-scan file.", "error");
          }
        } finally {
          this.editorRescanning = false;
        }
      },

      clearContainerTitle() {
        if (!this.editor) return;
        this.editor.containerTitle = "";
      },

      moveEditorTrack(index, delta) {
        if (!this.editor) return;
        this.editor.tracks = Ed.moveTrack(this.editor.tracks, index, delta);
      },

      moveEditorTrackItem(orderKey, delta) {
        if (!this.editor || !orderKey) return;
        Ed.moveOrderedTrack(this.editor, orderKey, delta);
      },

      toggleTrackEnabled(track) {
        track.enabled = !track.enabled;
        if (!this.editorAllVideoUnchecked && this.editor) {
          this.editor.confirmNoVideo = false;
        }
      },

      sanitizeTrackMetadata(track) {
        if (!track || !track.enabled) return;
        track.track_name = Ed.formatTrackBadge(track);
      },

      get allTracksSanitized() {
        if (!this.editor || !Array.isArray(this.editor.tracks)) return true;
        const activeTracks = this.editor.tracks.filter((t) => t && t.enabled);
        const sourceDone =
          activeTracks.length > 0 &&
          activeTracks.every((t) => t.track_name === Ed.formatTrackBadge(t));
        const addedDone =
          !this.editor.audioFix ||
          !this.editor.audioFix.enabled ||
          this.editor.audioFix.trackTitle === this.defaultAddedAudioTitle();
        return sourceDone && addedDone;
      },

      sanitizeAllTracksMetadata() {
        if (!this.editor || !Array.isArray(this.editor.tracks)) return;
        this.editor.tracks.forEach((track) => {
          if (track && track.enabled) {
            track.track_name = Ed.formatTrackBadge(track);
          }
        });
        if (this.editor.audioFix && this.editor.audioFix.enabled) {
          this.sanitizeAddedAudioMetadata();
        }
      },

      async submitEditorJob(bypassDeleteConfirm = false) {
        if (!this.editor || !this.editorFile || this.editorSubmitting || this.isEditorFileBusy) return;
        this.editorConflictMessage = null;

        if (!this.editorHasChanges) {
          this.showToast("No changes selected to process.", "info");
          return;
        }

        if (this.editorKeptTrackCount === 0) {
          this.showToast("Select at least one track to keep before processing.", "error");
          return;
        }

        if (this.editorAllVideoUnchecked && !this.editor.confirmNoVideo) {
          this.showToast(
            "Warning: No video track selected. Confirm the warning checkbox to proceed.",
            "error"
          );
          return;
        }

        if (this.editorOutputPreview.isSaveAsInvalid) {
          this.showToast(
            this.editorOutputPreview.validationError ||
            "Choose a valid Save As destination path.",
            "error"
          );
          return;
        }

        if (
          this.editor.outputMode === C.EDITOR.OUTPUT_SAVE_AS &&
          this.editor.saveAs &&
          this.editor.saveAs.deleteOriginal &&
          !bypassDeleteConfirm &&
          this.shouldConfirmDeleteFile
        ) {
          this.deleteModal = {
            kind: "save_as_delete_original",
            names: [this.editorFile.filename],
            targetPath: this.editorOutputPreview.targetDisplayPath,
            submitting: false,
            error: null,
          };
          return;
        }

        this.editorSubmitting = true;
        try {
          const payload = Ed.buildJobRequestPayload(this.editor, this.editorFile);
          await Api.createJob(payload);
          this.showToast(`Queued ${this.editorFile.filename}.`, "success");
          await this.loadJobs();
          this.backToParentFolder();
          this.openJobsDrawer();
        } catch (err) {
          if (err.status === C.UNITS.HTTP_CONFLICT) {
            this.editorConflictMessage =
              err.message || "A job is already queued or running for this file.";
            this.showToast("Job already queued or running for this file.", "info");
            this.loadJobs();
          } else {
            this.showToast(err.message || "Failed to submit job.", "error");
          }
        } finally {
          this.editorSubmitting = false;
        }
      },

      // Job cancel & warnings helpers
      isCancellingJob(jobId) {
        return Boolean(this.cancellingJobIds[jobId]);
      },

      _resolveJobSummaryForCancel(jobId) {
        if (
          this.isJobRunning &&
          (this.liveJob.job_id === jobId ||
            (this.jobsState.active && this.jobsState.active.id === jobId))
        ) {
          const fp =
            this.liveJob.filepath ||
            (this.jobsState.active && this.jobsState.active.filepath) ||
            "";
          return {
            jobId,
            filepath: fp,
            filename: Fmt.basenameOf(fp) || jobId,
            isRunning: true,
          };
        }
        const queued = (this.jobsState.queued || []).find((j) => j.id === jobId);
        const fp = (queued && queued.filepath) || "";
        return {
          jobId,
          filepath: fp,
          filename: Fmt.basenameOf(fp) || jobId,
          isRunning: false,
        };
      },

      closeCancelJobModal() {
        if (this.cancelJobModal && this.cancelJobModal.submitting) return;
        this.cancelJobModal = null;
      },

      async confirmCancelJobModal() {
        if (!this.cancelJobModal || this.cancelJobModal.submitting) return;
        const { jobId, dontAskAgain } = this.cancelJobModal;
        this.cancelJobModal.submitting = true;
        if (dontAskAgain) {
          this.userSettings = {
            ...this.userSettings,
            confirm_cancel_job: false,
          };
          this.updateUserSettings({ confirm_cancel_job: false });
        }
        try {
          await this._executeCancelJob(jobId);
        } finally {
          this.cancelJobModal = null;
        }
      },

      async cancelJobById(jobId, options = {}) {
        if (!jobId || this.isCancellingJob(jobId)) return;
        const shouldConfirm =
          !options.skipConfirm &&
          (!this.userSettings || this.userSettings.confirm_cancel_job !== false);
        if (shouldConfirm) {
          const info = this._resolveJobSummaryForCancel(jobId);
          this.cancelJobModal = {
            ...info,
            dontAskAgain: false,
            submitting: false,
          };
          return;
        }
        await this._executeCancelJob(jobId);
      },

      async _executeCancelJob(jobId) {
        if (!jobId || this.isCancellingJob(jobId)) return;
        this.cancellingJobIds = { ...this.cancellingJobIds, [jobId]: true };
        try {
          await Api.cancelJob(jobId);
          this.showToast("Cancellation requested.", "info");
          await this.loadJobs();
          this.loadDashboard();
        } catch (err) {
          this.showToast(err.message || "Failed to cancel job.", "error");
        } finally {
          const next = { ...this.cancellingJobIds };
          delete next[jobId];
          this.cancellingJobIds = next;
        }
      },

      toggleHistoryWarnings(jobId) {
        this.expandedHistoryWarnings = {
          ...this.expandedHistoryWarnings,
          [jobId]: !this.expandedHistoryWarnings[jobId],
        };
      },

      isDeletingHistory(jobId) {
        return Boolean(this.deletingHistoryIds[jobId]);
      },

      async deleteHistoryItem(jobId) {
        if (!jobId || this.isDeletingHistory(jobId)) return;
        this.deletingHistoryIds = { ...this.deletingHistoryIds, [jobId]: true };
        try {
          await Api.deleteJobHistoryItem(jobId);
          this.confirmClearHistory = false;
          await this.loadJobs();
          this.loadDashboard();
        } catch (err) {
          this.showToast(err.message || "Failed to remove history item.", "error");
        } finally {
          const next = { ...this.deletingHistoryIds };
          delete next[jobId];
          this.deletingHistoryIds = next;
        }
      },

      async clearAllHistory() {
        if (this.clearingHistory) return;
        this.clearingHistory = true;
        try {
          const res = await Api.clearJobHistory();
          this.confirmClearHistory = false;
          this.expandedHistoryWarnings = {};
          await this.loadJobs();
          this.loadDashboard();
          const count = Number((res && res.deleted) || 0);
          this.showToast(
            count > 0
              ? `Cleared ${count} ${count === 1 ? "job" : "jobs"} from history.`
              : "Job history is already empty.",
            "info"
          );
        } catch (err) {
          this.showToast(err.message || "Failed to clear job history.", "error");
        } finally {
          this.clearingHistory = false;
        }
      },

      // Library filter, search, sort & pagination actions
      toggleWorkflowChip(filterKey) {
        if (filterKey === C.FILTERS.ALL) {
          if (
            !this.needsConversion &&
            !this.noDolby &&
            !this.hasMetadata &&
            !this.audioType &&
            !this.audioCodec &&
            !this.videoCodec &&
            !this.resolution &&
            !this.dynamicRange &&
            !this.audioChannels &&
            !this.audioLanguage &&
            !this.noSubtitles
          ) {
            return;
          }
          this.needsConversion = false;
          this.noDolby = false;
          this.hasMetadata = false;
          this.audioType = "";
          this.audioCodec = "";
          this.videoCodec = "";
          this.resolution = "";
          this.dynamicRange = "";
          this.audioChannels = "";
          this.audioLanguage = "";
          this.noSubtitles = false;
        } else if (filterKey === C.FILTERS.NEEDS_CONVERSION) {
          this.needsConversion = !this.needsConversion;
          this.noDolby = false;
        } else if (filterKey === C.FILTERS.NO_DOLBY) {
          this.noDolby = !this.noDolby;
        } else if (filterKey === C.FILTERS.HAS_METADATA) {
          this.hasMetadata = !this.hasMetadata;
        }
        this._clearLibraryReturnOrigin();
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this._syncUrlHash(true);
        this.loadLibrary();
      },

      toggleAudioType(soundType) {
        this._clearLibraryReturnOrigin();
        this.audioType = this.audioType === soundType ? "" : soundType;
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this._syncUrlHash(true);
        this.loadLibrary();
      },

      onSearchInput() {
        if (this._searchTimer !== null) {
          clearTimeout(this._searchTimer);
        }
        this._searchTimer = setTimeout(() => {
          this._searchTimer = null;
          this._clearLibraryReturnOrigin();
          this.page = C.PAGINATION.DEFAULT_PAGE;
          this._syncUrlHash(true);
          this.loadLibrary();
        }, C.TIMINGS_MS.SEARCH_DEBOUNCE);
      },

      clearSearch() {
        if (this._searchTimer !== null) {
          clearTimeout(this._searchTimer);
          this._searchTimer = null;
        }
        this._clearLibraryReturnOrigin();
        this.searchQuery = "";
        this.appliedSearchQuery = "";
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this._syncUrlHash(true);
        this.loadLibrary({ clearStale: true });
      },

      toggleSort(field) {
        if (this.sortField === field) {
          this.sortOrder =
            this.sortOrder === C.SORT_ORDERS.ASC
              ? C.SORT_ORDERS.DESC
              : C.SORT_ORDERS.ASC;
        } else {
          this.sortField = field;
          this.sortOrder = C.SORT_ORDERS.ASC;
        }
        this._clearLibraryReturnOrigin();
        this.page = C.PAGINATION.DEFAULT_PAGE;
        this._syncUrlHash(true);
        this.loadLibrary();
      },

      goToPage(nextPage) {
        if (
          nextPage < C.PAGINATION.DEFAULT_PAGE ||
          (this.totalPages > 0 && nextPage > this.totalPages)
        ) {
          return;
        }
        this._clearLibraryReturnOrigin();
        this.page = nextPage;
        this._syncUrlHash(true);
        this.loadLibrary();
        if (this.$refs?.libraryScrollContainer) {
          this.$refs.libraryScrollContainer.scrollTo({ top: 0, behavior: "smooth" });
        } else {
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      },

      // Scan & per-row rescan actions
      async triggerScan(full = false) {
        this.scanMenuOpen = false;
        if (this.scanTriggering || this.isScanRunning) {
          this.showToast("A library scan is already running.", "info");
          return;
        }
        this.scanTriggering = true;
        try {
          const initial = await Api.triggerScan(full);
          this._lastScanStatus = initial.status || C.STATUSES.RUNNING;
          this.scanProgress = { ...this.scanProgress, ...initial };
          this._connectStreams();
          this.showToast(
            full ? "Started full library re-scan." : "Started incremental scan.",
            "info"
          );
        } catch (err) {
          if (err.status === C.UNITS.HTTP_CONFLICT) {
            this.showToast("A library scan is already in progress.", "info");
          } else {
            this.showToast(err.message || "Failed to start scan.", "error");
          }
        } finally {
          this.scanTriggering = false;
        }
      },

      isRescanning(fileId) {
        return Boolean(this.rescanningIds[fileId]);
      },

      isDuplicating(fileId) {
        return Boolean(this.duplicatingIds[fileId]);
      },

      async duplicateSingleFile(file) {
        if (!file || this.isDuplicating(file.id) || this.isFileBusy(file)) return;
        this.duplicatingIds = { ...this.duplicatingIds, [file.id]: true };
        try {
          const created = await Api.duplicateFile(file.id);
          await this.loadLibrary({ preserveSelection: true });
          this.loadDashboard();
          this.showToast(`Created duplicate ${created.filename}.`, "success");
        } catch (err) {
          this.showToast(
            err.message || `Failed to duplicate ${file.filename}.`,
            "error"
          );
        } finally {
          const next = { ...this.duplicatingIds };
          delete next[file.id];
          this.duplicatingIds = next;
        }
      },

      async rescanSingleFile(file) {
        if (!file || this.isRescanning(file.id) || this.isFileBusy(file)) return;
        this.rescanningIds = { ...this.rescanningIds, [file.id]: true };
        try {
          const updated = await Api.rescanFile(file.id);
          this.files = this.files.map((item) =>
            item.id === file.id ? { ...item, ...updated } : item
          );
          this.loadDashboard();
          this.showToast(`Re-scanned ${file.filename}.`, "success");
        } catch (err) {
          if (err.status === C.UNITS.HTTP_NOT_FOUND) {
            this.files = this.files.filter((item) => item.id !== file.id);
            this.totalFiles = Math.max(0, this.totalFiles - 1);
            this.loadDashboard();
            this.showToast(`${file.filename} was removed from disk.`, "info");
          } else {
            this.showToast(
              err.message || `Failed to re-scan ${file.filename}.`,
              "error"
            );
          }
        } finally {
          const next = { ...this.rescanningIds };
          delete next[file.id];
          this.rescanningIds = next;
        }
      },

      // Floating toast stack helpers
      showToast(message, type = C.TOAST.TYPE_INFO) {
        const result = TS.pushToast(this.toasts, message, type, ++this._toastSeq);
        if (!result.toast) return;
        (result.removed || []).forEach((old) => this._clearToastTimer(old.id));
        this.toasts = result.stack;
        const id = result.toast.id;
        this._toastTimers[id] = setTimeout(
          () => this.dismissToast(id),
          result.toast.durationMs
        );
      },

      dismissToast(id) {
        this._clearToastTimer(id);
        this.toasts = TS.dismissToast(this.toasts, id);
      },

      _clearToastTimer(id) {
        if (this._toastTimers[id] !== undefined) {
          clearTimeout(this._toastTimers[id]);
          delete this._toastTimers[id];
        }
      },
    };
  };
})(window);
