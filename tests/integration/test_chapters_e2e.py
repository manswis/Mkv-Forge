"""Phase C6 end-to-end integration tests for the full Chapters Editor vertical slice.

Exercises the complete stack through FastAPI HTTP endpoints, SQLite persistence,
real FFmpeg frame extraction (`pipe:1`, zero disk cache), `JobWorker`, `<1s`
`mkvpropedit` Instant Edit, Stage 2 `mkvmerge` full remux (`keep_both`), post-job
SQLite `chapters_count` refresh, `409 Conflict` safety, and temp XML cleanup.

When real MKVToolNix binaries (`mkvpropedit`, `mkvmerge`, `mkvextract`) are on PATH
(Docker / CI), the suite runs them directly; when absent (local macOS without
MKVToolNix), a faithful stateful subprocess boundary harness intercepts only
MKVToolNix invocations while delegating `ffmpeg` / `ffprobe` straight to real binaries.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileRepository, MediaFileUpsert, utc_now_iso
from app.services.chapter_service import (
    parse_matroska_chapters_xml,
    serialize_chapters_to_matroska_xml,
)
from app.utils.paths import is_mkvforge_temp_file

_SYNTHETIC_DURATION_SEC: int = 6
_SYNTHETIC_DURATION_US: int = _SYNTHETIC_DURATION_SEC * AppConstants.US_PER_SEC
_SYNTHETIC_DURATION_NS: int = _SYNTHETIC_DURATION_US * AppConstants.NS_PER_US
_SYNTHETIC_FPS: int = 24
_SYNTHETIC_WIDTH: int = 320
_SYNTHETIC_HEIGHT: int = 180
_SYNTHETIC_FFMPEG_TIMEOUT_SEC: float = 20.0
_JOB_WAIT_TIMEOUT_SEC: float = 15.0
_JOB_POLL_INTERVAL_SEC: float = 0.05
_JPEG_MAGIC: bytes = b"\xff\xd8\xff"
_FAST_FRAME_TS_MS: int = 1_000
_ACCURATE_FRAME_A_TS_MS: int = 2_000
_ACCURATE_FRAME_B_TS_MS: int = 3_000
_HTTP_UNPROCESSABLE_ENTITY: int = 422
_TOOL_IONICE: str = "ionice"

_HAS_FFMPEG: bool = shutil.which(AppConstants.TOOL_FFMPEG) is not None
_HAS_MKVTOOLNIX: bool = all(
    shutil.which(tool) is not None
    for tool in (
        AppConstants.TOOL_MKVPROPEDIT,
        AppConstants.TOOL_MKVMERGE,
        AppConstants.TOOL_MKVEXTRACT,
    )
)


def _create_synthetic_mkv(target_path: Path) -> None:
    """Create a real 6-second MKV with distinct per-second frames using real FFmpeg."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        AppConstants.TOOL_FFMPEG,
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        (
            f"testsrc2=duration={_SYNTHETIC_DURATION_SEC}"
            f":size={_SYNTHETIC_WIDTH}x{_SYNTHETIC_HEIGHT}:rate={_SYNTHETIC_FPS}"
        ),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout=stereo:sample_rate=48000:d={_SYNTHETIC_DURATION_SEC}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        "-g",
        str(_SYNTHETIC_FPS),
        "-c:a",
        "aac",
        "-shortest",
        str(target_path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_SYNTHETIC_FFMPEG_TIMEOUT_SEC,
        check=False,
    )
    if proc.returncode != AppConstants.EXIT_CODE_SUCCESS:
        raise RuntimeError(
            f"Failed to synthesize test MKV (rc={proc.returncode}): {proc.stderr}"
        )


