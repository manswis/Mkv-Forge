"""Stdlib-only local authentication, scrypt password hashing, and SQLite sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import AppConstants
from app.db.repositories import AppStateRepository, AuthRepository
from app.models.schemas import AuthStatusResponse, AuthUserSummary

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


class AuthError(RuntimeError):
    """Base error for local authentication operations."""


class AuthValidationError(AuthError):
    """Raised when username, display name, or password fails validation."""


class AuthConflictError(AuthError):
    """Raised on duplicate username, already-configured setup, or last-user deletion."""


class AuthCredentialsError(AuthError):
    """Raised on invalid username or password."""


class AuthNotFoundError(AuthError):
    """Raised when a target user record does not exist."""


def hash_password(password: str) -> str:
    """Hash a plaintext password using stdlib `hashlib.scrypt` with a random salt."""
    if not password:
        raise AuthValidationError("Password must not be empty")
    salt = secrets.token_bytes(AppConstants.AUTH_SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(AppConstants.UTF8_ENCODING),
        salt=salt,
        n=AppConstants.AUTH_SCRYPT_N,
        r=AppConstants.AUTH_SCRYPT_R,
        p=AppConstants.AUTH_SCRYPT_P,
        dklen=AppConstants.AUTH_SCRYPT_DKLEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(derived).decode("ascii")
    return (
        f"scrypt${AppConstants.AUTH_SCRYPT_N}${AppConstants.AUTH_SCRYPT_R}"
        f"${AppConstants.AUTH_SCRYPT_P}${salt_b64}${dk_b64}"
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Constant-time verification of a plaintext password against a stored scrypt hash."""
    if not password or not encoded_hash:
        return False
    parts = encoded_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n_param = int(parts[1])
        r_param = int(parts[2])
        p_param = int(parts[3])
        salt = base64.b64decode(parts[4].encode("ascii"), validate=True)
        expected = base64.b64decode(parts[5].encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return False
    if n_param <= 1 or r_param <= 0 or p_param <= 0 or not salt or not expected:
        return False
    try:
        derived = hashlib.scrypt(
            password.encode(AppConstants.UTF8_ENCODING),
            salt=salt,
            n=n_param,
            r=r_param,
            p=p_param,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(derived, expected)


def hash_session_token(raw_token: str) -> str:
    """Hash a raw cookie token with SHA-256 before storing or querying SQLite."""
    return hashlib.sha256(raw_token.encode(AppConstants.UTF8_ENCODING)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _row_to_user_summary(row: dict[str, Any]) -> AuthUserSummary:
    return AuthUserSummary(
        id=int(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=str(row.get("role") or AppConstants.AUTH_ROLE_ADMIN),
        is_anonymous=bool(row.get("is_anonymous")),
        created_at=str(row["created_at"]),
    )


def _validate_username(raw_username: str | None) -> str:
    normalized = (raw_username or "").strip().lower()
    if (
        len(normalized) < AppConstants.AUTH_MIN_USERNAME_LEN
        or len(normalized) > AppConstants.AUTH_MAX_USERNAME_LEN
    ):
        raise AuthValidationError(
            f"Username must be {AppConstants.AUTH_MIN_USERNAME_LEN}–"
            f"{AppConstants.AUTH_MAX_USERNAME_LEN} characters."
        )
    if normalized == AppConstants.AUTH_ANONYMOUS_USERNAME:
        raise AuthValidationError("Username 'anonymous' is reserved.")
    if not _USERNAME_RE.match(normalized):
        raise AuthValidationError(
            "Username may only contain letters, numbers, dots, hyphens, or underscores."
        )
    return normalized


def _validate_password(raw_password: str | None) -> str:
    if not raw_password or len(raw_password) < AppConstants.AUTH_MIN_PASSWORD_LEN:
        raise AuthValidationError(
            f"Password must be at least {AppConstants.AUTH_MIN_PASSWORD_LEN} characters."
        )
    if len(raw_password) > AppConstants.AUTH_MAX_PASSWORD_LEN:
        raise AuthValidationError(
            f"Password must be at most {AppConstants.AUTH_MAX_PASSWORD_LEN} characters."
        )
    return raw_password


def _normalize_display_name(raw_display_name: str | None, fallback_username: str) -> str:
    cleaned = (raw_display_name or "").strip()
    if not cleaned:
        return fallback_username
    if len(cleaned) > AppConstants.AUTH_MAX_DISPLAY_NAME_LEN:
        raise AuthValidationError(
            f"Display name must be at most {AppConstants.AUTH_MAX_DISPLAY_NAME_LEN} characters."
        )
    return cleaned


class AuthService:
    """Manages local accounts, onboarding state, and sliding SQLite sessions."""

    def __init__(
        self,
        *,
        state_repo: AppStateRepository,
        auth_repo: AuthRepository,
    ) -> None:
        self._state_repo = state_repo
        self._auth_repo = auth_repo

    def get_auth_mode(self) -> str:
        """Return persisted auth_mode (`unconfigured` | `anonymous` | `local`)."""
        raw = self._state_repo.get(AppConstants.STATE_KEY_AUTH_MODE)
        if raw in AppConstants.ALLOWED_AUTH_MODES:
            return raw
        return AppConstants.AUTH_MODE_UNCONFIGURED

    def get_anonymous_user(self) -> AuthUserSummary:
        """Return the built-in Anonymous user summary."""
        row = self._auth_repo.get_or_create_anonymous_user()
        return _row_to_user_summary(row)

    def resolve_session_user(self, raw_cookie_token: str | None) -> AuthUserSummary | None:
        """Validate a session cookie token, extend sliding expiry, and return the user."""
        if not raw_cookie_token or not raw_cookie_token.strip():
            return None
        token_hash = hash_session_token(raw_cookie_token.strip())
        row = self._auth_repo.get_session_with_user(token_hash)
        if row is None:
            return None

        now = _utc_now()
        expires_at = _parse_iso(str(row.get("expires_at") or ""))
        if expires_at is None or expires_at <= now:
            self._auth_repo.delete_session(token_hash)
            return None

        # Sliding expiry refresh when more than 1 day has elapsed since last touch
        last_seen = _parse_iso(str(row.get("last_seen_at") or ""))
        if last_seen is None or (now - last_seen) >= timedelta(hours=12):
            new_expiry = _format_iso(now + timedelta(days=AppConstants.AUTH_SESSION_TTL_DAYS))
            self._auth_repo.touch_session(token_hash, new_expiry)

        return _row_to_user_summary(row)

    def get_status(self, raw_cookie_token: str | None) -> AuthStatusResponse:
        """Compute current auth status for `GET /api/auth/status`."""
        mode = self.get_auth_mode()
        if mode == AppConstants.AUTH_MODE_LOCAL:
            user = self.resolve_session_user(raw_cookie_token)
            return AuthStatusResponse(
                auth_mode=AppConstants.AUTH_MODE_LOCAL,
                authenticated=user is not None,
                needs_setup=False,
                user=user,
            )

        anon = self.get_anonymous_user()
        return AuthStatusResponse(
            auth_mode=mode,  # type: ignore[arg-type]
            authenticated=True,
            needs_setup=(mode == AppConstants.AUTH_MODE_UNCONFIGURED),
            user=anon,
        )

    def _issue_session(self, user_id: int) -> str:
        raw_token = secrets.token_urlsafe(AppConstants.AUTH_SESSION_TOKEN_BYTES)
        token_hash = hash_session_token(raw_token)
        expires_at = _format_iso(
            _utc_now() + timedelta(days=AppConstants.AUTH_SESSION_TTL_DAYS)
        )
        self._auth_repo.create_session(
            token_hash=token_hash,
            user_id=user_id,
            expires_at=expires_at,
        )
        return raw_token

    def setup(
        self,
        *,
        mode: str,
        username: str | None = None,
        display_name: str | None = None,
        password: str | None = None,
    ) -> tuple[AuthStatusResponse, str | None]:
        """Configure first-run onboarding or upgrade from `anonymous` -> `local`."""
        current_mode = self.get_auth_mode()

        if mode == AppConstants.AUTH_MODE_ANONYMOUS:
            if current_mode == AppConstants.AUTH_MODE_LOCAL:
                raise AuthConflictError(
                    "Authentication is already enabled. Use disable endpoint with password."
                )
            anon = self.get_anonymous_user()
            self._state_repo.set(
                AppConstants.STATE_KEY_AUTH_MODE, AppConstants.AUTH_MODE_ANONYMOUS
            )
            logger.info("Auth mode configured: auth_mode=%s uid=%d", mode, anon.id)
            return (
                AuthStatusResponse(
                    auth_mode=AppConstants.AUTH_MODE_ANONYMOUS,
                    authenticated=True,
                    needs_setup=False,
                    user=anon,
                ),
                None,
            )

        if mode != AppConstants.AUTH_MODE_LOCAL:
            raise AuthValidationError(f"Unsupported auth mode: {mode!r}")

        if current_mode == AppConstants.AUTH_MODE_LOCAL:
            raise AuthConflictError("Local authentication is already configured.")

        clean_username = _validate_username(username)
        clean_password = _validate_password(password)
        clean_display = _normalize_display_name(display_name, clean_username)
        pw_hash = hash_password(clean_password)

        try:
            user_row = self._auth_repo.create_user(
                username=clean_username,
                display_name=clean_display,
                password_hash=pw_hash,
                role=AppConstants.AUTH_ROLE_ADMIN,
            )
        except sqlite3.IntegrityError as exc:
            raise AuthConflictError("Username already exists.") from exc

        self._state_repo.set(
            AppConstants.STATE_KEY_AUTH_MODE, AppConstants.AUTH_MODE_LOCAL
        )
        user = _row_to_user_summary(user_row)
        raw_token = self._issue_session(user.id)
        logger.info("Auth mode configured: auth_mode=%s uid=%d", mode, user.id)
        return (
            AuthStatusResponse(
                auth_mode=AppConstants.AUTH_MODE_LOCAL,
                authenticated=True,
                needs_setup=False,
                user=user,
            ),
            raw_token,
        )

    def login(self, *, username: str, password: str) -> tuple[AuthUserSummary, str]:
        """Verify local credentials and issue a new session token."""
        if self.get_auth_mode() != AppConstants.AUTH_MODE_LOCAL:
            raise AuthConflictError("Local authentication is not enabled.")

        user_row = self._auth_repo.get_user_by_username(username)
        if user_row is None or not verify_password(
            password, str(user_row.get("password_hash") or "")
        ):
            raise AuthCredentialsError("Invalid username or password.")

        user = _row_to_user_summary(user_row)
        raw_token = self._issue_session(user.id)
        logger.info("Local session created: auth_mode=local uid=%d", user.id)
        return user, raw_token

    def logout(self, raw_cookie_token: str | None) -> None:
        """Revoke the current session token if present."""
        if raw_cookie_token and raw_cookie_token.strip():
            self._auth_repo.delete_session(hash_session_token(raw_cookie_token.strip()))
            logger.info("Local session revoked: auth_mode=%s", self.get_auth_mode())

    def list_users(self) -> list[AuthUserSummary]:
        """Return all local (non-anonymous) user accounts."""
        return [_row_to_user_summary(row) for row in self._auth_repo.list_local_users()]

    def create_local_user(
        self,
        *,
        username: str,
        display_name: str | None,
        password: str,
    ) -> AuthUserSummary:
        """Create a new peer local account."""
        clean_username = _validate_username(username)
        clean_password = _validate_password(password)
        clean_display = _normalize_display_name(display_name, clean_username)
        pw_hash = hash_password(clean_password)
        try:
            row = self._auth_repo.create_user(
                username=clean_username,
                display_name=clean_display,
                password_hash=pw_hash,
                role=AppConstants.AUTH_ROLE_ADMIN,
            )
        except sqlite3.IntegrityError as exc:
            raise AuthConflictError("Username already exists.") from exc
        user = _row_to_user_summary(row)
        logger.info("Local user created: uid=%d", user.id)
        return user

    def change_password(
        self,
        *,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> None:
        """Verify current password and update the user's scrypt hash."""
        row = self._auth_repo.get_user_by_id(user_id)
        if row is None or bool(row.get("is_anonymous")):
            raise AuthNotFoundError("User account not found.")
        if not verify_password(current_password, str(row.get("password_hash") or "")):
            raise AuthCredentialsError("Current password is incorrect.")
        clean_new = _validate_password(new_password)
        self._auth_repo.update_password_hash(user_id, hash_password(clean_new))
        logger.info("Local user password updated: uid=%d", user_id)

    def delete_local_user(self, *, target_user_id: int) -> None:
        """Delete a local account, preventing deletion of the last remaining local user."""
        row = self._auth_repo.get_user_by_id(target_user_id)
        if row is None or bool(row.get("is_anonymous")):
            raise AuthNotFoundError("User account not found.")

        if (
            self.get_auth_mode() == AppConstants.AUTH_MODE_LOCAL
            and self._auth_repo.count_local_users() <= 1
        ):
            raise AuthConflictError(
                "Cannot delete the last remaining local account while sign-in is enabled."
            )

        self._auth_repo.delete_user(target_user_id)
        logger.info("Local user deleted: uid=%d", target_user_id)

    def disable_local_auth(
        self,
        *,
        user_id: int,
        password: str,
    ) -> AuthStatusResponse:
        """Verify password, revoke all sessions, and switch back to anonymous mode."""
        if self.get_auth_mode() != AppConstants.AUTH_MODE_LOCAL:
            raise AuthConflictError("Local authentication is not currently enabled.")

        row = self._auth_repo.get_user_by_id(user_id)
        if row is None or bool(row.get("is_anonymous")):
            raise AuthNotFoundError("User account not found.")
        if not verify_password(password, str(row.get("password_hash") or "")):
            raise AuthCredentialsError("Password is incorrect.")

        self._auth_repo.delete_all_sessions()
        self._state_repo.set(
            AppConstants.STATE_KEY_AUTH_MODE, AppConstants.AUTH_MODE_ANONYMOUS
        )
        anon = self.get_anonymous_user()
        logger.info("Auth mode switched to anonymous: actor_uid=%d", user_id)
        return AuthStatusResponse(
            auth_mode=AppConstants.AUTH_MODE_ANONYMOUS,
            authenticated=True,
            needs_setup=False,
            user=anon,
        )
