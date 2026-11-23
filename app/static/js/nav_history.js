/* Mobile-first browser Back-gesture & folder history manager for MKV Forge. */

(function attachNavHistory(global) {
  const C = global.MKVForgeConstants || {};
  const NAV = C.NAV_HISTORY || {
    MOBILE_MAX_WIDTH_PX: 767,
    STATE_KEY: "__mkvforgeNav",
    KIND_ROUTE: "route",
    KIND_OVERLAY: "overlay",
    LAYERS: {},
    LAYER_ORDER: [],
  };
  const ROOT_PATH = (C.FOLDER_TREE && C.FOLDER_TREE.ROOT_PATH) || "/";

  function isMobileViewport(win = global) {
    if (!win || typeof win !== "object") return false;
    const width = Number(win.innerWidth);
    return Number.isFinite(width) && width > 0 && width <= NAV.MOBILE_MAX_WIDTH_PX;
  }

  function getFolderAncestorPaths(rawPath, rootPath = ROOT_PATH) {
    const root = typeof rootPath === "string" && rootPath.trim() ? rootPath.trim() : "/";
    if (typeof rawPath !== "string") return [root];
    const trimmed = rawPath.trim();
    if (!trimmed || trimmed === root) return [root];

    const parts = trimmed
      .split("/")
      .map((seg) => seg.trim())
      .filter((seg) => seg.length > 0 && seg !== ".");

    if (parts.length === 0) return [root];

    const ancestors = [root];
    let acc = "";
    for (const part of parts) {
      acc += `/${part}`;
      ancestors.push(acc);
    }
    return ancestors;
  }

  function createRouteState() {
    return {
      [NAV.STATE_KEY]: {
        kind: NAV.KIND_ROUTE,
      },
    };
  }

  function createOverlayState(layerId, layerKey) {
    return {
      [NAV.STATE_KEY]: {
        kind: NAV.KIND_OVERLAY,
        layerId: Number(layerId),
        layerKey: String(layerKey || ""),
      },
    };
  }

  function readNavState(state) {
    if (!state || typeof state !== "object") return null;
    const meta = state[NAV.STATE_KEY];
    if (!meta || typeof meta !== "object") return null;
    return meta;
  }

  function _canPushState(win) {
    return Boolean(win && win.history && typeof win.history.pushState === "function");
  }

  function _canReplaceState(win) {
    return Boolean(win && win.history && typeof win.history.replaceState === "function");
  }

  function _canHistoryGo(win) {
    return Boolean(win && win.history && typeof win.history.go === "function");
  }

  function _currentHash(win) {
    if (!win || !win.location || typeof win.location.hash !== "string") return "";
    return win.location.hash;
  }

  function createMobileNavManager({ win = global } = {}) {
    let nextLayerId = 1;
    let pushedLayers = []; // Array<{ id: number, key: string, stale: boolean }>
    let pendingProgrammaticPops = 0;
    let pendingRouteNav = null; // { hash: string, onApplied?: Function } | null

    function _activeIndexById(layerId) {
      for (let i = 0; i < pushedLayers.length; i += 1) {
        if (pushedLayers[i].id === layerId) return i;
      }
      return -1;
    }

    function _hasKey(key) {
      for (let i = 0; i < pushedLayers.length; i += 1) {
        if (!pushedLayers[i].stale && pushedLayers[i].key === key) return true;
      }
      return false;
    }

    function _unwindTrailingStaleOrRemoved(activeSet) {
      // Mark any non-active entries stale first
      for (let i = 0; i < pushedLayers.length; i += 1) {
        if (!activeSet.has(pushedLayers[i].key)) {
          pushedLayers[i].stale = true;
        }
      }
      // Count contiguous stale entries at the top of the stack
      let popCount = 0;
      while (pushedLayers.length > 0 && pushedLayers[pushedLayers.length - 1].stale) {
        pushedLayers.pop();
        popCount += 1;
      }
      if (popCount > 0 && _canHistoryGo(win)) {
        pendingProgrammaticPops += 1;
        win.history.go(-popCount);
      }
    }

    function reconcile(activeKeys) {
      const normalized = Array.isArray(activeKeys)
        ? activeKeys
            .map((k) => String(k || "").trim())
            .filter((k) => k.length > 0)
        : [];
      const activeSet = new Set(normalized);

      // Always unwind any already-pushed layers that closed (even if viewport resized)
      _unwindTrailingStaleOrRemoved(activeSet);

      // Only push new transient overlay sentinels on mobile viewports
      if (!isMobileViewport(win) || !_canPushState(win)) {
        return;
      }

      const hash = _currentHash(win);
      for (const key of normalized) {
        if (_hasKey(key)) continue;
        const id = nextLayerId;
        nextLayerId += 1;
        pushedLayers.push({ id, key, stale: false });
        win.history.pushState(createOverlayState(id, key), "", hash);
      }
    }

    function handlePopState(event) {
      // 1. Finish any deferred multi-overlay route replacement cleanly
      if (pendingRouteNav) {
        const target = pendingRouteNav;
        pendingRouteNav = null;
        if (_canReplaceState(win)) {
          win.history.replaceState(createRouteState(), "", target.hash);
        } else if (win && win.location) {
          win.location.hash = target.hash;
        }
        if (typeof target.onApplied === "function") {
          target.onApplied(target.hash);
        }
        return {
          handled: true,
          dismissedKeys: [],
          ignoredProgrammatic: false,
          completedDeferredRoute: true,
        };
      }

      // 2. Ignore programmatic history.go(-k) triggered by UI button close
      if (pendingProgrammaticPops > 0) {
        pendingProgrammaticPops -= 1;
        return {
          handled: true,
          dismissedKeys: [],
          ignoredProgrammatic: true,
          completedDeferredRoute: false,
        };
      }

      const meta = readNavState(event ? event.state : null);

      // 3. If our stack has pushed overlay layers, determine which layer(s) were popped
      if (pushedLayers.length > 0) {
        let targetKeepCount = 0;
        if (meta && meta.kind === NAV.KIND_OVERLAY) {
          const idx = _activeIndexById(meta.layerId);
          if (idx >= 0) {
            targetKeepCount = idx + 1;
          } else {
            // Unknown overlay state -> conservatively pop only top layer
            targetKeepCount = pushedLayers.length - 1;
          }
        } else {
          // Landed back on a route entry -> all pushed layers above it were popped
          targetKeepCount = 0;
        }

        if (targetKeepCount < pushedLayers.length) {
          const popped = pushedLayers.slice(targetKeepCount);
          pushedLayers = pushedLayers.slice(0, targetKeepCount);

          // Collect non-stale keys in top-to-bottom order
          const dismissedKeys = [];
          for (let i = popped.length - 1; i >= 0; i -= 1) {
            if (!popped[i].stale) {
              dismissedKeys.push(popped[i].key);
            }
          }

          // Auto-skip any stale layers now exposed on top of the remaining stack
          let trailingStale = 0;
          while (
            pushedLayers.length > 0 &&
            pushedLayers[pushedLayers.length - 1].stale
          ) {
            pushedLayers.pop();
            trailingStale += 1;
          }
          if (trailingStale > 0 && _canHistoryGo(win)) {
            pendingProgrammaticPops += 1;
            win.history.go(-trailingStale);
          }

          return {
            handled: true,
            dismissedKeys,
            ignoredProgrammatic: false,
            completedDeferredRoute: false,
          };
        }
      }

      // 4. Stale Forward navigation onto an old overlay entry when no overlays are open
      if (meta && meta.kind === NAV.KIND_OVERLAY) {
        if (_canReplaceState(win)) {
          win.history.replaceState(createRouteState(), "", _currentHash(win));
        }
        return {
          handled: true,
          dismissedKeys: [],
          ignoredProgrammatic: false,
          completedDeferredRoute: false,
        };
      }

      // 5. Regular route history traversal (Back/Forward between folders or pages)
      return {
        handled: false,
        dismissedKeys: [],
        ignoredProgrammatic: false,
        completedDeferredRoute: false,
      };
    }

    function navigateHash(nextHash, { replace = false, onApplied = null } = {}) {
      if (typeof nextHash !== "string" || !nextHash) return false;

      // Consume any active overlay sentinels cleanly so route nav leaves zero orphans
      if (pushedLayers.length > 0) {
        const depth = pushedLayers.length;
        pushedLayers = [];
        if (depth === 1 || !_canHistoryGo(win)) {
          if (_canReplaceState(win)) {
            win.history.replaceState(createRouteState(), "", nextHash);
          } else if (win && win.location) {
            win.location.hash = nextHash;
          }
          if (typeof onApplied === "function") onApplied(nextHash);
          return true;
        }
        // Unwind depth - 1 entries asynchronously, then replace the final sentinel with nextHash
        pendingRouteNav = { hash: nextHash, onApplied };
        win.history.go(-(depth - 1));
        return true;
      }

      const curHash = _currentHash(win);
      if (curHash === nextHash) {
        if (typeof onApplied === "function") onApplied(nextHash);
        return false;
      }

      if (replace) {
        if (_canReplaceState(win)) {
          win.history.replaceState(createRouteState(), "", nextHash);
        } else if (win && win.location) {
          win.location.hash = nextHash;
        }
      } else if (_canPushState(win)) {
        win.history.pushState(createRouteState(), "", nextHash);
      } else if (win && win.location) {
        win.location.hash = nextHash;
      }

      if (typeof onApplied === "function") onApplied(nextHash);
      return true;
    }

    function seedFolderAncestors(ancestorHashes, { replaceFirst = false } = {}) {
      if (!isMobileViewport(win) || !_canPushState(win)) return false;
      if (!Array.isArray(ancestorHashes) || ancestorHashes.length < 2) return false;

      const cleaned = ancestorHashes.filter((h) => typeof h === "string" && h.length > 0);
      if (cleaned.length < 2) return false;

      // Drop any stale overlay sentinel slot first by replacing it with the first ancestor
      const consumeFirst = replaceFirst || pushedLayers.length > 0;
      pushedLayers = [];

      for (let i = 0; i < cleaned.length; i += 1) {
        const hash = cleaned[i];
        if (i === 0 && consumeFirst && _canReplaceState(win)) {
          win.history.replaceState(createRouteState(), "", hash);
        } else {
          win.history.pushState(createRouteState(), "", hash);
        }
      }
      return true;
    }

    function getDepth() {
      return pushedLayers.length;
    }

    function getPushedKeys() {
      return pushedLayers.filter((e) => !e.stale).map((e) => e.key);
    }

    return Object.freeze({
      reconcile,
      handlePopState,
      navigateHash,
      seedFolderAncestors,
      getDepth,
      getPushedKeys,
    });
  }

  global.MKVForgeNavHistory = Object.freeze({
    isMobileViewport,
    getFolderAncestorPaths,
    createRouteState,
    createOverlayState,
    readNavState,
    createMobileNavManager,
  });
})(window);
