"""Coalesced in-memory pub/sub broker for live `library_changed` SSE events."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from app.config import AppConstants
from app.models.schemas import LibraryChangedEvent


class LibraryEventBroker:
    """Coalesces rapid filesystem watcher bursts and broadcasts SSE notifications."""

    def __init__(
        self,
        *,
        coalesce_sec: float = AppConstants.WATCH_EVENT_COALESCE_SEC,
        subscriber_queue_size: int = AppConstants.LIBRARY_SUBSCRIBER_QUEUE_MAXSIZE,
    ) -> None:
        self._coalesce_sec = max(0.0, float(coalesce_sec))
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[LibraryChangedEvent | None]] = set()

        self._pending_actions: list[str] = []
        self._pending_paths: list[str] = []
        self._pending_ids: list[int] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the main asyncio event loop for thread-safe cross-thread emits."""
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        """Return number of currently connected library-event subscribers."""
        return len(self._subscribers)

    def record_change(
        self,
        *,
        action: str,
        filepath: str,
        file_id: int | None = None,
    ) -> None:
        """Queue one library mutation and schedule a coalesced SSE broadcast.

        Thread-safe: if invoked from a worker thread (`asyncio.to_thread`),
        marshals back onto the bound event loop via `call_soon_threadsafe`.
        """
        if self._closed:
            return

        try:
            running_loop = asyncio.get_running_loop()
            self._loop = running_loop
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(
                    lambda: self.record_change(
                        action=action, filepath=filepath, file_id=file_id
                    )
                )
            return

        self._pending_actions.append(action)
        if filepath and filepath not in self._pending_paths:
            self._pending_paths.append(filepath)
        if file_id is not None and file_id not in self._pending_ids:
            self._pending_ids.append(int(file_id))

        if self._coalesce_sec <= 0:
            self.flush_now()
            return

        if self._flush_task is None or self._flush_task.done():
            self._flush_task = running_loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._coalesce_sec)
            self.flush_now()
        except asyncio.CancelledError:
            pass

    def flush_now(self) -> LibraryChangedEvent | None:
        """Immediately emit any buffered library changes as a single SSE event."""
        if not self._pending_actions:
            return None

        actions = list(self._pending_actions)
        paths = list(self._pending_paths)
        ids = list(self._pending_ids)
        self._pending_actions.clear()
        self._pending_paths.clear()
        self._pending_ids.clear()

        unique_actions = set(actions)
        if len(unique_actions) == 1 and actions[0] in ("added", "updated", "deleted"):
            resolved_action = actions[0]
        else:
            resolved_action = "batch"

        event = LibraryChangedEvent(
            action=resolved_action,
            count=max(len(paths), len(actions)),
            filepaths=paths[: AppConstants.WATCH_MAX_EVENT_PATHS],
            file_ids=ids[: AppConstants.WATCH_MAX_EVENT_PATHS],
        )
        self._broadcast(event)
        return event

    def _broadcast(self, event: LibraryChangedEvent) -> None:
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

    def close(self) -> None:
        """Cancel pending coalesce timers and terminate all active subscribers."""
        self._closed = True
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
        self._pending_actions.clear()
        self._pending_paths.clear()
        self._pending_ids.clear()

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
        """Flush/drop queued events, disconnect active subscribers, and stay open for new clients."""
        self.close()
        self._closed = False

    async def subscribe(self) -> AsyncIterator[LibraryChangedEvent]:
        """Yield coalesced `LibraryChangedEvent` objects until closed."""
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[LibraryChangedEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        self._subscribers.add(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._subscribers.discard(queue)
