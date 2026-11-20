"""Health check API endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import AppConstants, Settings
from app.models.schemas import HealthResponse
from app.services.health_service import run_health_check

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> JSONResponse:
    """Verify media engines and data directory write access."""
    settings: Settings = request.app.state.settings
    report = await run_health_check(settings)
    status_code = (
        AppConstants.HTTP_OK
        if report.status == AppConstants.STATUS_OK
        else AppConstants.HTTP_SERVICE_UNAVAILABLE
    )
    return JSONResponse(
        status_code=status_code,
        content=report.model_dump(),
    )
