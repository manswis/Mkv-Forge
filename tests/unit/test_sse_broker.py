"""Unit tests for app.services.sse_broker."""

from __future__ import annotations

import asyncio
import re

from app.config import AppConstants
from app.services.sse_broker import JobSSEBroker


class TestJobSSEBroker:
    """Verify pub/sub broadcasting, rolling log ring buffer, and reconnect replay."""

    def test_initial_snapshot_is_idle_with_empty_logs(self) -> None:
        broker = JobSSEBroker()
        snap = broker.get_snapshot()

        assert snap.status == AppConstants.JOB_STATUS_IDLE
        assert snap.job_id is None
        assert snap.recent_logs == []

    def test_rolling_log_buffer_caps_at_max_lines_and_resets_on_new_job(self) -> None:
        broker = JobSSEBroker(max_log_lines=5)
        broker.start_job(job_id="job-1", filepath="movies/A.mkv")

        for i in range(12):
            broker.publish_log(job_id="job-1", filepath="movies/A.mkv", log_line=f"line-{i}")

        assert len(broker.recent_logs) == 5
        for idx, line in enumerate(broker.recent_logs, start=7):
            assert re.match(rf"^\[\d{{2}}:\d{{2}}:\d{{2}}\] line-{idx}$", line)
        assert broker.get_snapshot().recent_logs == broker.recent_logs

        # Starting a new job clears previous job's log buffer
        broker.start_job(job_id="job-2", filepath="movies/B.mkv")
        assert broker.recent_logs == []

    async def test_subscriber_receives_reconnect_snapshot_then_live_events(self) -> None:
        broker = JobSSEBroker(max_log_lines=10)
        broker.start_job(
            job_id="job-1",
            filepath="movies/Dune.mkv",
            stage="transcode",
            stage_label="Stage 1 of 2: Encoding EAC3",
        )
        broker.publish_progress(
            job_id="job-1",
            filepath="movies/Dune.mkv",
            stage="transcode",
            stage_label="Stage 1 of 2: Encoding EAC3",
            percent=40,
            speed="20.0x",
            eta="1m 10s",
        )
        broker.publish_log(
            job_id="job-1", filepath="movies/Dune.mkv", log_line="frame=1000"
        )

        # Simulate mobile client reconnecting mid-job
        stream = broker.subscribe()
        snapshot = await anext(stream)
        assert snapshot.job_id == "job-1"
        assert snapshot.percent == 40
        assert snapshot.speed == "20.0x"
        assert snapshot.eta == "1m 10s"
        assert snapshot.recent_logs is not None
        assert len(snapshot.recent_logs) == 1
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] frame=1000$", snapshot.recent_logs[0])

        # Stream a live progress update + terminal completion
        broker.publish_progress(
            job_id="job-1",
            filepath="movies/Dune.mkv",
            stage="remux",
            stage_label="Stage 2 of 2: Remuxing MKV",
            percent=75,
        )
        live_event = await anext(stream)
        assert live_event.stage == "remux"
        assert live_event.percent == 75
        assert live_event.recent_logs is None

        broker.publish_terminal(
            job_id="job-1",
            filepath="movies/Dune.mkv",
            status=AppConstants.JOB_STATUS_COMPLETED,
            stage="remux",
            stage_label="Completed",
            percent=100,
            warnings=["Minor warning"],
        )
        done_event = await anext(stream)
        assert done_event.status == AppConstants.JOB_STATUS_COMPLETED
        assert done_event.percent == 100
        assert done_event.warnings == ["Minor warning"]

        # Close terminates the async generator cleanly
        broker.close()
        remaining = [item async for item in stream]
        assert remaining == []
