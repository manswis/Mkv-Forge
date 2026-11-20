"""Thin FastAPI application entrypoint and static asset wiring."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_router
from app.api.events import router as events_router
from app.api.file_ops import router as file_ops_router
from app.api.files import router as files_router
from app.api.folders import router as folders_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.maintenance import router as maintenance_router
from app.api.scan import router as scan_router
from app.api.settings import router as settings_router
from app.api.uploads import router as uploads_router
from app.config import AppConstants, Settings, load_settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    AuthRepository,
    JobRepository,
    MediaFileRepository,
)
from app.logging_config import (
    configure_application_logging,
    format_http_access_log,
    is_quiet_access_path,
)
from app.services.auth_service import AuthService
from app.services.file_ops_service import FileOpsService
from app.services.job_worker import JobWorker
from app.services.library_events import LibraryEventBroker
from app.services.maintenance_service import MaintenanceService
from app.services.probe_parser import parse_mkvmerge_json
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker
from app.services.upload_service import UploadService
from app.services.user_settings import UserSettingsService
from app.services.watcher import LibraryWatcher

logger = logging.getLogger(__name__)
http_logger = logging.getLogger("app.http")


def _configure_logging(log_level: str) -> None:
    """Configure root and Uvicorn loggers with local timezone formatting."""
    configure_application_logging(log_level)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and wire a FastAPI instance for MKV Forge."""
    resolved_settings = settings if settings is not None else load_settings()
    _configure_logging(resolved_settings.log_level)
    db_manager = DatabaseManager(resolved_settings.db_path)
    state_repo = AppStateRepository(db_manager)
    auth_repo = AuthRepository(db_manager)
    auth_service = AuthService(state_repo=state_repo, auth_repo=auth_repo)
    media_repo = MediaFileRepository(db_manager)
    job_repo = JobRepository(db_manager)
    scanner = LibraryScanner(
        settings=resolved_settings,
        media_repo=media_repo,
        state_repo=state_repo,
    )
    job_broker = JobSSEBroker()
    upload_service = UploadService(settings=resolved_settings)
    job_worker = JobWorker(
        settings=resolved_settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        broker=job_broker,
        upload_service=upload_service,
    )
    library_broker = LibraryEventBroker()
    user_settings_service = UserSettingsService(
        state_repo=state_repo,
        auth_repo=auth_repo,
    )
    file_ops = FileOpsService(
        settings=resolved_settings,
        media_repo=media_repo,
        job_repo=job_repo,
        library_broker=library_broker,
    )
    library_watcher = LibraryWatcher(
        settings=resolved_settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        library_broker=library_broker,
    )
    maintenance_service = MaintenanceService(
        settings=resolved_settings,
        db=db_manager,
        state_repo=state_repo,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        library_broker=library_broker,
        user_settings_service=user_settings_service,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        _configure_logging(resolved_settings.log_level)
        library_broker.bind_loop(asyncio.get_running_loop())
        db_manager.initialize()
        stored_dr_version = state_repo.get(
            AppConstants.STATE_DYNAMIC_RANGE_CLASSIFIER_VERSION
        )
        force_dr_backfill = (
            stored_dr_version != AppConstants.DYNAMIC_RANGE_CLASSIFIER_VERSION
        )
        backfilled = await asyncio.to_thread(
            media_repo.backfill_video_dynamic_range,
            lambda payload: parse_mkvmerge_json(payload).video_dynamic_range,
            force=force_dr_backfill,
        )
        if force_dr_backfill:
            state_repo.set(
                AppConstants.STATE_DYNAMIC_RANGE_CLASSIFIER_VERSION,
                AppConstants.DYNAMIC_RANGE_CLASSIFIER_VERSION,
            )
        if backfilled > 0:
            logger.info("Backfilled video_dynamic_range for %d cached media files", backfilled)
        await job_worker.recover_from_crash()
        job_worker.start()
        library_watcher.start()
        maintenance_service.start_scheduler()
        logger.info(
            "Starting %s v%s (storage=%s, data=%s, db=%s, watch=%s)",
            AppConstants.APP_NAME,
            AppConstants.APP_VERSION,
            resolved_settings.storage_dir,
            resolved_settings.data_dir,
            resolved_settings.db_path,
            resolved_settings.watch_mode,
        )
        yield
        await maintenance_service.shutdown_scheduler()
        await library_watcher.shutdown()
        library_broker.close()
        await job_worker.shutdown()
        await scanner.shutdown()
        logger.info("Shutting down %s", AppConstants.APP_NAME)

    application = FastAPI(
        title=AppConstants.APP_NAME,
        version=AppConstants.APP_VERSION,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.db = db_manager
    application.state.state_repo = state_repo
    application.state.auth_repo = auth_repo
    application.state.auth_service = auth_service
    application.state.media_repo = media_repo
    application.state.job_repo = job_repo
    application.state.scanner = scanner
    application.state.job_broker = job_broker
    application.state.job_worker = job_worker
    application.state.library_broker = library_broker
    application.state.file_ops = file_ops
    application.state.library_watcher = library_watcher
    application.state.user_settings_service = user_settings_service
    application.state.upload_service = upload_service
    application.state.maintenance_service = maintenance_service

    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(dashboard_router)
    application.include_router(events_router)
    application.include_router(settings_router)
    application.include_router(maintenance_router)
    application.include_router(uploads_router)
    application.include_router(file_ops_router)
    application.include_router(files_router)
    application.include_router(folders_router)
    application.include_router(scan_router)
    application.include_router(jobs_router)

    @application.middleware("http")
    async def local_auth_middleware(request: Request, call_next) -> Response:
        """Resolve current user and enforce local session cookies ONLY in local auth mode."""
        raw_path = request.url.path
        if raw_path.startswith(AppConstants.STATIC_ROUTE_PREFIX) or raw_path in ("/", "/api/health"):
            return await call_next(request)

        auth_mode = await asyncio.to_thread(auth_service.get_auth_mode)
        if auth_mode != AppConstants.AUTH_MODE_LOCAL:
            request.state.current_user = await asyncio.to_thread(
                auth_service.get_anonymous_user
            )
            return await call_next(request)

        cookie_token = request.cookies.get(AppConstants.AUTH_SESSION_COOKIE_NAME)
        resolved_user = await asyncio.to_thread(
            auth_service.resolve_session_user, cookie_token
        )
        request.state.current_user = resolved_user

        if raw_path in AppConstants.AUTH_PUBLIC_PATHS or resolved_user is not None:
            return await call_next(request)

        return JSONResponse(
            status_code=AppConstants.HTTP_UNAUTHORIZED,
            content={"detail": "Authentication required"},
        )

    @application.middleware("http")
    async def observability_and_cache_middleware(
        request: Request, call_next
    ) -> Response:
        """Emit PII-free local-time HTTP diagnostic logs and revalidate static bundles."""
        req_id = uuid.uuid4().hex[: AppConstants.LOG_REQUEST_ID_LENGTH]
        raw_path = request.url.path
        started = time.perf_counter()

        try:
            response: Response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000.0
            route_obj = request.scope.get("route")
            route_label = getattr(route_obj, "path", None) or (
                "/static/*" if raw_path.startswith(AppConstants.STATIC_ROUTE_PREFIX) else raw_path
            )
            http_logger.error(
                format_http_access_log(
                    request_id=req_id,
                    method=request.method,
                    route=route_label,
                    status_code=500,
                    duration_ms=duration_ms,
                    query_keys=request.query_params.keys(),
                    sse_clients=job_broker.subscriber_count,
                    error_type=type(exc).__name__,
                )
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000.0
        if raw_path == "/" or any(
            raw_path.startswith(prefix)
            for prefix in AppConstants.STATIC_REVALIDATE_PREFIXES
        ):
            response.headers[AppConstants.CACHE_CONTROL_HEADER] = (
                AppConstants.CACHE_CONTROL_NO_CACHE
            )

        route_obj = request.scope.get("route")
        route_label = getattr(route_obj, "path", None) or (
            "/static/*" if raw_path.startswith(AppConstants.STATIC_ROUTE_PREFIX) else raw_path
        )
        msg = format_http_access_log(
            request_id=req_id,
            method=request.method,
            route=route_label,
            status_code=response.status_code,
            duration_ms=duration_ms,
            query_keys=request.query_params.keys(),
            sse_clients=job_broker.subscriber_count,
        )
        if response.status_code >= 500:
            http_logger.error(msg)
        elif duration_ms >= AppConstants.LOG_SLOW_REQUEST_MS or response.status_code >= 400:
            http_logger.warning(msg)
        elif is_quiet_access_path(raw_path):
            http_logger.debug(msg)
        else:
            http_logger.info(msg)

        return response

    if resolved_settings.static_dir.is_dir():
        application.mount(
            AppConstants.STATIC_ROUTE_PREFIX,
            StaticFiles(directory=str(resolved_settings.static_dir)),
            name="static",
        )

    @application.get("/favicon.ico", include_in_schema=False, response_model=None)
    async def serve_favicon() -> FileResponse | JSONResponse:
        icon_path = resolved_settings.static_dir / AppConstants.APP_ICON_REL_PATH
        if not icon_path.is_file():
            return JSONResponse(
                status_code=AppConstants.HTTP_NOT_FOUND,
                content={"detail": "App icon not found"},
            )
        return FileResponse(icon_path, media_type=AppConstants.SVG_MEDIA_TYPE)

    @application.get("/", include_in_schema=False, response_model=None)
    async def serve_index() -> FileResponse | JSONResponse:
        index_path = resolved_settings.static_dir / AppConstants.INDEX_FILENAME
        if not index_path.is_file():
            return JSONResponse(
                status_code=AppConstants.HTTP_SERVICE_UNAVAILABLE,
                content={"detail": "Frontend assets not found"},
            )
        return FileResponse(index_path)

    return application


app = create_app()
