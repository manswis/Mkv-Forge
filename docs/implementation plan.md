# MKV Forge — Implementation Plan & Progress Tracker

> **Workflow:** Develop and run `pytest` unit/integration tests locally on macOS first; verify Docker build/run separately. Check off items (`[x]`) as each milestone is completed and verified.

---

## Phase 1: Project Skeleton, Offline Foundation, Local Test Harness & Docker Setup

Goal: Clean, modular project layout with local `pytest` harness, offline Tailwind + Alpine.js build pipeline, and a permission-safe Docker setup ready for `isolated_lan`.

- [x] **1.1 Pragmatic Modular Repository Structure**
  - [x] Create backend package layout:
    - `app/main.py` (thin FastAPI wiring, static mounts, lifespan startup/shutdown)
    - `app/config.py` (env vars & centralized constants — zero magic numbers)
    - `app/utils/` (`paths.py` in Phase 2)
    - `app/db/` (`connection.py`, `repositories.py` in Phase 2)
    - `app/models/schemas.py`
    - `app/services/` (`health_service.py` in Phase 1; `probe_parser.py`, `scanner.py`, `ffmpeg_runner.py`, `mkv_builder.py`, `mkv_runner.py`, `job_worker.py`, `sse_broker.py` in Phases 2–4)
    - `app/api/` (`health.py` in Phase 1; `dashboard.py`, `files.py`, `scan.py`, `jobs.py` in Phases 2 & 4)
    - `app/static/` (`css/`, `js/`, `index.html`)
  - [x] Create `requirements.txt` (`fastapi`, `uvicorn[standard]`, `sse-starlette`, `pydantic`) and `requirements-dev.txt` (`pytest`, `pytest-asyncio`, `httpx`).
  - [x] Set up `tests/` layout (`conftest.py`, unit & integration test directories) and verify `pytest` runs cleanly on macOS.

- [x] **1.2 Offline Frontend Asset Pipeline**
  - [x] Configure Tailwind CSS input stylesheet (`frontend/input.css` + `tailwind.config.js`) targeting `app/static/css/app.css` using the standalone Tailwind CLI binary (zero Node.js, zero runtime CDN, zero FOUC).
  - [x] Vendor minified Alpine.js locally into `app/static/js/alpine.min.js`.
  - [x] Add a clean placeholder `app/static/index.html` verifying offline CSS + Alpine.js load with zero external requests.

- [x] **1.3 Health Check & App Wiring**
  - [x] Implement `GET /api/health` in `app/api/health.py` verifying `mkvmerge --version`, `ffmpeg -version`, `ffprobe -version`, and write access to `DATA_DIR`.
  - [x] Write unit tests for `app/config.py` and `GET /api/health` (mocked + failure cases).

- [x] **1.4 Permission-Safe Docker Setup (Verified Separately on Docker Host)**
  - [x] Write multi-stage `Dockerfile`:
    - Stage 1: Standalone Tailwind CLI compiling `app/static/css/app.css`.
    - Stage 2: `python:3.11-slim-bookworm` installing `mkvtoolnix`, `ffmpeg`, `gosu`, Python deps, and built static assets.
  - [x] Write `entrypoint.sh`: read `PUID` (default `1000`), `PGID` (default `1000`), and optional `UMASK` (default `022`), create/update non-root user, apply `umask`, `chown -R $PUID:$PGID /app/data`, and exec `uvicorn app.main:app --host 0.0.0.0 --port 7788 --workers 1` via `gosu`.
  - [x] Create `docker-compose.yml` matching `isolated_lan`, `./data:/app/data:rw`, `/mnt/usbmedia/media:/storage:rw`, and `PUID`/`PGID`/`UMASK`/`TZ`.

---

## Phase 2: Database Layer, Path Safety & Incremental Library Scanner

Goal: Fast, cached SQLite metadata indexing of `/storage` (`.mkv` & `.mp4`) with resilient probing, denormalized badges, and filterable API endpoints.

- [x] **2.1 SQLite Setup & Repositories (`app/db/`)**
  - [x] Initialize SQLite DB (`<DATA_DIR>/mkvforge.db`) with `PRAGMA journal_mode=WAL;` and `PRAGMA foreign_keys=ON;`.
  - [x] Create `app_state` table (`key`, `value`, `updated_at`) for `last_scan_at` and scan metadata.
  - [x] Create `media_files` table with indexes (`folder`, `has_aac_51`, `has_ac3_eac3`) and denormalized badge columns (`resolution`, `video_codec`, `audio_summary_json`, `subtitle_count`, `duration_us`, `container_title`, `has_global_tags`, `raw_json`).
  - [x] Create `jobs` table (`id`, `filepath`, `output_filepath`, `status`, `config_json`, `stage`, `progress`, `warnings_json`, `error_message`, `created_at`, `completed_at`).
  - [x] Write exhaustive repository unit tests (CRUD, filtering, pagination, `aac51_only`, `has_metadata`, `app_state`).

- [x] **2.2 Path Safety & Naming Utilities (`app/utils/paths.py`)**
  - [x] Implement `safe_resolve_path(rel_path, storage_root)` strictly enforcing paths resolve inside `STORAGE_DIR` (blocks `../` and symlink escapes).
  - [x] Implement Keep-Both collision resolver (`<stem>.remux.mkv`, `<stem>.remux.2.mkv`, ...).
  - [x] Implement temp filename helpers (`*.mkvforge.tmp`, `*.mkvforge.<ext>`) and `.mp4` target `.mkv` path resolution.
  - [x] Write unit tests covering traversal attacks, symlinks, unicode filenames, and Keep-Both collisions.

- [x] **2.3 `mkvmerge -J` Parser (`app/services/probe_parser.py`)**
  - [x] Parse `mkvmerge -J` JSON payload:
    - Convert `container.properties.duration` from **nanoseconds to microseconds** (`duration_ns // 1000`).
    - Extract `container_title` and `has_global_tags` (explicitly excluding `track_tags`).
    - Extract `has_aac_51` (audio codec contains `AAC` and `audio_channels == 6`) and `has_ac3_eac3` (codec contains `AC-3` or `E-AC-3`).
    - Extract `resolution` (`2160p`/`1080p`/`720p`/`SD`), `video_codec`, `audio_summary_json`, `subtitle_count`.
    - Extract per-track details including relative audio index (`audio_index`), `minimum_timestamp` (ns → ms delay), language, flags, and codecs.
  - [x] Write unit tests against diverse real-world `mkvmerge -J` fixtures (AAC 5.1, multi-audio, missing duration, global tags vs track tags, non-zero `minimum_timestamp`, `.mp4` probe).

- [x] **2.4 Incremental Scan Engine (`app/services/scanner.py`)**
  - [x] Recursively walk `STORAGE_DIR` for `.mkv` and `.mp4` files, ignoring `*.mkvforge.*`.
  - [x] Compare `(filepath, mtime, size_bytes)` against SQLite; skip unchanged files in incremental mode.
  - [x] Enforce per-file `30s` timeout on `mkvmerge -J`; log and skip corrupted/unreadable files without aborting the scan.
  - [x] Delete DB rows for files that no longer exist on disk; update `app_state.last_scan_at` even when 0 files changed.
  - [x] Implement `probe_single_file(filepath)` for single-file re-scan and post-job cache refreshes.
  - [x] Write integration tests (incremental skip, full rescan, deleted file cleanup, corrupt file skip, single-file rescan).

- [x] **2.5 Dashboard, Scanner & Library APIs (`app/api/`)**
  - [x] `GET /api/dashboard` — Total files, `aac51_only` count (`has_aac_51 = 1 AND has_ac3_eac3 = 0`), `has_metadata` count, `last_scan_at`, and recent job activity.
  - [x] `POST /api/scan` — Trigger incremental (or `?full=true`) scan in background; return `409 Conflict` if a scan is already running.
  - [x] `GET /api/scan/stream` — SSE stream (`X-Accel-Buffering: no`, `ping=15`) emitting `{ scanned, total, skipped, errors, current_file, status }`.
  - [x] `GET /api/files` — Paginated, sortable cached list supporting `q`, `folder`, `aac51_only`, `aac51`, `has_metadata`, `no_dolby`, `page`, `limit`, plus distinct folder list.
  - [x] `GET /api/files/{id}` — Full parsed track & metadata details for the editor.
  - [x] `POST /api/files/{id}/rescan` — Re-probe single file on disk and return refreshed details.
  - [x] Write API endpoint tests via `httpx.AsyncClient`.

