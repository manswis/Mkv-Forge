"""End-to-end integration tests for Phase 16 Speaker & Audio System Test Generator using real ffmpeg + mkvmerge."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants


def _probe_with_ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _probe_with_mkvmerge(path: Path) -> dict | None:
    if not shutil.which("mkvmerge"):
        return None
    proc = subprocess.run(
        ["mkvmerge", "-J", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestPhase16TestMediaGeneratorAPI:
    """Real FFmpeg + ffprobe (+ mkvmerge when installed) verification across codecs, layouts, collision, and auth."""

    @pytest.mark.parametrize(
        ("codec", "layout", "expected_channels", "signal_type"),
        [
            ("eac3", "5.1", 6, "tones"),
            ("ac3", "5.1", 6, "pink_noise"),
            ("aac", "2.1", 3, "tones"),
            ("opus", "5.1", 6, "tones"),
            ("flac", "7.1", 8, "tones"),
            ("pcm", "5.1", 6, "tones"),
        ],
    )
    async def test_generate_video_mkv_and_verify_real_channels_and_forced_subtitles(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        codec: str,
        layout: str,
        expected_channels: int,
        signal_type: str,
    ) -> None:
        resp = await api_client.post(
            "/api/tools/generate-test-media",
            json={
                "output_mode": "video",
                "codec": codec,
                "layout": layout,
                "signal_type": signal_type,
                "destination_dir": "/",
            },
        )
        assert resp.status_code == AppConstants.HTTP_CREATED, resp.text
        data = resp.json()
        assert data["codec"] == codec
        assert data["layout"] == layout
        assert data["channels"] == expected_channels
        assert data["container"] == "mkv"
        assert data["size_bytes"] > 0

        storage_dir: Path = api_app.state.settings.storage_dir
        out_path = storage_dir / data["filepath"]
        assert out_path.is_file()

        # Verify zero leftover .mkvforge. temp files
        stray = [p for p in storage_dir.rglob("*") if ".mkvforge." in p.name.lower()]
        assert stray == []

        # Verify actual muxed container tracks with real ffprobe
        probed = _probe_with_ffprobe(out_path)
        streams = probed["streams"]
        video_streams = [s for s in streams if s["codec_type"] == "video"]
        audio_streams = [s for s in streams if s["codec_type"] == "audio"]
        sub_streams = [s for s in streams if s["codec_type"] == "subtitle"]

        assert len(video_streams) == 1
        assert len(audio_streams) == 1
        assert int(audio_streams[0]["channels"]) == expected_channels
        assert len(sub_streams) == 1
        assert sub_streams[0]["disposition"].get("default") == 1
        assert sub_streams[0]["disposition"].get("forced") == 1

        # Also cross-check with mkvmerge -J whenever installed (e.g., Docker / CI)
        mkv_probe = _probe_with_mkvmerge(out_path)
        if mkv_probe is not None:
            mkv_audio = [t for t in mkv_probe["tracks"] if t["type"] == "audio"]
            mkv_subs = [t for t in mkv_probe["tracks"] if t["type"] == "subtitles"]
            assert mkv_audio[0]["properties"]["audio_channels"] == expected_channels
            assert mkv_subs[0]["properties"].get("default_track") is True
            assert mkv_subs[0]["properties"].get("forced_track") is True
            assert data["file_id"] is not None

    async def test_generate_audio_only_pcm_wav_and_collision_returns_409(
        self,
        api_client: httpx.AsyncClient,
        api_app,
    ) -> None:
        storage_dir: Path = api_app.state.settings.storage_dir
        sub_dir = storage_dir / "AudioTests"
        sub_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "output_mode": "audio",
            "codec": "pcm",
            "layout": "2.0",
            "container": "wav",
            "signal_type": "tones",
            "tone_duration_sec": 1.0,
            "gap_duration_sec": 0.5,
            "destination_dir": "/AudioTests",
            "filename": "Stereo_LPCM_Check.wav",
        }
        r1 = await api_client.post("/api/tools/generate-test-media", json=payload)
        assert r1.status_code == AppConstants.HTTP_CREATED, r1.text
        body = r1.json()
        assert body["filepath"] == "AudioTests/Stereo_LPCM_Check.wav"
        assert body["folder"] == "/AudioTests"
        assert body["channels"] == 2
        assert body["duration_sec"] == 4.5
        assert body["size_bytes"] > 10_000
        assert (sub_dir / "Stereo_LPCM_Check.wav").is_file()
        probed_wav = _probe_with_ffprobe(sub_dir / "Stereo_LPCM_Check.wav")
        assert abs(float(probed_wav["format"]["duration"]) - 4.5) < 0.25

        # Regenerating with identical filename fails safely with 409 Conflict (never overwrites)
        r2 = await api_client.post("/api/tools/generate-test-media", json=payload)
        assert r2.status_code == AppConstants.HTTP_CONFLICT

    async def test_generate_video_mkv_with_subtitles_disabled_omits_subtitle_stream(
        self,
        api_client: httpx.AsyncClient,
        api_app,
    ) -> None:
        resp = await api_client.post(
            "/api/tools/generate-test-media",
            json={
                "output_mode": "video",
                "codec": "eac3",
                "layout": "2.0",
                "include_subtitles": False,
                "tone_duration_sec": 1.0,
                "gap_duration_sec": 0.0,
                "destination_dir": "/",
                "filename": "NoSubs_EAC3_2.0.mkv",
            },
        )
        assert resp.status_code == AppConstants.HTTP_CREATED, resp.text
        data = resp.json()
        storage_dir: Path = api_app.state.settings.storage_dir
        out_path = storage_dir / data["filepath"]
        assert out_path.is_file()

        probed = _probe_with_ffprobe(out_path)
        streams = probed["streams"]
        video_streams = [s for s in streams if s["codec_type"] == "video"]
        audio_streams = [s for s in streams if s["codec_type"] == "audio"]
        sub_streams = [s for s in streams if s["codec_type"] == "subtitle"]
        assert len(video_streams) == 1
        assert len(audio_streams) == 1
        assert int(audio_streams[0]["channels"]) == 2
        assert len(sub_streams) == 0

    async def test_invalid_combinations_return_400_and_local_auth_enforces_401(
        self,
        api_client: httpx.AsyncClient,
    ) -> None:
        bad_dolby = await api_client.post(
            "/api/tools/generate-test-media",
            json={"output_mode": "video", "codec": "eac3", "layout": "7.1"},
        )
        assert bad_dolby.status_code == AppConstants.HTTP_BAD_REQUEST

        bad_opus = await api_client.post(
            "/api/tools/generate-test-media",
            json={"output_mode": "video", "codec": "opus", "layout": "2.1"},
        )
        assert bad_opus.status_code == AppConstants.HTTP_BAD_REQUEST

        # Enable local auth and verify unauthenticated access is rejected with 401
        setup = await api_client.post(
            "/api/auth/setup",
            json={"mode": "local", "username": "admin", "password": "secret123"},
        )
        assert setup.status_code == AppConstants.HTTP_OK
        await api_client.post("/api/auth/logout")

        unauth = await api_client.post(
            "/api/tools/generate-test-media",
            json={"output_mode": "video", "codec": "eac3", "layout": "2.0"},
        )
        assert unauth.status_code == AppConstants.HTTP_UNAUTHORIZED
