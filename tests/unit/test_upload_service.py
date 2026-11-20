"""Unit tests for Phase 10 UploadService (allowlists, streaming limits, cleanup & orphan sweep)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator

import pytest

from app.config import Settings
from app.services.upload_service import (
    UploadNotFoundError,
    UploadService,
    UploadTooLargeError,
    UploadValidationError,
    validate_external_filename,
    validate_storage_external_track,
)


async def _chunk_iter(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class TestExternalFilenameAndStorageValidation:
    """Verify extension allowlists and storage jail validation."""

    @pytest.mark.parametrize(
        "kind,filename,expected_ext",
        [
            ("subtitles", "English.srt", ".srt"),
            ("subtitles", "Signs.ASS", ".ass"),
            ("subtitles", "Legacy.ssa", ".ssa"),
            ("subtitles", "Web.vtt", ".vtt"),
            ("subtitles", "BluRay.sup", ".sup"),
            ("audio", "Hindi.ac3", ".ac3"),
            ("audio", "Atmos.eac3", ".eac3"),
            ("audio", "Track.aac", ".aac"),
            ("audio", "Wrapped.mka", ".mka"),
            ("audio", "Master.dts", ".dts"),
            ("audio", "Lossless.flac", ".flac"),
            ("audio", "Stereo.mp3", ".mp3"),
            ("audio", "Modern.opus", ".opus"),
            ("audio", "Apple.m4a", ".m4a"),
        ],
    )
    def test_accepts_allowlisted_extensions(
        self, kind: str, filename: str, expected_ext: str
    ) -> None:
        clean, ext = validate_external_filename(filename, kind)
        assert clean == filename
        assert ext == expected_ext

    @pytest.mark.parametrize(
        "kind,filename",
        [
            ("subtitles", ""),
            ("subtitles", "../escape.srt"),
            ("subtitles", "sub/nested.srt"),
            ("subtitles", ".hidden.srt"),
            ("subtitles", "Movie.mkv"),
            ("subtitles", "Track.ac3"),
            ("audio", "Subtitles.srt"),
            ("audio", "malware.exe"),
            ("video", "Clip.mkv"),
        ],
    )
    def test_rejects_disallowed_names_and_extensions(
        self, kind: str, filename: str
    ) -> None:
        with pytest.raises(UploadValidationError):
            validate_external_filename(filename, kind)

    def test_storage_external_track_enforces_jail_and_existence(
        self, test_settings: Settings
    ) -> None:
        sub_path = test_settings.storage_dir / "movies" / "English.srt"
        sub_path.parent.mkdir(parents=True, exist_ok=True)
        sub_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

        resolved = validate_storage_external_track(
            "/movies/English.srt", "subtitles", test_settings.storage_dir
        )
        assert resolved == sub_path.resolve()

        with pytest.raises(UploadValidationError):
            validate_storage_external_track(
                "../outside.srt", "subtitles", test_settings.storage_dir
            )

        with pytest.raises(UploadNotFoundError):
            validate_storage_external_track(
                "movies/Missing.srt", "subtitles", test_settings.storage_dir
            )


class TestUploadServiceStagingAndCleanup:
    """Verify streaming uploads, 413 limits, traversal rejection, and orphan sweeps."""

    async def test_stage_resolve_and_delete_upload(self, test_settings: Settings) -> None:
        service = UploadService(test_settings)
        info = await service.stage_upload_stream(
            kind="subtitles",
            filename="Hindi.srt",
            chunks=_chunk_iter(b"1\n00:00:01,000 --> 00:00:02,000\n", b"Namaste\n"),
        )
        assert info.kind == "subtitles"
        assert info.original_filename == "Hindi.srt"
        assert info.staged_path.is_file()
        assert info.staged_path.parent == service.uploads_dir.resolve()

        resolved = service.resolve_upload_path(info.upload_id, kind="subtitles")
        assert resolved == info.staged_path

        # Mismatched kind is rejected
        with pytest.raises(UploadValidationError):
            service.resolve_upload_path(info.upload_id, kind="audio")

        # Delete removes staged file
        assert service.delete_upload(info.upload_id) is True
        assert not info.staged_path.exists()

        with pytest.raises(UploadNotFoundError):
            service.resolve_upload_path(info.upload_id)

    async def test_rejects_empty_upload_and_leaves_zero_files(
        self, test_settings: Settings
    ) -> None:
        service = UploadService(test_settings)
        with pytest.raises(UploadValidationError, match="empty"):
            await service.stage_upload_stream(
                kind="subtitles",
                filename="Empty.srt",
                chunks=_chunk_iter(b""),
            )
        assert not list(service.uploads_dir.glob("*"))

    async def test_enforces_size_limit_upfront_and_mid_stream(
        self, test_settings: Settings
    ) -> None:
        tight = replace(test_settings, max_sub_upload_mb=1)
        service = UploadService(tight)
        over_limit = service.max_bytes_for_kind("subtitles") + 1

        # 1. Upfront Content-Length rejection
        with pytest.raises(UploadTooLargeError, match="client_max_body_size"):
            await service.stage_upload_stream(
                kind="subtitles",
                filename="Huge.srt",
                chunks=_chunk_iter(b"abc"),
                content_length=over_limit,
            )

        # 2. Mid-stream overflow rejection (without Content-Length) cleans partial file
        chunk = b"a" * (512 * 1024)
        with pytest.raises(UploadTooLargeError):
            await service.stage_upload_stream(
                kind="subtitles",
                filename="Overflow.srt",
                chunks=_chunk_iter(chunk, chunk, b"extra-byte"),
                content_length=None,
            )
        assert not list(service.uploads_dir.glob("*"))

    async def test_rejects_traversal_upload_id_and_sweeps_orphans(
        self, test_settings: Settings
    ) -> None:
        service = UploadService(test_settings)
        with pytest.raises(UploadValidationError):
            service.resolve_upload_path("../db.sqlite")
        with pytest.raises(UploadValidationError):
            service.resolve_upload_path("not-a-valid-uuid.srt")

        kept = await service.stage_upload_stream(
            kind="subtitles", filename="Keep.srt", chunks=_chunk_iter(b"keep")
        )
        orphan = await service.stage_upload_stream(
            kind="audio", filename="Orphan.ac3", chunks=_chunk_iter(b"orphan")
        )

        removed = service.sweep_orphaned_uploads({kept.upload_id})
        assert removed == 1
        assert kept.staged_path.is_file()
        assert not orphan.staged_path.exists()
