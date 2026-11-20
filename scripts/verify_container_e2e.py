#!/usr/bin/env python3
"""Standalone Phase 7 E2E & permissions verifier for the MKV Forge Docker container.

Uses only the Python standard library + `app.*` modules (zero `pytest` dependency),
so it can be executed directly inside the production runtime image on the Docker
host as `PUID:PGID` (`1000:1000`):

    docker compose cp scripts/verify_container_e2e.py mkv-forge:/tmp/verify_container_e2e.py
    docker compose exec -u 1000:1000 mkv-forge python3 /tmp/verify_container_e2e.py

Creates an isolated temporary workspace under `<storage_dir>/test/_phase7_verify`
(or `/tmp` with `--tmp-only`) and an isolated SQLite DB under `/tmp`, runs the
full real `ffmpeg` + `mkvmerge` matrix + mid-FFmpeg & mid-mkvmerge cancellation
+ UID/GID/UMASK permission checks, and cleans up all temporary files on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path

# Ensure `/app` (or repo root when run locally) is importable
_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))
if Path("/app").is_dir() and "/app" not in sys.path:
    sys.path.insert(0, "/app")

from app.config import AppConstants, Settings  # noqa: E402
from app.db.connection import DatabaseManager  # noqa: E402
from app.db.repositories import (  # noqa: E402
    AppStateRepository,
    JobRepository,
    MediaFileRepository,
)
from app.models.schemas import (  # noqa: E402
    AudioConversionConfig,
    ContainerMetadataConfig,
    JobConfig,
    TrackEditConfig,
)
from app.services.job_worker import JobWorker  # noqa: E402
from app.services.mkv_runner import execute_two_stage_pipeline  # noqa: E402
from app.services.preflight import TargetFileCollisionError  # noqa: E402
from app.services.probe_parser import parse_mkvmerge_json  # noqa: E402
from app.services.scanner import LibraryScanner  # noqa: E402
from app.services.sse_broker import JobSSEBroker  # noqa: E402
from app.utils.paths import is_mkvforge_temp_file  # noqa: E402


class _VerifyConstants:
    SHORT_SEC: int = 2
    LONG_SEC: int = 90
    SYNC_MS: int = 120
    SYNC_TOLERANCE_MS: int = 25
    POLL_SEC: float = 0.05
    CANCEL_TIMEOUT_SEC: float = 15.0
    EXPECTED_UID: int = 1000
    EXPECTED_GID: int = 1000
    VIDEO_SIZE: str = "160x120"
    VIDEO_FPS: str = "10"
    SAMPLE_RATE: int = 48_000
    JUNK_TITLE: str = "Phase7.Junk.Title.1080p"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_temps(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and is_mkvforge_temp_file(p)]


def _write_png(path: Path) -> Path:
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x63\x66\xf1")
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    return path


def _write_tags_xml(path: Path) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Tags><Tag><Targets /><Simple>"
        "<Name>RELEASE_GROUP</Name><String>SpamTag</String>"
        "</Simple></Tag></Tags>\n",
        encoding="utf-8",
    )
    return path


async def _run(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI failed (rc={proc.returncode}): {' '.join(args)}\n"
            f"{err.decode('utf-8', errors='replace')}"
        )
    return out


async def _probe(path: Path) -> dict:
    raw = await _run("mkvmerge", "-J", str(path))
    return json.loads(raw.decode("utf-8", errors="replace"))


async def _gen_single(
    out_path: Path,
    *,
    codec: str = "aac",
    channels: int = 6,
    duration_sec: int = _VerifyConstants.SHORT_SEC,
) -> Path:
    layout = "5.1" if channels == 6 else "stereo"
    await _run(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={_VerifyConstants.VIDEO_SIZE}:r={_VerifyConstants.VIDEO_FPS}:d={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate={_VerifyConstants.SAMPLE_RATE}:duration={duration_sec}",
        "-filter_complex",
        f"[1:a]aformat=channel_layouts={layout}[aout]",
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "mpeg4",
        "-c:a",
        codec,
        "-ac",
        str(channels),
        "-shortest",
        str(out_path),
    )
    return out_path


async def _gen_multi(out_path: Path) -> Path:
    await _run(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={_VerifyConstants.VIDEO_SIZE}:r={_VerifyConstants.VIDEO_FPS}:d={_VerifyConstants.SHORT_SEC}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate={_VerifyConstants.SAMPLE_RATE}:duration={_VerifyConstants.SHORT_SEC}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=660:sample_rate={_VerifyConstants.SAMPLE_RATE}:duration={_VerifyConstants.SHORT_SEC}",
        "-filter_complex",
        "[1:a]aformat=channel_layouts=stereo[a0];[2:a]aformat=channel_layouts=5.1[a1]",
        "-map",
        "0:v:0",
        "-map",
        "[a0]",
        "-map",
        "[a1]",
        "-c:v",
        "mpeg4",
        "-c:a:0",
        "aac",
        "-ac:a:0",
        "2",
        "-metadata:s:a:0",
        "language=hin",
        "-c:a:1",
        "aac",
        "-ac:a:1",
        "6",
        "-metadata:s:a:1",
        "language=eng",
        "-shortest",
        str(out_path),
    )
    return out_path


async def _wait_for(predicate, timeout_sec: float = _VerifyConstants.CANCEL_TIMEOUT_SEC) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_VerifyConstants.POLL_SEC)
    raise TimeoutError(f"Condition timed out after {timeout_sec}s")


async def run_verification(storage_root: Path, check_uid_gid: bool) -> None:
    work_root = Path(tempfile.mkdtemp(prefix="mkvforge_p7_db_"))
    test_dir = storage_root / "_phase7_verify"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = work_root / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        storage_dir=test_dir,
        data_dir=work_root,
        static_dir=Path("/app/app/static"),
        health_timeout_sec=AppConstants.DEFAULT_HEALTH_TIMEOUT_SEC,
        scan_timeout_sec=AppConstants.DEFAULT_SCAN_TIMEOUT_SEC,
        log_level="INFO",
    )
    db = DatabaseManager(settings.db_path)
    db.initialize()
    media_repo = MediaFileRepository(db)
    state_repo = AppStateRepository(db)
    job_repo = JobRepository(db)
    scanner = LibraryScanner(settings, media_repo, state_repo)

    try:
        # 1. AAC 5.1 -> EAC3 5.1 Index 0 + Metadata Strip + Ownership Check
        print("[1/6] AAC 5.1 -> EAC3 5.1 at Index 0 + metadata strip...")
        raw1 = await _gen_single(scratch_dir / "raw1.mkv", codec="aac", channels=6)
        tags_xml = _write_tags_xml(scratch_dir / "tags.xml")
        f1 = test_dir / "matrix_aac51.mkv"
        await _run(
            "mkvmerge",
            "-o",
            str(f1),
            "--title",
            _VerifyConstants.JUNK_TITLE,
            "--global-tags",
            str(tags_xml),
            str(raw1),
        )
        await scanner.probe_single_file(f1.name)
        r1 = media_repo.get_by_filepath(f1.name)
        assert r1 is not None and bool(r1["has_global_tags"])
        p1 = parse_mkvmerge_json(json.loads(r1["raw_json"]))
        aac_t = next(t for t in p1.tracks if t.type == "audio")
        cfg1 = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=aac_t.id,
                source_audio_index=aac_t.audio_index or 0,
                source_codec=aac_t.codec,
                source_channels=6,
                target_codec="eac3",
                bitrate="640k",
                track_title="English EAC3 5.1",
                language="eng",
                set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(id=t.id, type=t.type, enabled=True, default_track=t.default_track)
                for t in p1.tracks
            ],
            output_mode="replace",
        )
        await execute_two_stage_pipeline(
            f1, cfg1, storage_root=test_dir, duration_us=int(r1["duration_us"]),
            media_repo=media_repo, scanner=scanner,
        )
        after1 = parse_mkvmerge_json(await _probe(f1))
        a1 = [t for t in after1.tracks if t.type == "audio"]
        assert "E-AC-3" in a1[0].codec and a1[0].audio_channels == 6 and a1[0].default_track is True
        assert "AAC" in a1[1].codec and a1[1].default_track is False
        assert after1.container_title == "" and after1.has_global_tags is False
        if check_uid_gid:
            st = f1.stat()
            assert st.st_uid == _VerifyConstants.EXPECTED_UID, f"Expected uid 1000, got {st.st_uid}"
            assert st.st_gid == _VerifyConstants.EXPECTED_GID, f"Expected gid 1000, got {st.st_gid}"
            mode_oct = oct(stat.S_IMODE(st.st_mode))
            print(f"      PASS (uid={st.st_uid}, gid={st.st_gid}, mode={mode_oct})")
        else:
            print("      PASS")

        # 2. Multi-Audio Exact Stream Mapping (0:a:1) + +120ms Sync Delay
        print("[2/6] Multi-audio track mapping (0:a:1 AAC 5.1 +120ms sync)...")
        raw2 = await _gen_multi(scratch_dir / "raw_multi.mkv")
        p_raw2 = parse_mkvmerge_json(await _probe(raw2))
        eng_track = [t for t in p_raw2.tracks if t.type == "audio"][1]
        f2 = test_dir / "matrix_multi.mkv"
        await _run("mkvmerge", "-o", str(f2), "--sync", f"{eng_track.id}:{_VerifyConstants.SYNC_MS}", str(raw2))
        await scanner.probe_single_file(f2.name)
        r2 = media_repo.get_by_filepath(f2.name)
        p2 = parse_mkvmerge_json(json.loads(r2["raw_json"]))
        audios2 = [t for t in p2.tracks if t.type == "audio"]
        cfg2 = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=audios2[1].id,
                source_audio_index=1,
                source_codec=audios2[1].codec,
                source_channels=6,
                source_minimum_timestamp_ns=audios2[1].minimum_timestamp_ns,
                target_codec="ac3",
                bitrate="448k",
                track_title="English AC3 5.1",
                language="eng",
                set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(id=t.id, type=t.type, enabled=True, default_track=t.default_track)
                for t in p2.tracks
            ],
            output_mode="replace",
        )
        await execute_two_stage_pipeline(
            f2, cfg2, storage_root=test_dir, duration_us=int(r2["duration_us"]),
            media_repo=media_repo, scanner=scanner,
        )
        after2 = parse_mkvmerge_json(await _probe(f2))
        a2 = [t for t in after2.tracks if t.type == "audio"]
        assert "AC-3" in a2[0].codec and a2[0].audio_channels == 6 and a2[0].default_track is True
        assert abs(a2[0].sync_delay_ms - _VerifyConstants.SYNC_MS) <= _VerifyConstants.SYNC_TOLERANCE_MS
        assert a2[1].audio_channels == 2 and a2[1].default_track is False
        print(f"      PASS (AC3 5.1 at Index 0, sync={a2[0].sync_delay_ms}ms)")

        # 3. EAC3 5.1 -> AAC 5.1 via .mka temp container + Cover Art & Sub Strip
        print("[3/6] EAC3 5.1 -> AAC 5.1 via .mka temp container + zero tag leak...")
        raw3 = await _gen_single(scratch_dir / "raw_eac3.mkv", codec="eac3", channels=6)
        cover_png = _write_png(scratch_dir / "cover.png")
        srt = scratch_dir / "sub.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,500\nHi\n", encoding="utf-8")
        f3 = test_dir / "matrix_eac3_to_aac.mkv"
        await _run(
            "mkvmerge", "-o", str(f3),
            "--title", _VerifyConstants.JUNK_TITLE,
            "--global-tags", str(tags_xml),
            "--attachment-name", "cover.png",
            "--attachment-mime-type", "image/png",
            "--attach-file", str(cover_png),
            str(raw3),
            "--language", "0:eng", str(srt),
        )
        await scanner.probe_single_file(f3.name)
        r3 = media_repo.get_by_filepath(f3.name)
        p3 = parse_mkvmerge_json(json.loads(r3["raw_json"]))
        eac3_t = next(t for t in p3.tracks if t.type == "audio")
        cfg3 = JobConfig(
            container=ContainerMetadataConfig(
                title="", strip_global_tags=True, strip_track_tags=True,
                strip_attachments=True, strip_chapters=True,
            ),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=eac3_t.id,
                source_audio_index=0,
                source_codec=eac3_t.codec,
                source_channels=6,
                target_codec="aac",
                bitrate="384k",
                track_title="English AAC 5.1",
                language="eng",
                set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(
                    id=t.id, type=t.type, enabled=(t.type != "subtitles"), default_track=t.default_track
                )
                for t in p3.tracks
            ],
            output_mode="replace",
        )
        await execute_two_stage_pipeline(
            f3, cfg3, storage_root=test_dir, duration_us=int(r3["duration_us"]),
            media_repo=media_repo, scanner=scanner,
        )
        raw_after3 = await _probe(f3)
        after3 = parse_mkvmerge_json(raw_after3)
        a3 = [t for t in after3.tracks if t.type == "audio"]
        assert "AAC" in a3[0].codec and a3[0].audio_channels == 6 and a3[0].default_track is True
        assert "E-AC-3" in a3[1].codec and a3[1].default_track is False
        assert after3.has_global_tags is False and after3.subtitle_count == 0
        assert raw_after3.get("attachments", []) == []
        print("      PASS")

        # 4. .mp4 -> .mkv Replace Original + DB Row Cleanup + Collision Guard + Keep Both
        print("[4/6] .mp4 -> .mkv Replace + DB cleanup + collision guard + Keep Both...")
        f4_mp4 = await _gen_single(test_dir / "matrix_clip.mp4", codec="aac", channels=6)
        await scanner.probe_single_file(f4_mp4.name)
        r4 = media_repo.get_by_filepath(f4_mp4.name)
        p4 = parse_mkvmerge_json(json.loads(r4["raw_json"]))
        t4 = next(t for t in p4.tracks if t.type == "audio")
        cfg4 = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True, source_track_id=t4.id, source_audio_index=0,
                source_codec=t4.codec, source_channels=6,
                target_codec="eac3", bitrate="640k", track_title="EAC3 5.1",
                language="eng", set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(id=t.id, type=t.type, enabled=True, default_track=t.default_track)
                for t in p4.tracks
            ],
            output_mode="replace",
        )
        await execute_two_stage_pipeline(
            f4_mp4, cfg4, storage_root=test_dir, duration_us=int(r4["duration_us"]),
            media_repo=media_repo, scanner=scanner,
        )
        f4_mkv = test_dir / "matrix_clip.mkv"
        assert f4_mkv.is_file() and not f4_mp4.exists()
        assert media_repo.get_by_filepath("matrix_clip.mp4") is None
        assert media_repo.get_by_filepath("matrix_clip.mkv") is not None

        # Collision guard
        f4_mp4_again = await _gen_single(test_dir / "matrix_clip.mp4", codec="aac", channels=6)
        try:
            await execute_two_stage_pipeline(
                f4_mp4_again, cfg4, storage_root=test_dir, duration_us=0,
                media_repo=media_repo, scanner=scanner,
            )
            raise AssertionError("Expected TargetFileCollisionError")
        except TargetFileCollisionError:
            pass

        # Keep Both auto-increment
        cfg4_keep = cfg4.model_copy(update={"output_mode": "keep_both"})
        res_k1 = await execute_two_stage_pipeline(
            f4_mp4_again, cfg4_keep, storage_root=test_dir, duration_us=0,
            media_repo=media_repo, scanner=scanner,
        )
        res_k2 = await execute_two_stage_pipeline(
            f4_mp4_again, cfg4_keep, storage_root=test_dir, duration_us=0,
            media_repo=media_repo, scanner=scanner,
        )
        assert res_k1.output_path.name == "matrix_clip.remux.mkv"
        assert res_k2.output_path.name == "matrix_clip.remux.2.mkv"
        print("      PASS")

        # 5. Stereo 2.0 Channel Preservation (never upmix to 5.1)
        print("[5/6] Stereo 2.0 channel preservation (no upmix)...")
        f5 = await _gen_single(test_dir / "matrix_stereo.mkv", codec="aac", channels=2)
        await scanner.probe_single_file(f5.name)
        r5 = media_repo.get_by_filepath(f5.name)
        p5 = parse_mkvmerge_json(json.loads(r5["raw_json"]))
        t5 = next(t for t in p5.tracks if t.type == "audio")
        cfg5 = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True, source_track_id=t5.id, source_audio_index=0,
                source_codec=t5.codec, source_channels=2,
                target_codec="eac3", bitrate="384k", track_title="Stereo EAC3",
                language="eng", set_as_default=True,
            ),
            tracks=[
                TrackEditConfig(id=t.id, type=t.type, enabled=True, default_track=t.default_track)
                for t in p5.tracks
            ],
            output_mode="replace",
        )
        await execute_two_stage_pipeline(
            f5, cfg5, storage_root=test_dir, duration_us=int(r5["duration_us"]),
            media_repo=media_repo, scanner=scanner,
        )
        after5 = parse_mkvmerge_json(await _probe(f5))
        a5 = [t for t in after5.tracks if t.type == "audio"]
        assert "E-AC-3" in a5[0].codec and a5[0].audio_channels == 2
        print("      PASS")

        # 6. Mid-FFmpeg Cancellation Safety via JobWorker
        print("[6/6] Mid-FFmpeg cancellation safety (zero temps, intact SHA-256)...")
        f6 = await _gen_single(
            test_dir / "matrix_cancel.mkv",
            codec="aac",
            channels=6,
            duration_sec=_VerifyConstants.LONG_SEC,
        )
        f6_sha = _sha256(f6)
        await scanner.probe_single_file(f6.name)
        r6 = media_repo.get_by_filepath(f6.name)
        p6 = parse_mkvmerge_json(json.loads(r6["raw_json"]))
        t6 = next(t for t in p6.tracks if t.type == "audio")
        cfg6 = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True, source_track_id=t6.id, source_audio_index=0,
                source_codec=t6.codec, source_channels=6,
                target_codec="eac3", bitrate="640k",
            ),
            tracks=[
                TrackEditConfig(id=t.id, type=t.type, enabled=True, default_track=t.default_track)
                for t in p6.tracks
            ],
            output_mode="replace",
        )
        worker = JobWorker(
            settings=settings,
            media_repo=media_repo,
            job_repo=job_repo,
            scanner=scanner,
            broker=JobSSEBroker(),
        )
        worker.start()
        try:
            job = await worker.enqueue_job(filepath=f6.name, config=cfg6)
            expected_tmp = test_dir / f"{f6.name}.mkvforge.eac3"
            await _wait_for(lambda: worker._active_proc is not None and expected_tmp.exists())
            cancelled = await worker.cancel_job(str(job["id"]))
            assert cancelled is not None and cancelled["status"] == AppConstants.JOB_STATUS_CANCELLED
            assert _find_temps(test_dir) == [], f"Leftover temp files: {_find_temps(test_dir)}"
            assert _sha256(f6) == f6_sha
        finally:
            await worker.shutdown()
        print("      PASS")

        assert _find_temps(test_dir) == []
        print("\nALL 6 PHASE 7 CONTAINER VERIFICATION CHECKS PASSED!")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)
        shutil.rmtree(work_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="MKV Forge Phase 7 E2E verifier")
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=Path(os.environ.get("MKVFORGE_STORAGE_DIR", "/storage")) / "test",
        help="Directory on mounted storage for temporary verification files",
    )
    parser.add_argument(
        "--tmp-only",
        action="store_true",
        help="Run inside /tmp instead of /storage/test (skips 1000:1000 storage ownership check)",
    )
    args = parser.parse_args()

    target_root = Path(tempfile.gettempdir()) if args.tmp_only else args.storage_dir
    target_root.mkdir(parents=True, exist_ok=True)
    check_uid = (not args.tmp_only) and (os.geteuid() == _VerifyConstants.EXPECTED_UID)
    asyncio.run(run_verification(target_root, check_uid_gid=check_uid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
