/* Zero-FOUC Light / Dark / System theme, 6-color accent (Blue default) & font scale controller. */

(function initThemeModule(global) {
  "use strict";

  const C = global.MKVForgeConstants || {};
  const THEMES = C.THEMES || {
    MODE_DARK: "dark",
    MODE_LIGHT: "light",
    MODE_SYSTEM: "system",
    DEFAULT_MODE: "dark",
    DEFAULT_ACCENT: "blue",
    DEFAULT_FONT_SCALE: 100,
    MIN_FONT_SCALE: 85,
    MAX_FONT_SCALE: 125,
    FONT_SCALE_STEP: 5,
    DARK_META_COLOR: "#09090B",
    LIGHT_META_COLOR: "#F4F4F5",
  };
  const STORAGE_KEYS = C.STORAGE_KEYS || {
    THEME_MODE: "mkvforge.themeMode",
    ACCENT_COLOR: "mkvforge.accentColor",
    FONT_SCALE: "mkvforge.fontScale",
  };

  const VALID_MODES = new Set(["dark", "light", "system"]);
  const VALID_ACCENTS = new Set([
    "blue",
    "indigo",
    "emerald",
    "violet",
    "rose",
    "amber",
  ]);
  const SYSTEM_DARK_QUERY = "(prefers-color-scheme: dark)";

  const SURFACE_PALETTES = Object.freeze({
    dark: Object.freeze({
      "--rgb-canvas": "9 9 11",
      "--rgb-surface": "18 18 21",
      "--rgb-hairline": "39 39 42",
      "--color-canvas": "#09090b",
      "--color-surface": "#121215",
      "--color-hairline": "#27272a",
      "--color-text-primary": "#f4f4f5",
      "--color-text-secondary": "#d4d4d8",
      "--color-text-muted": "#a1a1aa",
    }),
    light: Object.freeze({
      "--rgb-canvas": "244 244 245",
      "--rgb-surface": "255 255 255",
      "--rgb-hairline": "228 228 231",
      "--color-canvas": "#f4f4f5",
      "--color-surface": "#ffffff",
      "--color-hairline": "#e4e4e7",
      "--color-text-primary": "#18181b",
      "--color-text-secondary": "#3f3f46",
      "--color-text-muted": "#52525b",
    }),
  });

  const ACCENT_PALETTES = Object.freeze({
    blue: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "59 130 246",
        "--rgb-accent-hover": "37 99 235",
        "--rgb-accent-soft": "147 197 253",
        "--color-accent": "#3b82f6",
        "--color-accent-hover": "#2563eb",
        "--color-accent-soft": "#93c5fd",
      }),
      light: Object.freeze({
        "--rgb-accent": "37 99 235",
        "--rgb-accent-hover": "29 78 216",
        "--rgb-accent-soft": "29 78 216",
        "--color-accent": "#2563eb",
        "--color-accent-hover": "#1d4ed8",
        "--color-accent-soft": "#1d4ed8",
      }),
    }),
    indigo: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "99 102 241",
        "--rgb-accent-hover": "79 70 229",
        "--rgb-accent-soft": "165 180 252",
        "--color-accent": "#6366f1",
        "--color-accent-hover": "#4f46e5",
        "--color-accent-soft": "#a5b4fc",
      }),
      light: Object.freeze({
        "--rgb-accent": "79 70 229",
        "--rgb-accent-hover": "67 56 202",
        "--rgb-accent-soft": "67 56 202",
        "--color-accent": "#4f46e5",
        "--color-accent-hover": "#4338ca",
        "--color-accent-soft": "#4338ca",
      }),
    }),
    emerald: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "16 185 129",
        "--rgb-accent-hover": "5 150 105",
        "--rgb-accent-soft": "110 231 183",
        "--color-accent": "#10b981",
        "--color-accent-hover": "#059669",
        "--color-accent-soft": "#6ee7b7",
      }),
      light: Object.freeze({
        "--rgb-accent": "5 150 105",
        "--rgb-accent-hover": "4 120 87",
        "--rgb-accent-soft": "4 120 87",
        "--color-accent": "#059669",
        "--color-accent-hover": "#047857",
        "--color-accent-soft": "#047857",
      }),
    }),
    violet: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "139 92 246",
        "--rgb-accent-hover": "124 58 237",
        "--rgb-accent-soft": "196 181 253",
        "--color-accent": "#8b5cf6",
        "--color-accent-hover": "#7c3aed",
        "--color-accent-soft": "#c4b5fd",
      }),
      light: Object.freeze({
        "--rgb-accent": "124 58 237",
        "--rgb-accent-hover": "109 40 217",
        "--rgb-accent-soft": "109 40 217",
        "--color-accent": "#7c3aed",
        "--color-accent-hover": "#6d28d9",
        "--color-accent-soft": "#6d28d9",
      }),
    }),
    rose: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "244 63 94",
        "--rgb-accent-hover": "225 29 72",
        "--rgb-accent-soft": "253 164 175",
        "--color-accent": "#f43f5e",
        "--color-accent-hover": "#e11d48",
        "--color-accent-soft": "#fda4af",
      }),
      light: Object.freeze({
        "--rgb-accent": "225 29 72",
        "--rgb-accent-hover": "190 18 60",
        "--rgb-accent-soft": "190 18 60",
        "--color-accent": "#e11d48",
        "--color-accent-hover": "#be123c",
        "--color-accent-soft": "#be123c",
      }),
    }),
    amber: Object.freeze({
      dark: Object.freeze({
        "--rgb-accent": "245 158 11",
        "--rgb-accent-hover": "217 119 6",
        "--rgb-accent-soft": "252 211 77",
        "--color-accent": "#f59e0b",
        "--color-accent-hover": "#d97706",
        "--color-accent-soft": "#fcd34d",
      }),
      light: Object.freeze({
        "--rgb-accent": "217 119 6",
        "--rgb-accent-hover": "180 83 9",
        "--rgb-accent-soft": "180 83 9",
        "--color-accent": "#d97706",
        "--color-accent-hover": "#b45309",
        "--color-accent-soft": "#b45309",
      }),
    }),
  });

  function normalizeThemeMode(mode) {
    const cleaned = String(mode || "").trim().toLowerCase();
    return VALID_MODES.has(cleaned) ? cleaned : THEMES.DEFAULT_MODE;
  }

  function normalizeAccentColor(accent) {
    const cleaned = String(accent || "").trim().toLowerCase();
    return VALID_ACCENTS.has(cleaned) ? cleaned : THEMES.DEFAULT_ACCENT;
  }

  function normalizeFontScale(scale) {
    if (scale === null || scale === undefined || scale === "" || typeof scale === "boolean") {
      return THEMES.DEFAULT_FONT_SCALE;
    }
    const parsed = Number(scale);
    if (!Number.isFinite(parsed)) {
      return THEMES.DEFAULT_FONT_SCALE;
    }
    const clamped = Math.max(
      THEMES.MIN_FONT_SCALE,
      Math.min(THEMES.MAX_FONT_SCALE, parsed)
    );
    const step = THEMES.FONT_SCALE_STEP || 5;
    return Math.round(clamped / step) * step;
  }

  function resolveEffectiveTheme(mode, systemPrefersDark = true) {
    const normalized = normalizeThemeMode(mode);
    if (normalized === THEMES.MODE_LIGHT) return THEMES.MODE_LIGHT;
    if (normalized === THEMES.MODE_DARK) return THEMES.MODE_DARK;
    return systemPrefersDark ? THEMES.MODE_DARK : THEMES.MODE_LIGHT;
  }

  function themeColorMetaHex(effectiveTheme) {
    return effectiveTheme === THEMES.MODE_LIGHT
      ? THEMES.LIGHT_META_COLOR
      : THEMES.DARK_META_COLOR;
  }

  function detectSystemPrefersDark() {
    try {
      if (typeof global.matchMedia === "function") {
        const mql = global.matchMedia(SYSTEM_DARK_QUERY);
        if (mql && typeof mql.matches === "boolean") {
          return mql.matches;
        }
      }
    } catch (_err) {
      // Safe fallback in restricted environments
    }
    return true;
  }

  function loadStoredThemePrefs() {
    let mode = THEMES.DEFAULT_MODE;
    let accent = THEMES.DEFAULT_ACCENT;
    let fontScale = THEMES.DEFAULT_FONT_SCALE;
    try {
      if (global.localStorage) {
        mode = normalizeThemeMode(
          global.localStorage.getItem(STORAGE_KEYS.THEME_MODE)
        );
        accent = normalizeAccentColor(
          global.localStorage.getItem(STORAGE_KEYS.ACCENT_COLOR)
        );
        fontScale = normalizeFontScale(
          global.localStorage.getItem(STORAGE_KEYS.FONT_SCALE)
        );
      }
    } catch (_err) {
      // Safe fallback
    }
    return { mode, accent, fontScale };
  }

  function applyFontScale(scale, options = {}) {
    const normalized = normalizeFontScale(scale);
    if (options.persist !== false) {
      try {
        if (global.localStorage) {
          global.localStorage.setItem(
            STORAGE_KEYS.FONT_SCALE,
            String(normalized)
          );
        }
      } catch (_err) {
        // Ignore storage errors
      }
    }

    const doc = global.document;
    if (doc && doc.documentElement) {
      const root = doc.documentElement;
      if (root.style) {
        root.style.fontSize = `${normalized}%`;
      }
      if (typeof root.setAttribute === "function") {
        root.setAttribute("data-font-scale", String(normalized));
      }
    }
    return normalized;
  }

  function applyTheme(mode, accent, options = {}) {
    const normalizedMode = normalizeThemeMode(mode);
    const normalizedAccent = normalizeAccentColor(accent);
    const systemDark =
      typeof options.systemPrefersDark === "boolean"
        ? options.systemPrefersDark
        : detectSystemPrefersDark();
    const effective = resolveEffectiveTheme(normalizedMode, systemDark);
    const metaHex = themeColorMetaHex(effective);

    if (options.persist !== false) {
      try {
        if (global.localStorage) {
          global.localStorage.setItem(STORAGE_KEYS.THEME_MODE, normalizedMode);
          global.localStorage.setItem(
            STORAGE_KEYS.ACCENT_COLOR,
            normalizedAccent
          );
        }
      } catch (_err) {
        // Ignore storage quota errors
      }
    }

    const doc = global.document;
    if (doc && doc.documentElement) {
      const root = doc.documentElement;
      const isDark = effective === THEMES.MODE_DARK;
      if (root.classList) {
        if (typeof root.classList.remove === "function" && typeof root.classList.add === "function") {
          root.classList.remove("dark", "light");
          root.classList.add(isDark ? "dark" : "light");
        } else if (typeof root.classList.toggle === "function") {
          root.classList.toggle("dark", isDark);
          root.classList.toggle("light", !isDark);
        }
      }
      if (typeof root.setAttribute === "function") {
        root.setAttribute("data-theme-mode", normalizedMode);
        root.setAttribute("data-theme", effective);
        root.setAttribute("data-accent", normalizedAccent);
      }

      // Apply CSS custom properties directly on root.style to guarantee immediate
      // repaint in Desktop Chrome regardless of stylesheet caching or layer order
      const surfaceTokens =
        SURFACE_PALETTES[effective] || SURFACE_PALETTES.dark;
      const accentGroup =
        ACCENT_PALETTES[normalizedAccent] || ACCENT_PALETTES.blue;
      const accentTokens = accentGroup[effective] || accentGroup.dark;

      if (root.style) {
        root.style.colorScheme = effective;
        if (typeof root.style.setProperty === "function") {
          Object.entries(surfaceTokens).forEach(([k, v]) => {
            root.style.setProperty(k, v);
          });
          Object.entries(accentTokens).forEach(([k, v]) => {
            root.style.setProperty(k, v);
          });
        }
      }

      if (doc.body && doc.body.style) {
        doc.body.style.backgroundColor = surfaceTokens["--color-canvas"];
        doc.body.style.color = surfaceTokens["--color-text-primary"];
      }

      if (typeof doc.querySelector === "function") {
        const meta = doc.querySelector('meta[name="theme-color"]');
        if (meta && typeof meta.setAttribute === "function") {
          meta.setAttribute("content", metaHex);
        }
      }
    }

    return {
      mode: normalizedMode,
      accent: normalizedAccent,
      effective,
      metaColor: metaHex,
    };
  }

  function bindSystemThemeListener(getModeFn, getAccentFn, onChangeFn) {
    try {
      if (typeof global.matchMedia !== "function") return null;
      const mql = global.matchMedia(SYSTEM_DARK_QUERY);
      if (!mql) return null;
      const handler = (event) => {
        const currentMode =
          typeof getModeFn === "function"
            ? normalizeThemeMode(getModeFn())
            : THEMES.DEFAULT_MODE;
        if (currentMode !== THEMES.MODE_SYSTEM) return;
        const currentAccent =
          typeof getAccentFn === "function"
            ? normalizeAccentColor(getAccentFn())
            : THEMES.DEFAULT_ACCENT;
        const applied = applyTheme(currentMode, currentAccent, {
          persist: false,
          systemPrefersDark:
            event && typeof event.matches === "boolean"
              ? event.matches
              : detectSystemPrefersDark(),
        });
        if (typeof onChangeFn === "function") {
          onChangeFn(applied);
        }
      };
      if (typeof mql.addEventListener === "function") {
        mql.addEventListener("change", handler);
      } else if (typeof mql.addListener === "function") {
        mql.addListener(handler);
      }
      return handler;
    } catch (_err) {
      return null;
    }
  }

  global.MKVForgeTheme = Object.freeze({
    normalizeThemeMode,
    normalizeAccentColor,
    normalizeFontScale,
    resolveEffectiveTheme,
    themeColorMetaHex,
    detectSystemPrefersDark,
    loadStoredThemePrefs,
    applyFontScale,
    applyTheme,
    bindSystemThemeListener,
  });
})(window);
