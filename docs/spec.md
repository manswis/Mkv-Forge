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

### 3.3 Capability-Driven Audio Conversion Matrix (Phase 14)

* Unified **Convert Audio** workflow inside the Editor's `+ Add / Convert` menu (auto-highlighting optimal recommendations for the file's actual source tracks, with a progressive-disclosure **Advanced** panel for power users):
  * **Supported Target Codecs & Hardware/Encoder Limits:**
    * **`eac3` (Dolby Digital Plus):** FFmpeg `eac3` encoder, max **5.1 (6 ch)** *(FFmpeg does not implement E-AC-3 dependent substreams required for 7.1)*, sample rates `{48000, 44100, 32000}` (fallback `48000`). Bitrates by layout: 5.1 → `448k / 640k (default) / 768k`; 3.0–4.0 → `256k / 384k (default) / 448k`; 2.0 → `192k / 256k (default) / 320k`; 1.0 → `96k / 128k (default) / 192k`.
    * **`ac3` (Dolby Digital):** FFmpeg `ac3` encoder, max **5.1 (6 ch)** *(ATSC A/52A spec limit)*, max bitrate **`640k`**, sample rates `{48000, 44100, 32000}` (fallback `48000`). Bitrates by layout: 5.1 → `384k / 448k (default) / 640k`; 3.0–4.0 → `256k / 384k (default) / 448k`; 2.0 → `192k / 256k (default) / 320k`; 1.0 → `96k / 128k (default) / 192k`.
    * **`aac` (Universal AAC):** FFmpeg `aac` encoder, max **7.1 (8 ch)**. Bitrates scaled to avoid native-AAC low-bitrate artifacts: 7.1 → `512k / 640k (default) / 768k`; 6.1 → `448k / 512k (default) / 640k`; 5.1 → `384k / 448k (default) / 512k`; 3.0–4.0 → `256k / 320k (default) / 384k`; 2.0 → `192k / 256k (default) / 320k`; 1.0 → `96k / 128k (default) / 192k`.
    * **`opus` (Modern High-Efficiency):** FFmpeg `libopus` encoder, max **7.1 (8 ch)**, native `48000 Hz`, explicit `-mapping_family 1` for multichannel (`> 2 ch`). Bitrates by layout: 7.1 → `384k / 448k (default) / 512k`; 6.1 → `320k / 384k (default) / 448k`; 5.1 → `256k / 320k (default) / 384k`; 3.0–4.0 → `192k / 256k (default) / 320k`; 2.0 → `128k / 192k (default) / 256k`; 1.0 → `64k / 96k (default) / 128k`.
    * **`flac` (Lossless PCM):** FFmpeg `flac` encoder, max **7.1 (8 ch)**, **only offered for lossless sources** (`TrueHD`, `DTS-HD MA`, `FLAC`, `PCM`) to prevent lossy→lossless file bloat. No bitrate parameter; preserves native bit depth (`16-bit` / `24-bit`) and sample rate untouched.
  * **Channel Selection (`target_channels`):**
    * Defaults to **`Match Source`** (`target_channels = None` → `min(source_channels, codec.max_channels)`).
    * Supports explicit downmix selection (`7.1`, `6.1`, `5.1`, `4.0`, `3.0`, `2.0`, `1.0`) strictly bounded by `1 <= target_channels <= min(source_channels, codec.max_channels)` — **never upmixes**.
    * Exact channel layout labels (`8 → 7.1`, `7 → 6.1`, `6 → 5.1`, `4 → 4.0`, `3 → 3.0`, `2 → 2.0`, `1 → 1.0`) drive auto-generated track titles (e.g. `FLAC 7.1 • eng • muxed`, `EAC3 5.1 • 768k • eng • muxed`, `AAC 2.0 • 256k • eng • muxed`).
  * **Placement & Default Flag Demotion:** Automatically inserted at **Audio Index 0** with `--default-track-flag 0:yes`, while explicitly clearing the default flag (`--default-track-flag <id>:no`) on existing audio tracks (unless overridden) so players never see double default audio tracks. User may optionally drop the original source track.

### 3.3.1 Global Playback Profile & 3-Step First-Run Onboarding (Phase 15)

