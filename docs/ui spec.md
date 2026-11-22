Yes—storing the scanned metadata in SQLite makes the UI feel **instant**. When you open the app on your phone or desktop, it reads straight from the local database in milliseconds without touching your USB drive. You only trigger disk I/O when you tap **Scan Library** (which skips unchanged files) or tap the **Re-scan** icon on an individual file.

---

# UI/UX Design & Flow Specification: MKV Forge

## 1. Visual Design System (Modern, Not "College Project")

To avoid the cluttered, harsh look of typical self-hosted tools, design around a **Linear / Overseerr-inspired dark aesthetic**:

* **Color Palette:**
  * **Canvas Background:** Deep charcoal (`#09090B`), never pure `#000000`.
  * **Card Surface:** Elevated slate (`#121215`) with hairline `1px` borders (`#27272A`) instead of heavy drop shadows.
  * **Primary Accent:** Crisp Indigo (`#6366F1`) for primary buttons and active states.
  * **Semantic Badges (Muted, Low-Glare):**
    * `AAC 5.1` (needs attention): Soft Amber (`bg-amber-500/15 text-amber-400`)
    * `EAC3` / `AC3` (ready): Soft Emerald (`bg-emerald-500/15 text-emerald-400`)
    * `Metadata` (junk tags present): Soft Zinc (`bg-zinc-500/15 text-zinc-300`)
* **Typography:** Clean sans-serif (`Inter` or system UI stack) for labels; monospace (`JetBrains Mono` / system mono) for filenames, bitrates, and CLI logs.
* **Offline CSS:** Compiled statically via the standalone Tailwind CLI (`app.css`, zero runtime CDN, zero FOUC on mobile).
* **Progressive Disclosure Rule:** Every screen shows only the **20% of controls you use 90% of the time**. Everything else lives inside a collapsed **`Advanced Settings ▾`** section.
* **v1 Scope:** Single-file editing and processing from the File Editor (no bulk multi-select actions in v1).

---

## 2. Navigation & Global Chrome

* **Desktop (768px and above):** Slim left sidebar (220px) with three items: **Dashboard**, **Library**, **Jobs**, plus a **Scan Library** button pinned at the bottom.
* **Mobile (less than 768px):** Clean top header (App title + Scan icon) and a fixed bottom tab bar (**Dashboard**, **Library**, **Jobs**) for thumb-friendly navigation.
* **Global Progress Pill:** Whenever a scan or remux job is running, a sleek floating pill appears above the bottom tab bar (mobile) or bottom-right (desktop):
  `● Stage 1/2: Encoding EAC3 5.1 — 64% (21.4x)`
  Tapping it slides open the **Live Job Drawer** from anywhere in the app.

---

## 3. Screen-by-Screen UI & Flow

### Screen 1: Dashboard (Landing View)

Loads instantaneously from SQLite (`GET /api/dashboard`) on open. Gives you an immediate health check of your library and one-tap entry points into what needs fixing.

```text
+-------------------------------------------------------+
|  MKV Forge                        [ ↻ Scan Library ]  |
+-------------------------------------------------------+
|  +-----------------+  +-----------------+             |
|  | 412             |  | 38              |             |
|  | Total Files     |  | AAC 5.1 Only    |  <-- Tap    |
|  +-----------------+  +-----------------+      to fix |
|  +-----------------+  +-----------------+             |
|  | 94              |  | 12m ago         |             |
|  | Extra Metadata  |  | Last Scan       |             |
|  +-----------------+  +-----------------+             |
|                                                       |
|  QUICK ACTIONS                                        |
|  [ Fix 38 AAC 5.1 Only Files → ]                      |
|  [ Clean 94 Files with Extra Metadata → ]             |
|                                                       |
|  RECENT ACTIVITY                                      |
|  ✓ Dune.Part.Two.mkv       EAC3 5.1 added • 10m ago   |
|  ✓ Seven.Pounds.2008.mkv   Metadata stripped • 2h ago |
+-------------------------------------------------------+
```

* **Stat Cards are Interactive:**
  * Tapping **AAC 5.1 Only** jumps straight to the Library screen with `aac51_only=true` (`has_aac_51 = 1 AND has_ac3_eac3 = 0`) pre-applied.
  * Tapping **Extra Metadata** jumps to the Library screen with `has_metadata=true` (non-empty container title or global tags; ignores `_STATISTICS_*` track tags).
