"""In-memory SSE progress broadcaster and rolling log ring buffer for jobs."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

from app.config import AppConstants
from app.logging_config import format_cli_log_line
from app.models.schemas import JobProgressEvent


class JobSSEBroker:
    """Pub/sub broker for real-time job progress with reconnect catch-up."""

    def __init__(
        self,
        *,
        max_log_lines: int = AppConstants.JOB_LOG_BUFFER_MAX_LINES,
        subscriber_queue_size: int = AppConstants.JOB_SUBSCRIBER_QUEUE_MAXSIZE,
    ) -> None:
        self._log_buffer: deque[str] = deque(maxlen=max_log_lines)
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[JobProgressEvent | None]] = set()
        self._latest: JobProgressEvent = JobProgressEvent(
            status=AppConstants.JOB_STATUS_IDLE
        )

    @property
    def recent_logs(self) -> list[str]:
        """Return a copy of the rolling log ring buffer for the active/last job."""
        return list(self._log_buffer)

    @property
    def subscriber_count(self) -> int:
        """Return number of currently connected SSE subscribers."""
        return len(self._subscribers)

    def get_snapshot(self) -> JobProgressEvent:
        """Return the latest state snapshot including the rolling log ring buffer."""
        return self._latest.model_copy(update={"recent_logs": list(self._log_buffer)})

    def _broadcast(self, event: JobProgressEvent) -> None:
        """Fan out an event to all connected SSE subscriber queues without blocking."""
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def start_job(
        self,
        *,
        job_id: str,
        filepath: str,
        stage: str | None = None,
        stage_label: str | None = None,
    ) -> None:
        """Reset the log ring buffer for a newly started job and broadcast."""
        self._log_buffer.clear()
        event = JobProgressEvent(
            job_id=job_id,
            filepath=filepath,
            stage=stage,
            stage_label=stage_label,
            percent=0,
            status=AppConstants.JOB_STATUS_RUNNING,
        )
        self._latest = event
        self._broadcast(event)

    def publish_progress(
        self,
        *,
        job_id: str,
        filepath: str,
        stage: str | None,
        stage_label: str | None,
        percent: int,
        speed: str | None = None,
        eta: str | None = None,
        status: str = AppConstants.JOB_STATUS_RUNNING,
    ) -> None:
        """Update latest job progress snapshot and broadcast to subscribers."""
        bounded = max(0, min(100, int(percent)))
        event = JobProgressEvent(
            job_id=job_id,
            filepath=filepath,
            stage=stage,
            stage_label=stage_label,
            percent=bounded,
            speed=speed,
            eta=eta,
            status=status,
        )
        self._latest = event
        self._broadcast(event)

    def publish_log(
        self,
        *,
        job_id: str,
        filepath: str,
        log_line: str,
    ) -> None:
        """Append a local-time prefixed CLI log line to the ring buffer and stream it live."""
        formatted_line = format_cli_log_line(log_line)
        self._log_buffer.append(formatted_line)
        event = self._latest.model_copy(
            update={
                "job_id": job_id,
                "filepath": filepath,
                "log_line": formatted_line,
                "recent_logs": None,
            }
        )
        self._broadcast(event)

    def publish_terminal(
        self,
        *,
        job_id: str,
        filepath: str,
        status: str,
        stage: str | None = None,
        stage_label: str | None = None,
        percent: int = 0,
        warnings: list[str] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Publish a terminal job status (`completed`, `failed`, or `cancelled`)."""
        event = JobProgressEvent(
            job_id=job_id,
            filepath=filepath,
            stage=stage,
            stage_label=stage_label,
            percent=max(0, min(100, int(percent))),
            status=status,
            warnings=list(warnings) if warnings else [],
            error_message=error_message,
        )
        self._latest = event
        self._broadcast(event)

    def close(self) -> None:
        """Signal all active SSE subscribers to terminate gracefully."""
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def reset(self) -> None:
        """Clear in-memory job snapshot and log ring buffer, disconnecting stale subscribers."""
        self._log_buffer.clear()
        self._latest = JobProgressEvent(status=AppConstants.JOB_STATUS_IDLE)
        self.close()

    async def subscribe(self) -> AsyncIterator[JobProgressEvent]:
        """Yield current snapshot (with rolling logs), then live progress events."""
        queue: asyncio.Queue[JobProgressEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        self._subscribers.add(queue)
        try:
            yield self.get_snapshot()
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._subscribers.discard(queue)
