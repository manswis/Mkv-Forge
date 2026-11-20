"""Integration tests for PII-free HTTP logging and unified /api/events/stream SSE."""

from __future__ import annotations

import logging
import re

import httpx

from app.config import AppConstants
from app.services.sse_broker import JobSSEBroker


class TestPiiFreeHttpObservability:
    """Verify HTTP logs include local-time diagnostics and NEVER leak filenames or query values."""

    async def test_http_logs_record_metrics_without_leaking_query_values_or_names(
        self, api_client: httpx.AsyncClient, caplog
    ) -> None:
        secret_query = "JohnDoePrivateWedding2026"
        secret_folder = "/personal/JaneSmith"

        with caplog.at_level(logging.INFO, logger="app.http"):
            resp = await api_client.get(
                "/api/files",
                params={"q": secret_query, "folder": secret_folder, "page": "1"},
            )

        assert resp.status_code == AppConstants.HTTP_OK
        http_records = [r.getMessage() for r in caplog.records if r.name == "app.http"]
        assert len(http_records) == 1
        logged = http_records[0]

        assert "method=GET" in logged
        assert "route=/api/files" in logged
        assert "status=200" in logged
        assert "ms=" in logged
        assert "qkeys=folder,page,q" in logged
        assert "sse=" in logged
        # Strict PII guarantee: neither query values nor folder/person names appear in logs
        assert secret_query not in logged
        assert secret_folder not in logged
        assert "JohnDoe" not in logged
        assert "JaneSmith" not in logged

    async def test_index_html_has_no_duplicate_alpine_x_init(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Alpine v3 auto-runs init() on x-data; x-init='init()' would double-boot the app."""
        html = (await api_client.get("/")).text
        assert 'x-data="mkvForgeApp()"' in html
        assert 'x-init="init()"' not in html


class TestUnifiedEventsSseAndCliTimestamps:
    """Verify /api/events/stream multiplexes snapshots and prefixes live job logs with local time."""

    async def test_unified_events_stream_headers_and_initial_snapshots(
        self, api_app
    ) -> None:
        from app.api.events import stream_unified_events

        broker: JobSSEBroker = api_app.state.job_broker
        broker.start_job(job_id="job-ts-1", filepath="movies/sample.mkv", stage="remux")
        broker.publish_log(
            job_id="job-ts-1",
            filepath="movies/sample.mkv",
            log_line="Progress: 15%",
        )
        assert len(broker.recent_logs) == 1
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] Progress: 15%$", broker.recent_logs[0])

        class _FakeRequest:
            def __init__(self, app) -> None:
                self.app = app
                self._checks = 0

            async def is_disconnected(self) -> bool:
                self._checks += 1
                return self._checks > 2

        response = await stream_unified_events(_FakeRequest(api_app))
        assert (
            response.headers.get(AppConstants.SSE_ACCEL_BUFFERING_HEADER)
            == AppConstants.SSE_ACCEL_BUFFERING_VALUE
        )
        assert (
            response.headers.get(AppConstants.CACHE_CONTROL_HEADER)
            == AppConstants.CACHE_CONTROL_NO_CACHE
        )
        assert response.ping_interval == AppConstants.SSE_PING_INTERVAL_SEC

        first = await anext(response.body_iterator)
        second = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert {first["event"], second["event"]} == {
            AppConstants.SSE_SCAN_EVENT_NAME,
            AppConstants.SSE_JOB_EVENT_NAME,
        }
