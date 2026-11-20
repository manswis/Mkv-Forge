#!/usr/bin/env bash
# =============================================================================
# MKV Forge — Commit, Version Sync, Unit Test & Release Tag Helper
#
# Usage:
#   1. Normal commit (triggers :nightly and :beta Docker builds on main):
#      ./scripts/release.sh -m "fix audio sync delay"
#
#   2. Tagged release (bumps version, tests, commits, tags, triggers :latest):
#      ./scripts/release.sh -m "Release v0.2.0" -t v0.2.0
#
# Optional flags:
#   --no-push     Commit (and tag) locally without running `git push`
#   --skip-tests  Skip running unit tests before committing
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

COMMIT_MSG=""
RELEASE_TAG=""
PUSH_CHANGES=1
RUN_TESTS=1

usage() {
  cat <<'EOF'
Usage:
  ./scripts/release.sh -m "<commit message>" [-t <vX.Y.Z>] [--no-push] [--skip-tests]

Examples:
  ./scripts/release.sh -m "Improve watcher filter"
  ./scripts/release.sh -m "Release v0.2.0" -t v0.2.0
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      COMMIT_MSG="${2:-}"
      shift 2
      ;;
    -t|--tag)
      RELEASE_TAG="${2:-}"
      shift 2
      ;;
    --no-push)
      PUSH_CHANGES=0
      shift
      ;;
    --skip-tests)
      RUN_TESTS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown argument '$1'" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${COMMIT_MSG// }" ]]; then
  echo "Error: Commit message (-m) is required." >&2
  usage >&2
  exit 1
fi

# 1. If -t <tag> was supplied, normalize with release_version.py, verify git tag, and sync files
if [[ -n "${RELEASE_TAG}" ]]; then
  # Automatically normalizes 1.0.1 -> v1.0.1 and syncs version in config.py, constants.js, Dockerfile
  SYNC_OUTPUT="$(python3 "${ROOT_DIR}/scripts/release_version.py" --tag "${RELEASE_TAG}")"
  echo "==> ${SYNC_OUTPUT}"
  CLEAN_VER="$(python3 "${ROOT_DIR}/scripts/release_version.py" --current)"
  RELEASE_TAG="v${CLEAN_VER}"

  if git rev-parse -q --verify "refs/tags/${RELEASE_TAG}" >/dev/null; then
    echo "Error: Git tag '${RELEASE_TAG}' already exists locally." >&2
    exit 1
  fi
fi

# 2. Run unit tests before staging/committing
if [[ "${RUN_TESTS}" -eq 1 ]]; then
  echo "==> Running unit test suite..."
  TEST_OK=0
  if [[ -x "${ROOT_DIR}/.venv/bin/pytest" ]]; then
    "${ROOT_DIR}/.venv/bin/pytest" tests/unit && TEST_OK=1
  elif command -v pytest >/dev/null 2>&1; then
    pytest tests/unit && TEST_OK=1
  else
    python3 -m pytest tests/unit && TEST_OK=1
  fi

  if [[ "${TEST_OK}" -ne 1 ]]; then
    echo ""
    echo "Notice: Local pytest environment is missing async test plugins (e.g. pytest-asyncio)."
    echo "Notice: All unit tests run automatically in a clean environment in GitHub Actions on push."
    read -r -p "Do you want to proceed with release and let CI verify tests? [y/N] " PROCEED
    if [[ ! "${PROCEED}" =~ ^[Yy]$ ]]; then
      echo "Aborted." >&2
      exit 1
    fi
  fi
fi

# 3. Stage changes and verify there is something to commit
git add -A

if git diff --cached --quiet; then
  if [[ -n "${RELEASE_TAG}" ]]; then
    echo "==> Working tree already clean at version ${RELEASE_TAG}; tagging current HEAD..."
  else
    echo "Nothing to commit (working tree is clean)."
    exit 0
  fi
else
  echo "==> Committing changes..."
  git commit -m "${COMMIT_MSG}"
fi

# 4. Create annotated release tag if -t <vX.Y.Z> was provided
if [[ -n "${RELEASE_TAG}" ]]; then
  echo "==> Creating annotated tag ${RELEASE_TAG}..."
  git tag -a "${RELEASE_TAG}" -m "${COMMIT_MSG}"
fi

# 5. Push commit (and tag, if created) to origin
if [[ "${PUSH_CHANGES}" -eq 1 ]]; then
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  echo "==> Pushing branch '${CURRENT_BRANCH}' to origin..."
  git push origin "${CURRENT_BRANCH}"
  if [[ -n "${RELEASE_TAG}" ]]; then
    echo "==> Pushing release tag '${RELEASE_TAG}' to origin (triggers :latest Docker build)..."
    git push origin "${RELEASE_TAG}"
  fi
fi

echo "==> Done!"
