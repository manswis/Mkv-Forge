/* Pure Speaker & Audio System Test Generator state, presets, validation & payload helpers. */

(function attachTestMediaHelpers(global) {
  "use strict";

  const { TEST_MEDIA } = global.MKVForgeConstants;
  const SAFE_FILENAME_RE = /^[A-Za-z0-9._ -]+$/;

  function getCodecMeta(codecId) {
    return (
      TEST_MEDIA.CODECS.find((c) => c.id === codecId) || TEST_MEDIA.CODECS[0]
    );
  }

  function getLayoutMeta(layoutId) {
    return (
      TEST_MEDIA.LAYOUTS.find((l) => l.id === layoutId) ||
      TEST_MEDIA.LAYOUTS.find((l) => l.id === "5.1")
    );
  }

  function getAllowedContainers(outputMode, codecId) {
    if (outputMode === TEST_MEDIA.OUTPUT_VIDEO) {
      return [TEST_MEDIA.VIDEO_CONTAINER];
    }
    const meta = getCodecMeta(codecId);
    return Array.from(meta.containers || ["mka"]);
  }

  function getBitrateConfig(codecId, layoutId) {
    const meta = getCodecMeta(codecId);
    if (meta.isLossless) {
      return { options: [], defaultBitrate: null };
    }
    const layoutMeta = getLayoutMeta(layoutId);
    const byCh = (TEST_MEDIA.BITRATES_BY_CODEC || {})[codecId] || {};
    if (byCh[layoutMeta.channels]) {
      return {
        options: Array.from(byCh[layoutMeta.channels].options),
        defaultBitrate: byCh[layoutMeta.channels].defaultBitrate,
      };
    }
    const keys = Object.keys(byCh)
      .map((k) => Number(k))
      .filter((n) => Number.isFinite(n))
      .sort((a, b) => b - a);
    const nearest =
      keys.find((k) => k <= layoutMeta.channels) ?? keys[keys.length - 1];
    if (nearest !== undefined && byCh[nearest]) {
      return {
        options: Array.from(byCh[nearest].options),
        defaultBitrate: byCh[nearest].defaultBitrate,
      };
    }
    return { options: [], defaultBitrate: null };
  }

  function buildDefaultTestFilename(draft) {
    const codecTag = String((draft && draft.codec) || "eac3").toUpperCase();
    const layout = String((draft && draft.layout) || "5.1");
    const allowed = getAllowedContainers(
      (draft && draft.outputMode) || TEST_MEDIA.OUTPUT_VIDEO,
      (draft && draft.codec) || "eac3"
    );
    const container =
      draft && draft.container && allowed.includes(draft.container)
        ? draft.container
        : allowed[0];
    const suffix =
      draft && draft.signalType === TEST_MEDIA.SIGNAL_PINK ? "_PinkNoise" : "";
    return `MKVForge_Test_${codecTag}_${layout}${suffix}.${container}`;
  }

  function estimateDurationSec(
    layoutId,
    toneDurationSec = TEST_MEDIA.SLOT_DURATION_SEC,
    gapDurationSec = TEST_MEDIA.GAP_DURATION_SEC
  ) {
    const layoutMeta = getLayoutMeta(layoutId);
    const ch = layoutMeta.channels;
    const tone = Number.isFinite(Number(toneDurationSec))
      ? Number(toneDurationSec)
      : TEST_MEDIA.SLOT_DURATION_SEC;
    const gap = Number.isFinite(Number(gapDurationSec))
      ? Number(gapDurationSec)
      : TEST_MEDIA.GAP_DURATION_SEC;
    const perSlot = tone + gap;
    if (ch <= 1) {
      return Math.round(perSlot * 10) / 10;
    }
    return Math.round((ch * perSlot + tone + gap) * 10) / 10;
  }

  function resolveProfileRecommendedSpec(playbackProfile) {
    if (
      !playbackProfile ||
      !playbackProfile.preset ||
      playbackProfile.preset === "neutral"
    ) {
      return null;
    }
    const maxCh = Number(playbackProfile.maxChannels || 6);
    let codec = playbackProfile.preferredCodec || "eac3";
    if (!TEST_MEDIA.CODECS.some((c) => c.id === codec)) {
      codec = "eac3";
    }
    let layout = "5.1";
    if (maxCh <= 2) {
      layout = "2.0";
    } else if (maxCh >= 8) {
      const supported = Array.isArray(playbackProfile.supportedCodecs)
        ? playbackProfile.supportedCodecs
        : [];
      if (codec === "eac3" || codec === "ac3") {
        // Dolby Digital / Plus caps at 5.1; if AVR supports FLAC/PCM 7.1, prefer FLAC 7.1 for eARC
        if (supported.includes("flac")) {
          codec = "flac";
          layout = "7.1";
        } else {
          layout = "5.1";
        }
      } else {
        layout = "7.1";
      }
    }
    const brCfg = getBitrateConfig(codec, layout);
    return {
      outputMode: TEST_MEDIA.OUTPUT_VIDEO,
      codec,
      layout,
      container: TEST_MEDIA.VIDEO_CONTAINER,
      bitrate: brCfg.defaultBitrate,
    };
  }

  function detectMatchingPresetId(draft, playbackProfile = null, preferredId = null) {
    if (!draft) return null;
    const matchesSpec = (spec) =>
      Boolean(
        spec &&
          draft.outputMode === spec.outputMode &&
          draft.codec === spec.codec &&
          draft.layout === spec.layout &&
          (spec.bitrate === undefined ||
            spec.bitrate === null ||
            draft.bitrate === spec.bitrate)
      );

    const profileSpec = resolveProfileRecommendedSpec(playbackProfile);
    if (preferredId === "your_system" && matchesSpec(profileSpec)) {
      return "your_system";
    }
    if (preferredId && preferredId !== "your_system") {
      const prefPreset = TEST_MEDIA.PRESETS.find((p) => p.id === preferredId);
      if (matchesSpec(prefPreset)) {
        return prefPreset.id;
      }
    }
    if (matchesSpec(profileSpec)) {
      return "your_system";
    }
    const matched = TEST_MEDIA.PRESETS.find((p) => matchesSpec(p));
    return matched ? matched.id : null;
  }

  function createTestMediaDraft({
    playbackProfile = null,
    destinationDir = TEST_MEDIA.DEFAULT_DESTINATION_DIR,
    fromProfile = false,
  } = {}) {
    const profileRec = fromProfile
      ? resolveProfileRecommendedSpec(playbackProfile)
      : null;
    const base = profileRec || {
      outputMode: TEST_MEDIA.OUTPUT_VIDEO,
      codec: "eac3",
      layout: "5.1",
      container: TEST_MEDIA.VIDEO_CONTAINER,
      bitrate: "640k",
    };
    const draft = {
      outputMode: base.outputMode,
      codec: base.codec,
      layout: base.layout,
      container: base.container,
      bitrate: base.bitrate,
      signalType: TEST_MEDIA.SIGNAL_TONES,
      toneDurationSec: TEST_MEDIA.SLOT_DURATION_SEC,
      gapDurationSec: TEST_MEDIA.GAP_DURATION_SEC,
      includeSubtitles: TEST_MEDIA.DEFAULT_INCLUDE_SUBTITLES,
      destinationDir: destinationDir || TEST_MEDIA.DEFAULT_DESTINATION_DIR,
      filename: "",
      filenameEdited: false,
      advancedOpen: false,
      activePresetId: profileRec ? "your_system" : "dolby_arc_51",
    };
    draft.filename = buildDefaultTestFilename(draft);
    return draft;
  }

  function applyTestMediaPreset(draft, presetId, playbackProfile = null) {
    const base = { ...(draft || createTestMediaDraft()) };
    let spec = null;
    if (presetId === "your_system") {
      spec = resolveProfileRecommendedSpec(playbackProfile);
    } else {
      spec = TEST_MEDIA.PRESETS.find((p) => p.id === presetId) || null;
    }
    if (!spec) return base;

    const allowed = getAllowedContainers(spec.outputMode, spec.codec);
    const brCfg = getBitrateConfig(spec.codec, spec.layout);
    const next = {
      ...base,
      outputMode: spec.outputMode,
      codec: spec.codec,
      layout: spec.layout,
      container: allowed[0],
      bitrate: spec.bitrate !== undefined ? spec.bitrate : brCfg.defaultBitrate,
      activePresetId: presetId,
    };
    if (!next.filenameEdited) {
      next.filename = buildDefaultTestFilename(next);
    }
    return next;
  }

  function _profileSupportsTestCombo(playbackProfile, codecId, channels) {
    if (
      !playbackProfile ||
      !playbackProfile.preset ||
      playbackProfile.preset === "neutral"
    ) {
      return true;
    }
    const maxCh = Number(playbackProfile.maxChannels || 6);
    if (channels > maxCh) return false;
    const supported = Array.isArray(playbackProfile.supportedCodecs)
      ? playbackProfile.supportedCodecs
      : [];
    if (supported.length === 0) return true;
    if (codecId === "aac") {
      return channels > 2
        ? supported.includes("aac_51")
        : supported.includes("aac_20");
    }
    return supported.includes(codecId);
  }

  function evaluateTestMediaConfig(draft, playbackProfile = null) {
    if (!draft) {
      return {
        valid: false,
        blockReason: "Configuration not initialized.",
        warnings: [],
        summary: "",
        effectiveContainer: TEST_MEDIA.VIDEO_CONTAINER,
        effectiveBitrate: null,
        channels: 0,
        estimatedDurationSec: 0,
      };
    }

    const codecMeta = getCodecMeta(draft.codec);
    const layoutMeta = getLayoutMeta(draft.layout);
    const allowedContainers = getAllowedContainers(draft.outputMode, draft.codec);
    const effectiveContainer = allowedContainers.includes(draft.container)
      ? draft.container
      : allowedContainers[0];
    const brCfg = getBitrateConfig(draft.codec, draft.layout);
    const effectiveBitrate = codecMeta.isLossless
      ? null
      : brCfg.options.includes(draft.bitrate)
        ? draft.bitrate
        : brCfg.defaultBitrate;

    // Hard Blocks (disable Generate button)
    let blockReason = null;

    const toneDur = Number(
      draft.toneDurationSec ?? TEST_MEDIA.SLOT_DURATION_SEC
    );
    const gapDur = Number(draft.gapDurationSec ?? TEST_MEDIA.GAP_DURATION_SEC);

    if (layoutMeta.channels > codecMeta.maxChannels) {
      blockReason = `${codecMeta.label} supports up to 5.1 (6 channels). Choose 5.1, or switch to FLAC, PCM, Opus, or AAC for ${layoutMeta.label}.`;
    } else if (draft.codec === "opus" && draft.layout === "2.1") {
      blockReason =
        "Opus (RFC 7845) defines 3-channel surround as 3.0, not 2.1 (FL+FR+LFE). Choose 2.0 or 5.1, or switch to E-AC-3, AC-3, AAC, FLAC, or PCM for 2.1.";
    } else if (
      !Number.isFinite(toneDur) ||
      toneDur < TEST_MEDIA.MIN_TONE_DURATION_SEC ||
      toneDur > TEST_MEDIA.MAX_TONE_DURATION_SEC
    ) {
      blockReason = `Tone length must be between ${TEST_MEDIA.MIN_TONE_DURATION_SEC}s and ${TEST_MEDIA.MAX_TONE_DURATION_SEC}s.`;
    } else if (
      !Number.isFinite(gapDur) ||
      gapDur < TEST_MEDIA.MIN_GAP_DURATION_SEC ||
      gapDur > TEST_MEDIA.MAX_GAP_DURATION_SEC
    ) {
      blockReason = `Gap length must be between ${TEST_MEDIA.MIN_GAP_DURATION_SEC}s and ${TEST_MEDIA.MAX_GAP_DURATION_SEC}s.`;
    } else if (
      draft.outputMode === TEST_MEDIA.OUTPUT_AUDIO &&
      draft.container &&
      !allowedContainers.includes(draft.container)
    ) {
      blockReason = `Container .${draft.container} is not compatible with ${codecMeta.label}.`;
    } else {
      const trimmedName = String(draft.filename || "").trim();
      if (!trimmedName) {
        blockReason = "Enter a filename for the generated test file.";
      } else if (
        trimmedName.includes("/") ||
        trimmedName.includes("\\") ||
        trimmedName.includes("..")
      ) {
        blockReason = "Filename must not contain path separators or '..'.";
      } else if (trimmedName.length > TEST_MEDIA.MAX_FILENAME_LEN) {
        blockReason = `Filename must be ${TEST_MEDIA.MAX_FILENAME_LEN} characters or fewer.`;
      } else if (!SAFE_FILENAME_RE.test(trimmedName)) {
        blockReason =
          "Filename may only contain letters, numbers, spaces, hyphens, underscores, and dots.";
      } else if (trimmedName.toLowerCase().includes(".mkvforge.")) {
        blockReason = "Filename cannot contain the reserved '.mkvforge.' marker.";
      }
    }

    // Soft Warnings (Generate remains enabled; honest user education)
    const warnings = [];
    if (!blockReason) {
      if (
        !_profileSupportsTestCombo(
          playbackProfile,
          draft.codec,
          layoutMeta.channels
        )
      ) {
        warnings.push({
          severity: "warning",
          message: `Heads up: Your configured Sound System profile doesn't natively support ${codecMeta.label} ${layoutMeta.label} — useful if you're testing fallback/transcoding.`,
        });
      }
      if (
        ["opus", "flac", "pcm"].includes(draft.codec) &&
        layoutMeta.channels > 2
      ) {
        warnings.push({
          severity: "info",
          message: `Multichannel ${codecMeta.label} (${layoutMeta.label}) decodes as LPCM over HDMI/eARC (Plex, Jellyfin, Infuse) — not supported over Optical SPDIF.`,
        });
      }
      if (draft.codec === "aac" && layoutMeta.channels > 2) {
        warnings.push({
          severity: "warning",
          message: `Many TVs & soundbars downmix AAC ${layoutMeta.label} to 2.0 stereo over ARC/Optical. Use E-AC-3 or AC-3 to test native Dolby surround.`,
        });
      }
      if (draft.outputMode === TEST_MEDIA.OUTPUT_AUDIO) {
        warnings.push({
          severity: "info",
          message:
            "Smart TV apps (Plex/DLNA) often show richer audio badges and on-screen active speaker labels with Video + Audio (.mkv).",
        });
      }
    }

    const modeLabel =
      draft.outputMode === TEST_MEDIA.OUTPUT_VIDEO
        ? "MKV Video"
        : `.${effectiveContainer.toUpperCase()} Audio`;
    const ratePart = effectiveBitrate ? ` • ${effectiveBitrate}` : " • Lossless";
    const summary = `${modeLabel} • ${codecMeta.label} ${layoutMeta.label}${ratePart}`;

    return {
      valid: !blockReason,
      blockReason,
      warnings,
      summary,
      effectiveContainer,
      effectiveBitrate,
      channels: layoutMeta.channels,
      estimatedDurationSec: estimateDurationSec(draft.layout, toneDur, gapDur),
    };
  }

  function buildTestMediaPayload(draft) {
    const evalRes = evaluateTestMediaConfig(draft);
    return {
      output_mode: draft.outputMode,
      codec: draft.codec,
      layout: draft.layout,
      container: evalRes.effectiveContainer,
      bitrate: evalRes.effectiveBitrate,
      signal_type: draft.signalType,
      tone_duration_sec: Number(
        draft.toneDurationSec ?? TEST_MEDIA.SLOT_DURATION_SEC
      ),
      gap_duration_sec: Number(
        draft.gapDurationSec ?? TEST_MEDIA.GAP_DURATION_SEC
      ),
      include_subtitles:
        draft.outputMode === TEST_MEDIA.OUTPUT_VIDEO
          ? draft.includeSubtitles !== false
          : false,
      destination_dir: draft.destinationDir || TEST_MEDIA.DEFAULT_DESTINATION_DIR,
      filename: String(draft.filename || "").trim(),
    };
  }

  global.MKVForgeTestMedia = Object.freeze({
    getCodecMeta,
    getLayoutMeta,
    getAllowedContainers,
    getBitrateConfig,
    buildDefaultTestFilename,
    estimateDurationSec,
    resolveProfileRecommendedSpec,
    detectMatchingPresetId,
    createTestMediaDraft,
    applyTestMediaPreset,
    evaluateTestMediaConfig,
    buildTestMediaPayload,
  });
})(window);
