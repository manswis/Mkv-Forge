"""Library scanner trigger and SSE progress stream API endpoints."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.config import AppConstants
from app.models.schemas import ScanProgressPayload
from app.services.library_events import LibraryEventBroker
from app.services.scanner import LibraryScanner

router = APIRouter(prefix="/api/scan", tags=["scan"])


@router.post("", response_model=ScanProgressPayload, status_code=AppConstants.HTTP_ACCEPTED)
async def trigger_scan(
    request: Request,
    full: bool = Query(default=False),
) -> JSONResponse:
    """Trigger an incremental (or full) background scan; reject overlaps with 409."""
    scanner: LibraryScanner = request.app.state.scanner
    started, progress = scanner.start_scan(full=full)
    if not started:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail="A library scan is already in progress",
        )
    return JSONResponse(
        status_code=AppConstants.HTTP_ACCEPTED,
        content=progress.model_dump(),
    )


@router.get("/stream")
async def stream_scan_progress(request: Request) -> EventSourceResponse:
    """Stream scan progress and coalesced `library_changed` events over one SSE stream."""
    scanner: LibraryScanner = request.app.state.scanner
    library_broker: LibraryEventBroker | None = getattr(
        request.app.state, "library_broker", None
    )

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        # Yield initial scan snapshot immediately so clients sync state on connect
        if await request.is_disconnected():
            return
        yield {
            "event": AppConstants.SSE_SCAN_EVENT_NAME,
            "data": scanner.current_progress.model_dump_json(),
        }

        merged_queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue(
            maxsize=AppConstants.JOB_SUBSCRIBER_QUEUE_MAXSIZE
        )

        async def _forward_scan() -> None:
            first_seen = True
            async for payload in scanner.subscribe():
                if first_seen:
                    first_seen = False
                    continue
                await merged_queue.put(
                    {
                        "event": AppConstants.SSE_SCAN_EVENT_NAME,
                        "data": payload.model_dump_json(),
                    }
                )
            await merged_queue.put(None)

        async def _forward_library() -> None:
            if library_broker is None:
                return
            async for event in library_broker.subscribe():
                await merged_queue.put(
                    {
                        "event": AppConstants.SSE_LIBRARY_CHANGED_EVENT_NAME,
                        "data": event.model_dump_json(),
                    }
                )

        tasks = [asyncio.create_task(_forward_scan())]
        if library_broker is not None:
            tasks.append(asyncio.create_task(_forward_library()))

        try:
            while True:
                if await request.is_disconnected():
                    break
                item = await merged_queue.get()
                if item is None:
                    break
                if await request.is_disconnected():
                    break
                yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return EventSourceResponse(
        _event_generator(),
        ping=AppConstants.SSE_PING_INTERVAL_SEC,
        headers={
            AppConstants.SSE_ACCEL_BUFFERING_HEADER: AppConstants.SSE_ACCEL_BUFFERING_VALUE,
        },
    )
