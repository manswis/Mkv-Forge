"""Exhaustive unit, integration, and Node VM tests for Processing Priority, Stage 1 Scratch, and Smart Fill Format."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import AppConstants, Settings, load_settings
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, JobRepository, MediaFileRepository
from app.main import create_app
from app.models.schemas import AudioConversionConfig, JobConfig, UserSettings, UserSettingsUpdate
from app.services.batch_compiler import (
    ParsedTrackInfo,
    build_converted_audio_title,
    format_smart_track_badge,
)
from app.services.ffmpeg_runner import build_ffmpeg_command
from app.services.job_worker import JobWorker, sweep_orphaned_temp_files
from app.services.library_events import LibraryEventBroker
from app.services.maintenance_service import MaintenanceService
from app.services.mkv_runner import execute_two_stage_pipeline
from app.services.preflight import (
    InsufficientDiskSpaceError,
    check_disk_space,
    run_preflight_checks,
)
from app.services.processing_policy import (
    ProcessingRuntimePolicy,
    build_nice_preexec_fn,
    list_managed_scratch_dirs,
    resolve_ffmpeg_threads,
    resolve_nice_increment,
    resolve_processing_policy,
    resolve_stage1_scratch_dir,
    wrap_command_with_io_priority,
)
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker
from app.services.user_settings import UserSettingsService
from app.utils.paths import build_temp_audio_path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_JS_DIR = REPO_ROOT / "app" / "static" / "js"


def _make_settings(tmp_path: Path, *, scratch_dir: Path | None = None) -> Settings:
    storage = tmp_path / "storage"
    data = tmp_path / "data"
    storage.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return Settings(
        storage_dir=storage,
        data_dir=data,
        static_dir=REPO_ROOT / "app" / "static",
        health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
        scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
        log_level=AppConstants.DEFAULT_LOG_LEVEL,
        watch_mode=AppConstants.WATCH_MODE_OFF,
        scratch_dir=scratch_dir,
    )


class TestProcessingPolicyAndDefaults:
    """Verify CPU/I/O priority modes, scratch resolution, and exact backward-compatible defaults."""

    def test_defaults_preserve_original_behavior(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        user_settings = UserSettings()
        assert user_settings.processing_priority_mode == AppConstants.PROCESSING_PRIORITY_AUTO
        assert user_settings.stage1_scratch_mode == AppConstants.STAGE1_SCRATCH_SOURCE
        assert user_settings.smart_fill_track_format == AppConstants.SMART_FILL_FORMAT_DETAILED

        policy = resolve_processing_policy(
            user_settings, settings=settings, cpu_count=8, ionice_available=True
        )
        assert policy.priority_mode == "auto"
        assert policy.ffmpeg_threads is None
        assert policy.nice_increment == 0
        assert policy.use_ionice_idle is False
        assert policy.scratch_mode == "source"
        assert policy.scratch_dir is None

    @pytest.mark.parametrize(
        ("mode", "cpus", "expected_threads", "expected_nice", "ionice_avail", "expected_ionice"),
        [
            ("auto", 8, None, 0, True, False),
            ("half_cores", 8, 4, 10, True, False),
            ("half_cores", 1, 1, 10, True, False),
            ("low_priority", 8, 2, 15, True, True),
            ("low_priority", 8, 2, 15, False, False),
            ("low_priority", 2, 1, 15, True, True),
            ("low_priority", 1, 1, 15, True, True),
        ],
    )
    def test_priority_modes_resolve_threads_nice_and_ionice(
        self,
        tmp_path: Path,
        mode: str,
        cpus: int,
        expected_threads: int | None,
        expected_nice: int,
        ionice_avail: bool,
        expected_ionice: bool,
    ) -> None:
        settings = _make_settings(tmp_path)
        assert resolve_ffmpeg_threads(mode, cpu_count=cpus) == expected_threads
        assert resolve_nice_increment(mode) == expected_nice
        policy = resolve_processing_policy(
            UserSettings(processing_priority_mode=mode),  # type: ignore[arg-type]
            settings=settings,
            cpu_count=cpus,
            ionice_available=ionice_avail,
        )
        assert policy.ffmpeg_threads == expected_threads
        assert policy.nice_increment == expected_nice
        assert policy.use_ionice_idle is expected_ionice

    def test_command_wrapping_and_nice_preexec(self, tmp_path: Path) -> None:
        src = tmp_path / "movie.mkv"
        tmp_out = tmp_path / "movie.mkv.mkvforge.eac3"
        cmd_auto = build_ffmpeg_command(
            src, tmp_out, audio_index=0, target_codec="eac3", bitrate="640k", channels=6
        )
        assert "-threads" not in cmd_auto
        assert "-filter_threads" not in cmd_auto

        cmd_capped = build_ffmpeg_command(
            src,
            tmp_out,
            audio_index=0,
            target_codec="eac3",
            bitrate="640k",
            channels=6,
            threads=3,
        )
        assert cmd_capped[:6] == ["ffmpeg", "-y", "-threads", "3", "-filter_threads", "3"]
        input_idx = cmd_capped.index("-i")
        encoder_idx = cmd_capped.index("-c:a")
        assert cmd_capped[encoder_idx : encoder_idx + 4] == ["-c:a", "eac3", "-threads", "3"]
        assert encoder_idx > input_idx

        assert wrap_command_with_io_priority(cmd_auto, use_ionice_idle=False) == cmd_auto
        wrapped = wrap_command_with_io_priority(cmd_auto, use_ionice_idle=True)
        assert wrapped[:4] == ["ionice", "-t", "-c", "3"]
        assert wrapped[4:] == cmd_auto

        assert build_nice_preexec_fn(0) is None
        fn = build_nice_preexec_fn(10)
        assert callable(fn)
        with patch("os.nice", side_effect=PermissionError("no cap")):
            fn()  # Must swallow OSError gracefully inside restricted containers

    def test_stage1_scratch_modes_and_custom_fallback(self, tmp_path: Path) -> None:
        custom = tmp_path / "fast_ssd"
        settings_no_custom = _make_settings(tmp_path, scratch_dir=None)
        settings_with_custom = _make_settings(tmp_path, scratch_dir=custom)

        assert resolve_stage1_scratch_dir("source", settings=settings_no_custom) is None
        assert resolve_stage1_scratch_dir("tmp", settings=settings_no_custom) is not None
        assert resolve_stage1_scratch_dir("data", settings=settings_no_custom) == settings_no_custom.data_scratch_dir
        # Fallback to source (None) if custom is requested without MKVFORGE_SCRATCH_DIR
        assert resolve_stage1_scratch_dir("custom", settings=settings_no_custom) is None
        assert resolve_stage1_scratch_dir("custom", settings=settings_with_custom) == custom

        # Env var parsing
        env_settings = load_settings(env={AppConstants.ENV_SCRATCH_DIR: str(custom)})
        assert env_settings.scratch_dir == custom
        assert custom in list_managed_scratch_dirs(settings_with_custom)

        # Unique non-colliding temp audio paths across same filename in different folders
        p1 = build_temp_audio_path(tmp_path / "s1" / "Ep01.mkv", "eac3", scratch_dir=custom)
        p2 = build_temp_audio_path(tmp_path / "s2" / "Ep01.mkv", "eac3", scratch_dir=custom)
        assert p1.parent == custom
        assert p2.parent == custom
        assert p1 != p2
        assert AppConstants.TEMP_MARKER in p1.name


class TestScratchPreflightPipelineAndCleanup:
    """Verify Stage 1 scratch preflight space check, pipeline execution, and cleanup."""

    def test_preflight_checks_scratch_mount_free_space(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        scratch = tmp_path / "scratch"
        storage.mkdir()
        scratch.mkdir()
        src = storage / "Movie.mkv"
        src.write_bytes(b"x" * 1024)

        def _fake_disk_usage(path: Path):
            if str(path).startswith(str(scratch)):
                return SimpleNamespace(total=10_000_000, used=9_999_000, free=1000)
            return SimpleNamespace(total=100_000_000_000, used=0, free=100_000_000_000)

        with patch("app.services.preflight.shutil.disk_usage", side_effect=_fake_disk_usage):
            with pytest.raises(InsufficientDiskSpaceError, match="scratch disk space"):
                check_disk_space(
                    src,
                    audio_conversion_enabled=True,
                    duration_us=60_000_000,
                    bitrate="640k",
                    scratch_dir=scratch,
                )

        # Still succeeds when audio conversion is disabled even if scratch is full
        with patch("app.services.preflight.shutil.disk_usage", side_effect=_fake_disk_usage):
            res = check_disk_space(
                src,
                audio_conversion_enabled=False,
                duration_us=60_000_000,
                bitrate="640k",
                scratch_dir=scratch,
            )
            assert res.estimated_audio_bytes == 0

    async def test_pipeline_writes_stage1_audio_in_scratch_and_stage2_tmp_on_storage(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        scratch = tmp_path / "ssd_scratch"
        storage.mkdir()
        src = storage / "Film.mkv"
        src.write_bytes(b"original-mkv")

        captured: dict[str, object] = {}

        async def _fake_ffmpeg(source_path, temp_audio_path, **kwargs):
            captured["temp_audio_path"] = temp_audio_path
            captured["threads"] = kwargs.get("threads")
            captured["nice_increment"] = kwargs.get("nice_increment")
            captured["use_ionice_idle"] = kwargs.get("use_ionice_idle")
            temp_audio_path.write_bytes(b"encoded-eac3")
            return SimpleNamespace(temp_audio_path=temp_audio_path)

        async def _fake_mkvmerge(source_path, output_tmp_path, config, **kwargs):
            captured["output_tmp_path"] = output_tmp_path
            captured["mkv_nice"] = kwargs.get("nice_increment")
            captured["mkv_ionice"] = kwargs.get("use_ionice_idle")
            output_tmp_path.write_bytes(b"remuxed-mkv")
            return SimpleNamespace(
                output_tmp_path=output_tmp_path, exit_code=0, warnings=[], log_lines=[]
            )

        policy = ProcessingRuntimePolicy(
            priority_mode="low_priority",
            ffmpeg_threads=2,
            nice_increment=15,
            use_ionice_idle=True,
            scratch_mode="custom",
            scratch_dir=scratch,
        )
        cfg = JobConfig(
            output_mode=AppConstants.OUTPUT_MODE_REPLACE,
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                source_channels=6,
                source_codec="AAC",
                target_codec="eac3",
                bitrate="640k",
            ),
        )

        with (
            patch("app.services.mkv_runner.run_ffmpeg_transcode", side_effect=_fake_ffmpeg),
            patch("app.services.mkv_runner.run_mkvmerge_remux", side_effect=_fake_mkvmerge),
        ):
            result = await execute_two_stage_pipeline(
                src,
                cfg,
                storage_root=storage,
                duration_us=60_000_000,
                processing_policy=policy,
            )

        assert result.output_path == src
        assert src.read_bytes() == b"remuxed-mkv"
        # Stage 1 temp audio went to scratch and was cleaned up in finally
        temp_audio = captured["temp_audio_path"]
        assert isinstance(temp_audio, Path)
        assert temp_audio.parent == scratch
        assert not temp_audio.exists()
        # Stage 2 temp MKV stayed on /storage for atomic os.replace()
        output_tmp = captured["output_tmp_path"]
        assert isinstance(output_tmp, Path)
        assert output_tmp.parent == storage
        # Priority policy forwarded to both FFmpeg and mkvmerge
        assert captured["threads"] == 2
        assert captured["nice_increment"] == 15
        assert captured["use_ionice_idle"] is True
        assert captured["mkv_nice"] == 15
        assert captured["mkv_ionice"] is True

    async def test_crash_recovery_and_maintenance_sweep_scratch_dir(self, tmp_path: Path) -> None:
        custom_scratch = tmp_path / "custom_scratch"
        custom_scratch.mkdir()
        settings = _make_settings(tmp_path, scratch_dir=custom_scratch)
        db = DatabaseManager(settings.db_path)
        db.initialize()

        # Leftover temp files in data_scratch_dir and custom_scratch
        settings.data_scratch_dir.mkdir(parents=True, exist_ok=True)
        stray_data = settings.data_scratch_dir / "Movie.mkv.deadbeef.mkvforge.eac3"
        stray_custom = custom_scratch / "Show.mkv.12345678.mkvforge.ac3"
        stray_data.write_bytes(b"12345")
        stray_custom.write_bytes(b"67890")

        state_repo = AppStateRepository(db)
        media_repo = MediaFileRepository(db)
        job_repo = JobRepository(db)
        scanner = LibraryScanner(settings=settings, media_repo=media_repo, state_repo=state_repo)
        broker = LibraryEventBroker()
        user_settings = UserSettingsService(state_repo=state_repo)
        maint = MaintenanceService(
            settings=settings,
            db=db,
            state_repo=state_repo,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            library_broker=broker,
            user_settings_service=user_settings,
        )
        status = maint.get_status()
        assert status.stray_temp_files_count == 2
        res = maint.clean_temp_and_uploads()
        assert res.removed_count == 2
        assert not stray_data.exists()
        assert not stray_custom.exists()

        # Crash recovery also sweeps scratch dirs
        stray_custom.write_bytes(b"orphan-after-crash")
        worker = JobWorker(
            settings=settings,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            broker=JobSSEBroker(),
            user_settings_service=user_settings,
        )
        _stale, removed = await worker.recover_from_crash()
        assert removed >= 1
        assert not stray_custom.exists()


class TestSmartFillTrackFormatBackendAndFrontend:
    """Verify all 3 Smart Fill presets produce identical output in Python and JS."""

    def test_backend_smart_fill_presets_and_fallback(self) -> None:
        audio = ParsedTrackInfo(
            id=1,
            type="audio",
            codec="EAC3",
            language="eng",
            track_name="",
            default_track=True,
            forced_track=False,
            audio_channels=6,
            audio_index=0,
        )
        audio_und = ParsedTrackInfo(
            id=2,
            type="audio",
            codec="AAC",
            language="und",
            track_name="",
            default_track=False,
            forced_track=False,
            audio_channels=2,
            audio_index=1,
        )
        video = ParsedTrackInfo(
            id=0,
            type="video",
            codec="HEVC",
            language="und",
            track_name="",
            default_track=True,
            forced_track=False,
            pixel_dimensions="1920x1080",
        )
        sub = ParsedTrackInfo(
            id=3,
            type="subtitles",
            codec="SRT",
            language="eng",
            track_name="",
            default_track=False,
            forced_track=False,
        )

        # Default ('detailed'): exact legacy behavior
        assert format_smart_track_badge(audio) == "Audio • EAC3 • 5.1 • eng"
        assert format_smart_track_badge(video) == "Video • HEVC • 1920x1080"
        assert format_smart_track_badge(sub) == "Sub • SRT • eng"
        assert (
            build_converted_audio_title(
                target_codec="eac3", target_channels=6, bitrate="640k", language="eng"
            )
            == "EAC3 5.1 • 640k • eng • muxed"
        )

        # Compact
        assert format_smart_track_badge(audio, track_format="compact") == "EAC3 5.1"
        assert format_smart_track_badge(video, track_format="compact") == "HEVC 1920x1080"
        assert format_smart_track_badge(sub, track_format="compact") == "English SRT"
        assert (
            build_converted_audio_title(
                target_codec="eac3",
                target_channels=6,
                bitrate="640k",
                language="eng",
                track_format="compact",
            )
            == "EAC3 5.1"
        )

        # Language · Codec
        assert format_smart_track_badge(audio, track_format="lang_codec") == "English · EAC3 5.1"
        assert format_smart_track_badge(audio_und, track_format="lang_codec") == "AAC 2.0"
        assert format_smart_track_badge(video, track_format="lang_codec") == "HEVC · 1920x1080"
        assert format_smart_track_badge(sub, track_format="lang_codec") == "English · SRT"
        assert (
            build_converted_audio_title(
                target_codec="eac3",
                target_channels=6,
                bitrate="640k",
                language="eng",
                track_format="lang_codec",
            )
            == "English · EAC3 5.1"
        )
        assert (
            build_converted_audio_title(
                target_codec="eac3",
                target_channels=6,
                bitrate="640k",
                language="und",
                track_format="lang_codec",
            )
            == "EAC3 5.1"
        )

        # Unknown format gracefully falls back to detailed
        assert format_smart_track_badge(audio, track_format="invalid_fmt") == "Audio • EAC3 • 5.1 • eng"

    async def test_settings_api_and_runtime_endpoint(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, scratch_dir=None)
        app = create_app(settings)
        app.state.db.initialize()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rt = await client.get("/api/settings/runtime")
            assert rt.status_code == 200
            rt_data = rt.json()
            assert rt_data["cpu_count"] >= 1
            assert rt_data["custom_scratch_path"] is None
            assert rt_data["data_scratch_path"] == str(settings.data_scratch_dir)

            # Reject custom scratch when MKVFORGE_SCRATCH_DIR is not configured
            bad = await client.patch("/api/settings", json={"stage1_scratch_mode": "custom"})
            assert bad.status_code == 400

            # Valid updates persist cleanly
            ok = await client.patch(
                "/api/settings",
                json={
                    "smart_fill_track_format": "lang_codec",
                    "processing_priority_mode": "low_priority",
                    "stage1_scratch_mode": "data",
                },
            )
            assert ok.status_code == 200
            body = ok.json()
            assert body["smart_fill_track_format"] == "lang_codec"
            assert body["processing_priority_mode"] == "low_priority"
            assert body["stage1_scratch_mode"] == "data"

            # Invalid enum rejected with 422
            inv = await client.patch("/api/settings", json={"processing_priority_mode": "turbo"})
            assert inv.status_code == 422

        app.state.library_broker.close()

    def test_frontend_smart_fill_and_settings_ui_landmarks(self) -> None:
        html = (STATIC_JS_DIR.parent / "index.html").read_text(encoding="utf-8")
        assert "C.SETTINGS_SECTIONS.PROCESSING_PERFORMANCE" in html
        assert "Processing &amp; Performance" in html
        assert "Smart Fill Track Format" in html
        assert "Stage 1 Scratch Directory" in html
        assert "smartFillFormatPreview" in html

        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(str(STATIC_JS_DIR))};
        const ctx = {{ window: null, globalThis: null }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "folder_tree.js", "editor.js"]) {{
          vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
        }}
        const Ed = ctx.MKVForgeEditor;
        const audio = {{ type: "audio", codec: "EAC3", audio_channels: 6, language: "eng" }};
        const video = {{ type: "video", codec: "HEVC", pixel_dimensions: "1920x1080", language: "und" }};
        const sub = {{ type: "subtitles", codec: "SRT", language: "eng" }};

        console.log(JSON.stringify({{
          detailed: Ed.getSmartFillPreview("detailed"),
          compact: Ed.getSmartFillPreview("compact"),
          lang_codec: Ed.getSmartFillPreview("lang_codec"),
          fallback: Ed.formatTrackBadge(audio, "unknown"),
          convertedUnd: Ed.buildDefaultAudioTitle("und", "eac3", 6, "640k", "lang_codec"),
        }}));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)
        assert out["detailed"] == {
            "audio": "Audio • EAC3 • 5.1 • eng",
            "video": "Video • HEVC • 1920x1080",
            "subtitle": "Sub • SRT • eng",
            "converted": "EAC3 5.1 • 640k • eng • muxed",
        }
        assert out["compact"] == {
            "audio": "EAC3 5.1",
            "video": "HEVC 1920x1080",
            "subtitle": "English SRT",
            "converted": "EAC3 5.1",
        }
        assert out["lang_codec"] == {
            "audio": "English · EAC3 5.1",
            "video": "HEVC · 1920x1080",
            "subtitle": "English · SRT",
            "converted": "English · EAC3 5.1",
        }
        assert out["fallback"] == "Audio • EAC3 • 5.1 • eng"
        assert out["convertedUnd"] == "EAC3 5.1"