* **Last Scan:** Powered by `app_state.last_scan_at` so incremental scans with 0 changed files still update the timestamp.
* **Empty State (First Launch):** If the SQLite DB is empty, the dashboard shows a single clean card: *"Your library hasn't been indexed yet"* with a prominent **Run Initial Scan** button.

---

### Screen 2: Library Browser

Designed for fast scanning and picking a single target file.

* **Top Controls:**
  * Full-width search bar (`Search movies, shows, folders...`, debounced).
  * Horizontally scrollable filter chips: `All` | `AAC 5.1 Only` | `AAC 5.1` | `Has Metadata` | `No Dolby` | `Folder ▾`.
* **File List (Mobile Cards / Desktop Table):**
  Each item renders from denormalized SQLite columns (`GET /api/files`) with only high-signal info:
  * **Line 1:** Clean filename (truncated gracefully) + file size (`4.2 GB`).
  * **Line 2:** Folder path in muted gray (`/Movies/English`).
  * **Line 3:** Codec & status pills (`1080p` `AAC 5.1` `Has Metadata` `2 Subs`) + a subtle **`↻` Re-scan** icon button (`POST /api/files/{id}/rescan`) on the right to re-probe just that single file if replaced or edited externally.

---

### Screen 3: File Editor (Simple Default + Hidden Advanced)

Tapping a file opens the Editor. The top bar displays the filename, size, and a **Re-scan File** button (`POST /api/files/{id}/rescan`).

#### Default View (Visible Immediately — 3 Clean Cards)

1. **Audio Fix Card** *(highlighted with a subtle indigo border if AAC 5.1 is detected)*:
   * Toggle: **Add Dolby 5.1 Track at Index 0** (ON/OFF)
   * Segmented control: `[ EAC3 5.1 ]` | `[ AC3 5.1 ]`
   * Text input: **Track Title** (auto-filled, e.g., `English EAC3 5.1`)
   * Helper text: *"Original AAC 5.1 will be kept at Index 1 (non-default)."*

2. **Metadata Cleanup Card:**
   * Input: **Container Title** (with a one-tap `× Clear` button inside the input).
   * Toggle: **Strip Global Tags & Junk Metadata** (ON by default when junk tags are detected).

3. **Tracks Card:**
   * Clean list of Video, Audio, and Subtitle tracks.
   * **Live Virtual Preview Row:** When **Add Dolby 5.1 Track at Index 0** is toggled ON, render a highlighted preview row at Audio Index 0 (`[NEW] English EAC3 5.1 • Default`) so the exact output track layout is visible before submitting. If *Drop original AAC 5.1* is checked, visibly dim/uncheck the source AAC row.
   * Each row has: `[✓]` Include checkbox, Track badge (`Audio • AAC 5.1 • eng`), Track Name, and compact `↑` `↓` reorder buttons.

#### Hidden by Default (`▸ Advanced Settings` Accordion)

Tapping **Advanced Settings** smoothly expands power-user controls without cluttering the main flow:

* **Audio Conversion Details:**
  * Source audio track selector (for multi-audio files like Hindi + English; channel count preserves source channels capped at 5.1).
  * Bitrate selector (`640k` / `448k` / `384k`).
  * Track language override.
  * Toggle: *Drop original AAC 5.1 track instead of keeping it at Index 1*.
* **Deep Metadata & Container Stripping:**
  * Strip Track-Level Tags (`--no-track-tags`).
  * Strip Embedded Attachments / Cover Art (`--no-attachments`).
  * Strip Chapters (`--no-chapters`).
* **Per-Track Fine-Tuning:**
  * Edit individual track languages, `Default` flag, and `Forced` flag.
* **Output Behavior:**
  * `[ Replace Original ]` (default, atomic swap; for `.mp4` inputs, writes `<stem>.mkv` and removes `.mp4` after success, failing safely if `<stem>.mkv` already exists) | `[ Keep Both (.remux.mkv) ]` (auto-increments `.remux.2.mkv`, etc. on collision).

#### Progressive-Disclosure Convert Audio Modal (Phase 14)

Instead of a global Simple/Advanced Settings switch, audio conversion uses **inline progressive disclosure** so the default path stays 2 taps while audiophiles unlock full control in 1 tap:

1. **Source-Aware `+ Add / Convert` Dropdown (Max 2–3 Recommendations + `Custom conversion…`):**
   * **AAC 5.1 source:** `E-AC-3 5.1` *(Recommended)*, `AC-3 5.1`, `Custom conversion…`.
   * **TrueHD / DTS-HD 7.1 source:** `FLAC 7.1` *(Lossless)*, `E-AC-3 5.1` *(AVR/Soundbar)*, `Opus 7.1` *(Plex/Jellyfin)*, `Custom conversion…`.
   * **E-AC-3 / AC-3 5.1 source:** `AAC 5.1`, `AAC 2.0` *(Stereo)*, `Custom conversion…`.
   * **2.0 Stereo source:** Top 2 non-identical codecs at `2.0`, plus `Custom conversion…`.
   * Picking any recommended option opens the modal with **Advanced collapsed**; picking `Custom conversion…` opens the modal with **Advanced expanded**.

2. **Collapsed Default Modal View (90% Happy Path):**
   * **Source Track** selector (compact read-only pill when only 1 audio track exists; dropdown for multi-audio files).
   * **Contextual Warning / Info Banner** *(rendered only when a real trade-off applies — see table below)*.
   * **Single Collapsible Disclosure Bar:** `▸ Advanced   E-AC-3 · Match (5.1) · 640k · eng` (shows active codec, resolved channel layout, bitrate or `Lossless`, and language at a glance).
   * **Track Title** input (auto-updates from codec + resolved channels + bitrate + language until manually edited), **Set as default**, and **Drop original source track** toggles.

3. **Expanded `▾ Advanced` Panel (Power Users & Audiophiles):**
   * **Format Pills:** `E-AC-3`, `AC-3`, `AAC`, `Opus`, plus `FLAC` *(shown only when the selected source track is lossless)*.
   * **Channels Pills:** `Match (<src>)` + valid downmixes (`7.1`, `6.1`, `5.1`, `2.0`, `1.0`) strictly filtered to `<= min(source_channels, codec.max_channels)` — never offers upmixing.
   * **Bitrate Pills:** Dynamically adapts to `(target_codec, resolved_channels)`; hidden for lossless `FLAC`.
   * **Language Selector:** ISO 639-2 language dropdown.
   * All pills use responsive `flex-wrap` with zero fixed widths so mobile and accessibility font scales never overflow.

4. **Contextual User-Facing Warnings & Hints:**
   * **Atmos / JOC / DTS:X detected (Amber Warning):** *"Re-encoding strips Dolby Atmos / DTS:X height object metadata — only the flat surround bed is kept. Leave the original track checked if your AVR/soundbar supports Atmos."*
   * **7.1 / 6.1 source → E-AC-3 or AC-3 (Amber Warning):** *"FFmpeg caps Dolby Digital / Plus at 5.1 — this track will downmix <src> → 5.1. Choose FLAC (lossless), Opus, or AAC to preserve <src>."* *(omits FLAC if source is lossy)*.
   * **AC-3 → E-AC-3 at same channels (Amber Warning):** *"Lossy-to-lossy sidegrade: Dolby devices that play E-AC-3 already play AC-3 natively. Re-encoding reduces quality without adding compatibility."*
   * **Same codec + same channels (Caution):** *"Re-encoding the same codec and channel layout causes generation loss. Only useful to reduce bitrate or fix compatibility."*
   * **Explicit downmix (Info):** *"Downmixing <src> → <target> using standard channel coefficients."*
   * **Opus / FLAC surround (Subtle Info):** Notes LPCM playback over HDMI/eARC (not optical SPDIF); highlights bit-exact lossless preservation on `FLAC`.

#### Sticky Bottom Bar & Guardrails

* Shows a 1-line summary of pending changes (e.g., `+ EAC3 5.1 (Index 0) • Strip Metadata`) and a primary **`Process File`** button.
* **Accidental Video Uncheck Guard:** If all video tracks are unchecked, display an explicit confirmation warning (*"Warning: No video track selected"*) before submitting.
* **Duplicate Job Handling:** If the file already has a `queued` or `running` job (`409 Conflict`), surface a clear banner linking to the active job in the Job Drawer.

---

### Screen 0: 3-Step Apple-Style Onboarding Wizard & Global Playback Profile (Phase 15)

Triggered **only** on fresh installs (`auth_mode === "unconfigured"`). Existing configured instances skip the wizard automatically and access the exact same Playback Profile controls inside **Settings → Sound System**.

