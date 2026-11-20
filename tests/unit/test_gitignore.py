"""Unit tests verifying .gitignore rules block garbage files while preserving tracked assets."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


class GitIgnoreTestConstants:
    """Centralized constants for .gitignore verification tests."""

    REPO_ROOT: Path = Path(__file__).resolve().parents[2]
    GITIGNORE_FILENAME: str = ".gitignore"
    GIT_BINARY: str = "git"
    CHECK_IGNORE_SUBCOMMAND: str = "check-ignore"
    LS_FILES_SUBCOMMAND: str = "ls-files"
    NO_INDEX_FLAG: str = "--no-index"
    EXIT_CODE_IGNORED: int = 0
    EXIT_CODE_NOT_IGNORED: int = 1
    MIN_EXPECTED_RULE_COUNT: int = 25
    COMMENT_PREFIX: str = "#"
    FORBIDDEN_TRACKED_TOKENS: tuple[str, ...] = (
        ".DS_Store",
        "__pycache__",
        ".pyc",
        ".mkvforge.tmp",
    )

    IGNORED_GARBAGE_PATHS: tuple[str, ...] = (
        ".DS_Store",
        "app/.DS_Store",
        "._Movie.mkv",
        "Thumbs.db",
        "app/__pycache__/main.cpython-314.pyc",
        "tests/unit/__pycache__/test_config.cpython-314.pyc",
        "app/services/scanner.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".venv/bin/python",
        "venv/lib/site.py",
        ".coverage",
        "htmlcov/index.html",
        ".mypy_cache/3.11/app.meta.json",
        ".ruff_cache/0.1.0/cache",
        "data/mkvforge.db",
        "data/mkvforge.db-wal",
        "data/mkvforge.db-shm",
        "data/uploads/sub.srt",
        "storage/Movies/Sample.mkv",
        "Movie.mkv",
        "Movie.mp4",
        "Movie.mkv.mkvforge.tmp",
        "Movie.mkv.mkvforge.tmp.eac3",
        "track.eac3",
        "subtitle.srt",
        ".tools/tailwindcss",
        "node_modules/pkg/index.js",
        ".env",
        ".env.local",
        "docker-compose.override.yml",
        "app.log",
        "logs/server.log",
        ".vscode/settings.json",
        ".idea/workspace.xml",
        "swapfile.swp",
    )

    TRACKED_ESSENTIAL_PATHS: tuple[str, ...] = (
        "Dockerfile",
        "docker-compose.yml",
        "README.md",
        "LICENSE",
        "requirements.txt",
        "requirements-dev.txt",
        "pytest.ini",
        "tailwind.config.js",
        "entrypoint.sh",
        ".env.example",
        "frontend/input.css",
        "scripts/build_css.sh",
        "scripts/verify_container_e2e.py",
        "app/main.py",
        "app/config.py",
        "app/static/index.html",
        "app/static/css/app.css",
        "app/static/js/app.js",
        "app/static/js/alpine.min.js",
        "app/static/img/app-icon.svg",
        "app/static/img/app-icon.png",
        "tests/conftest.py",
        "tests/unit/test_gitignore.py",
        "tests/unit/test_release_version.py",
        ".dockerignore",
        ".github/workflows/docker-publish.yml",
        "scripts/release.sh",
        "scripts/release_version.py",
    )

    README_FILENAME: str = "README.md"
    DOCKERIGNORE_FILENAME: str = ".dockerignore"
    WORKFLOW_REL_PATH: str = ".github/workflows/docker-publish.yml"
    REQUIRED_README_SECTIONS: tuple[str, ...] = (
        "## Quick Start with Docker",
        "### Image Release Channels",
        "ghcr.io/manswis/mkv-forge:latest",
        "ghcr.io/manswis/mkv-forge:nightly",
        "ghcr.io/manswis/mkv-forge:beta",
        "https://github.com/manswis/Mkv-Forge.git",
        "## Configuration (Environment Variables)",
        "### Choosing the Right `MKVFORGE_WATCH_MODE`",
        "|**`watch`**",
        "|**`poll`**",
        "|**`off`**",
        "MKVFORGE_STORAGE_DIR",
        "MKVFORGE_DATA_DIR",
    )
    REQUIRED_WORKFLOW_TOKENS: tuple[str, ...] = (
        "ghcr.io/manswis/mkv-forge",
        "manswis/mkv-forge",
        "type=raw,value=nightly",
        "type=raw,value=beta",
        "type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}",
        "platforms: linux/amd64,linux/arm64",
    )


def _run_git_check_ignore(rel_path: str, repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Run `git check-ignore --no-index` against `rel_path` inside `repo_root`."""
    if not rel_path or not rel_path.strip():
        raise ValueError("Path to check must not be empty")
    return subprocess.run(
        [
            GitIgnoreTestConstants.GIT_BINARY,
            GitIgnoreTestConstants.CHECK_IGNORE_SUBCOMMAND,
            GitIgnoreTestConstants.NO_INDEX_FLAG,
            rel_path,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


class TestGitIgnoreConfiguration:
    """Positive and negative test suite for repository .gitignore rules."""

    def test_gitignore_file_exists_and_contains_active_rules(self) -> None:
        gitignore_path = (
            GitIgnoreTestConstants.REPO_ROOT / GitIgnoreTestConstants.GITIGNORE_FILENAME
        )
        assert gitignore_path.is_file(), ".gitignore file must exist at repository root"

        lines = [
            line.strip()
            for line in gitignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith(GitIgnoreTestConstants.COMMENT_PREFIX)
        ]
        assert len(lines) >= GitIgnoreTestConstants.MIN_EXPECTED_RULE_COUNT

    @pytest.mark.parametrize("garbage_path", GitIgnoreTestConstants.IGNORED_GARBAGE_PATHS)
    def test_ignores_all_garbage_files_and_folders(self, garbage_path: str) -> None:
        result = _run_git_check_ignore(garbage_path, GitIgnoreTestConstants.REPO_ROOT)
        assert result.returncode == GitIgnoreTestConstants.EXIT_CODE_IGNORED, (
            f"Expected garbage path {garbage_path!r} to be ignored by .gitignore"
        )

    @pytest.mark.parametrize("essential_path", GitIgnoreTestConstants.TRACKED_ESSENTIAL_PATHS)
    def test_preserves_essential_project_and_static_files(self, essential_path: str) -> None:
        result = _run_git_check_ignore(essential_path, GitIgnoreTestConstants.REPO_ROOT)
        assert result.returncode == GitIgnoreTestConstants.EXIT_CODE_NOT_IGNORED, (
            f"Essential path {essential_path!r} must NOT be ignored by .gitignore"
        )

    @pytest.mark.parametrize("invalid_path", ["", "   "])
    def test_helper_rejects_empty_or_whitespace_paths(self, invalid_path: str) -> None:
        with pytest.raises(ValueError, match="Path to check must not be empty"):
            _run_git_check_ignore(invalid_path, GitIgnoreTestConstants.REPO_ROOT)

    def test_no_garbage_files_remain_tracked_in_git_index(self) -> None:
        result = subprocess.run(
            [GitIgnoreTestConstants.GIT_BINARY, GitIgnoreTestConstants.LS_FILES_SUBCOMMAND],
            cwd=GitIgnoreTestConstants.REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked_files = result.stdout.splitlines()
        leaked = [
            path
            for path in tracked_files
            if any(token in path for token in GitIgnoreTestConstants.FORBIDDEN_TRACKED_TOKENS)
        ]
        assert leaked == [], f"Garbage files are still tracked in git index: {leaked}"

    def test_readme_documents_watch_modes_and_optional_volume_env_vars(self) -> None:
        readme_path = GitIgnoreTestConstants.REPO_ROOT / GitIgnoreTestConstants.README_FILENAME
        assert readme_path.is_file()
        normalized = readme_path.read_text(encoding="utf-8").replace("| **`", "|**`")
        for token in GitIgnoreTestConstants.REQUIRED_README_SECTIONS:
            assert token in normalized, f"Missing expected documentation section/token: {token!r}"

    def test_docker_publish_workflow_and_dockerignore_are_configured(self) -> None:
        dockerignore_path = (
            GitIgnoreTestConstants.REPO_ROOT / GitIgnoreTestConstants.DOCKERIGNORE_FILENAME
        )
        workflow_path = (
            GitIgnoreTestConstants.REPO_ROOT / GitIgnoreTestConstants.WORKFLOW_REL_PATH
        )
        assert dockerignore_path.is_file(), ".dockerignore must exist at repository root"
        assert workflow_path.is_file(), "Docker publish workflow must exist"

        workflow_text = workflow_path.read_text(encoding="utf-8")
        for token in GitIgnoreTestConstants.REQUIRED_WORKFLOW_TOKENS:
            assert token in workflow_text, f"Missing expected workflow configuration: {token!r}"



