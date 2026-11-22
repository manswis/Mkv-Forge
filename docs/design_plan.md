# MKV Forge — UI/UX Redesign Plan & Progress Tracker

> **Workflow:** Execute one phase at a time with zero Alpine.js / backend regressions. Recompile `app/static/css/app.css` from `frontend/input.css` via Tailwind CLI (`v3.4.17`, matching `Dockerfile` Stage 1), run `pytest -q tests/unit` after every phase, and verify visually across **Desktop (1440px) + Mobile (390px)** in **Dark & Light** modes before checking off (`[x]`) each milestone.

---

## 1. Design Direction: "Precision Pro Utility"

MKV Forge is a cross-platform, self-hosted media utility (FFmpeg + MKVToolNix) accessed from desktop browsers (macOS, Windows, Linux) and phones (iOS, Android). Its UI must feel **calm, dense, tactile, and unmistakably human-crafted** — closer to **Linear, Raycast, Vercel Geist, and Apple Pro Tools (Finder / Compressor / Logic)** than either a generic AI Tailwind template or an airy consumer iOS app.

### 1.1 Adopting `dickwu/apple-design-skill` Selectively (Adopt vs. Reject)

| Adopt (High Value for MKV Forge) | Reject (Wrong Fit for a Media Utility) |
| :--- | :--- |
| **Mobile touch ergonomics:** Minimum `44×44px` hit targets, thumb-zone bottom bar, `env(safe-area-inset-bottom)` clearance, bottom-sheet modal presentation | **Consumer spaciousness:** 34pt Large Titles and 17pt airy body text waste viewport on long MKV filenames, track tables, and CLI logs |
| **Inset hairline-divided row groups:** Clean `1px` horizontal dividers inside Settings, File Editor, and Modals instead of nested boxes | **Heavy translucent "Liquid Glass" blur:** `backdrop-filter` over scrolling 500-row media tables causes mobile scroll jank and degrades dark-mode contrast |
| **Visual restraint & micro-copy:** Ruthlessly trim redundant helper prose; one clear primary action per view | **iOS-only chrome conventions:** Avoid fake native iOS chevrons, oversized pill buttons everywhere, or macOS traffic-light skeuomorphism on web |
| **Semantic adaptive color tokens:** Express elevation and text hierarchy via semantic CSS variables rather than raw hex overrides | **Bouncy spring physics:** Utility interactions should be crisp, fast (`120–180ms`), and honor `prefers-reduced-motion: reduce` |

### 1.2 The 5 "AI-Generated UI" Anti-Patterns We Are Eliminating

1. **Card Soup (Box-in-Box-in-Box):** Rounded bordered containers inside rounded bordered panels inside rounded bordered modals, all competing with identical `#27272A` borders.
2. **Rainbow Badge Noise:** Multiple tinted pills (`amber`, `emerald`, `sky`, `indigo`, `zinc`) stacked on every Library file row so nothing stands out.
3. **Micro-Typography Collapse:** Heavy reliance on `text-[10px]` and `text-[11px]` across four low-contrast grays (`zinc-200` → `zinc-500`) instead of a crisp 3-level hierarchy.
4. **Control Grammar Drift:** Inconsistent button heights (`min-h-[34px]`, `min-h-[40px]`, `py-1.5`, `py-2`), radii (`rounded-md`, `rounded-lg`, `rounded-xl`, `rounded-2xl`), and padding (`p-3`, `p-3.5`, `p-4`, `p-5`) accumulated across 16 phases.
5. **Brittle Light Mode Overrides:** Global `!important` overrides on `.text-zinc-*` and `.bg-zinc-*` in `frontend/input.css` instead of native semantic tokens.

---

## 2. Locked Design Token Specification

All visual constants live centrally in `frontend/input.css` and `tailwind.config.js` (zero magic numbers scattered across inline utility strings).

### 2.1 Surface Elevation, Interactive Fills, Shadows & Translucent Hairlines

All solid surface and text tokens are defined as **RGB channel triples** (`--rgb-*`) and mapped in `tailwind.config.js` via `rgb(var(--rgb-*) / <alpha-value>)` so Tailwind v3 opacity modifiers (`bg-elevated/90`, `text-ink-secondary/80`, etc.) work natively alongside `--color-*` aliases.

