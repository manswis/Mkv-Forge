"""Local-time structured logging configuration and PII-safe diagnostic formatters."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from typing import Iterable

from app.config import AppConstants

_CLI_TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s")


def get_local_now() -> datetime:
    """Return the current timezone-aware local datetime honoring container/host TZ."""
    return datetime.now().astimezone()


def format_cli_log_line(raw_line: str, *, now: datetime | None = None) -> str:
    """Prefix a CLI log line with local wall-clock time `[HH:MM:SS]` if not already present."""
    cleaned = str(raw_line).rstrip("\r\n")
    if not cleaned:
        return cleaned
    if _CLI_TIMESTAMP_PREFIX_RE.match(cleaned):
        return cleaned
    ts = (now or get_local_now()).strftime(AppConstants.LOG_CLI_TIME_FORMAT)
    return f"[{ts}] {cleaned}"


class LocalTimeFormatter(logging.Formatter):
    """Formats log records with explicit local time, milliseconds, TZ name, and UTC offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        local_dt = datetime.fromtimestamp(record.created).astimezone()
        base = local_dt.strftime(datefmt or AppConstants.LOG_DATETIME_FORMAT)
        millis = int(record.msecs)
        tz_abbr = local_dt.tzname() or "LOCAL"
        utc_offset = local_dt.strftime("%z")
        return f"{base}.{millis:03d} {tz_abbr} ({utc_offset})"


def configure_application_logging(log_level: str) -> None:
    """Configure root and Uvicorn loggers to emit consistent local-time formatted records."""
    resolved_level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = LocalTimeFormatter(
        fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt=AppConstants.LOG_DATETIME_FORMAT,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)
    if not root_logger.handlers:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    for uvicorn_name in AppConstants.LOG_UVICORN_LOGGERS:
        uv_logger = logging.getLogger(uvicorn_name)
        uv_logger.setLevel(resolved_level)
        if uv_logger.handlers:
            for handler in uv_logger.handlers:
                handler.setFormatter(formatter)

    quiet_third_party_level = getattr(
        logging,
        AppConstants.LOG_QUIET_THIRD_PARTY_LEVEL,
        logging.WARNING,
    )
    for noisy_logger_name in AppConstants.LOG_QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(noisy_logger_name).setLevel(quiet_third_party_level)


def is_quiet_access_path(path: str) -> bool:
    """Return True for high-frequency static/health paths logged only at DEBUG when healthy."""
    if path in AppConstants.LOG_QUIET_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in AppConstants.LOG_QUIET_PREFIXES)


def sanitize_query_keys(query_keys: Iterable[str]) -> str:
    """Return sorted unique query parameter names ONLY (never parameter values / PII)."""
    cleaned = sorted({str(key).strip() for key in query_keys if str(key).strip()})
    return ",".join(cleaned) if cleaned else "-"


def format_http_access_log(
    *,
    request_id: str,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
    query_keys: Iterable[str],
    sse_clients: int | None = None,
    error_type: str | None = None,
) -> str:
    """Build a concise, strictly PII-free diagnostic log line for an HTTP request."""
    parts = [
        f"req={request_id}",
        f"method={method}",
        f"route={route}",
        f"status={status_code}",
        f"ms={duration_ms:.1f}",
        f"qkeys={sanitize_query_keys(query_keys)}",
    ]
    if sse_clients is not None:
        parts.append(f"sse={sse_clients}")
    if error_type:
        parts.append(f"err={error_type}")
    return " ".join(parts)
