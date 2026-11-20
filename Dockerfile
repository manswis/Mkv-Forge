# syntax=docker/dockerfile:1

ARG TAILWIND_VERSION=v3.4.17
ARG APP_VERSION=1.0.0

# ---------------------------------------------------------------------------
# Stage 1: Compile static Tailwind CSS using standalone CLI (zero Node.js)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS css-builder

ARG TARGETARCH
ARG TAILWIND_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN case "${TARGETARCH}" in \
      amd64) TW_ARCH="x64" ;; \
      arm64) TW_ARCH="arm64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${TW_ARCH}" \
       -o /usr/local/bin/tailwindcss \
    && chmod +x /usr/local/bin/tailwindcss

COPY tailwind.config.js ./
COPY frontend/input.css ./frontend/input.css
COPY app/static/ ./app/static/

RUN tailwindcss -i ./frontend/input.css -o ./app/static/css/app.css --minify

# ---------------------------------------------------------------------------
# Stage 2: Runtime image with mkvtoolnix, ffmpeg, gosu, and FastAPI
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ARG APP_VERSION=1.0.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    MKVFORGE_APP_VERSION=${APP_VERSION} \
    MKVFORGE_STORAGE_DIR=/storage \
    MKVFORGE_DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       mkvtoolnix \
       ffmpeg \
       gosu \
       tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=css-builder /build/app/static/css/app.css ./app/static/css/app.css
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /app/data /storage

EXPOSE 7788

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7788/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
