"""Integration tests for Phase 2 Dashboard, Library Files, and Scan APIs."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from app.config import AppConstants, Settings
from app.services.scanner import ProbeError


def _sample_probe_payload(
    *,
    title: str = "",
    has_global_tags: bool = False,
    has_aac51: bool = True,
    has_eac3: bool = False,
) -> dict:
    tracks = [
        {
            "id": 0,
            "type": "video",
            "codec": "MPEG-H/HEVC/H.265",
            "properties": {"pixel_dimensions": "1920x1080"},
        }
    ]
    next_id = 1
    if has_aac51:
        tracks.append(
            {
                "id": next_id,
                "type": "audio",
                "codec": "AAC",
                "properties": {
                    "audio_channels": 6,
                    "language": "eng",
                    "default_track": True,
                    "minimum_timestamp": 25_000_000,
                },
            }
        )
        next_id += 1
    if has_eac3:
        tracks.append(
            {
                "id": next_id,
                "type": "audio",
                "codec": "E-AC-3",
                "properties": {
                    "codec_id": "A_EAC3",
                    "audio_channels": 6,
                    "language": "eng",
                },
            }
        )
        next_id += 1
    tracks.append(
        {
            "id": next_id,
            "type": "subtitles",
            "codec": "SubRip/SRT",
            "properties": {"language": "eng"},
        }
    )
    return {
        "container": {"properties": {"duration": 6_000_000_000_000, "title": title}},
        "global_tags": [{"num_entries": 2}] if has_global_tags else [],
        "track_tags": [{"num_entries": 4, "track_id": 0}],
        "attachments": [{"id": 1, "file_name": "cover.jpg"}],
        "chapters": [{"num_entries": 8}],
        "tracks": tracks,
    }


class TestDashboardAndFilesApi:
    """Verify GET /api/dashboard, GET /api/files, GET /api/files/{id}, and POST /rescan."""

    async def test_dashboard_empty_then_populated_after_scan(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        empty_resp = await api_client.get("/api/dashboard")
        assert empty_resp.status_code == AppConstants.HTTP_OK
        empty_body = empty_resp.json()
        assert empty_body["stats"]["total_files"] == 0
        assert empty_body["stats"]["aac51_only"] == 0
        assert empty_body["stats"]["has_metadata"] == 0
        assert empty_body["stats"]["last_scan_at"] is None
        assert empty_body["recent_activity"] == []

        # Populate two media files on disk
        movies_dir = test_settings.storage_dir / "movies"
        shows_dir = test_settings.storage_dir / "shows"
        movies_dir.mkdir()
        shows_dir.mkdir()
        (movies_dir / "NeedsFix.mkv").write_bytes(b"aaa")
        (shows_dir / "AlreadyDolby.mp4").write_bytes(b"bbbbbb")

        async def _fake_probe(path, _timeout):
            if path.name == "NeedsFix.mkv":
                data = _sample_probe_payload(title="Bad Title", has_aac51=True, has_eac3=False)
            else:
                data = _sample_probe_payload(title="", has_aac51=True, has_eac3=True)
            return data, json.dumps(data)

        with patch("app.services.scanner.run_mkvmerge_probe", new=AsyncMock(side_effect=_fake_probe)):
            scan_resp = await api_client.post("/api/scan")
            assert scan_resp.status_code == AppConstants.HTTP_ACCEPTED
            await api_app.state.scanner.wait_for_scan()

        dash_resp = await api_client.get("/api/dashboard")
        assert dash_resp.status_code == AppConstants.HTTP_OK
        stats = dash_resp.json()["stats"]
        assert stats["total_files"] == 2
        assert stats["aac51_only"] == 1
        assert stats["has_metadata"] == 1
        assert stats["last_scan_at"] is not None
        assert stats["last_scan_status"] == AppConstants.SCAN_STATUS_COMPLETED

        # Query /api/files with filters + cascading facets
        all_resp = await api_client.get("/api/files")
        assert all_resp.status_code == AppConstants.HTTP_OK
        all_body = all_resp.json()
        assert all_body["total"] == 2
        assert all_body["folders"] == ["/movies", "/shows"]
        assert all_body["audio_types"] == ["AAC 5.1", "EAC3 5.1"]
        assert all_body["facets"]["aac51_only"] == 1
        assert all_body["facets"]["has_metadata"] == 1

        # Search cascades audio_types, folders, and workflow facets to matching view only
        narrowed = await api_client.get("/api/files", params={"q": "NeedsFix"})
        narrowed_body = narrowed.json()
        assert narrowed_body["total"] == 1
        assert narrowed_body["folders"] == ["/movies"]
        assert narrowed_body["audio_types"] == ["AAC 5.1"]
        assert narrowed_body["facets"]["aac51_only"] == 1
        assert narrowed_body["facets"]["has_metadata"] == 1

        # Filter by generic audio_type param
        eac3_resp = await api_client.get("/api/files", params={"audio_type": "EAC3 5.1"})
        assert eac3_resp.json()["total"] == 1
        assert eac3_resp.json()["items"][0]["filename"] == "AlreadyDolby.mp4"

        aac_only_resp = await api_client.get("/api/files", params={"aac51_only": "true"})
        assert aac_only_resp.json()["total"] == 1
        target_item = aac_only_resp.json()["items"][0]
        assert target_item["filename"] == "NeedsFix.mkv"
        assert target_item["has_metadata"] is True
        assert target_item["resolution"] == "1080p"
        assert target_item["subtitle_count"] == 1

        # Detail endpoint returns full parsed tracks + sync_delay_ms + audio_index
        detail_resp = await api_client.get(f"/api/files/{target_item['id']}")
        assert detail_resp.status_code == AppConstants.HTTP_OK
        detail = detail_resp.json()
        assert detail["duration_us"] == 6_000_000_000
        assert detail["has_track_tags"] is True
        assert detail["attachments_count"] == 1
        assert detail["chapters_count"] == 8
        audio_tracks = [t for t in detail["tracks"] if t["type"] == "audio"]
        assert len(audio_tracks) == 1
        assert audio_tracks[0]["audio_index"] == 0
        assert audio_tracks[0]["sync_delay_ms"] == 25

        # Missing ID returns 404
        missing_resp = await api_client.get("/api/files/999999")
        assert missing_resp.status_code == AppConstants.HTTP_NOT_FOUND

    async def test_rescan_single_file_endpoint_cases(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        target = test_settings.storage_dir / "Movie.mkv"
        target.write_bytes(b"v1")

        v1 = _sample_probe_payload(title="Old", has_aac51=True, has_eac3=False)
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(return_value=(v1, json.dumps(v1))),
        ):
            await api_app.state.scanner.probe_single_file("Movie.mkv")

        file_row = api_app.state.media_repo.get_by_filepath("Movie.mkv")
        assert file_row is not None
        file_id = file_row["id"]

        # 1. Successful rescan reflects updated metadata
        v2 = _sample_probe_payload(title="", has_aac51=True, has_eac3=True)
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(return_value=(v2, json.dumps(v2))),
        ):
            rescan_resp = await api_client.post(f"/api/files/{file_id}/rescan")
        assert rescan_resp.status_code == AppConstants.HTTP_OK
        assert rescan_resp.json()["has_ac3_eac3"] is True
        assert rescan_resp.json()["has_metadata"] is False

        # 2. ProbeError returns 502 Bad Gateway
        with patch(
            "app.services.scanner.run_mkvmerge_probe",
            new=AsyncMock(side_effect=ProbeError("corrupt")),
        ):
            err_resp = await api_client.post(f"/api/files/{file_id}/rescan")
        assert err_resp.status_code == AppConstants.HTTP_BAD_GATEWAY

        # 3. Deleted file on disk returns 404 and removes DB row
        target.unlink()
        gone_resp = await api_client.post(f"/api/files/{file_id}/rescan")
        assert gone_resp.status_code == AppConstants.HTTP_NOT_FOUND
        assert api_app.state.media_repo.get_by_id(file_id) is None

        # 4. Unknown ID returns 404
        unknown_resp = await api_client.post("/api/files/99999/rescan")
        assert unknown_resp.status_code == AppConstants.HTTP_NOT_FOUND


class TestScanEndpointsAndSse:
    """Verify overlapping scan 409 rejection and SSE headers/payload."""

    async def test_overlapping_scan_returns_409(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        (test_settings.storage_dir / "Slow.mkv").write_bytes(b"data")
        gate = asyncio.Event()

        async def _blocking_probe(_path, _timeout):
            await gate.wait()
            data = _sample_probe_payload()
            return data, json.dumps(data)

        with patch("app.services.scanner.run_mkvmerge_probe", new=AsyncMock(side_effect=_blocking_probe)):
            first = await api_client.post("/api/scan")
            assert first.status_code == AppConstants.HTTP_ACCEPTED

            second = await api_client.post("/api/scan")
            assert second.status_code == AppConstants.HTTP_CONFLICT

            gate.set()
            await api_app.state.scanner.wait_for_scan()

    async def test_scan_stream_headers_and_initial_snapshot(
        self, api_app
    ) -> None:
        from app.api.scan import stream_scan_progress

        class _FakeRequest:
            def __init__(self, app) -> None:
                self.app = app
                self._checks = 0

            async def is_disconnected(self) -> bool:
                self._checks += 1
                return self._checks > 1

        response = await stream_scan_progress(_FakeRequest(api_app))
        assert (
            response.headers.get(AppConstants.SSE_ACCEL_BUFFERING_HEADER)
            == AppConstants.SSE_ACCEL_BUFFERING_VALUE
        )
        assert response.ping_interval == AppConstants.SSE_PING_INTERVAL_SEC

        # Consume the first emitted SSE event from the async generator
        first_event = await anext(response.body_iterator)
        assert first_event["event"] == AppConstants.SSE_SCAN_EVENT_NAME
        parsed = json.loads(first_event["data"])
        assert parsed["status"] == AppConstants.SCAN_STATUS_IDLE
        assert parsed["scanned"] == 0
