"""Unified multiplexed SSE endpoint carrying scan, library, and job events over one socket."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.config import AppConstants
from app.services.library_events import LibraryEventBroker
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stream")
async def stream_unified_events(request: Request) -> EventSourceResponse:
    """Multiplex `scan_progress`, `library_changed`, and `job_progress` over ONE SSE stream."""
    scanner: LibraryScanner = request.app.state.scanner
    library_broker: LibraryEventBroker | None = getattr(
        request.app.state, "library_broker", None
    )
    job_broker: JobSSEBroker = request.app.state.job_broker

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        if await request.is_disconnected():
            return

        # Emit initial scan + job snapshots immediately so clients hydrate in one round-trip
        yield {
            "event": AppConstants.SSE_SCAN_EVENT_NAME,
            "data": scanner.current_progress.model_dump_json(),
        }
        yield {
            "event": AppConstants.SSE_JOB_EVENT_NAME,
            "data": job_broker.get_snapshot().model_dump_json(),
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

        async def _forward_jobs() -> None:
            first_seen = True
            async for event in job_broker.subscribe():
                if first_seen:
                    first_seen = False
                    continue
                await merged_queue.put(
                    {
                        "event": AppConstants.SSE_JOB_EVENT_NAME,
                        "data": event.model_dump_json(),
                    }
                )
            await merged_queue.put(None)

        tasks = [
            asyncio.create_task(_forward_scan()),
            asyncio.create_task(_forward_jobs()),
        ]
        if library_broker is not None:
            tasks.append(asyncio.create_task(_forward_library()))

        logger.debug(
            "Unified SSE connected (job_subscribers=%d)",
            job_broker.subscriber_count + 1,
        )
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
            logger.debug(
                "Unified SSE disconnected (job_subscribers=%d)",
                job_broker.subscriber_count,
            )

    return EventSourceResponse(
        _event_generator(),
        ping=AppConstants.SSE_PING_INTERVAL_SEC,
        headers={
            AppConstants.SSE_ACCEL_BUFFERING_HEADER: AppConstants.SSE_ACCEL_BUFFERING_VALUE,
            AppConstants.CACHE_CONTROL_HEADER: AppConstants.CACHE_CONTROL_NO_CACHE,
        },
    )