---

## Phase 3: Two-Stage Processing Engine (FFmpeg + mkvmerge)

Goal: Deterministic, atomic audio transcoding and MKV remuxing with accurate progress, stream verification, and safe `.mp4` / `.mkv` handling.

- [x] **3.1 Pre-Flight Guards**
  - [x] Disk space check: verify `shutil.disk_usage(parent_dir).free` ≥ `1.1 * input_file_size + estimated_audio_bytes + 100 MB` (when audio conversion enabled; `1.1 * input_file_size + 100 MB` otherwise).
  - [x] `.mp4` Replace Original collision check: verify `<stem>.mkv` does not already exist before starting (fail safely if it does).
  - [x] Write unit tests for disk space boundary conditions and `.mp4` target collision guard.

- [x] **3.2 Stage 1: FFmpeg Audio Converter (`app/services/ffmpeg_runner.py`)**
  - [x] Verify mapped audio stream (`-map 0:a:<N>`) via `ffprobe` (confirm codec + channel count match `mkvmerge -J` selection) + `format.duration` fallback for `.mp4`.
  - [x] Derive `-ac` from source track channels capped at `6` (`min(source_channels, 6)`) — never upmix stereo.
  - [x] Run async subprocess writing `<same_dir>/<filename>.mkvforge.<eac3|ac3>` with `-progress pipe:1 -nostats`.
  - [x] Parse `out_time_us` (safely ignoring `N/A`) and `speed`; compute `percent = min(99, int((out_time_us / duration_us) * 100))`.
  - [x] Write unit tests for `ffprobe` verification, `-ac` capping, and progress/N/A parsing.

- [x] **3.3 Stage 2: Deterministic `mkvmerge` Command Builder (`app/services/mkv_builder.py`)**
  - [x] Build pure argument list (no shell interpolation):
    - `--gui-mode` and `-o <output_tmp>`.
    - Container metadata flags: `--title "<new_title>"` (or `""`), `--no-global-tags`, `--no-track-tags`, `--no-attachments`, `--no-chapters` (all per-file/per-track flags strictly before their input filename).
    - When Stage 1 ran: prepend `<temp_audio_file>` (file index `0`) with `--track-name 0:<title>`, `--language 0:<lang>`, `--default-track-flag 0:yes`, and `--sync 0:<delay_ms>` (`round(ns / 1_000_000)`) when source `minimum_timestamp` is non-zero.
    - Explicitly pass `--default-track-flag <id>:no` on existing audio tracks when adding a new Default Dolby track (prevents double-default audio tracks).
    - Emit `--no-video` / `--no-audio` / `--no-subtitles` whenever zero tracks of that type are kept (never empty track lists).
    - Apply `--video-tracks`, `--audio-tracks`, `--subtitle-tracks`, `--track-name`, `--language`, `--default-track-flag`, `--forced-display-flag`.
    - Build `--track-order` containing **only kept tracks** with accurate `<file_id>:<track_id>` indices.
  - [x] Write exhaustive unit tests covering all combinations (with/without Stage 1, dropped subtitles, dropped original audio, non-zero audio sync delay, double-default prevention, reordering).

- [x] **3.4 Stage 2 Runner, Exit Codes & Atomic Swap (`app/services/mkv_runner.py`)**
  - [x] Run async `mkvmerge`, parse `#GUI#progress (\d+)%` and `Progress: (\d+)%`, collect warnings/log lines.
  - [x] Exit codes: `0` = success, `1` = success-with-warnings (persist warnings in `jobs.warnings_json`), `2` = hard failure.
  - [x] Atomic output handling:
    - `.mkv` + *Replace Original*: `os.replace(tmp_path, original_path)`.
    - `.mp4` + *Replace Original*: re-verify `<stem>.mkv` doesn't exist, move `<stem>.mkv.mkvforge.tmp` → `<stem>.mkv`, delete original `.mp4` **only after** `<stem>.mkv` is safely in place, remove old `.mp4` DB row.
    - *Keep Both* (`.mkv` or `.mp4`): rename to auto-incremented `<stem>.remux.mkv` (`<stem>.remux.2.mkv`, ...).
  - [x] Re-probe final output file via `probe_single_file()` to refresh SQLite immediately.
  - [x] Guarantee `finally` cleanup of `<temp_audio_file>` and (on failure/cancel) `<output_tmp>`.
  - [x] Write unit + integration tests (including exit codes `0`/`1`/`2`, `.mp4` replace flow, Keep-Both auto-increment, and cleanup on error).

---

## Phase 4: Background Job Queue, Cancellation & Real-Time SSE Streaming

Goal: Sequential job execution, safe cancellation, crash recovery, and keepalive-protected live progress surviving mobile app switching.

- [x] **4.1 SSE Progress Broker (`app/services/sse_broker.py`)**
  - [x] Implement in-memory pub/sub broadcasting structured events `{ job_id, stage, stage_label, percent, speed, eta, log_line, status }`.
  - [x] Maintain rolling ring buffer of the last ~100 log lines + latest snapshot for the active job so reconnecting mobile clients immediately catch up.
  - [x] Configure `GET /api/jobs/stream` with `X-Accel-Buffering: no` and `ping=15` keepalive.

- [x] **4.2 Single-Worker Job Queue & Cancellation (`app/services/job_worker.py`)**
  - [x] Implement single background worker task pulling `queued` jobs from SQLite sequentially.
  - [x] Persist status transitions (`queued` → `running` → `completed` / `failed` / `cancelled`), warnings, and error messages in SQLite.
  - [x] Track active subprocess handle and per-job `cancel_event` (checked before Stage 1, between Stage 1 and Stage 2, and immediately before atomic file swap); on cancel (`DELETE /api/jobs/{job_id}`), mark queued jobs `cancelled` immediately, or send `SIGTERM` (5s fallback `SIGKILL`) to active subprocess and clean up `*.mkvforge.*` temp files.

- [x] **4.3 Startup Crash Recovery & Graceful Shutdown**
  - [x] On FastAPI lifespan startup: mark any stale `running` jobs in SQLite as `failed` ("Interrupted by container restart") and sweep `STORAGE_DIR` for orphaned `*.mkvforge.*` files; on shutdown, cleanly stop worker loop and terminate any active subprocess.

- [x] **4.4 Job APIs (`app/api/jobs.py`)**
  - [x] `POST /api/jobs` — Validate payload, enforce duplicate job guard (`409 Conflict` if `filepath` is already `queued`/`running`), insert into SQLite, wake worker.
  - [x] `GET /api/jobs` — Return active job, queued jobs, and recent history.
  - [x] `DELETE /api/jobs/{job_id}` — Cancel queued or active job.
  - [x] `GET /api/jobs/stream` — Live SSE endpoint.
  - [x] Write tests for queue sequencing, duplicate `409` guard, mid-job & between-stage cancellation + cleanup, startup crash recovery, `PRAGMA busy_timeout = 5000`, and SSE replay/events.

---

## Phase 5: Mobile-First Responsive Frontend (Dashboard, Library & Scanner)

Goal: Linear/Overseerr-inspired dark UI loading instantaneously from SQLite on phone and desktop, 100% offline.

- [x] **5.1 Design System, App Shell & Global Progress Pill**
  - [x] Implement dark palette (`#09090B` canvas, `#121215` cards, `#27272A` hairline borders, `#6366F1` accent, muted semantic badges), `100dvh`, `env(safe-area-inset-bottom)`, and bottom scroll clearance.
  - [x] Responsive navigation with hash routing (`#/dashboard`, `#/library?filter=...`, `#/jobs`) and `visibilitychange` mobile wake-up sync: slim left sidebar (`220px`) on desktop (≥ 768px); clean top header + thumb-friendly bottom tab bar (**Dashboard**, **Library**, **Jobs**) on mobile (< 768px).
  - [x] Global floating progress pill whenever a scan or job is active; tapping opens the active view / Live Job Drawer.

