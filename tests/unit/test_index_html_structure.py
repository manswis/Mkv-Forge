"""Unit tests guarding static HTML tag integrity and critical UI affordances."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

INDEX_HTML_PATH = Path(__file__).resolve(
).parents[2] / "app" / "static" / "index.html"
APP_JS_PATH = Path(__file__).resolve(
).parents[2] / "app" / "static" / "js" / "app.js"

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


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


class TestIndexHtmlStructure:
    """Verify DOM tag balance and key navigation/confirmation modal structure."""

    def test_index_html_has_balanced_tags(self) -> None:
        parser = _TagBalanceParser()
        parser.feed(_read_index_html())

        assert parser.errors == [
        ], f"HTML tag mismatches found: {parser.errors[:5]}"
        assert parser.stack == [
        ], f"Unclosed HTML tags remaining: {parser.stack[-5:]}"

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
        readme = (INDEX_HTML_PATH.parents[2] /
                  "README.md").read_text(encoding="utf-8")

        assert 'x-show="sec.id === C.SETTINGS_SECTIONS.ABOUT"' in html
        assert "CATEGORY 4: ABOUT & LEGAL TERMS" in html
        normalized_html = _normalize_ws(html)
        assert "7. Trademarks, Non-Affiliation &amp; Codec Licensing Notice" in normalized_html
        assert "Dolby Laboratories Licensing Corporation" in normalized_html
        assert "Dolby Laboratories Licensing Corporation" in readme

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
        api_keys_card_pos = html.index(
            '<!-- API Keys & Integrations -->', local_panel_start)
        disable_card_pos = html.index(
            '<!-- Switch Back to No-Auth Mode -->', local_panel_start)

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
        assert html.index(
            "/static/js/playback_profile.js") < html.index("/static/js/app.js")

        # 3-Step Apple-style Onboarding Wizard landmarks (uniform shell + direct Step 1 form)
        css_text = (INDEX_HTML_PATH.parent / "css" /
                    "app.css").read_text(encoding="utf-8")
        input_css = (
            INDEX_HTML_PATH.parents[2] / "frontend" / "input.css"
        ).read_text(encoding="utf-8")
        assert "/static/css/app.css?v=0.1.0-p18.18" in html
        assert "/static/js/constants.js?v=0.1.0-p18.25" in html
        assert "/static/js/api.js?v=0.1.0-p18.25" in html
        assert "/static/js/nav_history.js?v=0.1.0-p18.13" in html
        assert "/static/js/formatters.js?v=0.1.0-p18.18" in html
        assert "/static/js/playback_profile.js?v=0.1.0-p18.23" in html
        assert "/static/js/editor.js?v=0.1.0-p18.23" in html
        assert "/static/js/app.js?v=0.1.0-p18.25" in html
        assert "/static/js/batch.js?v=0.1.0-p18.23" in html
        assert html.index(
            "/static/js/nav_history.js") < html.index("/static/js/app.js")
        assert "/static/js/charts.js?v=0.1.0-p17.7" in html
        assert html.index(
            "/static/js/test_media.js") < html.index("/static/js/app.js")
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
        normalized_html = _normalize_ws(html)
        assert "Development Transparency" in normalized_html
        assert "MKV Forge is built with LLM assistance." in normalized_html
        assert "makes no outbound network calls at runtime." in normalized_html
        assert "Danger Zone · Factory Reset" in normalized_html
        assert "openFactoryResetModal()" in html
        assert 'id="factory-reset-title"' in html
        assert 'aria-labelledby="factory-reset-title"' in html
        assert "Your Media Files Are 100% Safe" in normalized_html
        assert "submitFactoryReset()" in html
        assert "submitFactoryReset()" in app_js

        # Phase 18: Sign-In screen closes before createdApiKeyModal & batchModal (never nests modals inside isLoginRequired)
        login_idx = html.index('x-show="isLoginRequired"')
        api_key_modal_idx = html.index('x-show="createdApiKeyModal !== null"')
        batch_modal_idx = html.index('x-show="batchModal !== null"')
        assert login_idx < api_key_modal_idx < batch_modal_idx
        login_to_apikey_slice = html[login_idx:api_key_modal_idx]
        assert "</form>" in login_to_apikey_slice
        assert "openBatchModalForSelection()" in html
        assert "runContextMenuAction('batch_folder')" in html

    def test_media_tap_opens_info_modal_and_context_menu_exposes_info_and_process(self) -> None:
        html = _read_index_html()
        app_js = APP_JS_PATH.read_text(encoding="utf-8")

        # All 3 Library Explorer media surfaces (Grid, Mobile List, Desktop Table) open Info Modal on tap
        assert '@click="onFileTileClick(file)"' in html
        assert '@click="openFileInfoModal(file)"' in html
        assert "toggleFileSelection(file.id) : openEditor(file)" not in html
        assert '@click="openEditor(file)"' not in html
        assert "this.openFileInfoModal(file);" in app_js

        # Hybrid Touch + Desktop Grid Selection landmarks (Toolbar Select/Done + 400ms Long-Press + pointer-events fix)
        input_css = (
            INDEX_HTML_PATH.parents[2] / "frontend" / "input.css").read_text(encoding="utf-8")
        css_text = (INDEX_HTML_PATH.parent / "css" /
                    "app.css").read_text(encoding="utf-8")
        assert ".ui-touch-tile" in input_css
        assert "-webkit-touch-callout: none;" in input_css
        assert ".ui-job-badge" in input_css and ".ui-job-badge" in css_text
        assert ".ui-table-actions-col" in input_css and ".ui-table-actions-col" in css_text
        assert html.count("ui-job-badge font-mono transition") == 3
        assert "ui-table-actions-col pl-2 pr-3.5 py-2.5 text-right" in html
        assert 'w-[56px] px-3 py-2.5 text-right' not in html
        assert "ui-touch-tile select-none" in html
        assert "startFileLongPress(file, $event)" in html
        assert "moveFileLongPress($event)" in html
        assert "endFileLongPress()" in html
        assert "handleFileCardContextMenu(file, $event)" in html
        assert "opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto focus-within:opacity-100 focus-within:pointer-events-auto" in html
        assert "x-text=\"mobileSelectMode ? 'Done' : 'Select'\"" in html

        # Info modal footer retains Open in Editor button
        assert "Open in Editor →" in html
        assert "openEditor(target)" in html

        # File context menu 3-group Finder/HIG ordering:
        # Group 1 (Open & Read): Process -> Info -> Download
        # Group 2 (Organize & Maintain): Rename -> Move -> Duplicate -> Refresh
        # Group 3 (Destructive): Delete
        menu_start = html.index('aria-label="Item actions"')
        menu_end = html.index("<!-- File Info / Full Media Metadata Modal", menu_start)
        file_menu_slice = html[menu_start:menu_end]
        process_pos = file_menu_slice.index("runContextMenuAction('process')")
        info_pos = file_menu_slice.index("runContextMenuAction('info')")
        download_pos = file_menu_slice.index("runContextMenuAction('download')")
        rename_pos = file_menu_slice.index("runContextMenuAction('rename')")
        move_pos = file_menu_slice.index("runContextMenuAction('move')")
        duplicate_pos = file_menu_slice.index("runContextMenuAction('duplicate')")
        rescan_pos = file_menu_slice.index("runContextMenuAction('rescan')")
        delete_pos = file_menu_slice.index("runContextMenuAction('delete')")
        assert (
            process_pos
            < info_pos
            < download_pos
            < rename_pos
            < move_pos
            < duplicate_pos
            < rescan_pos
            < delete_pos
        )
        assert file_menu_slice.count('class="ui-menu-divider"') == 2
        assert "<span>Process</span>" in file_menu_slice
        assert "<span>Info</span>" in file_menu_slice
        assert "<span>Download</span>" in file_menu_slice
        assert 'if (action === "process")' in app_js
        assert 'if (action === "download")' in app_js

    def test_batch_studio_user_templates_retag_dropdown_and_selectable_chips(self) -> None:
        html = _read_index_html()
        constants_js = (INDEX_HTML_PATH.parent / "js" /
                        "constants.js").read_text(encoding="utf-8")

        # User-owned templates only + pre-checked Update Template on Queue checkbox + non-overlapping responsive bar
        tpl_bar_start = html.index("User Templates Bar")
        tpl_bar_end = html.index("Compact 3-Tab Rules Panel", tpl_bar_start)
        tpl_bar_html = html[tpl_bar_start:tpl_bar_end]
        assert "flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between" in tpl_bar_html
        assert "ui-select select-dark min-w-0 flex-1 text-xs" in tpl_bar_html
        assert "min-w-[200px]" not in tpl_bar_html
        assert "No template (Custom rules)" in html
        assert "batchModal.updateTemplateOnQueue" in html
        assert "isBatchTemplateDirty" in html
        assert "(Starter)" not in html

        # Retag primary audio uses explicit language dropdown (disabled when checkbox unchecked) & inline diff badge
        assert 'id="batch-retag-lang-select"' in html
        assert ':disabled="!batchModal.rule.track_filter.retag_und_audio_to_preferred"' in html
        assert "batchRetagLanguageOptions" in html
        assert "onBatchRetagLanguageChange()" in html
        assert "Keep untagged (und)" in constants_js
        assert "chip.diff_from && chip.diff_to" in html
        assert "line-through text-[var(--color-status-err)]" in html
        assert "text-[var(--color-status-ok)]" in html

        # Compact 3-Tab Rules Panel (Audio | Metadata & Subs | Output & Rename) + Save As + Inline Rename
        tab_panel_start = html.index('aria-label="Batch rule categories"')
        tab_strip_end = html.index(
            "<!-- Tab 1: Smart Audio Conversion -->", tab_panel_start)
        tab_strip_html = html[tab_panel_start:tab_strip_end]
        assert "flex-nowrap" in tab_strip_html
        assert "overflow-x-auto" in tab_strip_html
        assert "overscroll-x-contain" in tab_strip_html
        assert "no-scrollbar" in tab_strip_html
        assert "touch-pan-x" in tab_strip_html
        assert "min-w-0" in tab_strip_html
        assert "flex-wrap" not in tab_strip_html
        assert tab_strip_html.count("shrink-0 whitespace-nowrap") == 3
        assert "selectBatchTab(C.BATCH.TAB_AUDIO, $el)" in tab_strip_html
        assert "selectBatchTab(C.BATCH.TAB_METADATA, $el)" in tab_strip_html
        assert "selectBatchTab(C.BATCH.TAB_OUTPUT, $el)" in tab_strip_html

        preview_start = html.index(
            "Live Per-File Dry-Run Preview Matrix", tab_panel_start)
        tab_panel_html = html[tab_panel_start:preview_start]
        assert tab_panel_html.count(">Subtitles:</label>") == 1
        assert "C.BATCH.TAB_AUDIO" in tab_panel_html
        assert "C.BATCH.TAB_METADATA" in tab_panel_html
        assert "C.BATCH.TAB_OUTPUT" in tab_panel_html
        assert "Smart Fill clean track names on all kept tracks" in tab_panel_html
        assert "Strip all attachments (fonts, covers)" in tab_panel_html
        assert tab_panel_html.index("Smart Fill clean track names on all kept tracks") < tab_panel_html.index(
            "Strip all attachments (fonts, covers)"
        )
        assert 'x-model="batchModal.rule.container.strip_attachments"' in tab_panel_html
        assert "onBatchRuleChange(C.BATCH.OVERRIDE_STRIP_ATTACHMENTS)" in tab_panel_html
        assert 'id="batch-audio-apply-to"' in tab_panel_html
        assert 'id="batch-output-mode-select"' in tab_panel_html
        assert 'id="batch-rename-pattern-input"' in tab_panel_html
        assert "openBatchDestinationFolderPicker()" in tab_panel_html
        assert "insertBatchRenameToken(tok.token)" in tab_panel_html
        assert "batchAudioApplyToOptions" in tab_panel_html
        assert "onBatchTargetCodecChange()" in tab_panel_html
        assert "Lossless sources only (TrueHD, DTS-HD, PCM)" in tab_panel_html
        assert "Convert When Source Track Is" not in html
        assert "Skip conversion if episode already has Dolby" not in html

        # Folder Picker renders at z-[60] (strictly above z-50 batchModal)
        fp_idx = html.index('x-show="folderPickerOpen && folderTree"')
        assert "z-[60]" in html[fp_idx: fp_idx + 180]

        # Selectable per-file chips + inline rename + collision guard in Dry-Run Preview
        assert "toggleBatchFileChip(item.file_id, chip)" in html
        assert "resetBatchFileOverrides(item.file_id)" in html
        assert "startInlineBatchRename(item)" in html
        assert "commitInlineBatchRename(item.file_id)" in html
        assert "item.is_renamed ? 'line-through text-[var(--color-status-err)]' : 'text-ink-primary'" in html
        assert "batchHasCollisions" in html
        assert ':aria-pressed="chip.selected ? \'true\' : \'false\'"' in html
        preview_html = html[preview_start:]
        assert "C.BATCH.CHIP_CAT_VIDEO" in preview_html
        assert "C.BATCH.CHIP_CAT_AUDIO" in preview_html
        assert preview_html.index("C.BATCH.CHIP_CAT_VIDEO") < preview_html.index(
            "C.BATCH.CHIP_CAT_AUDIO")
        assert "C.BATCH.CHIP_CAT_SUBTITLE" in preview_html
        assert "C.BATCH.CHIP_CAT_TAG" in preview_html

        # Multi-select bar uses industry-standard Select All / Deselect All terminology and groups Clear next to Select All (away from Delete)
        assert "allPageFilesSelected ? 'Deselect All' : 'Select All'" in html
        assert "Unselect page" not in html
        assert "Select page" not in html
        ms_start = html.index("Multi-Select Bulk Action Bar")
        ms_end = html.index("<!-- Library Error -->", ms_start)
        ms_html = html[ms_start:ms_end]
        assert ms_html.index("toggleSelectAllPage()") < ms_html.index("clearSelection()") < ms_html.index(
            "openBatchModalForSelection()") < ms_html.index("openDeleteSelectedFilesModal()")
        assert "order-2 ml-auto" in ms_html
        assert "lg:order-3" in ms_html

        # Clickable job folder path appears across all 3 history surfaces (Dashboard, Jobs Page, Live Jobs Drawer)
        assert html.count('openJobFolder(') == 3
        # title + aria-label + x-text x 3 surfaces
        assert html.count('Fmt.resolveJobFolder(') == 9
