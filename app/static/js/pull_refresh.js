/* Pure mobile pull-to-refresh gesture math and eligibility guards for MKV Forge. */

(function attachPullRefreshHelpers(global) {
  const { PULL_REFRESH } = global.MKVForgeConstants;
  const BLOCKED_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT", "OPTION"]);

  function createPullRefreshState() {
    return {
      tracking: false,
      pulling: false,
      refreshing: false,
      startX: 0,
      startY: 0,
      distancePx: 0,
      progress: 0,
      ready: false,
      rotationDeg: 0,
    };
  }

  function isInteractiveOrScrollableTarget(target) {
    let node = target;
    while (node && typeof node === "object") {
      const tagName = String(node.tagName || "").toUpperCase();
      if (BLOCKED_TAGS.has(tagName)) {
        return true;
      }
      if (
        typeof node.getAttribute === "function" &&
        node.getAttribute("data-no-pull-refresh") !== null
      ) {
        return true;
      }
      if (typeof node.scrollTop === "number" && node.scrollTop > 0) {
        return true;
      }
      if (
        typeof global.getComputedStyle === "function" &&
        node.nodeType === 1 &&
        typeof node.scrollWidth === "number" &&
        typeof node.clientWidth === "number" &&
        node.scrollWidth > node.clientWidth + 1
      ) {
        const overflowX = global.getComputedStyle(node).overflowX;
        if (overflowX === "auto" || overflowX === "scroll") {
          return true;
        }
      }
      node = node.parentElement || null;
    }
    return false;
  }

  function shouldStartPull(
    {
      viewportWidth,
      scrollY,
      touchCount,
      hasOpenOverlay,
      isBlockedTarget,
      isRefreshing,
    },
    cfg = PULL_REFRESH
  ) {
    if (isRefreshing || hasOpenOverlay || isBlockedTarget) return false;
    if (Number(touchCount) !== 1) return false;
    if (!Number.isFinite(Number(viewportWidth)) || Number(viewportWidth) > cfg.MOBILE_MAX_WIDTH_PX) {
      return false;
    }
    if (Number(scrollY || 0) > 0) return false;
    return true;
  }

  function shouldCancelForHorizontal(deltaX, deltaY) {
    return Math.abs(Number(deltaX || 0)) > Math.abs(Number(deltaY || 0));
  }

  function computePullMetrics(deltaY, cfg = PULL_REFRESH) {
    const raw = Number(deltaY);
    if (!Number.isFinite(raw) || raw <= 0) {
      return {
        distancePx: 0,
        progress: 0,
        ready: false,
        rotationDeg: 0,
      };
    }
    const distancePx = Math.min(cfg.MAX_PULL_PX, Math.round(raw * cfg.RESISTANCE));
    const progress = Math.min(1, distancePx / cfg.THRESHOLD_PX);
    return {
      distancePx,
      progress,
      ready: distancePx >= cfg.THRESHOLD_PX,
      rotationDeg: Math.round(progress * cfg.MAX_ROTATION_DEG),
    };
  }

  global.MKVForgePullRefresh = Object.freeze({
    createPullRefreshState,
    isInteractiveOrScrollableTarget,
    shouldStartPull,
    shouldCancelForHorizontal,
    computePullMetrics,
  });
})(window);