- [x] **5.2 Screen 1: Dashboard**
  - [x] Interactive stat cards (`Total Files`, `AAC 5.1 Only`, `Extra Metadata`, `Last Scan`).
  - [x] Quick Actions (`Fix N AAC 5.1 Only Files →`, `Clean N Files with Extra Metadata →`) jumping directly to filtered Library view.
  - [x] Recent Activity feed + clean Empty State (*"Run Initial Scan"*) when DB is empty.

- [x] **5.3 Screen 2: Library Browser & Scan Controls**
  - [x] Debounced search bar + horizontally scrollable filter chips (`All`, `AAC 5.1 Only`, `AAC 5.1`, `Has Metadata`, `No Dolby`, `Folder ▾`).
  - [x] Responsive list: stacked mobile cards (< 768px) and sortable desktop table (≥ 768px) with denormalized badges (`1080p`, `AAC 5.1`, `EAC3`, `Has Metadata`, `N Subs`) and per-file `↻ Re-scan` button with in-flight spinner/disabled state.
  - [x] Pagination controls + "Scan Library" (incremental) and "Full Re-scan" actions subscribed to `/api/scan/stream`.

---

## Phase 6: File Editor UI & Live Jobs Drawer

Goal: Progressive-disclosure MKVToolNix-style editor and two-tap AAC → EAC3/AC3 workflow from mobile.

- [x] **6.1 Default Editor Cards (Simple 80% Path)**
  - [x] Hash route `#/files/{id}` preserving previous `#/library?...` filters/search on back navigation + header with filename, size, and `Re-scan File` button.
  - [x] **Audio Fix Card** (indigo highlight when AAC 5.1 detected): Toggle ON/OFF, `[ EAC3 5.1 ] | [ AC3 5.1 ]`, Track Title input, live helper text.
  - [x] **Metadata Cleanup Card**: Container Title input with inline `× Clear` button + Strip Global Tags toggle (ON by default when junk metadata detected).
  - [x] **Tracks Card**: Include/Exclude checkbox, Track badge, Track Name, compact `↑` `↓` reorder buttons, plus live **`[NEW]` virtual preview row** at Audio Index 0 when Dolby conversion is toggled ON (with visual demotion/dimming of original AAC).

- [x] **6.2 Collapsible `▸ Advanced Settings` Accordion**
  - [x] Source audio track selector, Bitrate (`640k`/`448k`/`384k`), Track language override, *Drop original AAC 5.1* toggle.
  - [x] Deep stripping toggles: Track Tags, Attachments/Cover Art, Chapters.
  - [x] Per-track language, `Default`, and `Forced` flag controls.
  - [x] Output mode segmented control: `[ Replace Original ]` | `[ Keep Both (.remux.mkv) ]`.

- [x] **6.3 Sticky Bottom Bar, Guardrails & Jobs Drawer**
  - [x] Sticky bottom bar with 1-line pending changes summary (including `.mp4 → .mkv` replacement note) and **`Process File`** button.
  - [x] Client-side confirmation warning if all video tracks are unchecked; graceful `409 Conflict` banner linking to existing job in Jobs Drawer.
  - [x] **Jobs Drawer / Screen 4**: Active job card with Stage 1/2 badge, animated progress bar + speed & ETA, **Cancel Job** button (`DELETE /api/jobs/{id}`), collapsible **`▸ Show Live CLI Logs`** monospace viewer with smart auto-scroll (pauses when scrolled up + jump-to-latest), plus queued items and completed/failed history (showing `mkvmerge` exit code `1` warnings when present).

---

## Phase 7: Hardening, End-to-End Media Verification & Deployment

Goal: Prove reliability on real media files locally and in Docker behind Nginx Proxy Manager.

- [x] **7.1 Local End-to-End Media Test Matrix (Synthetic / Copy Media via `pytest`)**
  - [x] Generate small real test `.mkv` and `.mp4` fixtures via `ffmpeg`/`mkvmerge` (`tests/helpers/media_fixtures.py` & `scripts/verify_container_e2e.py`: AAC 5.1 + global metadata, multi-audio Hindi + English, stereo 2.0 audio, EAC3 → AAC `.mka`, embedded subtitle + cover art, non-zero audio sync delay).
  - [x] Verify EAC3 5.1 lands at Audio Index 0 (`Default=Yes`), original AAC 5.1 is demoted to Index 1 (`Default=No`), and container title/global tags are stripped (including `.mka` `-map_metadata -1` + `--no-global-tags` hardening).
  - [x] Verify multi-audio selects the *exact* chosen track (not always `0:a:0`) and stereo tracks are not upmixed to 5.1.
  - [x] Verify `.mp4` Replace Original outputs `<stem>.mkv`, removes `.mp4`, refuses to overwrite existing `<stem>.mkv`, and Keep Both auto-increments `.remux.2.mkv`.
  - [x] Verify mid-FFmpeg and mid-`mkvmerge` cancellation terminates immediately, deletes `*.mkvforge.*` temp files before returning, and leaves original untouched.

- [x] **7.2 Docker Build, Permissions & Proxy Verification (Separate Verification)**
  - [x] Build Docker image and confirm zero external CDN network requests in browser DevTools.
  - [x] Verify container runs as `PUID:PGID` (`1000:1000`), honors `UMASK`, and newly written files on `/mnt/usbmedia/media` have expected ownership/permissions (`uid=1000, gid=1000, mode=0o644`).
  - [x] Verify via Nginx Proxy Manager on mobile browser: live SSE updates stream smoothly without buffering, survive 60s+ idle periods via keepalive pings, and recover cleanly after locking/unlocking the phone screen.

---

## Phase 8: Live Filesystem Watcher & Non-Disruptive UI Auto-Refresh

Goal: Automatically detect added, modified, or deleted media files in `/storage` without waking spinning USB/HDD drives unnecessarily, corrupting in-progress downloads/copies, or disrupting users scrolling or editing in the UI.

- [x] **8.1 Configurable Watcher & Stability Debounce (`app/services/watcher.py`, `app/config.py`)**
  - [x] Add centralized configuration constants and env vars:
    - `MKVFORGE_WATCH_MODE` (`off` | `watch` | `poll`, default `watch` using Linux `inotify` via `watchfiles` with zero idle disk wake-ups; `poll` supports SMB/NFS/FUSE mounts where `inotify` is unavailable).
    - `MKVFORGE_WATCH_DEBOUNCE_SEC` (default `8s`) for write-completion stability verification.
    - `MKVFORGE_SCAN_INTERVAL_MIN` (default `0` = disabled) for optional periodic incremental fallback scan.
  - [x] Ignore temporary, partial, and non-media paths (`*.mkvforge.*`, `._*`, `*.part`, `*.!qB`, `*.tmp`, `*.crdownload`, `*.partial`, unsupported extensions).
  - [x] Implement stability debounce queue: when a file is created or modified, wait until `(size_bytes, mtime)` remain identical across two consecutive checks (`MKVFORGE_WATCH_DEBOUNCE_SEC` apart) before calling `probe_single_file(filepath)` (prevents probing half-copied movies or active torrent downloads).
  - [x] Suppress self-triggered events for any `filepath` currently owned by a `running` MKV Forge job, and defer watcher probing while a remux/FFmpeg job is actively writing to `/storage` to avoid USB I/O contention.
  - [x] On file deletion or move-out, remove only the single affected `media_files` row from SQLite (never trigger a full directory scan) and preserve the unmounted-storage safety guard.
  - [x] Wire clean startup and cancellation into FastAPI `lifespan` in `app/main.py` and expose watcher status in `GET /api/dashboard`.

- [x] **8.2 Live Library Change SSE Broadcast (`app/services/library_events.py`, `app/api/scan.py`)**
  - [x] Broadcast lightweight `library_changed` SSE events (`{ action: "added" | "updated" | "deleted" | "batch", count, filepaths, file_ids }`) whenever the watcher updates SQLite, coalescing rapid bursts (e.g., batch-copying a season folder) into a single debounced notification.
  - [x] Keep `X-Accel-Buffering: no` and `ping=15` keepalive semantics consistent with existing SSE streams (multiplexed cleanly onto `/api/scan/stream` so zero extra HTTP connections are opened).