* **Global Playback Profile (`app_state` / `SYSTEM_SETTING_KEYS`):**
  * MKV Forge operates on a single shared household media library, so the active **Playback Profile** (`playback_profile_preset`, `playback_supported_codecs`, `playback_max_channels`, `playback_preferred_codec`) is persisted **system-wide** in `app_state['user_settings']` (while UI preferences like theme, accent, font scale, and pagination remain per-user).
  * **Hardware Presets (all default target codecs strictly within `eac3`, `ac3`, `aac`):**
    * `dolby_arc` (*Soundbar / Smart TV · HDMI ARC*): supported `["eac3", "ac3", "aac_20"]`, max `6` (5.1), preferred target `"eac3"`.
    * `optical_legacy` (*Optical Soundbar / Legacy AVR · TOSLINK*): supported `["ac3", "aac_20"]`, max `6` (5.1), preferred target `"ac3"`.
    * `earc_avr` (*Home Theater AVR · HDMI eARC 7.1 / Atmos*): supported `["truehd", "dts_hd_ma", "eac3", "ac3", "dts", "flac", "pcm", "aac_20"]`, max `8` (7.1), preferred target `"eac3"`.
    * `streaming_clients` (*Plex / Jellyfin / Infuse · Apple TV, Shield, Web*): supported `["eac3", "ac3", "aac_20", "aac_51", "opus", "flac"]`, max `8` (7.1), preferred target `"aac"`.
    * `stereo_mobile` (*Stereo / Headphones / Mobile · 2.0*): supported `["aac_20", "opus", "ac3", "eac3", "mp3"]`, max `2` (2.0), preferred target `"aac"`.
    * `custom`: User-customized codec list, max channels (`2`, `6`, `8`), and preferred target codec (`eac3`, `ac3`, `aac`).
    * `neutral`: Unconfigured / skipped profile — never flags files as needing conversion until the user configures a sound system in Onboarding or Settings.
  * **Anti-Stereo-Commentary False-Negative Compatibility Rule:**
    * Splits `aac_20` (AAC `<= 2ch`) from `aac_51` (AAC `> 2ch`) and `dts_hd_ma` from lossy `dts`.
    * A track is compatible iff its normalized codec key is in `supported_codecs` **and** `channels <= max_channels` (when known).
    * **Multichannel Guard:** Whenever `max_channels > 2` and a file contains at least one multichannel audio track (`channels > 2`), the file is compatible **only if at least one multichannel track is compatible** — a stereo `2.0` commentary track can never mask an incompatible `5.1`/`7.1` main track.
    * Evaluated dynamically from cached `audio_summary_json` in SQLite (zero re-scan when changing sound systems).
* **3-Step First-Run Onboarding Wizard (`auth_mode == "unconfigured"` only):**
  * **Existing configured installs (`auth_mode != "unconfigured"`) are NEVER interrupted** on upgrade; they configure or change their Playback Profile anytime in Settings.
  * **Step 1 — Access & Authentication:** Two-card progressive choice between *Create Admin Account* (`local`) and *Continue Without Sign-In* (`anonymous`), with client-side validation and lossless Back/Next navigation.
  * **Step 2 — Sound System / Playback Profile:** Hardware preset cards + collapsible *Customize codecs & channels* + *Skip for now — configure later in Settings* (`neutral`).
  * **Step 3 — Initialize & Start Scan:** Confirmation summary + storage mount path/write-status check + mandatory CTA *Start Scanning Library* (commits auth + playback profile + triggers non-blocking `POST /api/scan` and lands directly on Dashboard).
* **Factory Reset (`POST /api/maintenance/factory-reset`):**
  * Located in a Danger Zone card inside **Settings → Storage & Database Maintenance**; never touches any media file inside `STORAGE_DIR`.
  * **Guardrails:** Refuses with `409 Conflict` if any scan or conversion job is active; in `local` auth mode requires admin role (`403` otherwise) and verified current password; in `anonymous` mode requires `confirm_text == "RESET"`.
  * **Physical Rebuild:** Acquires `op_lock`, pauses background loops (`maintenance_scheduler`, `library_watcher`, `job_worker`), unlinks `mkvforge.db` / `-wal` / `-shm`, clears contents inside `data/uploads/` while keeping the directory intact, calls `db.initialize()`, resets in-memory `job_broker` and `library_broker` buffers, resumes background loops, clears the browser session cookie + client `localStorage`/`sessionStorage`, and lands automatically on Onboarding Step 1.

### 3.3.2 Speaker & Audio System Test Generator (Phase 16)

