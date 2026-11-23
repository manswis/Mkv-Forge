"""API endpoints for utility tools (Speaker & Audio System Test Generator)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import AppConstants
from app.models.schemas import TestMediaGenerateRequest, TestMediaGenerateResponse
from app.services.folder_sync import FolderReconcileCache
from app.services.test_media_generator import (
    TestMediaCollisionError,
    TestMediaGenerationError,
    TestMediaInsufficientSpaceError,
    TestMediaTimeoutError,
    TestMediaValidationError,
    generate_test_media,
)

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.post(
    "/generate-test-media",
    response_model=TestMediaGenerateResponse,
    status_code=AppConstants.HTTP_CREATED,
)
async def create_test_media(
    payload: TestMediaGenerateRequest,
    request: Request,
) -> TestMediaGenerateResponse:
    """Synchronously synthesize a speaker & audio-system test clip inside STORAGE_DIR."""
    settings = request.app.state.settings
    scanner = request.app.state.scanner
    library_broker = request.app.state.library_broker

    try:
        result = await generate_test_media(
            payload,
            storage_root=settings.storage_dir,
            scanner=scanner,
            library_broker=library_broker,
        )
        folder_cache: FolderReconcileCache | None = getattr(
            request.app.state, "folder_reconcile_cache", None
        )
        if folder_cache is not None:
            folder_cache.invalidate_folder(
                payload.destination_dir or "/", include_parent=True
            )
            if result.filepath:
                folder_cache.invalidate_for_filepath(result.filepath)
        return result
    except TestMediaValidationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except TestMediaCollisionError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=str(exc),
        ) from exc
    except TestMediaInsufficientSpaceError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_INSUFFICIENT_STORAGE,
            detail=str(exc),
        ) from exc
    except TestMediaTimeoutError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except TestMediaGenerationError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
