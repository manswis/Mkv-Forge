"""Unit tests guarding static HTML tag integrity and critical UI affordances."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

INDEX_HTML_PATH = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"
APP_JS_PATH = Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "app.js"

HTML_VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
        # Self-closing SVG primitives commonly written without explicit end tags
        "circle",
        "line",
        "path",
        "polygon",
        "polyline",
        "rect",
    }
)


class _TagBalanceParser(HTMLParser):
    """Lightweight HTML/SVG tag-stack validator for detecting unclosed/mismatched elements."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in HTML_VOID_TAGS:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if tag in HTML_VOID_TAGS:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            top = self.stack[-1] if self.stack else None
            self.errors.append(
                f"Mismatched closing </{tag}> at line {self.getpos()[0]} (top={top})"
            )


def _read_index_html() -> str:
    return INDEX_HTML_PATH.read_text(encoding="utf-8")


class TestIndexHtmlStructure:
    """Verify DOM tag balance and key navigation/confirmation modal structure."""

    def test_index_html_has_balanced_tags(self) -> None:
        parser = _TagBalanceParser()
        parser.feed(_read_index_html())

        assert parser.errors == [], f"HTML tag mismatches found: {parser.errors[:5]}"
        assert parser.stack == [], f"Unclosed HTML tags remaining: {parser.stack[-5:]}"

    def test_logout_uses_modal_popup_instead_of_inline_sidebar_confirm(self) -> None:
        html = _read_index_html()
        app_js = APP_JS_PATH.read_text(encoding="utf-8")

        # Modal popup with alertdialog semantics exists
        assert 'id="logout-confirm-title"' in html
        assert 'aria-labelledby="logout-confirm-title"' in html
        assert 'x-show="logoutConfirmOpen"' in html
        assert '@keydown.escape.window="closeLogoutConfirm()"' in html

        # Inline sidebar confirmation state has been removed
        assert 'x-show="!logoutConfirmOpen"' not in html

        # Controller helpers exist in app.js
        assert "openLogoutConfirm()" in app_js
        assert "closeLogoutConfirm()" in app_js

    def test_mobile_menu_ends_with_user_profile_and_logout(self) -> None:
        html = _read_index_html()
        menu_start = html.index('aria-label="Navigation and actions"')
        menu_end = html.index("Docked Mobile Live Activity Strip", menu_start)
        menu_block = html[menu_start:menu_end]

        about_pos = menu_block.index("About &amp; Legal Terms")
        user_pos = menu_block.index("currentUserDisplayName")
        logout_pos = menu_block.index('openLogoutConfirm()')

        assert about_pos < user_pos < logout_pos
        assert "overflow-y-auto" in menu_block

    def test_about_section_has_settings_icon_and_dedicated_category(self) -> None:
        html = _read_index_html()

        assert 'x-show="sec.id === C.SETTINGS_SECTIONS.ABOUT"' in html
        assert "CATEGORY 4: ABOUT & LEGAL TERMS" in html

    def test_editor_displays_video_and_audio_track_sizes_when_available(self) -> None:
        html = _read_index_html()

        assert (
            "(row.track.type === 'video' || row.track.type === 'audio') && row.track.size_bytes > 0"
            in html
        )
        assert 'x-text="Fmt.formatBytes(row.track.size_bytes)"' in html

    def test_api_keys_card_is_inside_local_auth_panel_and_uses_fmt_formatter(self) -> None:
        html = _read_index_html()

        local_panel_start = html.index('<!-- Local Auth Mode Panel -->')
        api_keys_card_pos = html.index('<!-- API Keys & Integrations -->', local_panel_start)
        disable_card_pos = html.index('<!-- Switch Back to No-Auth Mode -->', local_panel_start)

        # API Keys card is scoped inside Local Auth Mode Panel (hidden in Anonymous mode)
        assert local_panel_start < api_keys_card_pos < disable_card_pos
        assert "F.formatDateTime(" not in html
        assert "Fmt.formatDateTime(k.created_at)" in html
        assert "Fmt.formatDateTime(k.last_used_at)" in html
        assert "Fmt.formatDateTime(createdApiKeyModal.created_at)" in html

    def test_phase14_convert_audio_progressive_disclosure_landmarks(self) -> None:
        html = _read_index_html()
        app_js = APP_JS_PATH.read_text(encoding="utf-8")

        # + Add / Convert dropdown renders source-aware recommendations + Custom conversion
        assert "editorRecommendedAudioOptions" in html
        assert "openCustomAddAudio()" in html

        # Progressive disclosure Convert Audio modal landmarks
        assert "addAudioDraft && editorDraftCodecMeta" in html
        assert "addAudioDraft.advancedOpen = !addAudioDraft.advancedOpen" in html
        assert "editorDraftAdvancedSummary" in html
        assert "editorDraftAvailableCodecs" in html
        assert "editorDraftChannelOptions" in html
        assert "editorDraftBitrateConfig" in html
        assert "editorDraftNotices" in html
        assert "editorDraftBlockStatus" in html
        assert "max-h-[90dvh] overflow-y-auto" in html

        # Controller handlers exist in app.js
        assert "openCustomAddAudio()" in app_js
        assert "onDraftTargetCodecChange(" in app_js
        assert "onDraftChannelsChange(" in app_js

    def test_phase15_three_step_onboarding_and_playback_profile_landmarks(self) -> None:
        html = _read_index_html()
        app_js = APP_JS_PATH.read_text(encoding="utf-8")

        # Playback Profile script loaded before app.js
        assert html.index("/static/js/playback_profile.js") < html.index("/static/js/app.js")

        # 3-Step Apple-style Onboarding Wizard landmarks (uniform shell + direct Step 1 form)
        css_text = (INDEX_HTML_PATH.parent / "css" / "app.css").read_text(encoding="utf-8")
        input_css = (
            INDEX_HTML_PATH.parents[2] / "frontend" / "input.css"
        ).read_text(encoding="utf-8")
        assert "/static/css/app.css?v=0.1.0-p17.8" in html
        assert "/static/js/app.js?v=0.1.0-p17.7" in html
        assert "/static/js/charts.js?v=0.1.0-p17.7" in html
        assert html.index("/static/js/test_media.js") < html.index("/static/js/app.js")
        assert "testMediaActivePresetId === preset.id" in html

        # Phase 16 Speaker & Audio System Test Generator entrypoints & exact ordering
        # Desktop sidebar: Library -> Audio Test -> Jobs
        lib_nav_idx = html.index("<span>Library</span>")
        audio_test_nav_idx = html.index("<span>Audio Test</span>")
        jobs_nav_idx = html.index("<span>Jobs</span>")
        assert lib_nav_idx < audio_test_nav_idx < jobs_nav_idx

        # Mobile 3-dot menu: Full Re-scan -> Audio Test Generator -> Select
        full_rescan_idx = html.index("<span>Full Re-scan</span>")
        mobile_audio_test_idx = html.index("<span>Audio Test Generator</span>")
        mobile_select_idx = html.index("toggleMobileSelectFromMenu()")
        assert full_rescan_idx < mobile_audio_test_idx < mobile_select_idx

        # Contextual Settings & Library entrypoints + Modal landmark
        assert "Generate Speaker Test File…" in html
        assert 'aria-label="Speaker &amp; Audio System Test Generator"' in html
        assert "submitTestMediaGenerate()" in html
        assert "submitTestMediaGenerate()" in app_js
        assert "onboarding-wizard-shell" in html
        for selector in (
            ".onboarding-wizard-shell",
            ".onboarding-header-row",
            ".onboarding-header-slot",
            ".onboarding-body",
            ".onboarding-step-center",
            ".onboarding-step-inner",
            ".onboarding-footer",
            ".onboarding-chevron-open",
            ".onboarding-step-fwd",
            ".onboarding-step-back",
            "@keyframes onboarding-slide-fwd",
            "@keyframes onboarding-slide-back",
            "scrollbar-gutter",
        ):
            assert selector in input_css, f"Missing {selector} in frontend/input.css (Docker CSS source)"
            assert selector in css_text, f"Missing {selector} in app/static/css/app.css"
        assert "onboarding-header-row" in html
        assert 'x-ref="onboardingBody"' in html
        assert "onboarding-footer" in html
        assert "C.ONBOARDING.STEP_ACCESS" in html
        assert "C.ONBOARDING.STEP_SOUND_SYSTEM" in html
        assert "C.ONBOARDING.STEP_INITIALIZE" in html
        assert "onboardingStepAnimClass" in html
        assert "Create Admin Account" not in html
        assert "onboardingNextFromAccess()" in html
        assert "onboardingContinueAnonymous()" in html
        assert "Continue Without Sign-In" in html
        assert "Re-enter Password" in html
        assert "Start Scanning Library" in html
        assert "Enter Dashboard without scanning" not in html

        # Settings Sound System & Playback Profile section
        assert "C.SETTINGS_SECTIONS.PLAYBACK_PROFILE" in html
        assert "selectSettingsPlaybackPreset(" in html
        assert "toggleSettingsPlaybackCodec(" in html

        # Dashboard generic profile-driven KPI replaces hardcoded AAC 5.1 Only card
        assert "dashboard.stats.playback_profile_configured" in html
        assert "dashboard.stats.needs_conversion" in html
        assert "AAC 5.1 Only" not in html

        # Controller handlers exist
        assert "finishOnboarding(" in app_js
        assert "selectSettingsPlaybackPreset(" in app_js

        # Factory Reset Danger Zone + plain-language alertdialog modal landmarks
        assert "Danger Zone · Factory Reset" in html
        assert "openFactoryResetModal()" in html
        assert 'id="factory-reset-title"' in html
        assert 'aria-labelledby="factory-reset-title"' in html
        assert "Your Media Files Are 100% Safe" in html
        assert "submitFactoryReset()" in html
        assert "submitFactoryReset()" in app_js