* **Purpose:** Synchronous (`POST /api/tools/generate-test-media`, ~1–3s) generator enabling users to verify AVR, soundbar, TV, and DAC decoding and speaker wiring without any bundled sample files or external font dependencies.
* **Supported Formats & Speaker Configurations:**
  * **Output Modes:** `Video + Audio (.mkv)` *(720p dark room `drawbox` speaker diagram lighting each active speaker + synced default/forced `.srt` speaker-label subtitles)* or `Audio Only` (`.mka`, `.eac3`, `.ac3`, `.m4a`, `.flac`, `.wav` filtered per codec).
  * **Audio Codecs (6):** `eac3`, `ac3`, `aac`, `opus` (`libopus`), `flac`, and `pcm` (`pcm_s16le` uncompressed LPCM 16-bit).
  * **Speaker Layouts:** `1.0` (mono), `2.0` (stereo), `2.1` (FL+FR+LFE), `5.1` (6 ch), `7.1` (8 ch).
  * **Test Signals:** `tones` (distinct musical frequency per speaker + `60 Hz` LFE pulse + final all-channel balance chord) or `pink_noise` (band-limited `-20 dBFS` calibration pink noise with `120 Hz` LFE lowpass).
* **Safety & Quality Guardrails:**
  * **Anti-Pop Envelope & Safe Headroom:** Every speaker slot applies `25 ms` `afade` in/out (`afade=t=in:st=0:d=0.025`, `afade=t=out:...`) with conservative `-14 dBFS` tones, `-18 dBFS` LFE / all-channel chord, and `-20 dBFS` pink noise (`amix=normalize=0`).
  * **Smart Hard Blocks vs. Soft Warnings:** Hard-blocks physically invalid combinations (`eac3`/`ac3` > `5.1`, `opus` + `2.1` per RFC 7845, illegal audio-only container pairings, invalid/colliding filenames, paths outside `/storage`); surfaces soft warnings (with `Generate` enabled) for Playback Profile mismatches, multichannel LPCM over optical, AAC surround downmix, and audio-only TV display caveats.
  * **Subprocess Timeout & Default Destination:** Strict `30s` timeout per FFmpeg/`mkvmerge` call with clean `finally` temp cleanup; defaults to `"/"` (or `currentDir` when launched from Library) without auto-creating unexpected subfolders; atomically moves output without overwrite and probes `.mkv` outputs into SQLite immediately.

### 3.4 Two-Stage Processing Pipeline & Atomic Writes

Jobs execute sequentially in a single-worker background queue (1 active job at a time to avoid USB I/O thrashing).

1. **Pre-Flight Checks:**
   * **Duplicate Job Guard:** Reject `POST /api/jobs` with `409 Conflict` if `filepath` already has a `queued` or `running` job.
   * **`.mp4` Target Collision Guard:** If processing an `.mp4` in *Replace Original* mode and `<stem>.mkv` already exists on disk, fail safely rather than overwriting `<stem>.mkv`.
   * **Disk Space Check:** Verify free space on the target file's parent directory is at least `1.1 * input_file_size + estimated_audio_bytes + 100 MB` (when audio conversion is enabled, using bitrate-based sizing for lossy codecs and conservative lossless PCM ceiling for `flac`; `1.1 * input_file_size + 100 MB` otherwise). Fail fast with a clear UI error if insufficient.

2. **Stage 1 — Audio Transcode (only if conversion is enabled):**
   * Map the selected source track by relative audio index (`-map 0:a:<N>`) derived from `mkvmerge -J` audio tracks in order, and verify codec, channel count, and sample rate against `ffprobe` before starting.
   * Apply audiophile FFmpeg safeguards:
     * **Full Dolby Dynamic Range on Decode:** Pass `-drc_scale 0` *before* `-i` whenever the verified source codec is `ac3` or `eac3`.
     * **Canonical Channel Layout Normalization:** Pass explicit `-channel_layout` (`7.1`, `6.1`, `5.1`, `4.0`, `3.0`, `stereo`, `mono`) so `5.1(side)` / `7.1(wide)` sources never fail encoder validation; pass `-mapping_family 1` and `-ar 48000` for `libopus` when `channels > 2`.
     * **Sample-Accurate Temp Container:** Write Stage 1 output for `aac`, `opus`, and `flac` into Matroska Audio (`.mka`) so `mkvmerge` preserves exact encoder priming delay / Opus pre-skip (preventing ~21 ms lip-sync drift); elementary `.eac3` / `.ac3` (or `.mka`) for Dolby streams.
     * **Lossless Integrity:** Omit `-b:a` and preserve native bit depth (`s16`/`s32`) and sample rate when encoding `flac`.
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

---

## 10. UI Action → Exact CLI Command Translation Reference (1:1 Code Audit)

