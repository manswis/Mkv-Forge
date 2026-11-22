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
            ".ui-nav-item",
            ".ui-nav-item-active",
            ".ui-menu-panel",
            ".ui-menu-item",
            ".ui-menu-item-active",
            ".ui-menu-item-danger",
            ".ui-menu-divider",
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

    def test_phase2_shell_chrome_elevation_contrast_and_typography_hygiene(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        # Extract Phase 2 ranges: Desktop Sidebar + Mobile Header/Menu + Toast Stack + Context Menu
        shell_start = html.index("<!-- Desktop Sidebar")
        shell_end = html.index("<!-- Main Content Viewport")
        shell_block = html[shell_start:shell_end]

        ctx_start = html.index("<!-- Global Floating Context Menu")
        ctx_end = html.index("<!-- File Info / Full Media Metadata Modal", ctx_start)
        ctx_block = html[ctx_start:ctx_end]

        phase2_html = shell_block + "\n" + ctx_block

        # Guardrail 3: Zero stray text-[10px] inside Phase 2 blocks
        assert "text-[10px]" not in phase2_html

        # Guardrail 1: Zero hardcoded text-white paired with solid bg-accent in Phase 2 blocks
        assert "text-[var(--color-accent-fg)]" in shell_block
        for line in phase2_html.splitlines():
            if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                assert "text-white" not in line, f"Hardcoded text-white on bg-accent: {line.strip()}"

        # Guardrail 2: Floating menus and toasts use elevated surface + default border + elevated shadow
        assert "bg-elevated border border-default shadow-elevated" in shell_block
        assert "border-default border-l-[3px] bg-elevated/95" in shell_block
        assert "shadow-elevated" in shell_block
        assert "border border-default bg-elevated/95 shadow-elevated" in ctx_block

        # Verify mobile touch target ergonomics (>= 44px) on top header buttons
        assert "h-11 w-11" in shell_block
        assert "min-h-[44px]" in shell_block

    def test_phase3_library_monochrome_metadata_truncation_and_bulk_elevation(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        lib_start = html.index("<!-- SCREEN 2: LIBRARY EXPLORER")
        lib_end = html.index("<!-- SCREEN 3: FILE EDITOR", lib_start)
        lib_block = html[lib_start:lib_end]

        # Guardrail 4: Zero stray text-[10px] or text-[9px] inside Library Explorer
        assert "text-[10px]" not in lib_block
        assert "text-[9px]" not in lib_block

        # Guardrail 1: Informational specs render as monochrome .ui-tag (no rainbow badge helpers)
        assert "Fmt.videoCodecBadgeClass(" not in lib_block
        assert "Fmt.dynamicRangeBadgeClass(" not in lib_block
        assert "Fmt.audioBadgeClass(" not in lib_block
        assert "Fmt.audioChipActiveClass(" not in lib_block
        assert 'class="ui-tag font-mono"' in lib_block
        assert 'class="ui-status ui-status-warn"' in lib_block

        # Guardrail 2: Safe truncation & tabular numerics on filenames, folders, sizes & counts
        assert "min-w-0 truncate font-mono" in lib_block
        assert "ui-num" in lib_block

        # Guardrail 3: Zero hardcoded text-white on solid bg-accent & elevated bulk bar
        for line in lib_block.splitlines():
            if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                assert "text-white" not in line, f"Hardcoded text-white on bg-accent: {line.strip()}"
        assert "border border-strong bg-elevated px-3.5 py-2.5 text-xs shadow-elevated" in lib_block

        # Regression guard: Other Files list renders only once (no duplicate outside scroll viewport)
        assert lib_block.count('x-for="of in otherFiles"') == 1

    def test_phase4_editor_inspector_virtual_rows_and_sticky_bar(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        ed_start = html.index("<!-- SCREEN 3: FILE EDITOR")
        ed_end = html.index("<!-- SCREEN 4: JOBS PAGE", ed_start)
        ed_block = html[ed_start:ed_end]

        bar_start = html.index("<!-- Sticky Bottom Action Bar")
        bar_end = html.index("<!-- + Add Audio: Base Audio Selector", bar_start)
        bar_block = html[bar_start:bar_end]

        phase4_html = ed_block + "\n" + bar_block

        # Guardrail 4: Zero stray text-[10px] or text-[9px] inside Phase 4 ranges
        assert "text-[10px]" not in phase4_html
        assert "text-[9px]" not in phase4_html

        # Guardrail 1: Inspector layout uses crisp section titles + .ui-list-group + .ui-segmented
        assert ed_block.count('class="ui-list-group"') >= 5
        assert "ui-segmented grid w-full grid-cols-3" in ed_block
        assert "ui-section-title" in ed_block

        # Guardrail 2: Virtual [NEW] & [IMPORT] rows use 2px left accent bar + tabular numerics
        assert ed_block.count("border-l-2 border-l-[var(--color-accent)]") >= 2
        assert "ui-num" in ed_block
        assert "ui-menu-panel bg-elevated border border-default shadow-elevated" in ed_block

        # Guardrail 3: Sticky bar elevated + .ui-btn-primary Process File CTA & zero hardcoded text-white on bg-accent
        assert "border-t border-default bg-elevated/95 shadow-elevated" in bar_block
        assert "ui-btn ui-btn-primary" in bar_block
        for line in phase4_html.splitlines():
            if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                assert "text-white" not in line, f"Hardcoded text-white on bg-accent: {line.strip()}"

    def test_phase5_dashboard_jobs_and_drawer_hygiene(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        dash_start = html.index("<!-- SCREEN 1: DASHBOARD")
        dash_end = html.index("<!-- SCREEN 2: LIBRARY EXPLORER", dash_start)
        dash_block = html[dash_start:dash_end]

        jobs_start = html.index("<!-- SCREEN 4: JOBS PAGE")
        jobs_end = html.index("<!-- SCREEN 5: FULL-SCREEN SETTINGS POPUP", jobs_start)
        jobs_block = html[jobs_start:jobs_end]

        drawer_start = html.index("<!-- Slide-Over Live Jobs Drawer")
        drawer_end = html.index("<!-- Global Floating Context Menu", drawer_start)
        drawer_block = html[drawer_start:drawer_end]

        phase5_html = dash_block + "\n" + jobs_block + "\n" + drawer_block

        # Guardrail 4: Zero stray text-[10px]/text-[9px] & zero hardcoded text-white on bg-accent
        assert "text-[10px]" not in phase5_html
        assert "text-[9px]" not in phase5_html
        for line in phase5_html.splitlines():
            if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                assert "text-white" not in line, f"Hardcoded text-white on bg-accent: {line.strip()}"

        # Guardrail 1: Preserve charts.js SVG hosts & live-hydration bindings inside .ui-panel
        assert 'x-ref="storagePieHost" x-effect="syncStoragePieChart($el)"' in dash_block
        assert 'x-ref="audioPieHost" x-effect="syncAudioPieChart($el)"' in dash_block
        assert "Fmt.dynamicRangeBadgeClass(" not in dash_block

        # Guardrail 2: Unified hairline KPI strip + .ui-list-group for Recent Activity, Queued & History
        assert "grid grid-cols-2 gap-px bg-[var(--color-border-subtle)] lg:grid-cols-4" in dash_block
        assert dash_block.count('class="ui-list-group"') >= 3
        assert jobs_block.count('class="ui-list-group"') >= 2
        assert drawer_block.count('class="ui-list-group"') >= 2

        # Guardrail 3: Recessed bg-inset CLI logs + elevated Slide-Over Drawer preserving pb-safe-drawer
        assert 'x-ref="pageLogBox"' in jobs_block
        assert "bg-inset p-3 font-mono text-xs leading-relaxed text-ink-primary" in jobs_block
        assert 'x-ref="drawerLogBox"' in drawer_block
        assert "border-l border-default bg-elevated shadow-elevated" in drawer_block
        assert "pb-safe-drawer" in drawer_block
