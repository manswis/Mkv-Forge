"""Single-responsibility version synchronization tool for MKV Forge releases."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys


class ReleaseVersionConstants:
    """Centralized constants and regex patterns for semantic version synchronization."""

    REPO_ROOT: Path = Path(__file__).resolve().parents[1]
    CONFIG_REL_PATH: Path = Path("app/config.py")
    FRONTEND_CONSTANTS_REL_PATH: Path = Path("app/static/js/constants.js")
    DOCKERFILE_REL_PATH: Path = Path("Dockerfile")
    UTF8_ENCODING: str = "utf-8"

    TAG_PATTERN: re.Pattern[str] = re.compile(
        r"^v?(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?)$"
    )
    CONFIG_VERSION_PATTERN: re.Pattern[str] = re.compile(
        r'(DEFAULT_APP_VERSION:\s*str\s*=\s*")([^"]+)(")'
    )
    FRONTEND_VERSION_PATTERN: re.Pattern[str] = re.compile(
        r'(NAME:\s*"MKV Forge",\s*\n\s*VERSION:\s*")([^"]+)(")'
    )
    DOCKERFILE_ARG_PATTERN: re.Pattern[str] = re.compile(
        r"(^ARG\s+APP_VERSION=)([^\s]+)",
        re.MULTILINE,
    )
    EXPECTED_CONFIG_MATCHES: int = 1
    EXPECTED_FRONTEND_MATCHES: int = 1
    EXPECTED_DOCKERFILE_MATCHES: int = 2


@dataclass(frozen=True, slots=True)
class VersionSyncResult:
    """Summary of synchronized version fields across project files."""

    tag: str
    version: str
    updated_files: tuple[Path, ...]


def normalize_release_tag(raw_tag: str) -> tuple[str, str]:
    """Validate a release tag or semver string and return `(normalized_git_tag, clean_version)`.

    Accepts both `v1.0.1` and `1.0.1`. The normalized git tag will always be prefixed with `v`
    (e.g., `v1.0.1`), while the clean version is `1.0.1`.
    """
    if not raw_tag or not raw_tag.strip():
        raise ValueError("Release tag must not be empty (expected format: vX.Y.Z or X.Y.Z)")

    cleaned = raw_tag.strip()
    match = ReleaseVersionConstants.TAG_PATTERN.match(cleaned)
    if match is None:
        raise ValueError(
            f"Invalid release tag {raw_tag!r}: must follow semantic versioning X.Y.Z (e.g. 1.0.1 or v1.0.1)"
        )
    version = match.group("version")
    normalized_tag = f"v{version}"
    return normalized_tag, version


def read_current_version(repo_root: Path = ReleaseVersionConstants.REPO_ROOT) -> str:
    """Extract current `DEFAULT_APP_VERSION` from `app/config.py`."""
    config_path = repo_root / ReleaseVersionConstants.CONFIG_REL_PATH
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    content = config_path.read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)
    match = ReleaseVersionConstants.CONFIG_VERSION_PATTERN.search(content)
    if match is None:
        raise ValueError(f"Could not locate DEFAULT_APP_VERSION in {config_path}")
    return match.group(2)


def _replace_exact_count(
    content: str,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    expected_count: int,
    file_path: Path,
) -> str:
    """Apply regex substitution and verify the exact expected match count."""
    updated, count = pattern.subn(replacement, content)
    if count != expected_count:
        raise ValueError(
            f"Expected {expected_count} version match(es) in {file_path}, found {count}"
        )
    return updated


def sync_project_version(
    raw_tag: str,
    *,
    repo_root: Path = ReleaseVersionConstants.REPO_ROOT,
) -> VersionSyncResult:
    """Validate `raw_tag` and synchronize version across backend, frontend, and Dockerfile."""
    normalized_tag, version = normalize_release_tag(raw_tag)

    config_path = repo_root / ReleaseVersionConstants.CONFIG_REL_PATH
    frontend_path = repo_root / ReleaseVersionConstants.FRONTEND_CONSTANTS_REL_PATH
    dockerfile_path = repo_root / ReleaseVersionConstants.DOCKERFILE_REL_PATH

    for required_path in (config_path, frontend_path, dockerfile_path):
        if not required_path.is_file():
            raise FileNotFoundError(f"Required version target file missing: {required_path}")

    config_text = config_path.read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)
    frontend_text = frontend_path.read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)
    dockerfile_text = dockerfile_path.read_text(encoding=ReleaseVersionConstants.UTF8_ENCODING)

    new_config_text = _replace_exact_count(
        config_text,
        ReleaseVersionConstants.CONFIG_VERSION_PATTERN,
        rf"\g<1>{version}\g<3>",
        expected_count=ReleaseVersionConstants.EXPECTED_CONFIG_MATCHES,
        file_path=config_path,
    )
    new_frontend_text = _replace_exact_count(
        frontend_text,
        ReleaseVersionConstants.FRONTEND_VERSION_PATTERN,
        rf"\g<1>{version}\g<3>",
        expected_count=ReleaseVersionConstants.EXPECTED_FRONTEND_MATCHES,
        file_path=frontend_path,
    )
    new_dockerfile_text = _replace_exact_count(
        dockerfile_text,
        ReleaseVersionConstants.DOCKERFILE_ARG_PATTERN,
        rf"\g<1>{version}",
        expected_count=ReleaseVersionConstants.EXPECTED_DOCKERFILE_MATCHES,
        file_path=dockerfile_path,
    )

    config_path.write_text(new_config_text, encoding=ReleaseVersionConstants.UTF8_ENCODING)
    frontend_path.write_text(new_frontend_text, encoding=ReleaseVersionConstants.UTF8_ENCODING)
    dockerfile_path.write_text(new_dockerfile_text, encoding=ReleaseVersionConstants.UTF8_ENCODING)

    return VersionSyncResult(
        tag=normalized_tag,
        version=version,
        updated_files=(config_path, frontend_path, dockerfile_path),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `scripts/release_version.py`."""
    parser = argparse.ArgumentParser(
        description="Read or synchronize MKV Forge semantic release version across project files."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--current",
        action="store_true",
        help="Print the current DEFAULT_APP_VERSION and exit.",
    )
    group.add_argument(
        "-t",
        "--tag",
        type=str,
        help="Validate tag (vX.Y.Z) and synchronize version across app/config.py, constants.js, and Dockerfile.",
    )

    args = parser.parse_args(argv)
    try:
        if args.current:
            print(read_current_version())
            return 0
        result = sync_project_version(args.tag)
        print(f"Synchronized project version to {result.version} (tag {result.tag})")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