| Semantic Token | Dark Mode (`html.dark`) | Light Mode (`:root` / `html.light`) | Usage |
| :--- | :--- | :--- | :--- |
| `--rgb-canvas` / `--color-canvas` | `9 9 11` (`#09090B`) | `244 244 245` (`#F4F4F5`) | Root app background |
| `--rgb-surface` / `--color-surface` | `17 17 20` (`#111114`) | `255 255 255` (`#FFFFFF`) | Primary cards, sidebar, main panels |
| `--rgb-elevated` / `--color-elevated` | `24 24 28` (`#18181C`) | `255 255 255` (`#FFFFFF` + `--shadow-elevated`) | Popovers, modals, sticky bars, sheets |
| `--rgb-inset` / `--color-inset` | `13 13 16` (`#0D0D10`) | `236 236 240` (`#ECECF0`) | Segmented tracks, input wells, code/log wells |
| `--color-hover-fill` | `rgba(255, 255, 255, 0.04)` | `rgba(9, 9, 11, 0.04)` | Subtle row & nav hover feedback |
| `--color-active-fill` | `rgba(255, 255, 255, 0.075)` | `rgba(9, 9, 11, 0.075)` | Active/pressed row & nav fill |
| `--shadow-panel` | `none` | `0 1px 2px rgba(9, 9, 11, 0.04)` | Crisp Light Mode separation for `#FFFFFF` cards on `#F4F4F5` |
| `--shadow-elevated` | `0 2px 6px rgba(0,0,0,0.40), 0 16px 36px -8px rgba(0,0,0,0.65)` | `0 1px 2px rgba(9,9,11,0.06), 0 10px 28px -6px rgba(9,9,11,0.12)` | Modals, sheets, popovers, context menus |
| `--color-border-subtle` | `rgba(255, 255, 255, 0.06)` | `rgba(9, 9, 11, 0.08)` | Row dividers, quiet panel edges |
| `--color-border-default` | `rgba(255, 255, 255, 0.11)` | `rgba(9, 9, 11, 0.13)` | Inputs, secondary buttons, interactive edges |
| `--color-border-strong` | `rgba(255, 255, 255, 0.20)` | `rgba(9, 9, 11, 0.24)` | Focused inputs, active selection rings |

### 2.2 Text Hierarchy (3 Levels Only)

| Semantic Token | Dark Mode | Light Mode | Usage |
| :--- | :--- | :--- | :--- |
| `--rgb-text-primary` / `--color-text-primary` | `244 244 245` (`#F4F4F5`) | `17 17 20` (`#111114`) | Filenames, headings, KPI values, active labels |
| `--rgb-text-secondary` / `--color-text-secondary` | `161 161 170` (`#A1A1AA`) | `82 82 91` (`#52525B`) | Descriptions, secondary controls, table headers |
| `--rgb-text-muted` / `--color-text-muted` | `113 113 122` (`#71717A`) | `113 113 122` (`#71717A`) | Folder paths, timestamps, disabled hints |

### 2.3 Color Discipline (Monochrome-First, Signal-Only Color)

- **Neutral Metadata (Zero Color):** Video resolution (`2160p`, `1080p`), codec (`HEVC`, `H.264`), audio summary (`EAC3 5.1`), subtitle counts (`2 Subs`), and file sizes render in quiet monochrome (`--color-text-secondary` / `--color-border-subtle`).
- **WCAG-AA Accent Contrast:** All 6 Light Mode accents guarantee `>= 4.5:1` contrast with pure white text (`#FFFFFF`) — including Amber (`#B45309`, `5.02:1`) and Emerald (`#047857`, `5.48:1`). Solid primary controls use `--color-accent-fg` so high-luminance Dark Mode accents (`amber`, `emerald`) stay crisp and accessible (`>= 8:1`).
- **Actionable Signal Only:**
  - **Accent (`--color-accent`):** Single primary CTA per screen/modal, active nav state, active segmented pill, focus rings (preserves all 6 WCAG-AA accent presets: `blue`, `indigo`, `emerald`, `violet`, `rose`, `amber`).
  - **Amber (Warning / Attention):** `Needs Conversion`, `Has Metadata`, soft hardware warnings.
  - **Emerald (Healthy / Complete):** Completed jobs, test file ready, verified writable storage.
  - **Red (Destructive / Error):** Failed jobs, hard validation blocks, delete/factory reset actions.

