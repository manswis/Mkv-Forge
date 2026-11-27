/* Pure, zero-DOM chapters, timeline scrubbing, and export engine for MKV Forge. */

(function attachChaptersEngine(global) {
  const { CHAPTERS, SCENE_PICKER, UNITS } = global.MKVForgeConstants;

  const GENERIC_CHAPTER_RE = /^chapter\s+\d+$/i;
  const LANG_CODE_RE = /^[a-z]{2,3}$/;
  const SECONDS_RE = /^\d+(?:[.,]\d+)?$/;
  const TIME_PARTS_RE = /^(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d+))?$/;
  const MS_PAD_WIDTH = 3;
  const TIME_PAD_WIDTH = 2;
  const NS_FRACTION_SUFFIX = "000000";

  function _pad2(value) {
    return String(Math.floor(value)).padStart(TIME_PAD_WIDTH, "0");
  }

  function _pad3(value) {
    return String(Math.floor(value)).padStart(MS_PAD_WIDTH, "0");
  }

  function _defaultChapterTitle(indexOneBased) {
    return `${CHAPTERS.TITLE_PREFIX}${String(indexOneBased).padStart(
      CHAPTERS.TITLE_PAD_WIDTH,
      "0"
    )}`;
  }

  function _truncateTitle(rawTitle) {
    return String(rawTitle ?? "")
      .trim()
      .slice(0, CHAPTERS.MAX_TITLE_LEN);
  }

  function _escapeXml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&apos;");
  }

  function _parseFractionToMs(rawFraction) {
    if (!rawFraction) return 0;
    const normalized = rawFraction.slice(0, MS_PAD_WIDTH).padEnd(MS_PAD_WIDTH, "0");
    return parseInt(normalized, 10) || 0;
  }

  function formatChapterTime(ms, options = {}) {
    const numeric = Number(ms);
    const safeMs = Number.isFinite(numeric) && numeric > 0 ? Math.round(numeric) : 0;
    const totalSeconds = Math.floor(safeMs / UNITS.MS_PER_SEC);
    const remMs = safeMs % UNITS.MS_PER_SEC;
    const hours = Math.floor(totalSeconds / UNITS.SEC_PER_HOUR);
    const minutes = Math.floor((totalSeconds % UNITS.SEC_PER_HOUR) / UNITS.SEC_PER_MIN);
    const seconds = totalSeconds % UNITS.SEC_PER_MIN;
    const base = `${_pad2(hours)}:${_pad2(minutes)}:${_pad2(seconds)}`;
    return options && options.includeMs ? `${base}.${_pad3(remMs)}` : base;
  }

  function parseChapterTimeInput(rawStr) {
    if (rawStr === null || rawStr === undefined) return null;
    const cleaned = String(rawStr).trim();
    if (!cleaned || cleaned.startsWith("-")) return null;

    if (SECONDS_RE.test(cleaned)) {
      const secVal = Number(cleaned.replace(",", "."));
      if (!Number.isFinite(secVal)) return null;
      return Math.round(secVal * UNITS.MS_PER_SEC);
    }

    const match = TIME_PARTS_RE.exec(cleaned);
    if (!match) return null;

    let hours = 0;
    let minutes = 0;
    let seconds = 0;
    if (match[3] !== undefined) {
      hours = parseInt(match[1], 10);
      minutes = parseInt(match[2], 10);
      seconds = parseInt(match[3], 10);
      if (minutes >= UNITS.SEC_PER_MIN || seconds >= UNITS.SEC_PER_MIN) return null;
    } else {
      minutes = parseInt(match[1], 10);
      seconds = parseInt(match[2], 10);
      if (seconds >= UNITS.SEC_PER_MIN) return null;
    }
    const msPart = _parseFractionToMs(match[4]);
    return (
      (hours * UNITS.SEC_PER_HOUR + minutes * UNITS.SEC_PER_MIN + seconds) *
        UNITS.MS_PER_SEC +
      msPart
    );
  }

  function clampFrameTimestampMs(tsMs, durationMs) {
    const rawTs = Number(tsMs);
    const safeTs = Number.isFinite(rawTs) && rawTs > 0 ? Math.round(rawTs) : 0;
    const rawDur = Number(durationMs);
    if (!Number.isFinite(rawDur) || rawDur <= 0) {
      return safeTs;
    }
    const maxFrameMs = Math.max(0, Math.floor(rawDur) - SCENE_PICKER.EOF_MARGIN_MS);
    return Math.min(safeTs, maxFrameMs);
  }

  function buildPeekFrameTimestamps(centerMs, durationMs) {
    const center = clampFrameTimestampMs(centerMs, durationMs);
    const offsetMs = SCENE_PICKER.PEEK_OFFSET_SEC * UNITS.MS_PER_SEC;
    const before = clampFrameTimestampMs(Math.max(0, center - offsetMs), durationMs);
    const after = clampFrameTimestampMs(center + offsetMs, durationMs);
    return [before, center, after];
  }

  function buildScanSceneSlots(durationMs, densityCount = SCENE_PICKER.SCAN_DEFAULT_DENSITY) {
    const rawDur = Number(durationMs);
    if (!Number.isFinite(rawDur) || rawDur <= 0) return [];

    const parsedCount = Number(densityCount);
    const safeCount =
      Number.isFinite(parsedCount) && parsedCount >= 1
        ? Math.min(Math.round(parsedCount), SCENE_PICKER.SCAN_MAX_SLOTS)
        : SCENE_PICKER.SCAN_DEFAULT_DENSITY;

    const maxFrameMs = clampFrameTimestampMs(rawDur, rawDur);
    if (safeCount === 1 || maxFrameMs === 0) {
      return [0];
    }

    const slots = [];
    let lastTs = -1;
    for (let i = 0; i < safeCount; i += 1) {
      const ts = Math.round((maxFrameMs * i) / (safeCount - 1));
      if (ts !== lastTs) {
        slots.push(ts);
        lastTs = ts;
      }
    }
    return slots;
  }

  function sortAndNormalizeChapters(chapters, durationMs = null) {
    if (!Array.isArray(chapters) || chapters.length === 0) return [];
    const hasDurationCap = Number.isFinite(Number(durationMs)) && Number(durationMs) > 0;
    const maxStartMs = hasDurationCap ? Math.floor(Number(durationMs)) : null;

    const valid = [];
    for (let i = 0; i < chapters.length; i += 1) {
      const item = chapters[i];
      if (!item || typeof item !== "object") continue;
      const rawStart = Number(item.start_ms);
      if (!Number.isFinite(rawStart) || rawStart < 0) continue;
      let startMs = Math.round(rawStart);
      if (maxStartMs !== null) {
        startMs = Math.min(startMs, maxStartMs);
      }
      valid.push({
        start_ms: startMs,
        title: _truncateTitle(item.title),
        _origIdx: i,
      });
    }

    valid.sort((a, b) => a.start_ms - b.start_ms || a._origIdx - b._origIdx);
    const capped = valid.slice(0, CHAPTERS.MAX_COUNT);
    return capped.map((item, idx) => ({
      start_ms: item.start_ms,
      title: item.title || _defaultChapterTitle(idx + 1),
    }));
  }

  function ensureZeroStartChapter(chapters) {
    const normalized = sortAndNormalizeChapters(chapters);
    if (normalized.length === 0) return [];
    if (normalized[0].start_ms === 0) {
      return normalized;
    }
    const prepended = [
      { start_ms: 0, title: CHAPTERS.DEFAULT_START_TITLE },
      ...normalized,
    ];
    return prepended.slice(0, CHAPTERS.MAX_COUNT);
  }

  function generateQuickSkipChapters(durationMs, intervalMin) {
    const rawDur = Number(durationMs);
    const rawInterval = Number(intervalMin);
    if (
      !Number.isFinite(rawDur) ||
      rawDur <= 0 ||
      !Number.isFinite(rawInterval) ||
      rawInterval <= 0
    ) {
      return [];
    }

    const stepMs = Math.round(rawInterval * UNITS.SEC_PER_MIN * UNITS.MS_PER_SEC);
    if (stepMs <= 0) return [];

    const out = [];
    for (
      let ts = 0;
      ts < rawDur && out.length < CHAPTERS.MAX_COUNT;
      ts += stepMs
    ) {
      out.push({
        start_ms: ts,
        title: _defaultChapterTitle(out.length + 1),
      });
    }
    return out;
  }

  function renumberGenericChapters(chapters) {
    const normalized = sortAndNormalizeChapters(chapters);
    return normalized.map((item, idx) => {
      const trimmed = _truncateTitle(item.title);
      const isGeneric = !trimmed || GENERIC_CHAPTER_RE.test(trimmed);
      return {
        start_ms: item.start_ms,
        title: isGeneric ? _defaultChapterTitle(idx + 1) : trimmed,
      };
    });
  }

  function shiftAllChapters(chapters, deltaMs, durationMs = null) {
    if (!Array.isArray(chapters) || chapters.length === 0) return [];
    const numericDelta = Number(deltaMs);
    const safeDelta = Number.isFinite(numericDelta) ? Math.round(numericDelta) : 0;
    const hasDurationCap = Number.isFinite(Number(durationMs)) && Number(durationMs) > 0;
    const maxStartMs = hasDurationCap ? Math.floor(Number(durationMs)) : null;

    const shifted = chapters
      .filter(
        (item) =>
          item &&
          typeof item === "object" &&
          Number.isFinite(Number(item.start_ms))
      )
      .map((item) => {
        let nextStart = Math.max(0, Math.round(Number(item.start_ms)) + safeDelta);
        if (maxStartMs !== null) {
          nextStart = Math.min(nextStart, maxStartMs);
        }
        return {
          start_ms: nextStart,
          title: item.title,
        };
      });

    return sortAndNormalizeChapters(shifted, durationMs);
  }

  function buildVideoFingerprint(fileDetail) {
    if (!fileDetail || typeof fileDetail !== "object") return "0";
    const id = Number(fileDetail.id || 0);
    const size = Number(fileDetail.size_bytes || 0);
    const mtime = Math.round(Number(fileDetail.mtime || 0));
    const dur = Number(fileDetail.duration_us || 0);
    return `${id}-${size}-${mtime}-${dur}`;
  }

  function exportChaptersAsOggText(chapters) {
    const normalized = sortAndNormalizeChapters(chapters);
    if (normalized.length === 0) return "";
    const lines = [];
    normalized.forEach((item, idx) => {
      const num = String(idx + 1).padStart(CHAPTERS.TITLE_PAD_WIDTH, "0");
      lines.push(
        `CHAPTER${num}=${formatChapterTime(item.start_ms, { includeMs: true })}`
      );
      lines.push(`CHAPTER${num}NAME=${item.title}`);
    });
    return `${lines.join("\n")}\n`;
  }

  function exportChaptersAsXml(chapters, language = CHAPTERS.DEFAULT_LANGUAGE) {
    const normalized = sortAndNormalizeChapters(chapters);
    if (normalized.length === 0) return "";

    const langCleaned = String(language || "").trim().toLowerCase();
    const langCode = LANG_CODE_RE.test(langCleaned)
      ? langCleaned
      : CHAPTERS.DEFAULT_LANGUAGE;

    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      "<Chapters>",
      "  <EditionEntry>",
      `    <EditionUID>${CHAPTERS.EDITION_UID}</EditionUID>`,
      "    <EditionFlagHidden>0</EditionFlagHidden>",
      "    <EditionFlagDefault>1</EditionFlagDefault>",
    ];

    normalized.forEach((item, idx) => {
      const atomUid = CHAPTERS.ATOM_UID_BASE + idx;
      const startStr = `${formatChapterTime(item.start_ms, {
        includeMs: true,
      })}${NS_FRACTION_SUFFIX}`;
      lines.push(
        "    <ChapterAtom>",
        `      <ChapterUID>${atomUid}</ChapterUID>`,
        `      <ChapterTimeStart>${startStr}</ChapterTimeStart>`,
        "      <ChapterFlagHidden>0</ChapterFlagHidden>",
        "      <ChapterFlagEnabled>1</ChapterFlagEnabled>",
        "      <ChapterDisplay>",
        `        <ChapterString>${_escapeXml(item.title)}</ChapterString>`,
        `        <ChapterLanguage>${langCode}</ChapterLanguage>`,
        "      </ChapterDisplay>",
        "    </ChapterAtom>"
      );
    });

    lines.push("  </EditionEntry>", "</Chapters>", "");
    return lines.join("\n");
  }

  function areChapterListsEqual(listA, listB) {
    const normA = sortAndNormalizeChapters(listA);
    const normB = sortAndNormalizeChapters(listB);
    if (normA.length !== normB.length) return false;
    for (let i = 0; i < normA.length; i += 1) {
      if (
        normA[i].start_ms !== normB[i].start_ms ||
        normA[i].title !== normB[i].title
      ) {
        return false;
      }
    }
    return true;
  }

  global.MKVForgeChapters = Object.freeze({
    formatChapterTime,
    parseChapterTimeInput,
    clampFrameTimestampMs,
    buildPeekFrameTimestamps,
    buildScanSceneSlots,
    sortAndNormalizeChapters,
    ensureZeroStartChapter,
    generateQuickSkipChapters,
    renumberGenericChapters,
    shiftAllChapters,
    buildVideoFingerprint,
    exportChaptersAsOggText,
    exportChaptersAsXml,
    areChapterListsEqual,
  });
})(window);
