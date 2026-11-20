#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
DATA_DIR="${MKVFORGE_DATA_DIR:-/app/data}"
APP_USER="mkvforge"
APP_GROUP="mkvforge"

if ! [[ "${PUID}" =~ ^[0-9]+$ ]] || ! [[ "${PGID}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: PUID and PGID must be non-negative integers (got PUID=${PUID}, PGID=${PGID})" >&2
  exit 1
fi

if ! [[ "${UMASK}" =~ ^[0-7]{3,4}$ ]]; then
  echo "ERROR: UMASK must be a valid 3- or 4-digit octal mask (got UMASK=${UMASK})" >&2
  exit 1
fi

umask "${UMASK}"

# Ensure group exists for PGID
if ! getent group "${PGID}" >/dev/null 2>&1; then
  if getent group "${APP_GROUP}" >/dev/null 2>&1; then
    groupmod -o -g "${PGID}" "${APP_GROUP}"
  else
    groupadd -o -g "${PGID}" "${APP_GROUP}"
  fi
fi

# Ensure user exists for PUID
if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  if id -u "${APP_USER}" >/dev/null 2>&1; then
    usermod -o -u "${PUID}" -g "${PGID}" "${APP_USER}"
  else
    useradd -o -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin "${APP_USER}"
  fi
fi

mkdir -p "${DATA_DIR}"
chown -R "${PUID}:${PGID}" "${DATA_DIR}"

exec gosu "${PUID}:${PGID}" uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 7788 \
  --workers 1 \
  --no-access-log
