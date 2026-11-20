"""Phase 7 end-to-end media matrix, cancellation safety, and deployment hardening tests."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
    MediaFileUpsert,
    utc_now_iso,
)
from app.models.schemas import (
    AudioConversionConfig,
    ContainerMetadataConfig,
    JobConfig,
    TrackEditConfig,
)
from app.services.ffmpeg_runner import (
    FFmpegTranscodeResult,
    run_ffmpeg_transcode,
    verify_audio_stream_with_ffprobe,
)
from app.services.job_worker import JobWorker
from app.services.mkv_runner import (
    JobCancelledError,
    execute_two_stage_pipeline,
)
from app.services.preflight import TargetFileCollisionError
from app.services.probe_parser import parse_mkvmerge_json
from app.services.scanner import LibraryScanner
from app.services.sse_broker import JobSSEBroker
from tests.helpers.media_fixtures import (
    MediaFixtureConstants,
    enrich_mkv_with_mkvmerge,
    ffprobe_json,
    find_temp_artifacts,
    generate_multi_audio_mkv,
    generate_single_audio_media,
    has_ffmpeg_tools,
    has_full_media_tools,
    mkvmerge_probe_json,
    sha256_file,
)


class _TestPhase7Constants:
    """Centralized timeouts and poll intervals for Phase 7 tests."""

    POLL_INTERVAL_SEC: float = 0.05
    WAIT_TIMEOUT_SEC: float = 20.0
    CANCEL_GRACE_SEC: float = 2.0
    CANCEL_TRIGGER_WAIT_SEC: float = 10.0


def _seed_media_row(
    repo: MediaFileRepository,
    filepath: str,
    *,
    size_bytes: int,
    duration_us: int = 2_000_000,
) -> int:
    return repo.upsert(
        MediaFileUpsert(
            filepath=filepath,
            folder="/test",
            filename=Path(filepath).name,
            size_bytes=size_bytes,
            mtime=1700000000.0,
            duration_us=duration_us,
            resolution="SD",
            video_codec="MPEG-4",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=True,
            has_ac3_eac3=False,
            raw_json="{}",
            scanned_at=utc_now_iso(),
        )
    )


async def _wait_for_condition(
    predicate,
    *,
    timeout_sec: float = _TestPhase7Constants.WAIT_TIMEOUT_SEC,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_TestPhase7Constants.POLL_INTERVAL_SEC)
    raise AssertionError(f"Condition was not satisfied within {timeout_sec}s")


def _make_worker(
    settings: Settings,
    db_manager: DatabaseManager,
) -> tuple[JobWorker, MediaFileRepository, JobRepository, LibraryScanner]:
    media_repo = MediaFileRepository(db_manager)
    state_repo = AppStateRepository(db_manager)
    job_repo = JobRepository(db_manager)
    scanner = LibraryScanner(settings, media_repo, state_repo)
    broker = JobSSEBroker()
    worker = JobWorker(
        settings=settings,
        media_repo=media_repo,
        job_repo=job_repo,
        scanner=scanner,
        broker=broker,
        cancel_grace_sec=_TestPhase7Constants.CANCEL_GRACE_SEC,
    )
    return worker, media_repo, job_repo, scanner


@pytest.mark.skipif(not has_ffmpeg_tools(), reason="Local ffmpeg/ffprobe not installed")
class TestPhase7RealFfmpegAndCancellation:
    """Real FFmpeg/ffprobe stream mapping, .mka metadata stripping, and cancellation safety."""

    async def test_multi_audio_exact_stream_mapping_and_stereo_preservation(
        self, tmp_path: Path
    ) -> None:
        """Confirm FFmpeg grabs the exact selected stream (0:a:1 5.1 vs 0:a:0 2.0) with zero upmix."""
        src = await generate_multi_audio_mkv(tmp_path / "multi_audio.mkv")

        # Stream 0:a:0 MUST be Hindi Stereo (2ch), Stream 0:a:1 MUST be English AAC 5.1 (6ch)
        info_a0 = await verify_audio_stream_with_ffprobe(
            src, audio_index=0, expected_codec="AAC", expected_channels=2
        )
        info_a1 = await verify_audio_stream_with_ffprobe(
            src, audio_index=1, expected_codec="AAC", expected_channels=6
        )
        assert info_a0.channels == AppConstants.CHANNELS_STEREO
        assert info_a1.channels == AppConstants.CHANNELS_51
        assert info_a0.stream_index != info_a1.stream_index

        # Transcode 0:a:0 (Stereo) -> EAC3: MUST remain 2 channels (never upmixed to 5.1)
        out_stereo = tmp_path / "multi_audio.mkv.mkvforge.eac3"
        res_stereo = await run_ffmpeg_transcode(
            src,
            out_stereo,
            audio_index=0,
            target_codec="eac3",
            bitrate="384k",
            expected_codec="AAC",
            expected_channels=2,
        )
        assert res_stereo.channels == AppConstants.CHANNELS_STEREO
        stereo_probe = await ffprobe_json(out_stereo)
        assert stereo_probe["streams"][0]["codec_name"] == "eac3"
        assert stereo_probe["streams"][0]["channels"] == AppConstants.CHANNELS_STEREO

        # Transcode 0:a:1 (AAC 5.1) -> AC3: MUST grab 6 channels
        out_51 = tmp_path / "multi_audio.mkv.mkvforge.ac3"
        res_51 = await run_ffmpeg_transcode(
            src,
            out_51,
            audio_index=1,
            target_codec="ac3",
            bitrate="448k",
            expected_codec="AAC",
            expected_channels=6,
        )
        assert res_51.channels == AppConstants.CHANNELS_51
        ac3_probe = await ffprobe_json(out_51)
        assert ac3_probe["streams"][0]["codec_name"] == "ac3"
        assert ac3_probe["streams"][0]["channels"] == AppConstants.CHANNELS_51

    async def test_aac_output_via_mka_temp_container_preserves_51_and_strips_metadata(
        self, tmp_path: Path
    ) -> None:
        """Confirm EAC3 5.1 -> AAC 5.1 writes valid 6ch .mka with zero leaked container title/tags."""
        src = await generate_single_audio_media(
            tmp_path / "dolby_tagged.mkv",
            audio_codec="eac3",
            channels=AppConstants.CHANNELS_51,
            container_title=MediaFixtureConstants.JUNK_CONTAINER_TITLE,
            global_tag_comment=MediaFixtureConstants.JUNK_TAG_VALUE,
        )

        out_mka = tmp_path / "dolby_tagged.mkv.mkvforge.mka"
        res = await run_ffmpeg_transcode(
            src,
            out_mka,
            audio_index=0,
            target_codec="aac",
            bitrate="384k",
            expected_codec="E-AC-3",
            expected_channels=6,
        )
        assert res.channels == AppConstants.CHANNELS_51
        assert out_mka.is_file() and out_mka.stat().st_size > 0

        mka_probe = await ffprobe_json(out_mka)
        audio_streams = [s for s in mka_probe["streams"] if s["codec_type"] == "audio"]
        assert len(audio_streams) == 1
        assert audio_streams[0]["codec_name"] == "aac"
        assert audio_streams[0]["channels"] == AppConstants.CHANNELS_51

        # Verify -map_metadata -1 prevented source title & comment from leaking into .mka
        fmt_tags = {
            k.lower(): str(v)
            for k, v in (mka_probe.get("format", {}).get("tags") or {}).items()
        }
        assert "title" not in fmt_tags
        assert "comment" not in fmt_tags

    async def test_mid_ffmpeg_cancellation_cleans_temps_and_preserves_original(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        """Cancel a real running FFmpeg process mid-transcode; verify 0 temps and intact original."""
        folder = test_settings.storage_dir / "test"
        folder.mkdir(parents=True, exist_ok=True)
        src = await generate_single_audio_media(
            folder / "long_cancel.mkv",
            audio_codec="aac",
            channels=AppConstants.CHANNELS_51,
            duration_sec=MediaFixtureConstants.LONG_DURATION_SEC,
        )
        original_sha = sha256_file(src)

        worker, media_repo, job_repo, _scanner = _make_worker(test_settings, db_manager)
        _seed_media_row(
            media_repo,
            "test/long_cancel.mkv",
            size_bytes=src.stat().st_size,
            duration_us=MediaFixtureConstants.LONG_DURATION_SEC * AppConstants.US_PER_SEC,
        )

        config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                source_codec="AAC",
                source_channels=6,
                target_codec="eac3",
                bitrate="640k",
            ),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
            ],
            output_mode="replace",
        )

        expected_temp = folder / "long_cancel.mkv.mkvforge.eac3"
        worker.start()
        try:
            created = await worker.enqueue_job(
                filepath="test/long_cancel.mkv", config=config
            )
            job_id = str(created["id"])

            # Wait until real FFmpeg has actually started and created the temp audio file
            await _wait_for_condition(
                lambda: worker._active_proc is not None and expected_temp.exists(),
                timeout_sec=_TestPhase7Constants.CANCEL_TRIGGER_WAIT_SEC,
            )

            cancelled = await worker.cancel_job(job_id)
            assert cancelled is not None
            assert cancelled["status"] == AppConstants.JOB_STATUS_CANCELLED

            # Immediately after cancel_job returns, ZERO temp files must remain
            assert find_temp_artifacts(test_settings.storage_dir) == []
            assert src.exists()
            assert sha256_file(src) == original_sha
            final_row = job_repo.get_by_id(job_id)
            assert final_row is not None
            assert final_row["status"] == AppConstants.JOB_STATUS_CANCELLED
        finally:
            await worker.shutdown()

    async def test_mid_stage2_cancellation_cleans_both_temps_and_preserves_original(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        """Cancel during Stage 2 while both temp audio and temp remux exist; verify 0 temps left."""
        folder = test_settings.storage_dir / "test"
        folder.mkdir(parents=True, exist_ok=True)
        src = folder / "stage2_cancel.mkv"
        src.write_bytes(b"original-mkv-bytes-must-survive")
        original_sha = sha256_file(src)

        worker, media_repo, job_repo, _scanner = _make_worker(test_settings, db_manager)
        _seed_media_row(
            media_repo, "test/stage2_cancel.mkv", size_bytes=src.stat().st_size
        )

        async def _fake_ffmpeg(_src, tmp_audio: Path, **_kwargs):
            tmp_audio.write_bytes(b"stage1-eac3-payload")
            return FFmpegTranscodeResult(
                temp_audio_path=tmp_audio,
                channels=6,
                effective_duration_us=10_000_000,
                log_lines=[],
            )

        stage2_started = asyncio.Event()
        tmp_remux_path = folder / "stage2_cancel.mkv.mkvforge.tmp"

        async def _blocking_stage2(
            _src,
            out_tmp: Path,
            _cfg,
            *,
            on_process_started=None,
            **_kwargs,
        ):
            # Spawn a real child subprocess that creates out_tmp and waits for SIGTERM
            script = (
                "import pathlib, time; "
                f"pathlib.Path({str(out_tmp)!r}).write_bytes(b'partial-mkv-output'); "
                "print('#GUI#progress 35%', flush=True); "
                "time.sleep(30)"
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if on_process_started is not None:
                on_process_started(proc)
            assert proc.stdout is not None
            await proc.stdout.readline()  # Wait until temp file is written
            stage2_started.set()
            rc = await proc.wait()
            raise RuntimeError(f"mkvmerge terminated with code {rc}")

        config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                source_codec="AAC",
                source_channels=6,
                target_codec="eac3",
                bitrate="640k",
            ),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
            ],
            output_mode="replace",
        )

        with (
            patch("app.services.mkv_runner.run_ffmpeg_transcode", new=AsyncMock(side_effect=_fake_ffmpeg)),
            patch("app.services.mkv_runner.run_mkvmerge_remux", new=AsyncMock(side_effect=_blocking_stage2)),
        ):
            worker.start()
            try:
                created = await worker.enqueue_job(
                    filepath="test/stage2_cancel.mkv", config=config
                )
                job_id = str(created["id"])
                await asyncio.wait_for(
                    stage2_started.wait(),
                    timeout=_TestPhase7Constants.CANCEL_TRIGGER_WAIT_SEC,
                )
                # Confirm BOTH temp artifacts exist right before cancel
                assert (folder / "stage2_cancel.mkv.mkvforge.eac3").exists()
                assert tmp_remux_path.exists()

                cancelled = await worker.cancel_job(job_id)
                assert cancelled is not None
                assert cancelled["status"] == AppConstants.JOB_STATUS_CANCELLED
                assert find_temp_artifacts(test_settings.storage_dir) == []
                assert sha256_file(src) == original_sha
                assert job_repo.get_by_id(job_id)["status"] == AppConstants.JOB_STATUS_CANCELLED
            finally:
                await worker.shutdown()

    async def test_cancel_during_ffprobe_prevents_ffmpeg_launch(
        self, tmp_path: Path
    ) -> None:
        """If cancelled while ffprobe is running, ffmpeg must never be spawned."""
        src = await generate_single_audio_media(tmp_path / "probe_cancel.mkv")
        out_audio = tmp_path / "probe_cancel.mkv.mkvforge.eac3"
        calls = 0

        def _check_cancelled() -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:  # Cancel arrives while ffprobe was executing
                raise JobCancelledError(AppConstants.CANCELLED_BY_USER_MESSAGE)

        with pytest.raises(JobCancelledError):
            await run_ffmpeg_transcode(
                src,
                out_audio,
                audio_index=0,
                target_codec="eac3",
                bitrate="640k",
                check_cancelled=_check_cancelled,
            )
        assert not out_audio.exists()


@pytest.mark.skipif(
    not has_full_media_tools(),
    reason="Full E2E media matrix requires ffmpeg, ffprobe, and mkvmerge",
)
class TestPhase7FullMediaMatrixWithMkvmerge:
    """Full end-to-end real ffmpeg + mkvmerge + SQLite matrix (runs on macOS w/ mkvtoolnix or Docker)."""

    async def test_aac51_to_eac3_index0_and_metadata_strip(
        self, test_settings: Settings, db_manager: DatabaseManager, tmp_path: Path
    ) -> None:
        """AAC 5.1 -> EAC3 5.1 at Audio Index 0 (Default=Yes), original AAC demoted, tags stripped."""
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        work = tmp_path / "work"
        work.mkdir()

        raw = await generate_single_audio_media(
            work / "raw.mkv", audio_codec="aac", channels=AppConstants.CHANNELS_51
        )
        target = await enrich_mkv_with_mkvmerge(
            raw,
            movies / "MatrixMovie.mkv",
            work_dir=work,
            container_title=MediaFixtureConstants.JUNK_CONTAINER_TITLE,
            add_global_tags=True,
            add_subtitle=True,
        )

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)
        await scanner.probe_single_file("movies/MatrixMovie.mkv")

        before_row = media_repo.get_by_filepath("movies/MatrixMovie.mkv")
        assert before_row is not None
        assert bool(before_row["has_aac_51"]) is True
        assert bool(before_row["has_ac3_eac3"]) is False
        assert bool(before_row["has_global_tags"]) is True
        assert before_row["container_title"] == MediaFixtureConstants.JUNK_CONTAINER_TITLE

        before_probe = parse_mkvmerge_json(json.loads(before_row["raw_json"]))
        tracks_cfg = [
            TrackEditConfig(
                id=t.id,
                type=t.type,
                enabled=True,
                track_name=t.track_name,
                language=t.language,
                default_track=t.default_track,
                forced_track=t.forced_track,
            )
            for t in before_probe.tracks
        ]
        aac_track = next(t for t in before_probe.tracks if t.type == "audio")

        config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=aac_track.id,
                source_audio_index=aac_track.audio_index or 0,
                source_codec=aac_track.codec,
                source_channels=aac_track.audio_channels or 6,
                source_minimum_timestamp_ns=aac_track.minimum_timestamp_ns,
                target_codec="eac3",
                bitrate="640k",
                track_title="English EAC3 5.1",
                language="eng",
                set_as_default=True,
                drop_source_track=False,
            ),
            tracks=tracks_cfg,
            output_mode="replace",
        )

        result = await execute_two_stage_pipeline(
            target,
            config,
            storage_root=test_settings.storage_dir,
            duration_us=int(before_row["duration_us"]),
            media_repo=media_repo,
            scanner=scanner,
        )
        assert result.output_path == target
        assert find_temp_artifacts(test_settings.storage_dir) == []

        after_probe = parse_mkvmerge_json(await mkvmerge_probe_json(target))
        audio_tracks = [t for t in after_probe.tracks if t.type == "audio"]
        assert len(audio_tracks) == 2
        assert "E-AC-3" in audio_tracks[0].codec
        assert audio_tracks[0].audio_channels == AppConstants.CHANNELS_51
        assert audio_tracks[0].default_track is True
        assert audio_tracks[0].track_name == "English EAC3 5.1"
        assert "AAC" in audio_tracks[1].codec
        assert audio_tracks[1].audio_channels == AppConstants.CHANNELS_51
        assert audio_tracks[1].default_track is False
        assert after_probe.container_title == ""
        assert after_probe.has_global_tags is False

        after_row = media_repo.get_by_filepath("movies/MatrixMovie.mkv")
        assert after_row is not None
        assert bool(after_row["has_ac3_eac3"]) is True
        assert bool(after_row["has_global_tags"]) is False

    async def test_multi_audio_selection_and_sync_delay_preservation(
        self, test_settings: Settings, db_manager: DatabaseManager, tmp_path: Path
    ) -> None:
        """Select Audio #1 (AAC 5.1 +120ms sync) over Audio #0 (Hindi 2.0); verify exact mapping & delay."""
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        work = tmp_path / "work"
        work.mkdir()

        raw = await generate_multi_audio_mkv(work / "raw_multi.mkv")
        raw_probe = parse_mkvmerge_json(await mkvmerge_probe_json(raw))
        audio_tracks_raw = [t for t in raw_probe.tracks if t.type == "audio"]
        english_51_raw = audio_tracks_raw[1]

        target = await enrich_mkv_with_mkvmerge(
            raw,
            movies / "MultiAudioSync.mkv",
            work_dir=work,
            audio_sync_track_id=english_51_raw.id,
            audio_sync_ms=MediaFixtureConstants.SYNC_DELAY_MS,
        )

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)
        await scanner.probe_single_file("movies/MultiAudioSync.mkv")

        row = media_repo.get_by_filepath("movies/MultiAudioSync.mkv")
        assert row is not None
        parsed = parse_mkvmerge_json(json.loads(row["raw_json"]))
        audios = [t for t in parsed.tracks if t.type == "audio"]
        assert audios[0].audio_channels == AppConstants.CHANNELS_STEREO
        assert audios[1].audio_channels == AppConstants.CHANNELS_51
        assert audios[1].sync_delay_ms >= (
            MediaFixtureConstants.SYNC_DELAY_MS
            - MediaFixtureConstants.SYNC_DELAY_TOLERANCE_MS
        )

        config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=audios[1].id,
                source_audio_index=1,
                source_codec=audios[1].codec,
                source_channels=audios[1].audio_channels or 6,
                source_minimum_timestamp_ns=audios[1].minimum_timestamp_ns,
                target_codec="ac3",
                bitrate="448k",
                track_title="English AC3 5.1",
                language="eng",
                set_as_default=True,
                drop_source_track=False,
            ),
            tracks=[
                TrackEditConfig(
                    id=t.id,
                    type=t.type,
                    enabled=True,
                    track_name=t.track_name,
                    language=t.language,
                    default_track=t.default_track,
                )
                for t in parsed.tracks
            ],
            output_mode="replace",
        )

        await execute_two_stage_pipeline(
            target,
            config,
            storage_root=test_settings.storage_dir,
            duration_us=int(row["duration_us"]),
            media_repo=media_repo,
            scanner=scanner,
        )

        after = parse_mkvmerge_json(await mkvmerge_probe_json(target))
        out_audios = [t for t in after.tracks if t.type == "audio"]
        assert len(out_audios) == 3
        assert "AC-3" in out_audios[0].codec
        assert out_audios[0].audio_channels == AppConstants.CHANNELS_51
        assert out_audios[0].default_track is True
        assert (
            abs(out_audios[0].sync_delay_ms - MediaFixtureConstants.SYNC_DELAY_MS)
            <= MediaFixtureConstants.SYNC_DELAY_TOLERANCE_MS
        )
        # Original Hindi Stereo stays 2ch and non-default
        assert out_audios[1].audio_channels == AppConstants.CHANNELS_STEREO
        assert out_audios[1].default_track is False

    async def test_eac3_to_aac_via_mka_and_attachment_sub_stripping(
        self, test_settings: Settings, db_manager: DatabaseManager, tmp_path: Path
    ) -> None:
        """EAC3 5.1 -> AAC 5.1 via .mka temp container with zero tag leak + cover art & sub strip."""
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        work = tmp_path / "work"
        work.mkdir()

        raw = await generate_single_audio_media(
            work / "raw_eac3.mkv", audio_codec="eac3", channels=AppConstants.CHANNELS_51
        )
        target = await enrich_mkv_with_mkvmerge(
            raw,
            movies / "DolbyToAac.mkv",
            work_dir=work,
            container_title=MediaFixtureConstants.JUNK_CONTAINER_TITLE,
            add_global_tags=True,
            add_subtitle=True,
            add_attachment=True,
        )

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)
        await scanner.probe_single_file("movies/DolbyToAac.mkv")
        row = media_repo.get_by_filepath("movies/DolbyToAac.mkv")
        assert row is not None
        parsed = parse_mkvmerge_json(json.loads(row["raw_json"]))
        eac3_track = next(t for t in parsed.tracks if t.type == "audio")

        config = JobConfig(
            container=ContainerMetadataConfig(
                title="",
                strip_global_tags=True,
                strip_track_tags=True,
                strip_attachments=True,
                strip_chapters=True,
            ),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=eac3_track.id,
                source_audio_index=eac3_track.audio_index or 0,
                source_codec=eac3_track.codec,
                source_channels=eac3_track.audio_channels or 6,
                target_codec="aac",
                bitrate="384k",
                track_title="English AAC 5.1",
                language="eng",
                set_as_default=True,
                drop_source_track=False,
            ),
            tracks=[
                TrackEditConfig(
                    id=t.id,
                    type=t.type,
                    enabled=(t.type != "subtitles"),  # Drop subtitle track
                    default_track=t.default_track,
                )
                for t in parsed.tracks
            ],
            output_mode="replace",
        )

        await execute_two_stage_pipeline(
            target,
            config,
            storage_root=test_settings.storage_dir,
            duration_us=int(row["duration_us"]),
            media_repo=media_repo,
            scanner=scanner,
        )
        assert find_temp_artifacts(test_settings.storage_dir) == []

        raw_after = await mkvmerge_probe_json(target)
        after = parse_mkvmerge_json(raw_after)
        out_audios = [t for t in after.tracks if t.type == "audio"]
        assert len(out_audios) == 2
        assert "AAC" in out_audios[0].codec
        assert out_audios[0].audio_channels == AppConstants.CHANNELS_51
        assert out_audios[0].default_track is True
        assert "E-AC-3" in out_audios[1].codec
        assert out_audios[1].default_track is False
        assert after.has_global_tags is False
        assert after.container_title == ""
        assert after.subtitle_count == 0
        assert raw_after.get("attachments", []) == []

    async def test_mp4_replace_collision_guard_and_keep_both_autoincrement(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        """Verify .mp4 -> .mkv Replace + DB cleanup, existing .mkv collision guard, and Keep Both."""
        movies = test_settings.storage_dir / "movies"
        movies.mkdir(parents=True, exist_ok=True)
        mp4 = await generate_single_audio_media(
            movies / "SampleClip.mp4",
            audio_codec="aac",
            channels=AppConstants.CHANNELS_51,
        )

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)
        await scanner.probe_single_file("movies/SampleClip.mp4")

        row = media_repo.get_by_filepath("movies/SampleClip.mp4")
        assert row is not None
        parsed = parse_mkvmerge_json(json.loads(row["raw_json"]))
        aac_track = next(t for t in parsed.tracks if t.type == "audio")

        base_config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=aac_track.id,
                source_audio_index=aac_track.audio_index or 0,
                source_codec=aac_track.codec,
                source_channels=aac_track.audio_channels or 6,
                target_codec="eac3",
                bitrate="640k",
                track_title="English EAC3 5.1",
                language="eng",
                set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(
                    id=t.id, type=t.type, enabled=True, default_track=t.default_track
                )
                for t in parsed.tracks
            ],
            output_mode="replace",
        )

        # 1. Replace Original: SampleClip.mp4 -> SampleClip.mkv, deletes .mp4 + old DB row
        res = await execute_two_stage_pipeline(
            mp4,
            base_config,
            storage_root=test_settings.storage_dir,
            duration_us=int(row["duration_us"]),
            media_repo=media_repo,
            scanner=scanner,
        )
        expected_mkv = movies / "SampleClip.mkv"
        assert res.output_path == expected_mkv
        assert res.replaced_mp4_deleted is True
        assert expected_mkv.is_file()
        assert not mp4.exists()
        assert media_repo.get_by_filepath("movies/SampleClip.mp4") is None
        assert media_repo.get_by_filepath("movies/SampleClip.mkv") is not None

        # 2. Collision guard: a second SampleClip.mp4 MUST refuse to overwrite SampleClip.mkv
        mp4_collide = await generate_single_audio_media(
            movies / "SampleClip.mp4",
            audio_codec="aac",
            channels=AppConstants.CHANNELS_51,
        )
        existing_sha = sha256_file(expected_mkv)
        with pytest.raises(TargetFileCollisionError, match="SampleClip.mkv"):
            await execute_two_stage_pipeline(
                mp4_collide,
                base_config,
                storage_root=test_settings.storage_dir,
                duration_us=0,
                media_repo=media_repo,
                scanner=scanner,
            )
        assert sha256_file(expected_mkv) == existing_sha
        assert mp4_collide.exists()

        # 3. Keep Both auto-increment: creates .remux.mkv then .remux.2.mkv
        keep_config = base_config.model_copy(update={"output_mode": "keep_both"})
        r1 = await execute_two_stage_pipeline(
            mp4_collide,
            keep_config,
            storage_root=test_settings.storage_dir,
            duration_us=0,
            media_repo=media_repo,
            scanner=scanner,
        )
        r2 = await execute_two_stage_pipeline(
            mp4_collide,
            keep_config,
            storage_root=test_settings.storage_dir,
            duration_us=0,
            media_repo=media_repo,
            scanner=scanner,
        )
        assert r1.output_path == movies / "SampleClip.remux.mkv"
        assert r2.output_path == movies / "SampleClip.remux.2.mkv"
        assert mp4_collide.exists()
        assert find_temp_artifacts(test_settings.storage_dir) == []


