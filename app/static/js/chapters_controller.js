/* Chapters Sheet controller methods, serial frame loader, and ObjectURL lifecycle. */

(function attachChaptersController(global) {
  const C = global.MKVForgeConstants;
  const Api = global.MKVForgeApi;
  const Ed = global.MKVForgeEditor;
  const Ch = global.MKVForgeChapters;

  const DRAWER_MAIN = "main";
  const DRAWER_SCAN_SCENES = "scan_scenes";
  const DRAWER_PASTE = "paste";
  const HTTP_NO_CONTENT = 204;
  const HTTP_SERVER_ERROR_MIN = 500;
  const FRAME_FETCH_MAX_ATTEMPTS = 2;

  function _readSavedScanDensity() {
    try {
      const raw = global.localStorage
        ? global.localStorage.getItem(C.SCENE_PICKER.SCAN_DENSITY_STORAGE_KEY)
        : null;
      const parsed = parseInt(String(raw || ""), 10);
      if (C.SCENE_PICKER.SCAN_DENSITY_PRESETS.includes(parsed)) {
        return parsed;
      }
    } catch (_err) {
      // Ignore storage access restrictions
    }
    return C.SCENE_PICKER.SCAN_DEFAULT_DENSITY;
  }

  function _saveScanDensity(density) {
    try {
      if (global.localStorage) {
        global.localStorage.setItem(
          C.SCENE_PICKER.SCAN_DENSITY_STORAGE_KEY,
          String(density)
        );
      }
    } catch (_err) {
      // Ignore storage access restrictions
    }
  }

  function _nextKey(modal) {
    modal._keySeq += 1;
    return `ch-${modal._keySeq}`;
  }

  function _toModalRows(modal, entries) {
    const normalized = Ch.sortAndNormalizeChapters(entries, modal.durationMs);
    return normalized.map((item) => ({
      _key: _nextKey(modal),
      start_ms: item.start_ms,
      timeText: Ch.formatChapterTime(item.start_ms),
      title: item.title,
      thumbUrl: null,
    }));
  }

  function _plainEntries(rows) {
    return (Array.isArray(rows) ? rows : []).map((row) => ({
      start_ms: row.start_ms,
      title: row.title,
    }));
  }

  function _triggerBlobDownload(content, filename, mimeType) {
    if (
      !global.document ||
      !global.URL ||
      typeof global.URL.createObjectURL !== "function"
    ) {
      return false;
    }
    const blob = new Blob([content], { type: mimeType });
    const url = global.URL.createObjectURL(blob);
    const link = global.document.createElement("a");
    link.href = url;
    link.download = filename;
    global.document.body.appendChild(link);
    link.click();
    global.document.body.removeChild(link);
    global.URL.revokeObjectURL(url);
    return true;
  }

  function createMethods() {
    // Kept strictly outside Alpine's reactive Proxy so AbortController & Set native
    // private slots never throw `TypeError: Illegal invocation` on abort/add/delete.
    const rt = {
      abortController: null,
      objectUrls: new Set(),
      fineTuneTimer: null,
      peekRunSeq: 0,
      rowRunSeq: 0,
      railRunSeq: 0,
      scanRunSeq: 0,
    };

    return {
      _abortChapterFrameRequests() {
        if (rt.fineTuneTimer !== null) {
          clearTimeout(rt.fineTuneTimer);
          rt.fineTuneTimer = null;
        }
        if (rt.abortController) {
          const active = rt.abortController;
          rt.abortController = null;
          active.abort();
        }
      },

      _trackChapterObjectUrl(url) {
        if (!url) return;
        rt.objectUrls.add(url);
      },

      _revokeChapterObjectUrl(url) {
        if (!url) return;
        rt.objectUrls.delete(url);
        if (global.URL && typeof global.URL.revokeObjectURL === "function") {
          try {
            global.URL.revokeObjectURL(url);
          } catch (_err) {
            // Safe revoke cleanup
          }
        }
      },

      _revokeAllChapterObjectUrls() {
        const urls = Array.from(rt.objectUrls);
        rt.objectUrls.clear();
        if (global.URL && typeof global.URL.revokeObjectURL === "function") {
          urls.forEach((url) => {
            try {
              global.URL.revokeObjectURL(url);
            } catch (_err) {
              // Safe revoke cleanup
            }
          });
        }
      },

      async _fetchChapterFrameObjectUrl(tsMs, mode, signal) {
        const modal = this.chaptersModal;
        if (!modal || !modal.hasVideo) {
          return { url: null, conflict: false, aborted: false };
        }
        const frameUrl = Api.buildSceneFrameUrl(
          modal.fileId,
          tsMs,
          mode,
          modal.videoFingerprint
        );
        for (let attempt = 1; attempt <= FRAME_FETCH_MAX_ATTEMPTS; attempt += 1) {
          try {
            const response = await fetch(frameUrl, { signal, cache: "default" });
            if (signal && signal.aborted) {
              return { url: null, conflict: false, aborted: true };
            }
            if (response.status === C.UNITS.HTTP_CONFLICT) {
              let detail =
                "This file has an active in-place job — scene previews are paused until it finishes.";
              try {
                const payload = await response.json();
                if (payload && typeof payload.detail === "string") {
                  detail = payload.detail;
                }
              } catch (_err) {
                // Keep fallback message
              }
              if (this.chaptersModal) {
                this.chaptersModal.conflictMessage = detail;
              }
              return { url: null, conflict: true, aborted: false };
            }
            if (!response.ok || response.status === HTTP_NO_CONTENT) {
              const retryable =
                response.status === HTTP_NO_CONTENT ||
                response.status >= HTTP_SERVER_ERROR_MIN;
              if (retryable && attempt < FRAME_FETCH_MAX_ATTEMPTS) {
                continue;
              }
              return { url: null, conflict: false, aborted: false };
            }
            const blob = await response.blob();
            if (signal && signal.aborted) {
              return { url: null, conflict: false, aborted: true };
            }
            const objectUrl = global.URL.createObjectURL(blob);
            this._trackChapterObjectUrl(objectUrl);
            return { url: objectUrl, conflict: false, aborted: false };
          } catch (err) {
            if (err && err.name === "AbortError") {
              return { url: null, conflict: false, aborted: true };
            }
            if (attempt < FRAME_FETCH_MAX_ATTEMPTS) {
              continue;
            }
            return { url: null, conflict: false, aborted: false };
          }
        }
      },

      _pushChapterUndoSnapshot() {
        const modal = this.chaptersModal;
        if (!modal) return;
        modal.undoSnapshot = _plainEntries(modal.chapters);
      },

      undoChapterEdit() {
        const modal = this.chaptersModal;
        if (!modal || !Array.isArray(modal.undoSnapshot)) return;
        this._clearChapterRailThumbUrls();
        this._collapseExpandedChapterPreview();
        modal.chapters = _toModalRows(modal, modal.undoSnapshot);
        modal.undoSnapshot = null;
      },

      _clearChapterRailThumbUrls() {
        const modal = this.chaptersModal;
        if (!modal) return;
        modal.chapters.forEach((row) => {
          if (row.thumbUrl) {
            this._revokeChapterObjectUrl(row.thumbUrl);
            row.thumbUrl = null;
          }
        });
        modal.previewsActive = false;
        modal.previewsLoading = false;
        modal.previewsLoadedCount = 0;
        modal.previewsTotalCount = 0;
      },

      _collapseExpandedChapterPreview() {
        const modal = this.chaptersModal;
        if (!modal) return;
        if (modal.expandedFrameUrl) {
          this._revokeChapterObjectUrl(modal.expandedFrameUrl);
          modal.expandedFrameUrl = null;
        }
        modal.expandedChapterKey = null;
        modal.expandedFrameLoading = false;
        modal.expandedFrameError = null;
      },

      _clearDraftPeekUrls() {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        (modal.draftPeek.frames || []).forEach((frame) => {
          if (frame && frame.url) {
            this._revokeChapterObjectUrl(frame.url);
            frame.url = null;
          }
        });
      },

      _clearScanSceneTileUrls() {
        const modal = this.chaptersModal;
        if (!modal) return;
        (modal.scanTiles || []).forEach((tile) => {
          if (tile && tile.url) {
            this._revokeChapterObjectUrl(tile.url);
            tile.url = null;
          }
        });
      },

      async openChaptersModal() {
        if (!this.editor || !this.editorFile) return;
        rt.peekRunSeq += 1;
        rt.rowRunSeq += 1;
        rt.railRunSeq += 1;
        rt.scanRunSeq += 1;
        this._abortChapterFrameRequests();
        this._revokeAllChapterObjectUrls();

        const file = this.editorFile;
        const durationMs = Math.max(
          0,
          Math.round(Number(file.duration_us || 0) / C.UNITS.US_PER_MS)
        );
        const modal = {
          fileId: file.id,
          filename: file.filename || "",
          durationMs,
          hasVideo: durationMs > 0,
          videoFingerprint: Ch.buildVideoFingerprint(file),
          loading: !this.editor.chaptersLoaded,
          error: null,
          conflictMessage: null,
          hasComplexChapters: Boolean(this.editor.hasComplexChapters),
          drawer: DRAWER_MAIN,
          _keySeq: 0,
          chapters: [],
          expandedChapterKey: null,
          expandedFrameUrl: null,
          expandedFrameLoading: false,
          expandedFrameError: null,
          draftPeek: null,
          previewsActive: false,
          previewsLoading: false,
          previewsLoadedCount: 0,
          previewsTotalCount: 0,
          scanDensity: _readSavedScanDensity(),
          scanTiles: [],
          scanLoading: false,
          scanLoadedCount: 0,
          scanTotalCount: 0,
          scanSelectedTsList: [],
          undoSnapshot: null,
          quickSkipOpen: false,
          quickSkipCustomMin: "",
          menuOpen: false,
          shiftOpen: false,
          shiftDraftSec: "",
          pasteText: "",
          pasteFilename: null,
          pasteBusy: false,
          pasteError: null,
          pasteWarnings: [],
        };
        this.chaptersModal = modal;

        if (this.editor.chaptersLoaded) {
          const sourceList =
            Ed.resolveEffectiveChaptersAction(this.editor) === C.CHAPTERS.ACTION_CLEAR
              ? []
              : this.editor.chapters.length > 0
                ? this.editor.chapters
                : this.editor.originalChapters || [];
          modal.chapters = _toModalRows(modal, sourceList);
          return;
        }

        try {
          const data = await Api.getFileChapters(file.id);
          if (!this.chaptersModal || this.chaptersModal.fileId !== file.id) return;
          const fetched = Ch.sortAndNormalizeChapters(
            Array.isArray(data.chapters) ? data.chapters : [],
            durationMs
          );
          this.editor.originalChapters = fetched.map((ch) => ({ ...ch }));
          this.editor.chaptersLoaded = true;
          this.editor.hasComplexChapters = Boolean(data.has_complex_chapters);
          this.chaptersModal.hasComplexChapters = Boolean(data.has_complex_chapters);

          const effectiveAction = Ed.resolveEffectiveChaptersAction(this.editor);
          const initialList =
            effectiveAction === C.CHAPTERS.ACTION_CLEAR
              ? []
              : effectiveAction === C.CHAPTERS.ACTION_REPLACE
                ? this.editor.chapters
                : fetched;
          this.chaptersModal.chapters = _toModalRows(this.chaptersModal, initialList);
        } catch (err) {
          if (!this.chaptersModal || this.chaptersModal.fileId !== file.id) return;
          if (err && err.status === C.UNITS.HTTP_CONFLICT) {
            this.chaptersModal.conflictMessage = err.message;
          } else {
            this.chaptersModal.error =
              (err && err.message) || "Failed to load chapters.";
          }
        } finally {
          if (this.chaptersModal && this.chaptersModal.fileId === file.id) {
            this.chaptersModal.loading = false;
          }
        }
      },

      closeChaptersModal() {
        rt.peekRunSeq += 1;
        rt.rowRunSeq += 1;
        rt.railRunSeq += 1;
        rt.scanRunSeq += 1;
        this._abortChapterFrameRequests();
        this._revokeAllChapterObjectUrls();
        this.chaptersModal = null;
      },

      commitChaptersModal() {
        const modal = this.chaptersModal;
        if (!modal || !this.editor) return;
        const cleaned = Ch.sortAndNormalizeChapters(
          _plainEntries(modal.chapters),
          modal.durationMs
        );
        Ed.applyChapterEdits(this.editor, cleaned, modal.durationMs);
        this.closeChaptersModal();
      },

      onChapterScrubberClick(event) {
        const modal = this.chaptersModal;
        if (!modal || !modal.hasVideo || !event || !event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (!rect || rect.width <= 0) return;
        const ratio = Math.max(
          0,
          Math.min(1, (event.clientX - rect.left) / rect.width)
        );
        const tsMs = Ch.clampFrameTimestampMs(
          Math.round(ratio * modal.durationMs),
          modal.durationMs
        );
        this.openChapterDraftPeek(tsMs);
      },

      async openChapterDraftPeek(tsMs, presetTitle = "") {
        const modal = this.chaptersModal;
        if (!modal) return;
        const runId = ++rt.peekRunSeq;
        this._abortChapterFrameRequests();
        this._clearDraftPeekUrls();
        this._collapseExpandedChapterPreview();
        modal.drawer = DRAWER_MAIN;
        modal.quickSkipOpen = false;
        modal.menuOpen = false;
        modal.conflictMessage = null;

        const clampedTs = Ch.clampFrameTimestampMs(tsMs, modal.durationMs);
        const peekTsList = Ch.buildPeekFrameTimestamps(clampedTs, modal.durationMs);
        const offsetSec = C.SCENE_PICKER.PEEK_OFFSET_SEC;
        const labels = [`-${offsetSec}s`, "Selected", `+${offsetSec}s`];

        modal.draftPeek = {
          tsMs: clampedTs,
          timeText: Ch.formatChapterTime(clampedTs),
          title: String(presetTitle || "").trim(),
          added: false,
          frames: peekTsList.map((frameTs, idx) => ({
            _id: `peek-${runId}-${idx}`,
            tsMs: frameTs,
            label: labels[idx],
            url: null,
            failed: false,
            isCenter: idx === 1,
          })),
          loading: modal.hasVideo,
          error: null,
        };

        if (!modal.hasVideo) return;

        const controller = new AbortController();
        rt.abortController = controller;
        const mode = C.SCENE_PICKER.MODE_FAST;

        // Fetch center frame first for perceived instant response, then neighbors serially
        const fetchOrder = [1, 0, 2];
        for (const frameIdx of fetchOrder) {
          const frame = this.chaptersModal.draftPeek.frames[frameIdx];
          const res = await this._fetchChapterFrameObjectUrl(
            frame.tsMs,
            mode,
            controller.signal
          );
          if (
            runId !== rt.peekRunSeq ||
            controller.signal.aborted ||
            res.aborted ||
            res.conflict ||
            !this.chaptersModal ||
            !this.chaptersModal.draftPeek
          ) {
            if (
              runId === rt.peekRunSeq &&
              this.chaptersModal &&
              this.chaptersModal.draftPeek
            ) {
              this.chaptersModal.draftPeek.loading = false;
            }
            return;
          }
          frame.url = res.url;
          frame.failed = !res.url;
        }
        if (
          runId === rt.peekRunSeq &&
          this.chaptersModal &&
          this.chaptersModal.draftPeek
        ) {
          this.chaptersModal.draftPeek.loading = false;
        }
      },

      nudgeChapterDraftPeek(deltaSec) {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        const nextTs = Ch.clampFrameTimestampMs(
          modal.draftPeek.tsMs + Math.round(Number(deltaSec || 0) * C.UNITS.MS_PER_SEC),
          modal.durationMs
        );
        this._setDraftPeekTimestamp(nextTs);
      },

      commitChapterDraftPeekTime() {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        const parsed = Ch.parseChapterTimeInput(modal.draftPeek.timeText);
        if (parsed === null) {
          modal.draftPeek.timeText = Ch.formatChapterTime(modal.draftPeek.tsMs);
          return;
        }
        const clamped = Ch.clampFrameTimestampMs(parsed, modal.durationMs);
        this._setDraftPeekTimestamp(clamped);
      },

      _setDraftPeekTimestamp(nextTs) {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        const runId = ++rt.peekRunSeq;
        modal.draftPeek.tsMs = nextTs;
        modal.draftPeek.timeText = Ch.formatChapterTime(nextTs);
        modal.draftPeek.added = false;
        const peekTsList = Ch.buildPeekFrameTimestamps(nextTs, modal.durationMs);
        modal.draftPeek.frames.forEach((frame, idx) => {
          frame.tsMs = peekTsList[idx];
        });

        if (!modal.hasVideo) return;
        this._abortChapterFrameRequests();
        modal.draftPeek.loading = true;
        rt.fineTuneTimer = setTimeout(() => {
          rt.fineTuneTimer = null;
          this._refreshDraftPeekCenterAccurate(runId);
        }, C.SCENE_PICKER.FINE_TUNE_DEBOUNCE_MS);
      },

      async _refreshDraftPeekCenterAccurate(runId) {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek || !modal.hasVideo || runId !== rt.peekRunSeq) {
          return;
        }
        const controller = new AbortController();
        rt.abortController = controller;
        const centerFrame = modal.draftPeek.frames[1];
        const res = await this._fetchChapterFrameObjectUrl(
          modal.draftPeek.tsMs,
          C.SCENE_PICKER.MODE_ACCURATE,
          controller.signal
        );
        if (
          runId !== rt.peekRunSeq ||
          !this.chaptersModal ||
          !this.chaptersModal.draftPeek
        ) {
          return;
        }
        if (!res.aborted && res.url) {
          if (centerFrame.url) {
            this._revokeChapterObjectUrl(centerFrame.url);
          }
          centerFrame.url = res.url;
          centerFrame.failed = false;
        }
        this.chaptersModal.draftPeek.loading = false;
      },

      selectDraftPeekFrame(frameIdx) {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        const frame = modal.draftPeek.frames[frameIdx];
        if (!frame) return;
        this.openChapterDraftPeek(frame.tsMs, modal.draftPeek.title);
      },

      setDraftPeekQuickTitle(chipTitle) {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek) return;
        modal.draftPeek.title = String(chipTitle || "").trim();
      },

      commitChapterDraftPeek() {
        const modal = this.chaptersModal;
        if (!modal || !modal.draftPeek || modal.draftPeek.added) return;
        this._pushChapterUndoSnapshot();
        const combined = [
          ..._plainEntries(modal.chapters),
          {
            start_ms: modal.draftPeek.tsMs,
            title: modal.draftPeek.title,
          },
        ];
        const withStart = Ch.ensureZeroStartChapter(combined);
        this._clearChapterRailThumbUrls();
        modal.chapters = _toModalRows(modal, withStart);
        modal.draftPeek.added = true;
        modal.draftPeek.title = "";
      },

      closeChapterDraftPeek() {
        rt.peekRunSeq += 1;
        this._abortChapterFrameRequests();
        this._clearDraftPeekUrls();
        if (this.chaptersModal) {
          this.chaptersModal.draftPeek = null;
        }
      },

      addManualChapterRow() {
        const modal = this.chaptersModal;
        if (!modal) return;
        const lastStart =
          modal.chapters.length > 0
            ? modal.chapters[modal.chapters.length - 1].start_ms
            : -C.UNITS.SEC_PER_MIN * C.UNITS.MS_PER_SEC;
        const nextTs = Ch.clampFrameTimestampMs(
          Math.max(0, lastStart + C.UNITS.SEC_PER_MIN * C.UNITS.MS_PER_SEC),
          modal.durationMs
        );
        this._pushChapterUndoSnapshot();
        const nextList = [
          ..._plainEntries(modal.chapters),
          { start_ms: nextTs, title: "" },
        ];
        this._clearChapterRailThumbUrls();
        modal.chapters = _toModalRows(modal, nextList);
      },

      async toggleChapterRowPreview(chapterKey) {
        const modal = this.chaptersModal;
        if (!modal) return;
        if (modal.expandedChapterKey === chapterKey) {
          rt.rowRunSeq += 1;
          this._abortChapterFrameRequests();
          this._collapseExpandedChapterPreview();
          return;
        }

        const row = modal.chapters.find((item) => item._key === chapterKey);
        if (!row) return;

        const runId = ++rt.rowRunSeq;
        rt.peekRunSeq += 1;
        this._abortChapterFrameRequests();
        this._clearDraftPeekUrls();
        modal.draftPeek = null;
        this._collapseExpandedChapterPreview();

        modal.expandedChapterKey = chapterKey;
        modal.expandedFrameLoading = modal.hasVideo;
        modal.expandedFrameError = null;

        if (!modal.hasVideo) return;

        const controller = new AbortController();
        rt.abortController = controller;
        const res = await this._fetchChapterFrameObjectUrl(
          row.start_ms,
          C.SCENE_PICKER.MODE_ACCURATE,
          controller.signal
        );
        if (
          runId !== rt.rowRunSeq ||
          !this.chaptersModal ||
          this.chaptersModal.expandedChapterKey !== chapterKey
        ) {
          return;
        }
        this.chaptersModal.expandedFrameLoading = false;
        if (res.aborted || res.conflict) return;
        if (res.url) {
          this.chaptersModal.expandedFrameUrl = res.url;
        } else {
          this.chaptersModal.expandedFrameError = "Could not load frame preview.";
        }
      },

      nudgeChapterRow(chapterKey, deltaSec) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const row = modal.chapters.find((item) => item._key === chapterKey);
        if (!row) return;
        this._pushChapterUndoSnapshot();
        const nextTs = Ch.clampFrameTimestampMs(
          row.start_ms + Math.round(Number(deltaSec || 0) * C.UNITS.MS_PER_SEC),
          modal.durationMs
        );
        row.start_ms = nextTs;
        row.timeText = Ch.formatChapterTime(nextTs);
        modal.chapters.sort((a, b) => a.start_ms - b.start_ms);

        if (modal.expandedChapterKey === chapterKey && modal.hasVideo) {
          const runId = ++rt.rowRunSeq;
          this._abortChapterFrameRequests();
          modal.expandedFrameLoading = true;
          modal.expandedFrameError = null;
          rt.fineTuneTimer = setTimeout(() => {
            rt.fineTuneTimer = null;
            this._refreshExpandedChapterFrame(chapterKey, runId);
          }, C.SCENE_PICKER.FINE_TUNE_DEBOUNCE_MS);
        }
      },

      async _refreshExpandedChapterFrame(chapterKey, runId = rt.rowRunSeq) {
        const modal = this.chaptersModal;
        if (
          !modal ||
          modal.expandedChapterKey !== chapterKey ||
          !modal.hasVideo ||
          runId !== rt.rowRunSeq
        ) {
          return;
        }
        const row = modal.chapters.find((item) => item._key === chapterKey);
        if (!row) return;
        const controller = new AbortController();
        rt.abortController = controller;
        const res = await this._fetchChapterFrameObjectUrl(
          row.start_ms,
          C.SCENE_PICKER.MODE_ACCURATE,
          controller.signal
        );
        if (
          runId !== rt.rowRunSeq ||
          !this.chaptersModal ||
          this.chaptersModal.expandedChapterKey !== chapterKey
        ) {
          return;
        }
        this.chaptersModal.expandedFrameLoading = false;
        if (res.aborted || res.conflict) return;
        if (res.url) {
          if (this.chaptersModal.expandedFrameUrl) {
            this._revokeChapterObjectUrl(this.chaptersModal.expandedFrameUrl);
          }
          this.chaptersModal.expandedFrameUrl = res.url;
        } else {
          this.chaptersModal.expandedFrameError = "Could not load frame preview.";
        }
      },

      commitChapterRowTime(chapterKey) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const row = modal.chapters.find((item) => item._key === chapterKey);
        if (!row) return;
        const parsed = Ch.parseChapterTimeInput(row.timeText);
        if (parsed === null) {
          row.timeText = Ch.formatChapterTime(row.start_ms);
          return;
        }
        const clamped =
          modal.durationMs > 0 ? Math.min(parsed, modal.durationMs) : parsed;
        if (clamped !== row.start_ms) {
          this._pushChapterUndoSnapshot();
          row.start_ms = clamped;
          modal.chapters.sort((a, b) => a.start_ms - b.start_ms);
        }
        row.timeText = Ch.formatChapterTime(row.start_ms);
      },

      setChapterRowQuickTitle(chapterKey, chipTitle) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const row = modal.chapters.find((item) => item._key === chapterKey);
        if (!row) return;
        this._pushChapterUndoSnapshot();
        row.title = String(chipTitle || "").trim();
      },

      deleteChapterRow(chapterKey) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const target = modal.chapters.find((item) => item._key === chapterKey);
        if (!target) return;
        this._pushChapterUndoSnapshot();
        if (target.thumbUrl) {
          this._revokeChapterObjectUrl(target.thumbUrl);
        }
        if (modal.expandedChapterKey === chapterKey) {
          rt.rowRunSeq += 1;
          this._abortChapterFrameRequests();
          this._collapseExpandedChapterPreview();
        }
        modal.chapters = modal.chapters.filter((item) => item._key !== chapterKey);
      },

      async toggleChapterRailPreviews() {
        const modal = this.chaptersModal;
        if (!modal || !modal.hasVideo || modal.chapters.length === 0) return;

        if (modal.previewsLoading) {
          rt.railRunSeq += 1;
          this._abortChapterFrameRequests();
          modal.previewsLoading = false;
          return;
        }
        if (modal.previewsActive) {
          rt.railRunSeq += 1;
          this._abortChapterFrameRequests();
          this._clearChapterRailThumbUrls();
          return;
        }

        const runId = ++rt.railRunSeq;
        this._abortChapterFrameRequests();
        modal.previewsActive = true;
        modal.previewsLoading = true;
        modal.previewsLoadedCount = 0;
        modal.previewsTotalCount = modal.chapters.length;
        modal.conflictMessage = null;

        const controller = new AbortController();
        rt.abortController = controller;
        const snapshotKeys = modal.chapters.map((row) => row._key);

        for (const rowKey of snapshotKeys) {
          const row = this.chaptersModal.chapters.find((item) => item._key === rowKey);
          if (!row) continue;
          const res = await this._fetchChapterFrameObjectUrl(
            row.start_ms,
            C.SCENE_PICKER.MODE_FAST,
            controller.signal
          );
          if (
            runId !== rt.railRunSeq ||
            res.aborted ||
            res.conflict ||
            !this.chaptersModal
          ) {
            if (runId === rt.railRunSeq && this.chaptersModal) {
              this.chaptersModal.previewsLoading = false;
            }
            return;
          }
          if (res.url) {
            row.thumbUrl = res.url;
          }
          this.chaptersModal.previewsLoadedCount += 1;
        }

        if (runId === rt.railRunSeq && this.chaptersModal) {
          this.chaptersModal.previewsLoading = false;
        }
      },

      toggleChapterDrawer(drawerName) {
        const modal = this.chaptersModal;
        if (!modal) return;
        modal.quickSkipOpen = false;
        modal.menuOpen = false;
        modal.shiftOpen = false;
        const nextDrawer =
          modal.drawer === drawerName ? DRAWER_MAIN : drawerName;
        if (modal.drawer === DRAWER_SCAN_SCENES && nextDrawer !== DRAWER_SCAN_SCENES) {
          this.stopScanScenesGrid();
        }
        modal.drawer = nextDrawer;
        if (
          nextDrawer === DRAWER_SCAN_SCENES &&
          modal.hasVideo &&
          modal.scanTiles.length === 0
        ) {
          this.loadScanScenesGrid();
        }
      },

      selectScanScenesDensity(densityCount) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const parsed = parseInt(String(densityCount || ""), 10);
        if (!C.SCENE_PICKER.SCAN_DENSITY_PRESETS.includes(parsed)) return;
        if (modal.scanDensity === parsed && modal.scanTiles.length > 0) return;

        modal.scanDensity = parsed;
        _saveScanDensity(parsed);
        rt.scanRunSeq += 1;
        this._abortChapterFrameRequests();
        this._clearScanSceneTileUrls();
        modal.scanTiles = [];
        modal.scanSelectedTsList = [];
        modal.scanLoadedCount = 0;
        modal.scanTotalCount = 0;
        this.loadScanScenesGrid();
      },

      async loadScanScenesGrid() {
        const modal = this.chaptersModal;
        if (!modal || !modal.hasVideo) return;
        const runId = ++rt.scanRunSeq;
        this._abortChapterFrameRequests();
        this._clearScanSceneTileUrls();

        const slots = Ch.buildScanSceneSlots(modal.durationMs, modal.scanDensity);
        modal.scanTiles = slots.map((tsMs) => ({
          tsMs,
          timeText: Ch.formatChapterTime(tsMs),
          url: null,
        }));
        modal.scanLoadedCount = 0;
        modal.scanTotalCount = slots.length;
        modal.scanLoading = slots.length > 0;
        modal.conflictMessage = null;

        if (slots.length === 0) return;

        const controller = new AbortController();
        rt.abortController = controller;

        for (let i = 0; i < modal.scanTiles.length; i += 1) {
          const tile = this.chaptersModal.scanTiles[i];
          const res = await this._fetchChapterFrameObjectUrl(
            tile.tsMs,
            C.SCENE_PICKER.MODE_FAST,
            controller.signal
          );
          if (
            runId !== rt.scanRunSeq ||
            res.aborted ||
            res.conflict ||
            !this.chaptersModal
          ) {
            if (runId === rt.scanRunSeq && this.chaptersModal) {
              this.chaptersModal.scanLoading = false;
            }
            return;
          }
          tile.url = res.url;
          this.chaptersModal.scanLoadedCount += 1;
        }

        if (runId === rt.scanRunSeq && this.chaptersModal) {
          this.chaptersModal.scanLoading = false;
        }
      },

      stopScanScenesGrid() {
        rt.scanRunSeq += 1;
        this._abortChapterFrameRequests();
        if (this.chaptersModal) {
          this.chaptersModal.scanLoading = false;
        }
      },

      toggleScanTileSelection(tsMs) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const idx = modal.scanSelectedTsList.indexOf(tsMs);
        if (idx >= 0) {
          modal.scanSelectedTsList.splice(idx, 1);
        } else {
          modal.scanSelectedTsList.push(tsMs);
        }
      },

      addSelectedScanTiles() {
        const modal = this.chaptersModal;
        if (!modal || modal.scanSelectedTsList.length === 0) return;
        this._pushChapterUndoSnapshot();
        const combined = [
          ..._plainEntries(modal.chapters),
          ...modal.scanSelectedTsList.map((tsMs) => ({ start_ms: tsMs, title: "" })),
        ];
        this._clearChapterRailThumbUrls();
        modal.chapters = _toModalRows(modal, Ch.ensureZeroStartChapter(combined));
        modal.scanSelectedTsList = [];
        modal.drawer = DRAWER_MAIN;
      },

      fineTuneScanTile(tsMs) {
        this.stopScanScenesGrid();
        this.openChapterDraftPeek(tsMs);
      },

      applyQuickSkipInterval(intervalMin) {
        const modal = this.chaptersModal;
        if (!modal) return;
        const minutes = Number(intervalMin);
        if (!Number.isFinite(minutes) || minutes <= 0) return;
        const generated = Ch.generateQuickSkipChapters(modal.durationMs, minutes);
        if (generated.length === 0) return;
        this._pushChapterUndoSnapshot();
        this._clearChapterRailThumbUrls();
        this._collapseExpandedChapterPreview();
        modal.chapters = _toModalRows(modal, generated);
        modal.quickSkipOpen = false;
        modal.quickSkipCustomMin = "";
      },

      async onChapterFileSelected(event) {
        const modal = this.chaptersModal;
        const file = event && event.target && event.target.files ? event.target.files[0] : null;
        if (!modal || !file) return;
        if (file.size > C.CHAPTERS.MAX_UPLOAD_BYTES) {
          modal.pasteError = "Chapter file is too large (max 512 KB).";
          return;
        }
        try {
          modal.pasteText = await file.text();
          modal.pasteFilename = file.name || null;
          modal.pasteError = null;
        } catch (_err) {
          modal.pasteError = "Could not read selected file.";
        }
      },

      async parseAndApplyPastedChapters(mode = "replace") {
        const modal = this.chaptersModal;
        if (!modal || modal.pasteBusy) return;
        const text = String(modal.pasteText || "").trim();
        if (!text) {
          modal.pasteError = "Paste chapter timestamps or choose a file first.";
          return;
        }
        modal.pasteBusy = true;
        modal.pasteError = null;
        modal.pasteWarnings = [];
        try {
          const res = await Api.parseFileChapters(
            modal.fileId,
            text,
            modal.pasteFilename
          );
          if (!this.chaptersModal) return;
          const parsed = Array.isArray(res.chapters) ? res.chapters : [];
          modal.pasteWarnings = Array.isArray(res.warnings) ? res.warnings : [];
          if (parsed.length === 0) {
            modal.pasteError = "No valid chapters found in the provided text.";
            return;
          }
          this._pushChapterUndoSnapshot();
          this._clearChapterRailThumbUrls();
          this._collapseExpandedChapterPreview();
          const combined =
            mode === "append"
              ? [..._plainEntries(modal.chapters), ...parsed]
              : parsed;
          modal.chapters = _toModalRows(modal, Ch.ensureZeroStartChapter(combined));
          modal.drawer = DRAWER_MAIN;
          modal.pasteText = "";
          modal.pasteFilename = null;
        } catch (err) {
          if (this.chaptersModal) {
            this.chaptersModal.pasteError =
              (err && err.message) || "Failed to parse chapters.";
          }
        } finally {
          if (this.chaptersModal) {
            this.chaptersModal.pasteBusy = false;
          }
        }
      },

      renumberChapters() {
        const modal = this.chaptersModal;
        if (!modal || modal.chapters.length === 0) return;
        this._pushChapterUndoSnapshot();
        const renumbered = Ch.renumberGenericChapters(_plainEntries(modal.chapters));
        this._clearChapterRailThumbUrls();
        modal.chapters = _toModalRows(modal, renumbered);
        modal.menuOpen = false;
      },

      applyShiftAllChapters() {
        const modal = this.chaptersModal;
        if (!modal || modal.chapters.length === 0) return;
        const sec = Number(modal.shiftDraftSec);
        if (!Number.isFinite(sec) || sec === 0) return;
        this._pushChapterUndoSnapshot();
        const shifted = Ch.shiftAllChapters(
          _plainEntries(modal.chapters),
          Math.round(sec * C.UNITS.MS_PER_SEC),
          modal.durationMs
        );
        this._clearChapterRailThumbUrls();
        modal.chapters = _toModalRows(modal, shifted);
        modal.shiftOpen = false;
        modal.shiftDraftSec = "";
      },

      exportChaptersFile(format) {
        const modal = this.chaptersModal;
        if (!modal || modal.chapters.length === 0) return;
        const stem = Ed.stripFileExtension(modal.filename || "chapters") || "chapters";
        const entries = _plainEntries(modal.chapters);
        if (format === "xml") {
          const lang =
            (this.userSettings && this.userSettings.preferred_language) ||
            C.CHAPTERS.DEFAULT_LANGUAGE;
          _triggerBlobDownload(
            Ch.exportChaptersAsXml(entries, lang),
            `${stem}.chapters.xml`,
            "application/xml;charset=utf-8"
          );
        } else {
          _triggerBlobDownload(
            Ch.exportChaptersAsOggText(entries),
            `${stem}.chapters.txt`,
            "text/plain;charset=utf-8"
          );
        }
        modal.menuOpen = false;
      },

      resetChaptersToOriginal() {
        const modal = this.chaptersModal;
        if (!modal || !this.editor) return;
        this._pushChapterUndoSnapshot();
        this._clearChapterRailThumbUrls();
        this._collapseExpandedChapterPreview();
        const originals = Array.isArray(this.editor.originalChapters)
          ? this.editor.originalChapters
          : [];
        modal.chapters = _toModalRows(modal, originals);
        modal.menuOpen = false;
      },

      clearAllChapters() {
        const modal = this.chaptersModal;
        if (!modal) return;
        this._pushChapterUndoSnapshot();
        this._abortChapterFrameRequests();
        this._clearChapterRailThumbUrls();
        this._collapseExpandedChapterPreview();
        modal.chapters = [];
        modal.menuOpen = false;
      },
    };
  }

  global.MKVForgeChaptersController = Object.freeze({
    createMethods,
  });
})(window);