- [x] **8.3 Non-Disruptive UI Auto-Refresh (`app/static/js/app.js`, `app/static/index.html`)**
  - [x] **Dashboard (`#/dashboard`):** Silently refresh stat cards and counts in real time when `library_changed` arrives + subtle `Live Watch` status badge.
  - [x] **Library (`#/library`):** Silently refresh the list only if the user is on page 1 at the top of the page; if scrolled down or on page 2+, keep the current scroll position untouched and display a subtle non-intrusive pill (`"N library changes detected on disk — Refresh"`) so cards/rows never jump mid-scroll.
  - [x] **File Editor (`#/files/{id}`):** Never reset or overwrite unsaved user edits when unrelated files change; only show a non-blocking inline banner (`"Reload File"` / `"Back to Library"`) if the currently open file itself changed externally.

- [x] **8.4 Unit & Integration Tests**
  - [x] Test stability debounce (rapidly growing file is deferred until `(size_bytes, mtime)` stabilizes; partial/temp extensions ignored).
  - [x] Test active-job echo suppression and I/O deferral while a job is `running`.
  - [x] Test single-file add, modify, rename, and delete DB synchronization without wiping unrelated rows (plus unmounted-storage guard).
  - [x] Test burst coalescing and SSE `library_changed` delivery, plus `MKVFORGE_WATCH_MODE=off` cleanly disabling the watcher.

---

## Phase 9: Library Explorer (Directory View + List View) & Safe File/Folder Operations

Goal: Transform the Library into a true Explorer-style Directory View (default) with toggleable flat All Files view, multi-select, and safe file/folder Rename, Move, and Delete operations that respect read-only mounts (`:ro` → `403 Forbidden`) and active job locks (`409 Conflict`).

- [x] **9.1 Repository & Query Extensions (`app/db/repositories.py`)**
  - [x] Add `direct_only` option on `MediaFileRepository.list_files()` (exact `folder = ?` for true Directory View vs. recursive prefix match) plus `get_folder_file_counts()`.
  - [x] Add transactional SQLite update helpers: `update_file_location()`, `update_folder_prefix()`, `delete_by_folder_prefix()`, and `JobRepository.get_active_job_in_folder()`.

- [x] **9.2 Safe File & Folder Operations Service (`app/services/file_ops_service.py`)**
  - [x] Implement single-file rename, single/multi-file move, and single/multi-file delete with active-job lock (`409`), strict storage jail (`400`), zero silent overwrites (`409`), and `:ro` / `PermissionError` / `EROFS` handling (`403 Forbidden` without mutating SQLite).
  - [x] Implement folder rename, folder move (blocking moving into self/descendant), and folder delete (blocking `/` root, requiring `recursive=True` for non-empty folders, active-job folder lock, and `:ro` → `403 Forbidden`).
  - [x] Broadcast thread-safe coalesced `library_changed` SSE events after every successful mutation.

- [x] **9.3 Explorer & File/Folder Ops APIs (`app/api/files.py`, `app/api/file_ops.py`, `app/api/folders.py`)**
  - [x] Extend `GET /api/files` with immediate `subfolders` and cumulative `folder_counts`.
  - [x] Add `POST /api/files/{id}/rename`, `POST /api/files/move`, `POST /api/files/delete`.
  - [x] Add `POST /api/folders/rename`, `POST /api/folders/move`, `POST /api/folders/delete` (and `:ro` → `403` on `POST /api/folders`).

- [x] **9.4 Responsive Explorer UI & Operations Modals (`app/static/index.html`, `app/static/js/app.js`, `app/static/js/api.js`)**
  - [x] View switcher **`[ Folders ]`** (default) | **`[ All Files ]`** persisted in URL hash + `localStorage` (Dashboard presets cleanly switch to All Files).
  - [x] Desktop (≥ 1024px) dual-pane explorer: left `+`/`−` Folder Tree pane + right Breadcrumbs, Subfolder rows, and Media File table with checkboxes & inline Rename/Move/Delete/Re-scan actions.
  - [x] Mobile (< 1024px) explorer: Breadcrumb bar + `↑ Up` + `Tree` picker button, Subfolder cards above File cards, and `Select` mode toggle for multi-select.
  - [x] Bulk Action Bar (`N selected`, `Move`, `Delete`, `Rename` when 1 selected) + Rename Modal, Unified `+`/`−` Folder Tree Move/Browse Picker, New Folder Modal, and explicit Delete Confirmation Modal.

- [x] **9.5 Exhaustive Unit & Integration Tests**
  - [x] Test file rename, single/batch move, single/batch delete, folder rename/move/delete, active-job `409` locks, overwrite `409` guards, traversal/root-delete `400` guards, moving folder into itself `400`, and simulated `:ro` (`EROFS`/`PermissionError`) returning `403 Forbidden` with untouched SQLite rows.

---

## Phase 10: Unified Track Actions Menu (Convert Audio + Import External Audio & Subtitles)

Goal: Replace the misleading `+ Add Audio ▾` button in the File Editor with a single unified Track Actions menu that cleanly separates **Convert Existing Audio** from **Import External Track (Subtitles & Audio)** — supporting imports from both `/storage` (via the `+`/`−` tree/file picker) and local device uploads.

- [x] **10.1 Staged Upload & External Track Discovery (`app/services/upload_service.py`, `app/api/uploads.py`, `app/api/folders.py`, `app/config.py`)**
  - [x] Centralize allowlisted external extensions and configurable upload size limits in `app/config.py`:
    - Subtitles: `.srt`, `.ass`, `.ssa`, `.vtt`, `.sup` (`MKVFORGE_MAX_SUB_UPLOAD_MB`, default `25`).
    - Audio: `.ac3`, `.eac3`, `.aac`, `.mka`, `.dts`, `.flac`, `.mp3`, `.opus`, `.m4a` (`MKVFORGE_MAX_AUDIO_UPLOAD_MB`, default `1024`).
  - [x] Implement `UploadService` + `POST /api/uploads/track` staging local uploads into `<DATA_DIR>/uploads/<uuid>.<ext>` (never writes to `/storage`, works even with `:ro` media mounts, returns clear `413` on size limit / proxy `client_max_body_size` note, validates extension and rejects traversal).
  - [x] Add `DELETE /api/uploads/{upload_id}` for discarding unsubmitted staged files and a startup crash-recovery sweep that removes orphaned staged uploads not referenced by any `queued`/`running` job.
  - [x] Extend `GET /api/folders` with optional `include_files=true&kind=subtitles|audio` so users can browse `/storage` using the existing folder browser to select existing subtitle or audio files directly (read-only safe).

- [x] **10.2 Multi-Input `mkvmerge` Builder & Pipeline Integration (`app/models/schemas.py`, `app/services/mkv_builder.py`, `app/services/mkv_runner.py`, `app/services/job_summary.py`)**
  - [x] Add `ExternalTrackConfig` (`id`, `kind: "audio" | "subtitles"`, `source: "storage" | "upload"`, `path` / `upload_id`, `track_name`, `language`, `default_track`, `forced_track`, `sync_delay_ms`) to `JobConfig` and validate storage jail / staged upload existence at `POST /api/jobs` time.
  - [x] Update `mkv_builder.py` to append each external track as `mkvmerge` input `2`, `3`, … with `--no-global-tags --no-track-tags --no-attachments --no-chapters`, `--track-name 0:...`, `--language 0:...`, `--default-track-flag 0:...`, `--forced-display-flag 0:...`, and `--sync 0:...` strictly preceding its input path.
  - [x] Place imported tracks at their exact user-ordered position in `--track-order` and enforce single-default-per-type across existing, converted, and imported tracks.
  - [x] Guarantee `finally:` cleanup of job-owned staged uploads after job completion, failure, or cancellation; surface `+ Imported Sub` / `+ Imported Audio` chips in `job_summary.py`.