> **Purpose:** Single source of truth mapping every user UI action directly to the exact JSON payload (`app/static/js/editor.js`) and shell-free subprocess `argv` (`app/services/ffmpeg_runner.py`, `app/services/mkv_builder.py`, `app/services/mkv_runner.py`, `app/services/scanner.py`, `app/services/health_service.py`). Use this table as the 1:1 review checklist whenever verifying or changing UI-to-CLI translation.

*(Note: All commands execute via `asyncio.create_subprocess_exec(*argv)` with zero shell interpolation. MKV Forge uses `mkvmerge` from MKVToolNix—not MakeMKV—for container inspection and remuxing, and `ffmpeg` / `ffprobe` for audio verification and transcoding.)*

### 10.1 Read-Only & System Subprocess Commands

| User / System Action | Trigger / Source | Exact Command(s) Executed |
| :--- | :--- | :--- |
| **Health Check** (`GET /api/health`) | `health_service.py` | 1. `mkvmerge --version`<br>2. `ffmpeg -version`<br>3. `ffprobe -version` |
| **Library Scan / Single-File Rescan / Post-Job Re-Probe** | `scanner.py` (`run_mkvmerge_probe`) | 1. `mkvmerge -J <source_path>`<br>2. *(Only if `global_tags` array is non-empty in step 1)* `ffprobe -v quiet -print_format json -show_entries format_tags <source_path>` |

---

### 10.2 Container Metadata & Attachment Actions (File Editor → Stage 2 `mkvmerge`)

All source-file options are emitted strictly **before** `<source_path>` in `build_mkvmerge_command()` (`app/services/mkv_builder.py`).

| User UI Action | Frontend State / Payload Field (`editor.js`) | Exact `mkvmerge` Flag(s) Emitted |
| :--- | :--- | :--- |
| **Edit Container Title** | `container.title = "<new_title>"` *(if changed from original)* | `--title "<new_title>"` *(global option after `-o <output_tmp>`)* |
| **Leave Container Title Unchanged** | `container.title = null` | *(Omitted — existing title preserved)* |
| **Click `× Clear` on Container Title** | `container.title = ""` | `--title ""` *(clears container title)* |
| **Uncheck `Keep Global Tags`** | `container.strip_global_tags = true` | `--no-global-tags` *(before `<source_path>`)* |
| **Uncheck `Keep Track Tags`** | `container.strip_track_tags = true` | `--no-track-tags` *(before `<source_path>`)* |
| **Uncheck `Keep Chapters`** | `container.strip_chapters = true` | `--no-chapters` *(before `<source_path>`)* |
| **Strip All Attachments** *(uncheck all attachments or toggle `Strip Attachments`)* | `container.strip_attachments = true` *(or `keep_attachment_ids = []`)* | `--no-attachments` *(before `<source_path>`)* |
| **Keep Subset of Attachments** *(e.g. keep IDs 1 & 3, drop ID 2)* | `container.strip_attachments = false`, `container.keep_attachment_ids = [1, 3]` | `--attachments 1,3` *(before `<source_path>`)* |
| **Default Preset: `Remove All Metadata` ON** *(Settings → default on file open)* | Sets `containerTitle = ""`, every track's `track_name = ""`, `stripGlobalTags = true` *(if present)*, `stripTrackTags = true` *(if present)*, `stripAttachments = true` *(if present)*; keeps `stripChapters = false` | Combines `--title ""` *(if title existed)*, `--no-global-tags`, `--no-track-tags`, `--no-attachments`, and `--track-name <id>:` for each kept source track *(chapters preserved)* |

---

### 10.3 Source Track Actions (Video / Audio / Subtitles → Stage 2 `mkvmerge`)

