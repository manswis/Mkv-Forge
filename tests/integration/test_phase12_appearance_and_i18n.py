"""Integration and Node VM tests for Phase 12 Appearance (Light/Dark/System + 6 Accents) & i18n."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx

from app.config import AppConstants


class TestPhase12AppearanceAndI18n:
    """Verify theme/accent/language persistence, CSS variables, theme.js, and i18n.js."""

    async def test_settings_theme_accent_and_language_round_trip_and_validation(
        self, api_client: httpx.AsyncClient
    ) -> None:
        initial = (await api_client.get("/api/settings")).json()
        assert initial["theme_mode"] == "dark"
        assert initial["accent_color"] == "blue"
        assert initial["font_scale"] == 100
        assert initial["language"] == "en"

        for mode in ("light", "system", "dark"):
            resp = await api_client.patch("/api/settings", json={"theme_mode": mode})
            assert resp.status_code == AppConstants.HTTP_OK
            assert resp.json()["theme_mode"] == mode

        for accent in ("indigo", "emerald", "violet", "rose", "amber", "blue"):
            resp = await api_client.patch(
                "/api/settings", json={"accent_color": accent}
            )
            assert resp.status_code == AppConstants.HTTP_OK
            assert resp.json()["accent_color"] == accent

        for scale in (85, 115, 125, 100):
            resp = await api_client.patch("/api/settings", json={"font_scale": scale})
            assert resp.status_code == AppConstants.HTTP_OK
            assert resp.json()["font_scale"] == scale

        # Invalid theme_mode, accent_color, font_scale, and language are rejected with 422
        assert (
            await api_client.patch("/api/settings", json={"theme_mode": "neon"})
        ).status_code == 422
        assert (
            await api_client.patch(
                "/api/settings", json={"accent_color": "chartreuse"}
            )
        ).status_code == 422
        assert (
            await api_client.patch("/api/settings", json={"font_scale": 50})
        ).status_code == 422
        assert (
            await api_client.patch("/api/settings", json={"font_scale": 200})
        ).status_code == 422
        assert (
            await api_client.patch("/api/settings", json={"language": "fr"})
        ).status_code == 422

    async def test_compiled_css_variables_and_offline_html_landmarks(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        css = (await api_client.get("/static/css/app.css")).text
        theme_js = await api_client.get("/static/js/theme.js")
        i18n_js = await api_client.get("/static/js/i18n.js")

        assert theme_js.status_code == AppConstants.HTTP_OK
        assert i18n_js.status_code == AppConstants.HTTP_OK
        assert re.search(r"https?://", theme_js.text) is None
        assert re.search(r"https?://", i18n_js.text) is None

        # Zero-FOUC head script + script tags + Appearance UI landmarks
        assert 'localStorage.getItem("mkvforge.themeMode")' in html
        assert 'localStorage.getItem("mkvforge.accentColor")' in html
        assert "/static/js/theme.js" in html
        assert "/static/js/i18n.js" in html
        assert 'aria-label="Theme mode"' in html
        assert 'aria-label="Accent color"' in html
        assert 'aria-label="Font size scale"' in html
        assert 'aria-label="Interface language"' in html
        assert "setThemeMode(tm.id)" in html
        assert "setAccentColor(ac.id)" in html
        assert "setFontScale($event.target.value)" in html
        assert "setUiLanguage($event.target.value)" in html

        # CSS custom properties for surfaces + all 6 accent themes
        for token in (
            "--color-canvas",
            "--color-surface",
            "--color-hairline",
            "--color-accent",
            "--color-accent-hover",
            "--color-accent-soft",
        ):
            assert token in css
        for accent in ("indigo", "blue", "emerald", "violet", "rose", "amber"):
            assert f"data-accent={accent}" in css or f'data-accent="{accent}"' in css

    def test_theme_and_i18n_runtime_behavior_and_edge_cases(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const storage = {{}};
        const rootClasses = new Set(["dark"]);
        const rootAttrs = {{}};
        const rootStyleProps = {{}};
        const rootStyle = {{
          fontSize: "100%",
          colorScheme: "dark",
          setProperty: (k, v) => {{ rootStyleProps[k] = String(v); }},
        }};
        const bodyStyle = {{ backgroundColor: "", color: "" }};
        let metaThemeColor = "#09090B";
        let mqlMatches = true;
        let mqlListener = null;
        const patchPayloads = [];
        let serverSettings = null;

        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{
            getItem: (k) => (k in storage ? storage[k] : null),
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          matchMedia: () => ({{
            get matches() {{ return mqlMatches; }},
            addEventListener: (_evt, fn) => {{ mqlListener = fn; }},
          }}),
          location: {{ hash: "#/dashboard", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{
            visibilityState: "visible",
            readyState: "loading",
            addEventListener: () => {{}},
            body: {{ style: bodyStyle }},
            documentElement: {{
              style: rootStyle,
              classList: {{
                add: (...cls) => cls.forEach((c) => rootClasses.add(c)),
                remove: (...cls) => cls.forEach((c) => rootClasses.delete(c)),
                toggle: (cls, force) => {{
                  if (force) rootClasses.add(cls);
                  else rootClasses.delete(cls);
                }},
                contains: (cls) => rootClasses.has(cls),
              }},
              setAttribute: (k, v) => {{ rootAttrs[k] = String(v); }},
            }},
            querySelector: (sel) => sel === 'meta[name="theme-color"]'
              ? {{ setAttribute: (_k, v) => {{ metaThemeColor = v; }} }}
              : null,
          }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
          fetch: async (url, opts) => {{
            const body = opts && opts.body ? JSON.parse(opts.body) : {{}};
            if (url === "/api/settings" && opts && opts.method === "PATCH") {{
              patchPayloads.push(body);
              serverSettings = {{
                ...(serverSettings || ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS),
                ...body,
              }};
              return {{
                ok: true,
                status: 200,
                json: async () => serverSettings,
              }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{}}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "theme.js", "i18n.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const Theme = ctx.MKVForgeTheme;
        const I18n = ctx.MKVForgeI18n;
        const app = ctx.mkvForgeApp();

        // 1. Normalization & fallback edge cases (Blue is first and default)
        const firstAccentOption = ctx.MKVForgeConstants.THEMES.ACCENTS[0].id;
        const badMode = Theme.normalizeThemeMode("invalid");
        const nullMode = Theme.normalizeThemeMode(null);
        const upperLight = Theme.normalizeThemeMode("  LIGHT ");
        const badAccent = Theme.normalizeAccentColor("chartreuse");
        const nullAccent = Theme.normalizeAccentColor("");
        const upperRose = Theme.normalizeAccentColor(" ROSE ");
        const minScale = Theme.normalizeFontScale(40);
        const maxScale = Theme.normalizeFontScale(200);
        const stepScale = Theme.normalizeFontScale(112);
        const nanScale = Theme.normalizeFontScale("oops");

        // 2. Effective theme resolution & DOM application (including inline Chrome style vars)
        const appliedLight = Theme.applyTheme("light", "emerald");
        const isLightClass = rootClasses.has("light") && !rootClasses.has("dark");
        const lightMeta = metaThemeColor;
        const accentAttr = rootAttrs["data-accent"];
        const lightCanvasInline = rootStyleProps["--color-canvas"];
        const lightBodyBg = bodyStyle.backgroundColor;

        // 3. System mode + live OS prefers-color-scheme change listener
        app.userSettings.theme_mode = "system";
        app.userSettings.accent_color = "violet";
        Theme.bindSystemThemeListener(
          () => app.userSettings.theme_mode,
          () => app.userSettings.accent_color
        );
        mqlMatches = false;
        if (mqlListener) mqlListener({{ matches: false }});
        const sysLightTheme = rootAttrs["data-theme"];
        const sysLightMeta = metaThemeColor;
        mqlMatches = true;
        if (mqlListener) mqlListener({{ matches: true }});
        const sysDarkTheme = rootAttrs["data-theme"];
        const sysDarkMeta = metaThemeColor;

        // 4. i18n lookup & fallback edge cases
        const knownKey = I18n.t("settings.theme_mode", "en");
        const unknownLocale = I18n.t("settings.accent_color", "de");
        const missingKeyWithFallback = I18n.t("missing.key", "en", "Fallback Text");
        const missingKeyNoFallback = I18n.t("missing.key", "en");
        const nullKey = I18n.t(null, "en", "Safe");

        // 5. Controller actions (setThemeMode, setAccentColor, setFontScale, setUiLanguage)
        (async () => {{
          await app.setThemeMode("light");
          await app.setAccentColor("rose");
          await app.setFontScale(115);
          await app.setUiLanguage("en");
          console.log(JSON.stringify({{
            firstAccentOption,
            badMode, nullMode, upperLight,
            badAccent, nullAccent, upperRose,
            minScale, maxScale, stepScale, nanScale,
            appliedLight, isLightClass, lightMeta, accentAttr,
            lightCanvasInline, lightBodyBg,
            sysLightTheme, sysLightMeta, sysDarkTheme, sysDarkMeta,
            knownKey, unknownLocale, missingKeyWithFallback, missingKeyNoFallback, nullKey,
            patchPayloads,
            finalAccentAttr: rootAttrs["data-accent"],
            finalThemeAttr: rootAttrs["data-theme"],
            finalFontSize: rootStyle.fontSize,
            storedMode: storage["mkvforge.themeMode"],
            storedAccent: storage["mkvforge.accentColor"],
            storedFontScale: storage["mkvforge.fontScale"],
          }}));
          process.exit(0);
        }})();
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)

        assert out["firstAccentOption"] == "blue"
        assert out["badMode"] == "dark"
        assert out["nullMode"] == "dark"
        assert out["upperLight"] == "light"
        assert out["badAccent"] == "blue"
        assert out["nullAccent"] == "blue"
        assert out["upperRose"] == "rose"
        assert out["minScale"] == 85
        assert out["maxScale"] == 125
        assert out["stepScale"] == 110
        assert out["nanScale"] == 100
        assert out["appliedLight"]["effective"] == "light"
        assert out["isLightClass"] is True
        assert out["lightMeta"] == "#F4F4F5"
        assert out["accentAttr"] == "emerald"
        assert out["lightCanvasInline"] == "#f4f4f5"
        assert out["lightBodyBg"] == "#f4f4f5"
        assert out["sysLightTheme"] == "light"
        assert out["sysLightMeta"] == "#F4F4F5"
        assert out["sysDarkTheme"] == "dark"
        assert out["sysDarkMeta"] == "#09090B"
        assert out["knownKey"] == "Theme Mode"
        assert out["unknownLocale"] == "Accent Color"
        assert out["missingKeyWithFallback"] == "Fallback Text"
        assert out["missingKeyNoFallback"] == "missing.key"
        assert out["nullKey"] == "Safe"
        assert out["patchPayloads"] == [
            {"theme_mode": "light"},
            {"accent_color": "rose"},
            {"font_scale": 115},
            {"language": "en"},
        ]
        assert out["finalAccentAttr"] == "rose"
        assert out["finalThemeAttr"] == "light"
        assert out["finalFontSize"] == "115%"
        assert out["storedMode"] == "light"
        assert out["storedAccent"] == "rose"
        assert out["storedFontScale"] == "115"
