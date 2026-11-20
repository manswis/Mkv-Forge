"""Unit tests for app.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConstants, load_settings


class TestLoadSettings:
    """Verify environment variable parsing and validation."""

    def test_defaults_when_env_is_empty(self) -> None:
        settings = load_settings(env={})

        assert settings.storage_dir == Path(AppConstants.DEFAULT_STORAGE_DIR)
        assert settings.data_dir == Path(AppConstants.DEFAULT_DATA_DIR)
        assert settings.health_timeout_sec == AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC
        assert settings.scan_timeout_sec == AppConstants.DEFAULT_SCAN_TIMEOUT_SEC
        assert settings.log_level == AppConstants.DEFAULT_LOG_LEVEL
        assert settings.db_path == Path(AppConstants.DEFAULT_DATA_DIR) / AppConstants.DB_FILENAME
        assert settings.static_dir.name == "static"

    def test_custom_env_overrides(self, tmp_path: Path) -> None:
        custom_static = tmp_path / "custom_static"
        env = {
            AppConstants.ENV_STORAGE_DIR: f"  {tmp_path / 'media'}  ",
            AppConstants.ENV_DATA_DIR: f"  {tmp_path / 'db'}  ",
            AppConstants.ENV_HEALTH_TIMEOUT_SEC: "12.5",
            AppConstants.ENV_LOG_LEVEL: "debug",
        }

        settings = load_settings(env=env, static_dir=custom_static)

        assert settings.storage_dir == tmp_path / "media"
        assert settings.data_dir == tmp_path / "db"
        assert settings.static_dir == custom_static
        assert settings.health_timeout_sec == 12.5
        assert settings.log_level == "DEBUG"

    def test_blank_env_values_fall_back_to_defaults(self) -> None:
        env = {
            AppConstants.ENV_STORAGE_DIR: "   ",
            AppConstants.ENV_DATA_DIR: "",
            AppConstants.ENV_HEALTH_TIMEOUT_SEC: "  ",
            AppConstants.ENV_LOG_LEVEL: "",
        }

        settings = load_settings(env=env)

        assert settings.storage_dir == Path(AppConstants.DEFAULT_STORAGE_DIR)
        assert settings.data_dir == Path(AppConstants.DEFAULT_DATA_DIR)
        assert settings.health_timeout_sec == AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC
        assert settings.log_level == AppConstants.DEFAULT_LOG_LEVEL

    @pytest.mark.parametrize("bad_timeout", ["abc", "0", "-2.5", "60.1", "999"])
    def test_invalid_timeout_raises_value_error(self, bad_timeout: str) -> None:
        env = {AppConstants.ENV_HEALTH_TIMEOUT_SEC: bad_timeout}

        with pytest.raises(ValueError, match=AppConstants.ENV_HEALTH_TIMEOUT_SEC):
            load_settings(env=env)

    @pytest.mark.parametrize(
        "boundary_timeout",
        [
            str(AppConstants.MIN_HEALTH_TIMEOUT_SEC),
            str(AppConstants.MAX_HEALTH_TIMEOUT_SEC),
        ],
    )
    def test_boundary_timeouts_are_accepted(self, boundary_timeout: str) -> None:
        settings = load_settings(
            env={AppConstants.ENV_HEALTH_TIMEOUT_SEC: boundary_timeout}
        )

        assert settings.health_timeout_sec == float(boundary_timeout)

    @pytest.mark.parametrize("bad_level", ["TRACE", "VERBOSE", "123"])
    def test_invalid_log_level_raises_value_error(self, bad_level: str) -> None:
        env = {AppConstants.ENV_LOG_LEVEL: bad_level}

        with pytest.raises(ValueError, match=AppConstants.ENV_LOG_LEVEL):
            load_settings(env=env)
