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
      return "ui-status ui-status-ok";
    }
    if (codec === "AAC" && channels === 6) {
      return "ui-status ui-status-warn";
    }
    return "ui-tag";
  }

  function jobStatusBadgeClass(status) {
    switch (status) {
      case "completed":
        return "ui-status ui-status-ok";
      case "running":
        return "border border-accent/35 bg-accent/15 text-accent";
      case "queued":
        return "ui-tag";
      case "cancelled":
        return "ui-status ui-status-warn";
      case "failed":
        return "ui-status ui-status-err";
      default:
        return "ui-tag";
    }
  }

  function audioChipActiveClass(audioType) {
    const upper = String(audioType || "").toUpperCase();
    if (upper.startsWith("EAC3") || upper.startsWith("AC3")) {
      return "ui-status-ok";
    }
    if (upper === "AAC 5.1") {
      return "ui-status-warn";
    }
    return "border-accent bg-accent/15 text-accent";
  }

  function jobChangeBadgeClass(kind) {
    switch (kind) {
      case "added":
        return "border border-accent/35 bg-accent/15 text-accent";
      case "removed":
        return "ui-status-err border";
      case "changed":
        return "ui-status-warn border";
      case "output":
        return "ui-status-ok border";
      default:
        return "ui-tag";
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
      return "ui-status ui-status-info";
    }
    return "ui-tag";
  }

  function dynamicRangeBadgeClass(dynamicRange) {
    const upper = String(dynamicRange || "").trim().toUpperCase();
    if (upper.startsWith("DV")) {
      return "border border-accent/35 bg-accent/15 text-accent";
    }
    if (upper.startsWith("HDR") || upper === "HLG") {
      return "ui-status ui-status-warn";
    }
    return "ui-tag";
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