| User UI Action | Frontend State / Payload Field (`editor.js`) | Exact `mkvmerge` Flag(s) Emitted |
| :--- | :--- | :--- |
| **Keep Selected Video / Audio / Subtitle Tracks** | `tracks[i].enabled = true` | `--video-tracks <id1,id2>` / `--audio-tracks <id1,id2>` / `--subtitle-tracks <id1,id2>` *(before `<source_path>`)* |
| **Uncheck ALL Tracks of a Type** *(e.g. drop all subtitles)* | Zero enabled tracks of that type | `--no-video` / `--no-audio` / `--no-subtitles` *(never emits invalid empty `--*-tracks ""`)* |
| **Edit Track Title** | `tracks[i].track_name = "<name>"` | `--track-name <id>:<name>` *(empty string `--track-name <id>:` clears title)* |
| **Change Track Language** | `tracks[i].language = "<iso>"` *(e.g. `eng`, `hin`, `jpn`, `und`)* | `--language <id>:<iso>` |
| **Toggle `Default` Flag** | `tracks[i].default_track = true\|false` | `--default-track-flag <id>:yes\|no` *(auto-forced to `no` on all source audio tracks if a converted or imported audio track owns default; auto-forced to `no` on all source subtitle tracks if an imported subtitle owns default)* |
| **Toggle `Forced` Flag** | `tracks[i].forced_track = true\|false` | `--forced-display-flag <id>:yes\|no` |
| **Reorder Tracks (`↑` / `↓` buttons)** | `tracks` array order + `external_tracks[i].order_index` | Global `--track-order <file_id>:<track_id>,...` at end of command (`file_id = 0` for Stage 1 converted audio; `source_file_index = 1` if Stage 1 ran else `0`; external imports start at `source_file_index + 1`) |

---

### 10.4 Audio Conversion Actions (`+ Add / Convert` → Pre-Check `ffprobe` + Stage 1 `ffmpeg` + Stage 2 `mkvmerge`)

When `audio_conversion.enabled = true`, MKV Forge executes **3 sequential commands** (plus the post-job `mkvmerge -J` re-probe):

#### Command 1 — Stream Verification (`verify_audio_stream_with_ffprobe` in `app/services/ffmpeg_runner.py`)
```bash
ffprobe -v quiet -print_format json \
  -show_entries format=duration:stream=index,codec_name,channels,sample_rate,channel_layout \
  -select_streams a \
  <source_path>
```
*Verifies relative stream `0:a:<source_audio_index>` exists and matches `expected_codec` and `expected_channels`, and extracts real `sample_rate`, `channels`, and `format_duration_us`.*

#### Command 2 — Stage 1 Audio Transcode (`build_ffmpeg_command` in `app/services/ffmpeg_runner.py`)
```bash
ffmpeg -y \
  [-drc_scale 0] \
  -i <source_path> \
  -map 0:a:<source_audio_index> \
  -map_metadata -1 -map_chapters -1 \
  -c:a <encoder> \
  [-b:a <effective_bitrate>] \
  -ac <target_channels> \
  [-channel_layout <layout>] \
  [-mapping_family 1] \
  [-ar <target_sample_rate>] \
  -progress pipe:1 -nostats \
  <temp_audio_path>
```

**Exact Per-Codec & Conditional Flag Translation Matrix:**

| UI Selection / Condition | Exact Flag / Value Translated | Code Source |
| :--- | :--- | :--- |
| **Target Codec = `E-AC-3`** | `-c:a eac3`, temp `<dir>/<filename>.mkvforge.eac3`, max `6` ch (`5.1`), `-ar 48000` only if source SR ∉ `{48000, 44100, 32000}` | `AppConstants`, `paths.py`, `ffmpeg_runner.py` |
| **Target Codec = `AC-3`** | `-c:a ac3`, temp `<dir>/<filename>.mkvforge.ac3`, max `6` ch (`5.1`), max bitrate `640k`, `-ar 48000` only if source SR ∉ `{48000, 44100, 32000}` | `AppConstants`, `paths.py`, `ffmpeg_runner.py` |
| **Target Codec = `AAC`** | `-c:a aac`, temp `<dir>/<filename>.mkvforge.mka`, max `8` ch (`7.1`), native SR preserved | `AppConstants`, `paths.py`, `ffmpeg_runner.py` |
| **Target Codec = `Opus`** | `-c:a libopus`, temp `<dir>/<filename>.mkvforge.mka`, max `8` ch (`7.1`), `-ar 48000` whenever source SR ≠ `48000` (or unknown), `-mapping_family 1` when `target_channels > 2`, mono (`1.0`) bitrate clamped at `256k` | `AppConstants`, `paths.py`, `ffmpeg_runner.py` |
| **Target Codec = `FLAC` (Lossless)** | `-c:a flac`, temp `<dir>/<filename>.mkvforge.mka`, max `8` ch (`7.1`), **`-b:a` omitted**, native bit depth & SR preserved *(UI only enables for lossless sources)* | `AppConstants`, `paths.py`, `ffmpeg_runner.py` |
| **Source Codec is `AC-3` or `E-AC-3`** | Emits `-drc_scale 0` **before `-i`** to disable Dolby Dynamic Range Compression on decode | `ffmpeg_runner.py:242-247` |
| **Channels = `Match Source`** | `target_channels = null` → `min(source_channels, codec.max_channels)` *(never upmixes)* | `ffmpeg_runner.py:93-118` |
| **Channels = Explicit Downmix** (`5.1`, `2.0`, `1.0`, etc.) | `-ac <N>` strictly bounded by `1 <= N <= min(source_channels, codec.max_channels)` | `ffmpeg_runner.py:93-118` |
| **Multichannel Output (`target_channels > 2`)** | Emits `-channel_layout <layout>` using `3→3.0`, `4→quad`, `5→5.0`, `6→5.1`, `7→6.1`, `8→7.1` | `config.py:140-149`, `ffmpeg_runner.py:269-272` |

