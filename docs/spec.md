# Project Specification: MKV Forge (Web MKV Editor & Audio Converter)

## 1. Overview

A lightweight, self-hosted, single-container web application combining **MKVToolNix-style track/metadata editing** with **AAC 5.1 to AC3/EAC3 audio conversion**. Designed for offline homelab environments (`isolated_lan`) behind a reverse proxy (Nginx Proxy Manager), with a mobile-first responsive UI and real-time progress reporting.

**v1 Scope:** Single-file editing and job submission from the File Editor (no bulk multi-select actions in v1).

---

## 2. Tech Stack & Constraints

* **Backend:** Python 3.11+, FastAPI, Uvicorn (strictly `--workers 1` since the worker queue, active subprocess handle, and SSE log ring buffer live in-process), SQLite3 (stdlib, WAL mode).
* **Media Engines:** `mkvtoolnix` (`mkvmerge`), `ffmpeg` + `ffprobe`.
* **Frontend:** Vanilla HTML5/JS + Tailwind CSS (compiled to static `app.css` via standalone Tailwind CLI in multi-stage Docker build—zero Node.js, zero FOUC) + vendored Alpine.js.
* **100% Offline Requirement:** Zero external CDN calls. All CSS, JS, and icon assets must be compiled/vendored inside `app/static/` at Docker build time.
* **Real-Time Transport:** Server-Sent Events (`SSE`) via `sse-starlette` with `X-Accel-Buffering: no` and 15-second keepalive pings (`ping=15`) to survive Nginx Proxy Manager's default 60s `proxy_read_timeout` and mobile browser backgrounding.
* **Permissions:** Must support `PUID`, `PGID` (default `1000`), and optional `UMASK` (default `022`, yielding `0644` files; switchable to `002` for `0664`) via a lightweight `entrypoint.sh` (`gosu`) so `/app/data` and output media files are owned by the configured UID/GID, never `root`.
* **Local Development & Testing:** Developed and tested locally on macOS with `pytest` unit/integration tests first; Docker image build and runtime verified separately.

---

## 3. Core Functional Requirements

### 3.1 Library Scanner & Metadata Cache

* Recursively scan `/storage` for `.mkv` and `.mp4` files, ignoring `*.mkvforge.*` temp artifacts.
* Extract full container and track metadata using `mkvmerge -J <filepath>` with a per-file `30s` subprocess timeout. Log and skip corrupted/unreadable files rather than aborting the scan.
* Convert `container.properties.duration` from **nanoseconds** to **microseconds** (`duration_ns // 1000`) when storing `duration_us`.
* Cache parsed metadata in SQLite keyed by `(filepath, mtime, size_bytes)` alongside lightweight denormalized summary fields (`resolution`, `video_codec`, `audio_summary_json`, `subtitle_count`) for fast Library rendering without deserializing `raw_json` per row.
* Record `last_scan_at` in an `app_state` key-value table so Dashboard reflects the last scan even when an incremental scan skips all unchanged files.
* **Incremental Scans:** Skip files whose `mtime` and `size_bytes` match the database. Remove DB entries for deleted files.
* **Manual Refresh:** Support full library re-scan, incremental scan, and single-file re-probe (`POST /api/files/{id}/rescan`).

* **Library View & Filtering:**
  * Search by filename or folder path.
  * One-tap filter chips:
    * `AAC 5.1 Only` (`aac51_only=true`: `has_aac_51 = 1 AND has_ac3_eac3 = 0` — primary "needs fixing" filter used by the Dashboard)
    * `AAC 5.1` (`aac51=true`: `has_aac_51 = 1`)
    * `Has Global Metadata` (`has_metadata=true`: non-empty `container_title` OR non-empty `global_tags` — explicitly excludes `track_tags`, since almost every MKV contains `_STATISTICS_*` track tags)
    * `No AC3/EAC3` (`no_dolby=true`: `has_ac3_eac3 = 0`)
    * `Folder` (folder dropdown filter)
  * Display key badges per file: resolution, audio codecs/channels, subtitle count, file size, and metadata status.

### 3.2 MKVToolNix-Style Editor (Per-File)

Clicking any file opens the **Track & Metadata Editor**:

* **Container Metadata:**
  * Edit or clear **Container Title** (`--title "<title>"` or `--title ""`).
  * Toggle **Strip Global Tags** (`--no-global-tags`).
  * Toggle **Strip Track Tags** (`--no-track-tags`).
  * Toggle **Strip Attachments / Cover Art** (`--no-attachments`).
  * Toggle **Strip Chapters** (`--no-chapters`).
