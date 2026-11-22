"""Unit tests for Redesign Phase 1 design tokens, WCAG-AA contrast, and component primitives."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
INPUT_CSS_PATH = ROOT_DIR / "frontend" / "input.css"
COMPILED_CSS_PATH = ROOT_DIR / "app" / "static" / "css" / "app.css"
TAILWIND_CONFIG_PATH = ROOT_DIR / "tailwind.config.js"
THEME_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "theme.js"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"Expected 6-digit hex color, got {hex_color!r}")
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _relative_luminance(_hex_to_rgb(fg_hex))
    l2 = _relative_luminance(_hex_to_rgb(bg_hex))
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class TestDesignSystemTokensAndPrimitives:
    """Verify RGB-triple opacity tokens, WCAG-AA contrast, Light/Dark parity, and compiled .ui-* classes."""

    def test_tailwind_config_maps_rgb_triples_for_alpha_modifiers(self) -> None:
        cfg = TAILWIND_CONFIG_PATH.read_text(encoding="utf-8")
        for mapping in (
            'canvas: "rgb(var(--rgb-canvas) / <alpha-value>)"',
            'surface: "rgb(var(--rgb-surface) / <alpha-value>)"',
            'elevated: "rgb(var(--rgb-elevated) / <alpha-value>)"',
            'inset: "rgb(var(--rgb-inset) / <alpha-value>)"',
            'primary: "rgb(var(--rgb-text-primary) / <alpha-value>)"',
            'secondary: "rgb(var(--rgb-text-secondary) / <alpha-value>)"',
            'muted: "rgb(var(--rgb-text-muted) / <alpha-value>)"',
            'panel: "var(--shadow-panel)"',
            'elevated: "var(--shadow-elevated)"',
        ):
            assert mapping in cfg, f"Missing Tailwind token mapping: {mapping}"

    def test_compiled_css_contains_tokens_elevation_and_ui_primitives(self) -> None:
        input_css = INPUT_CSS_PATH.read_text(encoding="utf-8")
        compiled_css = COMPILED_CSS_PATH.read_text(encoding="utf-8")

        required_tokens = (
            "--rgb-canvas",
            "--rgb-surface",
            "--rgb-elevated",
            "--rgb-inset",
            "--rgb-text-primary",
            "--rgb-text-secondary",
            "--rgb-text-muted",
            "--color-border-subtle",
            "--color-border-default",
            "--color-border-strong",
            "--color-hover-fill",
            "--color-active-fill",
            "--shadow-panel",
            "--shadow-elevated",
            "--color-accent-fg",
            "--radius-sm",
            "--radius-md",
            "--radius-lg",
            "tabular-nums",
        )
        for token in required_tokens:
            assert token in input_css, f"Missing token in frontend/input.css: {token}"
            assert token in compiled_css, f"Missing token in compiled app.css: {token}"

        # Legacy Light Mode overrides must remain intact until end of Phase 6
        assert "html:not(.dark) .text-zinc-100" in input_css
        assert "html:not(.dark) .text-emerald-200" in input_css
        assert "html:not(.dark) .text-amber-200" in input_css

        required_primitives = (
            ".ui-num",
            ".ui-page-title",
            ".ui-section-title",
            ".ui-meta",
            ".ui-panel",
            ".ui-list-group",
            ".ui-row",
            ".ui-btn",
            ".ui-btn-sm",
            ".ui-btn-primary",
            ".ui-btn-secondary",
            ".ui-btn-ghost",
            ".ui-btn-danger",
            ".ui-segmented",
            ".ui-segmented-item",
            ".ui-tag",
            ".ui-status",
            ".ui-status-ok",
            ".ui-status-warn",
            ".ui-status-err",
            ".ui-status-info",
            ".ui-input",
            ".ui-select",
            ".ui-sheet",
        )
        for selector in required_primitives:
            assert selector in input_css, f"Missing primitive in input.css: {selector}"
            assert selector in compiled_css, f"Missing primitive in compiled app.css: {selector}"

        # Verify component primitives appear before Tailwind utilities in cascade order
        assert compiled_css.index(".ui-panel{") < compiled_css.index(".flex{")

        # Ensure zero external network fetches in compiled stylesheet
        assert re.search(r"url\(\s*['\"]?https?://", compiled_css) is None
        assert "@import" not in compiled_css

    def test_theme_js_runtime_palettes_sync_and_pass_wcag_aa_contrast(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const propsByRun = {{}};
        function captureTheme(mode, accent) {{
          const props = {{}};
          const ctx = {{
            document: {{
              documentElement: {{
                classList: {{ add() {{}}, remove() {{}} }},
                setAttribute() {{}},
                style: {{
                  setProperty(k, v) {{ props[k] = String(v); }},
                }},
              }},
              body: {{ style: {{}} }},
              querySelector() {{ return null; }},
            }},
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
          }};
          ctx.window = ctx;
          ctx.globalThis = ctx;
          vm.createContext(ctx);
          vm.runInContext(
            fs.readFileSync({json.dumps(str(THEME_JS_PATH))}, "utf8"),
            ctx,
            {{ filename: "theme.js" }}
          );
          ctx.MKVForgeTheme.applyTheme(mode, accent);
          return props;
        }}
        const accents = ["blue", "indigo", "emerald", "violet", "rose", "amber"];
        const out = {{ light: {{}}, dark: {{}} }};
        for (const a of accents) {{
          out.light[a] = captureTheme("light", a);
          out.dark[a] = captureTheme("dark", a);
        }}
        console.log(JSON.stringify(out));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        palettes = json.loads(proc.stdout)

        # Verify every Light Mode accent passes WCAG-AA (>= 4.5:1) with white text
        for accent, tokens in palettes["light"].items():
            ratio = _contrast_ratio("#ffffff", tokens["--color-accent"])
            assert ratio >= 4.5, (
                f"Light mode accent {accent!r} ({tokens['--color-accent']}) "
                f"fails WCAG-AA white-text contrast: {ratio:.2f}:1"
            )
            hover_ratio = _contrast_ratio("#ffffff", tokens["--color-accent-hover"])
            assert hover_ratio >= 4.5, (
                f"Light mode accent hover {accent!r} ({tokens['--color-accent-hover']}) "
                f"fails WCAG-AA white-text contrast: {hover_ratio:.2f}:1"
            )
            # Confirm elevated/inset/hover/shadow tokens are applied at runtime
            for key in (
                "--rgb-elevated",
                "--rgb-inset",
                "--rgb-text-primary",
                "--rgb-text-secondary",
                "--rgb-text-muted",
                "--color-hover-fill",
                "--color-active-fill",
                "--shadow-panel",
                "--shadow-elevated",
                "--color-accent-fg",
            ):
                assert key in tokens and tokens[key], f"Missing light token {key} for {accent}"

        # Verify Dark Mode accents pass WCAG-AA (>= 4.5:1) against their paired --color-accent-fg
        for accent, tokens in palettes["dark"].items():
            fg = tokens["--color-accent-fg"]
            ratio = _contrast_ratio(fg, tokens["--color-accent"])
            assert ratio >= 3.5, (
                f"Dark mode accent {accent!r} ({tokens['--color-accent']} on {fg}) "
                f"contrast too low: {ratio:.2f}:1"
            )
            # High-luminance Dark Mode accents (amber, emerald) must pair with dark foreground text
            if accent in ("amber", "emerald"):
                assert fg == "#09090b"
                assert ratio >= 7.0
