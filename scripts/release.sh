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
#   3. Run full test suite with code coverage report (standalone, no commit):
#      ./scripts/release.sh --cov
#
# Optional flags:
#   --cov         Run full test suite with line coverage report (standalone if -m omitted)
#   --no-push     Commit (and tag) locally without running `git push`
#   --skip-tests  Skip running unit tests before committing
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

PYTHON_BIN="python3"
if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
fi

# Resolve a Python interpreter that has pytest installed (for --cov and test execution)
PYTEST_PYTHON="${PYTHON_BIN}"
if ! "${PYTEST_PYTHON}" -m pytest --version >/dev/null 2>&1; then
  if command -v pytest >/dev/null 2>&1; then
    SHEBANG_PY="$(head -n 1 "$(command -v pytest)" | sed 's/^#![[:space:]]*//')"
    if [[ -x "${SHEBANG_PY}" ]] && "${SHEBANG_PY}" -m pytest --version >/dev/null 2>&1; then
      PYTEST_PYTHON="${SHEBANG_PY}"
    fi
  fi
fi

COMMIT_MSG=""
RELEASE_TAG=""
PUSH_CHANGES=1
RUN_TESTS=1
RUN_COV=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/release.sh -m "<commit message>" [-t <vX.Y.Z>] [--cov] [--no-push] [--skip-tests]
  ./scripts/release.sh --cov

Examples:
  ./scripts/release.sh --cov
  ./scripts/release.sh -m "Improve watcher filter"
  ./scripts/release.sh -m "Improve watcher filter" --cov
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
    --cov)
      RUN_COV=1
      shift
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

# Standalone coverage mode: `./scripts/release.sh --cov` without -m or -t
if [[ "${RUN_COV}" -eq 1 && -z "${COMMIT_MSG// }" && -z "${RELEASE_TAG}" ]]; then
  echo "==> Running full test suite with coverage report..."
  exec "${PYTEST_PYTHON}" "${ROOT_DIR}/scripts/run_coverage.py" tests -q
fi

if [[ -z "${COMMIT_MSG// }" ]]; then
  echo "Error: Commit message (-m) is required." >&2
  usage >&2
  exit 1
fi

# 1. If -t <tag> was supplied, validate/normalize WITHOUT mutating files yet
CLEAN_VER=""
RESUMING_LOCAL_TAG=0
if [[ -n "${RELEASE_TAG}" ]]; then
  NORM_OUTPUT="$("${PYTHON_BIN}" "${ROOT_DIR}/scripts/release_version.py" --normalize "${RELEASE_TAG}")"
  read -r RELEASE_TAG CLEAN_VER <<< "${NORM_OUTPUT}"

  if git rev-parse -q --verify "refs/tags/${RELEASE_TAG}" >/dev/null; then
    TAG_COMMIT="$(git rev-list -n 1 "${RELEASE_TAG}")"
    HEAD_COMMIT="$(git rev-parse HEAD)"
    IS_CLEAN=0
    if [[ -z "$(git status --porcelain)" ]]; then
      IS_CLEAN=1
    fi

    if [[ "${PUSH_CHANGES}" -eq 1 && "${IS_CLEAN}" -eq 1 && "${TAG_COMMIT}" == "${HEAD_COMMIT}" ]]; then
      if git ls-remote --exit-code --tags origin "refs/tags/${RELEASE_TAG}" >/dev/null 2>&1; then
        echo "Error: Release tag '${RELEASE_TAG}' already exists on origin. Bump to a newer version." >&2
        exit 1
      fi
      RESUMING_LOCAL_TAG=1
      echo "==> Local tag '${RELEASE_TAG}' already points to HEAD and is not on origin yet; resuming publish..."
    else
      echo "Error: Git tag '${RELEASE_TAG}' already exists locally." >&2
      exit 1
    fi
  fi
fi

# 2. Run tests (with coverage when --cov is passed) BEFORE mutating version files or committing
if [[ "${RUN_TESTS}" -eq 1 ]]; then
  TEST_OK=0
  if [[ "${RUN_COV}" -eq 1 ]]; then
    echo "==> Running full test suite with coverage report..."
    "${PYTEST_PYTHON}" "${ROOT_DIR}/scripts/run_coverage.py" tests -q && TEST_OK=1
  else
    echo "==> Running unit test suite..."
    if [[ -x "${ROOT_DIR}/.venv/bin/pytest" ]]; then
      "${ROOT_DIR}/.venv/bin/pytest" tests/unit && TEST_OK=1
    elif command -v pytest >/dev/null 2>&1; then
      pytest tests/unit && TEST_OK=1
    else
      "${PYTEST_PYTHON}" -m pytest tests/unit && TEST_OK=1
    fi
  fi

  if [[ "${TEST_OK}" -ne 1 ]]; then
    echo "" >&2
    if [[ "${RUN_COV}" -eq 1 ]]; then
      echo "Error: Test suite failed during coverage check. Fix failing tests before releasing (or pass --skip-tests to override)." >&2
    else
      echo "Error: Unit tests failed. Fix failing tests before releasing (or pass --skip-tests to override)." >&2
    fi
    exit 1
  fi
fi

# 3. Sync version files, stage, commit, and create annotated tag (skipped when resuming an unpushed local tag)
if [[ "${RESUMING_LOCAL_TAG}" -eq 0 ]]; then
  if [[ -n "${RELEASE_TAG}" ]]; then
    SYNC_OUTPUT="$("${PYTHON_BIN}" "${ROOT_DIR}/scripts/release_version.py" --tag "${RELEASE_TAG}")"
    echo "==> ${SYNC_OUTPUT}"
  fi

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

  if [[ -n "${RELEASE_TAG}" ]]; then
    echo "==> Creating annotated tag ${RELEASE_TAG}..."
    git tag -a "${RELEASE_TAG}" -m "${COMMIT_MSG}"
  fi
fi

# 4. Push commit (and tag, if created) to origin
if [[ "${PUSH_CHANGES}" -eq 1 ]]; then
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  echo "==> Pushing branch '${CURRENT_BRANCH}' to origin..."
  git push origin "${CURRENT_BRANCH}"
  if [[ -n "${RELEASE_TAG}" ]]; then
    echo "==> Pushing release tag '${RELEASE_TAG}' to origin..."
    git push origin "${RELEASE_TAG}"
    echo "==> GitHub Actions will run unit tests, publish multi-arch ':latest' Docker images, and create GitHub Release 'MKV Forge ${CLEAN_VER}' automatically."
  fi
fi

echo "==> Done!"
