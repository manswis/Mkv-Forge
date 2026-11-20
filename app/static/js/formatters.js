/* Pure display formatters and semantic badge helpers for MKV Forge. */

(function attachFormatters(global) {
  const { UNITS, VIDEO_CODEC_TOOLTIPS } = global.MKVForgeConstants;
  const SIZE_LABELS = ["B", "KB", "MB", "GB", "TB"];

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) {
      return "—";
    }
    const value = Math.max(0, Number(bytes));
    if (value === 0) return "0 B";

    const exponent = Math.min(
      Math.floor(Math.log(value) / Math.log(UNITS.BYTES_PER_KB)),
      SIZE_LABELS.length - 1
    );
    const scaled = value / Math.pow(UNITS.BYTES_PER_KB, exponent);
    const decimals = exponent === 0 ? 0 : scaled >= 100 ? 0 : 1;
    return `${scaled.toFixed(decimals)} ${SIZE_LABELS[exponent]}`;
  }

  function formatDurationUs(durationUs) {
    const us = Number(durationUs);
    if (!us || Number.isNaN(us) || us <= 0) return "—";

    const totalSec = Math.floor(us / UNITS.US_PER_SEC);
    const hours = Math.floor(totalSec / UNITS.SEC_PER_HOUR);
    const minutes = Math.floor((totalSec % UNITS.SEC_PER_HOUR) / UNITS.SEC_PER_MIN);
    const seconds = totalSec % UNITS.SEC_PER_MIN;

    if (hours > 0) {
      return `${hours}h ${String(minutes).padStart(2, "0")}m`;
    }
    if (minutes > 0) {
      return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
    }
    return `${seconds}s`;
  }

  function formatRelativeTime(isoString) {
    if (!isoString) return "Never";
    const parsedMs = Date.parse(isoString);
    if (Number.isNaN(parsedMs)) return "—";

    const diffSec = Math.max(0, Math.floor((Date.now() - parsedMs) / UNITS.MS_PER_SEC));
    if (diffSec < UNITS.SEC_PER_MIN) return "Just now";
    if (diffSec < UNITS.SEC_PER_HOUR) {
      return `${Math.floor(diffSec / UNITS.SEC_PER_MIN)}m ago`;
    }
    if (diffSec < UNITS.SEC_PER_DAY) {
      return `${Math.floor(diffSec / UNITS.SEC_PER_HOUR)}h ago`;
    }
    return `${Math.floor(diffSec / UNITS.SEC_PER_DAY)}d ago`;
  }

  function formatDateTime(isoString) {
    if (!isoString) return "Never";
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatAudioTrackBadge(track) {
    if (!track) return "";
    const parts = [track.codec || "Audio"];
    if (track.channel_layout) {
      parts.push(track.channel_layout);
    }
    if (track.language && track.language !== "und") {
      parts.push(track.language);
    }
    return parts.join(" ");
  }

  function audioBadgeClass(track) {
    const codec = String((track && track.codec) || "").toUpperCase();
    const channels = track ? Number(track.channels) : 0;

    if (codec === "EAC3" || codec === "AC3") {
      return "bg-emerald-500/15 text-emerald-400 border border-emerald-500/20";
    }
    if (codec === "AAC" && channels === 6) {
      return "bg-amber-500/15 text-amber-400 border border-amber-500/20";
    }
    return "bg-zinc-500/15 text-zinc-300 border border-zinc-500/20";
  }

  function jobStatusBadgeClass(status) {
    switch (status) {
      case "completed":
        return "bg-emerald-500/15 text-emerald-400";
      case "running":
        return "bg-accent/20 text-indigo-300";
      case "queued":
        return "bg-zinc-500/15 text-zinc-300";
      case "cancelled":
        return "bg-amber-500/15 text-amber-400";
      case "failed":
        return "bg-red-500/15 text-red-400";
      default:
        return "bg-zinc-500/15 text-zinc-300";
    }
  }

  function audioChipActiveClass(audioType) {
    const upper = String(audioType || "").toUpperCase();
    if (upper.startsWith("EAC3") || upper.startsWith("AC3")) {
      return "border-emerald-500/50 bg-emerald-500/15 text-emerald-300";
    }
    if (upper === "AAC 5.1") {
      return "border-amber-500/50 bg-amber-500/15 text-amber-300";
    }
    return "border-accent bg-accent/20 text-indigo-200";
  }

  function jobChangeBadgeClass(kind) {
    switch (kind) {
      case "added":
        return "border border-indigo-500/30 bg-accent/15 text-indigo-200";
      case "removed":
        return "border border-red-500/25 bg-red-500/10 text-red-300";
      case "changed":
        return "border border-amber-500/25 bg-amber-500/10 text-amber-200";
      case "output":
        return "border border-emerald-500/25 bg-emerald-500/10 text-emerald-300";
      default:
        return "border border-hairline bg-zinc-800/80 text-zinc-300";
    }
  }

  function basenameOf(filepath) {
    if (!filepath) return "";
    const normalized = String(filepath).replace(/\\/g, "/");
    const idx = normalized.lastIndexOf("/");
    return idx >= 0 ? normalized.slice(idx + 1) : normalized;
  }

  function fileExtensionLabel(filename) {
    const base = basenameOf(filename);
    const dotIdx = base.lastIndexOf(".");
    if (dotIdx <= 0 || dotIdx === base.length - 1) return "FILE";
    return base.slice(dotIdx + 1).toUpperCase().slice(0, 6);
  }

  function videoCodecTooltip(videoCodec) {
    const cleaned = String(videoCodec || "").trim();
    if (!cleaned) return "";
    return VIDEO_CODEC_TOOLTIPS[cleaned] || cleaned;
  }

  function videoCodecBadgeClass(videoCodec) {
    const upper = String(videoCodec || "").trim().toUpperCase();
    if (upper === "HEVC" || upper === "AV1") {
      return "bg-sky-500/15 text-sky-300 border border-sky-500/20";
    }
    return "bg-zinc-800 text-zinc-300 border border-hairline";
  }

  function dynamicRangeBadgeClass(dynamicRange) {
    const upper = String(dynamicRange || "").trim().toUpperCase();
    if (upper.startsWith("DV")) {
      return "bg-violet-500/15 text-violet-300 border border-violet-500/25";
    }
    if (upper.startsWith("HDR") || upper === "HLG") {
      return "bg-amber-500/15 text-amber-300 border border-amber-500/25";
    }
    return "bg-zinc-800/70 text-zinc-400 border border-hairline";
  }

  function formatBitrateBps(bps) {
    const val = Number(bps);
    if (!Number.isFinite(val) || val <= 0) return "—";
    if (val >= UNITS.BPS_PER_MBPS) {
      return `${(val / UNITS.BPS_PER_MBPS).toFixed(2)} Mbps`;
    }
    if (val >= UNITS.BPS_PER_KBPS) {
      return `${Math.round(val / UNITS.BPS_PER_KBPS)} kbps`;
    }
    return `${Math.round(val)} bps`;
  }

  function formatSampleRateHz(hz) {
    const val = Number(hz);
    if (!Number.isFinite(val) || val <= 0) return "—";
    if (val >= UNITS.HZ_PER_KHZ) {
      const khz = val / UNITS.HZ_PER_KHZ;
      return `${Number.isInteger(khz) ? khz.toFixed(0) : khz.toFixed(1)} kHz`;
    }
    return `${Math.round(val)} Hz`;
  }

  global.MKVForgeFormatters = Object.freeze({
    formatBytes,
    formatDurationUs,
    formatRelativeTime,
    formatDateTime,
    formatBitrateBps,
    formatSampleRateHz,
    formatAudioTrackBadge,
    audioBadgeClass,
    audioChipActiveClass,
    jobStatusBadgeClass,
    jobChangeBadgeClass,
    basenameOf,
    fileExtensionLabel,
    videoCodecTooltip,
    videoCodecBadgeClass,
    dynamicRangeBadgeClass,
  });
})(window);
