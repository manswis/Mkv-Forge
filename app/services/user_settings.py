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

        known_fields = UserSettings.model_fields.keys()
        filtered = {k: v for k, v in parsed.items() if k in known_fields}
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
        return {
            k: v
            for k, v in parsed.items()
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

    def update(
        self,
        patch: UserSettingsUpdate,
        *,
        user_id: int | None = None,
    ) -> UserSettings:
        """Persist updated settings to their hybrid scope and return merged effective state."""
        updates = patch.model_dump(exclude_none=True)
        if "visible_media_extensions" in updates:
            updates["visible_media_extensions"] = normalize_visible_extensions_csv(
                str(updates["visible_media_extensions"])
            )
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
