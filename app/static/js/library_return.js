/* Library page & scroll position origin manager for MKV Forge. */

(function attachLibraryReturn(global) {
  const C = global.MKVForgeConstants;

  const DEFAULT_PAGE = (C && C.PAGINATION && C.PAGINATION.DEFAULT_PAGE) || 1;
  const VIEW_DIR = (C && C.LIBRARY_VIEWS && C.LIBRARY_VIEWS.DIR) || "dir";
  const VIEW_LIST = (C && C.LIBRARY_VIEWS && C.LIBRARY_VIEWS.LIST) || "list";
  const ROOT_PATH = (C && C.FOLDER_TREE && C.FOLDER_TREE.ROOT_PATH) || "/";
  const STORAGE_KEY =
    (C && C.STORAGE_KEYS && C.STORAGE_KEYS.LIBRARY_RETURN_STATE) ||
    "mkvforge.libraryReturnState";
  const MIN_OFFSET_PX =
    (C && C.SCROLL_RESTORE && C.SCROLL_RESTORE.MIN_OFFSET_PX) || 0;
  const LIBRARY_HASH_PREFIX = "#/library";

  function normalizeOffset(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num < MIN_OFFSET_PX) {
      return MIN_OFFSET_PX;
    }
    return Math.round(num);
  }

  function normalizeHash(rawHash) {
    if (typeof rawHash !== "string") return "";
    const trimmed = rawHash.trim();
    if (!trimmed) return "";
    if (trimmed.startsWith("#/")) return trimmed;
    if (trimmed.startsWith("#")) return `#/${trimmed.slice(1)}`;
    if (trimmed.startsWith("/")) return `#${trimmed}`;
    return `#/${trimmed}`;
  }

  function isLibraryHash(rawHash) {
    const normalized = normalizeHash(rawHash);
    return (
      normalized === LIBRARY_HASH_PREFIX ||
      normalized.startsWith(`${LIBRARY_HASH_PREFIX}?`)
    );
  }

  function hashesMatch(hashA, hashB) {
    const a = normalizeHash(hashA);
    const b = normalizeHash(hashB);
    if (!a || !b) return false;
    if (a === b) return true;
    const [pathA, queryA = ""] = a.split("?");
    const [pathB, queryB = ""] = b.split("?");
    if (pathA !== pathB) return false;
    const paramsA = new URLSearchParams(queryA);
    const paramsB = new URLSearchParams(queryB);
    const keys = new Set([...paramsA.keys(), ...paramsB.keys()]);
    for (const key of keys) {
      if ((paramsA.get(key) || "") !== (paramsB.get(key) || "")) {
        return false;
      }
    }
    return true;
  }

  function createOriginSnapshot(input) {
    if (!input || typeof input !== "object") return null;
    const hash = normalizeHash(input.hash);
    if (!isLibraryHash(hash)) return null;

    const parsedPage = parseInt(String(input.page ?? ""), 10);
    const page =
      Number.isFinite(parsedPage) && parsedPage >= DEFAULT_PAGE
        ? parsedPage
        : DEFAULT_PAGE;

    const view = input.view === VIEW_LIST ? VIEW_LIST : VIEW_DIR;
    const folder =
      typeof input.folder === "string" && input.folder.trim()
        ? input.folder.trim()
        : ROOT_PATH;

    const rawFileId = input.fileId ?? input.file_id ?? null;
    const parsedFileId =
      rawFileId === null || rawFileId === ""
        ? null
        : parseInt(String(rawFileId), 10);
    const fileId =
      parsedFileId !== null && Number.isFinite(parsedFileId) && parsedFileId > 0
        ? parsedFileId
        : null;

    return {
      hash,
      page,
      view,
      folder,
      scrollTop: normalizeOffset(input.scrollTop),
      windowScrollY: normalizeOffset(input.windowScrollY),
      fileId,
    };
  }

  function _resolveStorage(storageOverride) {
    if (storageOverride !== undefined) return storageOverride;
    try {
      return global.sessionStorage || null;
    } catch (_err) {
      return null;
    }
  }

  function saveOrigin(snapshot, storageOverride) {
    const valid = createOriginSnapshot(snapshot);
    const storage = _resolveStorage(storageOverride);
    if (!valid) {
      clearOrigin(storage);
      return null;
    }
    if (storage && typeof storage.setItem === "function") {
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify(valid));
      } catch (_err) {
        // Ignore storage quota / privacy mode errors
      }
    }
    return valid;
  }

  function loadOrigin(storageOverride) {
    const storage = _resolveStorage(storageOverride);
    if (!storage || typeof storage.getItem !== "function") return null;
    try {
      const raw = storage.getItem(STORAGE_KEY);
      if (!raw) return null;
      return createOriginSnapshot(JSON.parse(raw));
    } catch (_err) {
      return null;
    }
  }

  function clearOrigin(storageOverride) {
    const storage = _resolveStorage(storageOverride);
    if (!storage || typeof storage.removeItem !== "function") return;
    try {
      storage.removeItem(STORAGE_KEY);
    } catch (_err) {
      // Ignore storage errors
    }
  }

  function applyScrollPosition({
    container = null,
    win = null,
    scrollTop = MIN_OFFSET_PX,
    windowScrollY = MIN_OFFSET_PX,
  } = {}) {
    const targetContainerTop = normalizeOffset(scrollTop);
    const targetWindowY = normalizeOffset(windowScrollY);
    let appliedContainer = false;
    let appliedWindow = false;
    let needsRetry = false;

    if (container && typeof container === "object") {
      const scrollHeight = Number(container.scrollHeight || 0);
      const clientHeight = Number(container.clientHeight || 0);
      const maxScrollable = Math.max(MIN_OFFSET_PX, scrollHeight - clientHeight);
      if (targetContainerTop > MIN_OFFSET_PX && maxScrollable <= MIN_OFFSET_PX) {
        needsRetry = true;
      } else {
        const clampedTop = Math.min(targetContainerTop, maxScrollable);
        if (typeof container.scrollTo === "function") {
          try {
            container.scrollTo({ top: clampedTop, behavior: "instant" });
          } catch (_err) {
            container.scrollTop = clampedTop;
          }
        } else {
          container.scrollTop = clampedTop;
        }
        appliedContainer = true;
      }
    }

    if (win && typeof win.scrollTo === "function") {
      try {
        win.scrollTo({ top: targetWindowY, behavior: "instant" });
      } catch (_err) {
        win.scrollTo(MIN_OFFSET_PX, targetWindowY);
      }
      appliedWindow = true;
    }

    return { appliedContainer, appliedWindow, needsRetry };
  }

  const exported = Object.freeze({
    normalizeOffset,
    normalizeHash,
    isLibraryHash,
    hashesMatch,
    createOriginSnapshot,
    saveOrigin,
    loadOrigin,
    clearOrigin,
    applyScrollPosition,
  });

  global.MKVForgeLibraryReturn = exported;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  }
})(typeof window !== "undefined" ? window : globalThis);
