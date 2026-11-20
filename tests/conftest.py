"""Shared pytest fixtures for MKV Forge."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.main import create_app


@pytest.fixture
def project_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def test_settings(tmp_path: Path, project_root: Path) -> Settings:
    """Return isolated Settings backed by temporary storage and data dirs."""
    storage_dir = tmp_path / "storage"
    data_dir = tmp_path / "data"
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        storage_dir=storage_dir,
        data_dir=data_dir,
        static_dir=project_root / "app" / "static",
        health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
        scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
        log_level=AppConstants.DEFAULT_LOG_LEVEL,
        watch_mode=AppConstants.WATCH_MODE_OFF,
        watch_debounce_sec=0.05,
        scan_interval_min=0,
    )


@pytest.fixture
def db_manager(test_settings: Settings) -> DatabaseManager:
    """Initialize and return an isolated WAL SQLite DatabaseManager."""
    manager = DatabaseManager(test_settings.db_path)
    manager.initialize()
    return manager


@pytest.fixture
async def api_app(test_settings: Settings):
    """Yield an initialized FastAPI app instance and clean up on teardown."""
    application = create_app(settings=test_settings)
    application.state.db.initialize()
    try:
        yield application
    finally:
        await application.state.library_watcher.shutdown()
        application.state.library_broker.close()
        await application.state.job_worker.shutdown()
        await application.state.scanner.shutdown()


@pytest.fixture
async def api_client(api_app) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an async HTTP client wired to an isolated FastAPI app."""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client
