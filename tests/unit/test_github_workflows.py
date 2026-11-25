"""Unit tests for GHCR publishing, cleanup workflows, and CI Docker build change detection."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_build_changes  # noqa: E402


class WorkflowTestConstants:
    """Centralized paths and expectations for GitHub Actions workflow tests."""

    DOCKERFILE_PATH: Path = REPO_ROOT / "Dockerfile"
    DOCKER_PUBLISH_WORKFLOW: Path = REPO_ROOT / ".github/workflows/docker-publish.yml"
    GHCR_CLEANUP_WORKFLOW: Path = REPO_ROOT / ".github/workflows/ghcr-cleanup.yml"
    UTF8_ENCODING: str = "utf-8"
    EXPECTED_PYTHON_VERSION: str = "3.13"
    EXPECTED_APP_VERSION_ARG_COUNT: int = 2
    ZERO_SHA: str = "0" * 40
    MISSING_SHA: str = "deadbeef" * 5
    GIT_USER_NAME: str = "Test Bot"
    GIT_USER_EMAIL: str = "test@example.com"


def _read_workflow(path: Path) -> str:
    """Read workflow YAML content and assert the file exists and has no tab indentation."""
    assert path.is_file(), f"Missing workflow file: {path}"
    content = path.read_text(encoding=WorkflowTestConstants.UTF8_ENCODING)
    assert "\t" not in content, f"YAML workflow must not contain tab characters: {path}"
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        pass
    else:
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), f"Workflow must parse to a YAML mapping: {path}"
    return content


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside `repo` and return stripped stdout."""
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_git_repo(tmp_path: Path) -> Path:
    """Initialize a minimal git repo with one initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", WorkflowTestConstants.GIT_USER_NAME)
    _git(repo, "config", "user.email", WorkflowTestConstants.GIT_USER_EMAIL)
    (repo / "app.py").write_text("print('init')\n", encoding=WorkflowTestConstants.UTF8_ENCODING)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    return repo


# ---------------------------------------------------------------------------
# 1. docker-publish.yml Policy & Guardrail Tests
# ---------------------------------------------------------------------------


def test_docker_publish_preserves_beta_nightly_and_semver_without_sha_bloat() -> None:
    """Publish workflow keeps :nightly, :beta, and SemVer tags while removing :sha-* tags."""
    content = _read_workflow(WorkflowTestConstants.DOCKER_PUBLISH_WORKFLOW)

    assert "type=raw,value=nightly" in content
    assert "type=raw,value=beta" in content
    assert "type=semver,pattern={{version}}" in content
    assert "type=semver,pattern={{major}}.{{minor}}" in content
    assert "type=semver,pattern={{major}}" in content
    assert "type=raw,value=latest" in content

    # Per-commit sha-* tags permanently pin old main builds in GHCR and must remain absent
    assert "type=sha" not in content
    assert "prefix=sha-" not in content


def test_docker_publish_disables_provenance_and_sbom_attestations() -> None:
    """Disable BuildKit provenance and SBOM attestations to avoid extra untagged GHCR digests."""
    content = _read_workflow(WorkflowTestConstants.DOCKER_PUBLISH_WORKFLOW)

    assert re.search(r"^\s+provenance:\s*false\s*$", content, re.MULTILINE)
    assert re.search(r"^\s+sbom:\s*false\s*$", content, re.MULTILINE)


def test_docker_publish_avoids_invalid_push_tags_and_paths_ignore_combination() -> None:
    """GitHub Actions rejects combining push.tags and push.paths-ignore; gating uses test output."""
    content = _read_workflow(WorkflowTestConstants.DOCKER_PUBLISH_WORKFLOW)

    assert "paths-ignore:" not in content
    assert 'tags:\n      - "v*.*.*"' in content
    assert "fetch-depth: 0" in content
    assert "python3 scripts/check_build_changes.py" in content
    assert "should_build: ${{ steps.changes.outputs.should_build }}" in content
    assert "if: needs.test.outputs.should_build == 'true'" in content


def test_dockerfile_and_ci_use_trixie_python313_and_upstream_mkvtoolnix_repo() -> None:
    """Dockerfile builds on Debian Trixie + Python 3.13 with signed upstream MKVToolNix APT repo."""
    dockerfile = WorkflowTestConstants.DOCKERFILE_PATH.read_text(
        encoding=WorkflowTestConstants.UTF8_ENCODING
    )
    publish_wf = _read_workflow(WorkflowTestConstants.DOCKER_PUBLISH_WORKFLOW)

    assert "FROM debian:trixie-slim AS css-builder" in dockerfile
    assert f"FROM python:{WorkflowTestConstants.EXPECTED_PYTHON_VERSION}-slim-trixie AS runtime" in dockerfile
    assert "bookworm" not in dockerfile
    assert f'python-version: "{WorkflowTestConstants.EXPECTED_PYTHON_VERSION}"' in publish_wf

    # Signed official MKVToolNix Debian Trixie repository (amd64 + arm64)
    assert "https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg" in dockerfile
    assert "signed-by=/etc/apt/keyrings/gpg-pub-moritzbunkus.gpg" in dockerfile
    assert "https://mkvtoolnix.download/debian/ trixie main" in dockerfile
    assert "apt-get purge -y --auto-remove curl" in dockerfile

    # Build-time smoke verification runs AFTER pruning curl
    assert dockerfile.index("apt-get purge -y --auto-remove curl") < dockerfile.index(
        "mkvmerge --version"
    )
    assert "ffmpeg -version" in dockerfile

    # Release version sync requires exactly 2 ARG APP_VERSION= declarations
    app_version_args = re.findall(r"^ARG\s+APP_VERSION=", dockerfile, re.MULTILINE)
    assert len(app_version_args) == WorkflowTestConstants.EXPECTED_APP_VERSION_ARG_COUNT


# ---------------------------------------------------------------------------
# 2. ghcr-cleanup.yml Policy & Multi-Arch Safety Tests
# ---------------------------------------------------------------------------


def _extract_yaml_block_lines(content: str, key: str) -> list[str]:
    """Extract non-empty lines belonging to a YAML `key: |` block scalar by indentation."""
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip(" ")
        if stripped.startswith(f"{key}:") and stripped.rstrip().endswith("|"):
            key_indent = len(line) - len(stripped)
            block_lines: list[str] = []
            for follow in lines[idx + 1 :]:
                if not follow.strip():
                    continue
                follow_indent = len(follow) - len(follow.lstrip(" "))
                if follow_indent <= key_indent:
                    break
                block_lines.append(follow.strip())
            return block_lines
    raise AssertionError(f"YAML block scalar '{key}: |' not found")


def test_ghcr_cleanup_workflow_uses_multi_arch_safe_action_and_protects_live_tags() -> None:
    """Cleanup workflow prunes legacy sha-* tags and orphans while protecting latest/beta/nightly."""
    content = _read_workflow(WorkflowTestConstants.GHCR_CLEANUP_WORKFLOW)

    assert "uses: dataaxiom/ghcr-cleanup-action@v1" in content
    assert "packages: write" in content
    assert "contents: write" not in content
    assert "package: mkv-forge" in content
    assert "delete-untagged: true" in content
    assert "cancel-in-progress: false" in content

    delete_tags = _extract_yaml_block_lines(content, "delete-tags")
    assert delete_tags == ["sha-*"]

    # exclude-tags must be newline-separated (comma-separated would treat all tags as one literal)
    assert "latest,beta,nightly" not in content
    excluded_tags = set(_extract_yaml_block_lines(content, "exclude-tags"))
    assert excluded_tags == {"latest", "beta", "nightly"}


def test_ghcr_cleanup_workflow_supports_schedule_and_manual_dry_run() -> None:
    """Cleanup workflow runs weekly on schedule and supports safe dry-run manual dispatch."""
    content = _read_workflow(WorkflowTestConstants.GHCR_CLEANUP_WORKFLOW)

    assert "schedule:" in content
    assert 'cron: "0 3 * * 0"' in content
    assert "workflow_dispatch:" in content
    assert "dry_run:" in content
    assert "type: boolean" in content
    assert "default: false" in content
    assert "dry-run: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run }}" in content


# ---------------------------------------------------------------------------
# 3. scripts/check_build_changes.py Exhaustive Positive, Negative & Edge Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CHANGELOG.MD",
        "./README.md",
        "app/notes.md",
        "docs/architecture.md",
        "docs/images/ui-preview.png",
        "docs\\nested\\guide.txt",
        "DOCS/UPPERCASE.PNG",
    ],
)
def test_is_docs_only_path_positive(path: str) -> None:
    """Markdown files and files inside docs/ are recognized as docs-only."""
    assert check_build_changes.is_docs_only_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "app/main.py",
        "Dockerfile",
        "requirements.txt",
        "docs_extra/script.py",
        "README.md.bak",
        ".github/workflows/docker-publish.yml",
        "scripts/check_build_changes.py",
    ],
)
def test_is_docs_only_path_negative(path: str) -> None:
    """Application, Docker, workflow, and near-match paths are never classified as docs-only."""
    assert check_build_changes.is_docs_only_path(path) is False


def test_should_build_for_changed_files_matrix() -> None:
    """Empty diffs fail open; docs-only diffs skip; mixed or code diffs trigger build."""
    assert check_build_changes.should_build_for_changed_files([]) is True
    assert check_build_changes.should_build_for_changed_files(["", "   "]) is True
    assert check_build_changes.should_build_for_changed_files(["README.md", "docs/a.png"]) is False
    assert check_build_changes.should_build_for_changed_files(["README.md", "app/main.py"]) is True
    assert check_build_changes.should_build_for_changed_files(["Dockerfile"]) is True


def test_evaluate_should_build_with_real_git_repo_docs_only_and_code_pushes(tmp_path: Path) -> None:
    """Real git push diffs skip docs-only pushes and build code pushes."""
    repo = _init_git_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")

    # Docs-only commit
    (repo / "README.md").write_text("# Docs update\n", encoding=WorkflowTestConstants.UTF8_ENCODING)
    (repo / "docs").mkdir()
    (repo / "docs" / "screenshot.png").write_bytes(b"\x89PNG")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "docs only")

    assert (
        check_build_changes.evaluate_should_build(
            event_name="push",
            github_ref="refs/heads/main",
            push_before_sha=base_sha,
            repo_root=repo,
        )
        is False
    )

    # Release tags and workflow_dispatch must ALWAYS build even on a docs-only commit
    assert (
        check_build_changes.evaluate_should_build(
            event_name="push",
            github_ref="refs/tags/v1.0.7",
            push_before_sha=base_sha,
            repo_root=repo,
        )
        is True
    )
    assert (
        check_build_changes.evaluate_should_build(
            event_name="workflow_dispatch",
            github_ref="refs/heads/main",
            push_before_sha=base_sha,
            repo_root=repo,
        )
        is True
    )

    # Mixed commit (docs + code) must build
    before_code_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("# Docs + code\n", encoding=WorkflowTestConstants.UTF8_ENCODING)
    (repo / "app.py").write_text("print('updated')\n", encoding=WorkflowTestConstants.UTF8_ENCODING)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "code change")

    assert (
        check_build_changes.evaluate_should_build(
            event_name="push",
            github_ref="refs/heads/main",
            push_before_sha=before_code_sha,
            repo_root=repo,
        )
        is True
    )


def test_evaluate_should_build_pull_request_and_fail_open_edge_cases(tmp_path: Path) -> None:
    """PR diffs use merge-base; zero/missing SHAs and empty commits fail open safely."""
    repo = _init_git_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")

    # Docs-only PR
    (repo / "README.md").write_text("# PR docs\n", encoding=WorkflowTestConstants.UTF8_ENCODING)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pr docs")

    assert (
        check_build_changes.evaluate_should_build(
            event_name="pull_request",
            github_ref="refs/pull/42/merge",
            pr_base_sha=base_sha,
            repo_root=repo,
        )
        is False
    )

    # Fail-open on all-zero SHA, empty SHA, or missing/force-pushed SHA
    for invalid_sha in ("", WorkflowTestConstants.ZERO_SHA, WorkflowTestConstants.MISSING_SHA):
        assert (
            check_build_changes.evaluate_should_build(
                event_name="push",
                github_ref="refs/heads/main",
                push_before_sha=invalid_sha,
                repo_root=repo,
            )
            is True
        )

    # Fail-open on explicit empty commit
    before_empty = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "trigger rebuild")
    assert (
        check_build_changes.evaluate_should_build(
            event_name="push",
            github_ref="refs/heads/main",
            push_before_sha=before_empty,
            repo_root=repo,
        )
        is True
    )


def test_cli_main_writes_github_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI writes should_build=true to GITHUB_OUTPUT and prints stdout cleanly."""
    output_file = tmp_path / "github_output.txt"
    exit_code = check_build_changes.main(
        [
            "--event-name",
            "workflow_dispatch",
            "--github-ref",
            "refs/heads/main",
            "--github-output",
            str(output_file),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "true"
    assert output_file.read_text(encoding=WorkflowTestConstants.UTF8_ENCODING) == "should_build=true\n"
