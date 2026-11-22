"""Unit tests for semantic release version synchronization and surround track auto-selection."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.config import AppConstants, resolve_app_version
from scripts.release_version import (
    ReleaseVersionConstants,
    main as release_version_main,
    normalize_release_tag,
    read_current_version,
    sync_project_version,
)


class ReleaseTestConstants:
    """Centralized constants for release version synchronization tests."""

    VALID_TAGS: tuple[tuple[str, str], ...] = (
        ("v0.1.0", "0.1.0"),
        ("0.1.0", "0.1.0"),
        ("v1.0.0", "1.0.0"),
        ("1.0.1", "1.0.1"),
        ("v2.14.3", "2.14.3"),
        ("2.14.3", "2.14.3"),
        ("v1.0.0-beta.1", "1.0.0-beta.1"),
        ("1.0.0-beta.1", "1.0.0-beta.1"),
        ("  v0.5.2  ", "0.5.2"),
        ("  0.5.2  ", "0.5.2"),
    )

    INVALID_TAGS: tuple[str, ...] = (
        "",
        "   ",
        "v1.2",
        "1.2",
        "v1.2.3.4",
        "1.2.3.4",
        "v01.2.3",
        "01.2.3",
        "version1.0.0",
        "vabc.def.ghi",
    )

    SAMPLE_TAG_ONE: str = "v1.4.2"
    SAMPLE_VERSION_ONE: str = "1.4.2"
    SAMPLE_TAG_TWO: str = "v2.0.0-rc.1"
    SAMPLE_VERSION_TWO: str = "2.0.0-rc.1"
    CUSTOM_ENV_VERSION: str = "1.2.3-nightly.7ad92b8"


def _copy_version_targets_to_tmp(tmp_path: Path) -> Path:
    """Create a sandboxed replica of the repository's version-bearing files."""
    repo_root = ReleaseVersionConstants.REPO_ROOT
    for rel_path in (
        ReleaseVersionConstants.CONFIG_REL_PATH,
        ReleaseVersionConstants.FRONTEND_CONSTANTS_REL_PATH,
        ReleaseVersionConstants.DOCKERFILE_REL_PATH,
    ):
        src = repo_root / rel_path
        dst = tmp_path / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tmp_path


