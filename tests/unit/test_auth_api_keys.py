"""Unit tests for API key generation, hashing, repository persistence, and auth resolution."""

from __future__ import annotations

import string
from pathlib import Path

import pytest

from app.api.dashboard import _build_dashboard_analytics
from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, AuthRepository
from app.services.auth_service import (
    AuthService,
    AuthValidationError,
    generate_api_key,
    hash_api_key,
)


class TestApiKeyGenerationAndHashing:
    """Verify cryptographic properties, prefix masking, and SHA-256 hashing."""

    def test_generates_valid_api_key_structure(self) -> None:
        raw_key, prefix, key_hash = generate_api_key()

        assert raw_key.startswith(AppConstants.AUTH_API_KEY_PREFIX)
        assert len(raw_key) >= 40
        assert prefix.startswith("mkv_")
        assert "••••" in prefix
        assert prefix.endswith(raw_key[-4:])
        assert len(key_hash) == 64
        assert all(c in string.hexdigits for c in key_hash)

    def test_hash_api_key_deterministic_and_strips_whitespace(self) -> None:
        raw_key = "mkv_sample_test_key_12345"
        hash_1 = hash_api_key(raw_key)
        hash_2 = hash_api_key(f"  {raw_key}  \n")

        assert hash_1 == hash_2
        assert len(hash_1) == 64

    def test_two_generated_keys_are_unique(self) -> None:
        key_1, _, hash_1 = generate_api_key()
        key_2, _, hash_2 = generate_api_key()

        assert key_1 != key_2
        assert hash_1 != hash_2


class TestApiKeyRepositoryAndService:
    """Verify database CRUD operations, cascade deletes, and service resolution."""

    def test_api_key_crud_lifecycle(self, db_manager: DatabaseManager) -> None:
        auth_repo = AuthRepository(db_manager)
        user = auth_repo.create_user(
            username="admin",
            display_name="Administrator",
            password_hash="scrypt$test",
        )
        user_id = int(user["id"])

        # 1. Create API key
        raw_key, prefix, key_hash = generate_api_key()
        created = auth_repo.create_api_key(
            user_id=user_id,
            name="Homepage Widget",
            key_prefix=prefix,
            key_hash=key_hash,
        )
        assert created["name"] == "Homepage Widget"
        assert created["key_prefix"] == prefix
        assert created["last_used_at"] is None
        assert created["creator_name"] == "Administrator"

        # 2. List API keys
        keys = auth_repo.list_api_keys(user_id)
        assert len(keys) == 1
        assert keys[0]["id"] == created["id"]
        assert keys[0]["creator_name"] == "Administrator"

        # 3. Lookup with joined user
        joined = auth_repo.get_api_key_with_user(key_hash)
        assert joined is not None
        assert joined["username"] == "admin"
        assert joined["api_key_name"] == "Homepage Widget"

        # 4. Touch key
        auth_repo.touch_api_key(created["id"])
        updated_keys = auth_repo.list_api_keys(user_id)
        assert updated_keys[0]["last_used_at"] is not None

        # 5. Delete key
        deleted = auth_repo.delete_api_key(created["id"], user_id)
        assert deleted is True
        assert len(auth_repo.list_api_keys(user_id)) == 0
        assert auth_repo.get_api_key_with_user(key_hash) is None

    def test_api_key_cascades_on_user_delete(self, db_manager: DatabaseManager) -> None:
        auth_repo = AuthRepository(db_manager)
        user = auth_repo.create_user(
            username="tempuser",
            display_name="Temp User",
            password_hash="scrypt$test",
        )
        user_id = int(user["id"])

        _, prefix, key_hash = generate_api_key()
        auth_repo.create_api_key(
            user_id=user_id,
            name="Temp Key",
            key_prefix=prefix,
            key_hash=key_hash,
        )
        assert len(auth_repo.list_api_keys(user_id)) == 1

        # Delete user -> API key cascades
        auth_repo.delete_user(user_id)
        assert len(auth_repo.list_api_keys(user_id)) == 0
        assert auth_repo.get_api_key_with_user(key_hash) is None

    def test_auth_service_api_key_operations(self, db_manager: DatabaseManager) -> None:
        auth_repo = AuthRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        service = AuthService(state_repo=state_repo, auth_repo=auth_repo)

        user = auth_repo.create_user(
            username="botmaster",
            display_name="Bot Master",
            password_hash="scrypt$test",
        )
        user_id = int(user["id"])

        # Create key through service
        created = service.create_api_key(user_id=user_id, name="Monitoring")
        assert created["name"] == "Monitoring"
        assert created["creator_name"] == "Bot Master"
        raw_key = created["key"]
        assert raw_key.startswith("mkv_")

        # Resolve user with valid key
        resolved = service.resolve_api_key_user(raw_key)
        assert resolved is not None
        assert resolved.username == "botmaster"
        assert resolved.is_anonymous is False

        # Resolve with invalid key
        assert service.resolve_api_key_user("mkv_nonexistent_key_12345") is None
        assert service.resolve_api_key_user("") is None
        assert service.resolve_api_key_user(None) is None

        # Name length validation
        with pytest.raises(AuthValidationError):
            service.create_api_key(user_id=user_id, name="A" * 70)

        # Delete key
        assert service.delete_api_key(user_id=user_id, api_key_id=created["id"]) is True
        assert service.resolve_api_key_user(raw_key) is None


class TestDashboardTopCodecAnalytics:
    """Verify dashboard analytics computes flattened top codec label and percentage."""

    def test_computes_top_codec_when_tracks_exist(self, tmp_path: Path) -> None:
        raw = {
            "codec_counts": {
                "EAC3": 25,
                "AC3": 10,
                "AAC": 5,
            },
            "mkv_bytes": 1000,
            "mkv_count": 5,
        }
        analytics = _build_dashboard_analytics(raw, tmp_path)

        assert analytics.total_audio_tracks == 40
        assert analytics.top_codec_label == "DD+ (EAC3)"
        assert analytics.top_codec_percent == 62.5

    def test_computes_empty_top_codec_when_no_audio(self, tmp_path: Path) -> None:
        raw = {
            "codec_counts": {},
            "mkv_bytes": 0,
            "mkv_count": 0,
        }
        analytics = _build_dashboard_analytics(raw, tmp_path)

        assert analytics.total_audio_tracks == 0
        assert analytics.top_codec_label == ""
        assert analytics.top_codec_percent == 0.0
