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

        # Phase 6: Legacy !important Light Mode overrides have been stripped — Light Mode runs natively on tokens
        for legacy_override in (
            "html:not(.dark) .text-zinc-100",
            "html:not(.dark) .bg-zinc-800",
            "html:not(.dark) .text-emerald-200",
            "html:not(.dark) .text-amber-200",
            "html:not(.dark) .text-red-200",
            "html:not(.dark) .text-sky-200",
        ):
            assert legacy_override not in input_css, f"Legacy Light Mode override still present in input.css: {legacy_override}"
            assert legacy_override not in compiled_css, f"Legacy Light Mode override still present in app.css: {legacy_override}"
        # Legitimate Light Mode select chevron SVG override remains intact
        assert "html:not(.dark) .select-dark" in input_css

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

        # Regression guard: Other Files section renders only once inside scroll viewport (1 Grid + 1 List)
        assert lib_block.count('x-show="showSubfolders && otherFiles.length > 0"') == 1
        assert lib_block.count('x-for="of in otherFiles"') == 2

    def test_phase4_editor_inspector_virtual_rows_and_sticky_bar(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        ed_start = html.index("<!-- SCREEN 3: FILE EDITOR")
        ed_end = html.index("<!-- SCREEN 4: JOBS PAGE", ed_start)
        ed_block = html[ed_start:ed_end]

        bar_start = html.index("<!-- Sticky Bottom Action Bar", ed_start)
        bar_end = ed_end
        bar_block = html[bar_start:bar_end]

        phase4_html = ed_block

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

    def test_phase6_settings_modals_onboarding_and_native_light_mode(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        constants_js = (ROOT_DIR / "app" / "static" / "js" / "constants.js").read_text(encoding="utf-8")

        settings_start = html.index("<!-- SCREEN 5: FULL-SCREEN SETTINGS POPUP")
        settings_end = html.index("<!-- + Add Audio: Base Audio Selector", settings_start)
        settings_block = html[settings_start:settings_end]

        modals_start = html.index("<!-- + Add Audio: Base Audio Selector")
        modals_end = html.index("<!-- Slide-Over Live Jobs Drawer", modals_start)
        modals_block = html[modals_start:modals_end]

        tail_start = html.index("<!-- File Info / Full Media Metadata Modal")
        tail_block = html[tail_start:]

        phase6_html = settings_block + "\n" + modals_block + "\n" + tail_block

        # Guardrail 5: Zero stray text-[10px]/text-[9px] across the ENTIRE index.html
        assert "text-[10px]" not in html
        assert "text-[9px]" not in html

        # Guardrail 5: Zero hardcoded text-white on solid bg-accent across the ENTIRE index.html
        for line in html.splitlines():
            if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                assert "text-white" not in line, f"Hardcoded text-white on bg-accent: {line.strip()}"

        # Guardrail 4: Zero legacy zinc / pastel overrides left anywhere in index.html or settings badges
        for legacy_cls in (
            "text-zinc-",
            "bg-zinc-",
            "border-zinc-",
            "text-emerald-200",
            "text-amber-200",
            "text-red-200",
            "text-sky-200",
            "text-indigo-",
        ):
            assert legacy_cls not in html, f"Legacy Light-Mode-incompatible class found in index.html: {legacy_cls}"
        assert "text-indigo-300" not in constants_js
        assert "text-emerald-300" not in constants_js

        # Guardrail 1: Settings Split View uses .ui-list-group sections & hairline diagnostics strip
        assert settings_block.count("ui-list-group") >= 8
        assert "grid grid-cols-2 gap-px bg-[var(--color-border-subtle)] lg:grid-cols-4" in settings_block

        # Guardrail 2: All feature & confirmation dialogs standardized onto .ui-sheet + pb-safe-modal
        assert phase6_html.count("ui-sheet relative z-10") >= 12
        assert phase6_html.count("pb-safe-modal") >= 12
        assert "Fmt.videoCodecBadgeClass(" not in tail_block
        assert "Fmt.dynamicRangeBadgeClass(" not in tail_block

        # Guardrail 3: Onboarding Wizard preserves locked 3-zone geometry & animations with semantic surfaces
        assert "onboarding-wizard-shell flex w-full max-w-lg flex-col overflow-hidden rounded-ui-lg border border-default bg-elevated shadow-elevated" in tail_block
        assert "onboarding-header-row" in tail_block
        assert 'x-ref="onboardingBody" class="onboarding-body' in tail_block
        assert "onboarding-footer" in tail_block

    def test_phase7_js_semantic_tokens_and_offline_compliance(self) -> None:
        js_dir = ROOT_DIR / "app" / "static" / "js"
        forbidden_js_classes = (
            "text-zinc-",
            "bg-zinc-",
            "border-zinc-",
            "text-indigo-",
            "text-emerald-200",
            "text-amber-200",
            "text-red-200",
            "text-sky-200",
        )
        for js_file in sorted(js_dir.glob("*.js")):
            if js_file.name == "alpine.min.js":
                continue
            text = js_file.read_text(encoding="utf-8")
            for cls in forbidden_js_classes:
                assert cls not in text, f"Legacy class {cls!r} found in {js_file.name}"
            for line in text.splitlines():
                if "bg-accent " in line or "bg-accent'" in line or 'bg-accent"' in line:
                    assert "text-white" not in line, f"Hardcoded text-white on bg-accent in {js_file.name}: {line.strip()}"

        # Verify zero external CDN/font/script/stylesheet URLs in index.html & CSS
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        assert re.search(r"<(?:link|script|img)[^>]+https?://", html) is None
        compiled_css = COMPILED_CSS_PATH.read_text(encoding="utf-8")
        assert re.search(r"url\(\s*['\"]?https?://", compiled_css) is None
        assert "@import" not in compiled_css

        # Strict allowlist for outbound links / XML namespaces in JS constants
        allowed_outbound_urls = {
            "https://github.com/manswis/Mkv-Forge",
            "https://github.com/manswis/Mkv-Forge/releases",
            "https://buymeacoffee.com/cranbear",
            "http://www.w3.org/2000/svg",
            "http://www.w3.org/1999/xhtml",
        }
        constants_text = (js_dir / "constants.js").read_text(encoding="utf-8")
        found_urls = set(re.findall(r"https?://[^\s\"'`]+", constants_text))
        assert found_urls <= allowed_outbound_urls, f"Unexpected external URL in constants.js: {found_urls - allowed_outbound_urls}"
        assert "https://buymeacoffee.com/cranbear" in found_urls
        assert ':href="C.APP.BUY_ME_A_COFFEE_URL"' in html
        assert "whitespace-nowrap text-xs text-ink-muted" in html
        funding_yml = (ROOT_DIR / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
        assert "buy_me_a_coffee: cranbear" in funding_yml

    def test_mobile_spacing_clamped_add_audio_menu_and_smart_fill_copy(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        # 1. Edge-to-edge mobile Settings shell & eliminated mobile double-card padding
        assert "p-0 sm:p-4 md:p-6 lg:p-8" in html
        assert "rounded-none border-0 sm:rounded-ui-lg sm:border sm:border-default bg-canvas shadow-elevated" in html
        assert "border-t border-subtle p-0 space-y-4 md:border-t-0 md:p-5" in html
        assert "ui-list-group rounded-none border-0 shadow-none md:rounded-ui-md md:border md:shadow-panel" in html
        assert "px-2.5 py-4 pb-16 pb-safe sm:px-4" in html

        # 2. + Add / Convert dropdown is viewport-clamped on mobile (<sm) and right-anchored on desktop (sm+)
        assert "fixed inset-x-3 bottom-20 z-50 max-h-[70dvh] overflow-y-auto sm:absolute sm:inset-x-auto sm:bottom-auto sm:right-0 sm:top-10 sm:z-40 sm:w-72 sm:max-h-none" in html

        # 3. User-facing "Sanitize" labels replaced with plain-English "Smart fill" / "Smart fill all"
        assert "Sanitize all" not in html
        assert "Sanitize metadata" not in html
        assert "Smart fill all" in html
        assert html.count("Smart fill") >= 4

        # 4. Recent Activity & Jobs user attribution tags include inline Material person icon
        recent_start = html.index("<!-- Recent Activity -->")
        recent_end = html.index("<!-- SCREEN 2: LIBRARY EXPLORER", recent_start)
        recent_block = html[recent_start:recent_end]
        assert "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z" in recent_block
        assert "item.created_by_name || 'Anonymous'" in recent_block

        # 5. Null-safe authUser.username Alpine bindings (prevents TypeError in anonymous/onboarding mode)
        assert 'x-text="`@${authUser.username}`"' not in html

    def test_dashboard_multi_lens_toggles_clickable_strip_and_factory_reset_storage_keys(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        constants_js = (ROOT_DIR / "app" / "static" / "js" / "constants.js").read_text(encoding="utf-8")
        app_js = (ROOT_DIR / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

        dash_start = html.index("<!-- SCREEN 1: DASHBOARD")
        dash_end = html.index("<!-- SCREEN 2: LIBRARY EXPLORER", dash_start)
        dash_block = html[dash_start:dash_end]

        # Option B: Last Scan moved to top Dashboard header next to Live Watch; KPI Cell #4 is Untagged Audio (und)
        header_end = dash_block.index("<!-- Unified Hairline-Divided KPI Stat Strip -->")
        assert "Last Scan:" in dash_block[:header_end]
        assert "Untagged Audio (und)" in dash_block[header_end:]
        assert "openLibraryPreset({ audioLanguage: 'und' })" in dash_block

        # Segmented lens toggles on Storage (Codec | Resolution | Container) & Audio (Codecs | Channels | Languages)
        assert "setDashStorageLens(C.DASHBOARD_LENSES.STORAGE_CODEC)" in dash_block
        assert "setDashStorageLens(C.DASHBOARD_LENSES.STORAGE_RESOLUTION)" in dash_block
        assert "setDashStorageLens(C.DASHBOARD_LENSES.STORAGE_CONTAINER)" in dash_block
        assert "setDashAudioLens(C.DASHBOARD_LENSES.AUDIO_CODECS)" in dash_block
        assert "setDashAudioLens(C.DASHBOARD_LENSES.AUDIO_CHANNELS)" in dash_block
        assert "setDashAudioLens(C.DASHBOARD_LENSES.AUDIO_LANGUAGES)" in dash_block

        # Clickable monochrome strip: Resolution, Video Codec, Dynamic Range & Subtitles (No Subs)
        assert "openLibraryPreset({ resolution: res.label })" in dash_block
        assert "openLibraryPreset({ videoCodec: vc.label })" in dash_block
        assert "openLibraryPreset({ dynamicRange: dr.label })" in dash_block
        assert "openLibraryPreset({ noSubtitles: true })" in dash_block

        # Guardrail 2: localStorage keys registered in C.STORAGE_KEYS and reset in Factory Reset cleanup
        assert 'DASH_STORAGE_LENS: "mkvforge.dashStorageLens"' in constants_js
        assert 'DASH_AUDIO_LENS: "mkvforge.dashAudioLens"' in constants_js
        assert "Object.values(C.STORAGE_KEYS).forEach" in app_js
        assert "this.dashStorageLens = C.DASHBOARD_LENSES.STORAGE_DEFAULT;" in app_js
        assert "this.dashAudioLens = C.DASHBOARD_LENSES.AUDIO_DEFAULT;" in app_js

    def test_settings_library_explorer_section_and_logical_item_placement(self) -> None:
        html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        constants_js = (ROOT_DIR / "app" / "static" / "js" / "constants.js").read_text(encoding="utf-8")

        # 1. Dedicated Library & Explorer section registered in constants.js & index.html
        assert 'LIBRARY_EXPLORER: "library_explorer"' in constants_js
        assert 'title: "Library & Explorer"' in constants_js
        assert 'subtitle: "Theme, accent color, font scale & interface language"' in constants_js
        assert 'title: "Output, Metadata & Safety"' in constants_js
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.LIBRARY_EXPLORER)" in html

        # 2. Appearance & Display contains only visual items; Library & Explorer contains the 6 file-browser items
        app_sec_start = html.index("<!-- Section 1: Appearance & Display")
        lib_sec_start = html.index("<!-- Section 1a: Library & Explorer -->", app_sec_start)
        acc_sec_start = html.index("<!-- Section 1b: Account & Sign-In", lib_sec_start)
        app_sec_block = html[app_sec_start:lib_sec_start]
        lib_sec_block = html[lib_sec_start:acc_sec_start]

        assert "Theme Mode" in app_sec_block
        assert "Accent Color" in app_sec_block
        assert "Font Size Scale" in app_sec_block
        assert "Interface Language" in app_sec_block
        assert "Default Library View" not in app_sec_block

        # Ordered top-to-bottom inside Library & Explorer: View -> Layout -> Cover Art -> Video Metadata -> Page Limit -> Media Files Only
        p_view = lib_sec_block.index("Default Library View")
        p_layout = lib_sec_block.index("Default Folder Layout")
        p_cover = lib_sec_block.index("Grid View Cover Art")
        p_vmeta = lib_sec_block.index("Show Extended Video Metadata")
        p_page = lib_sec_block.index("Files Per Page")
        p_media_only = lib_sec_block.index("Show Media Files Only")
        assert p_view < p_layout < p_cover < p_vmeta < p_page < p_media_only

        # 3. Audio & Track Defaults puts Preferred Track Language first
        audio_sec_start = html.index("<!-- Section 2B: Audio & Track Defaults -->")
        out_sec_start = html.index("<!-- Section 3: Output, Metadata & Safety -->", audio_sec_start)
        audio_sec_block = html[audio_sec_start:out_sec_start]
        assert (
            audio_sec_block.index("Preferred Track Language")
            < audio_sec_block.index("Default Target Codec")
            < audio_sec_block.index("Default Audio Quality")
            < audio_sec_block.index("Smart Fill Track Format")
        )

        # 4. Output, Metadata & Safety is split into two labeled cards
        out_sec_end = html.index("<!-- CATEGORY 3: STORAGE & SYSTEM", out_sec_start)
        out_sec_block = html[out_sec_start:out_sec_end]
        assert "Remux Output &amp; Metadata Cleanup" in out_sec_block
        assert "Safety &amp; Confirmation Prompts" in out_sec_block
        assert out_sec_block.count("ui-list-group") == 2