- [x] **10.3 Unified Track Actions Menu & Import Track Modal (`app/static/index.html`, `app/static/js/editor.js`, `app/static/js/app.js`, `app/static/js/api.js`)**
  - [x] Replace `+ Add Audio ▾` in the Tracks card header with a single unified **`+ Add / Convert ▾`** menu containing two clearly labeled sections:
    1. **Convert Existing Audio:** target codec actions (`Convert Audio → EAC3 5.1`, `→ AC3 5.1`, `→ AAC 5.1`) opening the existing Base Audio picker modal.
    2. **Import External File:** `Import Subtitle…` and `Import Audio…` opening the Import Track modal.
  - [x] Build the responsive Import Track modal with source tabs (**From Storage** browsing folders + matching files | **Upload from Device** with file picker + live upload progress bar), Track Title, Language dropdown, Default/Forced flags, and Sync Delay (ms).
  - [x] Render imported tracks inline in the Tracks list with a distinct **`[IMPORT]`** badge, reorder (`↑`/`↓`), `Edit`, and `× Remove` (cleaning up discarded staged uploads), and wire them into the No-Op Submit Guard and Pre-Submit Summary.

- [x] **10.4 Unit & Integration Tests**
  - [x] Test upload validation (allowlists, size limits, traversal rejection, unsubmitted delete, crash-recovery orphan sweep) and `/api/folders?include_files=true&kind=...` filtering on `:ro` mounts.
  - [x] Test `mkv_builder.py` multi-input argument ordering, per-input tag stripping, `--sync`, `--track-order`, and double-default prevention across existing + converted + imported tracks.
  - [x] Test `job_summary.py` imported-track change chips, `finally:` staged-upload cleanup on finish/cancel/fail, and Node.js `editor.js` payload contract validation against Pydantic `JobCreateRequest`.

---

## Phase 11: Settings UI, Preferences & Database/Cache Maintenance (Manual + Scheduled)

Goal: Introduce a dedicated `#/settings` screen exposing Library & Editor defaults alongside safe, non-destructive Storage & SQLite maintenance actions (both manual one-tap and optional idle-only scheduled auto-cleanup).

- [x] **11.1 Expanded User Settings Schema & Persistence (`app/config.py`, `app/models/schemas.py`, `app/services/user_settings.py`, `app/api/settings.py`)**
  - [x] Extend `UserSettingsService` (`app_state` key `user_settings`) with validated defaults for:
    - **Library & Display:** `show_video_details` (`true`), `default_library_view` (`dir` | `list`, default `dir`), `default_folder_layout` (`list` | `grid`, default `list`), `page_limit` (`25` | `50` | `100`, default `50`).
    - **Processing & Editor Defaults:** `default_output_mode` (`keep_both` | `replace` | `save_as`, default `keep_both`), `default_dolby_codec` (`eac3` | `ac3`, default `eac3`), `default_dolby_bitrate` (`640k` | `448k` | `384k`, default `640k`), `default_strip_global_tags` (`false`), `preferred_language` (`eng`).
    - **Auto-Cleanup Schedule:** `auto_cleanup_enabled` (`true`), `auto_cleanup_interval_days` (`1` | `7` | `30`, default `7`), `job_history_retention_days` (`0` = never | `7` | `30` | `90`, default `30`), `auto_cleanup_temp` (`true`), `auto_cleanup_jobs` (`true`), `auto_cleanup_optimize_db` (`true`).
  - [x] Hydrate settings on `GET /api/dashboard` and support partial updates via `PATCH /api/settings` with strict Pydantic validation.

- [x] **11.2 Maintenance & Scheduled Cleanup Service (`app/services/maintenance_service.py`, `app/api/maintenance.py`, `app/main.py`)**
  - [x] Implement read-only `GET /api/maintenance/status` reporting DB size, WAL size, reclaimable freelist bytes (`PRAGMA page_count`, `PRAGMA freelist_count`, `PRAGMA page_size`), indexed files count, terminal job history count, orphaned staged upload count/bytes, stray `.mkvforge.*` count/bytes, and `last_auto_cleanup_at`.
  - [x] Implement manual maintenance endpoints (each rejecting with `409 Conflict` while a scan or remux/transcode job is actively running, and logging strictly PII-free metrics):
    - `POST /api/maintenance/optimize-db` — runs `PRAGMA wal_checkpoint(TRUNCATE)`, `VACUUM`, and `PRAGMA optimize`; reports `bytes_before`, `bytes_after`, `bytes_reclaimed`, `duration_ms`.
    - `POST /api/maintenance/clean-temp` — removes unreferenced files in `<DATA_DIR>/uploads` older than `ORPHAN_UPLOAD_MIN_AGE_SEC` (`3600s`) and stray `.mkvforge.*` files in `/storage` (only when zero jobs are running).
    - `POST /api/maintenance/prune-missing` — removes SQLite rows whose media files no longer exist on disk, guarded by a strict USB-unmount check (`409` if `/storage` is missing, has 0 entries, or 0 indexed files exist on disk while DB has rows), and emits a coalesced `library_changed` SSE event.
    - `POST /api/maintenance/prune-jobs` — deletes terminal (`completed`/`failed`/`cancelled`) job rows older than configured `job_history_retention_days` (never touches `queued`/`running` jobs).
    - `POST /api/maintenance/run-all` — runs safe temp cleanup → missing-file prune → job retention prune → DB optimize sequentially.
  - [x] Wire an idle-only background maintenance scheduler into FastAPI `lifespan` that checks `last_auto_cleanup_at` and runs enabled cleanup tasks only when no scan or job is active.

- [x] **11.3 Settings & Maintenance UI (`app/static/index.html`, `app/static/js/app.js`, `app/static/js/editor.js`, `app/static/js/api.js`, `app/static/js/constants.js`)**
  - [x] Add hash route `#/settings` accessible from a compact Settings gear button in the desktop sidebar footer and mobile top header.
  - [x] Wire Library & Editor defaults directly into Library pagination/view initialization and File Editor state creation (`createEditorState` + `+ Add / Convert` defaults).
  - [x] Build the responsive **Storage & Database Maintenance** card with live diagnostics, busy-state lock banner (when scan/job is active), schedule controls, and one-tap manual action buttons with inline spinners and space-reclaimed toasts.

- [x] **11.4 Unit & Integration Tests**
  - [x] Test `MaintenanceService`: `VACUUM` freelist reclamation, safe orphan upload age threshold & active-job upload preservation, stray `.mkvforge.*` cleanup, USB unmount protection on `prune_missing_files`, and job-retention pruning without touching active/queued jobs.
  - [x] Test `/api/maintenance/*` happy paths, `409 Conflict` while a job or scan is active, idle-only scheduler trigger, and expanded `PATCH /api/settings` validation & persistence.

---

## Phase 12: Appearance (Light / Dark / System + Theme Colors) & Language Foundation

Goal: Add full Light, Dark, and System theme support with curated accent color presets (zero FOUC, 100% offline CSS variables) and an offline localization foundation (`English` default).

- [x] **12.1 Semantic CSS Variable Palette & Accent Presets (`frontend/input.css`, `tailwind.config.js`)**
  - [x] Refactor hardcoded dark surface/accent hex tokens into semantic CSS custom properties (`--color-canvas`, `--color-surface`, `--color-hairline`, `--color-accent`, `--color-accent-hover`, `--color-accent-soft`) supporting `:root` (Light) and `html.dark` (Dark).
  - [x] Define 6 WCAG-AA contrast-checked accent themes via `data-accent`: **`indigo`** (default), **`blue`**, **`emerald`**, **`violet`**, **`rose`**, **`amber`**.
  - [x] Audit and adapt hardcoded `text-zinc-*` / `bg-zinc-*` utility pairs in `app/static/index.html` so Light Mode renders with crisp readability across tables, cards, modals, context menus, badges, and CLI log boxes.