### 2.4 Typography, Numerics, Radius & Control Sizing

| Category | Standard |
| :--- | :--- |
| **Type Scale** | `Page Title`: `1.125rem–1.25rem` (18–20px, semibold, `-0.015em` tracking) · `Section Title`: `0.875rem` (14px, semibold) · `Body / Control`: `0.8125rem–0.875rem` (13–14px) · `Meta / Table Header`: `0.75rem` (12px). Eliminate `text-[10px]` for any readable content. |
| **Tabular Numerics** | `font-variant-numeric: tabular-nums` enforced globally on `.ui-num`, `.font-mono`, KPI counters, file sizes, bitrates, durations, and job progress percentages. |
| **Radius Scale** | `6px` (`--radius-sm`) for buttons/inputs/tags · `10px` (`--radius-md`) for panels/list groups · `14px` (`--radius-lg`) for modals/sheets · `9999px` strictly for status dots & toggles. |
| **Control Heights** | `32px` compact desktop action · `36px` standard button/input · `44px` minimum mobile touch target (`min-h-[44px]` below `md`). |
| **Motion** | `140ms cubic-bezier(0.2, 0, 0, 1)` for hover/press/disclosure; `220ms cubic-bezier(0.22, 1, 0.36, 1)` for drawers/sheets; `prefers-reduced-motion: reduce` disables all transforms. |

---

## 3. Reusable Component Primitives (`frontend/input.css`)

Defined inside `@layer components` (and synced into compiled `app/static/css/app.css`) so every screen shares identical visual DNA:

- **`.ui-panel`**: Primary surface container (`background: var(--color-surface)`, `1px solid var(--color-border-subtle)`, `border-radius: var(--radius-md)`). Never nested inside another `.ui-panel`.
- **`.ui-list-group`**: Inset container whose direct children are separated by `1px solid var(--color-border-subtle)` horizontal hairlines (`> * + *`), replacing card-inside-card stacks across Settings, File Editor, and Modals.
- **`.ui-btn` + modifiers (`.ui-btn-primary`, `.ui-btn-secondary`, `.ui-btn-ghost`, `.ui-btn-danger`, `.ui-btn-sm`)**: Unified height, padding, focus ring, disabled opacity, and crisp active press state.
- **`.ui-segmented` + `.ui-segmented-item`**: Single inset track (`var(--color-inset)`) with elevated active pill used across Library View Switcher, Output Mode, Codec/Timing toggles, and Theme Switcher.
- **`.ui-tag`**: Quiet monochrome hairline metadata tag (`12px`, `tabular-nums`, zero loud tint).
- **`.ui-status` + modifiers (`.ui-status-warn`, `.ui-status-ok`, `.ui-status-err`, `.ui-status-info`)**: Compact status indicator (dot + label or quiet tinted pill) strictly for actionable states.
- **`.ui-input` / `.ui-select`**: Consistent input surface (`var(--color-inset)`), border (`var(--color-border-default)`), focus ring, and placeholder contrast across Dark & Light mode.
- **`.ui-sheet`**: Responsive modal shell — centered dialog (`max-w-*`, `border-radius: var(--radius-lg)`) on desktop (`sm:`), crisp pinned bottom-sheet (`rounded-t-2xl`, `pb-safe-modal`) on mobile with pinned header, scrollable body, and pinned footer.

---

## 4. Phased Implementation Plan & Progress Tracker

### Phase 0: CSS Build Pipeline Parity & Lightweight Baseline

Goal: Guarantee byte-identical Tailwind CSS compilation between local macOS dev and Docker Stage 1, keeping baseline capture lightweight so momentum stays high.

- [x] **0.1 Local CSS Build Script (`scripts/build_css.sh`)**
  - [x] Verified `scripts/build_css.sh` invokes Tailwind CLI `v3.4.17` (`tailwindcss -i ./frontend/input.css -o ./app/static/css/app.css --minify`, matching `Dockerfile` line 3 & line 33).
  - [x] Verified running `./scripts/build_css.sh` produces zero warnings and passes `tests/unit/test_index_html_structure.py`.