* **Track Management (Video, Audio, Subtitles):**
  * View all tracks with ID, type, codec, channels/resolution, language, title, and flags.
  * **Include / Exclude** checkbox per track (with client-side warning if all video tracks are unchecked).
  * **Edit Track Name** (free text).
  * **Edit Language** (ISO 639-2/BCP-47 selector).
  * **Toggle Flags:** `Default`, `Forced`.
  * **Reorder Tracks:** Up/down controls (mobile-friendly tap buttons, not drag-only) to define output track order.
  * **Virtual Preview Row:** When "Add Converted Audio Track" is ON, render a live preview row at Audio Index 0 (`[NEW] <Title> • Default`) in the track list.

### 3.3 AAC 5.1 → AC3 / EAC3 Track Addition

* Dedicated **"Add Converted Audio Track"** panel in the editor (auto-highlighted when an AAC 5.1 track is detected):
  * **Enable Toggle:** Off by default (one-tap enable).
  * **Source Track Selector:** Choose which audio track to transcode (defaults to first AAC 5.1 track).
  * **Target Codec:** `EAC3 (Dolby Digital Plus)` or `AC3 (Dolby Digital)`.
  * **Bitrate:** `640k` (default), `448k`, `384k`.
  * **Track Title:** Editable text input (default: `<Language> EAC3 5.1` or `<Language> AC3 5.1`).
  * **Track Language:** Inherits from source track, editable.
  * **Channel Preservation:** Derive FFmpeg `-ac` from the source track's channel count capped at `6` (`min(source_channels, 6)`), avoiding fake upmixing of stereo sources.
  * **Placement & Default Flag Demotion:** Automatically inserted at **Audio Index 0** with `--default-track-flag 0:yes`, while explicitly clearing the default flag (`--default-track-flag <id>:no`) on existing audio tracks (unless overridden) so players never see double default audio tracks. User may optionally drop the original AAC track.

### 3.4 Two-Stage Processing Pipeline & Atomic Writes

Jobs execute sequentially in a single-worker background queue (1 active job at a time to avoid USB I/O thrashing).

1. **Pre-Flight Checks:**
   * **Duplicate Job Guard:** Reject `POST /api/jobs` with `409 Conflict` if `filepath` already has a `queued` or `running` job.
   * **`.mp4` Target Collision Guard:** If processing an `.mp4` in *Replace Original* mode and `<stem>.mkv` already exists on disk, fail safely rather than overwriting `<stem>.mkv`.
   * **Disk Space Check:** Verify free space on the target file's parent directory is at least `1.1 * input_file_size + estimated_audio_bytes + 100 MB` (when audio conversion is enabled; `1.1 * input_file_size + 100 MB` otherwise). Fail fast with a clear UI error if insufficient.

2. **Stage 1 — Audio Transcode (only if conversion is enabled):**
   * Map the selected source track by relative audio index (`-map 0:a:<N>`) derived from `mkvmerge -J` audio tracks in order, and verify codec + channel count against `ffprobe` before starting.
   * Run:
     `ffmpeg -y -i "<input>" -map 0:a:<N> -c:a <eac3|ac3> -b:a <bitrate> -ac <channels> -progress pipe:1 -nostats "<same_dir>/<filename>.mkvforge.<eac3|ac3>"`
   * Store `<temp_audio_file>` in the **same directory** as the source file (avoiding root overlayfs exhaustion on long movies) and delete it in a `finally` block after Stage 2 completes, fails, or is cancelled.

3. **Stage 2 — MKV Remux:**
   * Build `mkvmerge --gui-mode -o "<output_tmp>" ...` via argument list (no shell interpolation):
     * When Stage 1 ran, prepend `<temp_audio_file>` (file index `0`) with `--track-name 0:"<title>"`, `--language 0:<lang>`, `--default-track-flag 0:yes`, and `--sync 0:<delay_ms>` if the source audio track had a non-zero `properties.minimum_timestamp` (converted from ns to ms).
     * For the source file (file index `1` if Stage 1 ran, else `0`):
       * Emit `--no-video` / `--no-audio` / `--no-subtitles` whenever zero tracks of that type are kept (never empty track lists).
       * Apply `--video-tracks`, `--audio-tracks`, `--subtitle-tracks`, `--track-name`, `--language`, `--default-track-flag`, `--forced-display-flag`, and container metadata strip flags.
     * Build `--track-order` containing **only kept tracks** with accurate `<file_id>:<track_id>` pairs.

