"""Exhaustive Phase C3 tests for chapters save pipeline (`mkvpropedit`, `mkvmerge`, temp XML, summary)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConstants, load_settings
from app.models.schemas import (
    AudioConversionConfig,
    ChapterEntry,
    ContainerMetadataConfig,
    ExternalTrackConfig,
    JobConfig,
    ParsedTrackInfo,
    TrackEditConfig,
    UserSettings,
)
from app.services.chapter_service import (
    extract_chapters_from_file,
    parse_matroska_chapters_xml,
)
from app.services.job_summary import summarize_job_changes
from app.services.job_worker import JobWorker
from app.services.mkv_builder import build_mkvmerge_command
from app.services.mkv_runner import (
    MkvMergeError,
    MkvRemuxResult,
    _materialize_chapters_xml_sync,
    execute_two_stage_pipeline,
    run_mkvmerge_remux,
)
from app.services.mkvpropedit_runner import (
    MkvPropEditError,
    MkvPropEditResult,
    build_mkvpropedit_command,
    can_use_instant_header_edit,
    run_mkvpropedit_edit,
)


def _sample_source_tracks() -> list[ParsedTrackInfo]:
    return [
        ParsedTrackInfo(id=0, type="video", codec="HEVC", default_track=True),
        ParsedTrackInfo(
            id=1,
            type="audio",
            codec="E-AC-3",
            language="eng",
            track_name="English 5.1",
            default_track=True,
        ),
    ]


def _sample_track_edits() -> list[TrackEditConfig]:
    return [
        TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
        TrackEditConfig(
            id=1,
            type="audio",
            enabled=True,
            track_name="English 5.1",
            language="eng",
            default_track=True,
        ),
    ]


def _sample_chapters() -> list[ChapterEntry]:
    return [
        ChapterEntry(start_ms=0, title="Intro"),
        ChapterEntry(start_ms=90_000, title="Opening Credits"),
        ChapterEntry(start_ms=600_000, title="Act I"),
    ]


class TestInstantHeaderEditChapterEligibility:
    """Verify `can_use_instant_header_edit` allows chapter replace/clear on MKV Replace jobs."""

    @pytest.mark.parametrize(
        "container",
        [
            ContainerMetadataConfig(),
            ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            ContainerMetadataConfig(chapters_action="clear"),
            ContainerMetadataConfig(strip_chapters=True),
        ],
    )
    def test_mkv_replace_with_chapter_actions_is_eligible(
        self, tmp_path: Path, container: ContainerMetadataConfig
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = JobConfig(
            container=container,
            tracks=_sample_track_edits(),
            output_mode="replace",
        )
        eligible, reason = can_use_instant_header_edit(
            mkv,
            cfg,
            source_tracks=_sample_source_tracks(),
            binary_available=True,
        )
        assert eligible is True
        assert reason is None

    def test_non_replace_or_mp4_or_tag_strip_still_ineligible(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mp4 = tmp_path / "Movie.mp4"
        replace_chapters = ContainerMetadataConfig(
            chapters_action="replace",
            chapters=_sample_chapters(),
        )
        tracks = _sample_source_tracks()

        # Keep Both -> must use mkvmerge
        eligible_kb, _ = can_use_instant_header_edit(
            mkv,
            JobConfig(
                container=replace_chapters,
                tracks=_sample_track_edits(),
                output_mode="keep_both",
            ),
            source_tracks=tracks,
            binary_available=True,
        )
        assert eligible_kb is False

        # Save As -> must use mkvmerge
        eligible_sa, _ = can_use_instant_header_edit(
            mkv,
            JobConfig(
                container=replace_chapters,
                tracks=_sample_track_edits(),
                output_mode="save_as",
                output_path="exports/Movie.mkv",
            ),
            source_tracks=tracks,
            binary_available=True,
        )
        assert eligible_sa is False

        # MP4 container -> must use mkvmerge
        eligible_mp4, _ = can_use_instant_header_edit(
            mp4,
            JobConfig(
                container=replace_chapters,
                tracks=_sample_track_edits(),
                output_mode="replace",
            ),
            source_tracks=tracks,
            binary_available=True,
        )
        assert eligible_mp4 is False

        # Stripping tags alongside chapters -> still requires full remux
        eligible_tags, reason_tags = can_use_instant_header_edit(
            mkv,
            JobConfig(
                container=ContainerMetadataConfig(
                    strip_global_tags=True,
                    chapters_action="clear",
                ),
                tracks=_sample_track_edits(),
                output_mode="replace",
            ),
            source_tracks=tracks,
            binary_available=True,
        )
        assert eligible_tags is False
        assert reason_tags == "stripping tags or attachments requires full remux"


class TestMkvPropEditChapterCommand:
    """Verify exact `mkvpropedit` argv for keep / replace / clear and error guards."""

    def test_keep_emits_zero_chapter_flags(self, tmp_path: Path) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = JobConfig(
            container=ContainerMetadataConfig(chapters_action="keep"),
            tracks=_sample_track_edits(),
        )
        argv = build_mkvpropedit_command(
            mkv, cfg, source_tracks=_sample_source_tracks()
        )
        assert "--chapters" not in argv
        assert argv == [AppConstants.TOOL_MKVPROPEDIT, str(mkv)]

    def test_replace_emits_chapters_xml_flag_even_for_chapter_only_edit(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        xml = tmp_path / "chapters.mkvforge.chapters.xml"
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
        )
        argv = build_mkvpropedit_command(
            mkv,
            cfg,
            source_tracks=_sample_source_tracks(),
            chapters_xml_path=xml,
        )
        assert argv == [AppConstants.TOOL_MKVPROPEDIT, str(mkv), "--chapters", str(xml)]

    def test_replace_without_xml_path_raises_value_error(self, tmp_path: Path) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
        )
        with pytest.raises(ValueError, match="chapters_xml_path is required"):
            build_mkvpropedit_command(
                mkv, cfg, source_tracks=_sample_source_tracks()
            )

    @pytest.mark.parametrize(
        "container",
        [
            ContainerMetadataConfig(chapters_action="clear"),
            ContainerMetadataConfig(strip_chapters=True),
        ],
    )
    def test_clear_emits_empty_chapters_flag(
        self, tmp_path: Path, container: ContainerMetadataConfig
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        cfg = JobConfig(container=container, tracks=_sample_track_edits())
        argv = build_mkvpropedit_command(
            mkv, cfg, source_tracks=_sample_source_tracks()
        )
        assert argv == [AppConstants.TOOL_MKVPROPEDIT, str(mkv), "--chapters", ""]

    @pytest.mark.asyncio
    async def test_run_mkvpropedit_edit_executes_chapter_only_change(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"fake-mkv")
        xml = tmp_path / "ch.mkvforge.chapters.xml"
        xml.write_text("<Chapters/>", encoding="utf-8")
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
        )

        captured_argv: list[str] = []

        class _FakeProc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"The changes are written to the file.\n", b""

        async def _fake_exec(*args: str, **_kwargs: object) -> _FakeProc:
            captured_argv.extend(args)
            return _FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            result = await run_mkvpropedit_edit(
                mkv,
                cfg,
                source_tracks=_sample_source_tracks(),
                chapters_xml_path=xml,
            )

        assert result.exit_code == 0
        assert captured_argv[-2:] == ["--chapters", str(xml)]


class TestMkvMergeChapterCommand:
    """Verify exact Stage 2 `mkvmerge` argv for keep / replace / clear and error guards."""

    def test_keep_emits_no_source_chapter_flags(self, tmp_path: Path) -> None:
        src = tmp_path / "Movie.mkv"
        out = tmp_path / "Movie.mkv.mkvforge.tmp"
        cfg = JobConfig(
            container=ContainerMetadataConfig(chapters_action="keep"),
            tracks=_sample_track_edits(),
        )
        argv = build_mkvmerge_command(src, out, cfg)
        assert "--chapters" not in argv
        assert "--no-chapters" not in argv

    def test_replace_emits_global_chapters_and_source_no_chapters(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Movie.mkv"
        out = tmp_path / "Movie.mkv.mkvforge.tmp"
        xml = tmp_path / "ch.mkvforge.chapters.xml"
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                title="Dune",
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="keep_both",
        )
        argv = build_mkvmerge_command(src, out, cfg, chapters_xml_path=xml)

        chapters_idx = argv.index("--chapters")
        assert argv[chapters_idx + 1] == str(xml)
        src_idx = argv.index(str(src))
        no_chapters_idx = argv.index("--no-chapters")
        assert chapters_idx < src_idx
        assert no_chapters_idx < src_idx

    def test_replace_without_xml_path_raises_value_error(self, tmp_path: Path) -> None:
        src = tmp_path / "Movie.mkv"
        out = tmp_path / "Movie.mkv.mkvforge.tmp"
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
        )
        with pytest.raises(ValueError, match="chapters_xml_path is required"):
            build_mkvmerge_command(src, out, cfg)

    @pytest.mark.parametrize(
        "container",
        [
            ContainerMetadataConfig(chapters_action="clear"),
            ContainerMetadataConfig(strip_chapters=True),
        ],
    )
    def test_clear_emits_no_chapters_before_source_and_no_chapters_file(
        self, tmp_path: Path, container: ContainerMetadataConfig
    ) -> None:
        src = tmp_path / "Movie.mkv"
        out = tmp_path / "Movie.mkv.mkvforge.tmp"
        cfg = JobConfig(container=container, tracks=_sample_track_edits())
        argv = build_mkvmerge_command(src, out, cfg)
        assert "--chapters" not in argv
        assert argv.index("--no-chapters") < argv.index(str(src))

    @pytest.mark.asyncio
    async def test_run_mkvmerge_remux_forwards_chapters_xml_path(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Movie.mkv"
        out = tmp_path / "Movie.mkv.mkvforge.tmp"
        xml = tmp_path / "ch.mkvforge.chapters.xml"
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="keep_both",
        )
        captured_argv: list[str] = []

        class _EmptyStream:
            async def readline(self) -> bytes:
                return b""

        class _FakeProc:
            stdout = _EmptyStream()
            stderr = _EmptyStream()

            async def wait(self) -> int:
                return 0

        async def _fake_exec(*args: str, **_kwargs: object) -> _FakeProc:
            captured_argv.extend(args)
            return _FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            res = await run_mkvmerge_remux(
                src, out, cfg, chapters_xml_path=xml
            )

        assert res.exit_code == 0
        idx = captured_argv.index("--chapters")
        assert captured_argv[idx + 1] == str(xml)


class TestTempChaptersXmlLifecycleAndPipeline:
    """Verify temp XML serialization, language injection, pipeline routing, and guaranteed cleanup."""

    def test_materialize_chapters_xml_sync_writes_valid_xml_and_cleans_up_on_write_error(
        self,
    ) -> None:
        xml_path = _materialize_chapters_xml_sync(_sample_chapters(), language="eng")
        try:
            assert xml_path.is_file()
            assert xml_path.name.endswith(AppConstants.TEMP_CHAPTERS_SUFFIX)
            raw = xml_path.read_text(encoding="utf-8")
            assert "<ChapterLanguage>eng</ChapterLanguage>" in raw
            parsed, is_complex = parse_matroska_chapters_xml(raw)
            assert is_complex is False
            assert parsed == _sample_chapters()
        finally:
            xml_path.unlink(missing_ok=True)

        # Simulate disk write failure inside os.fdopen -> temp file must be unlinked
        created_paths: list[Path] = []
        real_mkstemp = __import__("tempfile").mkstemp

        def _tracking_mkstemp(**kwargs: object) -> tuple[int, str]:
            fd, path = real_mkstemp(**kwargs)
            created_paths.append(Path(path))
            return fd, path

        def _failing_fdopen(fd: int, *_args: object, **_kwargs: object) -> object:
            os.close(fd)
            raise OSError("Simulated disk full")

        with (
            patch("app.services.mkv_runner.tempfile.mkstemp", side_effect=_tracking_mkstemp),
            patch("app.services.mkv_runner.os.fdopen", side_effect=_failing_fdopen),
        ):
            with pytest.raises(OSError, match="Simulated disk full"):
                _materialize_chapters_xml_sync(_sample_chapters(), language="eng")

        assert len(created_paths) == 1
        assert not created_paths[0].exists()

    @pytest.mark.asyncio
    async def test_pipeline_instant_edit_replace_chapters_materializes_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"source-mkv")
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="replace",
        )
        observed_xml_paths: list[Path] = []
        observed_xml_contents: list[str] = []

        async def _fake_propedit(
            source_path: Path,
            _config: JobConfig,
            *,
            chapters_xml_path: Path | None = None,
            **_kwargs: object,
        ) -> MkvPropEditResult:
            assert chapters_xml_path is not None
            assert chapters_xml_path.is_file()
            observed_xml_paths.append(chapters_xml_path)
            observed_xml_contents.append(chapters_xml_path.read_text(encoding="utf-8"))
            return MkvPropEditResult(target_path=source_path, exit_code=0)

        scanner = MagicMock()
        scanner.probe_single_file = AsyncMock()

        with (
            patch("app.services.mkv_runner.can_use_instant_header_edit", return_value=(True, None)),
            patch("app.services.mkv_runner.run_mkvpropedit_edit", side_effect=_fake_propedit),
        ):
            res = await execute_two_stage_pipeline(
                mkv,
                cfg,
                storage_root=tmp_path,
                source_tracks=_sample_source_tracks(),
                scanner=scanner,
                chapter_language="jpn",
            )

        assert res.output_path == mkv
        assert res.mkvmerge_exit_code == 0
        assert len(observed_xml_paths) == 1
        assert "<ChapterLanguage>jpn</ChapterLanguage>" in observed_xml_contents[0]
        # Temp XML MUST be deleted in finally:
        assert not observed_xml_paths[0].exists()
        # Post-job probe refreshes chapters_count in SQLite immediately
        scanner.probe_single_file.assert_awaited_once_with("Movie.mkv")

    @pytest.mark.asyncio
    async def test_pipeline_instant_edit_clear_creates_no_temp_xml(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"source-mkv")
        cfg = JobConfig(
            container=ContainerMetadataConfig(chapters_action="clear"),
            tracks=_sample_track_edits(),
            output_mode="replace",
        )

        async def _fake_propedit(
            source_path: Path,
            _config: JobConfig,
            *,
            chapters_xml_path: Path | None = None,
            **_kwargs: object,
        ) -> MkvPropEditResult:
            assert chapters_xml_path is None
            return MkvPropEditResult(target_path=source_path, exit_code=0)

        with (
            patch("app.services.mkv_runner.can_use_instant_header_edit", return_value=(True, None)),
            patch("app.services.mkv_runner.run_mkvpropedit_edit", side_effect=_fake_propedit),
        ):
            res = await execute_two_stage_pipeline(
                mkv,
                cfg,
                storage_root=tmp_path,
                source_tracks=_sample_source_tracks(),
            )
        assert res.mkvmerge_exit_code == 0

    @pytest.mark.asyncio
    async def test_pipeline_instant_edit_error_still_cleans_up_temp_xml(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"source-mkv")
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="replace",
        )
        observed_xml_paths: list[Path] = []

        async def _failing_propedit(
            _source_path: Path,
            _config: JobConfig,
            *,
            chapters_xml_path: Path | None = None,
            **_kwargs: object,
        ) -> MkvPropEditResult:
            assert chapters_xml_path is not None
            observed_xml_paths.append(chapters_xml_path)
            raise MkvPropEditError("Simulated mkvpropedit failure")

        with (
            patch("app.services.mkv_runner.can_use_instant_header_edit", return_value=(True, None)),
            patch("app.services.mkv_runner.run_mkvpropedit_edit", side_effect=_failing_propedit),
        ):
            with pytest.raises(MkvPropEditError, match="Simulated mkvpropedit failure"):
                await execute_two_stage_pipeline(
                    mkv,
                    cfg,
                    storage_root=tmp_path,
                    source_tracks=_sample_source_tracks(),
                )

        assert len(observed_xml_paths) == 1
        assert not observed_xml_paths[0].exists()

    @pytest.mark.asyncio
    async def test_pipeline_full_remux_replace_chapters_materializes_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"source-mkv")
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="keep_both",
        )
        observed_xml_paths: list[Path] = []

        async def _fake_remux(
            _source_path: Path,
            output_tmp_path: Path,
            _config: JobConfig,
            *,
            chapters_xml_path: Path | None = None,
            **_kwargs: object,
        ) -> MkvRemuxResult:
            assert chapters_xml_path is not None
            assert chapters_xml_path.is_file()
            observed_xml_paths.append(chapters_xml_path)
            output_tmp_path.write_bytes(b"remuxed-mkv")
            return MkvRemuxResult(output_tmp_path=output_tmp_path, exit_code=0)

        scanner = MagicMock()
        scanner.probe_single_file = AsyncMock()

        with (
            patch("app.services.mkv_runner.run_preflight_checks"),
            patch("app.services.mkv_runner.run_mkvmerge_remux", side_effect=_fake_remux),
        ):
            res = await execute_two_stage_pipeline(
                mkv,
                cfg,
                storage_root=tmp_path,
                source_tracks=_sample_source_tracks(),
                scanner=scanner,
                chapter_language="eng",
            )

        assert res.output_path == tmp_path / "Movie.remux.mkv"
        assert res.output_path.is_file()
        assert len(observed_xml_paths) == 1
        assert not observed_xml_paths[0].exists()
        scanner.probe_single_file.assert_awaited_once_with("Movie.remux.mkv")

    @pytest.mark.asyncio
    async def test_pipeline_full_remux_error_still_cleans_up_temp_xml(
        self, tmp_path: Path
    ) -> None:
        mkv = tmp_path / "Movie.mkv"
        mkv.write_bytes(b"source-mkv")
        cfg = JobConfig(
            container=ContainerMetadataConfig(
                chapters_action="replace",
                chapters=_sample_chapters(),
            ),
            tracks=_sample_track_edits(),
            output_mode="keep_both",
        )
        observed_xml_paths: list[Path] = []

        async def _failing_remux(
            _source_path: Path,
            _output_tmp_path: Path,
            _config: JobConfig,
            *,
            chapters_xml_path: Path | None = None,
            **_kwargs: object,
        ) -> MkvRemuxResult:
            assert chapters_xml_path is not None
            observed_xml_paths.append(chapters_xml_path)
            raise MkvMergeError("Simulated mkvmerge error")

        with (
            patch("app.services.mkv_runner.run_preflight_checks"),
            patch("app.services.mkv_runner.run_mkvmerge_remux", side_effect=_failing_remux),
        ):
            with pytest.raises(MkvMergeError, match="Simulated mkvmerge error"):
                await execute_two_stage_pipeline(
                    mkv,
                    cfg,
                    storage_root=tmp_path,
                    source_tracks=_sample_source_tracks(),
                )

        assert len(observed_xml_paths) == 1
        assert not observed_xml_paths[0].exists()


class TestJobWorkerChapterLanguageResolution:
    """Verify `JobWorker._resolve_chapter_language` reads `preferred_language` or falls back to `und`."""

    def test_resolve_chapter_language_with_and_without_settings_service(
        self, tmp_path: Path
    ) -> None:
        settings = load_settings(
            {"STORAGE_DIR": str(tmp_path), "DATA_DIR": str(tmp_path / "data")}
        )
        worker_no_svc = JobWorker(
            settings=settings,
            media_repo=MagicMock(),
            job_repo=MagicMock(),
            scanner=MagicMock(),
            broker=MagicMock(),
            user_settings_service=None,
        )
        assert worker_no_svc._resolve_chapter_language() == AppConstants.CHAPTER_DEFAULT_LANGUAGE

        mock_user_svc = MagicMock()
        mock_user_svc.get.return_value = UserSettings(preferred_language="fra")
        worker_with_svc = JobWorker(
            settings=settings,
            media_repo=MagicMock(),
            job_repo=MagicMock(),
            scanner=MagicMock(),
            broker=MagicMock(),
            user_settings_service=mock_user_svc,
        )
        assert worker_with_svc._resolve_chapter_language() == "fra"

        mock_user_svc.get.return_value = MagicMock(preferred_language="   ")
        assert (
            worker_with_svc._resolve_chapter_language()
            == AppConstants.CHAPTER_DEFAULT_LANGUAGE
        )


class TestJobSummaryChapters:
    """Verify `summarize_job_changes` surfaces chapter updates and clears cleanly."""

    def test_replace_single_and_multiple_chapters_and_clear(self) -> None:
        # 1 chapter -> singular
        cfg_one = {
            "container": {
                "chapters_action": "replace",
                "chapters": [{"start_ms": 0, "title": "Start"}],
            },
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes_one = summarize_job_changes(
            json.dumps(cfg_one), filepath="movies/A.mkv"
        )
        assert ("changed", "Updated 1 chapter") in [
            (c.kind, c.label) for c in changes_one
        ]

        # N chapters -> plural
        cfg_many = {
            "container": {
                "chapters_action": "replace",
                "chapters": [
                    {"start_ms": 0, "title": "Intro"},
                    {"start_ms": 60_000, "title": "Act I"},
                    {"start_ms": 120_000, "title": "Credits"},
                ],
            },
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes_many = summarize_job_changes(
            json.dumps(cfg_many), filepath="movies/A.mkv"
        )
        assert ("changed", "Updated 3 chapters") in [
            (c.kind, c.label) for c in changes_many
        ]

        # Explicit clear
        cfg_clear = {
            "container": {"chapters_action": "clear"},
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes_clear = summarize_job_changes(
            json.dumps(cfg_clear), filepath="movies/A.mkv"
        )
        assert ("removed", "Cleared chapters") in [
            (c.kind, c.label) for c in changes_clear
        ]

        # Legacy strip_chapters=True
        cfg_legacy = {
            "container": {"strip_chapters": True},
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes_legacy = summarize_job_changes(
            json.dumps(cfg_legacy), filepath="movies/A.mkv"
        )
        assert ("removed", "Cleared chapters") in [
            (c.kind, c.label) for c in changes_legacy
        ]

        # Keep -> emits no chapter pill
        cfg_keep = {
            "container": {"chapters_action": "keep"},
            "audio_conversion": {"enabled": False},
            "tracks": [{"id": 0, "type": "video", "enabled": True}],
            "output_mode": "replace",
        }
        _mode, changes_keep = summarize_job_changes(
            json.dumps(cfg_keep), filepath="movies/A.mkv"
        )
        assert all("chapter" not in c.label.lower() for c in changes_keep)


class TestRealMkvPropEditChapterRoundTrip:
    """Real MKVToolNix `mkvpropedit` + `mkvextract` add -> update -> clear round-trip."""

    @pytest.mark.asyncio
    async def test_real_mkvpropedit_add_update_and_clear_round_trip(
        self, tmp_path: Path
    ) -> None:
        if not (
            shutil.which(AppConstants.TOOL_FFMPEG)
            and shutil.which(AppConstants.TOOL_MKVPROPEDIT)
            and shutil.which(AppConstants.TOOL_MKVEXTRACT)
        ):
            pytest.skip("ffmpeg, mkvpropedit, and mkvextract are required for live round-trip")

        mkv = tmp_path / "roundtrip.mkv"
        subprocess.run(
            [
                AppConstants.TOOL_FFMPEG,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:d=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(mkv),
            ],
            check=True,
            capture_output=True,
        )

        # 1. Add chapters via real mkvpropedit
        initial_chapters = [
            ChapterEntry(start_ms=0, title="Opening"),
            ChapterEntry(start_ms=2_000, title="Middle"),
        ]
        xml_1 = _materialize_chapters_xml_sync(initial_chapters, language="eng")
        try:
            res_add = await run_mkvpropedit_edit(
                mkv,
                JobConfig(
                    container=ContainerMetadataConfig(
                        chapters_action="replace",
                        chapters=initial_chapters,
                    ),
                    tracks=[TrackEditConfig(id=0, type="video", enabled=True)],
                ),
                source_tracks=[ParsedTrackInfo(id=0, type="video", codec="AVC")],
                chapters_xml_path=xml_1,
            )
            assert res_add.exit_code == 0
        finally:
            xml_1.unlink(missing_ok=True)

        read_1, complex_1 = await extract_chapters_from_file(mkv)
        assert complex_1 is False
        assert read_1 == initial_chapters

        # 2. Update chapters
        updated_chapters = [
            ChapterEntry(start_ms=0, title="Cold Open"),
            ChapterEntry(start_ms=1_500, title="Act I"),
            ChapterEntry(start_ms=3_000, title="Credits"),
        ]
        xml_2 = _materialize_chapters_xml_sync(updated_chapters, language="eng")
        try:
            res_upd = await run_mkvpropedit_edit(
                mkv,
                JobConfig(
                    container=ContainerMetadataConfig(
                        chapters_action="replace",
                        chapters=updated_chapters,
                    ),
                    tracks=[TrackEditConfig(id=0, type="video", enabled=True)],
                ),
                source_tracks=[ParsedTrackInfo(id=0, type="video", codec="AVC")],
                chapters_xml_path=xml_2,
            )
            assert res_upd.exit_code == 0
        finally:
            xml_2.unlink(missing_ok=True)

        read_2, complex_2 = await extract_chapters_from_file(mkv)
        assert complex_2 is False
        assert read_2 == updated_chapters

        # 3. Clear chapters via `--chapters ""`
        res_clr = await run_mkvpropedit_edit(
            mkv,
            JobConfig(
                container=ContainerMetadataConfig(chapters_action="clear"),
                tracks=[TrackEditConfig(id=0, type="video", enabled=True)],
            ),
            source_tracks=[ParsedTrackInfo(id=0, type="video", codec="AVC")],
        )
        assert res_clr.exit_code == 0

        read_3, complex_3 = await extract_chapters_from_file(mkv)
        assert complex_3 is False
        assert read_3 == []
