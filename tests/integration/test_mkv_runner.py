"""Integration tests for Stage 2 mkvmerge runner, atomic swap, .mp4 cleanup, and pipeline."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConstants, Settings
from app.db.connection import DatabaseManager
from app.db.repositories import (
    AppStateRepository,
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
    FFmpegError,
    FFmpegTranscodeResult,
    run_ffmpeg_transcode,
    verify_audio_stream_with_ffprobe,
)
from app.services.mkv_runner import (
    MkvMergeError,
    execute_two_stage_pipeline,
    finalize_remux_output,
    parse_mkvmerge_progress_line,
    run_mkvmerge_remux,
)
from app.services.preflight import TargetFileCollisionError
from app.services.scanner import LibraryScanner


def _fake_stream_reader(lines: list[bytes]) -> MagicMock:
    reader = MagicMock()
    iterator = iter([*lines, b""])
    reader.readline = AsyncMock(side_effect=lambda: next(iterator, b""))
    return reader


def _seed_media_row(repo: MediaFileRepository, filepath: str) -> int:
    return repo.upsert(
        MediaFileUpsert(
            filepath=filepath,
            folder="/movies",
            filename=Path(filepath).name,
            size_bytes=1234,
            mtime=1700000000.0,
            duration_us=0,
            resolution="1080p",
            video_codec="H.264",
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


def _simple_config(
    *,
    convert_audio: bool = False,
    output_mode: str = "replace",
) -> JobConfig:
    return JobConfig(
        container=ContainerMetadataConfig(title="", strip_global_tags=True),
        audio_conversion=AudioConversionConfig(
            enabled=convert_audio,
            source_track_id=1,
            source_audio_index=0,
            source_channels=6,
            target_codec="eac3",
            bitrate="640k",
        ),
        tracks=[
            TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
            TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
        ],
        output_mode=output_mode,
    )


class TestMkvmergeProgressAndExitCodes:
    """Verify progress parsing and exit codes 0, 1 (warnings), and 2 (failure)."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("#GUI#progress 0%", 0),
            ("#GUI#progress 67%", 67),
            ("#GUI#progress 100%", 100),
            ("Progress: 42%", 42),
            ("Muxing took 3 seconds.", None),
        ],
    )
    def test_parse_mkvmerge_progress_line(
        self, line: str, expected: int | None
    ) -> None:
        assert parse_mkvmerge_progress_line(line) == expected

    async def test_exit_code_1_succeeds_and_captures_warnings(
        self, tmp_path: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.stdout = _fake_stream_reader(
            [
                b"#GUI#progress 50%\n",
                b"#GUI#warning Minor timestamp gap detected\n",
                b"#GUI#progress 100%\n",
            ]
        )
        fake_proc.stderr = _fake_stream_reader([])
        fake_proc.wait = AsyncMock(return_value=AppConstants.MKVMERGE_EXIT_WARNINGS)

        percents: list[int] = []
        with patch(
            "app.services.mkv_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            res = await run_mkvmerge_remux(
                tmp_path / "a.mkv",
                tmp_path / "a.mkv.mkvforge.tmp",
                _simple_config(),
                on_progress=lambda pct, _s: percents.append(pct),
            )

        assert res.exit_code == 1
        assert percents == [50, 100]
        assert res.warnings == ["#GUI#warning Minor timestamp gap detected"]

    async def test_exit_code_2_raises_mkvmerge_error(self, tmp_path: Path) -> None:
        fake_proc = MagicMock()
        fake_proc.stdout = _fake_stream_reader([b"#GUI#error Invalid track ID\n"])
        fake_proc.stderr = _fake_stream_reader([])
        fake_proc.wait = AsyncMock(return_value=AppConstants.MKVMERGE_EXIT_ERROR)

        with patch(
            "app.services.mkv_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            with pytest.raises(MkvMergeError, match="exit code 2"):
                await run_mkvmerge_remux(
                    tmp_path / "a.mkv",
                    tmp_path / "a.mkv.mkvforge.tmp",
                    _simple_config(),
                )


class TestAtomicFinalizeAndPipeline:
    """Verify atomic .mkv swap, .mp4 replace + DB cleanup (Guardrail #3), Keep-Both, and finally cleanup."""

    async def test_mp4_replace_original_creates_mkv_deletes_mp4_and_updates_db(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        mp4_path = movies / "Sample.mp4"
        mp4_path.write_bytes(b"original-mp4")

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)
        _seed_media_row(media_repo, "movies/Sample.mp4")

        # Simulate Stage 1 creating temp audio and Stage 2 creating temp remux
        async def _fake_ffmpeg(src, tmp_audio, **_kwargs):
            tmp_audio.write_bytes(b"encoded-eac3")
            return FFmpegTranscodeResult(
                temp_audio_path=tmp_audio,
                channels=6,
                effective_duration_us=10_000_000,
                log_lines=[],
            )

        async def _fake_mkvmerge(src, out_tmp, _cfg, **_kwargs):
            out_tmp.write_bytes(b"remuxed-mkv-bytes")
            from app.services.mkv_runner import MkvRemuxResult

            return MkvRemuxResult(output_tmp_path=out_tmp, exit_code=0)

        fake_probe_data = {
            "container": {"properties": {"duration": 10_000_000_000}},
            "global_tags": [],
            "tracks": [
                {"id": 0, "type": "video", "codec": "H.264", "properties": {"pixel_dimensions": "1920x1080"}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"audio_channels": 6, "language": "eng"}},
            ],
        }

        stages: list[tuple[str, str]] = []
        with (
            patch("app.services.mkv_runner.run_ffmpeg_transcode", new=AsyncMock(side_effect=_fake_ffmpeg)),
            patch("app.services.mkv_runner.run_mkvmerge_remux", new=AsyncMock(side_effect=_fake_mkvmerge)),
            patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(return_value=(fake_probe_data, json.dumps(fake_probe_data))),
            ),
        ):
            result = await execute_two_stage_pipeline(
                mp4_path,
                _simple_config(convert_audio=True, output_mode="replace"),
                storage_root=test_settings.storage_dir,
                duration_us=0,
                media_repo=media_repo,
                scanner=scanner,
                on_stage=lambda s, lbl: stages.append((s, lbl)),
            )

        expected_mkv = movies / "Sample.mkv"
        assert result.output_path == expected_mkv
        assert result.output_rel_filepath == "movies/Sample.mkv"
        assert result.replaced_mp4_deleted is True
        assert expected_mkv.read_bytes() == b"remuxed-mkv-bytes"
        assert not mp4_path.exists()
        # Temp audio & tmp remux must be cleaned up
        assert not (movies / "Sample.mp4.mkvforge.eac3").exists()
        assert not (movies / "Sample.mkv.mkvforge.tmp").exists()
        # Guardrail #3: old .mp4 row deleted AND new .mkv row indexed
        assert media_repo.get_by_filepath("movies/Sample.mp4") is None
        new_row = media_repo.get_by_filepath("movies/Sample.mkv")
        assert new_row is not None
        assert bool(new_row["has_ac3_eac3"]) is True
        assert [s[0] for s in stages] == ["transcode", "remux"]

    async def test_keep_both_auto_increments_and_preserves_original(
        self, test_settings: Settings
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        src = movies / "Film.mkv"
        src.write_bytes(b"orig")
        (movies / "Film.remux.mkv").write_bytes(b"first-remux")
        tmp_out = movies / "Film.mkv.mkvforge.tmp"
        tmp_out.write_bytes(b"second-remux")

        final_path, final_rel, deleted_mp4 = await finalize_remux_output(
            src,
            tmp_out,
            output_mode="keep_both",
            storage_root=test_settings.storage_dir,
        )

        assert final_path == movies / "Film.remux.2.mkv"
        assert final_rel == "movies/Film.remux.2.mkv"
        assert deleted_mp4 is False
        assert final_path.read_bytes() == b"second-remux"
        assert src.read_bytes() == b"orig"
        assert not tmp_out.exists()

    async def test_cleanup_on_stage2_failure_preserves_original_and_deletes_temps(
        self, test_settings: Settings
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        src = movies / "Safe.mkv"
        src.write_bytes(b"original-intact")

        async def _fake_ffmpeg(_src, tmp_audio, **_kwargs):
            tmp_audio.write_bytes(b"temp-audio")
            return FFmpegTranscodeResult(
                temp_audio_path=tmp_audio,
                channels=6,
                effective_duration_us=10_000_000,
                log_lines=[],
            )

        async def _failing_mkvmerge(_src, out_tmp, _cfg, **_kwargs):
            out_tmp.write_bytes(b"partial-tmp")
            raise MkvMergeError("mkvmerge failed with exit code 2")

        with (
            patch("app.services.mkv_runner.run_ffmpeg_transcode", new=AsyncMock(side_effect=_fake_ffmpeg)),
            patch("app.services.mkv_runner.run_mkvmerge_remux", new=AsyncMock(side_effect=_failing_mkvmerge)),
        ):
            with pytest.raises(MkvMergeError, match="exit code 2"):
                await execute_two_stage_pipeline(
                    src,
                    _simple_config(convert_audio=True, output_mode="replace"),
                    storage_root=test_settings.storage_dir,
                    duration_us=10_000_000,
                )

        assert src.read_bytes() == b"original-intact"
        assert not (movies / "Safe.mkv.mkvforge.eac3").exists()
        assert not (movies / "Safe.mkv.mkvforge.tmp").exists()

    async def test_mp4_replace_race_collision_fails_safely(
        self, test_settings: Settings
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        mp4 = movies / "Race.mp4"
        mp4.write_bytes(b"mp4")
        tmp_out = movies / "Race.mkv.mkvforge.tmp"
        tmp_out.write_bytes(b"new-mkv")
        # Another process created Race.mkv after preflight!
        existing_mkv = movies / "Race.mkv"
        existing_mkv.write_bytes(b"do-not-overwrite")

        with pytest.raises(TargetFileCollisionError, match="Race.mkv"):
            await finalize_remux_output(
                mp4,
                tmp_out,
                output_mode="replace",
                storage_root=test_settings.storage_dir,
            )

        assert existing_mkv.read_bytes() == b"do-not-overwrite"
        assert mp4.exists()

    async def test_save_as_writes_to_custom_subfolder_and_preserves_original(
        self, test_settings: Settings, db_manager: DatabaseManager
    ) -> None:
        movies = test_settings.storage_dir / "movies"
        movies.mkdir()
        src = movies / "Original.mp4"
        src.write_bytes(b"original-mp4-untouched")

        media_repo = MediaFileRepository(db_manager)
        state_repo = AppStateRepository(db_manager)
        scanner = LibraryScanner(test_settings, media_repo, state_repo)

        async def _fake_mkvmerge(_src, out_tmp: Path, _cfg, **_kwargs):
            out_tmp.write_bytes(b"saved-as-mkv-bytes")
            from app.services.mkv_runner import MkvRemuxResult

            return MkvRemuxResult(output_tmp_path=out_tmp, exit_code=0)

        fake_probe_data = {
            "container": {"properties": {"duration": 5_000_000_000}},
            "global_tags": [],
            "tracks": [
                {"id": 0, "type": "video", "codec": "H.264", "properties": {"pixel_dimensions": "1920x1080"}},
                {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"audio_channels": 6, "language": "eng"}},
            ],
        }

        cfg = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(enabled=False),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=True),
            ],
            output_mode="save_as",
            output_path="exports/dolby/Original (Dolby).mkv",
        )

        with (
            patch("app.services.mkv_runner.run_mkvmerge_remux", new=AsyncMock(side_effect=_fake_mkvmerge)),
            patch(
                "app.services.scanner.run_mkvmerge_probe",
                new=AsyncMock(return_value=(fake_probe_data, json.dumps(fake_probe_data))),
            ),
        ):
            res = await execute_two_stage_pipeline(
                src,
                cfg,
                storage_root=test_settings.storage_dir,
                duration_us=5_000_000,
                media_repo=media_repo,
                scanner=scanner,
            )

        expected_dst = test_settings.storage_dir / "exports" / "dolby" / "Original (Dolby).mkv"
        assert res.output_path == expected_dst
        assert res.output_rel_filepath == "exports/dolby/Original (Dolby).mkv"
        assert res.replaced_mp4_deleted is False
        assert expected_dst.read_bytes() == b"saved-as-mkv-bytes"
        # Original file MUST remain completely untouched!
        assert src.read_bytes() == b"original-mp4-untouched"
        # New file immediately indexed in SQLite
        assert media_repo.get_by_filepath("exports/dolby/Original (Dolby).mkv") is not None

        # Second Save As to the same existing destination MUST fail safely!
        with pytest.raises(TargetFileCollisionError, match="Original \\(Dolby\\)\\.mkv"):
            await execute_two_stage_pipeline(
                src,
                cfg,
                storage_root=test_settings.storage_dir,
                duration_us=5_000_000,
                media_repo=media_repo,
                scanner=scanner,
            )
        assert expected_dst.read_bytes() == b"saved-as-mkv-bytes"

    @pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="Local ffmpeg/ffprobe not installed",
    )
    async def test_real_ffmpeg_and_ffprobe_stage1_on_synthetic_aac51_mp4(
        self, tmp_path: Path
    ) -> None:
        """Smoke-test real ffprobe + Stage 1 FFmpeg against a 1s synthetic AAC 5.1 .mp4."""
        mp4_file = tmp_path / "synthetic_aac51.mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=5.1:sample_rate=48000:d=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-ac",
            "6",
            "-shortest",
            str(mp4_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        assert proc.returncode == 0

        # Verify ffprobe sees 6-channel AAC AND populates format_duration_us (~1s)
        probe = await verify_audio_stream_with_ffprobe(
            mp4_file,
            audio_index=0,
            expected_codec="AAC",
            expected_channels=6,
        )
        assert probe.channels == 6
        assert probe.format_duration_us >= 900_000

        # Run Stage 1 transcode with duration_us=0 (exercising real .mp4 fallback)
        out_eac3 = tmp_path / "synthetic_aac51.mp4.mkvforge.eac3"
        result = await run_ffmpeg_transcode(
            mp4_file,
            out_eac3,
            audio_index=0,
            target_codec="eac3",
            bitrate="640k",
            duration_us=0,
            expected_codec="AAC",
            expected_channels=6,
        )
        assert out_eac3.is_file()
        assert out_eac3.stat().st_size > 0
        assert result.channels == 6
        assert result.effective_duration_us >= 900_000