#### Command 3 — Stage 2 MKV Mux (`build_mkvmerge_command` in `app/services/mkv_builder.py`)
Prepends `<temp_audio_path>` as **Input File 0** (shifting `<source_path>` to **Input File 1**):

| UI Option | Payload Field | Exact `mkvmerge` Translation |
| :--- | :--- | :--- |
| **Converted Track (File 0)** | `audio_conversion.*` | `--no-global-tags --no-track-tags --no-attachments --no-chapters [--track-name 0:<track_title>] [--language 0:<language>] --default-track-flag 0:<yes\|no> --forced-display-flag 0:no [--sync 0:<delay_ms>] <temp_audio_path>` |
| **Placement at Audio Index 0** | Automatic | Inserts `0:0` in `--track-order` immediately before the first kept source audio track (or immediately after the last kept video track if no source audio remains) |
| **Set Converted Track as Default** | `set_as_default = true` | `--default-track-flag 0:yes` on File 0 **and** forces `--default-track-flag <id>:no` on every kept source audio track |
| **Drop Base Audio Track** | `drop_source_track = true` | Removes `source_track_id` from File 1's `--audio-tracks` (or emits `--no-audio` if no other source audio tracks remain) and excludes `1:<source_track_id>` from `--track-order` |
| **Preserve Source Audio Sync Delay** | `source_minimum_timestamp_ns != 0` | Computes `delay_ms = round(ns / 1_000_000)` and emits `--sync 0:<delay_ms>` on File 0 |

---

### 10.5 Import External Audio / Subtitle Actions (`+ Add / Convert → Import …`)

Each imported file is appended after `<source_path>` as Input File `source_file_index + 1 + i`:

| User UI Action | Payload Field (`external_tracks[i]`) | Exact `mkvmerge` Flag(s) Emitted Before `<ext_path>` |
| :--- | :--- | :--- |
| **Import External Audio** (`.ac3`, `.eac3`, `.aac`, `.mka`, `.dts`, `.flac`, `.mp3`, `.opus`, `.m4a`) | `kind = "audio"` | `--no-global-tags --no-track-tags --no-attachments --no-chapters --no-video --no-subtitles [--track-name 0:<name>] [--language 0:<lang>] --default-track-flag 0:<yes\|no> --forced-display-flag 0:<yes\|no> [--sync 0:<sync_delay_ms>] <ext_path>` |
| **Import External Subtitle** (`.srt`, `.ass`, `.ssa`, `.vtt`, `.sup`) | `kind = "subtitles"` | `--no-global-tags --no-track-tags --no-attachments --no-chapters --no-video --no-audio [--track-name 0:<name>] [--language 0:<lang>] --default-track-flag 0:<yes\|no> --forced-display-flag 0:<yes\|no> [--sync 0:<sync_delay_ms>] <ext_path>` |
| **Set Imported Track Position** | `order_index = <pos>` | Inserts `<file_idx>:0` at position `<pos>` in `--track-order` |

---

### 10.6 Output Mode & Post-Remux Finalization (`app/services/mkv_runner.py`)

| User UI Action (`Output Mode`) | `mkvmerge -o` Temp Target | Atomic Finalization + Re-Probe |
| :--- | :--- | :--- |
| **Replace Original (`.mkv` source)** | `<dir>/<filename>.mkvforge.tmp` | `os.replace(tmp, source)` → `mkvmerge -J <source>` |
| **Replace Original (`.mp4` source → `.mkv`)** | `<dir>/<stem>.mkv.mkvforge.tmp` | Fail if `<stem>.mkv` exists → atomic link/move to `<stem>.mkv` → `source.unlink()` *(only after `.mkv` verified)* → delete old `.mp4` DB row → `mkvmerge -J <stem>.mkv` |
| **Keep Both** | `<dir>/<filename>.mkvforge.tmp` | Atomic move to `<stem>.remux.mkv` (or `.remux.2.mkv`, …) → `mkvmerge -J <new_path>` |
| **Save As (`output_path`, optional `delete_original`)** | `<target_dir>/<target_name>.mkvforge.tmp` | Fail if target exists → atomic move to `<output_path>` → if `delete_original = true`, delete original & old DB row → `mkvmerge -J <output_path>` |