- [x] **0.2 Lightweight Baseline & Phase Hygiene**
  - [x] Keep visual verification focused on key viewports (Desktop `1440px` + Mobile `390px`, Dark & Light) per touched phase rather than heavy pre-capture suites; audit every touched `index.html` block for zero stray `text-[10px]` and zero dropped Alpine directives, committing cleanly after each phase.

---

### Phase 1: Design Tokens & Component Primitives Foundation

Goal: Upgrade the CSS token layer (RGB triples for Tailwind v3 `<alpha-value>` opacity compatibility, Light Mode contrast/elevation, WCAG-AA Amber & Emerald) and add reusable `.ui-*` primitives in `frontend/input.css`, `tailwind.config.js`, and `app/static/js/theme.js` with **zero visual breakage** to existing HTML.

- [x] **1.1 Token System Upgrade (`frontend/input.css`, `tailwind.config.js`, `app/static/js/theme.js`)**
  - [x] Add RGB channel triples (`--rgb-elevated`, `--rgb-inset`, `--rgb-text-primary`, `--rgb-text-secondary`, `--rgb-text-muted`) plus `--color-hover-fill`, `--color-active-fill`, `--shadow-panel`, `--shadow-elevated`, `--color-border-subtle/default/strong`, `--color-accent-fg`, and radius/height/motion tokens for both Light (`:root` / `html.light`) and Dark (`html.dark`).
  - [x] Upgrade Light Mode Amber (`#B45309`, `5.02:1`) and Emerald (`#047857`, `5.48:1`) accents to pass WCAG-AA (`>= 4.5:1`) with white text, and sync `SURFACE_PALETTES` / `ACCENT_PALETTES` in `app/static/js/theme.js`.
  - [x] Expose `elevated`, `inset`, `ink` (`primary`/`secondary`/`muted`), `borderColor`, and `boxShadow` (`panel`, `elevated`) in `tailwind.config.js` using `rgb(var(--rgb-*) / <alpha-value>)`.
  - [x] Enforce `font-variant-numeric: tabular-nums` utility rules (`.ui-num`, `.font-mono`).
  - [x] Keep legacy `!important` Light Mode overrides intact until the end of Phase 6 so Light Mode never regresses mid-migration; preserve all unlayered `.onboarding-*` CSS rules.
- [x] **1.2 Component Primitives (`frontend/input.css`, `tailwind.config.js`)**
  - [x] Implement `@layer components` primitives (safelisted for `ui-*` so utilities always win cascade order and primitives emit before screens migrate): `.ui-panel`, `.ui-list-group`, `.ui-btn*`, `.ui-segmented*`, `.ui-tag`, `.ui-status*`, `.ui-input`, `.ui-select`, `.ui-sheet`.
  - [x] Compile `app/static/css/app.css` via `./scripts/build_css.sh`, bump `app.css` & `theme.js` cache-busters in `app/static/index.html`, and add exhaustive unit tests in `tests/unit/test_design_system.py`.

---

### Phase 2: Global App Shell, Navigation & Overlays

Goal: Make the persistent frame (sidebar, mobile header, bottom tab bar, floating live pill, toasts, context menu) calm, crisp, and effortless to navigate.

Target regions in `app/static/index.html`:
- Desktop Sidebar (`L68–245`)
- Mobile Top Header + Docked Activity Strip + Bottom Tab Bar (`L246–546`)
- Floating Toast Stack (`L547–606`)
- Global Floating Context Menu (`L7209–7344`)

- [ ] **2.1 Desktop Sidebar Polish (`L68–245`)**
  - [ ] Replace boxed borders on nav items with quiet hover fills and a crisp active-item surface + subtle left accent indicator.
  - [ ] Group navigation cleanly (`Dashboard`, `Library`, `Audio Test`, `Jobs` — preserving exact test ordering `Library → Audio Test → Jobs`) with tighter visual rhythm and refined footer (`Settings`, `Scan`, `Account/Sign Out`).
