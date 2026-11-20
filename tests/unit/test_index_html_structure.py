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