---

### 10.7 Concrete End-to-End 1:1 Worked Examples

#### Example 1 — User Action: *Remove All Metadata Only* (keep video `#0`, audio `#1`, sub `#2`; Replace Original)
* **Commands executed (1 remux command + 1 post-job re-probe):**
```bash
# Stage 1 (Remux — FFmpeg skipped)
mkvmerge --gui-mode -o "/storage/Movies/Movie.mkv.mkvforge.tmp" \
  --title "" \
  --no-global-tags --no-track-tags --no-attachments \
  --video-tracks 0 --audio-tracks 1 --subtitle-tracks 2 \
  --track-name 0: --language 0:eng --default-track-flag 0:yes --forced-display-flag 0:no \
  --track-name 1: --language 1:eng --default-track-flag 1:yes --forced-display-flag 1:no \
  --track-name 2: --language 2:eng --default-track-flag 2:no --forced-display-flag 2:no \
  "/storage/Movies/Movie.mkv" \
  --track-order 0:0,0:1,0:2

# Post-swap cache refresh (after atomic os.replace)
mkvmerge -J "/storage/Movies/Movie.mkv"
```

#### Example 2 — User Action: *Convert AAC 5.1 (`#1`, audio index `0`) → E-AC-3 5.1 640k (Default, Keep Original) + Strip Global Tags*
* **Commands executed (3 pipeline commands + 1 post-job re-probe):**
```bash
# 1. Pre-Stage 1 Stream Verification
ffprobe -v quiet -print_format json \
  -show_entries format=duration:stream=index,codec_name,channels,sample_rate,channel_layout \
  -select_streams a "/storage/Movies/Movie.mkv"

# 2. Stage 1 — Audio Transcode (FFmpeg)
ffmpeg -y -i "/storage/Movies/Movie.mkv" \
  -map 0:a:0 -map_metadata -1 -map_chapters -1 \
  -c:a eac3 -b:a 640k -ac 6 -channel_layout 5.1 \
  -progress pipe:1 -nostats \
  "/storage/Movies/Movie.mkv.mkvforge.eac3"

# 3. Stage 2 — MKV Remux (mkvmerge: File 0 = new EAC3, File 1 = original MKV)
mkvmerge --gui-mode -o "/storage/Movies/Movie.mkv.mkvforge.tmp" \
  --no-global-tags --no-track-tags --no-attachments --no-chapters \
  --track-name "0:EAC3 5.1 • 640k • eng • muxed" --language 0:eng \
  --default-track-flag 0:yes --forced-display-flag 0:no \
  "/storage/Movies/Movie.mkv.mkvforge.eac3" \
  --no-global-tags \
  --video-tracks 0 --audio-tracks 1 --no-subtitles \
  --track-name 0: --language 0:eng --default-track-flag 0:yes --forced-display-flag 0:no \
  --track-name 1: --language 1:eng --default-track-flag 1:no --forced-display-flag 1:no \
  "/storage/Movies/Movie.mkv" \
  --track-order 1:0,0:0,1:1

# 4. Post-swap cache refresh (temp audio deleted in finally block)
mkvmerge -J "/storage/Movies/Movie.mkv"
```

#### Example 3 — User Action: *Convert AC-3 5.1 (`#1`) → Opus 5.1 320k + Drop Base Track*
* **Commands executed (shows `-drc_scale 0`, `libopus`, `-mapping_family 1`, `-ar 48000`, `.mka` temp, and `--no-audio` on source):**
```bash
# 1. Stream verification
ffprobe -v quiet -print_format json \
  -show_entries format=duration:stream=index,codec_name,channels,sample_rate,channel_layout \
  -select_streams a "/storage/Movies/Movie.mkv"

# 2. Stage 1 — FFmpeg (DRC disabled before -i, canonical 5.1 layout + Opus mapping_family 1)
ffmpeg -y -drc_scale 0 -i "/storage/Movies/Movie.mkv" \
  -map 0:a:0 -map_metadata -1 -map_chapters -1 \
  -c:a libopus -b:a 320k -ac 6 -channel_layout 5.1 -mapping_family 1 -ar 48000 \
  -progress pipe:1 -nostats \
  "/storage/Movies/Movie.mkv.mkvforge.mka"

# 3. Stage 2 — mkvmerge (base audio dropped -> --no-audio on File 1)
mkvmerge --gui-mode -o "/storage/Movies/Movie.mkv.mkvforge.tmp" \
  --no-global-tags --no-track-tags --no-attachments --no-chapters \
  --track-name "0:Opus 5.1 • 320k • eng • muxed" --language 0:eng \
  --default-track-flag 0:yes --forced-display-flag 0:no \
  "/storage/Movies/Movie.mkv.mkvforge.mka" \
  --video-tracks 0 --no-audio --no-subtitles \
  --track-name 0: --language 0:eng --default-track-flag 0:yes --forced-display-flag 0:no \
  "/storage/Movies/Movie.mkv" \
  --track-order 1:0,0:0
```

