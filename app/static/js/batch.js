/* Pure Batch Studio & Reusable Templates state factory and helper functions. */

(function attachBatchHelpers(global) {
  const C = global.MKVForgeConstants || {};
  const EDITOR = C.EDITOR || {};
  const BATCH = C.BATCH || {
    TITLE_ACTION_CLEAR: "clear",
    TITLE_ACTION_KEEP: "keep",
    AUDIO_LANG_MODE_PREFERRED: "preferred",
    AUDIO_LANG_MODE_FIRST: "first_audio",
    AUDIO_LANG_MODE_CUSTOM: "custom",
    MATCH_INCOMPATIBLE_OR_NON_DOLBY: "incompatible_or_non_dolby",
    MATCH_AAC51_ONLY: "aac_51_only",
    MATCH_ANY_NON_TARGET: "any_non_target",
    KEEP_TRACKS_ALL: "all",
    KEEP_TRACKS_PREFERRED: "preferred_only",
    KEEP_SUBS_NONE: "none",
    ACTION_ENQUEUE: "enqueue",
    ACTION_SKIP_COMPLIANT: "skip_already_compliant",
    ACTION_SKIP_ACTIVE_JOB: "skip_active_job",
    ACTION_SKIP_NOT_READY: "skip_not_ready",
    ACTION_SKIP_WARNING: "skip_warning",
    STAGE_TRANSCODE_AND_REMUX: "transcode_and_remux",
    STAGE_REMUX_ONLY: "remux_only",
    STAGE_NONE: "none",
    CHIP_CAT_VIDEO: "video",
    CHIP_CAT_AUDIO: "audio",
    CHIP_CAT_SUBTITLE: "subtitle",
    CHIP_CAT_TAG: "tag",
    OVERRIDE_TRACK_ENABLED: "track_enabled",
    OVERRIDE_AUDIO_CONVERT: "audio_convert_enabled",
    OVERRIDE_RETAG_UND: "retag_und_enabled",
    OVERRIDE_CLEAR_TITLE: "clear_title",
    OVERRIDE_STRIP_GLOBAL_TAGS: "strip_global_tags",
    OVERRIDE_STRIP_TRACK_TAGS: "strip_track_tags",
    OVERRIDE_STRIP_ATTACHMENTS: "strip_attachments",
    OVERRIDE_STRIP_CHAPTERS: "strip_chapters",
    OVERRIDE_SMART_FILL_NAMES: "smart_fill_track_names",
    OVERRIDE_OUTPUT_FILENAME: "output_filename",
    TAB_AUDIO: "audio",
    TAB_METADATA: "metadata",
    TAB_OUTPUT: "output",
    RENAME_START_DEFAULT: 1,
    MAX_VISIBLE_CHIPS_PER_GROUP: 8,
    PREVIEW_DEBOUNCE_MS: 160,
    RETAG_UND_LABEL: "Keep untagged (und)",
  };

  function _resolveBatchDefaultCodec(prefs) {
    const Ed = global.MKVForgeEditor;
    if (Ed && typeof Ed.resolveDefaultAudioCodec === "function") {
      return Ed.resolveDefaultAudioCodec(prefs);
    }
    const validTargets = EDITOR.ALL_TARGET_CODECS || ["eac3", "ac3", "aac", "opus", "flac"];
    const explicit = String(prefs.default_audio_codec || "").trim().toLowerCase();
    if (validTargets.includes(explicit)) return explicit;
    if (
      prefs.playback_profile_preset &&
      prefs.playback_profile_preset !== "neutral" &&
      validTargets.includes(prefs.playback_preferred_codec)
    ) {
      return prefs.playback_preferred_codec;
    }
    if (!explicit && prefs.default_dolby_codec === EDITOR.CODEC_AC3) {
      return EDITOR.CODEC_AC3;
    }
    return EDITOR.CODEC_EAC3;
  }

  function _resolveBatchDefaultBitrate(prefs, targetCodec) {
    const Ed = global.MKVForgeEditor;
    if (
      Ed &&
      typeof Ed.resolveDefaultAudioQuality === "function" &&
      typeof Ed.resolveQualityBitrate === "function"
    ) {
      const quality = Ed.resolveDefaultAudioQuality(prefs);
      return Ed.resolveQualityBitrate(
        targetCodec,
        EDITOR.MAX_DOLBY_CHANNELS || 6,
        quality
      );
    }
    if (targetCodec === EDITOR.CODEC_FLAC) return null;
    const legacyMap = EDITOR.LEGACY_BITRATE_TO_QUALITY || {
      "640k": "high",
      "448k": "balanced",
      "384k": "compact",
    };
    const rawTier = String(prefs.default_audio_quality || "").trim().toLowerCase();
    const rawLegacy = String(prefs.default_dolby_bitrate || "").trim().toLowerCase();
    const tier =
      rawTier === "balanced" || rawTier === "compact"
        ? rawTier
        : rawTier === "high" && rawLegacy && rawLegacy !== "640k" && legacyMap[rawLegacy]
          ? legacyMap[rawLegacy]
          : legacyMap[rawLegacy] || "high";
    const matrix = (EDITOR.QUALITY_BITRATE_MATRIX || {})[targetCodec] || {};
    const chBucket = matrix[EDITOR.MAX_DOLBY_CHANNELS || 6];
    if (chBucket && chBucket[tier]) return chBucket[tier];
    return Array.isArray(EDITOR.BITRATES) && EDITOR.BITRATES.includes(prefs.default_dolby_bitrate)
      ? prefs.default_dolby_bitrate
      : EDITOR.DEFAULT_BITRATE || "640k";
  }

  function createDefaultBatchRule(userSettings = null) {
    const prefs = userSettings && typeof userSettings === "object" ? userSettings : {};
    const prefLang =
      String(prefs.preferred_language || EDITOR.DEFAULT_PREFERRED_LANGUAGE || "eng").trim() || "eng";
    const defaultUndLang = EDITOR.DEFAULT_LANGUAGE || "und";
    const defaultCodec = _resolveBatchDefaultCodec(prefs);
    const defaultBitrate = _resolveBatchDefaultBitrate(prefs, defaultCodec);
    const isDolbyDefault =
      defaultCodec === EDITOR.CODEC_EAC3 || defaultCodec === EDITOR.CODEC_AC3;
    const defaultOutputMode =
      prefs.default_output_mode === EDITOR.OUTPUT_REPLACE
        ? EDITOR.OUTPUT_REPLACE
        : prefs.default_output_mode === EDITOR.OUTPUT_SAVE_AS
          ? EDITOR.OUTPUT_SAVE_AS
          : EDITOR.OUTPUT_KEEP_BOTH;
    const removeMetadata = prefs.default_remove_all_metadata !== false;

    return {
      container: {
        title_action: removeMetadata ? BATCH.TITLE_ACTION_CLEAR : BATCH.TITLE_ACTION_KEEP,
        strip_global_tags: removeMetadata || Boolean(prefs.default_strip_global_tags),
        strip_track_tags: removeMetadata,
        strip_attachments: removeMetadata,
        strip_chapters: false,
        smart_fill_track_names: true,
      },
      audio: {
        enabled: true,
        source_language_mode: BATCH.AUDIO_LANG_MODE_PREFERRED,
        custom_source_language: prefLang,
        match_condition: isDolbyDefault
          ? BATCH.MATCH_INCOMPATIBLE_OR_NON_DOLBY
          : BATCH.MATCH_ANY_NON_TARGET,
        target_codec: defaultCodec,
        target_channels: null,
        bitrate: defaultBitrate,
        accept_existing_dolby: isDolbyDefault,
        set_as_default: true,
        drop_source_track: false,
      },
      track_filter: {
        retag_und_audio_to_preferred: false,
        retag_und_target_language: defaultUndLang,
        preferred_language: prefLang,
        keep_audio_mode: BATCH.KEEP_TRACKS_ALL,
        keep_audio_languages: [],
        keep_subtitle_mode: BATCH.KEEP_TRACKS_ALL,
        keep_subtitle_languages: [],
      },
      rename: {
        enabled: false,
        pattern: "",
        start_number: BATCH.RENAME_START_DEFAULT || 1,
      },
      output_mode: defaultOutputMode,
      destination_folder: null,
      delete_original: false,
    };
  }

  function cloneBatchRule(rule, userSettings = null) {
    const base = createDefaultBatchRule(userSettings);
    if (!rule || typeof rule !== "object") return base;
    const c = rule.container && typeof rule.container === "object" ? rule.container : {};
    const a = rule.audio && typeof rule.audio === "object" ? rule.audio : {};
    const tf = rule.track_filter && typeof rule.track_filter === "object" ? rule.track_filter : {};
    const rn = rule.rename && typeof rule.rename === "object" ? rule.rename : {};
    const parsedStart = Number(rn.start_number);
    const validStart =
      Number.isInteger(parsedStart) && parsedStart >= 0 && parsedStart <= 9999
        ? parsedStart
        : base.rename.start_number;

    return {
      container: {
        title_action:
          c.title_action === BATCH.TITLE_ACTION_KEEP
            ? BATCH.TITLE_ACTION_KEEP
            : BATCH.TITLE_ACTION_CLEAR,
        strip_global_tags:
          c.strip_global_tags !== undefined
            ? Boolean(c.strip_global_tags)
            : base.container.strip_global_tags,
        strip_track_tags:
          c.strip_track_tags !== undefined
            ? Boolean(c.strip_track_tags)
            : base.container.strip_track_tags,
        strip_attachments:
          c.strip_attachments !== undefined
            ? Boolean(c.strip_attachments)
            : base.container.strip_attachments,
        strip_chapters: Boolean(c.strip_chapters),
        smart_fill_track_names:
          c.smart_fill_track_names !== undefined
            ? Boolean(c.smart_fill_track_names)
            : base.container.smart_fill_track_names,
      },
      audio: {
        enabled: a.enabled !== undefined ? Boolean(a.enabled) : true,
        source_language_mode: a.source_language_mode || base.audio.source_language_mode,
        custom_source_language: a.custom_source_language || base.audio.custom_source_language,
        match_condition: a.match_condition || base.audio.match_condition,
        target_codec: a.target_codec || base.audio.target_codec,
        target_channels:
          a.target_channels !== null && a.target_channels !== undefined && a.target_channels !== ""
            ? Number(a.target_channels)
            : null,
        bitrate: a.bitrate !== undefined ? a.bitrate : base.audio.bitrate,
        accept_existing_dolby:
          a.accept_existing_dolby !== undefined ? Boolean(a.accept_existing_dolby) : true,
        set_as_default: a.set_as_default !== undefined ? Boolean(a.set_as_default) : true,
        drop_source_track: Boolean(a.drop_source_track),
      },
      track_filter: {
        retag_und_audio_to_preferred: Boolean(tf.retag_und_audio_to_preferred),
        retag_und_target_language:
          String(tf.retag_und_target_language || "").trim() ||
          base.track_filter.retag_und_target_language,
        preferred_language: tf.preferred_language || base.track_filter.preferred_language,
        keep_audio_mode: tf.keep_audio_mode || BATCH.KEEP_TRACKS_ALL,
        keep_audio_languages: Array.isArray(tf.keep_audio_languages)
          ? [...tf.keep_audio_languages]
          : [],
        keep_subtitle_mode: tf.keep_subtitle_mode || BATCH.KEEP_TRACKS_ALL,
        keep_subtitle_languages: Array.isArray(tf.keep_subtitle_languages)
          ? [...tf.keep_subtitle_languages]
          : [],
      },
      rename: {
        enabled: Boolean(rn.enabled),
        pattern: String(rn.pattern || ""),
        start_number: validStart,
      },
      output_mode:
        rule.output_mode === EDITOR.OUTPUT_REPLACE
          ? EDITOR.OUTPUT_REPLACE
          : rule.output_mode === EDITOR.OUTPUT_SAVE_AS
            ? EDITOR.OUTPUT_SAVE_AS
            : EDITOR.OUTPUT_KEEP_BOTH,
      destination_folder: rule.destination_folder ? String(rule.destination_folder) : null,
      delete_original: Boolean(rule.delete_original),
    };
  }

  function serializeBatchRule(rule, userSettings = null) {
    return JSON.stringify(cloneBatchRule(rule, userSettings));
  }

  function buildRetagLanguageOptions(detectedLanguages = [], currentCode = "") {
    const undCode = EDITOR.DEFAULT_LANGUAGE || "und";
    const undLabel = BATCH.RETAG_UND_LABEL || "Keep untagged (und)";
    const standardOptions = Array.isArray(EDITOR.LANGUAGE_OPTIONS)
      ? EDITOR.LANGUAGE_OPTIONS
      : [];
    const labelByCode = {};
    for (const opt of standardOptions) {
      if (opt && opt.code) {
        labelByCode[String(opt.code).toLowerCase()] = opt.label;
      }
    }

    const result = [{ code: undCode, label: undLabel }];
    const seen = new Set([undCode]);

    const addCode = (rawCode, detectedHint) => {
      const code = String(rawCode || "").trim().toLowerCase();
      if (!code || seen.has(code)) return;
      seen.add(code);
      const baseLabel = labelByCode[code] || `${code.toUpperCase()} (${code})`;
      result.push({
        code,
        label: detectedHint ? `${baseLabel} • In batch` : baseLabel,
      });
    };

    if (Array.isArray(detectedLanguages)) {
      for (const lang of detectedLanguages) {
        addCode(lang, true);
      }
    }

    if (currentCode) {
      addCode(currentCode, false);
    }

    for (const opt of standardOptions) {
      if (opt && opt.code) {
        addCode(opt.code, false);
      }
    }

    return result;
  }

  function _pruneEmptyFileOverride(entry) {
    if (!entry || typeof entry !== "object") return null;
    const cleaned = {};
    for (const [k, v] of Object.entries(entry)) {
      if (k === BATCH.OVERRIDE_TRACK_ENABLED) {
        if (v && typeof v === "object" && Object.keys(v).length > 0) {
          cleaned[k] = v;
        }
      } else if (v !== undefined && v !== null) {
        cleaned[k] = v;
      }
    }
    return Object.keys(cleaned).length > 0 ? cleaned : null;
  }

  function toggleFileChipOverride(fileOverrides, fileId, chip) {
    const baseMap = fileOverrides && typeof fileOverrides === "object" ? fileOverrides : {};
    if (!chip || chip.disabled || fileId === undefined || fileId === null) {
      return baseMap;
    }

    const key = String(fileId);
    const existing = baseMap[key] ? { ...baseMap[key] } : {};
    const nextValue = !Boolean(chip.selected);
    const matchesDefault = nextValue === Boolean(chip.default_selected);

    if (chip.override_key === BATCH.OVERRIDE_TRACK_ENABLED) {
      if (chip.track_id === undefined || chip.track_id === null) return baseMap;
      const tracks = { ...(existing[BATCH.OVERRIDE_TRACK_ENABLED] || {}) };
      const tid = String(chip.track_id);
      if (matchesDefault) {
        delete tracks[tid];
      } else {
        tracks[tid] = nextValue;
      }
      if (Object.keys(tracks).length > 0) {
        existing[BATCH.OVERRIDE_TRACK_ENABLED] = tracks;
      } else {
        delete existing[BATCH.OVERRIDE_TRACK_ENABLED];
      }
    } else if (chip.override_key) {
      if (matchesDefault) {
        delete existing[chip.override_key];
      } else {
        existing[chip.override_key] = nextValue;
      }
    }

    const nextMap = { ...baseMap };
    const pruned = _pruneEmptyFileOverride(existing);
    if (pruned) {
      nextMap[key] = pruned;
    } else {
      delete nextMap[key];
    }
    return nextMap;
  }

  function clearOverrideFieldForAllFiles(
    fileOverrides,
    overrideKey,
    previewItems = [],
    trackCategory = null
  ) {
    const baseMap = fileOverrides && typeof fileOverrides === "object" ? fileOverrides : {};
    if (!overrideKey || Object.keys(baseMap).length === 0) return baseMap;

    const categoryTrackIdsByFile = {};
    if (overrideKey === BATCH.OVERRIDE_TRACK_ENABLED && trackCategory && Array.isArray(previewItems)) {
      for (const item of previewItems) {
        const fid = String(item.file_id);
        const ids = new Set();
        for (const chip of item.chips || []) {
          if (chip.category === trackCategory && chip.track_id !== null && chip.track_id !== undefined) {
            ids.add(String(chip.track_id));
          }
        }
        categoryTrackIdsByFile[fid] = ids;
      }
    }

    const nextMap = {};
    for (const [fid, entry] of Object.entries(baseMap)) {
      const copy = { ...entry };
      if (overrideKey === BATCH.OVERRIDE_TRACK_ENABLED && copy[BATCH.OVERRIDE_TRACK_ENABLED]) {
        const tracks = { ...copy[BATCH.OVERRIDE_TRACK_ENABLED] };
        const toRemove = categoryTrackIdsByFile[fid];
        if (toRemove) {
          for (const tid of toRemove) {
            delete tracks[tid];
          }
        } else {
          for (const tid of Object.keys(tracks)) {
            delete tracks[tid];
          }
        }
        if (Object.keys(tracks).length > 0) {
          copy[BATCH.OVERRIDE_TRACK_ENABLED] = tracks;
        } else {
          delete copy[BATCH.OVERRIDE_TRACK_ENABLED];
        }
      } else {
        delete copy[overrideKey];
      }
      const pruned = _pruneEmptyFileOverride(copy);
      if (pruned) {
        nextMap[fid] = pruned;
      }
    }
    return nextMap;
  }

  function resetFileOverrides(fileOverrides, fileId) {
    const baseMap = fileOverrides && typeof fileOverrides === "object" ? fileOverrides : {};
    const key = String(fileId);
    if (!(key in baseMap)) return baseMap;
    const nextMap = { ...baseMap };
    delete nextMap[key];
    return nextMap;
  }

  function getChipsByCategory(item, category) {
    if (!item || !Array.isArray(item.chips)) return [];
    return item.chips.filter((c) => c && c.category === category);
  }

  function getAudioApplyToOptions(targetCodec) {
    const codec = String(targetCodec || "eac3").toLowerCase();
    const targetDisplayMap = {
      eac3: "E-AC-3",
      ac3: "AC-3",
      aac: "AAC",
      opus: "Opus",
      flac: "FLAC",
    };
    const targetLabel = targetDisplayMap[codec] || codec.toUpperCase();

    if (codec === "flac") {
      return [];
    }
    if (codec === "eac3" || codec === "ac3") {
      return [
        {
          value: BATCH.MATCH_INCOMPATIBLE_OR_NON_DOLBY,
          label: "Files without Dolby (AAC, DTS, TrueHD, FLAC…)",
        },
        {
          value: BATCH.MATCH_AAC51_ONLY,
          label: "AAC 5.1 surround files only",
        },
        {
          value: BATCH.MATCH_ANY_NON_TARGET,
          label: `All files not already in ${targetLabel}`,
        },
      ];
    }
    if (codec === "opus") {
      return [
        {
          value: BATCH.MATCH_ANY_NON_TARGET,
          label: `All files not already in ${targetLabel}`,
        },
        {
          value: BATCH.MATCH_AAC51_ONLY,
          label: "AAC 5.1 surround files only",
        },
      ];
    }
    return [
      {
        value: BATCH.MATCH_ANY_NON_TARGET,
        label: `All files not already in ${targetLabel}`,
      },
    ];
  }

  function getBitrateOptionsForCodec(targetCodec) {
    const codec = String(targetCodec || "eac3").toLowerCase();
    const byCodec = BATCH.BITRATE_OPTIONS_BY_CODEC || {};
    if (Array.isArray(byCodec[codec])) {
      return byCodec[codec];
    }
    if (codec === "flac") return [];
    return Array.isArray(BATCH.BITRATE_OPTIONS)
      ? BATCH.BITRATE_OPTIONS
      : ["640k", "448k", "384k", "256k", "192k"];
  }

  function normalizeAudioRuleForTargetCodec(audioRule, userSettings = null) {
    if (!audioRule || typeof audioRule !== "object") return;
    const codec = String(audioRule.target_codec || "eac3").toLowerCase();
    const options = getAudioApplyToOptions(codec);
    const validValues = new Set(options.map((o) => o.value));

    const allowedBitrates = getBitrateOptionsForCodec(codec);
    const defaultsByCodec = BATCH.DEFAULT_BITRATE_BY_CODEC || {
      eac3: "640k",
      ac3: "448k",
      aac: "448k",
      opus: "320k",
      flac: null,
    };
    const qualityPreferred =
      userSettings && typeof userSettings === "object"
        ? _resolveBatchDefaultBitrate(userSettings, codec)
        : null;
    if (allowedBitrates.length === 0) {
      audioRule.bitrate = null;
    } else if (
      qualityPreferred &&
      allowedBitrates.includes(qualityPreferred) &&
      !allowedBitrates.includes(audioRule.bitrate)
    ) {
      audioRule.bitrate = qualityPreferred;
    } else if (!allowedBitrates.includes(audioRule.bitrate)) {
      audioRule.bitrate = defaultsByCodec[codec] || allowedBitrates[0];
    }

    if (options.length === 0) {
      audioRule.match_condition = BATCH.MATCH_ANY_NON_TARGET;
      audioRule.accept_existing_dolby = false;
      return;
    }

    if (!validValues.has(audioRule.match_condition)) {
      audioRule.match_condition = options[0].value;
    }

    audioRule.accept_existing_dolby =
      (codec === "eac3" || codec === "ac3") &&
      audioRule.match_condition === BATCH.MATCH_INCOMPATIBLE_OR_NON_DOLBY;
  }

  function setFileOutputFilenameOverride(fileOverrides, fileId, rawFilename) {
    const baseMap = fileOverrides && typeof fileOverrides === "object" ? fileOverrides : {};
    if (fileId === undefined || fileId === null) return baseMap;
    const key = String(fileId);
    const cleaned = String(rawFilename || "").trim();
    const existing = baseMap[key] ? { ...baseMap[key] } : {};
    if (cleaned) {
      existing[BATCH.OVERRIDE_OUTPUT_FILENAME] = cleaned;
    } else {
      delete existing[BATCH.OVERRIDE_OUTPUT_FILENAME];
    }
    const nextMap = { ...baseMap };
    const pruned = _pruneEmptyFileOverride(existing);
    if (pruned) {
      nextMap[key] = pruned;
    } else {
      delete nextMap[key];
    }
    return nextMap;
  }

  function formatAudioTabSummary(rule) {
    if (!rule || !rule.audio || !rule.audio.enabled) return "Off";
    const codecMap = { eac3: "EAC3", ac3: "AC3", aac: "AAC", opus: "Opus", flac: "FLAC" };
    const lbl = codecMap[rule.audio.target_codec] || String(rule.audio.target_codec || "").toUpperCase();
    return rule.audio.target_codec === "flac" || !rule.audio.bitrate
      ? lbl
      : `${lbl} ${rule.audio.bitrate}`;
  }

  function formatMetadataTabSummary(rule) {
    if (!rule) return "Default";
    let count = 0;
    if (rule.container && rule.container.title_action === BATCH.TITLE_ACTION_CLEAR) count += 1;
    if (rule.container && (rule.container.strip_global_tags || rule.container.strip_track_tags)) count += 1;
    if (rule.container && rule.container.smart_fill_track_names) count += 1;
    if (rule.container && rule.container.strip_attachments) count += 1;
    if (rule.track_filter && rule.track_filter.retag_und_audio_to_preferred) count += 1;
    if (rule.track_filter && rule.track_filter.keep_subtitle_mode !== BATCH.KEEP_TRACKS_ALL) count += 1;
    return count > 0 ? `${count} active` : "Default";
  }

  function formatOutputTabSummary(rule) {
    if (!rule) return "Keep Both";
    const modeLabel =
      rule.output_mode === EDITOR.OUTPUT_REPLACE
        ? "Replace"
        : rule.output_mode === EDITOR.OUTPUT_SAVE_AS
          ? "Save As"
          : "Keep Both";
    const hasRename = Boolean(rule.rename && rule.rename.enabled && String(rule.rename.pattern || "").trim());
    return hasRename ? `${modeLabel} · Rename` : modeLabel;
  }

  function formatPlanStageBadge(item) {
    if (!item) return { label: "Unknown", statusClass: "ui-tag" };
    if (item.collision_error) {
      return {
        label: "Collision",
        statusClass: "ui-status ui-status-err",
      };
    }
    if (item.action === BATCH.ACTION_ENQUEUE) {
      if (item.stage_plan === BATCH.STAGE_TRANSCODE_AND_REMUX) {
        return {
          label: "Convert + Remux",
          statusClass: "ui-status ui-status-info",
        };
      }
      return {
        label: "Fast Remux Only",
        statusClass: "ui-status ui-status-ok",
      };
    }
    if (item.action === BATCH.ACTION_SKIP_ACTIVE_JOB) {
      return {
        label: "Skip (In Queue)",
        statusClass: "ui-status ui-status-warn",
      };
    }
    if (item.action === BATCH.ACTION_SKIP_COMPLIANT) {
      return {
        label: "Skip (Compliant)",
        statusClass: "ui-tag text-ink-muted",
      };
    }
    return {
      label: "Skipped",
      statusClass: "ui-status ui-status-warn",
    };
  }

  global.MKVForgeBatch = Object.freeze({
    createDefaultBatchRule,
    cloneBatchRule,
    serializeBatchRule,
    buildRetagLanguageOptions,
    toggleFileChipOverride,
    clearOverrideFieldForAllFiles,
    resetFileOverrides,
    setFileOutputFilenameOverride,
    getChipsByCategory,
    getAudioApplyToOptions,
    getBitrateOptionsForCodec,
    normalizeAudioRuleForTargetCodec,
    formatAudioTabSummary,
    formatMetadataTabSummary,
    formatOutputTabSummary,
    formatPlanStageBadge,
  });
})(window);