* **Visual Motion & Ergonomics:**
  * Single centered uniform-height 3-zone responsive card (`max-w-lg`, `.onboarding-wizard-shell` with `height` and `min-height` locked to `min(36rem, calc(100dvh - 1.5rem))`) featuring a fixed-height top progress header (`.onboarding-header-row`, 3-segment bar + Back chevron on Steps 2–3), scrollable middle step body (`.onboarding-body`, `scrollbar-gutter: stable`, auto-resetting `scrollTop` on step transitions), and pinned 2-slot bottom action footer with safe-area padding so window dimensions never jump across Steps 1–3 on desktop or mobile.
  * Smooth directional horizontal slide + fade transitions (`260ms cubic-bezier(0.22, 1, 0.36, 1)`) forward and backward, respecting `prefers-reduced-motion: reduce`.
  * Lossless Back/Next navigation: auth setup, playback profile save, and optional initial scan commit cleanly on Step 3 so refreshing mid-wizard never leaves the instance half-configured.
* **Step 1 — Access:** Direct Username / Password / Re-enter Password inputs with primary blue **`Create`** button and subtle **`Continue Without Sign-In`** text button directly beneath it.
* **Step 2 — Sound System:** Select a hardware preset card (*HDMI ARC Soundbar/TV*, *Optical/Legacy AVR*, *HDMI eARC AVR 7.1*, *Plex/Jellyfin/Infuse*, *Stereo/Mobile 2.0*), optionally expand `▸ Customize codecs & channels` (`aac_20` vs `aac_51`, `eac3`, `ac3`, `dts`, `dts_hd_ma`, `truehd`, `flac`, `opus`, `pcm`, `mp3`; max channels `2.0 / 5.1 / 7.1`; preferred target `E-AC-3 / AC-3 / AAC`), or click *Skip for now*.
* **Step 3 — Ready to Scan:** Displays setup summary + mounted storage path & write-check badge + "What happens next" overview, with single mandatory CTA **`Start Scanning Library`** (commits auth + playback profile, triggers background scan, and lands directly in Dashboard).
* **Generic Dashboard & Library Integration:**
  * Replaces hardcoded Dashboard `"AAC 5.1 Only"` KPI card and Library `"No Dolby"` chip with profile-driven **`Needs Conversion`** (or a 1-tap **`Configure Sound System`** card when profile is `neutral`).
  * Settings includes a dedicated **Sound System (Playback Profile)** panel (global across household users) allowing instant profile switching without re-scanning.

---

### Screen 4: Live Progress & Jobs Drawer

Opens automatically when you tap **Process File** (or when you tap the Global Progress Pill).

* **Active Job Card:**
  * Filename + current stage badge (`Stage 1 of 2: Encoding EAC3 5.1` → `Stage 2 of 2: Remuxing MKV`).
  * Smooth animated progress bar with exact percentage (`64%`) and speed (`21.4x`).
  * Red outline **`Cancel Job`** button.
  * Collapsed **`▸ Show Live CLI Logs`** toggle (expands a dark monospace terminal box streaming raw `ffmpeg`/`mkvmerge` output only if you want to inspect it).
* **Queue & History:**
  * Below the active job: Queued items (with remove/cancel button) and Completed/Failed/Cancelled history with timestamps, warnings (if `mkvmerge` exited with code `1`), and error details if anything failed.

---

## 4. End-to-End User Flow (Mobile Happy Path)

1. **Open App:** Dashboard loads from SQLite in under 100ms. You see `38 AAC 5.1 Only`.
2. **Pick File:** Tap the card → Library opens filtered to `AAC 5.1 Only` → tap a movie.
3. **Configure (2 Taps):** Toggle **Add Dolby 5.1 at Index 0** ON (confirm the `[NEW]` preview row appears at Audio Index 0), verify **Strip Metadata** is ON, tap **Process File**. *(Never need to open Advanced unless it's a multi-language edge case.)*
4. **Watch or Walk Away:** Progress drawer shows live Stage 1 (FFmpeg) and Stage 2 (`mkvmerge`) percentages over keepalive-protected SSE. You can lock your phone; when you return, SSE reconnects automatically, the file is atomically swapped, SQLite is auto-updated, and the movie disappears from your `AAC 5.1 Only` count.