- [ ] **2.2 Mobile Header, Overflow Menu & Bottom Tab Bar (`L246–546`)**
  - [ ] Tighten top bar height and quiet bottom hairline; ensure every mobile button/menu row meets `min-h-[44px]`.
  - [ ] Refine 3-dot overflow menu into crisp hairline-divided groups (preserving exact ordering `Full Re-scan → Audio Test Generator → Select` and footer `About → User → Sign Out`).
  - [ ] Polish bottom tab bar active indicator and docked live activity pill with clean safe-area clearance (`pb-safe`).
- [ ] **2.3 Toasts & Context Menu (`L547–606`, `L7209–7344`)**
  - [ ] Align toast cards and right-click context menus onto `--color-elevated` + `--color-border-default` with tight padding and crisp keyboard/hover focus states.
- [ ] **2.4 Phase 2 Verification**
  - [ ] Compile CSS + run `pytest -q tests/unit` + inspect Desktop & Mobile (Dark & Light).

---

### Phase 3: Library Explorer (Highest Daily-Use Screen)

Goal: Eliminate badge clutter and stacked toolbars so filenames, folders, and actionable health status are scannable in a fraction of a second.

Target regions in `app/static/index.html`:
- Library Header, Breadcrumbs, Search & Filter Controls (`L1093–1619`)
- Folders Tile Grid (`L1620–1837`)
- List View: Subfolders, Desktop Table & Mobile File Cards (`L1838–2360`)

- [ ] **3.1 Unified Control Toolbar (`L1093–1619`)**
  - [ ] Consolidate View Switcher (`Folders | All Files`), Search Input, Filter Chips, and Sort/Layout toggles into unified `36px` (desktop) / `44px` (mobile) `.ui-segmented` and `.ui-input` controls.
  - [ ] Streamline Breadcrumb bar + Folder actions (`New Folder`, `Generate Test File`, `Select`) with quiet ghost buttons instead of competing bordered rectangles.
- [ ] **3.2 Quiet Metadata & Signal-Only Status (`L1838–2360`)**
  - [ ] Convert informational specs (`2160p`, `HEVC`, `EAC3 5.1`, `2 Subs`) from tinted pills into clean monochrome `.ui-tag` / `·`-delimited metadata so the media filename dominates visual hierarchy.
  - [ ] Reserve color strictly for actionable states (e.g., quiet amber `.ui-status-warn` for `Needs Conversion` / `Has Metadata`; quiet neutral/check when clean).
  - [ ] Enforce `tabular-nums` alignment on file size, item counts, and track counts across desktop table columns; keep mobile cards to 2 crisp scannable lines with generous tap targets.
- [ ] **3.3 Folders Grid & Bulk Action Bar (`L1620–1837`)**
  - [ ] Replace heavy square card outlines on folder tiles with subtle surface elevation, quiet hover lift, and crisp truncation for long folder names.
  - [ ] Polish multi-select bulk bar into a sleek floating elevated bar (`--color-elevated` + `--color-border-strong`).
- [ ] **3.4 Phase 3 Verification**
  - [ ] Compile CSS + run `pytest -q tests/unit` + test long filenames, narrow 320px/390px viewports, and 125% font scale.

---

### Phase 4: File Editor & Track Inspector

Goal: Refactor the File Editor from stacked bordered cards into a clean **Inspector Layout** (Linear / Logic Pro style) where every track, toggle, and virtual preview aligns on a single crisp grid.

Target regions in `app/static/index.html`:
- Editor Header & Status Banners (`L2361–2585`)
- Metadata Cleanup Section (`L2586–2691`)
- Tracks Inspector & Live `[NEW]` Preview Row (`L2692–3126`)
- Attachments & Output Behavior (`L3127–3325`)
- Collapsed Advanced Settings (`L3326–3406`)
- Sticky Bottom Action Bar (`L5473–5544`)

- [ ] **4.1 Editor Header & Inspector Sections (`L2361–2691`, `L3127–3406`)**
  - [ ] Replace nested bordered boxes with crisp section headings + `.ui-list-group` hairline-divided rows for Metadata Cleanup, Attachments, Output Behavior (`.ui-segmented`), and Collapsed Advanced Settings.
  - [ ] Trim redundant explanatory paragraphs into sharp 1-line helper text (`12px`, `--color-text-secondary`).