def _build_probe_json(
    mkv_path: Path,
    chapters_count: int,
    *,
    container_title: str = "",
) -> dict[str, Any]:
    """Return an `mkvmerge -J`-compatible probe payload for the synthetic MKV."""
    return {
        "file_name": str(mkv_path),
        "container": {
            "recognized": True,
            "supported": True,
            "type": "Matroska",
            "properties": {
                "duration": _SYNTHETIC_DURATION_NS,
                "title": container_title,
            },
        },
        "chapters": [{"num_entries": chapters_count}] if chapters_count > 0 else [],
        "global_tags": [],
        "track_tags": [],
        "attachments": [],
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "MPEG-4p10/AVC/H.264",
                "properties": {
                    "codec_id": "V_MPEG4/ISO/AVC",
                    "pixel_dimensions": f"{_SYNTHETIC_WIDTH}x{_SYNTHETIC_HEIGHT}",
                    "default_track": True,
                    "forced_track": False,
                    "enabled_track": True,
                },
            },
            {
                "id": 1,
                "type": "audio",
                "codec": "AAC",
                "properties": {
                    "codec_id": "A_AAC",
                    "audio_channels": 2,
                    "language": "eng",
                    "default_track": True,
                    "forced_track": False,
                    "enabled_track": True,
                },
            },
        ],
    }


def _seed_synthetic_mkv_row(
    repo: MediaFileRepository,
    mkv_path: Path,
    rel_filepath: str,
    *,
    chapters_count: int = 0,
) -> int:
    """Insert a realistic `media_files` row matching the synthetic MKV."""
    stat = mkv_path.stat()
    raw_probe = _build_probe_json(mkv_path, chapters_count)
    folder = "/" + str(Path(rel_filepath).parent).lstrip(".") if "/" in rel_filepath else "/"
    return repo.upsert(
        MediaFileUpsert(
            filepath=rel_filepath,
            folder=folder,
            filename=mkv_path.name,
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            duration_us=_SYNTHETIC_DURATION_US,
            resolution=f"{_SYNTHETIC_HEIGHT}p",
            video_codec="H.264",
            audio_summary_json=json.dumps(
                [
                    {
                        "id": 1,
                        "codec": "AAC",
                        "channels": 2,
                        "language": "eng",
                        "track_name": None,
                        "default_track": True,
                    }
                ]
            ),
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=False,
            raw_json=json.dumps(raw_probe),
            scanned_at=utc_now_iso(),
        )
    )


def _make_fake_stream(chunks: list[bytes]) -> MagicMock:
    """Create an async StreamReader-like mock yielding `chunks` then EOF."""
    reader = MagicMock()
    iterator = iter([*chunks, b""])
    reader.readline = AsyncMock(side_effect=lambda: next(iterator, b""))
    return reader


def _make_fake_process(
    *,
    returncode: int = AppConstants.EXIT_CODE_SUCCESS,
    stdout_bytes: bytes = b"",
    stderr_bytes: bytes = b"",
) -> MagicMock:
    """Build an asyncio.subprocess.Process-compatible mock."""
    proc = MagicMock()
    proc.pid = 42424
    proc.returncode = returncode
    stdout_lines = [line + b"\n" for line in stdout_bytes.splitlines()] if stdout_bytes else []
    stderr_lines = [line + b"\n" for line in stderr_bytes.splitlines()] if stderr_bytes else []
    proc.stdout = _make_fake_stream(stdout_lines)
    proc.stderr = _make_fake_stream(stderr_lines)
    proc.communicate = AsyncMock(return_value=(stdout_bytes, stderr_bytes))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