4. **Same-Filesystem Atomic Output & `.mp4` Handling:**
   * **Source is `.mkv`:**
     * *Replace Original (default):* Write to `<dir>/<filename>.mkvforge.tmp`; on exit code `0` (or `1` with warnings), atomically swap via `os.replace(output_tmp, original_path)`.
     * *Keep Both:* Auto-increment target name if needed (`<stem>.remux.mkv`, `<stem>.remux.2.mkv`, ...), write temp in same dir, then rename.
   * **Source is `.mp4`:**
     * *Replace Original:* Write to `<dir>/<stem>.mkv.mkvforge.tmp`; re-verify `<stem>.mkv` does not exist; move to `<stem>.mkv`; delete the original `.mp4` **only after** `<stem>.mkv` is safely in place; update SQLite (index `<stem>.mkv`, remove old `.mp4` row).
     * *Keep Both:* Write to auto-incremented `<stem>.remux.mkv` (`<stem>.remux.2.mkv`, ...), leaving `.mp4` untouched.
   * Re-probe the final output file with `mkvmerge -J` and update SQLite immediately.

### 3.5 Real-Time Progress & Job Queue

* **Never-Stuck UI:**
  * **Stage 1 (FFmpeg):** Parse `out_time_us` (ignoring `N/A`) and `speed` from `-progress pipe:1`. Calculate `percent = min(99, int((out_time_us / duration_us) * 100))`.
  * **Stage 2 (`mkvmerge`):** Parse `Progress: (\d+)%` from `--gui-mode` stdout.
  * Stream structured SSE events: `{ job_id, stage, stage_label, percent, speed, eta, log_line, status }`.
  * Keep a rolling in-memory ring buffer (~100 lines) for the active job so reconnecting mobile clients immediately catch up.
* **Job Controls:**
  * View active job, queued jobs, and completed/failed/cancelled history (persisted in SQLite).
  * **Cancel** button on active or queued jobs (sends `SIGTERM` with 5s fallback to `SIGKILL` on active subprocess and cleans up `*.mkvforge.*` temp files).
  * Expandable live log drawer showing raw `ffmpeg`/`mkvmerge` output.

---

## 4. Database Schema (SQLite: `/app/data/mkvforge.db`, WAL Mode)

* **`app_state`**
  * `key` (TEXT PK — e.g., `last_scan_at`, `last_scan_status`)
  * `value` (TEXT)
  * `updated_at` (TIMESTAMP)

* **`media_files`**
  * `id` (INTEGER PK)
  * `filepath` (TEXT UNIQUE)
  * `folder` (TEXT, indexed)
  * `filename` (TEXT)
  * `size_bytes` (INTEGER)
  * `mtime` (REAL)
  * `duration_us` (INTEGER)
  * `resolution` (TEXT — e.g., `2160p`, `1080p`, `720p`, `SD`)
  * `video_codec` (TEXT)
  * `audio_summary_json` (TEXT — compact JSON list of audio codec/channel/language summaries)
  * `subtitle_count` (INTEGER)
  * `container_title` (TEXT)
  * `has_global_tags` (BOOLEAN)
  * `has_aac_51` (BOOLEAN, indexed)
  * `has_ac3_eac3` (BOOLEAN, indexed)
  * `raw_json` (TEXT — full `mkvmerge -J` output)
  * `scanned_at` (TIMESTAMP)