- [x] **12.2 Zero-Flash Theme & Offline i18n Modules (`app/static/js/theme.js`, `app/static/js/i18n.js`, `app/static/index.html`, `app/static/js/app.js`)**
  - [x] Add `theme_mode` (`dark` | `light` | `system`, default `dark`), `accent_color` (`indigo` | `blue` | `emerald` | `violet` | `rose` | `amber`, default `indigo`), and `language` (`en`, default `en`) to `UserSettingsService` and `PATCH /api/settings`.
  - [x] Create dedicated `app/static/js/theme.js` managing `html.dark`, `data-accent`, `<meta name="theme-color">`, `prefers-color-scheme` live listener (when `system`), and `localStorage` cache + tiny synchronous `<head>` bootstrap snippet to prevent white flash before Alpine initializes.
  - [x] Create minimal, lightweight offline `app/static/js/i18n.js` (`en` default dictionary + `t(key)` helper) wired into Settings language selector (`English (en)`), keeping Phase 12 focused on clean Light/Dark contrast and accent CSS variables.
  - [x] Add the **Appearance & Language** card to `#/settings` with segmented `Dark | Light | System` control, interactive 6-color visual swatch picker, and Language selector.

- [x] **12.3 Unit & Integration Tests**
  - [x] Test `theme.js` mode resolution (`dark`, `light`, `system` with mocked `matchMedia`), accent validation/fallback to `indigo`, and `<meta name="theme-color">` sync.
  - [x] Test `i18n.js` key lookup, fallback behavior on missing/unknown locales, and `PATCH /api/settings` validation rejecting invalid theme modes, accent colors, or languages.
  - [x] Verify compiled `app.css` contains CSS variable definitions for both light/dark surfaces and all 6 accent themes with zero external URLs.

---

## Phase 13: Optional Local Accounts, First-Run Onboarding & Job User Attribution

Goal: Add lightweight, 100%-offline local user management with zero-friction opt-in onboarding (create local account OR continue without authentication using a built-in `Anonymous` user), browser-remembered sessions, flat peer accounts (all users equal, no RBAC restrictions), hybrid per-user/system settings, and user attribution displayed on every job in History & Dashboard Recent Activity.

- [x] **13.1 SQLite Schema Migrations & Auth Repository (`app/db/connection.py`, `app/db/repositories.py`)**
  - [x] Add idempotent migrations creating:
    - `users` (`id`, `username` UNIQUE lowercase, `display_name`, `password_hash`, `role` `DEFAULT 'admin'` — future-proofed column while all local accounts act as equal peers with no RBAC restrictions, `is_anonymous`, `preferences_json`, `created_at`).
    - `sessions` (`token_hash` PK, `user_id` FK ON DELETE CASCADE, `expires_at`, `created_at`, `last_seen_at`).
  - [x] Add `created_by_user_id` and `created_by_name` (`DEFAULT 'Anonymous'`) columns to `jobs` (backfilling existing history rows to `'Anonymous'`).
  - [x] Persist `auth_mode` (`unconfigured` | `anonymous` | `local`) in `app_state` and seed the built-in `Anonymous` user record on demand.

- [x] **13.2 Local Auth Service, Session Cookies & Route Protection (`app/services/auth_service.py`, `app/api/auth.py`, `app/main.py`, `app/config.py`)**
  - [x] Implement stdlib-only `scrypt` password hashing (`hashlib.scrypt` + per-user random salt + `hmac.compare_digest` constant-time verification — zero extra pip dependencies).
  - [x] Issue `HttpOnly`, `SameSite=Lax` session cookies (`mkvforge_session`, SHA-256 hashed in SQLite, 30-day sliding expiry) with `Secure` derived dynamically from `request.url.scheme == "https"` or `X-Forwarded-Proto == "https"` (never hardcoded `Secure=True`, ensuring internal HTTP reverse-proxy logins work reliably).
  - [x] Add auth API endpoints:
    - `GET /api/auth/status` — returns `{ auth_mode, authenticated, needs_setup, user }`.
    - `POST /api/auth/setup` — first-run choice: either create initial local account (`mode: "local"`) OR enable passwordless mode (`mode: "anonymous"`).
    - `POST /api/auth/login` & `POST /api/auth/logout`.
    - `GET /api/auth/users`, `POST /api/auth/users`, `PATCH /api/auth/password`, `DELETE /api/auth/users/{id}`, and `POST /api/auth/disable` (switch back to anonymous mode with password verification). All signed-in users are equal peers; guard prevents deleting the last remaining local account while `auth_mode == "local"`.
  - [x] Enforce auth middleware ONLY when `auth_mode == "local"` (public allowlist: `/`, `/static/*`, `/api/health`, `/api/auth/status`, `/api/auth/login`, `/api/auth/setup`; all other `/api/*` + `/api/events/stream` return `401` when unauthenticated). In `anonymous` / `unconfigured` mode, every request resolves transparently to the built-in `Anonymous` user.
  - [x] Keep HTTP & auth logs strictly PII-free (log `uid` and `auth_mode` only, never usernames, display names, passwords, or tokens).

- [x] **13.3 Job Attribution & Hybrid Per-User / System Settings (`app/api/jobs.py`, `app/api/dashboard.py`, `app/api/settings.py`, `app/services/user_settings.py`, `app/services/job_worker.py`, `app/models/schemas.py`)**
  - [x] Stamp `created_by_user_id` and `created_by_name` on every job at `POST /api/jobs` enqueue time from the resolved current user (`Anonymous` or signed-in user).
  - [x] Expose `created_by_name` on `JobItemResponse` and `RecentJobItem` so **Jobs History** and **Dashboard Recent Activity** show a clean attribution badge (`by <Name>`).
  - [x] Implement **Hybrid Settings** scoping transparently through `GET /api/settings` and `PATCH /api/settings`:
    - **Per-User (`users.preferences_json`):** Appearance & Display (`theme_mode`, `accent_color`, `font_scale`, `language`, `show_video_details`, `default_library_view`, `default_folder_layout`, `page_limit`) and Personal Editor/UX defaults (`default_output_mode`, `default_dolby_codec`, `default_dolby_bitrate`, `default_strip_global_tags`, `preferred_language`, `confirm_cancel_job`).
    - **System-Wide (`app_state`):** Storage & Scheduled Auto-Cleanup (`auto_cleanup_enabled`, `auto_cleanup_interval_days`, `job_history_retention_days`, `auto_cleanup_temp`, `auto_cleanup_jobs`, `auto_cleanup_optimize_db`).
    - In `anonymous` mode, all keys read/write to `app_state` as before; in `local` mode, `GET /api/settings` merges `{ ...system_settings, ...current_user.preferences }` and `PATCH /api/settings` routes each key to its respective scope.

- [x] **13.4 First-Run Onboarding Modal, Login Screen & Account Settings UI (`app/static/index.html`, `app/static/js/app.js`, `app/static/js/api.js`)**
  - [x] **First-Run Onboarding Modal** (`needs_setup === true`): clean two-option card offering **Create Local Account** vs. **Continue Without Sign-In** (one tap, never shown again once chosen).
  - [x] **Sign-In Screen** (`auth_mode === "local" && !authenticated`): centered responsive login card with `<form autocomplete="on">` for native browser password manager & autofill support, blocking data/SSE calls until signed in.
  - [x] **`Settings → Account` Section**:
    - In anonymous mode: shows `"Running without authentication (Anonymous)"` + **"Enable Sign-In & Create Account"** flow.
    - In local auth mode: shows current user, **Change Password**, **Manage Local Users** (add/remove peer homelab users while preventing deletion of the last remaining local user), **Sign Out**, and **Switch to No-Auth Mode** (password-confirmed).
  - [x] Display `by <User>` pills in Jobs Queue, Jobs History, and Dashboard Recent Activity.

- [x] **13.5 Exhaustive Unit & Integration Tests**
  - [x] Test `scrypt` hashing/verification, wrong-password rejection, sliding session expiry, revoked/tampered cookie rejection, and zero PII in logs.
  - [x] Test first-run setup flows (`anonymous` vs `local`), idempotency (`409` if already configured), upgrading from `anonymous` → `local`, downgrading `local` → `anonymous` (requires valid password), and last-user deletion guard (`400`/`409`).
  - [x] Test route protection (`401` on protected APIs & `/api/events/stream` when `auth_mode == "local"` without cookie; `200` with valid session cookie; `200` without cookie when `auth_mode == "anonymous"`).
  - [x] Test hybrid settings isolation (User A changing `theme_mode` / `font_scale` / `confirm_cancel_job` does not affect User B, while `auto_cleanup_*` / `job_history_retention_days` updates apply system-wide).
  - [x] Test job user attribution (`created_by_name` persists on `POST /api/jobs`, survives user deletion, and surfaces in `GET /api/jobs` and `GET /api/dashboard`).