class TestReleaseVersionSync:
    """Verify tag validation, file synchronization, CLI entrypoint, and failure modes."""

    @pytest.mark.parametrize(("raw_tag", "expected_version"), ReleaseTestConstants.VALID_TAGS)
    def test_normalize_release_tag_accepts_valid_semver_tags(
        self, raw_tag: str, expected_version: str
    ) -> None:
        normalized_tag, version = normalize_release_tag(raw_tag)
        assert normalized_tag == f"v{expected_version}"
        assert version == expected_version

    @pytest.mark.parametrize("bad_tag", ReleaseTestConstants.INVALID_TAGS)
    def test_normalize_release_tag_rejects_malformed_tags(self, bad_tag: str) -> None:
        with pytest.raises(ValueError, match="Release tag|Invalid release tag"):
            normalize_release_tag(bad_tag)

    def test_sync_project_version_updates_all_target_files_idempotently(
        self, tmp_path: Path
    ) -> None:
        sandbox_root = _copy_version_targets_to_tmp(tmp_path)

        res1 = sync_project_version(
            ReleaseTestConstants.SAMPLE_TAG_ONE, repo_root=sandbox_root
        )
        assert res1.version == ReleaseTestConstants.SAMPLE_VERSION_ONE
        assert read_current_version(sandbox_root) == ReleaseTestConstants.SAMPLE_VERSION_ONE

        frontend_text = (
            sandbox_root / ReleaseVersionConstants.FRONTEND_CONSTANTS_REL_PATH
        ).read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)
        dockerfile_text = (
            sandbox_root / ReleaseVersionConstants.DOCKERFILE_REL_PATH
        ).read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)

        assert f'VERSION: "{ReleaseTestConstants.SAMPLE_VERSION_ONE}"' in frontend_text
        assert (
            dockerfile_text.count(f"ARG APP_VERSION={ReleaseTestConstants.SAMPLE_VERSION_ONE}")
            == ReleaseVersionConstants.EXPECTED_DOCKERFILE_MATCHES
        )

        # Second sync to a pre-release tag succeeds cleanly
        res2 = sync_project_version(
            ReleaseTestConstants.SAMPLE_TAG_TWO, repo_root=sandbox_root
        )
        assert res2.version == ReleaseTestConstants.SAMPLE_VERSION_TWO
        assert read_current_version(sandbox_root) == ReleaseTestConstants.SAMPLE_VERSION_TWO

    def test_sync_project_version_fails_safely_on_missing_file_or_corrupt_pattern(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            read_current_version(tmp_path)

        with pytest.raises(FileNotFoundError):
            sync_project_version(ReleaseTestConstants.SAMPLE_TAG_ONE, repo_root=tmp_path)

        sandbox_root = _copy_version_targets_to_tmp(tmp_path)
        corrupt_config = sandbox_root / ReleaseVersionConstants.CONFIG_REL_PATH
        corrupt_config.write_text("# corrupted file without version\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Could not locate DEFAULT_APP_VERSION"):
            read_current_version(sandbox_root)

        with pytest.raises(ValueError, match="Expected 1 version match"):
            sync_project_version(
                ReleaseTestConstants.SAMPLE_TAG_ONE, repo_root=sandbox_root
            )

    def test_cli_main_current_and_invalid_tag_exit_codes(self) -> None:
        assert release_version_main(["--current"]) == 0
        assert release_version_main(["--normalize", "1.2.3"]) == 0
        assert release_version_main(["--normalize", "invalid-tag"]) == 1
        assert release_version_main(["--tag", "invalid-tag"]) == 1

    def test_release_script_no_push_resume_and_abort_on_test_failure(
        self, tmp_path: Path
    ) -> None:
        """Verify release.sh supports --no-push resume, blocks duplicate remote tags, and aborts cleanly on failing tests."""
        origin_dir = tmp_path / "origin.git"
        work_dir = tmp_path / "repo"
        work_dir.mkdir()

        def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )

        _git(tmp_path, "init", "--bare", str(origin_dir))
        _git(work_dir, "init", "-b", "main")
        _git(work_dir, "config", "user.email", "test@example.com")
        _git(work_dir, "config", "user.name", "Test User")
        _git(work_dir, "remote", "add", "origin", str(origin_dir))

        # Copy version-bearing files + release scripts into isolated repo
        _copy_version_targets_to_tmp(work_dir)
        scripts_dir = work_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        for name in ("release.sh", "release_version.py"):
            src = ReleaseVersionConstants.REPO_ROOT / "scripts" / name
            dst = scripts_dir / name
            shutil.copy2(src, dst)

        # Create a minimal unit test in isolated repo
        unit_dir = work_dir / "tests" / "unit"
        unit_dir.mkdir(parents=True, exist_ok=True)
        sample_test = unit_dir / "test_smoke.py"
        sample_test.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

        _git(work_dir, "add", "-A")
        _git(work_dir, "commit", "-m", "Initial commit")
        _git(work_dir, "push", "-u", "origin", "main")

        # 1. Failing unit test aborts BEFORE modifying version files
        sample_test.write_text("def test_fail():\n    assert False\n", encoding="utf-8")
        before_version = read_current_version(work_dir)
        failed_run = subprocess.run(
            ["bash", "scripts/release.sh", "-m", "Should abort", "-t", "9.9.9", "--no-push"],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        assert failed_run.returncode != 0
        assert "Unit tests failed" in failed_run.stderr
        assert read_current_version(work_dir) == before_version

        # Restore passing test and commit
        sample_test.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _git(work_dir, "add", "-A")
        _git(work_dir, "commit", "-m", "Fix test")

        # 2. --no-push commits & creates local tag v9.9.9 without pushing to origin
        no_push_run = subprocess.run(
            ["bash", "scripts/release.sh", "-m", "Release 9.9.9", "-t", "9.9.9", "--no-push"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Creating annotated tag v9.9.9" in no_push_run.stdout
        assert read_current_version(work_dir) == "9.9.9"
        remote_tags_before = _git(work_dir, "ls-remote", "--tags", "origin").stdout
        assert "refs/tags/v9.9.9" not in remote_tags_before

        # 3. Re-running without --no-push smoothly resumes publishing the unpushed local tag
        resume_run = subprocess.run(
            ["bash", "scripts/release.sh", "-m", "Release 9.9.9", "-t", "9.9.9", "--skip-tests"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "resuming publish" in resume_run.stdout
        remote_tags_after = _git(work_dir, "ls-remote", "--tags", "origin").stdout
        assert "refs/tags/v9.9.9" in remote_tags_after

        # 4. Re-running once tag exists on origin fails safely
        dup_run = subprocess.run(
            ["bash", "scripts/release.sh", "-m", "Release 9.9.9", "-t", "9.9.9", "--skip-tests"],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        assert dup_run.returncode != 0
        assert "already exists on origin" in dup_run.stderr

        # 5. Malformed tag fails immediately without mutating version files
        bad_tag_run = subprocess.run(
            ["bash", "scripts/release.sh", "-m", "Bad tag", "-t", "not-a-semver", "--skip-tests"],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        assert bad_tag_run.returncode != 0
        assert read_current_version(work_dir) == "9.9.9"

    def test_resolve_app_version_honors_env_override_and_falls_back_on_blank(
        self,
    ) -> None:
        assert resolve_app_version({}) == AppConstants.DEFAULT_APP_VERSION
        assert (
            resolve_app_version({AppConstants.ENV_APP_VERSION: "   "})
            == AppConstants.DEFAULT_APP_VERSION
        )
        assert (
            resolve_app_version(
                {AppConstants.ENV_APP_VERSION: f"  {ReleaseTestConstants.CUSTOM_ENV_VERSION}  "}
            )
            == ReleaseTestConstants.CUSTOM_ENV_VERSION
        )


class TestEditorSurroundTrackAutoSelection:
    """Verify editor.js prefers 5.1/7.1 surround tracks (e.g. DTS 5.1) over 2.0 stereo tracks."""

    def test_prefers_dts_51_over_stereo_aac_when_building_add_audio_options(self) -> None:
        repo_root = ReleaseVersionConstants.REPO_ROOT
        node_script = f"""
        global.window = global;
        require({json.dumps(str(repo_root / 'app/static/js/constants.js'))});
        require({json.dumps(str(repo_root / 'app/static/js/editor.js'))});
        const Ed = window.MKVForgeEditor;

        const fileDetail = {{
          id: 99,
          filepath: "movies/MultiAudioDts.mkv",
          filename: "MultiAudioDts.mkv",
          folder: "/movies",
          container_title: "",
          has_global_tags: false,
          has_metadata: false,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "eng", track_name: "", default_track: true, forced_track: false, pixel_dimensions: "1920x1080" }},
            {{ id: 1, type: "audio", codec: "AAC", language: "hin", track_name: "Stereo Dub", default_track: true, forced_track: false, audio_channels: 2, audio_index: 0 }},
            {{ id: 2, type: "audio", codec: "DTS", language: "eng", track_name: "DTS 5.1 Main", default_track: false, forced_track: false, audio_channels: 6, audio_index: 1 }},
          ],
        }};

        const state = Ed.createEditorState(fileDetail);
        const options = Ed.getAvailableAddAudioOptions(state);
        const eac3Opt = options.find(o => o.codec === "eac3");
        const ac3Opt = options.find(o => o.codec === "ac3");
        console.log(JSON.stringify({{
          eac3PreferredId: eac3Opt.preferredSourceTrackId,
          eac3Label: eac3Opt.label,
          ac3PreferredId: ac3Opt.preferredSourceTrackId,
          ac3Label: ac3Opt.label,
          isStereoSurround: Ed.isSurroundTrack(state.tracks[1]),
          isDtsSurround: Ed.isSurroundTrack(state.tracks[2]),
        }}));
        """
        res = subprocess.run(
            ["node", "-e", node_script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(res.stdout.strip())
        assert payload["eac3PreferredId"] == 2
        assert payload["eac3Label"] == "EAC3 5.1"
        assert payload["ac3PreferredId"] == 2
        assert payload["ac3Label"] == "AC3 5.1"
        assert payload["isStereoSurround"] is False
        assert payload["isDtsSurround"] is True