- [ ] **4.2 Tracks List & Live `[NEW]` Virtual Preview (`L2692–3126`)**
  - [ ] Align each track row cleanly: include checkbox · track type icon/tag · editable title · codec/channels/language/size (`tabular-nums`) · compact `Default`/`Forced` toggles · `↑`/`↓` reorder controls.
  - [ ] Style the live `[NEW]` converted-audio preview and `[IMPORT]` external track rows with a crisp `2px` left accent indicator + quiet tag instead of heavy tinted full-box outlines.
  - [ ] Preserve all `test_index_html_structure.py` landmarks (video/audio track sizes, `+ Add / Convert` dropdown, `editorRecommendedAudioOptions`).
- [ ] **4.3 Sticky Bottom Action Bar (`L5473–5544`)**
  - [ ] Elevate cleanly above content with subtle top hairline, 1-line pending-changes summary, and a single unmistakable primary **Process File** button.
- [ ] **4.4 Phase 4 Verification**
  - [ ] Compile CSS + run `pytest -q tests/unit` + verify mobile & desktop editor with multi-audio/subtitle tracks and long track names.

---

### Phase 5: Dashboard, Jobs Page & Live Jobs Drawer

Goal: Give the landing Dashboard and Job Monitor crisp data density and high signal-to-noise ratio.

Target regions in `app/static/index.html`:
- Dashboard (`L645–1092`)
- Jobs Page (`L3407–3655`)
- Slide-Over Live Jobs Drawer (`L6999–7208`)

- [ ] **5.1 Dashboard KPI Strip, Charts & Recent Activity (`L645–1092`)**
  - [ ] Unify the KPI cards into a single hairline-divided stat strip (`divide-x` / `divide-y` inside `.ui-panel`) with bold `tabular-nums` metrics and clear hover affordance on interactive stats.
  - [ ] Frame codec/library charts cleanly inside `.ui-panel` with quieter legends (keeping `charts.js` SVG in-place patching untouched).
  - [ ] Convert Recent Activity rows into a clean `.ui-list-group` with monochrome timestamps, `by <User>` attribution tag, and quiet status dot.
- [ ] **5.2 Jobs Page & Slide-Over Jobs Drawer (`L3407–3655`, `L6999–7208`)**
  - [ ] Tighten active job progress card: clean Stage badge, smooth progress bar, aligned `tabular-nums` speed/ETA/percent readout, and recessed `--color-inset` live CLI log terminal.
  - [ ] Use `.ui-list-group` for Queued Jobs and History rows with clear status indicators and expandable warning details.
- [ ] **5.3 Phase 5 Verification**
  - [ ] Compile CSS + run `pytest -q tests/unit` + inspect empty state, active scan, active job, and populated history states.

---

### Phase 6: Unified Modals, Settings Split View, Onboarding & Auth

Goal: Guarantee every modal, dialog, and settings screen feels like part of the exact same product — uniform headers, body rhythm, footers, and mobile bottom-sheet presentation.

Target regions in `app/static/index.html`:
- Settings Full-Screen Split View (`L3656–5472`)
- Convert Audio Modal (`L5545–5785`)
- Import External Track Modal (`L5786–6052`)
- Speaker & Audio System Test Generator Modal (`L6053–6424`)
- Folder Picker, New Folder, Rename, Confirm & Factory Reset Modals (`L6425–6998`, `L7716–7842`)
- File MediaInfo Modal (`L7345–7715`)
- 3-Step Onboarding Wizard & Sign-In Screen (`L7843–8405`)

- [ ] **6.1 Settings Split View (`L3656–5472`)**
  - [ ] Refactor General & Display, Sound System / Playback Profile, Media & Remuxing, Storage & System (Maintenance + Auth + API Keys), and About into clean `.ui-list-group` sections (Apple Settings clarity + Linear density).
  - [ ] Preserve every landmark verified by `test_index_html_structure.py` (Local Auth panel scoping, API key `Fmt.formatDateTime`, Sound System controls, Danger Zone).