class TestPhase7DeploymentPermissionsAndMobileAudit:
    """Verify Docker permissions, offline bundle guarantees, and mobile SSE/NPM hardening."""

    def test_entrypoint_syntax_and_permission_controls(
        self, project_root: Path
    ) -> None:
        entrypoint = project_root / "entrypoint.sh"
        assert entrypoint.is_file()
        check = subprocess.run(
            ["bash", "-n", str(entrypoint)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stderr

        body = entrypoint.read_text(encoding=AppConstants.UTF8_ENCODING)
        assert 'PUID="${PUID:-1000}"' in body
        assert 'PGID="${PGID:-1000}"' in body
        assert 'UMASK="${UMASK:-022}"' in body
        assert 'umask "${UMASK}"' in body
        assert 'chown -R "${PUID}:${PGID}" "${DATA_DIR}"' in body
        assert 'exec gosu "${PUID}:${PGID}" uvicorn app.main:app' in body
        assert "--workers 1" in body

    def test_dockerfile_and_compose_enforce_nonroot_and_single_worker(
        self, project_root: Path
    ) -> None:
        dockerfile = (project_root / "Dockerfile").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        compose = (project_root / "docker-compose.yml").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        assert "mkvtoolnix" in dockerfile
        assert "ffmpeg" in dockerfile
        assert "gosu" in dockerfile
        assert "HEALTHCHECK" in dockerfile
        assert "/api/health" in dockerfile
        assert 'PUID: "1000"' in compose
        assert 'PGID: "1000"' in compose
        assert 'UMASK: "022"' in compose
        assert "isolated_lan" in compose

    def test_zero_external_urls_across_all_static_assets(
        self, project_root: Path
    ) -> None:
        static_dir = project_root / "app" / "static"
        html = (static_dir / "index.html").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        assert not re.search(r"https?://", html), "index.html must be 100% offline"

        for js_file in (static_dir / "js").glob("*.js"):
            if js_file.name == "alpine.min.js":
                continue
            body = js_file.read_text(encoding=AppConstants.UTF8_ENCODING)
            assert not re.search(r"https?://", body), (
                f"{js_file.name} must not contain external URLs"
            )

        css = (static_dir / "css" / "app.css").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        assert not re.search(r"@import\s+url\(|url\(\s*['\"]?https?://", css)

    def test_mobile_wakeup_resync_and_sse_npm_headers_configured(
        self, project_root: Path
    ) -> None:
        app_js = (project_root / "app" / "static" / "js" / "app.js").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        api_js = (project_root / "app" / "static" / "js" / "api.js").read_text(
            encoding=AppConstants.UTF8_ENCODING
        )
        assert "visibilitychange" in app_js
        assert "online" in app_js
        assert "forceReconnect" in app_js
        assert "forceReconnect" in api_js

        # Verify SSE endpoints enforce X-Accel-Buffering: no and 15s keepalive ping
        assert AppConstants.SSE_PING_INTERVAL_SEC == 15
        assert AppConstants.SSE_ACCEL_BUFFERING_HEADER == "X-Accel-Buffering"
        assert AppConstants.SSE_ACCEL_BUFFERING_VALUE == "no"
