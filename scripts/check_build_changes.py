"""Single-responsibility detector for Docker-build-relevant repository changes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


class BuildChangeConstants:
    """Centralized constants for CI Docker build gating."""

    ALWAYS_BUILD_EVENTS: frozenset[str] = frozenset({"workflow_dispatch"})
    PULL_REQUEST_EVENT: str = "pull_request"
    RELEASE_TAG_PREFIX: str = "refs/tags/v"
    DOCS_ONLY_EXTENSIONS: tuple[str, ...] = (".md",)
    DOCS_ONLY_DIR_PREFIXES: tuple[str, ...] = ("docs/",)
    ZERO_SHA_CHAR: str = "0"
    DEFAULT_HEAD_REF: str = "HEAD"
    OUTPUT_KEY_SHOULD_BUILD: str = "should_build"
    OUTPUT_TRUE: str = "true"
    OUTPUT_FALSE: str = "false"
    UTF8_ENCODING: str = "utf-8"
    GIT_BINARY: str = "git"
    GIT_SUCCESS_CODE: int = 0


def is_docs_only_path(raw_path: str) -> bool:
    """Return True iff `raw_path` is strictly a documentation file (`*.md` or `docs/**`)."""
    normalized = raw_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return False

    lowered = normalized.lower()
    if any(lowered.startswith(prefix) for prefix in BuildChangeConstants.DOCS_ONLY_DIR_PREFIXES):
        return True
    if any(lowered.endswith(ext) for ext in BuildChangeConstants.DOCS_ONLY_EXTENSIONS):
        return True
    return False


def should_build_for_changed_files(changed_files: Sequence[str]) -> bool:
    """Return True if Docker build should run for `changed_files`.

    Fails open (returns True) when `changed_files` is empty (e.g., an explicit
    empty commit meant to trigger a rebuild) or contains any non-documentation file.
    """
    meaningful_paths = [path.strip() for path in changed_files if path and path.strip()]
    if not meaningful_paths:
        return True
    return any(not is_docs_only_path(path) for path in meaningful_paths)


def _is_missing_or_zero_sha(sha: str) -> bool:
    """Return True when a commit SHA is empty or all-zeros (new branch/ref)."""
    cleaned = sha.strip()
    return not cleaned or all(ch == BuildChangeConstants.ZERO_SHA_CHAR for ch in cleaned)


def _commit_exists(sha: str, *, repo_root: Path | None = None) -> bool:
    """Verify `sha` resolves to a valid local git commit object."""
    result = subprocess.run(
        [BuildChangeConstants.GIT_BINARY, "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == BuildChangeConstants.GIT_SUCCESS_CODE


def resolve_changed_files(
    *,
    event_name: str,
    base_sha: str,
    head_ref: str = BuildChangeConstants.DEFAULT_HEAD_REF,
    repo_root: Path | None = None,
) -> list[str] | None:
    """Return changed file paths between `base_sha` and `head_ref`, or `None` to fail open."""
    if _is_missing_or_zero_sha(base_sha):
        return None
    cleaned_base = base_sha.strip()
    if not _commit_exists(cleaned_base, repo_root=repo_root):
        return None

    range_operator = "..." if event_name == BuildChangeConstants.PULL_REQUEST_EVENT else ".."
    diff_range = f"{cleaned_base}{range_operator}{head_ref}"
    result = subprocess.run(
        [BuildChangeConstants.GIT_BINARY, "diff", "--name-only", diff_range],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != BuildChangeConstants.GIT_SUCCESS_CODE:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def evaluate_should_build(
    *,
    event_name: str,
    github_ref: str,
    push_before_sha: str = "",
    pr_base_sha: str = "",
    head_ref: str = BuildChangeConstants.DEFAULT_HEAD_REF,
    repo_root: Path | None = None,
) -> bool:
    """Determine whether the Docker build/publish job should execute for this CI event."""
    cleaned_event = event_name.strip()
    cleaned_ref = github_ref.strip()

    if cleaned_event in BuildChangeConstants.ALWAYS_BUILD_EVENTS:
        return True
    if cleaned_ref.startswith(BuildChangeConstants.RELEASE_TAG_PREFIX):
        return True

    base_sha = (
        pr_base_sha
        if cleaned_event == BuildChangeConstants.PULL_REQUEST_EVENT
        else push_before_sha
    )
    changed_files = resolve_changed_files(
        event_name=cleaned_event,
        base_sha=base_sha,
        head_ref=head_ref,
        repo_root=repo_root,
    )
    if changed_files is None:
        return True
    return should_build_for_changed_files(changed_files)


def write_github_output(output_file: Path, *, should_build: bool) -> None:
    """Append `should_build=true|false` to the GitHub Actions step output file."""
    value = (
        BuildChangeConstants.OUTPUT_TRUE
        if should_build
        else BuildChangeConstants.OUTPUT_FALSE
    )
    with output_file.open("a", encoding=BuildChangeConstants.UTF8_ENCODING) as handle:
        handle.write(f"{BuildChangeConstants.OUTPUT_KEY_SHOULD_BUILD}={value}\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint used by `.github/workflows/docker-publish.yml`."""
    parser = argparse.ArgumentParser(
        description="Determine whether CI should build and publish Docker images."
    )
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", ""),
        help="GitHub Actions event name (e.g. push, pull_request, workflow_dispatch).",
    )
    parser.add_argument(
        "--github-ref",
        default=os.environ.get("GITHUB_REF", ""),
        help="GitHub ref (e.g. refs/heads/main or refs/tags/v1.0.7).",
    )
    parser.add_argument(
        "--push-before-sha",
        default=os.environ.get("PUSH_BEFORE_SHA", ""),
        help="Previous commit SHA for push events (github.event.before).",
    )
    parser.add_argument(
        "--pr-base-sha",
        default=os.environ.get("PR_BASE_SHA", ""),
        help="Base commit SHA for pull_request events (github.event.pull_request.base.sha).",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT", ""),
        help="Optional path to GITHUB_OUTPUT file to append should_build=true|false.",
    )
    args = parser.parse_args(argv)

    should_build = evaluate_should_build(
        event_name=args.event_name,
        github_ref=args.github_ref,
        push_before_sha=args.push_before_sha,
        pr_base_sha=args.pr_base_sha,
    )
    value = (
        BuildChangeConstants.OUTPUT_TRUE
        if should_build
        else BuildChangeConstants.OUTPUT_FALSE
    )
    print(value)

    if args.github_output:
        write_github_output(Path(args.github_output), should_build=should_build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