class _MkvToolNixHarness:
    """Run real MKVToolNix if installed, or faithfully simulate its file/XML contract.

    Real `ffmpeg` and `ffprobe` always execute directly without interception.
    """

    def __init__(self, *, fail_mkvpropedit: bool = False) -> None:
        self.fail_mkvpropedit = fail_mkvpropedit
        self.recorded_commands: list[list[str]] = []
        self._chapters_by_inode: dict[tuple[int, int], str] = {}

    @staticmethod
    def _file_key(mkv_path: Path) -> tuple[int, int]:
        st = mkv_path.stat()
        return (st.st_dev, st.st_ino)

    def seed_chapters_xml(self, mkv_path: Path, xml_text: str) -> None:
        """Pre-populate initial Matroska XML for a file when simulating MKVToolNix."""
        self._chapters_by_inode[self._file_key(mkv_path)] = xml_text

    def _chapters_count_for(self, mkv_path: Path) -> int:
        xml_text = self._chapters_by_inode.get(self._file_key(mkv_path), "")
        if not xml_text.strip():
            return 0
        entries, _ = parse_matroska_chapters_xml(xml_text)
        return len(entries)

    def _handle_mkvpropedit(self, argv: list[str]) -> MagicMock:
        if self.fail_mkvpropedit:
            return _make_fake_process(
                returncode=AppConstants.MKVMERGE_EXIT_ERROR,
                stderr_bytes=b"Error: simulated mkvpropedit write failure\n",
            )
        target_key = self._file_key(Path(argv[1]))
        if "--chapters" in argv:
            chap_arg = argv[argv.index("--chapters") + 1]
            if chap_arg == "":
                self._chapters_by_inode.pop(target_key, None)
            else:
                xml_text = Path(chap_arg).read_text(encoding=AppConstants.UTF8_ENCODING)
                # Validate materialized XML using the real parser (strict contract check)
                parse_matroska_chapters_xml(xml_text)
                self._chapters_by_inode[target_key] = xml_text
        return _make_fake_process(
            returncode=AppConstants.EXIT_CODE_SUCCESS,
            stdout_bytes=b"The changes are written to the file.\n",
        )

    def _handle_mkvmerge(self, argv: list[str]) -> MagicMock:
        # Probe call: `mkvmerge -J <path>`
        if "-J" in argv:
            probe_path = Path(argv[argv.index("-J") + 1])
            payload = _build_probe_json(
                probe_path,
                self._chapters_count_for(probe_path),
            )
            return _make_fake_process(
                returncode=AppConstants.EXIT_CODE_SUCCESS,
                stdout_bytes=json.dumps(payload).encode(AppConstants.UTF8_ENCODING),
            )

        # Remux call: `mkvmerge --gui-mode -o <tmp_out> ... <source> [--track-order ...]`
        out_path = Path(argv[argv.index("-o") + 1])
        source_path = next(
            Path(tok)
            for tok in argv[1:]
            if not tok.startswith("-")
            and Path(tok).resolve() != out_path.resolve()
            and Path(tok).suffix.lower() in (AppConstants.EXT_MKV, AppConstants.EXT_MP4)
            and Path(tok).is_file()
        )
        shutil.copy2(source_path, out_path)
        out_key = self._file_key(out_path)
        src_key = self._file_key(source_path)

        if "--chapters" in argv:
            xml_path = Path(argv[argv.index("--chapters") + 1])
            xml_text = xml_path.read_text(encoding=AppConstants.UTF8_ENCODING)
            parse_matroska_chapters_xml(xml_text)
            self._chapters_by_inode[out_key] = xml_text
        elif "--no-chapters" in argv:
            self._chapters_by_inode.pop(out_key, None)
        elif src_key in self._chapters_by_inode:
            self._chapters_by_inode[out_key] = self._chapters_by_inode[src_key]

        return _make_fake_process(
            returncode=AppConstants.EXIT_CODE_SUCCESS,
            stdout_bytes=b"#GUI#progress 50%\n#GUI#progress 100%\n",
        )

    def _handle_mkvextract(self, argv: list[str]) -> MagicMock:
        # `mkvextract <abs_path> chapters <tmp_xml>`
        src_key = self._file_key(Path(argv[1]))
        out_xml = Path(argv[3])
        xml_text = self._chapters_by_inode.get(src_key, "")
        out_xml.write_text(xml_text, encoding=AppConstants.UTF8_ENCODING)
        return _make_fake_process(returncode=AppConstants.EXIT_CODE_SUCCESS)

    @contextmanager
    def activate(self) -> Iterator[_MkvToolNixHarness]:
        """Wrap subprocess execution so MKVToolNix commands are recorded (and simulated if absent)."""
        real_exec = asyncio.create_subprocess_exec

        async def _intercept_exec(*args: Any, **kwargs: Any):
            argv = [str(a) for a in args]
            # Strip optional ionice prefix when inspecting tool binary
            tool_idx = 0
            if argv and Path(argv[0]).name == _TOOL_IONICE:
                for idx, token in enumerate(argv):
                    if Path(token).name in (
                        AppConstants.TOOL_MKVPROPEDIT,
                        AppConstants.TOOL_MKVMERGE,
                        AppConstants.TOOL_MKVEXTRACT,
                        AppConstants.TOOL_FFMPEG,
                    ):
                        tool_idx = idx
                        break
            tool_argv = argv[tool_idx:]
            tool_name = Path(tool_argv[0]).name if tool_argv else ""

            if tool_name in (
                AppConstants.TOOL_MKVPROPEDIT,
                AppConstants.TOOL_MKVMERGE,
                AppConstants.TOOL_MKVEXTRACT,
            ):
                self.recorded_commands.append(tool_argv)
                if not _HAS_MKVTOOLNIX or self.fail_mkvpropedit:
                    if tool_name == AppConstants.TOOL_MKVPROPEDIT:
                        return self._handle_mkvpropedit(tool_argv)
                    if tool_name == AppConstants.TOOL_MKVMERGE:
                        return self._handle_mkvmerge(tool_argv)
                    return self._handle_mkvextract(tool_argv)

            return await real_exec(*args, **kwargs)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_intercept_exec),
            patch(
                "app.services.mkvpropedit_runner.is_mkvpropedit_available",
                return_value=True,
            ),
        ):
            yield self