* **`jobs`**
  * `id` (TEXT PK, UUID)
  * `filepath` (TEXT, indexed)
  * `output_filepath` (TEXT)
  * `status` (TEXT: `queued`, `running`, `completed`, `failed`, `cancelled`, indexed)
  * `config_json` (TEXT — user's edit/transcode payload)
  * `stage` (TEXT)
  * `progress` (INTEGER)
  * `warnings_json` (TEXT)
  * `error_message` (TEXT)
  * `created_at` (TIMESTAMP)
  * `completed_at` (TIMESTAMP)

---

## 5. API Endpoints

* `GET /api/health` — Verify `mkvmerge --version`, `ffmpeg -version`, `ffprobe -version`, and write access to `/app/data`.
* `GET /api/dashboard` — Return summary stats (`total_files`, `aac51_only`, `has_metadata`, `last_scan_at`) and recent job activity.
* `GET /api/files` — List cached files with query params (`q`, `folder`, `aac51_only`, `aac51`, `has_metadata`, `no_dolby`, `sort`, `order`, `page`, `limit`) plus distinct folder list.
* `GET /api/files/{id}` — Return full parsed track/metadata JSON for a single file.
* `POST /api/files/{id}/rescan` — Re-probe a single file on disk and update SQLite (returns updated file record, or `404` if removed).
* `POST /api/scan` — Trigger incremental (or `?full=true`) library scan in background; rejects overlapping scans (`409 Conflict`).
* `GET /api/scan/stream` — SSE stream for scan progress (`scanned`, `total`, `current_file`, `skipped`, `errors`, `status`).
* `POST /api/jobs` — Validate payload, enforce duplicate job guard (`409 Conflict`), enqueue a remux/transcode job.
* `GET /api/jobs` — List active, queued, and historical jobs.
* `DELETE /api/jobs/{job_id}` — Cancel an active or queued job.
* `GET /api/jobs/stream` — SSE stream broadcasting real-time stage, percentage, speed, logs, and status.

---

## 6. Modular Backend Layout (Clean & Pragmatic)

```text
app/
  main.py                  # Thin FastAPI app wiring, static mounts, lifespan startup/shutdown
  config.py                # Env vars & constants (STORAGE_DIR, DATA_DIR, timeouts, defaults)
  utils/
    paths.py               # safe_resolve_path(), Keep-Both collision auto-increment, temp naming
  db/
    connection.py          # SQLite WAL setup & schema migrations
    repositories.py        # Clean CRUD queries for media_files, jobs, and app_state
  models/
    schemas.py             # Pydantic request/response models
  services/
    probe_parser.py        # Parse mkvmerge -J JSON (duration ns->us, badges, flags, sync delay)
    scanner.py             # Incremental / full library walker + single-file probe + error resilience
    ffmpeg_runner.py       # ffprobe stream verification + Stage 1 async FFmpeg execution & progress
    mkv_builder.py         # Pure deterministic mkvmerge argv builder (track order, flags, sync)
    mkv_runner.py          # Stage 2 async mkvmerge execution, exit codes, atomic swap / mp4 handling
    job_worker.py          # Single-worker queue loop, disk-space pre-flight, cancellation, crash sweep
    sse_broker.py          # In-memory pub/sub + active job log ring buffer
  api/
    health.py
    dashboard.py
    files.py
    scan.py
    jobs.py
  static/
    css/                   # Compiled app.css (built via standalone Tailwind CLI)
    js/                    # Vendored Alpine.js + modular app JS
    index.html
tests/                     # pytest unit & integration suite
```

---

## 7. UI/UX Specification (Mobile & Desktop)

See [ui spec.md](file:///Users/manojbiswas/Documents/Projects/cranbear/MkvEdit/ui%20spec.md) for the complete screen-by-screen design and visual system.

---

## 8. Docker & Deployment Specification

* **Base Image:** `python:3.11-slim-bookworm` (multi-stage build with standalone Tailwind CLI stage for compiling `app.css`)
* **Installed Packages:** `mkvtoolnix`, `ffmpeg`, `gosu`
* **Exposed Port:** `7788`
* **Volumes:**
  * `/app/data` — SQLite DB and app state.
  * `/storage` — Media library root (read/write).
* **Environment Variables:**
  * `PUID=1000`
  * `PGID=1000`
  * `UMASK=022` *(optional; set `002` for group-writable `0664`)*
  * `TZ=America/Toronto`
* **Network:** Attached to external `isolated_lan` (no host port binding; reverse-proxied via NPM to `http://mkv-forge:7788`).

---

## 9. Safety & Edge-Case Guardrails

* **Disk Space Check:** Verify free space on the target parent dir is at least `1.1 * input_file_size + estimated_audio_bytes + 100 MB` before starting.
* **Path Traversal Protection:** Strictly validate that all target file paths resolve inside `/storage`.
* **Duplicate Job Guard:** Return `409 Conflict` if a file already has a `queued` or `running` job.
* **`.mp4` Overwrite Guard:** Refuse to overwrite an existing `<stem>.mkv` when replacing an `.mp4`.
* **Crash Cleanup:** On container startup, mark any stale `running` jobs in SQLite as `failed` and sweep `/storage` for orphaned `*.mkvforge.*` temp files.
* **MKVToolNix Exit Codes:** Treat `mkvmerge` exit code `0` as success, `1` as success-with-warnings (surface warnings in job log, still complete atomic swap), and `2` as hard failure (delete temp files, preserve original).
