/* Pure global Playback Profile presets, codec toggles, and onboarding summary helpers. */

(function attachPlaybackProfileHelpers(global) {
  const C = global.MKVForgeConstants;

  const PRESETS = Object.freeze([
    Object.freeze({
      id: "dolby_arc",
      title: "Soundbar / Smart TV (HDMI ARC)",
      shortLabel: "HDMI ARC (5.1)",
      badge: "Most Common",
      description: "Plays Dolby Digital / Plus (5.1) & AAC Stereo. Flags DTS, TrueHD & AAC 5.1.",
      supportedCodecs: Object.freeze(["eac3", "ac3", "aac_20"]),
      maxChannels: 6,
      preferredCodec: "eac3",
    }),
    Object.freeze({
      id: "optical_legacy",
      title: "Optical Soundbar / Legacy AVR",
      shortLabel: "Optical (5.1)",
      badge: "TOSLINK",
      description: "Optical SPDIF carries AC-3 5.1 & Stereo only. Flags E-AC-3, DTS, TrueHD & AAC 5.1.",
      supportedCodecs: Object.freeze(["ac3", "aac_20"]),
      maxChannels: 6,
      preferredCodec: "ac3",
    }),
    Object.freeze({
      id: "earc_avr",
      title: "Home Theater AVR (HDMI eARC)",
      shortLabel: "eARC AVR (7.1)",
      badge: "7.1 / Atmos",
      description: "Direct-plays TrueHD, DTS-HD MA, Dolby, FLAC & PCM up to 7.1 (passes through Atmos/DTS:X).",
      supportedCodecs: Object.freeze([
        "truehd",
        "dts_hd_ma",
        "eac3",
        "ac3",
        "dts",
        "flac",
        "pcm",
        "aac_20",
      ]),
      maxChannels: 8,
      preferredCodec: "eac3",
    }),
    Object.freeze({
      id: "streaming_clients",
      title: "Plex / Jellyfin / Infuse",
      shortLabel: "Streaming Clients (7.1)",
      badge: "Apple TV · Shield",
      description: "Modern software players handling AAC, Opus, FLAC & Dolby up to 7.1.",
      supportedCodecs: Object.freeze(["eac3", "ac3", "aac_20", "aac_51", "opus", "flac"]),
      maxChannels: 8,
      preferredCodec: "aac",
    }),
    Object.freeze({
      id: "stereo_mobile",
      title: "Stereo / Headphones / Mobile",
      shortLabel: "Stereo (2.0)",
      badge: "2.0 Only",
      description: "Flags multichannel 5.1/7.1-only files so you can downmix clean 2.0 stereo tracks.",
      supportedCodecs: Object.freeze(["aac_20", "opus", "ac3", "eac3", "mp3"]),
      maxChannels: 2,
      preferredCodec: "aac",
    }),
  ]);

  const CODEC_OPTIONS = Object.freeze([
    Object.freeze({ key: "eac3", label: "E-AC-3", hint: "Dolby Digital Plus" }),
    Object.freeze({ key: "ac3", label: "AC-3", hint: "Dolby Digital" }),
    Object.freeze({ key: "dts", label: "DTS", hint: "Core DTS" }),
    Object.freeze({ key: "dts_hd_ma", label: "DTS-HD MA", hint: "Lossless DTS" }),
    Object.freeze({ key: "truehd", label: "TrueHD", hint: "Dolby Lossless / Atmos" }),
    Object.freeze({ key: "aac_51", label: "AAC 5.1+", hint: "Multichannel AAC" }),
    Object.freeze({ key: "aac_20", label: "AAC 2.0", hint: "Stereo AAC" }),
    Object.freeze({ key: "opus", label: "Opus", hint: "Modern LPCM" }),
    Object.freeze({ key: "flac", label: "FLAC", hint: "Lossless PCM" }),
    Object.freeze({ key: "pcm", label: "PCM / LPCM", hint: "Uncompressed" }),
    Object.freeze({ key: "mp3", label: "MP3", hint: "Legacy Stereo" }),
  ]);

  const CHANNEL_CAP_OPTIONS = Object.freeze([
    Object.freeze({ value: 2, label: "2.0 Stereo", hint: "TV speakers / headphones" }),
    Object.freeze({ value: 6, label: "5.1 Surround", hint: "Soundbars & 5.1 AVRs" }),
    Object.freeze({ value: 8, label: "7.1 Home Theater", hint: "7.1 / Atmos / DTS:X beds" }),
  ]);

  // Preset/Profile default target codecs are strictly limited to eac3, ac3, aac.
  const PREFERRED_TARGET_OPTIONS = Object.freeze([
    Object.freeze({ codec: "eac3", label: "E-AC-3", hint: "Dolby Digital Plus" }),
    Object.freeze({ codec: "ac3", label: "AC-3", hint: "Dolby Digital (Legacy/Optical)" }),
    Object.freeze({ codec: "aac", label: "AAC", hint: "Universal Stereo / Surround" }),
  ]);

  function getPresetById(presetId) {
    if (!presetId) return null;
    return PRESETS.find((p) => p.id === presetId) || null;
  }

  function createProfileDraftFromSettings(settings) {
    const src = settings || {};
    const rawPreset = String(src.playback_profile_preset || "neutral");
    const presetObj = getPresetById(rawPreset);
    if (presetObj) {
      return {
        preset: presetObj.id,
        supportedCodecs: Array.from(presetObj.supportedCodecs),
        maxChannels: presetObj.maxChannels,
        preferredCodec: presetObj.preferredCodec,
        customOpen: false,
      };
    }
    if (rawPreset === "custom") {
      return {
        preset: "custom",
        supportedCodecs: Array.isArray(src.playback_supported_codecs)
          ? Array.from(src.playback_supported_codecs)
          : [],
        maxChannels: [2, 6, 8].includes(Number(src.playback_max_channels))
          ? Number(src.playback_max_channels)
          : 6,
        preferredCodec: ["eac3", "ac3", "aac"].includes(src.playback_preferred_codec)
          ? src.playback_preferred_codec
          : "eac3",
        customOpen: true,
      };
    }
    return {
      preset: "neutral",
      supportedCodecs: [],
      maxChannels: 6,
      preferredCodec: "eac3",
      customOpen: false,
    };
  }

  function applyPresetToDraft(draft, presetId) {
    const base = draft || createProfileDraftFromSettings(null);
    if (presetId === "neutral") {
      return {
        ...base,
        preset: "neutral",
        supportedCodecs: [],
        customOpen: false,
      };
    }
    const presetObj = getPresetById(presetId);
    if (!presetObj) return base;
    return {
      ...base,
      preset: presetObj.id,
      supportedCodecs: Array.from(presetObj.supportedCodecs),
      maxChannels: presetObj.maxChannels,
      preferredCodec: presetObj.preferredCodec,
    };
  }

  function toggleCodecInDraft(draft, codecKey) {
    const base = draft || createProfileDraftFromSettings(null);
    const list = Array.isArray(base.supportedCodecs) ? [...base.supportedCodecs] : [];
    const idx = list.indexOf(codecKey);
    if (idx >= 0) {
      list.splice(idx, 1);
    } else {
      list.push(codecKey);
    }
    return {
      ...base,
      preset: "custom",
      supportedCodecs: list,
      customOpen: true,
    };
  }

  function formatChannelCapLabel(maxChannels) {
    const ch = Number(maxChannels || 6);
    if (ch <= 2) return "2.0";
    if (ch >= 8) return "7.1";
    return "5.1";
  }

  function formatProfileSummary(profile) {
    if (!profile || profile.preset === "neutral") {
      return "Not configured — set up anytime in Settings";
    }
    const presetObj = getPresetById(profile.preset);
    if (presetObj) {
      return `${presetObj.shortLabel} · Target ${presetObj.preferredCodec.toUpperCase()}`;
    }
    const count = Array.isArray(profile.supportedCodecs) ? profile.supportedCodecs.length : 0;
    const chLabel = formatChannelCapLabel(profile.maxChannels);
    const target = String(profile.preferredCodec || "eac3").toUpperCase();
    return `Custom (${chLabel}) · ${count} codec${count === 1 ? "" : "s"} · Target ${target}`;
  }

  function buildSettingsPatchFromDraft(draft) {
    if (!draft || draft.preset === "neutral") {
      return {
        playback_profile_preset: "neutral",
        playback_supported_codecs: [],
      };
    }
    const presetObj = getPresetById(draft.preset);
    if (presetObj) {
      return {
        playback_profile_preset: presetObj.id,
        playback_supported_codecs: Array.from(presetObj.supportedCodecs),
        playback_max_channels: presetObj.maxChannels,
        playback_preferred_codec: presetObj.preferredCodec,
      };
    }
    const codecs = Array.isArray(draft.supportedCodecs)
      ? Array.from(new Set(draft.supportedCodecs))
      : [];
    if (codecs.length === 0) {
      return {
        playback_profile_preset: "neutral",
        playback_supported_codecs: [],
      };
    }
    return {
      playback_profile_preset: "custom",
      playback_supported_codecs: codecs,
      playback_max_channels: [2, 6, 8].includes(Number(draft.maxChannels))
        ? Number(draft.maxChannels)
        : 6,
      playback_preferred_codec: ["eac3", "ac3", "aac"].includes(draft.preferredCodec)
        ? draft.preferredCodec
        : "eac3",
    };
  }

  global.MKVForgePlaybackProfile = Object.freeze({
    PRESETS,
    CODEC_OPTIONS,
    CHANNEL_CAP_OPTIONS,
    PREFERRED_TARGET_OPTIONS,
    getPresetById,
    createProfileDraftFromSettings,
    applyPresetToDraft,
    toggleCodecInDraft,
    formatChannelCapLabel,
    formatProfileSummary,
    buildSettingsPatchFromDraft,
  });
})(window);
