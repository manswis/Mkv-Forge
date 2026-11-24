/* Thin HTTP API client and auto-reconnecting SSE manager for MKV Forge. */

(function attachApiClient(global) {
  const { API, TIMINGS_MS } = global.MKVForgeConstants;

  const AUTH_ENDPOINTS_PREFIX = `${API.AUTH}/`;
  let _unauthorizedHandler = null;

  function setUnauthorizedHandler(fn) {
    _unauthorizedHandler = typeof fn === "function" ? fn : null;
  }

  class ApiError extends Error {
    constructor(message, status, detail) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.detail = detail;
    }
  }

  async function requestJson(url, options = {}) {
    let controller = null;
    let timeoutId = null;
    let timedOut = false;
    const externalSignal = options.signal || null;
    const fetchOptions = {
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
      ...options,
    };

    if (typeof AbortController === "function") {
      controller = new AbortController();
      fetchOptions.signal = controller.signal;
      if (externalSignal) {
        if (externalSignal.aborted) {
          controller.abort();
        } else {
          externalSignal.addEventListener("abort", () => controller.abort(), {
            once: true,
          });
        }
      }
      timeoutId = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, TIMINGS_MS.REQUEST_TIMEOUT);
    }

    let response;
    try {
      response = await fetch(url, fetchOptions);
    } catch (err) {
      if (err && err.name === "AbortError") {
        if (timedOut) {
          throw new ApiError("Request timed out. Click Retry to refresh.", 0, "timeout");
        }
        throw new ApiError("Request cancelled", 0, "aborted");
      }
      throw err;
    } finally {
      if (timeoutId !== null) clearTimeout(timeoutId);
    }

    let body = null;
    try {
      body = await response.json();
    } catch (_err) {
      body = null;
    }

    if (!response.ok) {
      const detail =
        body && typeof body.detail === "string"
          ? body.detail
          : `Request failed (${response.status})`;
      const err = new ApiError(detail, response.status, detail);
      if (
        response.status === 401 &&
        typeof url === "string" &&
        !url.startsWith(AUTH_ENDPOINTS_PREFIX) &&
        typeof _unauthorizedHandler === "function"
      ) {
        try {
          _unauthorizedHandler(err, url);
        } catch (_handlerErr) {
          // Never mask original ApiError
        }
      }
      throw err;
    }
    return body;
  }

  function buildFilesQuery(params) {
    const qs = new URLSearchParams();
    if (params.q && params.q.trim()) qs.set("q", params.q.trim());
    if (params.folder && params.folder.trim()) qs.set("folder", params.folder.trim());
    if (params.audio_type && params.audio_type.trim()) {
      qs.set("audio_type", params.audio_type.trim());
    }
    if (params.audio_codec && params.audio_codec.trim()) {
      qs.set("audio_codec", params.audio_codec.trim());
    }
    if (params.video_codec && params.video_codec.trim()) {
      qs.set("video_codec", params.video_codec.trim());
    }
    if (params.resolution && params.resolution.trim()) {
      qs.set("resolution", params.resolution.trim());
    }
    if (params.dynamic_range && params.dynamic_range.trim()) {
      qs.set("dynamic_range", params.dynamic_range.trim());
    }
    if (params.audio_channels && params.audio_channels.trim()) {
      qs.set("audio_channels", params.audio_channels.trim());
    }
    if (params.audio_language && params.audio_language.trim()) {
      qs.set("audio_language", params.audio_language.trim());
    }
    if (params.no_subtitles) qs.set("no_subtitles", "true");
    if (params.aac51_only) qs.set("aac51_only", "true");
    if (params.aac51) qs.set("aac51", "true");
    if (params.has_metadata) qs.set("has_metadata", "true");
    if (params.no_dolby) qs.set("no_dolby", "true");
    if (params.needs_conversion) qs.set("needs_conversion", "true");
    if (params.sort) qs.set("sort", params.sort);
    if (params.order) qs.set("order", params.order);
    if (params.page) qs.set("page", String(params.page));
    if (params.limit) qs.set("limit", String(params.limit));
    const encoded = qs.toString();
    return encoded ? `${API.FILES}?${encoded}` : API.FILES;
  }

  class ManagedEventSource {
    constructor(url, eventNameOrHandlers, onMessage) {
      this._url = url;
      if (typeof eventNameOrHandlers === "string") {
        this._handlers = { [eventNameOrHandlers]: onMessage };
      } else {
        this._handlers = eventNameOrHandlers || {};
      }
      this._source = null;
      this._retryTimer = null;
      this._retryDelay = TIMINGS_MS.SSE_RECONNECT_BASE;
      this._closed = false;
    }

    connect() {
      this._closed = false;
      this._clearRetryTimer();
      this._closeCurrent();

      const source = new EventSource(this._url);
      this._source = source;

      source.addEventListener("open", () => {
        this._retryDelay = TIMINGS_MS.SSE_RECONNECT_BASE;
      });

      Object.entries(this._handlers).forEach(([eventName, handler]) => {
        if (typeof handler !== "function") return;
        source.addEventListener(eventName, (event) => {
          try {
            const parsed = JSON.parse(event.data);
            handler(parsed);
          } catch (_err) {
            // Ignore malformed frames cleanly
          }
        });
      });

      source.addEventListener("error", () => {
        if (this._closed) return;
        if (source.readyState === EventSource.CLOSED) {
          this._scheduleReconnect();
        }
      });
    }

    ensureConnected() {
      if (this._closed) return;
      if (
        !this._source ||
        this._source.readyState === EventSource.CLOSED
      ) {
        this._retryDelay = TIMINGS_MS.SSE_RECONNECT_BASE;
        this.connect();
      }
    }

    forceReconnect() {
      if (this._closed) return;
      this._retryDelay = TIMINGS_MS.SSE_RECONNECT_BASE;
      this.connect();
    }

    close() {
      this._closed = true;
      this._clearRetryTimer();
      this._closeCurrent();
    }

    _closeCurrent() {
      if (this._source) {
        try {
          this._source.close();
        } catch (_err) {
          // Safe cleanup
        }
        this._source = null;
      }
    }

    _clearRetryTimer() {
      if (this._retryTimer !== null) {
        clearTimeout(this._retryTimer);
        this._retryTimer = null;
      }
    }

    _scheduleReconnect() {
      if (this._retryTimer !== null || this._closed) return;
      const delay = this._retryDelay;
      this._retryDelay = Math.min(
        this._retryDelay * 2,
        TIMINGS_MS.SSE_RECONNECT_MAX
      );
      this._retryTimer = setTimeout(() => {
        this._retryTimer = null;
        if (!this._closed) {
          this.connect();
        }
      }, delay);
    }
  }

  global.MKVForgeApi = Object.freeze({
    ApiError,
    ManagedEventSource,
    setUnauthorizedHandler,
    getHealth: () => requestJson(API.HEALTH),
    getDashboard: () => requestJson(API.DASHBOARD),
    listFiles: (params = {}, options = {}) =>
      requestJson(buildFilesQuery(params), options),
    listFolders: (path = "/", options = {}) => {
      const qs = new URLSearchParams({ path: path || "/" });
      if (options && options.includeFiles) qs.set("include_files", "true");
      if (options && options.kind) qs.set("kind", options.kind);
      return requestJson(`${API.FOLDERS}?${qs.toString()}`);
    },
    createFolder: (parent, name) =>
      requestJson(API.FOLDERS, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent: parent || "/", name }),
      }),
    renameFolder: (path, newName) =>
      requestJson(`${API.FOLDERS}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, new_name: newName }),
      }),
    moveFolder: (path, destinationFolder) =>
      requestJson(`${API.FOLDERS}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, destination_folder: destinationFolder }),
      }),
    deleteFolder: (path, recursive = false) =>
      requestJson(`${API.FOLDERS}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, recursive: Boolean(recursive) }),
      }),
    renameFile: (fileId, newName) =>
      requestJson(`${API.FILES}/${encodeURIComponent(fileId)}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: newName }),
      }),
    duplicateFile: (fileId) =>
      requestJson(`${API.FILES}/${encodeURIComponent(fileId)}/duplicate`, {
        method: "POST",
      }),
    moveFiles: (fileIds, destinationFolder) =>
      requestJson(`${API.FILES}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_ids: fileIds,
          destination_folder: destinationFolder,
        }),
      }),
    deleteFiles: (fileIds) =>
      requestJson(`${API.FILES}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: fileIds }),
      }),
    renameOtherFile: (filepath, newName) =>
      requestJson(`${API.FILES}/other/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filepath, new_name: newName }),
      }),
    moveOtherFile: (filepath, destinationFolder) =>
      requestJson(`${API.FILES}/other/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filepath, destination_folder: destinationFolder }),
      }),
    deleteOtherFile: (filepath) =>
      requestJson(`${API.FILES}/other/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filepath }),
      }),
    getFileDetail: (fileId) => requestJson(`${API.FILES}/${encodeURIComponent(fileId)}`),
    getFileInfo: (fileId) => requestJson(`${API.FILES}/${encodeURIComponent(fileId)}/info`),
    rescanFile: (fileId) =>
      requestJson(`${API.FILES}/${encodeURIComponent(fileId)}/rescan`, {
        method: "POST",
      }),
    triggerScan: (full = false) =>
      requestJson(`${API.SCAN}?full=${full ? "true" : "false"}`, {
        method: "POST",
      }),
    getJobs: () => requestJson(API.JOBS),
    createJob: (payload) =>
      requestJson(API.JOBS, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    cancelJob: (jobId) =>
      requestJson(`${API.JOBS}/${encodeURIComponent(jobId)}`, {
        method: "DELETE",
      }),
    deleteJobHistoryItem: (jobId) =>
      requestJson(`${API.JOBS}/history/${encodeURIComponent(jobId)}`, {
        method: "DELETE",
      }),
    clearJobHistory: () =>
      requestJson(`${API.JOBS}/history`, {
        method: "DELETE",
      }),
    previewBatchJobs: (payload) =>
      requestJson(API.JOBS_BATCH_PREVIEW, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    enqueueBatchJobs: (payload) =>
      requestJson(API.JOBS_BATCH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    listTemplates: () => requestJson(API.TEMPLATES),
    createTemplate: (payload) =>
      requestJson(API.TEMPLATES, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    updateTemplate: (templateId, payload) =>
      requestJson(`${API.TEMPLATES}/${encodeURIComponent(templateId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    deleteTemplate: (templateId) =>
      requestJson(`${API.TEMPLATES}/${encodeURIComponent(templateId)}`, {
        method: "DELETE",
      }),
    getSettings: () => requestJson(API.SETTINGS),
    getSettingsRuntime: () => requestJson(API.SETTINGS_RUNTIME),
    updateSettings: (patch) =>
      requestJson(API.SETTINGS, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch || {}),
      }),
    getMaintenanceStatus: () => requestJson(`${API.MAINTENANCE}/status`),
    runMaintenanceAction: (action) =>
      requestJson(`${API.MAINTENANCE}/${encodeURIComponent(action)}`, {
        method: "POST",
      }),
    factoryReset: (payload) =>
      requestJson(API.MAINTENANCE_FACTORY_RESET, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    getAuthStatus: () => requestJson(`${API.AUTH}/status`),
    setupAuth: (payload) =>
      requestJson(`${API.AUTH}/setup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    login: (username, password) =>
      requestJson(`${API.AUTH}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      }),
    logout: () =>
      requestJson(`${API.AUTH}/logout`, {
        method: "POST",
      }),
    getAuthUsers: () => requestJson(`${API.AUTH}/users`),
    createAuthUser: (payload) =>
      requestJson(`${API.AUTH}/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      }),
    changePassword: (currentPassword, newPassword) =>
      requestJson(`${API.AUTH}/password`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      }),
    deleteAuthUser: (userId) =>
      requestJson(`${API.AUTH}/users/${encodeURIComponent(userId)}`, {
        method: "DELETE",
      }),
    disableAuth: (password) =>
      requestJson(`${API.AUTH}/disable`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      }),
    getApiKeys: () => requestJson(`${API.AUTH}/api-keys`),
    createApiKey: (name) =>
      requestJson(`${API.AUTH}/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name || "Default" }),
      }),
    deleteApiKey: (keyId) =>
      requestJson(`${API.AUTH}/api-keys/${encodeURIComponent(keyId)}`, {
        method: "DELETE",
      }),
    uploadExternalTrack: (kind, file, onProgress = null) =>
      new Promise((resolve, reject) => {
        if (!file) {
          reject(new Error("No file selected."));
          return;
        }
        const qs = new URLSearchParams({
          kind: String(kind || ""),
          filename: String(file.name || "track"),
        });
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `${API.UPLOADS_TRACK}?${qs.toString()}`);
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("Accept", "application/json");

        xhr.upload.onprogress = (evt) => {
          if (typeof onProgress === "function" && evt.lengthComputable && evt.total > 0) {
            const pct = Math.min(100, Math.round((evt.loaded / evt.total) * 100));
            onProgress(pct, evt.loaded, evt.total);
          }
        };

        xhr.onload = () => {
          let payload = null;
          try {
            payload = JSON.parse(xhr.responseText || "null");
          } catch (_err) {
            payload = null;
          }
          if (xhr.status >= 200 && xhr.status < 300 && payload) {
            resolve(payload);
            return;
          }
          let message = `Upload failed (HTTP ${xhr.status})`;
          if (payload && typeof payload.detail === "string") {
            message = payload.detail;
          } else if (xhr.status === 413) {
            message =
              "File exceeds upload size limit (check reverse proxy client_max_body_size if behind Nginx Proxy Manager).";
          }
          const err = new Error(message);
          err.status = xhr.status;
          reject(err);
        };

        xhr.onerror = () => {
          reject(new Error("Network error while uploading file."));
        };

        xhr.send(file);
      }),
    deleteUploadedTrack: (uploadId) =>
      requestJson(`${API.UPLOADS}/${encodeURIComponent(uploadId)}`, {
        method: "DELETE",
      }),
    generateTestMedia: (payload) =>
      requestJson(API.TOOLS_GENERATE_TEST_MEDIA, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
        timeoutMs: TIMINGS_MS.TEST_MEDIA_REQUEST_TIMEOUT,
      }),
  });
})(window);
