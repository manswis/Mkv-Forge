/* Pure File Editor state factory, logical codec matrix, track preview, and Job payload serializer. */

(function attachEditorHelpers(global) {
  const { EDITOR, EXTERNAL_TRACKS } = global.MKVForgeConstants;

  function formatChannelLabel(channels) {
    const count = Number(channels || 0);
    if (EDITOR.CHANNEL_LABELS && EDITOR.CHANNEL_LABELS[count]) {
      return EDITOR.CHANNEL_LABELS[count];
    }
    if (count > 0) return `${count}ch`;
    return "5.1";
  }

  function canonicalizeAudioCodec(rawCodec, codecId) {
    const lower = String(rawCodec || "").trim().toLowerCase();
    const cid = String(codecId || "").trim().toUpperCase();
    if (lower.includes("e-ac-3") || lower.includes("eac3") || lower.includes("eac-3") || cid === "A_EAC3") {
      return "eac3";
    }
    if (lower.includes("ac-3") || lower.includes("ac3") || cid === "A_AC3") {
      return "ac3";
    }
    if (lower.includes("aac") || cid.startsWith("A_AAC")) {
      return "aac";
    }
    if (lower.includes("truehd") || cid === "A_TRUEHD") return "truehd";
    if (lower.includes("dts") || cid.startsWith("A_DTS")) return "dts";
    if (lower.includes("opus") || cid === "A_OPUS") return "opus";
    if (lower.includes("flac") || cid === "A_FLAC") return "flac";
    if (lower.includes("mp3") || lower.includes("layer 3") || cid === "A_MPEG/L3") return "mp3";
    if (lower.includes("pcm") || cid.startsWith("A_PCM")) return "pcm";
    if (lower.includes("alac") || cid === "A_ALAC") return "alac";
    return lower || null;
  }

  function isLosslessAudioTrack(track) {
    if (!track || track.type !== "audio") return false;
    const raw = String(track.codec || "").trim().toLowerCase();
    if (raw.includes("dts-hd") || raw.includes("dts_hd") || raw.includes("dtshd")) {
      return !raw.includes("high resolution") && !raw.includes("hra");
    }
    const canonical = canonicalizeAudioCodec(track.codec, track.codec_id);
    const losslessSet = EDITOR.LOSSLESS_SOURCE_CODECS || ["truehd", "flac", "pcm"];
    return Boolean(canonical && losslessSet.includes(canonical));
  }

  function hasObjectAudioMetadata(track) {
    if (!track || track.type !== "audio") return false;
    const haystack = `${track.codec || ""} ${track.codec_id || ""} ${track.track_name || ""}`.toLowerCase();
    return (
      haystack.includes("atmos") ||
      haystack.includes("joc") ||
      haystack.includes("dts:x") ||
      haystack.includes("dts-x") ||
      haystack.includes("dtsx")
    );
  }

  function getCodecMeta(targetCodec) {
    return EDITOR.TARGET_CODEC_META[targetCodec] || EDITOR.TARGET_CODEC_META.eac3;
  }

  function resolveTargetChannels(sourceChannels, targetCodec, requestedChannels = null) {
    const meta = getCodecMeta(targetCodec);
    const maxForCodec = Number(meta.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
    const parsedSource = Number(sourceChannels || 0);
    const effectiveSource = parsedSource > 0 ? parsedSource : EDITOR.MAX_DOLBY_CHANNELS;
    const ceiling = Math.min(effectiveSource, maxForCodec);
    if (requestedChannels === null || requestedChannels === undefined || requestedChannels === "") {
      return ceiling;
    }
    const req = Number(requestedChannels);
    if (!Number.isFinite(req) || req < 1) return ceiling;
    return Math.min(Math.round(req), ceiling);
  }

  function getBitrateConfigFor(targetCodec, resolvedChannels) {
    const meta = getCodecMeta(targetCodec);
    if (meta.isLossless) {
      return { options: [], defaultBitrate: null };
    }
    const byCh = meta.bitratesByChannels || {};
    const ch = Number(resolvedChannels || EDITOR.MAX_DOLBY_CHANNELS);
    if (byCh[ch]) {
      return {
        options: Array.from(byCh[ch].options),
        defaultBitrate: byCh[ch].defaultBitrate,
      };
    }
    // Fall back to nearest defined channel bucket <= ch, else default codec bitrates
    const definedKeys = Object.keys(byCh)
      .map((k) => Number(k))
      .filter((n) => Number.isFinite(n))
      .sort((a, b) => b - a);
    const nearest = definedKeys.find((k) => k <= ch) ?? definedKeys[definedKeys.length - 1];
    if (nearest !== undefined && byCh[nearest]) {
      return {
        options: Array.from(byCh[nearest].options),
        defaultBitrate: byCh[nearest].defaultBitrate,
      };
    }
    return {
      options: Array.from(meta.bitrates || []),
      defaultBitrate: meta.defaultBitrate || EDITOR.DEFAULT_BITRATE,
    };
  }

  function resolveDefaultAudioQuality(userSettings = null) {
    const prefs = userSettings && typeof userSettings === "object" ? userSettings : {};
    const validTiers = [
      EDITOR.AUDIO_QUALITY_HIGH || "high",
      EDITOR.AUDIO_QUALITY_BALANCED || "balanced",
      EDITOR.AUDIO_QUALITY_COMPACT || "compact",
    ];
    const legacyMap = EDITOR.LEGACY_BITRATE_TO_QUALITY || {
      "640k": "high",
      "448k": "balanced",
      "384k": "compact",
    };
    const rawTier = String(prefs.default_audio_quality || "").trim().toLowerCase();
    const rawLegacy = String(prefs.default_dolby_bitrate || "").trim().toLowerCase();
    if (validTiers.includes(rawTier)) {
      // Honor direct legacy default_dolby_bitrate mutations when quality is still default 'high'
      if (
        rawTier === (EDITOR.AUDIO_QUALITY_HIGH || "high") &&
        rawLegacy &&
        rawLegacy !== "640k" &&
        legacyMap[rawLegacy]
      ) {
        return legacyMap[rawLegacy];
      }
      return rawTier;
    }
    if (rawLegacy && legacyMap[rawLegacy]) {
      return legacyMap[rawLegacy];
    }
    return EDITOR.AUDIO_QUALITY_HIGH || "high";
  }

  function resolveDefaultAudioCodec(userSettings = null, playbackProfile = null) {
    const prefs = userSettings && typeof userSettings === "object" ? userSettings : {};
    const validTargets = EDITOR.ALL_TARGET_CODECS || ["eac3", "ac3", "aac", "opus", "flac"];
    const explicit = String(prefs.default_audio_codec || "").trim().toLowerCase();
    if (validTargets.includes(explicit)) {
      return explicit;
    }
    const profilePreset =
      (playbackProfile && playbackProfile.preset) || prefs.playback_profile_preset || "neutral";
    const profileCodec = String(
      (playbackProfile && playbackProfile.preferredCodec) ||
        prefs.playback_preferred_codec ||
        ""
    )
      .trim()
      .toLowerCase();
    if (profilePreset && profilePreset !== "neutral" && validTargets.includes(profileCodec)) {
      return profileCodec;
    }
    if (!explicit && prefs.default_dolby_codec === EDITOR.CODEC_AC3) {
      return EDITOR.CODEC_AC3;
    }
    return EDITOR.CODEC_EAC3;
  }

  function resolveQualityBitrate(targetCodec, resolvedChannels, qualityTier = "high") {
    const meta = getCodecMeta(targetCodec);
    if (meta.isLossless) {
      return null;
    }
    const tier = [
      EDITOR.AUDIO_QUALITY_HIGH || "high",
      EDITOR.AUDIO_QUALITY_BALANCED || "balanced",
      EDITOR.AUDIO_QUALITY_COMPACT || "compact",
    ].includes(qualityTier)
      ? qualityTier
      : EDITOR.AUDIO_QUALITY_HIGH || "high";

    const matrix = (EDITOR.QUALITY_BITRATE_MATRIX || {})[meta.codec] || {};
    const maxForCodec = Number(meta.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
    const parsedCh = Number(resolvedChannels || EDITOR.MAX_DOLBY_CHANNELS);
    const clampedCh = Math.max(
      1,
      Math.min(Number.isFinite(parsedCh) && parsedCh > 0 ? Math.round(parsedCh) : EDITOR.MAX_DOLBY_CHANNELS, maxForCodec)
    );
    if (matrix[clampedCh] && matrix[clampedCh][tier]) {
      return matrix[clampedCh][tier];
    }
    const keys = Object.keys(matrix)
      .map((k) => Number(k))
      .filter((n) => Number.isFinite(n))
      .sort((a, b) => b - a);
    const nearest = keys.find((k) => k <= clampedCh) ?? keys[keys.length - 1];
    if (nearest !== undefined && matrix[nearest] && matrix[nearest][tier]) {
      return matrix[nearest][tier];
    }
    return getBitrateConfigFor(meta.codec, clampedCh).defaultBitrate;
  }

  function formatQualityBitrateReadout(targetCodec, qualityTier = "high") {
    const meta = getCodecMeta(targetCodec);
    const codecLabel = meta.displayLabel || meta.label || String(targetCodec || "E-AC-3").toUpperCase();
    if (meta.isLossless) {
      return `${codecLabel} (1.0 – 7.1): Lossless PCM · Bitrate N/A`;
    }
    const maxCh = Number(meta.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
    const slots = [
      { ch: 1, label: "1.0" },
      { ch: 2, label: "2.0" },
      { ch: 6, label: "5.1" },
    ];
    if (maxCh >= 8) {
      slots.push({ ch: 8, label: "7.1" });
    }
    const parts = slots.map(
      (s) => `${s.label} ${resolveQualityBitrate(meta.codec, s.ch, qualityTier)}`
    );
    const capNote = maxCh <= EDITOR.MAX_DOLBY_CHANNELS ? " (max 5.1)" : "";
    return `${codecLabel}: ${parts.join(" · ")}${capNote}`;
  }

  function getAvailableTargetCodecs(sourceTrack) {
    const lossless = isLosslessAudioTrack(sourceTrack);
    const ordered = EDITOR.ALL_TARGET_CODECS || ["eac3", "ac3", "aac", "opus", "flac"];
    return ordered
      .map((code) => getCodecMeta(code))
      .filter((meta) => !meta.requiresLosslessSource || lossless);
  }

  function getAvailableChannelOptions(sourceTrack, targetCodec) {
    const meta = getCodecMeta(targetCodec);
    const maxForCodec = Number(meta.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
    const rawSrc = Number((sourceTrack && sourceTrack.audio_channels) || 0);
    // When source channel metadata is absent, offer Match Source only (never fabricate downmixes/upmixes)
    if (rawSrc <= 0) {
      const capLabel = formatChannelLabel(maxForCodec);
      return [
        {
          value: null,
          resolvedChannels: maxForCodec,
          label: `Match Source (max ${capLabel})`,
          shortLabel: "Match",
          isMatch: true,
        },
      ];
    }
    const srcChannels = rawSrc;
    const ceiling = Math.min(srcChannels, maxForCodec);
    const ceilingLabel = formatChannelLabel(ceiling);
    const isCapped = ceiling < srcChannels;

    const options = [
      {
        value: null,
        resolvedChannels: ceiling,
        label: isCapped ? `${ceilingLabel} (Max)` : `Match (${ceilingLabel})`,
        shortLabel: ceilingLabel,
        isMatch: !isCapped,
      },
    ];

    const standardDownmix = EDITOR.STANDARD_DOWNMIX_CHANNELS || [8, 7, 6, 4, 3, 2, 1];
    // Offer canonical downmix steps strictly below ceiling (5.1, 2.0, 1.0)
    const canonicalSteps = [
      EDITOR.CHANNEL_COUNTS ? EDITOR.CHANNEL_COUNTS.SURROUND_51 : 6,
      EDITOR.CHANNEL_COUNTS ? EDITOR.CHANNEL_COUNTS.STEREO : 2,
      EDITOR.CHANNEL_COUNTS ? EDITOR.CHANNEL_COUNTS.MONO : 1,
    ];
    standardDownmix.forEach((ch) => {
      if (ch < ceiling && canonicalSteps.includes(ch)) {
        const chLabel = formatChannelLabel(ch);
        options.push({
          value: ch,
          resolvedChannels: ch,
          label: ch === 2 ? `${chLabel} Stereo` : ch === 1 ? `${chLabel} Mono` : chLabel,
          shortLabel: chLabel,
          isMatch: false,
        });
      }
    });

    return options;
  }

  function isConversionCombinationBlocked(sourceTrack, targetCodec, resolvedChannels) {
    if (!sourceTrack) {
      return { blocked: true, reason: "Select a base audio track first." };
    }
    const meta = getCodecMeta(targetCodec);
    const rawSrc = Number(sourceTrack.audio_channels || 0);
    const hasKnownChannels = rawSrc > 0;
    const srcChannels = hasKnownChannels ? rawSrc : EDITOR.MAX_DOLBY_CHANNELS;
    const outChannels = resolveTargetChannels(srcChannels, targetCodec, resolvedChannels);
    const canonicalSrc = canonicalizeAudioCodec(sourceTrack.codec, sourceTrack.codec_id);

    if (meta.requiresLosslessSource && !isLosslessAudioTrack(sourceTrack)) {
      return {
        blocked: true,
        reason: "FLAC is only available for lossless sources (TrueHD, DTS-HD MA, FLAC, PCM).",
      };
    }

    const isDownmix = hasKnownChannels && outChannels < srcChannels;
    if (canonicalSrc && canonicalSrc === targetCodec && !isDownmix) {
      return {
        blocked: true,
        reason: `Source track is already ${meta.displayLabel || meta.label}${hasKnownChannels ? " " + formatChannelLabel(srcChannels) : ""}. Choose another format or downmix channels.`,
      };
    }

    if (canonicalSrc === EDITOR.CODEC_AC3 && targetCodec === EDITOR.CODEC_EAC3 && !isDownmix) {
      return {
        blocked: true,
        reason: "Dolby devices that play E-AC-3 already play AC-3 natively — re-encoding AC-3 → E-AC-3 loses quality with no compatibility gain.",
      };
    }

    return { blocked: false, reason: null };
  }

  function getConversionNotices(sourceTrack, targetCodec, resolvedChannels) {
    if (!sourceTrack) return [];
    const notices = [];
    const meta = getCodecMeta(targetCodec);
    const srcChannels = Number(sourceTrack.audio_channels || 0) || EDITOR.MAX_DOLBY_CHANNELS;
    const outChannels = resolveTargetChannels(srcChannels, targetCodec, resolvedChannels);
    const srcLabel = formatChannelLabel(srcChannels);
    const outLabel = formatChannelLabel(outChannels);
    const losslessSource = isLosslessAudioTrack(sourceTrack);
    const maxForCodec = Number(meta.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);

    if (hasObjectAudioMetadata(sourceTrack)) {
      notices.push({
        severity: "warning",
        message:
          "Re-encoding strips Dolby Atmos / DTS:X height object metadata — only the flat surround bed is kept. Keep the original track if your AVR/soundbar supports Atmos.",
      });
    }

    if (srcChannels > maxForCodec && (targetCodec === EDITOR.CODEC_EAC3 || targetCodec === EDITOR.CODEC_AC3)) {
      const altList = losslessSource ? "FLAC (lossless), Opus, or AAC" : "Opus or AAC";
      notices.push({
        severity: "warning",
        message: `FFmpeg caps Dolby Digital / Plus at 5.1 — this track will downmix ${srcLabel} → ${outLabel}. Choose ${altList} to preserve ${srcLabel}.`,
      });
    } else if (outChannels < srcChannels) {
      notices.push({
        severity: "info",
        message: `Downmixing ${srcLabel} → ${outLabel}${outChannels === 2 ? " Stereo" : ""} using standard channel coefficients.`,
      });
    }

    if (targetCodec === EDITOR.CODEC_FLAC && losslessSource) {
      notices.push({
        severity: "success",
        message: `Lossless ${outLabel} PCM conversion — preserves native bit depth and sample rate with zero generation loss (requires HDMI/eARC, not optical SPDIF).`,
      });
    } else if (targetCodec === EDITOR.CODEC_OPUS && outChannels > 2) {
      notices.push({
        severity: "info",
        message: `Opus ${outLabel} is decoded to LPCM by Plex / Jellyfin / Infuse over HDMI/eARC (not supported over optical SPDIF).`,
      });
    }

    return notices;
  }

  function getAllowedTargetsForTrack(track) {
    if (!track || track.type !== "audio") return [];
    const canonical = canonicalizeAudioCodec(track.codec, track.codec_id);
    if (canonical && EDITOR.LOGICAL_TARGET_MAP[canonical]) {
      return EDITOR.LOGICAL_TARGET_MAP[canonical];
    }
    return EDITOR.LOGICAL_TARGET_MAP.default;
  }

  function getAvailableAddAudioOptions(editorState) {
    if (!editorState) return [];
    const audioTracks = editorState.tracks.filter((t) => t.type === "audio");
    if (audioTracks.length === 0) return [];

    const orderedCodecs = [EDITOR.CODEC_EAC3, EDITOR.CODEC_AC3, EDITOR.CODEC_AAC];
    const options = [];

    orderedCodecs.forEach((targetCodec) => {
      const eligibleTracks = audioTracks.filter((track) =>
        getAllowedTargetsForTrack(track).includes(targetCodec)
      );
      if (eligibleTracks.length === 0) return;

      const meta = getCodecMeta(targetCodec);
      const preferredTrack =
        eligibleTracks.find((t) => isAac51Track(t)) ||
        eligibleTracks.find((t) => isSurroundTrack(t)) ||
        eligibleTracks[0];
      const outChannels = resolveTargetChannels(preferredTrack.audio_channels, targetCodec, null);
      const channelLabel = formatChannelLabel(outChannels);
      const bitrateCfg = getBitrateConfigFor(targetCodec, outChannels);

      options.push({
        codec: targetCodec,
        label: `${meta.label} ${channelLabel}`,
        subtitle: meta.subtitle,
        defaultBitrate: bitrateCfg.defaultBitrate || meta.defaultBitrate,
        bitrates: bitrateCfg.options.length > 0 ? bitrateCfg.options : meta.bitrates,
        preferredSourceTrackId: preferredTrack.id,
        eligibleTracks,
      });
    });

    return options;
  }

  const COMMENTARY_TRACK_RE = /\b(commentary|comments|audio\s*description|visually\s*impaired|descriptive|director(?:'s)?|cast|crew|isolated\s*score|narration|ad|vi|sdh)\b/i;

  function _normalizeLanguageCode(rawLang) {
    const cleaned = String(rawLang || "").trim().toLowerCase();
    if (!cleaned || cleaned === "und" || cleaned === "null" || cleaned === "unknown" || cleaned === "none") {
      return EDITOR.DEFAULT_LANGUAGE;
    }
    const aliases = {
      en: "eng",
      hi: "hin",
      bn: "ben",
      ja: "jpn",
      es: "spa",
      fr: "fra",
      fre: "fra",
      de: "deu",
      ger: "deu",
      ko: "kor",
      zh: "zho",
      chi: "zho",
      it: "ita",
      ru: "rus",
      pt: "por",
      ta: "tam",
      te: "tel",
    };
    return aliases[cleaned] || cleaned;
  }

  function _isCommentaryTrack(track) {
    const name = track && (track.original_track_name || track.track_name || "");
    return Boolean(name && COMMENTARY_TRACK_RE.test(String(name)));
  }

  function _pickPrimaryBaseAudioTrack(audioTracks, preferredLanguage = null) {
    const prefNorm = _normalizeLanguageCode(preferredLanguage);
    const hasPrefLang = Boolean(prefNorm && prefNorm !== EDITOR.DEFAULT_LANGUAGE);

    const pickFromPool = (pool) =>
      pool.find((t) => t.default_track && isAac51Track(t)) ||
      pool.find((t) => t.default_track && Number(t.audio_channels || 0) >= EDITOR.MAX_DOLBY_CHANNELS) ||
      pool.find((t) => isAac51Track(t)) ||
      pool.find((t) => Number(t.audio_channels || 0) >= EDITOR.MAX_DOLBY_CHANNELS) ||
      pool.find((t) => t.default_track) ||
      pool[0];

    if (hasPrefLang) {
      const mainPrefTracks = audioTracks.filter(
        (t) =>
          !_isCommentaryTrack(t) &&
          _normalizeLanguageCode(t.language) === prefNorm
      );
      if (mainPrefTracks.length > 0) {
        return pickFromPool(mainPrefTracks);
      }
    }

    const nonCommentary = audioTracks.filter((t) => !_isCommentaryTrack(t));
    if (nonCommentary.length > 0) {
      return pickFromPool(nonCommentary);
    }
    return pickFromPool(audioTracks);
  }

  function getRecommendedAddAudioOptions(
    editorState,
    playbackProfile = null,
    userSettings = null
  ) {
    if (!editorState) return [];
    const audioTracks = editorState.tracks.filter((t) => t.type === "audio");
    if (audioTracks.length === 0) return [];

    const prefs = userSettings && typeof userSettings === "object" ? userSettings : null;
    const primaryTrack = _pickPrimaryBaseAudioTrack(
      audioTracks,
      prefs ? prefs.preferred_language : null
    );

    const srcChannels = Number(primaryTrack.audio_channels || 0) || EDITOR.MAX_DOLBY_CHANNELS;
    const canonical = canonicalizeAudioCodec(primaryTrack.codec, primaryTrack.codec_id);
    const lossless = isLosslessAudioTrack(primaryTrack);

    // Build top 2-3 smart recommendations dynamically from source characteristics
    const specs = [];
    if (lossless && srcChannels > EDITOR.MAX_DOLBY_CHANNELS) {
      specs.push(
        { codec: EDITOR.CODEC_FLAC, targetChannels: null, badge: "Lossless" },
        { codec: EDITOR.CODEC_EAC3, targetChannels: null, badge: "AVR / TV" },
        { codec: EDITOR.CODEC_OPUS, targetChannels: null, badge: "Compact 7.1" }
      );
    } else if (lossless) {
      specs.push(
        { codec: EDITOR.CODEC_EAC3, targetChannels: null, badge: "Recommended" },
        { codec: EDITOR.CODEC_FLAC, targetChannels: null, badge: "Lossless" },
        { codec: EDITOR.CODEC_AC3, targetChannels: null, badge: "Legacy" }
      );
    } else if (canonical === EDITOR.CODEC_AAC && srcChannels > EDITOR.MAX_DOLBY_CHANNELS) {
      specs.push(
        { codec: EDITOR.CODEC_EAC3, targetChannels: null, badge: "AVR / TV" },
        { codec: EDITOR.CODEC_OPUS, targetChannels: null, badge: `Keeps ${formatChannelLabel(srcChannels)}` },
        { codec: EDITOR.CODEC_AC3, targetChannels: null, badge: "Legacy" }
      );
    } else if (canonical === EDITOR.CODEC_AAC) {
      specs.push(
        { codec: EDITOR.CODEC_EAC3, targetChannels: null, badge: "Recommended" },
        { codec: EDITOR.CODEC_AC3, targetChannels: null, badge: "Legacy" }
      );
    } else if (canonical === EDITOR.CODEC_EAC3) {
      specs.push(
        { codec: EDITOR.CODEC_AC3, targetChannels: null, badge: "Legacy" },
        { codec: EDITOR.CODEC_AAC, targetChannels: srcChannels > 2 ? 2 : null, badge: srcChannels > 2 ? "Stereo" : "Universal" }
      );
    } else if (canonical === EDITOR.CODEC_AC3) {
      specs.push(
        { codec: EDITOR.CODEC_AAC, targetChannels: srcChannels > 2 ? 2 : null, badge: srcChannels > 2 ? "Stereo" : "Universal" },
        { codec: EDITOR.CODEC_OPUS, targetChannels: null, badge: "Modern" }
      );
    } else {
      specs.push(
        { codec: EDITOR.CODEC_EAC3, targetChannels: null, badge: "Recommended" },
        { codec: EDITOR.CODEC_AC3, targetChannels: null, badge: "Legacy" },
        { codec: EDITOR.CODEC_OPUS, targetChannels: null, badge: "Modern" }
      );
    }

    // Promote preferred target codec:
    // 1. Explicit Default Target Codec (when not 'auto') wins as 'Default'
    // 2. Otherwise ('auto'), configured Playback Profile wins as 'Your System' (supports all 5 codecs)
    // 3. Otherwise (neutral profile), legacy default_dolby_codec === 'ac3' promotes AC-3 as 'Default'
    const validTargets = EDITOR.ALL_TARGET_CODECS || ["eac3", "ac3", "aac", "opus", "flac"];
    const explicitDefaultCodec =
      prefs && validTargets.includes(prefs.default_audio_codec)
        ? prefs.default_audio_codec
        : null;
    const hasConfiguredProfile = Boolean(
      playbackProfile &&
        playbackProfile.preset &&
        playbackProfile.preset !== "neutral" &&
        validTargets.includes(playbackProfile.preferredCodec)
    );

    const promoteCodec = (prefCodec, prefTargetCh, badgeLabel) => {
      const check = isConversionCombinationBlocked(primaryTrack, prefCodec, prefTargetCh);
      if (check.blocked) return;
      const existingIdx = specs.findIndex(
        (s) => s.codec === prefCodec && (s.targetChannels ?? null) === prefTargetCh
      );
      if (existingIdx >= 0) {
        const [matched] = specs.splice(existingIdx, 1);
        specs.unshift({ ...matched, badge: badgeLabel });
      } else {
        specs.unshift({
          codec: prefCodec,
          targetChannels: prefTargetCh,
          badge: badgeLabel,
        });
        if (specs.length > 3) specs.pop();
      }
    };

    if (explicitDefaultCodec) {
      const prefMaxCh = hasConfiguredProfile
        ? Number(playbackProfile.maxChannels || EDITOR.MAX_AUDIO_CHANNELS)
        : Number(getCodecMeta(explicitDefaultCodec).maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
      const prefTargetCh =
        hasConfiguredProfile && srcChannels > prefMaxCh ? prefMaxCh : null;
      promoteCodec(explicitDefaultCodec, prefTargetCh, "Default");
    } else if (hasConfiguredProfile) {
      const prefCodec = playbackProfile.preferredCodec;
      const prefMaxCh = Number(playbackProfile.maxChannels || EDITOR.MAX_DOLBY_CHANNELS);
      const prefTargetCh = srcChannels > prefMaxCh ? prefMaxCh : null;
      promoteCodec(prefCodec, prefTargetCh, "Your System");
    } else if (prefs && prefs.default_dolby_codec === EDITOR.CODEC_AC3) {
      promoteCodec(EDITOR.CODEC_AC3, null, "Default");
    }

    return specs.map((spec) => {
      const meta = getCodecMeta(spec.codec);
      const outChannels = resolveTargetChannels(srcChannels, spec.codec, spec.targetChannels);
      const chLabel = formatChannelLabel(outChannels);
      const bitrateCfg = getBitrateConfigFor(spec.codec, outChannels);
      return {
        key: `${spec.codec}-${spec.targetChannels ?? "match"}`,
        codec: spec.codec,
        targetChannels: spec.targetChannels,
        resolvedChannels: outChannels,
        label: `${meta.displayLabel || meta.label} ${chLabel}`,
        subtitle: meta.subtitle,
        badge: spec.badge || null,
        defaultBitrate: bitrateCfg.defaultBitrate,
        bitrates: bitrateCfg.options,
        isLossless: Boolean(meta.isLossless),
        preferredSourceTrackId: primaryTrack.id,
        eligibleTracks: audioTracks,
      };
    });
  }

  function resolveSmartFillFormat(rawFormat) {
    const cleaned = String(rawFormat || "").trim().toLowerCase();
    if (cleaned === "compact" || cleaned === "lang_codec" || cleaned === "detailed") {
      return cleaned;
    }
    return "detailed";
  }

  function resolveLanguageDisplayName(langCode) {
    const code = String(langCode || "").trim().toLowerCase();
    if (!code || code === EDITOR.DEFAULT_LANGUAGE) return "";
    const mapped =
      EDITOR.LANGUAGE_DISPLAY_NAMES && EDITOR.LANGUAGE_DISPLAY_NAMES[code];
    return mapped || code.toUpperCase();
  }

  function buildDefaultAudioTitle(
    langCode,
    targetCodec,
    targetChannels,
    bitrate,
    format = "detailed"
  ) {
    const fmt = resolveSmartFillFormat(format);
    const normalizedLang = String(langCode || EDITOR.DEFAULT_LANGUAGE).toLowerCase();
    const meta = getCodecMeta(targetCodec);
    const resolvedCh = resolveTargetChannels(targetChannels, targetCodec, targetChannels);
    const layout = formatChannelLabel(resolvedCh);
    const spec = `${meta.label} ${layout}`;
    const hasLang = Boolean(normalizedLang && normalizedLang !== EDITOR.DEFAULT_LANGUAGE);

    if (fmt === "compact") {
      return spec;
    }
    if (fmt === "lang_codec") {
      const langName = hasLang ? resolveLanguageDisplayName(normalizedLang) : "";
      return langName ? `${langName} · ${spec}` : spec;
    }

    const pieces = [spec];
    if (!meta.isLossless) {
      const resolvedBitrate = String(
        bitrate || getBitrateConfigFor(targetCodec, resolvedCh).defaultBitrate || ""
      ).trim();
      if (resolvedBitrate) {
        pieces.push(resolvedBitrate);
      }
    }
    if (hasLang) {
      pieces.push(normalizedLang);
    }
    pieces.push("muxed");
    return pieces.join(" • ");
  }

  function isAac51Track(track) {
    if (!track || track.type !== "audio") return false;
    return (
      Number(track.audio_channels) === EDITOR.MAX_DOLBY_CHANNELS &&
      canonicalizeAudioCodec(track.codec, track.codec_id) === "aac"
    );
  }

  function isSurroundTrack(track) {
    if (!track || track.type !== "audio") return false;
    return Number(track.audio_channels) >= EDITOR.MAX_DOLBY_CHANNELS;
  }

  function isMp4File(fileDetail) {
    if (!fileDetail || !fileDetail.filename) return false;
    return String(fileDetail.filename).toLowerCase().endsWith(".mp4");
  }

  function stripFileExtension(filename) {
    const raw = String(filename || "").trim();
    const lastDot = raw.lastIndexOf(".");
    if (lastDot <= 0) return raw;
    return raw.slice(0, lastDot);
  }

  function ensureMkvFilename(rawName) {
    const trimmed = String(rawName || "").trim();
    if (!trimmed) return "";
    return trimmed.toLowerCase().endsWith(".mkv") ? trimmed : `${trimmed}.mkv`;
  }

  function normalizeDisplayFolder(rawFolder) {
    const trimmed = String(rawFolder || "").trim().replace(/^\/+|\/+$/g, "");
    if (!trimmed) return "/";
    return `/${trimmed}`;
  }

  function buildOutputPreview(editorState, fileDetail) {
    const originalFilename = (fileDetail && fileDetail.filename) || "";
    const originalFolder = normalizeDisplayFolder(fileDetail && fileDetail.folder);
    const originalRelPath = (fileDetail && fileDetail.filepath) || "";
    const stem = stripFileExtension(originalFilename);
    const mp4 = isMp4File(fileDetail);

    const replaceFilename = mp4 ? `${stem}.mkv` : originalFilename;
    const keepBothFilename = `${stem}.remux.mkv`;
    const mode = (editorState && editorState.outputMode) || EDITOR.OUTPUT_KEEP_BOTH;

    if (mode === EDITOR.OUTPUT_KEEP_BOTH) {
      const folderPrefix = originalFolder === "/" ? "" : originalFolder.slice(1);
      const targetRelPath = folderPrefix
        ? `${folderPrefix}/${keepBothFilename}`
        : keepBothFilename;
      return {
        mode,
        isMp4: mp4,
        originalFilename,
        originalFolder,
        targetFilename: keepBothFilename,
        targetFolder: originalFolder,
        targetRelPath,
        targetDisplayPath: `/${targetRelPath}`,
        isSaveAsInvalid: false,
        validationError: null,
      };
    }

    if (mode === EDITOR.OUTPUT_SAVE_AS) {
      const saveAs = (editorState && editorState.saveAs) || {};
      const rawFolder = saveAs.folder || originalFolder;
      const rawFilename = String(saveAs.filename || "").trim();

      let validationError = null;
      if (!rawFilename) {
        validationError = "Enter an output filename.";
      } else if (rawFilename.includes("/") || rawFilename.includes("\\")) {
        validationError = "Filename must not contain slashes; set subfolders in Destination Folder.";
      } else if (rawFilename === "." || rawFilename === ".." || rawFilename.startsWith("._")) {
        validationError = "Enter a valid media filename.";
      } else if (rawFolder.includes("\\")) {
        validationError = "Folder path must use forward slashes (/).";
      } else if (
        rawFolder
          .split("/")
          .some((segment) => segment === ".." || segment === ".")
      ) {
        validationError = "Folder path cannot contain '..' or '.' segments.";
      }

      const targetFilename = ensureMkvFilename(rawFilename);
      if (
        !validationError &&
        targetFilename.toLowerCase().includes(EDITOR.TEMP_FILE_MARKER)
      ) {
        validationError = "Filename cannot contain reserved '.mkvforge.' marker.";
      }

      const targetFolder = normalizeDisplayFolder(rawFolder);
      const folderPrefix = targetFolder === "/" ? "" : targetFolder.slice(1);
      const targetRelPath = targetFilename
        ? folderPrefix
          ? `${folderPrefix}/${targetFilename}`
          : targetFilename
        : "";

      if (
        !validationError &&
        targetRelPath &&
        originalRelPath &&
        targetRelPath.toLowerCase() === originalRelPath.toLowerCase()
      ) {
        validationError =
          "Destination matches the original file. Choose another folder/filename or switch to Replace Original.";
      }

      return {
        mode,
        isMp4: mp4,
        originalFilename,
        originalFolder,
        targetFilename: targetFilename || `${stem}.mkv`,
        targetFolder,
        targetRelPath,
        targetDisplayPath: targetRelPath ? `/${targetRelPath}` : targetFolder,
        isSaveAsInvalid: Boolean(validationError),
        validationError,
      };
    }

    // Default: Replace Original
    const folderPrefix = originalFolder === "/" ? "" : originalFolder.slice(1);
    const targetRelPath = folderPrefix
      ? `${folderPrefix}/${replaceFilename}`
      : replaceFilename;
    return {
      mode: EDITOR.OUTPUT_REPLACE,
      isMp4: mp4,
      originalFilename,
      originalFolder,
      targetFilename: replaceFilename,
      targetFolder: originalFolder,
      targetRelPath,
      targetDisplayPath: `/${targetRelPath}`,
      isSaveAsInvalid: false,
      validationError: null,
    };
  }

  function createEditorState(fileDetail, userSettings = null) {
    const prefs = userSettings && typeof userSettings === "object" ? userSettings : {};
    const validOutputModes = [
      EDITOR.OUTPUT_KEEP_BOTH,
      EDITOR.OUTPUT_REPLACE,
      EDITOR.OUTPUT_SAVE_AS,
    ];
    const defaultOutputMode = validOutputModes.includes(prefs.default_output_mode)
      ? prefs.default_output_mode
      : EDITOR.OUTPUT_KEEP_BOTH;
    const defaultDolbyCodec = resolveDefaultAudioCodec(prefs);
    const defaultQuality = resolveDefaultAudioQuality(prefs);
    const defaultDolbyBitrate = resolveQualityBitrate(
      defaultDolbyCodec,
      EDITOR.MAX_DOLBY_CHANNELS,
      defaultQuality
    );

    const removeAllMetadata = prefs.default_remove_all_metadata !== false;

    const editableTracks = (fileDetail.tracks || [])
      .filter((t) => EDITOR.SUPPORTED_TRACK_TYPES.includes(t.type))
      .map((t) => ({
        id: t.id,
        type: t.type,
        codec: t.codec || "",
        codec_id: t.codec_id || null,
        language: t.language || EDITOR.DEFAULT_LANGUAGE,
        original_language: t.language || EDITOR.DEFAULT_LANGUAGE,
        track_name: removeAllMetadata ? "" : (t.track_name || ""),
        original_track_name: t.track_name || "",
        default_track: Boolean(t.default_track),
        original_default_track: Boolean(t.default_track),
        forced_track: Boolean(t.forced_track),
        original_forced_track: Boolean(t.forced_track),
        audio_channels: t.audio_channels ?? null,
        audio_index: t.audio_index ?? null,
        pixel_dimensions: t.pixel_dimensions || null,
        minimum_timestamp_ns: Number(t.minimum_timestamp_ns || 0),
        sync_delay_ms: Number(t.sync_delay_ms || 0),
        size_bytes:
          t.size_bytes !== null &&
          t.size_bytes !== undefined &&
          Number.isFinite(Number(t.size_bytes)) &&
          Number(t.size_bytes) > 0
            ? Number(t.size_bytes)
            : null,
        enabled: true,
      }));

    const initialOrderIds = editableTracks.map((t) => t.id);
    const initialTitle = fileDetail.container_title || "";
    const initialFolder = normalizeDisplayFolder(fileDetail.folder);
    const defaultSaveAsFilename = `${stripFileExtension(fileDetail.filename)}.mkv`;
    const editableAttachments = (fileDetail.attachments || []).map((att) => ({
      id: att.id,
      file_name: att.file_name || `attachment_${att.id}`,
      content_type: att.content_type || "application/octet-stream",
      size_bytes: Number(att.size_bytes || 0),
      description: att.description || "",
      uid: att.uid || null,
      enabled: !removeAllMetadata,
    }));
    const tagEntries = Array.isArray(fileDetail.tag_entries)
      ? fileDetail.tag_entries
      : [];
    const hasGlobalTagsPresent = Boolean(
      fileDetail.has_global_tags ||
        Number(fileDetail.global_tags_count || 0) > 0 ||
        tagEntries.length > 0 ||
        fileDetail.has_metadata
    );
    const hasTrackTagsPresent = Boolean(
      fileDetail.has_track_tags || Number(fileDetail.track_tags_count || 0) > 0
    );
    const hasAttachmentsPresent = Boolean(
      editableAttachments.length > 0 ||
        Number(fileDetail.attachments_count || 0) > 0
    );

    return {
      containerTitle: removeAllMetadata ? "" : initialTitle,
      originalContainerTitle: initialTitle,
      stripGlobalTags: Boolean(
        prefs.default_strip_global_tags || (removeAllMetadata && hasGlobalTagsPresent)
      ),
      stripTrackTags: removeAllMetadata ? hasTrackTagsPresent : false,
      stripAttachments: removeAllMetadata ? hasAttachmentsPresent : false,
      stripChapters: false,
      attachments: editableAttachments,
      tagEntries,
      initialTrackOrderIds: initialOrderIds,
      audioFix: {
        enabled: false,
        sourceTrackId: null,
        targetCodec: defaultDolbyCodec,
        targetChannels: null,
        bitrate: defaultDolbyBitrate,
        language: prefs.preferred_language || EDITOR.DEFAULT_LANGUAGE,
        trackTitle: "",
        titleManuallyEdited: false,
        setAsDefault: true,
        dropSourceTrack: false,
      },
      tracks: editableTracks,
      externalTracks: [],
      trackOrderKeys: initialOrderIds.map((id) => `src:${id}`),
      nextExternalSeq: 1,
      outputMode: defaultOutputMode,
      saveAs: {
        folder: initialFolder,
        filename: defaultSaveAsFilename,
        deleteOriginal: false,
      },
      advancedOpen: false,
      confirmNoVideo: false,
    };
  }

  function toggleAttachmentEnabled(editorState, attId) {
    if (!editorState || !Array.isArray(editorState.attachments)) return;
    editorState.attachments = editorState.attachments.map((att) =>
      att.id === attId ? { ...att, enabled: !att.enabled } : att
    );
    editorState.stripAttachments =
      editorState.attachments.length > 0 &&
      editorState.attachments.every((att) => !att.enabled);
  }

  function setAllAttachmentsEnabled(editorState, enabled) {
    if (!editorState) return;
    const keep = Boolean(enabled);
    if (Array.isArray(editorState.attachments) && editorState.attachments.length > 0) {
      editorState.attachments = editorState.attachments.map((att) => ({
        ...att,
        enabled: keep,
      }));
    }
    editorState.stripAttachments = !keep;
  }

  function findSelectedSourceAudio(editorState) {
    if (!editorState || editorState.audioFix.sourceTrackId === null) return null;
    return (
      editorState.tracks.find(
        (t) => t.type === "audio" && t.id === Number(editorState.audioFix.sourceTrackId)
      ) || null
    );
  }

  function _syncTracksFromOrderKeys(editorState) {
    if (!editorState || !Array.isArray(editorState.trackOrderKeys)) return;
    const byId = new Map(editorState.tracks.map((t) => [t.id, t]));
    const ordered = [];
    editorState.trackOrderKeys.forEach((key) => {
      if (key.startsWith("src:")) {
        const id = Number(key.slice(4));
        const found = byId.get(id);
        if (found) ordered.push(found);
      }
    });
    if (ordered.length === editorState.tracks.length) {
      editorState.tracks = ordered;
    }
  }

  function moveTrack(tracks, index, delta) {
    const targetIdx = index + delta;
    if (index < 0 || targetIdx < 0 || index >= tracks.length || targetIdx >= tracks.length) {
      return tracks;
    }
    const copy = [...tracks];
    const [moved] = copy.splice(index, 1);
    copy.splice(targetIdx, 0, moved);
    return copy;
  }

  function moveOrderedTrack(editorState, orderKey, delta) {
    if (!editorState || !Array.isArray(editorState.trackOrderKeys)) return;
    const idx = editorState.trackOrderKeys.indexOf(orderKey);
    const targetIdx = idx + delta;
    if (
      idx < 0 ||
      targetIdx < 0 ||
      targetIdx >= editorState.trackOrderKeys.length
    ) {
      return;
    }
    const nextKeys = [...editorState.trackOrderKeys];
    const [moved] = nextKeys.splice(idx, 1);
    nextKeys.splice(targetIdx, 0, moved);
    editorState.trackOrderKeys = nextKeys;
    _syncTracksFromOrderKeys(editorState);
  }

  function buildDefaultExternalTitle(langCode, kind, originalFilename) {
    const normalizedLang = String(langCode || EDITOR.DEFAULT_LANGUAGE).toLowerCase();
    const langName = EDITOR.LANGUAGE_DISPLAY_NAMES[normalizedLang] || "";
    if (langName) {
      return kind === EXTERNAL_TRACKS.KIND_AUDIO
        ? `${langName} Audio`
        : langName;
    }
    return stripFileExtension(originalFilename || "");
  }

  function upsertExternalTrack(editorState, draft) {
    if (!editorState || !draft) return null;
    const list = Array.isArray(editorState.externalTracks)
      ? [...editorState.externalTracks]
      : [];
    const existingIdx = draft.id
      ? list.findIndex((item) => item.id === draft.id)
      : -1;
    const id =
      existingIdx >= 0
        ? draft.id
        : `ext-${editorState.nextExternalSeq++}`;

    const entry = {
      id,
      kind: draft.kind === EXTERNAL_TRACKS.KIND_AUDIO
        ? EXTERNAL_TRACKS.KIND_AUDIO
        : EXTERNAL_TRACKS.KIND_SUBTITLES,
      source: draft.source === EXTERNAL_TRACKS.SOURCE_UPLOAD
        ? EXTERNAL_TRACKS.SOURCE_UPLOAD
        : EXTERNAL_TRACKS.SOURCE_STORAGE,
      path: draft.path || null,
      uploadId: draft.uploadId || null,
      originalFilename: String(draft.originalFilename || "").trim(),
      sizeBytes: Number(draft.sizeBytes || 0),
      trackName: String(draft.trackName || "").trim(),
      language: String(draft.language || EDITOR.DEFAULT_LANGUAGE).trim() || EDITOR.DEFAULT_LANGUAGE,
      defaultTrack: Boolean(draft.defaultTrack),
      forcedTrack: Boolean(draft.forcedTrack),
      syncDelayMs: Math.max(
        EXTERNAL_TRACKS.MIN_SYNC_DELAY_MS,
        Math.min(
          EXTERNAL_TRACKS.MAX_SYNC_DELAY_MS,
          parseInt(String(draft.syncDelayMs || 0), 10) || 0
        )
      ),
    };

    // Enforce single-default among imported tracks of the same kind
    if (entry.defaultTrack) {
      list.forEach((other, idx) => {
        if (idx !== existingIdx && other.kind === entry.kind) {
          other.defaultTrack = false;
        }
      });
    }

    if (existingIdx >= 0) {
      list[existingIdx] = entry;
      editorState.externalTracks = list;
    } else {
      list.push(entry);
      editorState.externalTracks = list;

      const orderKey = `ext:${id}`;
      const keys = Array.isArray(editorState.trackOrderKeys)
        ? [...editorState.trackOrderKeys]
        : editorState.tracks.map((t) => `src:${t.id}`);
      const trackById = new Map(editorState.tracks.map((t) => [t.id, t]));
      const extById = new Map(list.map((e) => [e.id, e]));

      // Insert after the last track of the same kind (audio after audio, subs at end)
      let insertAt = keys.length;
      if (entry.kind === EXTERNAL_TRACKS.KIND_AUDIO) {
        let lastAudioOrVideo = -1;
        keys.forEach((k, i) => {
          if (k.startsWith("src:")) {
            const tr = trackById.get(Number(k.slice(4)));
            if (tr && (tr.type === "audio" || tr.type === "video")) {
              lastAudioOrVideo = i;
            }
          } else if (k.startsWith("ext:")) {
            const ex = extById.get(k.slice(4));
            if (ex && ex.kind === EXTERNAL_TRACKS.KIND_AUDIO) {
              lastAudioOrVideo = i;
            }
          }
        });
        insertAt = lastAudioOrVideo + 1;
      }
      keys.splice(insertAt, 0, orderKey);
      editorState.trackOrderKeys = keys;
    }

    return entry;
  }

  function removeExternalTrack(editorState, extId) {
    if (!editorState || !extId) return null;
    const removed =
      (editorState.externalTracks || []).find((e) => e.id === extId) || null;
    editorState.externalTracks = (editorState.externalTracks || []).filter(
      (e) => e.id !== extId
    );
    editorState.trackOrderKeys = (editorState.trackOrderKeys || []).filter(
      (k) => k !== `ext:${extId}`
    );
    return removed;
  }

  function buildDisplayTracks(editorState) {
    if (!editorState) return [];
    const rows = [];
    const audioFix = editorState.audioFix;
    const sourceAudio = findSelectedSourceAudio(editorState);
    const showVirtual = Boolean(audioFix.enabled && sourceAudio);
    const meta = getCodecMeta(audioFix.targetCodec);
    const resolvedCh = resolveTargetChannels(
      sourceAudio ? sourceAudio.audio_channels : EDITOR.MAX_DOLBY_CHANNELS,
      audioFix.targetCodec,
      audioFix.targetChannels
    );
    const channelLabel = formatChannelLabel(resolvedCh);

    const externalTracks = Array.isArray(editorState.externalTracks)
      ? editorState.externalTracks
      : [];
    const extById = new Map(externalTracks.map((e) => [e.id, e]));
    const trackById = new Map(editorState.tracks.map((t) => [t.id, t]));
    const trackIdxById = new Map(editorState.tracks.map((t, idx) => [t.id, idx]));

    const orderKeys =
      Array.isArray(editorState.trackOrderKeys) && editorState.trackOrderKeys.length > 0
        ? editorState.trackOrderKeys
        : [
            ...editorState.tracks.map((t) => `src:${t.id}`),
            ...externalTracks.map((e) => `ext:${e.id}`),
          ];

    const convertedOwnsAudioDefault = Boolean(showVirtual && audioFix.setAsDefault);
    const defaultExtAudio = convertedOwnsAudioDefault
      ? null
      : externalTracks.find(
          (e) => e.kind === EXTERNAL_TRACKS.KIND_AUDIO && e.defaultTrack
        ) || null;
    const defaultExtSub =
      externalTracks.find(
        (e) => e.kind === EXTERNAL_TRACKS.KIND_SUBTITLES && e.defaultTrack
      ) || null;

    const virtualRow = showVirtual
      ? {
          isVirtual: true,
          isExternal: false,
          key: "__new_converted_audio__",
          title: audioFix.trackTitle || `${meta.label} ${channelLabel}`,
          codecBadge: `${meta.label} ${channelLabel}`,
          language: audioFix.language || EDITOR.DEFAULT_LANGUAGE,
          bitrate: meta.isLossless ? "Lossless" : audioFix.bitrate,
          isLossless: Boolean(meta.isLossless),
          setAsDefault: Boolean(audioFix.setAsDefault),
          dropSourceTrack: Boolean(audioFix.dropSourceTrack),
          sourceTrackId: sourceAudio.id,
          sourceAudioIndex: sourceAudio.audio_index,
          sourceSummary: `from Track #${sourceAudio.id} (Audio #${sourceAudio.audio_index ?? 0} • ${sourceAudio.codec} ${formatChannelLabel(sourceAudio.audio_channels)} • ${sourceAudio.language})`,
          syncDelayMs: sourceAudio ? sourceAudio.sync_delay_ms : 0,
        }
      : null;

    let insertedVirtual = false;

    orderKeys.forEach((orderKey, orderPos) => {
      if (orderKey.startsWith("src:")) {
        const trackId = Number(orderKey.slice(4));
        const track = trackById.get(trackId);
        if (!track) return;

        const isDroppedByAudioFix = Boolean(
          showVirtual &&
            audioFix.dropSourceTrack &&
            sourceAudio &&
            track.id === sourceAudio.id
        );
        const isDemotedByNewDolby = Boolean(
          !isDroppedByAudioFix &&
            track.enabled &&
            track.default_track &&
            ((track.type === "audio" &&
              (convertedOwnsAudioDefault || Boolean(defaultExtAudio))) ||
              (track.type === "subtitles" && Boolean(defaultExtSub)))
        );

        if (showVirtual && !insertedVirtual && track.type === "audio") {
          rows.push(virtualRow);
          insertedVirtual = true;
        }

        rows.push({
          isVirtual: false,
          isExternal: false,
          key: `track-${track.id}`,
          orderKey,
          orderPos,
          canMoveUp: orderPos > 0,
          canMoveDown: orderPos < orderKeys.length - 1,
          index: trackIdxById.get(track.id) ?? 0,
          track,
          isDroppedByAudioFix,
          isDemotedByNewDolby,
        });
        return;
      }

      if (orderKey.startsWith("ext:")) {
        const extId = orderKey.slice(4);
        const ext = extById.get(extId);
        if (!ext) return;

        if (
          showVirtual &&
          !insertedVirtual &&
          ext.kind === EXTERNAL_TRACKS.KIND_AUDIO
        ) {
          rows.push(virtualRow);
          insertedVirtual = true;
        }

        const effectiveDefault =
          ext.kind === EXTERNAL_TRACKS.KIND_AUDIO
            ? Boolean(defaultExtAudio && defaultExtAudio.id === ext.id)
            : Boolean(defaultExtSub && defaultExtSub.id === ext.id);

        rows.push({
          isVirtual: false,
          isExternal: true,
          key: `ext-${ext.id}`,
          orderKey,
          orderPos,
          canMoveUp: orderPos > 0,
          canMoveDown: orderPos < orderKeys.length - 1,
          ext,
          effectiveDefault,
        });
      }
    });

    if (showVirtual && !insertedVirtual) {
      let lastVideoPos = -1;
      rows.forEach((entry, i) => {
        if (!entry.isVirtual && !entry.isExternal && entry.track.type === "video") {
          lastVideoPos = i;
        }
      });
      rows.splice(lastVideoPos + 1, 0, virtualRow);
    }

    return rows;
  }

  function hasUncheckedAllVideo(editorState) {
    if (!editorState) return false;
    const videoTracks = editorState.tracks.filter((t) => t.type === "video");
    if (videoTracks.length === 0) return false;
    return videoTracks.every((t) => !t.enabled);
  }

  function countEffectiveKeptTracks(editorState) {
    if (!editorState) return 0;
    const sourceAudio = findSelectedSourceAudio(editorState);
    const hasNewAudio = Boolean(editorState.audioFix.enabled && sourceAudio);
    const droppedId =
      hasNewAudio && editorState.audioFix.dropSourceTrack ? sourceAudio.id : null;
    const keptSource = editorState.tracks.filter(
      (t) => t.enabled && t.id !== droppedId
    ).length;
    const importedCount = Array.isArray(editorState.externalTracks)
      ? editorState.externalTracks.length
      : 0;
    return keptSource + (hasNewAudio ? 1 : 0) + importedCount;
  }

  function hasPendingChanges(editorState, fileDetail) {
    if (!editorState || !fileDetail) return false;
    if (editorState.audioFix.enabled && findSelectedSourceAudio(editorState)) {
      return true;
    }
    if (
      Array.isArray(editorState.externalTracks) &&
      editorState.externalTracks.length > 0
    ) {
      return true;
    }
    if (editorState.containerTitle !== editorState.originalContainerTitle) {
      return true;
    }
    if (
      editorState.stripGlobalTags ||
      editorState.stripTrackTags ||
      editorState.stripAttachments ||
      editorState.stripChapters
    ) {
      return true;
    }
    if (
      Array.isArray(editorState.attachments) &&
      editorState.attachments.some((att) => !att.enabled)
    ) {
      return true;
    }
    if (isMp4File(fileDetail)) {
      return true;
    }
    if (editorState.outputMode === EDITOR.OUTPUT_SAVE_AS) {
      return true;
    }

    const currentOrder = editorState.tracks.map((t) => t.id);
    if (
      currentOrder.length !== editorState.initialTrackOrderIds.length ||
      currentOrder.some((id, idx) => id !== editorState.initialTrackOrderIds[idx])
    ) {
      return true;
    }

    return editorState.tracks.some(
      (t) =>
        !t.enabled ||
        t.track_name !== t.original_track_name ||
        t.language !== t.original_language ||
        t.default_track !== t.original_default_track ||
        t.forced_track !== t.original_forced_track
    );
  }

  function buildPendingSummary(editorState, fileDetail) {
    if (!editorState || !fileDetail) return "";
    if (!hasPendingChanges(editorState, fileDetail)) {
      return "No changes selected";
    }

    const parts = [];
    const sourceAudio = findSelectedSourceAudio(editorState);

    if (editorState.audioFix.enabled && sourceAudio) {
      const meta = getCodecMeta(editorState.audioFix.targetCodec);
      const resolvedCh = resolveTargetChannels(
        sourceAudio.audio_channels,
        editorState.audioFix.targetCodec,
        editorState.audioFix.targetChannels
      );
      const ch = formatChannelLabel(resolvedCh);
      parts.push(`+ ${meta.label} ${ch} (from #${sourceAudio.id})`);
      if (editorState.audioFix.dropSourceTrack) {
        parts.push("Drop Base Audio");
      }
    }

    const externalTracks = Array.isArray(editorState.externalTracks)
      ? editorState.externalTracks
      : [];
    const importedSubCount = externalTracks.filter(
      (e) => e.kind === EXTERNAL_TRACKS.KIND_SUBTITLES
    ).length;
    const importedAudioCount = externalTracks.filter(
      (e) => e.kind === EXTERNAL_TRACKS.KIND_AUDIO
    ).length;
    if (importedAudioCount > 0) {
      parts.push(
        `+ ${importedAudioCount} Imported ${importedAudioCount === 1 ? "Audio" : "Audio Tracks"}`
      );
    }
    if (importedSubCount > 0) {
      parts.push(
        `+ ${importedSubCount} Imported ${importedSubCount === 1 ? "Sub" : "Subs"}`
      );
    }

    if (
      editorState.containerTitle === "" &&
      editorState.originalContainerTitle !== ""
    ) {
      parts.push("Clear Title");
    } else if (editorState.containerTitle !== editorState.originalContainerTitle) {
      parts.push("Update Title");
    }

    if (
      editorState.stripGlobalTags ||
      editorState.stripTrackTags ||
      editorState.stripAttachments ||
      editorState.stripChapters
    ) {
      parts.push("Strip Metadata");
    }

    const attachments = Array.isArray(editorState.attachments)
      ? editorState.attachments
      : [];
    const droppedAttachments = attachments.filter((att) => !att.enabled).length;
    if (
      !editorState.stripAttachments &&
      droppedAttachments > 0 &&
      droppedAttachments < attachments.length
    ) {
      parts.push(
        `Drop ${droppedAttachments} ${droppedAttachments === 1 ? "Attachment" : "Attachments"}`
      );
    }

    const excludedCount = editorState.tracks.filter((t) => !t.enabled).length;
    if (excludedCount > 0) {
      parts.push(`Drop ${excludedCount} ${excludedCount === 1 ? "Track" : "Tracks"}`);
    }

    const preview = buildOutputPreview(editorState, fileDetail);
    if (editorState.outputMode === EDITOR.OUTPUT_SAVE_AS) {
      parts.push(`Save as ${preview.targetDisplayPath}`);
    } else if (editorState.outputMode === EDITOR.OUTPUT_KEEP_BOTH) {
      parts.push(`Keep Both (${preview.targetFilename})`);
    } else if (isMp4File(fileDetail)) {
      parts.push(`${preview.originalFilename} → ${preview.targetFilename}`);
    }

    return parts.length > 0 ? parts.join(" • ") : "Update track metadata / order";
  }

  function formatTrackBadge(track, format = "detailed") {
    const fmt = resolveSmartFillFormat(format);
    const codec = (track && track.codec) || "Unknown";
    const langCode = String((track && track.language) || "").trim();
    const hasLang = Boolean(
      langCode && langCode.toLowerCase() !== EDITOR.DEFAULT_LANGUAGE
    );
    const langName = hasLang ? resolveLanguageDisplayName(langCode) : "";

    if (fmt === "compact") {
      if (track && track.type === "audio") {
        return track.audio_channels
          ? `${codec} ${formatChannelLabel(track.audio_channels)}`
          : codec;
      }
      if (track && track.type === "video") {
        return track.pixel_dimensions ? `${codec} ${track.pixel_dimensions}` : codec;
      }
      return langName ? `${langName} ${codec}` : codec;
    }

    if (fmt === "lang_codec") {
      if (track && track.type === "audio") {
        const spec = track.audio_channels
          ? `${codec} ${formatChannelLabel(track.audio_channels)}`
          : codec;
        return langName ? `${langName} · ${spec}` : spec;
      }
      if (track && track.type === "video") {
        return track.pixel_dimensions ? `${codec} · ${track.pixel_dimensions}` : codec;
      }
      return langName ? `${langName} · ${codec}` : codec;
    }

    // Default ('detailed'): exact original format (`Audio • EAC3 • 5.1 • eng`)
    const typeLabel =
      track && track.type === "video"
        ? "Video"
        : track && track.type === "audio"
          ? "Audio"
          : "Sub";
    const pieces = [typeLabel, codec];
    if (track && track.type === "audio" && track.audio_channels) {
      pieces.push(formatChannelLabel(track.audio_channels));
    } else if (track && track.type === "video" && track.pixel_dimensions) {
      pieces.push(track.pixel_dimensions);
    }
    if (hasLang) {
      pieces.push(langCode);
    }
    return pieces.join(" • ");
  }

  function getSmartFillPreview(format = "detailed") {
    const fmt = resolveSmartFillFormat(format);
    return {
      audio: formatTrackBadge(
        { type: "audio", codec: "EAC3", audio_channels: 6, language: "eng" },
        fmt
      ),
      video: formatTrackBadge(
        { type: "video", codec: "HEVC", pixel_dimensions: "1920x1080", language: "und" },
        fmt
      ),
      subtitle: formatTrackBadge(
        { type: "subtitles", codec: "SRT", language: "eng" },
        fmt
      ),
      converted: buildDefaultAudioTitle("eng", "eac3", 6, "640k", fmt),
    };
  }

  function buildJobRequestPayload(editorState, fileDetail) {
    const sourceAudio = findSelectedSourceAudio(editorState);
    const convertEnabled = Boolean(editorState.audioFix.enabled && sourceAudio);

    const containerTitle =
      editorState.containerTitle === editorState.originalContainerTitle
        ? null
        : editorState.containerTitle;

    // Compute exact order_index for each external track across kept output tracks
    const displayRows = buildDisplayTracks(editorState);
    const externalOrderIndexById = new Map();
    let keptPosition = 0;
    displayRows.forEach((row) => {
      if (row.isVirtual) {
        keptPosition += 1;
      } else if (row.isExternal && row.ext) {
        externalOrderIndexById.set(row.ext.id, keptPosition);
        keptPosition += 1;
      } else if (row.track && row.track.enabled && !row.isDroppedByAudioFix) {
        keptPosition += 1;
      }
    });

    const externalTracks = (editorState.externalTracks || []).map((ext) => ({
      id: ext.id,
      kind: ext.kind,
      source: ext.source,
      path:
        ext.source === EXTERNAL_TRACKS.SOURCE_STORAGE && ext.path
          ? String(ext.path).replace(/^\/+/, "")
          : null,
      upload_id:
        ext.source === EXTERNAL_TRACKS.SOURCE_UPLOAD ? ext.uploadId : null,
      original_filename: ext.originalFilename || "",
      track_name: ext.trackName || null,
      language: ext.language || EDITOR.DEFAULT_LANGUAGE,
      default_track: Boolean(ext.defaultTrack),
      forced_track: Boolean(ext.forcedTrack),
      sync_delay_ms: Number(ext.syncDelayMs || 0),
      order_index: externalOrderIndexById.has(ext.id)
        ? externalOrderIndexById.get(ext.id)
        : null,
    }));

    const attachments = Array.isArray(editorState.attachments)
      ? editorState.attachments
      : [];
    const keptAttachmentIds = attachments
      .filter((att) => att.enabled)
      .map((att) => att.id);
    const stripAllAttachments = Boolean(
      attachments.length > 0
        ? keptAttachmentIds.length === 0
        : editorState.stripAttachments
    );
    const partialKeptAttachmentIds =
      !stripAllAttachments &&
      attachments.length > 0 &&
      keptAttachmentIds.length > 0 &&
      keptAttachmentIds.length < attachments.length
        ? keptAttachmentIds
        : null;

    return {
      file_id: fileDetail.id,
      config: {
        container: {
          title: containerTitle,
          strip_global_tags: Boolean(editorState.stripGlobalTags),
          strip_track_tags: Boolean(editorState.stripTrackTags),
          strip_attachments: stripAllAttachments,
          keep_attachment_ids: partialKeptAttachmentIds,
          strip_chapters: Boolean(editorState.stripChapters),
        },
        audio_conversion: {
          enabled: convertEnabled,
          source_track_id: convertEnabled ? sourceAudio.id : null,
          source_audio_index:
            convertEnabled &&
            sourceAudio.audio_index !== null &&
            sourceAudio.audio_index !== undefined
              ? Number(sourceAudio.audio_index)
              : 0,
          source_channels:
            convertEnabled && sourceAudio.audio_channels
              ? Number(sourceAudio.audio_channels)
              : null,
          source_codec: convertEnabled ? sourceAudio.codec : null,
          source_minimum_timestamp_ns: convertEnabled
            ? Number(sourceAudio.minimum_timestamp_ns || 0)
            : 0,
          target_codec: editorState.audioFix.targetCodec,
          target_channels:
            convertEnabled &&
            sourceAudio &&
            editorState.audioFix.targetChannels !== null &&
            editorState.audioFix.targetChannels !== undefined
              ? resolveTargetChannels(
                  sourceAudio.audio_channels,
                  editorState.audioFix.targetCodec,
                  editorState.audioFix.targetChannels
                )
              : null,
          bitrate:
            convertEnabled &&
            getCodecMeta(editorState.audioFix.targetCodec).isLossless
              ? null
              : editorState.audioFix.bitrate,
          track_title: editorState.audioFix.trackTitle,
          language: editorState.audioFix.language || EDITOR.DEFAULT_LANGUAGE,
          set_as_default: Boolean(editorState.audioFix.setAsDefault),
          drop_source_track: Boolean(editorState.audioFix.dropSourceTrack),
        },
        tracks: editorState.tracks.map((t) => ({
          id: t.id,
          type: t.type,
          enabled: Boolean(t.enabled),
          track_name: t.track_name,
          language: t.language || EDITOR.DEFAULT_LANGUAGE,
          default_track: Boolean(t.default_track),
          forced_track: Boolean(t.forced_track),
        })),
        external_tracks: externalTracks,
        output_mode: editorState.outputMode,
        output_path:
          editorState.outputMode === EDITOR.OUTPUT_SAVE_AS
            ? buildOutputPreview(editorState, fileDetail).targetRelPath
            : null,
        delete_original:
          editorState.outputMode === EDITOR.OUTPUT_SAVE_AS
            ? Boolean(editorState.saveAs && editorState.saveAs.deleteOriginal)
            : false,
      },
    };
  }

  global.MKVForgeEditor = Object.freeze({
    formatChannelLabel,
    canonicalizeAudioCodec,
    isLosslessAudioTrack,
    hasObjectAudioMetadata,
    getCodecMeta,
    resolveTargetChannels,
    getBitrateConfigFor,
    resolveDefaultAudioCodec,
    resolveDefaultAudioQuality,
    resolveQualityBitrate,
    formatQualityBitrateReadout,
    getAvailableTargetCodecs,
    getAvailableChannelOptions,
    isConversionCombinationBlocked,
    getConversionNotices,
    getAllowedTargetsForTrack,
    getAvailableAddAudioOptions,
    getRecommendedAddAudioOptions,
    buildDefaultAudioTitle,
    buildDefaultExternalTitle,
    isAac51Track,
    isSurroundTrack,
    isMp4File,
    stripFileExtension,
    ensureMkvFilename,
    normalizeDisplayFolder,
    buildOutputPreview,
    createEditorState,
    toggleAttachmentEnabled,
    setAllAttachmentsEnabled,
    findSelectedSourceAudio,
    moveTrack,
    moveOrderedTrack,
    upsertExternalTrack,
    removeExternalTrack,
    buildDisplayTracks,
    hasUncheckedAllVideo,
    countEffectiveKeptTracks,
    hasPendingChanges,
    buildPendingSummary,
    formatTrackBadge,
    getSmartFillPreview,
    buildJobRequestPayload,
  });
})(window);
