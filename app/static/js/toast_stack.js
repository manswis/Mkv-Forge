/* Pure toast stack helpers (normalize, push with cap/dedupe, dismiss) for MKV Forge. */

(function attachToastStackHelpers(global) {
  const { TOAST } = global.MKVForgeConstants;

  function normalizeType(rawType) {
    return TOAST.TYPES.includes(rawType) ? rawType : TOAST.TYPE_INFO;
  }

  function durationFor(type) {
    return TOAST.DURATION_MS[normalizeType(type)] || TOAST.DURATION_MS.info;
  }

  function pushToast(stack, message, type, nextId) {
    const text = String(message == null ? "" : message).trim();
    const list = Array.isArray(stack) ? stack : [];
    if (!text) return { stack: list, toast: null };

    const normalized = normalizeType(type);
    // Identical visible toast is replaced (fresh timer + progress) instead of stacking
    const withoutDuplicate = list.filter(
      (item) => !(item.message === text && item.type === normalized)
    );
    const toast = {
      id: nextId,
      message: text,
      type: normalized,
      durationMs: durationFor(normalized),
    };
    const next = [...withoutDuplicate, toast];
    const overflow = Math.max(0, next.length - TOAST.MAX_VISIBLE);
    const kept = next.slice(overflow);
    const keptIds = new Set(kept.map((item) => item.id));
    const removed = list.filter((item) => !keptIds.has(item.id));
    return { stack: kept, toast, removed };
  }

  function dismissToast(stack, id) {
    const list = Array.isArray(stack) ? stack : [];
    return list.filter((item) => item.id !== id);
  }

  global.MKVForgeToastStack = Object.freeze({
    normalizeType,
    durationFor,
    pushToast,
    dismissToast,
  });
})(window);