- [ ] **6.2 Feature Modals (`L5545–6424`, `L7345–7715`)**
  - [ ] Apply `.ui-sheet`, `.ui-segmented`, `.ui-list-group`, and `.ui-btn` across Convert Audio, Import Track, Speaker Test Generator, and MediaInfo modals.
  - [ ] Keep Progressive Disclosure (`Advanced options`) crisp and unobtrusive with identical disclosure row chrome across Convert Audio and Audio Test.
- [ ] **6.3 Utility & Confirmation Dialogs (`L6425–6998`, `L7716–7842`)**
  - [ ] Standardize Folder Picker, New Folder, Rename, Sign Out, Cancel Job, Delete, and Factory Reset dialogs onto uniform `.ui-sheet` header/footer rhythm and crisp destructive confirmation hierarchy.
- [ ] **6.4 Onboarding Wizard & Sign-In Polish (`L7843–8405`)**
  - [ ] Align typography, input wells, and button heights with the new tokens while strictly preserving the locked uniform 3-zone `.onboarding-wizard-shell` geometry and directional slide animations.
- [ ] **6.5 Apple HIG Micro-Copy Audit (All Screens)**
  - [ ] Shorten verbose multi-sentence helper blurbs into crisp, scannable one-liners without losing any technical warnings or safety explanations.
- [ ] **6.6 Legacy Light Mode Override Cleanup (`frontend/input.css`)**
  - [ ] Only after all screens (Phases 2–6) use the new semantic tokens, remove obsolete `!important` `.text-zinc-*` / `.bg-zinc-*` Light Mode overrides from `frontend/input.css` and verify Light Mode contrast across every screen.
- [ ] **6.7 Phase 6 Verification**
  - [ ] Compile CSS + run `pytest -q tests/unit` + verify zero stray `text-[10px]` or dropped Alpine directives.

---

### Phase 7: Full QA Matrix, Docker CSS Build & Release Readiness

Goal: Prove zero regressions across automated tests, Docker multi-stage CSS compilation, accessibility settings, and real-world devices.

- [ ] **7.1 Automated Test & Build Gates**
  - [ ] Run full unit test suite: `pytest -q tests/unit` (all 452+ tests green, including `test_index_html_structure.py` tag balance & landmark guards).
  - [ ] Run JS syntax check across all static scripts (`node --check app/static/js/*.js`).
  - [ ] Verify Docker CSS Stage 1 build cleanly compiles `frontend/input.css` → `app/static/css/app.css` (`docker build --target css-builder .`).
  - [ ] Verify zero external network/CDN/font requests in DevTools (100% offline compliance).
- [ ] **7.2 Visual & Accessibility Matrix Sign-Off**
  - [ ] **Viewports:** Desktop (`1440×900`, `1280×720`) + Mobile (`390×844`, `360×800`).
  - [ ] **Themes:** Dark mode + Light mode + all 6 accent colors (`blue`, `indigo`, `emerald`, `violet`, `rose`, `amber`).
  - [ ] **Accessibility:** `125%` font scale (zero clipped labels or horizontal overflow), `prefers-reduced-motion: reduce`, keyboard `Tab`/`Esc` navigation across every modal, and `>=44px` mobile touch targets.
  - [ ] **Before vs. After Review:** Side-by-side comparison against Phase 0 baseline screenshots.

---

## 5. Non-Negotiable Engineering Guardrails

1. **Zero Business-Logic Changes During Visual Phases:** Do not modify backend APIs or Alpine controller logic (`app/static/js/*.js`) unless fixing a pure styling class binding.
2. **Single Source of Truth for CSS:** Every style change goes in `frontend/input.css` first and is compiled into `app/static/css/app.css` — never hand-edit `app.css` directly.
3. **Zero Magic Numbers:** All spacing, elevation, radius, height, and motion tokens belong in `frontend/input.css` `:root` / `tailwind.config.js`.
4. **Test-First Landmark Preservation:** Every phase must keep `tests/unit/test_index_html_structure.py` and the full `tests/unit` suite 100% green before moving on.
5. **No Regressions on Mobile Safe Areas or Scroll Containers:** Keep `100dvh`, `pb-safe`, `pb-safe-modal`, `pb-safe-drawer`, and `overflow-y-auto` constraints intact across every sheet and scrollable list.
