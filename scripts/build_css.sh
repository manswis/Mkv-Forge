#!/usr/bin/env bash
set -euo pipefail

TAILWIND_VERSION="v3.4.17"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${ROOT_DIR}/.tools"
TW_BIN="${TOOLS_DIR}/tailwindcss"

mkdir -p "${TOOLS_DIR}" "${ROOT_DIR}/app/static/css"

if [[ ! -x "${TW_BIN}" ]]; then
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  case "${OS}:${ARCH}" in
    Darwin:arm64) ASSET="tailwindcss-macos-arm64" ;;
    Darwin:x86_64) ASSET="tailwindcss-macos-x64" ;;
    Linux:x86_64) ASSET="tailwindcss-linux-x64" ;;
    Linux:aarch64|Linux:arm64) ASSET="tailwindcss-linux-arm64" ;;
    *)
      echo "Unsupported platform: ${OS}/${ARCH}" >&2
      exit 1
      ;;
  esac
  curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${ASSET}" -o "${TW_BIN}"
  chmod +x "${TW_BIN}"
fi

cd "${ROOT_DIR}"
"${TW_BIN}" -i ./frontend/input.css -o ./app/static/css/app.css --minify
