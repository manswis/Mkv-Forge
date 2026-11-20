"""Integration tests for Phase 1 HTTP endpoints and offline static assets."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import AppConstants, Settings
from app.main import create_app
from app.models.schemas import (
    DataDirCheckStatus,
    HealthResponse,
    HealthToolsReport,
    ToolCheckStatus,
)


def _healthy_report(data_path: Path) -> HealthResponse:
    return HealthResponse(
        status=AppConstants.STATUS_OK,
        app=AppConstants.APP_NAME,
        version=AppConstants.APP_VERSION,
        tools=HealthToolsReport(
            mkvmerge=ToolCheckStatus(available=True, version="mkvmerge v74.0.0"),
            ffmpeg=ToolCheckStatus(available=True, version="ffmpeg version 6.1"),
            ffprobe=ToolCheckStatus(available=True, version="ffprobe version 6.1"),
        ),
        data_dir=DataDirCheckStatus(path=str(data_path), writable=True),
    )


def _degraded_report(data_path: Path) -> HealthResponse:
    return HealthResponse(
        status=AppConstants.STATUS_DEGRADED,
        app=AppConstants.APP_NAME,
        version=AppConstants.APP_VERSION,
        tools=HealthToolsReport(
            mkvmerge=ToolCheckStatus(available=False, error="not found"),
            ffmpeg=ToolCheckStatus(available=True, version="ffmpeg version 6.1"),
            ffprobe=ToolCheckStatus(available=True, version="ffprobe version 6.1"),
        ),
        data_dir=DataDirCheckStatus(path=str(data_path), writable=True),
    )


class TestHealthEndpoint:
    """Verify GET /api/health HTTP semantics and payload shape."""

    async def test_returns_200_when_healthy(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        report = _healthy_report(test_settings.data_dir)
        with patch(
            "app.api.health.run_health_check",
            new=AsyncMock(return_value=report),
        ):
            response = await api_client.get("/api/health")

        assert response.status_code == AppConstants.HTTP_OK
        body = response.json()
        assert body["status"] == AppConstants.STATUS_OK
        assert body["app"] == AppConstants.APP_NAME
        assert body["tools"]["mkvmerge"]["available"] is True
        assert body["tools"]["ffmpeg"]["available"] is True
        assert body["tools"]["ffprobe"]["available"] is True
        assert body["data_dir"]["writable"] is True

    async def test_returns_503_when_degraded(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        report = _degraded_report(test_settings.data_dir)
        with patch(
            "app.api.health.run_health_check",
            new=AsyncMock(return_value=report),
        ):
            response = await api_client.get("/api/health")

        assert response.status_code == AppConstants.HTTP_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["status"] == AppConstants.STATUS_DEGRADED
        assert body["tools"]["mkvmerge"]["available"] is False


class TestOfflineStaticAssets:
    """Verify offline frontend assets are served locally with zero CDN calls."""

    async def test_index_page_loads_and_has_no_external_urls(
        self, api_client: httpx.AsyncClient
    ) -> None:
        response = await api_client.get("/")

        assert response.status_code == AppConstants.HTTP_OK
        html = response.text
        assert "/static/css/app.css" in html
        assert "/static/js/alpine.min.js" in html
        assert re.search(r"https?://", html) is None

    async def test_vendored_css_and_alpine_js_are_served(
        self, api_client: httpx.AsyncClient
    ) -> None:
        css_resp = await api_client.get("/static/css/app.css")
        js_resp = await api_client.get("/static/js/alpine.min.js")

        assert css_resp.status_code == AppConstants.HTTP_OK
        assert len(css_resp.content) > 0
        assert js_resp.status_code == AppConstants.HTTP_OK
        assert len(js_resp.content) > 0

    async def test_missing_index_returns_503(self, tmp_path: Path) -> None:
        empty_static = tmp_path / "empty_static"
        empty_static.mkdir()
        settings = Settings(
            storage_dir=tmp_path,
            data_dir=tmp_path,
            static_dir=empty_static,
            health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
            scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
            log_level=AppConstants.DEFAULT_LOG_LEVEL,
        )
        app = create_app(settings=settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/")

        assert response.status_code == AppConstants.HTTP_SERVICE_UNAVAILABLE
