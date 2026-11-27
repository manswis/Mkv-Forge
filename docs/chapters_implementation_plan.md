# MKV Forge — Chapters Editor & Interactive Timeline Implementation Plan

> **Companion Spec:** [chapters_editor_spec.md](file:///Users/manojbiswas/Documents/Projects/Mkv-Forge/docs/chapters_editor_spec.md)  
> **Workflow:** Implement strictly one phase at a time with zero big-bang changes. Every phase must end with a green test gate on macOS (`pytest`) before checking off `[x]` and advancing to the next phase. Zero server disk cache, zero new global Settings.

---

## Phase C1: Constants, Schema Contract & Pure Chapter Domain Engine

Goal: Build the completely isolated, 100% unit-tested chapter parsing, serialization, validation, and `mkvextract` reading engine without touching any existing job runner or UI code.

- [x] **C1.1 Centralized Constants (`app/config.py`)**
  - [x] Add all chapter & scene-frame constants to `AppConstants` (zero magic numbers):
    - `CHAPTER_MAX_COUNT = 500`, `CHAPTER_MAX_TITLE_LEN = 200`, `CHAPTER_PARSE_MAX_BYTES = 512 * 1024`, `CHAPTER_EXTRACT_TIMEOUT_SEC = 10.0`, `CHAPTER_QUICK_INTERVALS_MIN = (5, 10, 15)`, `CHAPTER_DEFAULT_LANGUAGE = "und"`, `TEMP_CHAPTERS_SUFFIX = ".mkvforge.chapters.xml"`.
    - `CHAPTER_ACTION_KEEP = "keep"`, `CHAPTER_ACTION_REPLACE = "replace"`, `CHAPTER_ACTION_CLEAR = "clear"`, `ALLOWED_CHAPTER_ACTIONS = ("keep", "replace", "clear")`.
    - `SCENE_FRAME_MODE_FAST = "fast"`, `SCENE_FRAME_MODE_ACCURATE = "accurate"`, `ALLOWED_SCENE_FRAME_MODES = ("fast", "accurate")`.
    - `SCENE_FRAME_SCAN_DENSITY_PRESETS = (12, 24, 40)`, `SCENE_FRAME_SCAN_DEFAULT_DENSITY = 24`, `SCENE_FRAME_PEEK_OFFSET_SEC = 15`.
    - `SCENE_FRAME_FAST_WIDTH_PX = 320`, `SCENE_FRAME_ACCURATE_WIDTH_PX = 480`, `SCENE_FRAME_ACCURATE_PRESEEK_SEC = 10.0`, `SCENE_FRAME_EOF_MARGIN_MS = 500`.
    - `SCENE_FRAME_FAST_QUALITY = 5`, `SCENE_FRAME_ACCURATE_QUALITY = 4`, `SCENE_FRAME_TIMEOUT_SEC = 10.0`, `SCENE_FRAME_DISCONNECT_POLL_SEC = 0.15`, `SCENE_FRAME_TERMINATE_GRACE_SEC = 0.5`, `SCENE_FRAME_MAX_CONCURRENCY = 1`, `SCENE_FRAME_HTTP_CACHE_MAX_AGE_SEC = 86400`.

- [x] **C1.2 Pydantic Schema Contract (`app/models/schemas.py`)**
  - [x] Add `ChapterEntry(start_ms: int = Field(..., ge=0), title: str = Field(default="", max_length=AppConstants.CHAPTER_MAX_TITLE_LEN))`.
  - [x] Add API models: `ChaptersReadResponse` (including `has_complex_chapters: bool = False`), `ChaptersParseRequest`, `ChaptersParseResponse`.
  - [x] Extend `ContainerMetadataConfig` with:
    - `chapters_action: Literal["keep", "replace", "clear"] = "keep"`
    - `chapters: list[ChapterEntry] | None = None`
    - Backward-compatible validator: map legacy `strip_chapters=True` (when `chapters_action == "keep"`) → `chapters_action = "clear"`; require valid non-empty `chapters` (≤ `CHAPTER_MAX_COUNT`) when `chapters_action == "replace"`; clear/reject conflicting `chapters` when `chapters_action == "clear"`.

- [x] **C1.3 Pure Chapter Service (`app/services/chapter_service.py`)**
  - [x] Implement forgiving timestamp parser (`parse_timestamp_to_ms`) accepting `MM:SS`, `H:MM:SS`, `HH:MM:SS.mmm`, `HH:MM:SS.nnnnnnnnn`, and formatter (`format_ms_to_matroska_time`).
  - [x] Implement `parse_matroska_chapters_xml(xml_text)` safely rejecting XML DTD/entity declarations, returning `(chapters, has_complex_chapters)` where `has_complex_chapters` is `True` if there are multiple `<EditionEntry>` elements, `<EditionFlagOrdered>1</EditionFlagOrdered>`, or nested `<ChapterAtom>` sub-chapters.
  - [x] Implement `parse_ffprobe_chapters_json(payload)` parsing `ffprobe -show_chapters -print_format json` output for `.mp4` containers.
  - [x] Implement `serialize_chapters_to_matroska_xml(chapters, *, language="und")` producing valid MKVToolNix-compatible `<Chapters><EditionEntry><ChapterAtom>...</ChapterAtom></EditionEntry></Chapters>` with deterministic UIDs and proper XML escaping.
  - [x] Implement unified auto-detecting `parse_chapters_text(raw_text, *, duration_ms=None)` handling Matroska XML, OGG (`CHAPTER01=`), and YouTube/plain text/CSV lines, with out-of-range warnings, duplicate merging, and `CHAPTER_MAX_COUNT` enforcement.
  - [x] Implement async `extract_chapters_from_file(abs_path, *, timeout_sec=AppConstants.CHAPTER_EXTRACT_TIMEOUT_SEC)` routing `.mkv` → `mkvextract <abs_path> chapters <tmp.xml>` (with guaranteed temp-file cleanup in `finally:`) and `.mp4` (non-MKV) → `ffprobe -v quiet -print_format json -show_chapters <abs_path>`.

- [x] **C1.4 Phase C1 Verification Gate (`tests/unit/test_chapter_service.py`)**
  - [x] Test XML serialize → parse round-trip (Unicode, `& < > "`, 1–500 chapters), `has_complex_chapters` detection (multi-edition, nested `<ChapterAtom>`, `<EditionFlagOrdered>1</EditionFlagOrdered>`, and `False` for flat single-edition), `.mp4` `ffprobe -show_chapters` parsing, all 3 auto-detected text formats, duration warnings, XXE/DTD rejection, negative/empty/malformed inputs, `mkvextract`/`ffprobe` failure/timeout, and `strip_chapters=True` backward compatibility.
  - [x] Run `pytest tests/unit/test_chapter_service.py -v` and confirm 100% green.

---

## Phase C2: Stateless Scene Frame Extractor & Chapters REST API (All 3 Guardrails)

Goal: Deliver the fast, zero-disk-cache (`pipe:1`) frame extraction engine and REST endpoints with all 3 technical guardrails locked down before wiring the save pipeline or UI.

- [x] **C2.1 Stateless Scene Frame Extractor (`app/services/scene_frame_service.py`)**
  - [x] Implement `clamp_frame_timestamp_ms(ts_ms, duration_us)` clamping to `[0, max(0, duration_ms - SCENE_FRAME_EOF_MARGIN_MS)]`.
  - [x] **Guardrail #1 (Fix `±1s` Keyframe Trap + Lightweight 10-bit HDR):** Implement `build_scene_frame_ffmpeg_command(abs_path, *, ts_ms, mode)`:
    - `mode="fast"`: `-v error -ss <ts> -i <file> -an -sn -dn -frames:v 1 -vf scale='min(320,iw)':-2,format=yuvj420p -q:v 5 -f image2pipe -vcodec mjpeg pipe:1`
    - `mode="accurate"`: `pre_seek = max(0.0, ts - 10.0)`, `post_seek = ts - pre_seek` → `-v error -ss <pre_seek> -i <file> -ss <post_seek> -an -sn -dn -frames:v 1 -vf scale='min(480,iw)':-2,format=yuvj420p -q:v 4 -f image2pipe -vcodec mjpeg pipe:1`
  - [x] **Guardrail #2 (Real Instant Disconnect Cancellation):** Implement async `extract_scene_frame(...)`:
    - Bounded by `asyncio.Semaphore(AppConstants.SCENE_FRAME_MAX_CONCURRENCY)` (`1`), with best-effort low I/O priority (`ionice`/`nice`) when an unrelated job is active.
    - Races `proc.communicate()` against `is_disconnected()` polling (`0.15s`) and `SCENE_FRAME_TIMEOUT_SEC` (`10s`); immediately `proc.terminate()` → grace `0.5s` → `proc.kill()` + `await proc.wait()` on disconnect or timeout.
    - Validates JPEG magic bytes (`\xff\xd8\xff`) directly in memory (zero server disk writes — **Option A**).

- [x] **C2.2 Chapters & Frame API Router (`app/api/chapters.py`, `app/main.py`)**
  - [x] **Guardrail #3 (Active-Job `409 Conflict`):** Add `_ensure_file_safe_for_chapter_read(file_row, job_repo)` returning `409 Conflict` when a `replace` job (or `delete_original` `save_as` job) is actively running on this file, or when `scan_status != "ready"`.
  - [x] `GET /api/files/{file_id}/chapters` — Fast-returns `[]` when `chapters_count == 0` (zero subprocesses); otherwise calls `extract_chapters_from_file(...)`.
  - [x] `POST /api/files/{file_id}/chapters/parse` — Validates payload size (≤ 512 KB), calls `parse_chapters_text(...)`, returns `{ detected_format, chapters, warnings }`.
  - [x] `GET /api/files/{file_id}/frame?ts_ms=<int>&mode=<fast|accurate>&v=<fingerprint>` — Validates video file (`duration_us > 0`), enforces `409` active-job guard, returns `image/jpeg` with `Cache-Control: private, max-age=86400`, zero disk writes.
  - [x] Mount `chapters.router` in `app/main.py`.

- [x] **C2.3 Phase C2 Verification Gate (`tests/unit/test_scene_frame_service.py`, `tests/integration/test_chapters_api.py`)**
  - [x] Verify exact `fast` vs `accurate` argv (`ts < 10s`, `ts == 0`, `ts >= duration` clamping, `pipe:1`, `format=yuvj420p`), disconnect subprocess kill, zero files written to `<data_dir>`.
  - [x] Verify real `ffmpeg` on synthetic 10-bit `yuv420p10le` MKV returns distinct JPEGs 1 second apart (`mode="accurate"` at `1000ms` vs `2000ms`).
  - [x] Verify `409 Conflict` on active `replace` job, `200 OK` on unrelated active job, `404`/`400` error guards, and `Cache-Control: private, max-age=86400`.
  - [x] Run `pytest tests/unit/test_scene_frame_service.py tests/integration/test_chapters_api.py -v` and confirm 100% green.

---

## Phase C3: Save Pipeline Integration (`<1s` `mkvpropedit` Instant Edit & Stage 2 `mkvmerge`)

Goal: Wire chapter saving cleanly into both `mkvpropedit` (`<1s` in-place) and `mkvmerge` (full remux), preserving untouched chapters byte-for-byte and guaranteeing temp XML cleanup.

- [x] **C3.1 Instant Header Edit Support (`app/services/mkvpropedit_runner.py`)**
  - [x] Update `can_use_instant_header_edit(...)` to allow `chapters_action in ("keep", "replace", "clear")` on MKV `replace` jobs (removing `container.strip_chapters` as a `mkvpropedit` blocker).
  - [x] Update `build_mkvpropedit_command(..., chapters_xml_path=None)` and `run_mkvpropedit_edit(...)`: `--chapters <xml_path>` (`replace`), `--chapters ""` (`clear`), omitted (`keep`).

- [x] **C3.2 Full Remux Support (`app/services/mkv_builder.py`)**
  - [x] Update `build_mkvmerge_command(..., chapters_xml_path=None)`: `--no-chapters --chapters <xml_path>` (`replace`), `--no-chapters` (`clear` / legacy `strip_chapters=True`), omitted (`keep`).

- [x] **C3.3 Pipeline Temp XML Lifecycle & Job Summary (`app/services/mkv_runner.py`, `app/services/job_worker.py`, `app/services/job_summary.py`)**
  - [x] Materialize temp chapters XML (using preferred language or `und`) when `chapters_action == "replace"`, pass to `mkvpropedit` / `mkvmerge`, and always unlink in `finally:`.
  - [x] Surface `Updated N Chapters` / `Cleared Chapters` summary pills in `job_summary.py`.
  - [x] Confirm post-job `probe_single_file()` refreshes `chapters_count` in SQLite immediately.

- [x] **C3.4 Phase C3 Verification Gate (`tests/unit/test_chapters_pipeline.py`)**
  - [x] Verify `can_use_instant_header_edit`, exact `mkvpropedit` & `mkvmerge` argv, real `mkvpropedit` + `mkvextract` round-trip (`add` → `update` → `clear` via `--chapters ""`), and guaranteed temp XML cleanup even on error.
  - [x] Run `pytest tests/unit/test_chapters_pipeline.py tests/unit/test_instant_edit_track_flags_and_dynamics.py -v` and confirm 100% green.

---

## Phase C4: Pure Frontend Chapters Engine & Editor State Integration

Goal: Build all pure, zero-DOM frontend chapter logic (`chapters.js`), constants, API wrappers, and File Editor state/payload integration, locked down with Node VM unit tests before touching HTML markup.

- [ ] **C4.1 Frontend Constants & API Helpers (`app/static/js/constants.js`, `app/static/js/api.js`)**
  - [ ] Add `CHAPTERS` & `SCENE_PICKER` constants (`ACTION_KEEP/REPLACE/CLEAR`, `MAX_COUNT: 500`, `MAX_TITLE_LEN: 200`, `QUICK_INTERVALS_MIN: [5, 10, 15]`, `QUICK_NAME_CHIPS: ["Intro", "Opening", "Intermission", "Song", "Credits"]`, `NUDGE_STEPS_SEC: [-30, -5, -1, 1, 5, 30]`, `PEEK_OFFSET_SEC: 15`, `EOF_MARGIN_MS: 500`, `SCAN_DENSITY_PRESETS: [12, 24, 40]`, `SCAN_DEFAULT_DENSITY: 24`, `SCAN_DENSITY_STORAGE_KEY: "mkvforge.scene_scan_density"`, `FINE_TUNE_DEBOUNCE_MS: 180`).
  - [ ] Add `getFileChapters(fileId)`, `parseFileChapters(fileId, rawText, filename)`, and `buildSceneFrameUrl(fileId, tsMs, mode, videoFingerprint)` to `app/static/js/api.js`.

- [ ] **C4.2 Pure Chapters & Timeline Helper Module (`app/static/js/chapters.js`)**
  - [ ] `formatChapterTime(ms)` & forgiving `parseChapterTimeInput(rawStr)`.
  - [ ] `generateQuickSkipChapters(durationMs, intervalMin)` & `ensureZeroStartChapter(chapters)` (TV-player `00:00:00` guard).
  - [ ] `sortAndNormalizeChapters(chapters, durationMs)` & `renumberGenericChapters(chapters)` (renumbers `Chapter NN` only, preserving custom names).
  - [ ] `shiftAllChapters(chapters, deltaMs, durationMs)` (clamped to `[0, durationMs]`).
  - [ ] `clampFrameTimestampMs(tsMs, durationMs)` (clamped to `[0, max(0, durationMs - 500)]`) & `buildPeekFrameTimestamps(centerMs, durationMs)` (`[max(0, center - 15s), center, min(maxFrameMs, center + 15s)]`).
  - [ ] `buildScanSceneSlots(durationMs, densityCount)` (clean `12 / 24 / 40` evenly spaced slots across `[0, maxFrameMs]`).
  - [ ] `buildVideoFingerprint(fileDetail)`, `exportChaptersAsOggText(chapters)`, `exportChaptersAsXml(chapters)`, and `areChapterListsEqual(a, b)`.

- [ ] **C4.3 File Editor State & Instant-Edit Parity (`app/static/js/editor.js`)**
  - [ ] Extend `createEditorState(...)`: add `chaptersAction: "keep"`, `chapters: []`, `originalChapters: null`, `chaptersLoaded: false`, synced 1:1 with `stripChapters`.
  - [ ] Update `hasPendingChanges(...)`, `isInstantHeaderEditEligible(...)` (MKV `replace` + chapter edits/clears → **eligible for `⚡ Instant Edit`**), `buildPendingSummary(...)`, and `buildJobRequestPayload(...)`.

- [ ] **C4.4 Phase C4 Verification Gate (`tests/unit/test_chapters_frontend.py`)**
  - [ ] Node VM tests covering `chapters.js` and `editor.js` (parsing, Quick Skip, auto-`00:00:00` guard, generic-only renumber, Shift All clamping, 3-frame peek timestamps, 60-slot scan cap, `isInstantHeaderEditEligible` parity, untouched `"keep"` omission).
  - [ ] Run `node --check app/static/js/constants.js app/static/js/api.js app/static/js/chapters.js app/static/js/editor.js && pytest tests/unit/test_chapters_frontend.py -v` and confirm 100% green.

---

## Phase C5: Unified Chapters Sheet UI, Inline Vertical-Rail Preview & All 3 Entry Points

Goal: Wire the Alpine.js controller (`app.js`) and responsive UI (`index.html`) matching the unified single-screen design with `44px` touch targets and leak-free `AbortController` / `URL.revokeObjectURL` lifecycle.

- [ ] **C5.1 Alpine Controller State & Serial Fetch Lifecycle (`app/static/js/app.js`)**
  - [ ] Extend `openEditor(file, { openChapters = false })` and `runContextMenuAction("chapters")` so all 3 entry points open the exact same Chapters Sheet cleanly.
  - [ ] Manage `chaptersModal` state (`drawer: "main" | "scan_scenes" | "paste"`, `expandedChapterKey`, `draftPeek` `{ tsMs, title, frames, centerObjectUrl, loading }`, `undoSnapshot`, `quickSkipOpen`, `menuOpen`, `shiftDraft`, `pasteDraft`).
  - [ ] Implement serial 1-at-a-time frame loader with single `_sceneFetchAbortController`:
    - Fetches 3-frame Draft Peek (`-15s | tapped | +15s`, `mode="fast"`) on timeline bar tap, switching center frame to `mode="accurate"` on any `±` nudge (optimistic instant timestamp update + `180ms` debounce).
    - Fetches single `mode="accurate"` frame when expanding `[👁]` inline on a chapter row (1 expanded at a time; zero frames fetched on modal open).
    - Supports opt-in **`[ 🖼 Show Previews ]`** on existing chapters (`chapters.length > 0`): serially streams `320px` `mode="fast"` thumbnails down the vertical rail (`Loading N/M… [Stop]` → `Hide Previews`), aborting cleanly on Stop or modal close.
    - Fetches 1-level `Scan Scenes` grid serially (single clean 3-preset density row `12 / 24 / 40` persisted in `localStorage`), preserving loaded `objectUrl`s in RAM while modal stays open and revoking **all** `objectUrl`s on density change or modal close.
    - Immediately aborts on modal close, **Stop**, row switch, timeline re-tap, or nudge; gracefully renders `409 Conflict` active-job banner.
  - [ ] Implement 1-tap Undo snapshot, Quick Skip popover, Unified Paste/Upload (`POST /parse` + file drop/picker), Renumber, Shift All, Export `.txt`/`.xml`, Reset to Original, and Clear All.

- [ ] **C5.2 Responsive HTML Surfaces (`app/static/index.html`)**
  - [ ] Load `/static/js/chapters.js?v=...` once before `editor.js` / `app.js` and bump cache-busters.
  - [ ] Wire all 3 entry points:
    - **File Editor Section 1 `Chapters` row:** Always visible (`12 chapters [Edit…]` / `No chapters [+ Add Chapters]` / `[Modified]` / `[Will Clear]`) synced with `Strip Chapters`.
    - **File Info Modal:** Secondary **`Chapters`** button next to `Open in Editor →` + tappable `N Chapters` pill → `openEditor(target, { openChapters: true })`.
    - **Right-Click / 3-Dot Context Menu:** **`Chapters`** right under **`Process`** in Group 1 (`Process` → `Chapters` → `Info` → `Download`), disabled when busy/scanning/scan-error.
  - [ ] Build the Unified Chapters Sheet (`max-h-[90dvh]`, `Escape` close with abort cleanup):
    - **Sticky Horizontal Movie Scrubber (`40px` tap zone)** with chapter dots + **Action Chip Strip** (`⚡ Quick Skip ▾`, `📋 Paste / Upload`, `🎬 Scan Scenes`, `+ Add`, `⋯`).
    - **Draft Peek Card** (`Not added yet`, 3 fast frames, `±30s/-5s/-1s/+1s/+5s/+30s`, Title + Quick-Name Chips, `+ Add Chapter` / `×`).
    - **Vertical Left Timeline Rail (both Desktop & Mobile):** Shows 1-line amber complex-chapter warning banner when `has_complex_chapters === true`; empty hint for `0` chapters; `●` nodes for existing chapters with opt-in **`[ 🖼 Show Previews ]`** (`Loading N/M… [Stop]` / `Hide Previews`) header toggle and inline accordion `[👁]` accurate preview + `±` nudges + Quick-Name Chips right under the tapped row (`○ End of movie` footer node).
    - **Optional 1-Level `🎬 Scan Scenes` Grid** (single clean 3-preset density row `Compact · 12` / `Balanced · 24` / `Detailed · 40` persisted in `localStorage`, Stop button, tap to fine-tune in Draft Peek or multi-select) & **Unified `📋 Paste / Upload` Drawer**.

- [ ] **C5.3 Phase C5 Verification Gate (`tests/unit/test_index_html_structure.py`)**
  - [ ] Update script cache-buster assertions, Group 1 context-menu ordering (`Process` → `Chapters` → `Info` → `Download`), and verify zero duplicate `<script>` tags + balanced HTML tags.
  - [ ] Add landmark assertions for all 3 entry points, sticky horizontal scrubber, Draft Peek Card, vertical left-rail inline preview, Quick-Name Chips, 1-level Scan Scenes grid, `⋯` menu, `AbortController` + `URL.revokeObjectURL`, and zero parallel `<img :src="/api/files/.../frame">` patterns.
  - [ ] Run `node --check app/static/js/app.js && pytest tests/unit/test_index_html_structure.py -v` and confirm 100% green.

---

## Phase C6: Full End-to-End Verification & Release Readiness

Goal: Prove the entire vertical slice works seamlessly across unit, integration, real FFmpeg/MKVToolNix, desktop & mobile viewports, and zero regressions.

- [ ] **C6.1 End-to-End Integration Suite (`tests/integration/test_chapters_e2e.py`)**
  - [ ] Real synthetic MKV end-to-end: `GET /chapters` (`[]`) → `POST /chapters/parse` → `GET /frame?mode=fast` & `mode=accurate` → `POST /api/jobs` (`replace` mode) → verify `<1s` `mkvpropedit` instant edit runs → verify `GET /chapters` returns written chapters and SQLite `chapters_count` updates immediately.
  - [ ] Clear flow (`chapters_action="clear"`) → verify `mkvpropedit --chapters ""` clears chapters and `chapters_count == 0`.
  - [ ] Full remux flow (`keep_both` + chapters replace) → verify Stage 2 `mkvmerge` writes chapters to `.remux.mkv` while keeping original untouched.

- [ ] **C6.2 Full Regression Gate**
  - [ ] Run full suite: `pytest tests/unit tests/integration -q` — 0 failures.
  - [ ] Verify `git diff` cleanliness: zero magic numbers, zero dead code, zero files written into `<data_dir>` during frame extraction, zero new global Settings.
  - [ ] Manual smoke check on desktop + mobile viewport (all 3 entry points, tap horizontal scrubber → 3-frame Draft Peek → `+ Add Chapter`, tap `[👁]` on vertical rail → inline accurate `±1s` fine-tune, `🎬 Scan Scenes` instant cancel, and `<1s` Instant Edit apply).