async def _wait_for_job_terminal(
    api_client: httpx.AsyncClient,
    job_id: str,
) -> dict[str, Any]:
    """Poll `GET /api/jobs` until `job_id` reaches a terminal state."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _JOB_WAIT_TIMEOUT_SEC
    while loop.time() < deadline:
        resp = await api_client.get("/api/jobs")
        assert resp.status_code == AppConstants.HTTP_OK
        payload = resp.json()
        for item in payload.get("history", []):
            if item["id"] == job_id:
                return item
        await asyncio.sleep(_JOB_POLL_INTERVAL_SEC)
    raise AssertionError(f"Timed out waiting for job {job_id} to complete")


def _build_chapter_job_payload(
    *,
    file_id: int,
    output_mode: str,
    chapters_action: str | None = None,
    chapters: list[dict[str, Any]] | None = None,
    strip_chapters: bool = False,
) -> dict[str, Any]:
    """Construct a valid `POST /api/jobs` payload matching frontend `editor.js`."""
    container: dict[str, Any] = {
        "title": "",
        "strip_global_tags": False,
        "strip_track_tags": False,
        "strip_attachments": False,
        "strip_chapters": strip_chapters,
    }
    if chapters_action is not None:
        container["chapters_action"] = chapters_action
    if chapters is not None:
        container["chapters"] = chapters
    return {
        "file_id": file_id,
        "config": {
            "container": container,
            "audio_conversion": {"enabled": False},
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "enabled": True,
                    "default_track": True,
                    "forced_track": False,
                },
                {
                    "id": 1,
                    "type": "audio",
                    "enabled": True,
                    "language": "eng",
                    "default_track": True,
                    "forced_track": False,
                },
            ],
            "output_mode": output_mode,
        },
    }


def _change_labels(job_item: dict[str, Any]) -> list[str]:
    """Extract human-readable change pill labels from a `JobItemResponse` dict."""
    return [str(entry["label"]) for entry in job_item.get("changes", [])]


def _find_leftover_temp_files(storage_dir: Path) -> list[Path]:
    """Return any leftover `*.mkvforge.*` temporary files under `storage_dir`."""
    return [
        p
        for p in storage_dir.rglob("*")
        if p.is_file() and is_mkvforge_temp_file(p)
    ]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg binary required for E2E frame extraction")
class TestChaptersFullVerticalE2E:
    """Phase C6.1 end-to-end verification of Chapters Editor, real FFmpeg, and save pipelines."""

    async def test_e2e_read_parse_real_ffmpeg_frames_instant_edit_and_clear(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        test_settings: Settings,
    ) -> None:
        """Full vertical slice: 0 chapters -> parse -> real FFmpeg frames -> instant replace -> clear."""
        rel_path = "movies/VerticalSlice.mkv"
        mkv_path = test_settings.storage_dir / rel_path
        _create_synthetic_mkv(mkv_path)
        file_id = _seed_synthetic_mkv_row(
            api_app.state.media_repo,
            mkv_path,
            rel_path,
            chapters_count=0,
        )

        harness = _MkvToolNixHarness()
        with harness.activate():
            # 1. GET /chapters on 0-chapter MKV returns [] with ZERO subprocesses
            r_init = await api_client.get(f"/api/files/{file_id}/chapters")
            assert r_init.status_code == AppConstants.HTTP_OK
            assert r_init.json() == {
                "file_id": file_id,
                "duration_us": _SYNTHETIC_DURATION_US,
                "chapters": [],
                "has_complex_chapters": False,
            }
            assert harness.recorded_commands == []

            # 2. POST /chapters/parse auto-detects YouTube text and warns on past-duration line
            r_parse = await api_client.post(
                f"/api/files/{file_id}/chapters/parse",
                json={
                    "raw_text": (
                        "00:00 Intro\n"
                        "00:02 Action Scene\n"
                        "00:04 Credits\n"
                        "09:59 Beyond Movie End\n"
                    ),
                    "filename": "chapters.txt",
                },
            )
            assert r_parse.status_code == AppConstants.HTTP_OK
            parsed_body = r_parse.json()
            assert parsed_body["detected_format"] == "plain_text"
            parsed_chapters = parsed_body["chapters"]
            assert parsed_chapters == [
                {"start_ms": 0, "title": "Intro"},
                {"start_ms": 2000, "title": "Action Scene"},
                {"start_ms": 4000, "title": "Credits"},
            ]
            assert any("past movie duration" in w for w in parsed_body["warnings"])

            # 3. Real FFmpeg GET /frame (fast + accurate 1s apart) with ZERO <data_dir> writes
            data_dir_before = set(test_settings.data_dir.rglob("*"))
            fp = f"{_SYNTHETIC_DURATION_US}_{_SYNTHETIC_HEIGHT}p_H.264"
            r_fast = await api_client.get(
                f"/api/files/{file_id}/frame?ts_ms={_FAST_FRAME_TS_MS}&mode=fast&v={fp}"
            )
            r_acc_a = await api_client.get(
                f"/api/files/{file_id}/frame?ts_ms={_ACCURATE_FRAME_A_TS_MS}&mode=accurate&v={fp}"
            )
            r_acc_b = await api_client.get(
                f"/api/files/{file_id}/frame?ts_ms={_ACCURATE_FRAME_B_TS_MS}&mode=accurate&v={fp}"
            )
            for frame_resp in (r_fast, r_acc_a, r_acc_b):
                assert frame_resp.status_code == AppConstants.HTTP_OK
                assert frame_resp.headers["content-type"] == "image/jpeg"
                assert (
                    frame_resp.headers["cache-control"]
                    == f"private, max-age={AppConstants.SCENE_FRAME_HTTP_CACHE_MAX_AGE_SEC}"
                )
                assert frame_resp.content.startswith(_JPEG_MAGIC)
            # Frame-exact Guardrail #1: 2000ms and 3000ms MUST yield distinct JPEG payloads
            assert r_acc_a.content != r_acc_b.content
            # Option A: ZERO files written into <data_dir>
            assert set(test_settings.data_dir.rglob("*")) == data_dir_before

            # 4. POST /api/jobs (replace + chapters_action="replace") -> <1s mkvpropedit instant edit
            api_app.state.job_worker.start()
            r_job = await api_client.post(
                "/api/jobs",
                json=_build_chapter_job_payload(
                    file_id=file_id,
                    output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                    chapters_action=AppConstants.CHAPTER_ACTION_REPLACE,
                    chapters=parsed_chapters,
                ),
            )
            assert r_job.status_code == AppConstants.HTTP_ACCEPTED
            replace_job_id = r_job.json()["id"]
            completed_replace = await _wait_for_job_terminal(api_client, replace_job_id)
            assert completed_replace["status"] == AppConstants.JOB_STATUS_COMPLETED
            assert completed_replace["progress"] == 100
            assert "Updated 3 chapters" in _change_labels(completed_replace)

            # Prove <1s mkvpropedit ran (and NOT full mkvmerge remux with -o)
            propedit_cmds = [
                cmd for cmd in harness.recorded_commands if Path(cmd[0]).name == AppConstants.TOOL_MKVPROPEDIT
            ]
            remux_cmds = [
                cmd
                for cmd in harness.recorded_commands
                if Path(cmd[0]).name == AppConstants.TOOL_MKVMERGE and "-o" in cmd
            ]
            assert len(propedit_cmds) == 1
            assert "--chapters" in propedit_cmds[0]
            assert remux_cmds == []
            assert _find_leftover_temp_files(test_settings.storage_dir) == []

            # SQLite chapters_count immediately refreshed to 3
            r_detail = await api_client.get(f"/api/files/{file_id}")
            assert r_detail.status_code == AppConstants.HTTP_OK
            assert r_detail.json()["chapters_count"] == 3

            # GET /chapters now returns the exact 3 written chapters
            r_read = await api_client.get(f"/api/files/{file_id}/chapters")
            assert r_read.status_code == AppConstants.HTTP_OK
            assert r_read.json()["chapters"] == parsed_chapters
            assert r_read.json()["has_complex_chapters"] is False

            # 5. Clear flow (`chapters_action="clear"`) -> mkvpropedit --chapters ""
            r_clear = await api_client.post(
                "/api/jobs",
                json=_build_chapter_job_payload(
                    file_id=file_id,
                    output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                    chapters_action=AppConstants.CHAPTER_ACTION_CLEAR,
                ),
            )
            assert r_clear.status_code == AppConstants.HTTP_ACCEPTED
            completed_clear = await _wait_for_job_terminal(api_client, r_clear.json()["id"])
            assert completed_clear["status"] == AppConstants.JOB_STATUS_COMPLETED
            assert "Cleared chapters" in _change_labels(completed_clear)

            propedit_cmds_after = [
                cmd for cmd in harness.recorded_commands if Path(cmd[0]).name == AppConstants.TOOL_MKVPROPEDIT
            ]
            assert len(propedit_cmds_after) == 2
            clear_cmd = propedit_cmds_after[-1]
            assert clear_cmd[clear_cmd.index("--chapters") + 1] == ""
            assert _find_leftover_temp_files(test_settings.storage_dir) == []

            # SQLite chapters_count immediately refreshed to 0 and GET /chapters == []
            r_detail_cleared = await api_client.get(f"/api/files/{file_id}")
            assert r_detail_cleared.status_code == AppConstants.HTTP_OK
            assert r_detail_cleared.json()["chapters_count"] == 0

            r_read_cleared = await api_client.get(f"/api/files/{file_id}/chapters")
            assert r_read_cleared.status_code == AppConstants.HTTP_OK
            assert r_read_cleared.json()["chapters"] == []

    async def test_e2e_keep_both_full_remux_writes_chapters_and_preserves_original(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        test_settings: Settings,
    ) -> None:
        """Full remux flow (`keep_both` + chapters replace) writes `.remux.mkv` and leaves original untouched."""
        rel_path = "movies/OriginalFilm.mkv"
        mkv_path = test_settings.storage_dir / rel_path
        _create_synthetic_mkv(mkv_path)
        original_bytes = mkv_path.read_bytes()

        from app.models.schemas import ChapterEntry

        initial_chapters = [ChapterEntry(start_ms=0, title="Original Chapter")]
        initial_xml = serialize_chapters_to_matroska_xml(initial_chapters)
        file_id = _seed_synthetic_mkv_row(
            api_app.state.media_repo,
            mkv_path,
            rel_path,
            chapters_count=1,
        )

        harness = _MkvToolNixHarness()
        if _HAS_MKVTOOLNIX:
            tmp_seed_xml = test_settings.storage_dir / "seed.xml"
            tmp_seed_xml.write_text(initial_xml, encoding=AppConstants.UTF8_ENCODING)
            try:
                subprocess.run(
                    [AppConstants.TOOL_MKVPROPEDIT, str(mkv_path), "--chapters", str(tmp_seed_xml)],
                    check=True,
                    capture_output=True,
                )
            finally:
                tmp_seed_xml.unlink(missing_ok=True)
            original_bytes = mkv_path.read_bytes()
        else:
            harness.seed_chapters_xml(mkv_path, initial_xml)

        new_chapters = [
            {"start_ms": 0, "title": "Remux Intro"},
            {"start_ms": 3000, "title": "Remux Climax"},
        ]

        with harness.activate():
            api_app.state.job_worker.start()
            r_job = await api_client.post(
                "/api/jobs",
                json=_build_chapter_job_payload(
                    file_id=file_id,
                    output_mode=AppConstants.OUTPUT_MODE_KEEP_BOTH,
                    chapters_action=AppConstants.CHAPTER_ACTION_REPLACE,
                    chapters=new_chapters,
                ),
            )
            assert r_job.status_code == AppConstants.HTTP_ACCEPTED
            done_job = await _wait_for_job_terminal(api_client, r_job.json()["id"])
            assert done_job["status"] == AppConstants.JOB_STATUS_COMPLETED
            assert done_job["output_filepath"] == "movies/OriginalFilm.remux.mkv"
            assert "Updated 2 chapters" in _change_labels(done_job)

            # Prove Stage 2 mkvmerge ran with `--no-chapters` + `--chapters <xml>` (and NOT mkvpropedit)
            propedit_cmds = [
                cmd for cmd in harness.recorded_commands if Path(cmd[0]).name == AppConstants.TOOL_MKVPROPEDIT
            ]
            remux_cmds = [
                cmd
                for cmd in harness.recorded_commands
                if Path(cmd[0]).name == AppConstants.TOOL_MKVMERGE and "-o" in cmd
            ]
            assert propedit_cmds == []
            assert len(remux_cmds) == 1
            assert "--no-chapters" in remux_cmds[0]
            assert "--chapters" in remux_cmds[0]
            assert _find_leftover_temp_files(test_settings.storage_dir) == []

            # Original file bytes and original chapter remain 100% untouched
            assert mkv_path.read_bytes() == original_bytes
            r_orig_ch = await api_client.get(f"/api/files/{file_id}/chapters")
            assert r_orig_ch.status_code == AppConstants.HTTP_OK
            assert r_orig_ch.json()["chapters"] == [
                {"start_ms": 0, "title": "Original Chapter"}
            ]

            # New `.remux.mkv` is indexed in SQLite with chapters_count == 2 and readable chapters
            remux_row = api_app.state.media_repo.get_by_filepath("movies/OriginalFilm.remux.mkv")
            assert remux_row is not None
            r_remux_detail = await api_client.get(f"/api/files/{remux_row['id']}")
            assert r_remux_detail.status_code == AppConstants.HTTP_OK
            assert r_remux_detail.json()["chapters_count"] == 2
            r_remux_ch = await api_client.get(f"/api/files/{remux_row['id']}/chapters")
            assert r_remux_ch.status_code == AppConstants.HTTP_OK
            assert r_remux_ch.json()["chapters"] == new_chapters

    async def test_e2e_legacy_strip_chapters_validation_409_and_error_cleanup(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        test_settings: Settings,
    ) -> None:
        """Negative & edge E2E: invalid payloads -> 422, legacy strip_chapters, active 409, and error cleanup."""
        rel_path = "movies/EdgeGuards.mkv"
        mkv_path = test_settings.storage_dir / rel_path
        _create_synthetic_mkv(mkv_path)
        file_id = _seed_synthetic_mkv_row(
            api_app.state.media_repo,
            mkv_path,
            rel_path,
            chapters_count=2,
        )

        # 1. Invalid chapter payloads rejected synchronously with 422 before hitting the queue
        r_empty_replace = await api_client.post(
            "/api/jobs",
            json=_build_chapter_job_payload(
                file_id=file_id,
                output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                chapters_action=AppConstants.CHAPTER_ACTION_REPLACE,
                chapters=[],
            ),
        )
        assert r_empty_replace.status_code == _HTTP_UNPROCESSABLE_ENTITY

        r_clear_with_entries = await api_client.post(
            "/api/jobs",
            json=_build_chapter_job_payload(
                file_id=file_id,
                output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                chapters_action=AppConstants.CHAPTER_ACTION_CLEAR,
                chapters=[{"start_ms": 0, "title": "Invalid"}],
            ),
        )
        assert r_clear_with_entries.status_code == _HTTP_UNPROCESSABLE_ENTITY

        # 2. Active replace job triggers 409 Conflict on BOTH /chapters and /frame
        job_repo = api_app.state.job_repo
        job_repo.create_job(
            job_id="active-replace-guard",
            filepath=rel_path,
            config_json=json.dumps({"output_mode": AppConstants.OUTPUT_MODE_REPLACE}),
        )
        job_repo.claim_next_queued_job()
        assert (
            await api_client.get(f"/api/files/{file_id}/frame?ts_ms=1000&mode=fast")
        ).status_code == AppConstants.HTTP_CONFLICT
        assert (
            await api_client.get(f"/api/files/{file_id}/chapters")
        ).status_code == AppConstants.HTTP_CONFLICT
        job_repo.mark_completed("active-replace-guard", output_filepath=rel_path)

        # 3. Failed mkvpropedit job cleans up temp XML cleanly and transitions to `failed`
        fail_harness = _MkvToolNixHarness(fail_mkvpropedit=True)
        with fail_harness.activate():
            api_app.state.job_worker.start()
            r_fail_job = await api_client.post(
                "/api/jobs",
                json=_build_chapter_job_payload(
                    file_id=file_id,
                    output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                    chapters_action=AppConstants.CHAPTER_ACTION_REPLACE,
                    chapters=[{"start_ms": 0, "title": "Should Fail Cleanly"}],
                ),
            )
            assert r_fail_job.status_code == AppConstants.HTTP_ACCEPTED
            failed_job = await _wait_for_job_terminal(api_client, r_fail_job.json()["id"])
            assert failed_job["status"] == AppConstants.JOB_STATUS_FAILED
            assert _find_leftover_temp_files(test_settings.storage_dir) == []

        # 4. Legacy `strip_chapters=True` (without `chapters_action`) maps to `clear` and succeeds
        ok_harness = _MkvToolNixHarness()
        with ok_harness.activate():
            r_legacy = await api_client.post(
                "/api/jobs",
                json=_build_chapter_job_payload(
                    file_id=file_id,
                    output_mode=AppConstants.OUTPUT_MODE_REPLACE,
                    strip_chapters=True,
                ),
            )
            assert r_legacy.status_code == AppConstants.HTTP_ACCEPTED
            legacy_done = await _wait_for_job_terminal(api_client, r_legacy.json()["id"])
            assert legacy_done["status"] == AppConstants.JOB_STATUS_COMPLETED
            assert "Cleared chapters" in _change_labels(legacy_done)
            assert _find_leftover_temp_files(test_settings.storage_dir) == []
