"""Unit tests for app.utils.paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.paths import (
    PathSafetyError,
    build_temp_audio_path,
    build_temp_remux_path,
    ensure_mp4_replace_target_available,
    ensure_save_as_target_available,
    extract_folder,
    is_mkvforge_temp_file,
    is_supported_media_file,
    resolve_keep_both_path,
    resolve_mp4_replace_target,
    resolve_save_as_target,
    safe_resolve_path,
    to_relative_storage_path,
)


class TestSafeResolvePath:
    """Verify strict storage-root boundary enforcement."""

    def test_resolves_nested_and_unicode_relative_paths(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()
        resolved = safe_resolve_path("Movies/日本語/Café 2024.mkv", storage)

        assert resolved == (storage / "Movies/日本語/Café 2024.mkv").resolve(strict=False)
        assert resolved.is_relative_to(storage.resolve(strict=False))

    def test_accepts_absolute_path_inside_storage(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        target = storage / "shows" / "ep1.mkv"
        resolved = safe_resolve_path(target, storage)

        assert resolved == target.resolve(strict=False)

    @pytest.mark.parametrize(
        "malicious_path",
        [
            "../secret.mkv",
            "movies/../../etc/passwd",
            "/etc/passwd",
            "",
            "   ",
            "movies/bad\x00name.mkv",
            ".",
        ],
    )
    def test_blocks_traversal_empty_null_and_root_paths(
        self, tmp_path: Path, malicious_path: str
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()

        with pytest.raises(PathSafetyError):
            safe_resolve_path(malicious_path, storage)

    def test_blocks_symlink_escape_outside_storage(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        outside = tmp_path / "outside"
        storage.mkdir()
        outside.mkdir()
        secret = outside / "secret.mkv"
        secret.write_bytes(b"data")

        escape_link = storage / "escape.mkv"
        escape_link.symlink_to(secret)

        with pytest.raises(PathSafetyError):
            safe_resolve_path("escape.mkv", storage)

    def test_allow_root_flag_permits_storage_root(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()

        resolved = safe_resolve_path(".", storage, allow_root=True)
        assert resolved == storage.resolve(strict=False)


class TestRelativeAndFolderHelpers:
    """Verify POSIX relative path and display folder formatting."""

    def test_to_relative_storage_path_and_extract_folder(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage"
        nested = storage / "Movies" / "English" / "Dune.mkv"
        root_file = storage / "RootMovie.mkv"

        rel_nested = to_relative_storage_path(nested, storage)
        rel_root = to_relative_storage_path(root_file, storage)

        assert rel_nested == "Movies/English/Dune.mkv"
        assert extract_folder(rel_nested) == "/Movies/English"
        assert rel_root == "RootMovie.mkv"
        assert extract_folder(rel_root) == "/"


class TestMediaAndTempFilePredicates:
    """Verify media extension filtering and temp artifact detection."""

    @pytest.mark.parametrize(
        "filename",
        ["movie.mkv", "MOVIE.MKV", "clip.mp4", "CLIP.MP4", "show.s01e01.mkv"],
    )
    def test_accepts_supported_media_files(self, filename: str) -> None:
        assert is_supported_media_file(Path(filename)) is True

    @pytest.mark.parametrize(
        "filename",
        [
            "movie.mkv.mkvforge.tmp",
            "movie.mkv.mkvforge.eac3",
            "movie.mkv.MKVFORGE.TMP",
            "._movie.mkv",
            "subtitles.srt",
            "video.avi",
            "readme.txt",
        ],
    )
    def test_rejects_temp_hidden_and_unsupported_files(self, filename: str) -> None:
        assert is_supported_media_file(Path(filename)) is False

    def test_is_mkvforge_temp_file(self) -> None:
        assert is_mkvforge_temp_file(Path("movie.mkv.mkvforge.tmp")) is True
        assert is_mkvforge_temp_file(Path("movie.mkv.mkvforge.ac3")) is True
        assert is_mkvforge_temp_file(Path("movie.mkv")) is False


class TestTempAndOutputNaming:
    """Verify atomic temp naming, mp4 replace collision guard, and Keep-Both increments."""

    def test_build_temp_remux_path_for_mkv_and_mp4(self, tmp_path: Path) -> None:
        mkv_src = tmp_path / "Dune.mkv"
        mp4_src = tmp_path / "Dune.mp4"

        assert build_temp_remux_path(mkv_src) == tmp_path / "Dune.mkv.mkvforge.tmp"
        assert build_temp_remux_path(mp4_src) == tmp_path / "Dune.mkv.mkvforge.tmp"

    def test_build_temp_audio_path(self, tmp_path: Path) -> None:
        src = tmp_path / "Dune.mkv"

        assert build_temp_audio_path(src, "eac3") == tmp_path / "Dune.mkv.mkvforge.eac3"
        assert build_temp_audio_path(src, ".AC3") == tmp_path / "Dune.mkv.mkvforge.ac3"

        with pytest.raises(ValueError):
            build_temp_audio_path(src, "../bad")
        with pytest.raises(ValueError):
            build_temp_audio_path(src, "  ")

    def test_mp4_replace_target_and_collision_guard(self, tmp_path: Path) -> None:
        mp4_src = tmp_path / "Film.mp4"
        mp4_src.write_bytes(b"mp4")

        assert resolve_mp4_replace_target(mp4_src) == tmp_path / "Film.mkv"
        assert ensure_mp4_replace_target_available(mp4_src) == tmp_path / "Film.mkv"

        # Existing <stem>.mkv must fail safely rather than overwrite
        (tmp_path / "Film.mkv").write_bytes(b"existing")
        with pytest.raises(FileExistsError, match="Film.mkv"):
            ensure_mp4_replace_target_available(mp4_src)

        with pytest.raises(ValueError):
            resolve_mp4_replace_target(tmp_path / "Film.mkv")

    def test_resolve_keep_both_path_auto_increments_on_collision(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "Movie.mp4"

        first = resolve_keep_both_path(src)
        assert first == tmp_path / "Movie.remux.mkv"
        first.write_bytes(b"1")

        second = resolve_keep_both_path(src)
        assert second == tmp_path / "Movie.remux.2.mkv"
        second.write_bytes(b"2")

        third = resolve_keep_both_path(src)
        assert third == tmp_path / "Movie.remux.3.mkv"

    def test_save_as_target_validation_and_collision_guard(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "storage"
        storage.mkdir()
        src = storage / "movies" / "Film.mkv"
        src.parent.mkdir()
        src.write_bytes(b"orig")

        # Valid destination in a new subfolder
        resolved = resolve_save_as_target(
            "exports/dolby/Film (EAC3).mkv", storage, source_path=src
        )
        assert resolved == (storage / "exports" / "dolby" / "Film (EAC3).mkv").resolve()

        # Invalid cases: empty, traversal, backslash, non-mkv, temp marker, AppleDouble, same as source
        for bad in (
            "",
            "   ",
            "../outside.mkv",
            "exports/../../outside.mkv",
            "exports\\Film.mkv",
            "exports/Film.mp4",
            "exports/Film.mkv.mkvforge.tmp",
            "exports/._Film.mkv",
            "movies/Film.mkv",
        ):
            with pytest.raises(PathSafetyError):
                resolve_save_as_target(bad, storage, source_path=src)

        # Collision with an existing destination raises FileExistsError
        existing = storage / "exports" / "Taken.mkv"
        existing.parent.mkdir()
        existing.write_bytes(b"already-here")
        with pytest.raises(FileExistsError, match="Taken.mkv"):
            ensure_save_as_target_available("exports/Taken.mkv", storage, source_path=src)