---

## Phase 14: Generic Audiophile Audio Conversion Matrix & Progressive-Disclosure UI

Goal: Evolve MKV Forge's hardcoded 5.1 Dolby converter into a capability-driven, audiophile-grade multi-codec audio conversion engine (`E-AC-3`, `AC-3`, `AAC`, `Opus`, `FLAC`) with user-selectable channel matching/downmixing (`Match Source`, `7.1`, `6.1`, `5.1`, `2.0`, `1.0`), per-`(codec, channel)` smart bitrate ladders, FFmpeg audiophile safeguards (`.mka` priming-delay preservation, `-drc_scale 0`, `libopus` layout normalization), and a clean **Progressive-Disclosure UI** (smart source-aware defaults collapsed by default; full audiophile controls 1 tap away).

- [x] **14.1 Backend Audio Capability Matrix & Schema Contract (`app/config.py`, `app/models/schemas.py`)**
  - [x] Define a centralized `AUDIO_CODEC_CAPABILITIES` constant matrix in `AppConstants`:
    - `eac3`: encoder `eac3`, max channels `6` (5.1), temp ext `.eac3`, sample rates `{48000, 44100, 32000}` (fallback `48000`), lossy, per-channel bitrates (including `768k` for 5.1).
    - `ac3`: encoder `ac3`, max channels `6` (5.1), temp ext `.ac3`, sample rates `{48000, 44100, 32000}` (fallback `48000`), lossy, max bitrate `640k`.
    - `aac`: encoder `aac`, max channels `8` (7.1), temp ext `.mka`, lossy, per-channel bitrates (`96k`–`768k`).
    - `opus`: encoder `libopus`, max channels `8` (7.1), temp ext `.mka`, fixed sample rate `48000`, `-mapping_family 1` when `channels > 2`, lossy, per-channel bitrates (`64k`–`512k`).
    - `flac`: encoder `flac`, max channels `8` (7.1), temp ext `.mka`, lossless (`requires_lossless_source = True`), no bitrate, preserves native bit depth (`s16`/`s32`) and sample rate.
  - [x] Add `ALLOWED_TARGET_CODECS = ("eac3", "ac3", "aac", "opus", "flac")`, expanded `BITRATE_BPS_MAP` (`64k`, `96k`, `128k`, `160k`, `192k`, `256k`, `320k`, `384k`, `448k`, `512k`, `640k`, `768k`), and `CHANNEL_LAYOUT_MAP` (`1 → mono`, `2 → stereo`, `3 → 3.0`, `4 → 4.0`, `6 → 5.1`, `7 → 6.1`, `8 → 7.1`).
  - [x] Extend `AudioConversionConfig` in `app/models/schemas.py`:
    - Expand `target_codec` to `Literal["eac3", "ac3", "aac", "opus", "flac"]`.
    - Make `bitrate: str | None` optional (ignored/omitted for `flac`, validated against codec's allowed bitrate set for lossy codecs; rejects e.g. `768k` on `ac3`).
    - Add `target_channels: int | None = Field(default=None, ge=1, le=8)` (`None` = match source capped at `codec.max_channels`).
    - Add model validator enforcing: no upmixing when `source_channels` is known, `target_channels <= codec.max_channels`, and rejecting `flac` when `source_codec` is clearly lossy unless explicitly overridden.

- [x] **14.2 Audiophile Stage 1 FFmpeg Runner, Pre-Flight Sizing & Job Summary (`app/services/ffmpeg_runner.py`, `app/services/preflight.py`, `app/services/job_worker.py`, `app/services/job_summary.py`, `app/utils/paths.py`)**
  - [x] Replace `compute_target_channels(source_channels)` with codec-aware `compute_target_channels(source_channels, target_codec, requested_channels=None)` enforcing `1 <= resolved <= min(source_channels, codec.max_channels)` (never upmixes).
  - [x] Update `resolve_target_sample_rate(source_sample_rate, target_codec)` to return `48000` for `opus`, Dolby-safe fallback `48000` for `eac3`/`ac3`, and `None` (preserve native rate) for `aac` and `flac`.
  - [x] Update `build_ffmpeg_command(...)`:
    - Pass `-drc_scale 0` **before** `-i` whenever `source_codec` is `ac3` or `eac3` to preserve full native dynamic range on decode.
    - Select FFmpeg encoder (`libopus` for `opus`, etc.) and output container extension (`.mka` via `-f matroska` for `aac`, `opus`, `flac` so exact priming delay / Opus pre-skip is preserved; `.eac3`/`.ac3` for Dolby).
    - Apply canonical `-channel_layout` (`7.1`, `6.1`, `5.1`, `4.0`, `3.0`, `stereo`, `mono`) and `-mapping_family 1` for `libopus` when `target_channels > 2`.
    - Omit `-b:a` for `flac` and never downgrade sample format (preserves 24-bit TrueHD/DTS-HD MA).
  - [x] Update `preflight.py` disk space estimation to handle `flac` (conservative lossless ceiling based on `duration_us * target_channels * 48000 * 3 bytes`) and all new lossy bitrates up to `768k`.
  - [x] Update `job_worker.py` & `job_summary.py` to pass `target_channels`, `source_codec`, and format clean stage/summary pills (e.g. `Encoding FLAC 7.1`, `Encoding Opus 7.1 • 448k`, `Encoding EAC3 5.1 • 768k`).

- [x] **14.3 Frontend Capability Engine & Pure Helpers (`app/static/js/constants.js`, `app/static/js/editor.js`, `app/static/js/app.js`)**
  - [x] Extract dedicated audio capability constants into `constants.js` (`TARGET_CODEC_META`, per-channel bitrate ladders, channel layout labels `7.1 / 6.1 / 5.1 / 4.0 / 3.0 / 2.0 / 1.0`, lossless source codec set, Atmos/DTS:X detection patterns).
  - [x] Update `editor.js` pure helpers (zero magic numbers):
    - `formatChannelLabel(channels)` returning exact labels (`8 → 7.1`, `7 → 6.1`, `6 → 5.1`, `4 → 4.0`, `3 → 3.0`, `2 → 2.0`, `1 → 1.0`).
    - `isLosslessAudioTrack(track)` and `hasObjectAudioMetadata(track)` (Atmos / JOC / DTS:X detection).
    - `getRecommendedAddAudioOptions(editorState)` returning the top 2–3 source-aware recommendations + `Custom conversion…` trigger.
    - `getAvailableTargetCodecs(sourceTrack)` (includes `flac` only for lossless sources) and `getAvailableChannelOptions(sourceTrack, targetCodec)` (`Match (<src>)` + valid downmixes, never upmixing).
    - `getBitrateOptionsFor(targetCodec, targetChannels)` returning `{ options, defaultBitrate }` (or `null` for lossless `flac`).
    - `getConversionNotices(sourceTrack, targetCodec, targetChannels)` returning structured `{ severity, message }` warnings (Atmos strip, 7.1→5.1 Dolby cap, AC-3→E-AC-3 sidegrade, same-codec re-encode, downmix info, Opus/FLAC HDMI/eARC LPCM note).
    - Update `buildDefaultAudioTitle(...)` & `buildJobRequestPayload(...)` to include `target_channels` and omit `bitrate` for `flac`.

- [x] **14.4 Progressive-Disclosure Convert Audio Modal & Dropdown UI (`app/static/index.html`)**
  - [x] Update `+ Add / Convert` dropdown to show the 2–3 dynamic source-aware recommendations (with `Recommended` / `Lossless` pills) plus a subtle `Custom conversion…` option.
  - [x] Refactor the **Convert Audio** modal for clean progressive disclosure:
    - Always-visible compact controls: Source Track selector, dynamic **Contextual Warning/Info Banner** (hidden when optimal), single collapsible **`▸ Advanced`** bar displaying live summary (`E-AC-3 · Match (5.1) · 640k · eng` or `FLAC · Match (7.1) · Lossless · eng`), Track Title input, `Set as default`, and `Drop original source track`.
    - Expanded **`▾ Advanced`** panel: responsive `flex-wrap` pills for **Format**, **Channels**, **Bitrate** (auto-hidden for `FLAC`), and **Language** selector.
    - Auto-expand Advanced when launched via `Custom conversion…`; stay collapsed by default when launched from a quick recommendation.
  - [x] Update the inline `[NEW]` virtual preview row in the Tracks card to display exact output layout & `Lossless` vs bitrate badge (e.g. `FLAC 7.1 • Lossless • eng • muxed`).

- [x] **14.5 Exhaustive Unit & Integration Tests**
  - [x] **Backend unit tests (`tests/unit/test_ffmpeg_runner.py`, `tests/unit/test_preflight.py`, `tests/unit/test_job_summary.py`):**
    - Positive tests for all 5 codecs (`eac3`, `ac3`, `aac`, `opus`, `flac`) across `7.1`, `6.1`, `5.1`, `4.0`, `3.0`, `2.0`, `1.0`.
    - Verify `.mka` temp output for `aac`/`opus`/`flac`, `-mapping_family 1` + `-ar 48000` for `libopus` > 2ch, `-drc_scale 0` before `-i` for `ac3`/`eac3` sources only, `-b:a` omitted for `flac`, and `768k` accepted on `eac3` but rejected on `ac3`.
    - Negative & edge cases: upmix rejection (`2.0 → 5.1`), `eac3`/`ac3` > 6ch rejection (`target_channels=8`), invalid bitrates, lossy→`flac` validation, `null`/0 channel inputs.
  - [x] **Frontend pure logic & UI tests (`tests/unit/test_audio_matrix.py`, `tests/unit/test_index_html_structure.py`):**
    - Node VM tests verifying `formatChannelLabel` (`7.1`/`6.1`/`5.1`/`4.0`/`3.0`/`2.0`/`1.0`), `getRecommendedAddAudioOptions` across AAC 5.1 / TrueHD 7.1 / EAC3 5.1 / Stereo 2.0, `FLAC` hidden for lossy sources, `getConversionNotices` for every warning condition (Atmos, 7.1→5.1 Dolby cap, AC-3→E-AC-3 sidegrade, downmix, same-codec), and `buildJobRequestPayload` serialization.
    - HTML tag balance + progressive-disclosure modal landmarks (`advancedOpen` / disclosure summary bar / warning banner).
  - [x] **End-to-end API integration tests (`tests/integration/test_phase14_audio_conversion.py`):**
    - Verify `POST /api/jobs` accepts valid `opus 7.1`, `flac 7.1`, `eac3 5.1 @ 768k`, and `aac 2.0` downmix payloads and rejects invalid combinations with `422`.

---

## Phase 15: 3-Step Apple-Style Onboarding & Global Playback Profile

Goal: Eliminate hardcoded AAC/Dolby assumptions across the Dashboard and Library by introducing a **global household Playback Profile** (`app_state` / `SYSTEM_SETTING_KEYS`) paired with a frictionless, Apple-grade **3-Step First-Run Onboarding Wizard** (Access → Sound System → Start Scan) and a dedicated **Sound System** Settings panel—while never interrupting existing production installs and preventing stereo-commentary false negatives.

- [x] **15.1 Pure Playback Profile Engine & Constants (`app/services/playback_profile.py`, `app/config.py`, `app/models/schemas.py`, `app/services/user_settings.py`)**
  - [x] Create dedicated `app/services/playback_profile.py` with centralized presets (`dolby_arc`, `optical_legacy`, `earc_avr`, `streaming_clients`, `stereo_mobile`, `custom`, `neutral`) whose default target codecs are strictly within `("eac3", "ac3", "aac")`.
  - [x] Implement `classify_track_codec_key(codec, channels)` (separating `aac_20` vs `aac_51` and `dts_hd_ma` vs `dts`) and `evaluate_file_needs_conversion(audio_tracks, profile)` enforcing the **anti-stereo-commentary multichannel guard** (if `max_channels > 2` and file has multichannel tracks, require at least one compatible multichannel track).
  - [x] Extend `AppConstants.SYSTEM_SETTING_KEYS`, `UserSettings`, `UserSettingsUpdate`, and `UserSettingsService` so `playback_profile_preset`, `playback_supported_codecs`, `playback_max_channels`, and `playback_preferred_codec` persist globally across all household users while UI preferences stay per-user.
  - [x] Extend `AuthStatusResponse` (`storage_path`, `storage_writable`), `DashboardStats` (`needs_conversion`, `playback_profile_configured`, `playback_profile_label`, `playback_profile_preset`), `LibraryFacets` (`needs_conversion`), and `MediaFileListItem` (`needs_conversion`).

- [x] **15.2 Zero-Rescan SQLite Compatibility Filtering & API Wiring (`app/db/repositories.py`, `app/api/dashboard.py`, `app/api/files.py`, `app/api/auth.py`)**
  - [x] Evaluate `needs_conversion` dynamically from cached `audio_summary_json` against the active global `PlaybackProfile` across `GET /api/dashboard` and `GET /api/files?needs_conversion=1` (zero re-scan, zero schema migration, preserving backward-compatible legacy query aliases).
  - [x] Expose mounted `storage_path` and `storage_writable` on `GET /api/auth/status` for Step 3 verification.

- [x] **15.3 Pure Frontend Playback Profile Module & Editor Integration (`app/static/js/playback_profile.js`, `app/static/js/constants.js`, `app/static/js/editor.js`, `app/static/js/app.js`)**
  - [x] Create `app/static/js/playback_profile.js` exporting preset definitions, codec labels, summary formatters, and profile-aware target codec prioritization (strictly within `eac3`, `ac3`, `aac` for preset defaults).
  - [x] Wire `editor.js` `getRecommendedAddAudioOptions(editorState, playbackProfile)` to respect the household's Playback Profile when configured.
  - [x] Wire 3-step onboarding controller state (`onboardingWizardStep`, `onboardingDirection`, `onboardingAuthChoice`, `onboardingProfileDraft`, lossless Back/Next, atomic Step 3 commit: `setupAuth → patchUserSettings → optional triggerScan → Dashboard`), Settings Playback Profile handlers, and profile-driven `needsConversion` Library filter state in `app.js`.

- [x] **15.4 Apple-Style Animated 3-Step Onboarding UI, Settings Panel & Generic Dashboard/Library (`app/static/index.html`, `app/static/css/app.css`)**
  - [x] Add smooth directional slide+fade transition animations (`.onboarding-step-fwd`, `.onboarding-step-back`) with `prefers-reduced-motion: reduce` support in `app/static/css/app.css`.
  - [x] Replace single-step first-run modal in `index.html` with the responsive 3-step Onboarding Wizard (only shown when `auth_mode === "unconfigured"`).
  - [x] Add dedicated **Sound System (Playback Profile)** Settings section so users can configure or change presets/custom codecs/max channels anytime.
  - [x] Replace hardcoded Dashboard `"AAC 5.1 Only"` KPI card and Library `"No Dolby"` chip with profile-driven **`Needs Conversion`** (or a 1-tap `Set Up Sound System` prompt when `neutral`).

- [x] **15.5 Exhaustive Unit & Integration Tests**
  - [x] Unit tests (`tests/unit/test_playback_profile.py`) covering all 7 presets, `aac_20` vs `aac_51` splitting, anti-stereo-commentary multichannel false-negative protection, `2.0 / 5.1 / 7.1` channel ceilings, `neutral` mode, malformed/null inputs, and Node VM tests for `playback_profile.js` + `app.js` 3-step wizard transitions.
  - [x] Integration tests (`tests/integration/test_phase15_onboarding_and_profile.py`) covering global household profile persistence across 2 local users, Dashboard `needs_conversion` counts, `GET /api/files?needs_conversion=1` filtering with stereo-commentary edge cases, Step 1–3 onboarding flow on fresh install vs zero interruption on existing configured installs, and HTML tag balance + landmarks.