#### Example 4 — User Action: *Import External `.srt` Subtitle (`eng`, Forced, `+250 ms` Sync Delay)*
* **Commands executed (1 remux command + 1 post-job re-probe):**
```bash
mkvmerge --gui-mode -o "/storage/Movies/Movie.mkv.mkvforge.tmp" \
  --video-tracks 0 --audio-tracks 1 --no-subtitles \
  --track-name 0: --language 0:eng --default-track-flag 0:yes --forced-display-flag 0:no \
  --track-name 1: --language 1:eng --default-track-flag 1:yes --forced-display-flag 1:no \
  "/storage/Movies/Movie.mkv" \
  --no-global-tags --no-track-tags --no-attachments --no-chapters \
  --no-video --no-audio \
  --track-name "0:English (Forced)" --language 0:eng \
  --default-track-flag 0:no --forced-display-flag 0:yes --sync 0:250 \
  "/app/data/uploads/<upload_id>.srt" \
  --track-order 0:0,0:1,1:0
```

---

### 10.8 Translation Review Invariants (Checklist for Future Changes)

1. **Per-file option scoping:** Every `--track-name`, `--language`, `--default-track-flag`, `--forced-display-flag`, `--sync`, `--*-tracks`, and `--no-*` flag in `mkvmerge` **must precede** the input path it targets.
2. **Never emit empty track lists:** Zero kept tracks of a type must emit `--no-video` / `--no-audio` / `--no-subtitles`, never `--*-tracks ""`.
3. **Single default track ownership:** Converted audio (`set_as_default=true`) > Imported default audio > Source default audio; Imported default subtitle > Source default subtitle.
4. **No silent upmixing:** `target_channels` is always clamped to `1 <= N <= min(source_channels, codec.max_channels)` in both `editor.js` and `ffmpeg_runner.py`.
5. **DRC decode safety:** `-drc_scale 0` must always appear **before `-i`** and **only** for `ac3` / `eac3` sources (FFmpeg rejects `-drc_scale` on non-Dolby decoders).
6. **Sample-accurate container for priming-delay codecs:** `aac`, `opus`, and `flac` Stage 1 outputs must write to `.mka` (never raw ADTS/elementary streams) so `mkvmerge` preserves encoder priming delay / Opus pre-skip.

### 10.9 Speaker & Audio System Test Generator Commands (`app/services/test_media_generator.py`)

* **`Video + Audio (.mkv)` mode (single-pass FFmpeg command + post-gen `mkvmerge -J` index when available):**
  1. `ffmpeg -y -i <tmp_labels.srt> -filter_complex "<video_drawbox_graph>;<audio_channel_graph>" -map [vout] -map [aout] -map 0:s:0 -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p -t <total_sec> -c:a <encoder> -ar 48000 [-b:a <bitrate>] [-mapping_family 1] -metadata title="MKV Forge Speaker Test — <Codec> <Layout>" -metadata:s:v:0 title="Speaker Layout Diagram (<Layout>)" -metadata:s:a:0 title="<Codec> <Layout> • <Bitrate|Lossless>" -metadata:s:a:0 language=eng -disposition:v:0 default -disposition:a:0 default -c:s srt -metadata:s:s:0 title="Active Speaker Labels" -metadata:s:s:0 language=eng -disposition:s:0 default+forced -f matroska <tmp_final>`
* **`Audio Only` mode (1 command):**
  1. `ffmpeg -y -filter_complex "<audio_channel_graph>" -map [aout] -c:a <encoder> -ar 48000 [-b:a <bitrate>] [-mapping_family 1] <tmp_audio.<ext>>`
