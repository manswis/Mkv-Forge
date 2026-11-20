"""Unit tests for local-time structured logging and PII-free diagnostic formatters."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.config import AppConstants
from app.logging_config import (
    LocalTimeFormatter,
    configure_application_logging,
    format_cli_log_line,
    format_http_access_log,
    is_quiet_access_path,
    sanitize_query_keys,
)


class TestLocalTimeFormatterAndCliPrefix:
    """Verify explicit local-time formatting with TZ abbreviation and UTC offset."""

    def test_local_time_formatter_includes_millis_tz_and_offset(self) -> None:
        formatter = LocalTimeFormatter(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
        )
        record = logging.LogRecord(
            name="app.http",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        rendered = formatter.format(record)
        assert re.match(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \S+ \([+-]\d{4}\) INFO\s+\[app\.http\] hello$",
            rendered,
        ), f"Unexpected log format: {rendered!r}"

    def test_format_cli_log_line_prefixes_once_and_handles_empty(self) -> None:
        fixed_dt = datetime(2026, 11, 19, 20, 35, 9, tzinfo=timezone.utc)
        prefixed = format_cli_log_line("Muxing took 4 seconds.\r\n", now=fixed_dt)
        assert prefixed == "[20:35:09] Muxing took 4 seconds."

        # Idempotent when line already carries a [HH:MM:SS] prefix
        assert format_cli_log_line(prefixed, now=fixed_dt) == prefixed
        # Empty/blank lines degrade safely without phantom timestamps
        assert format_cli_log_line("") == ""
        assert format_cli_log_line("\n") == ""


class TestPiiFreeHttpLogFormatting:
    """Verify query sanitization, quiet path classification, and diagnostic fields."""

    def test_sanitize_query_keys_sorts_deduplicates_and_handles_empty(self) -> None:
        assert sanitize_query_keys([]) == "-"
        assert sanitize_query_keys(["", "   "]) == "-"
        assert sanitize_query_keys(["sort", "q", "folder", "q"]) == "folder,q,sort"

    def test_format_http_access_log_contains_diagnostics_without_values(self) -> None:
        line = format_http_access_log(
            request_id="abc12345",
            method="GET",
            route="/api/files",
            status_code=200,
            duration_ms=14.26,
            query_keys=["q", "folder"],
            sse_clients=1,
        )
        assert line == "req=abc12345 method=GET route=/api/files status=200 ms=14.3 qkeys=folder,q sse=1"

        err_line = format_http_access_log(
            request_id="deadbeef",
            method="POST",
            route="/api/jobs",
            status_code=500,
            duration_ms=2.0,
            query_keys=[],
            error_type="RuntimeError",
        )
        assert "err=RuntimeError" in err_line
        assert "qkeys=-" in err_line

    def test_is_quiet_access_path(self) -> None:
        assert is_quiet_access_path("/api/health") is True
        assert is_quiet_access_path("/static/js/app.js") is True
        assert is_quiet_access_path("/api/files") is False
        assert is_quiet_access_path("/") is False

    def test_configure_application_logging_handles_unknown_level_safely(self) -> None:
        configure_application_logging("NOT_A_REAL_LEVEL")
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert any(isinstance(h.formatter, LocalTimeFormatter) for h in root.handlers)

    def test_configure_application_logging_silences_watchfiles_change_detected_spam(
        self,
    ) -> None:
        configure_application_logging(AppConstants.DEFAULT_LOG_LEVEL)
        for logger_name in AppConstants.LOG_QUIET_THIRD_PARTY_LOGGERS:
            noisy_logger = logging.getLogger(logger_name)
            assert noisy_logger.level == logging.WARNING
            assert noisy_logger.isEnabledFor(logging.INFO) is False
            assert noisy_logger.isEnabledFor(logging.WARNING) is True

