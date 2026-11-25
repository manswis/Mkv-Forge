"""Persistent hybrid user & system settings service backed by SQLite."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.config import AppConstants
from app.db.repositories import AppStateRepository, AuthRepository
from app.models.schemas import UserSettings, UserSettingsUpdate
from app.services.folder_service import normalize_visible_extensions_csv
from app.services.playback_profile import (
    PLAYBACK_PRESETS,
    PlaybackProfileSpec,
    resolve_playback_profile,
    sanitize_supported_codecs,
)

logger = logging.getLogger(__name__)


class UserSettingsService:
    """Loads, validates, merges, and persists hybrid UserSettings in SQLite.

    - In anonymous / system context (`user_id=None`): reads and writes all settings
      in `app_state['user_settings']`.
    - In signed-in local user context (`user_id=<int>`):
      - `get(user_id)` merges `{...system_settings, ...user_preferences}` (strictly
        restricted to `AppConstants.USER_SETTING_KEYS`).
      - `update(patch, user_id)` routes `USER_SETTING_KEYS` to `users.preferences_json`
        and `SYSTEM_SETTING_KEYS` to `app_state['user_settings']`.
    """

    def __init__(
        self,
        state_repo: AppStateRepository,
        auth_repo: AuthRepository | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._auth_repo = auth_repo

    @staticmethod
    def _migrate_legacy_audio_dict(raw: dict[str, Any]) -> dict[str, Any]:
        """Seamlessly migrate legacy `default_dolby_*` settings into generic audio defaults."""
        migrated = dict(raw)
        if "default_audio_codec" not in migrated and "default_dolby_codec" in migrated:
            legacy_codec = str(migrated.get("default_dolby_codec") or "").strip().lower()
            if legacy_codec == AppConstants.TARGET_CODEC_AC3:
                migrated["default_audio_codec"] = AppConstants.TARGET_CODEC_AC3
            else:
                migrated["default_audio_codec"] = AppConstants.AUDIO_CODEC_AUTO

        if "default_audio_quality" not in migrated and "default_dolby_bitrate" in migrated:
            legacy_br = str(migrated.get("default_dolby_bitrate") or "").strip().lower()
            migrated["default_audio_quality"] = AppConstants.LEGACY_DOLBY_BITRATE_TO_QUALITY.get(
                legacy_br,
                AppConstants.DEFAULT_AUDIO_QUALITY,
            )
        return migrated

    @staticmethod
    def _normalize_audio_default_updates(updates: dict[str, Any]) -> None:
        """Keep new `default_audio_*` fields and legacy `default_dolby_*` aliases synchronized."""
        if "default_audio_codec" in updates:
            codec = updates["default_audio_codec"]
            if codec in (AppConstants.TARGET_CODEC_EAC3, AppConstants.TARGET_CODEC_AC3):
                updates.setdefault("default_dolby_codec", codec)
        elif "default_dolby_codec" in updates:
            updates["default_audio_codec"] = updates["default_dolby_codec"]

        if "default_audio_quality" in updates:
            quality = updates["default_audio_quality"]
            mapped_br = AppConstants.AUDIO_QUALITY_TO_LEGACY_DOLBY_BITRATE.get(quality)
            if mapped_br:
                updates.setdefault("default_dolby_bitrate", mapped_br)
        elif "default_dolby_bitrate" in updates:
            legacy_br = updates["default_dolby_bitrate"]
            updates["default_audio_quality"] = AppConstants.LEGACY_DOLBY_BITRATE_TO_QUALITY.get(
                legacy_br,
                AppConstants.DEFAULT_AUDIO_QUALITY,
            )

    def _load_system_dict(self) -> dict[str, Any]:
        raw = self._state_repo.get(AppConstants.STATE_KEY_USER_SETTINGS)
        if not raw:
            return UserSettings().model_dump()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt user_settings JSON in app_state; falling back to defaults")
            return UserSettings().model_dump()
        if not isinstance(parsed, dict):
            return UserSettings().model_dump()

        migrated = self._migrate_legacy_audio_dict(parsed)
        known_fields = UserSettings.model_fields.keys()
        filtered = {k: v for k, v in migrated.items() if k in known_fields}
        try:
            return UserSettings.model_validate(filtered).model_dump()
        except ValidationError:
            logger.warning("Invalid user_settings values in app_state; falling back to defaults")
            return UserSettings().model_dump()

    def _load_user_prefs_dict(self, user_id: int) -> dict[str, Any]:
        if self._auth_repo is None:
            return {}
        raw = self._auth_repo.get_user_preferences_json(user_id)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        migrated = self._migrate_legacy_audio_dict(parsed)
        return {
            k: v
            for k, v in migrated.items()
            if k in AppConstants.USER_SETTING_KEYS
        }

    def get(self, *, user_id: int | None = None) -> UserSettings:
        """Return effective settings (merged with per-user preferences if signed in)."""
        system_dict = self._load_system_dict()
        if user_id is None or self._auth_repo is None:
            return UserSettings.model_validate(system_dict)

        user_overrides = self._load_user_prefs_dict(user_id)
        if not user_overrides:
            return UserSettings.model_validate(system_dict)

        try:
            return UserSettings.model_validate({**system_dict, **user_overrides})
        except ValidationError:
            logger.warning(
                "Invalid per-user preferences for uid=%d; falling back to system defaults",
                user_id,
            )
            return UserSettings.model_validate(system_dict)

    def get_playback_profile(self) -> PlaybackProfileSpec:
        """Return the resolved global household PlaybackProfileSpec."""
        system_dict = self._load_system_dict()
        return resolve_playback_profile(
            preset=system_dict.get("playback_profile_preset"),
            supported_codecs=system_dict.get("playback_supported_codecs"),
            max_channels=system_dict.get("playback_max_channels"),
            preferred_target_codec=system_dict.get("playback_preferred_codec"),
        )

    def _normalize_playback_updates(self, updates: dict[str, Any]) -> None:
        """Expand named playback presets or sanitize custom codec lists in place."""
        if "playback_supported_codecs" in updates:
            updates["playback_supported_codecs"] = sanitize_supported_codecs(
                updates["playback_supported_codecs"]
            )
        preset = updates.get("playback_profile_preset")
        if preset == AppConstants.PLAYBACK_PRESET_NEUTRAL:
            updates["playback_supported_codecs"] = []
        elif isinstance(preset, str) and preset in PLAYBACK_PRESETS:
            defn = PLAYBACK_PRESETS[preset]
            updates["playback_supported_codecs"] = list(defn.supported_codecs)
            updates["playback_max_channels"] = defn.max_channels
            updates["playback_preferred_codec"] = defn.preferred_target_codec

    def update(
        self,
        patch: UserSettingsUpdate,
        *,
        user_id: int | None = None,
    ) -> UserSettings:
        """Persist updated settings to their hybrid scope and return merged effective state."""
        updates = patch.model_dump(exclude_none=True)
        if "stage1_scratch_storage_folder" in updates:
            cleaned_folder = str(updates["stage1_scratch_storage_folder"]).strip()
            updates["stage1_scratch_storage_folder"] = cleaned_folder or None
        if "visible_media_extensions" in updates:
            updates["visible_media_extensions"] = normalize_visible_extensions_csv(
                str(updates["visible_media_extensions"])
            )
        self._normalize_playback_updates(updates)
        self._normalize_audio_default_updates(updates)
        system_dict = self._load_system_dict()

        if user_id is None or self._auth_repo is None:
            validated = UserSettings.model_validate({**system_dict, **updates})
            self._state_repo.set(
                AppConstants.STATE_KEY_USER_SETTINGS,
                validated.model_dump_json(),
            )
            return validated

        system_updates = {
            k: v for k, v in updates.items() if k in AppConstants.SYSTEM_SETTING_KEYS
        }
        user_updates = {
            k: v for k, v in updates.items() if k in AppConstants.USER_SETTING_KEYS
        }

        if system_updates:
            updated_system = UserSettings.model_validate(
                {**system_dict, **system_updates}
            )
            self._state_repo.set(
                AppConstants.STATE_KEY_USER_SETTINGS,
                updated_system.model_dump_json(),
            )
            system_dict = updated_system.model_dump()

        existing_user_prefs = self._load_user_prefs_dict(user_id)
        merged_user_prefs = {**existing_user_prefs, **user_updates}
        effective = UserSettings.model_validate({**system_dict, **merged_user_prefs})

        if user_updates:
            persisted_user_prefs = {
                k: getattr(effective, k) for k in merged_user_prefs
            }
            self._auth_repo.set_user_preferences_json(
                user_id,
                json.dumps(persisted_user_prefs, separators=(",", ":")),
            )

        return effective
