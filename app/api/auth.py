"""Local authentication, first-run onboarding, and user management REST endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response

from app.config import AppConstants
from app.models.schemas import (
    AuthDisableRequest,
    AuthLoginRequest,
    AuthPasswordChangeRequest,
    AuthSetupRequest,
    AuthStatusResponse,
    AuthUserCreateRequest,
    AuthUsersListResponse,
    AuthUserSummary,
)
from app.services.auth_service import (
    AuthConflictError,
    AuthCredentialsError,
    AuthNotFoundError,
    AuthService,
    AuthValidationError,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _is_https_request(request: Request) -> bool:
    """Derive cookie Secure flag dynamically from scheme or reverse-proxy header."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        first_proto = forwarded_proto.split(",")[0].strip().lower()
        return first_proto == "https"
    return request.url.scheme.lower() == "https"


def _set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    response.set_cookie(
        key=AppConstants.AUTH_SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=AppConstants.AUTH_SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=AppConstants.AUTH_SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        path="/",
    )


def _require_local_user(request: Request) -> AuthUserSummary:
    current: AuthUserSummary | None = getattr(request.state, "current_user", None)
    if current is None or current.is_anonymous:
        raise HTTPException(
            status_code=AppConstants.HTTP_UNAUTHORIZED,
            detail="Sign in with a local account to perform this action.",
        )
    return current


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(request: Request) -> AuthStatusResponse:
    """Return current auth_mode, onboarding requirement, and resolved user."""
    auth_service: AuthService = request.app.state.auth_service
    cookie_token = request.cookies.get(AppConstants.AUTH_SESSION_COOKIE_NAME)
    return await asyncio.to_thread(auth_service.get_status, cookie_token)


@router.post("/setup", response_model=AuthStatusResponse)
async def setup_auth(
    request: Request,
    response: Response,
    payload: AuthSetupRequest,
) -> AuthStatusResponse:
    """Configure first-run onboarding (`anonymous` or `local`) or upgrade to `local`."""
    auth_service: AuthService = request.app.state.auth_service
    try:
        status, raw_token = await asyncio.to_thread(
            auth_service.setup,
            mode=payload.mode,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except AuthValidationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST, detail=str(exc)
        ) from exc
    except AuthConflictError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT, detail=str(exc)
        ) from exc

    if raw_token is not None:
        _set_session_cookie(response, request, raw_token)
    return status


@router.post("/login", response_model=AuthStatusResponse)
async def login(
    request: Request,
    response: Response,
    payload: AuthLoginRequest,
) -> AuthStatusResponse:
    """Sign in with a local username and password and issue a sliding session cookie."""
    auth_service: AuthService = request.app.state.auth_service
    try:
        user, raw_token = await asyncio.to_thread(
            auth_service.login,
            username=payload.username,
            password=payload.password,
        )
    except AuthCredentialsError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except AuthConflictError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT, detail=str(exc)
        ) from exc

    _set_session_cookie(response, request, raw_token)
    return AuthStatusResponse(
        auth_mode=AppConstants.AUTH_MODE_LOCAL,
        authenticated=True,
        needs_setup=False,
        user=user,
    )


@router.post("/logout", response_model=AuthStatusResponse)
async def logout(request: Request, response: Response) -> AuthStatusResponse:
    """Revoke current session cookie and clear browser cookie."""
    auth_service: AuthService = request.app.state.auth_service
    cookie_token = request.cookies.get(AppConstants.AUTH_SESSION_COOKIE_NAME)
    await asyncio.to_thread(auth_service.logout, cookie_token)
    _clear_session_cookie(response, request)
    return await asyncio.to_thread(auth_service.get_status, None)


@router.get("/users", response_model=AuthUsersListResponse)
async def list_users(request: Request) -> AuthUsersListResponse:
    """List all local (non-anonymous) accounts."""
    _require_local_user(request)
    auth_service: AuthService = request.app.state.auth_service
    users = await asyncio.to_thread(auth_service.list_users)
    return AuthUsersListResponse(users=users)


@router.post(
    "/users",
    response_model=AuthUserSummary,
    status_code=AppConstants.HTTP_CREATED,
)
async def create_user(
    request: Request,
    payload: AuthUserCreateRequest,
) -> AuthUserSummary:
    """Create a new peer local account."""
    _require_local_user(request)
    auth_service: AuthService = request.app.state.auth_service
    try:
        return await asyncio.to_thread(
            auth_service.create_local_user,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except AuthValidationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST, detail=str(exc)
        ) from exc
    except AuthConflictError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT, detail=str(exc)
        ) from exc


@router.patch("/password")
async def change_password(
    request: Request,
    payload: AuthPasswordChangeRequest,
) -> dict[str, bool]:
    """Change the signed-in user's password."""
    current = _require_local_user(request)
    auth_service: AuthService = request.app.state.auth_service
    try:
        await asyncio.to_thread(
            auth_service.change_password,
            user_id=current.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthCredentialsError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except AuthValidationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST, detail=str(exc)
        ) from exc
    except AuthNotFoundError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: int) -> dict[str, bool]:
    """Delete a local account while guarding against deleting the last remaining user."""
    _require_local_user(request)
    auth_service: AuthService = request.app.state.auth_service
    try:
        await asyncio.to_thread(auth_service.delete_local_user, target_user_id=user_id)
    except AuthNotFoundError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND, detail=str(exc)
        ) from exc
    except AuthConflictError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT, detail=str(exc)
        ) from exc
    return {"ok": True}


@router.post("/disable", response_model=AuthStatusResponse)
async def disable_auth(
    request: Request,
    response: Response,
    payload: AuthDisableRequest,
) -> AuthStatusResponse:
    """Verify current user's password and switch back to passwordless anonymous mode."""
    current = _require_local_user(request)
    auth_service: AuthService = request.app.state.auth_service
    try:
        status = await asyncio.to_thread(
            auth_service.disable_local_auth,
            user_id=current.id,
            password=payload.password,
        )
    except AuthCredentialsError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except AuthConflictError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT, detail=str(exc)
        ) from exc
    except AuthNotFoundError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND, detail=str(exc)
        ) from exc

    _clear_session_cookie(response, request)
    return status
