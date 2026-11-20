/* Lightweight, 100%-offline localization foundation (English default). */

(function initI18nModule(global) {
  "use strict";

  const DEFAULT_LOCALE = "en";
  const SUPPORTED_LOCALES = Object.freeze(["en"]);

  const DICTIONARIES = Object.freeze({
    en: Object.freeze({
      "app.name": "MKV Forge",
      "nav.dashboard": "Dashboard",
      "nav.library": "Library",
      "nav.jobs": "Jobs",
      "nav.settings": "Settings",
      "settings.title": "Settings",
      "settings.subtitle": "Preferences, media pipeline defaults, and storage maintenance",
      "settings.category.preferences": "General & Display",
      "settings.category.remuxing": "Media & Remuxing",
      "settings.category.storage": "Storage & System",
      "settings.section.appearance": "Appearance & Display",
      "settings.section.audio_tracks": "Audio & Track Defaults",
      "settings.section.output_metadata": "Output & Metadata",
      "settings.section.maintenance": "Storage & DB Cleanup",
      "settings.section.auto_cleanup": "Scheduled Auto-Cleanup",
      "settings.theme_mode": "Theme Mode",
      "settings.theme_mode.desc": "Switch between Dark, Light, or follow your OS color scheme.",
      "settings.accent_color": "Accent Color",
      "settings.accent_color.desc": "Primary highlight color across buttons, badges, and active states.",
      "settings.language": "Interface Language",
      "settings.language.desc": "Display language for the MKV Forge interface.",
    }),
  });

  function normalizeLocale(locale) {
    const cleaned = String(locale || "").trim().toLowerCase();
    return SUPPORTED_LOCALES.includes(cleaned) ? cleaned : DEFAULT_LOCALE;
  }

  function t(key, locale = DEFAULT_LOCALE, fallback = "") {
    if (!key || typeof key !== "string") {
      return String(fallback || "");
    }
    const normalized = normalizeLocale(locale);
    const dict = DICTIONARIES[normalized] || DICTIONARIES[DEFAULT_LOCALE];
    if (dict && Object.prototype.hasOwnProperty.call(dict, key)) {
      return dict[key];
    }
    const defaultDict = DICTIONARIES[DEFAULT_LOCALE];
    if (defaultDict && Object.prototype.hasOwnProperty.call(defaultDict, key)) {
      return defaultDict[key];
    }
    return fallback ? String(fallback) : key;
  }

  global.MKVForgeI18n = Object.freeze({
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    DICTIONARIES,
    normalizeLocale,
    t,
  });
})(window);
