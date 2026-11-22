"""Integration tests for Phase 5 offline frontend shell, assets, and mobile UX."""

from __future__ import annotations

import re

import httpx

from app.config import AppConstants


class TestPhase5FrontendAssets:
    """Verify all Phase 5 offline assets are served cleanly with zero external URLs."""

    async def test_all_modular_js_and_compiled_css_served_locally(
        self, api_client: httpx.AsyncClient
    ) -> None:
        asset_paths = [
            "/static/css/app.css",
            "/static/js/constants.js",
            "/static/js/formatters.js",
            "/static/js/api.js",
            "/static/js/app.js",
            "/static/js/alpine.min.js",
        ]

        for path in asset_paths:
            resp = await api_client.get(path)
            assert resp.status_code == AppConstants.HTTP_OK, f"Failed to load {path}"
            assert len(resp.content) > 0, f"Empty asset at {path}"
            if path == "/static/css/app.css":
                assert re.search(r"@import\s+['\"]?https?://|url\(['\"]?https?://", resp.text) is None
            elif path != "/static/js/alpine.min.js":
                scrubbed = resp.text.replace("https://github.com/manswis/Mkv-Forge", "")
                assert re.search(r"https?://", scrubbed) is None, (
                    f"External URL found in {path}"
                )

    async def test_index_html_has_offline_mobile_ux_and_phase5_screens(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/")
        assert resp.status_code == AppConstants.HTTP_OK
        html = resp.text

        # Zero external CDN URLs
        assert re.search(r"https?://", html) is None

        # Mobile viewport & safe-area polish (docked progress — zero floating bottom overlap)
        assert "viewport-fit=cover" in html
        assert "min-h-dvh" in html
        assert "pb-safe" in html
        assert "mobile-pill-offset" not in html
        assert "Docked Live Activity Widget" in html
        assert "Docked Mobile Live Activity Strip" in html
        assert "pb-safe-modal" in html
        assert "pb-safe-drawer" in html

        # All modular offline scripts wired in load order
        for script_path in (
            "/static/js/constants.js",
            "/static/js/formatters.js",
            "/static/js/api.js",
            "/static/js/app.js",
            "/static/js/alpine.min.js",
        ):
            assert script_path in html

        # Core Phase 5 screens & controls
        assert "Dashboard" in html
        assert "Library" in html
        assert "Jobs" in html
        assert "needs_conversion" in html
        assert "Extra Metadata" in html
        assert "Run Initial Scan" in html
        assert "Full Re-scan" in html
        assert "showGlobalPill" in html
        assert "isRescanning(file.id)" in html
        assert "visibleAudioChips" in html
        assert "visibleWorkflowChips" in html
        assert "toggleAudioType" in html

    async def test_app_js_wires_hash_routing_and_visibility_resume(
        self, api_client: httpx.AsyncClient
    ) -> None:
        app_js = (await api_client.get("/static/js/app.js")).text
        api_js = (await api_client.get("/static/js/api.js")).text
        css = (await api_client.get("/static/css/app.css")).text

        assert "hashchange" in app_js
        assert "#/library" in app_js
        assert "#/dashboard" in app_js
        assert "#/jobs" in app_js
        assert "visibilitychange" in app_js
        assert "handleAppResume" in app_js

        assert "ManagedEventSource" in api_js
        assert "EventSource" in api_js
        assert "forceReconnect" in api_js

        # Verify custom safe-area / 100dvh utilities compiled into app.css
        assert "100dvh" in css
        assert "safe-area-inset-bottom" in css

    async def test_brand_header_navigates_to_dashboard_without_redundant_nav_button(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        # Brand header (desktop + mobile) navigates to Dashboard alongside explicit nav links
        assert html.count('aria-label="Go to Dashboard"') == 2
        assert "C.APP.NAME" in html
        assert "<span>Dashboard</span>" in html
        assert "grid-cols-2" in html
