"""Exhaustive Phase C4 Node VM tests for `chapters.js`, `api.js`, `constants.js`, and `editor.js`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.models.schemas import ChapterEntry, JobCreateRequest
from app.services.chapter_service import parse_chapters_text

STATIC_JS_DIR = (
    Path(__file__).resolve().parents[2] / "app" / "static" / "js"
)


def _run_node_script(js_code: str) -> str:
    """Execute a Node.js verification script and return stdout."""
    proc = subprocess.run(
        ["node", "-e", js_code],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class TestChaptersFrontendConstantsAndApi:
    """Verify frozen chapter/scene-picker constants and thin `api.js` wrappers."""

    def test_constants_and_api_chapter_methods(self) -> None:
        js_dir = str(STATIC_JS_DIR)
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(js_dir)};
        const fetchCalls = [];
        const ctx = {{
          window: null,
          globalThis: null,
          URLSearchParams,
          encodeURIComponent,
          AbortController,
          setTimeout,
          clearTimeout,
          fetch: async (url, opts) => {{
            fetchCalls.push({{ url, opts }});
            return {{
              ok: true,
              status: 200,
              json: async () => ({{ ok: true, url }}),
            }};
          }},
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "api.js"]) {{
          vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
        }}

        const C = ctx.MKVForgeConstants;
        const Api = ctx.MKVForgeApi;

        // 1. Constants integrity & immutability
        if (C.CHAPTERS.ACTION_KEEP !== "keep") throw new Error("ACTION_KEEP mismatch");
        if (C.CHAPTERS.ACTION_REPLACE !== "replace") throw new Error("ACTION_REPLACE mismatch");
        if (C.CHAPTERS.ACTION_CLEAR !== "clear") throw new Error("ACTION_CLEAR mismatch");
        if (C.CHAPTERS.MAX_COUNT !== 500) throw new Error("MAX_COUNT mismatch");
        if (C.CHAPTERS.MAX_TITLE_LEN !== 200) throw new Error("MAX_TITLE_LEN mismatch");
        if (JSON.stringify(C.CHAPTERS.QUICK_INTERVALS_MIN) !== "[5,10,15]") throw new Error("QUICK_INTERVALS_MIN mismatch");
        if (JSON.stringify(C.CHAPTERS.QUICK_NAME_CHIPS) !== '["Intro","Opening","Intermission","Song","Credits"]') throw new Error("QUICK_NAME_CHIPS mismatch");
        if (JSON.stringify(C.CHAPTERS.NUDGE_STEPS_SEC) !== "[-30,-5,-1,1,5,30]") throw new Error("NUDGE_STEPS_SEC mismatch");
        if (C.SCENE_PICKER.PEEK_OFFSET_SEC !== 15) throw new Error("PEEK_OFFSET_SEC mismatch");
        if (C.SCENE_PICKER.EOF_MARGIN_MS !== 500) throw new Error("EOF_MARGIN_MS mismatch");
        if (JSON.stringify(C.SCENE_PICKER.SCAN_DENSITY_PRESETS) !== "[12,24,40]") throw new Error("SCAN_DENSITY_PRESETS mismatch");
        if (C.SCENE_PICKER.SCAN_DEFAULT_DENSITY !== 24) throw new Error("SCAN_DEFAULT_DENSITY mismatch");
        if (C.SCENE_PICKER.SCAN_DENSITY_STORAGE_KEY !== "mkvforge.scene_scan_density") throw new Error("SCAN_DENSITY_STORAGE_KEY mismatch");
        if (C.SCENE_PICKER.FINE_TUNE_DEBOUNCE_MS !== 180) throw new Error("FINE_TUNE_DEBOUNCE_MS mismatch");
        if (C.NAV_HISTORY.LAYERS.CHAPTERS_MODAL !== "chapters_modal") throw new Error("CHAPTERS_MODAL layer missing");

        // 2. buildSceneFrameUrl positive & negative edge cases
        const u1 = Api.buildSceneFrameUrl(42, 12345.6, "accurate", "42-1000-2000-3000");
        if (u1 !== "/api/files/42/frame?ts_ms=12346&mode=accurate&v=42-1000-2000-3000") {{
          throw new Error("Unexpected accurate frame URL: " + u1);
        }}
        const u2 = Api.buildSceneFrameUrl(7, -50, "invalid-mode");
        if (u2 !== "/api/files/7/frame?ts_ms=0&mode=fast") {{
          throw new Error("Unexpected clamped/default frame URL: " + u2);
        }}
        const u3 = Api.buildSceneFrameUrl(7, "not-a-num");
        if (u3 !== "/api/files/7/frame?ts_ms=0&mode=fast") {{
          throw new Error("Unexpected NaN frame URL: " + u3);
        }}

        // 3. getFileChapters & parseFileChapters async calls
        (async () => {{
          await Api.getFileChapters(15);
          await Api.parseFileChapters(15, "00:00 Intro", "notes.txt");
          await Api.parseFileChapters(15, null);

          if (fetchCalls.length !== 3) throw new Error("Expected 3 fetch calls");
          if (fetchCalls[0].url !== "/api/files/15/chapters") throw new Error("Bad chapters GET url");
          if (fetchCalls[1].url !== "/api/files/15/chapters/parse") throw new Error("Bad parse POST url");
          const body1 = JSON.parse(fetchCalls[1].opts.body);
          if (body1.raw_text !== "00:00 Intro" || body1.filename !== "notes.txt") {{
            throw new Error("Bad parse body1: " + fetchCalls[1].opts.body);
          }}
          const body2 = JSON.parse(fetchCalls[2].opts.body);
          if (body2.raw_text !== "" || body2.filename !== null) {{
            throw new Error("Bad parse body2: " + fetchCalls[2].opts.body);
          }}
          console.log("API_CONSTANTS_OK");
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        out = _run_node_script(script)
        assert "API_CONSTANTS_OK" in out


class TestChaptersPureEngine:
    """Exhaustive positive + negative tests for `app/static/js/chapters.js` and backend parity."""

    def test_all_chapters_js_functions_and_backend_cross_parse(self) -> None:
        js_dir = str(STATIC_JS_DIR)
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(js_dir)};
        const ctx = {{ window: null, globalThis: null }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "chapters.js"]) {{
          vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
        }}
        const Ch = ctx.MKVForgeChapters;

        const eq = (a, b, msg) => {{
          if (JSON.stringify(a) !== JSON.stringify(b)) {{
            throw new Error(`${{msg}}: expected ${{JSON.stringify(b)}}, got ${{JSON.stringify(a)}}`);
          }}
        }};

        // 1. formatChapterTime (default, includeMs, negative, NaN, hours >= 10)
        eq(Ch.formatChapterTime(0), "00:00:00", "fmt 0");
        eq(Ch.formatChapterTime(-500), "00:00:00", "fmt neg");
        eq(Ch.formatChapterTime(NaN), "00:00:00", "fmt NaN");
        eq(Ch.formatChapterTime(3661505), "01:01:01", "fmt h:m:s");
        eq(Ch.formatChapterTime(3661505, {{ includeMs: true }}), "01:01:01.505", "fmt includeMs");
        eq(Ch.formatChapterTime(36000000), "10:00:00", "fmt 10h");

        // 2. parseChapterTimeInput (HH:MM:SS.mmm, comma, MM:SS, seconds, invalid/negative)
        eq(Ch.parseChapterTimeInput("01:02:03.456"), 3723456, "parse HMS.mmm");
        eq(Ch.parseChapterTimeInput("01:02:03,4"), 3723400, "parse HMS,d");
        eq(Ch.parseChapterTimeInput("00:05:00"), 300000, "parse HMS");
        eq(Ch.parseChapterTimeInput("12:30"), 750000, "parse MS");
        eq(Ch.parseChapterTimeInput("5:07.5"), 307500, "parse M:S.d");
        eq(Ch.parseChapterTimeInput("90"), 90000, "parse raw sec");
        eq(Ch.parseChapterTimeInput("90,25"), 90250, "parse raw sec comma");
        eq(Ch.parseChapterTimeInput("0"), 0, "parse 0");
        eq(Ch.parseChapterTimeInput("01:02:03.999999"), 3723999, "parse long frac");
        eq(Ch.parseChapterTimeInput(null), null, "parse null");
        eq(Ch.parseChapterTimeInput(undefined), null, "parse undef");
        eq(Ch.parseChapterTimeInput(""), null, "parse empty");
        eq(Ch.parseChapterTimeInput("-5"), null, "parse neg");
        eq(Ch.parseChapterTimeInput("01:60:00"), null, "parse bad min");
        eq(Ch.parseChapterTimeInput("01:02:60"), null, "parse bad sec HMS");
        eq(Ch.parseChapterTimeInput("05:60"), null, "parse bad sec MS");
        eq(Ch.parseChapterTimeInput("not-time"), null, "parse garbage");
        eq(Ch.parseChapterTimeInput("9".repeat(400)), null, "parse Infinity sec");

        // 3. clampFrameTimestampMs (EOF 500ms margin, zero, negative, missing duration)
        eq(Ch.clampFrameTimestampMs(120000, 120000), 119500, "clamp EOF");
        eq(Ch.clampFrameTimestampMs(500000, 120000), 119500, "clamp past EOF");
        eq(Ch.clampFrameTimestampMs(-100, 120000), 0, "clamp neg");
        eq(Ch.clampFrameTimestampMs(100, 300), 0, "clamp ultra short <500ms");
        eq(Ch.clampFrameTimestampMs(5000, 0), 5000, "clamp zero duration");
        eq(Ch.clampFrameTimestampMs(NaN, 120000), 0, "clamp NaN");

        // 4. buildPeekFrameTimestamps (-15s | center | +15s clamped)
        eq(Ch.buildPeekFrameTimestamps(60000, 180000), [45000, 60000, 75000], "peek middle");
        eq(Ch.buildPeekFrameTimestamps(5000, 180000), [0, 5000, 20000], "peek near start");
        eq(Ch.buildPeekFrameTimestamps(175000, 180000), [160000, 175000, 179500], "peek near end");

        // 5. buildScanSceneSlots (12 / 24 / 40 presets, 60-slot cap, invalid, ultra-short)
        eq(Ch.buildScanSceneSlots(0, 24), [], "scan 0 dur");
        eq(Ch.buildScanSceneSlots(-100, 24), [], "scan neg dur");
        eq(Ch.buildScanSceneSlots(400, 24), [0], "scan <500ms");
        eq(Ch.buildScanSceneSlots(7200000, 1), [0], "scan 1 slot");
        const slots12 = Ch.buildScanSceneSlots(7200000, 12);
        eq(slots12.length, 12, "scan 12 len");
        eq(slots12[0], 0, "scan 12 start");
        eq(slots12[11], 7199500, "scan 12 end clamped");
        const slotsDefault = Ch.buildScanSceneSlots(7200000);
        eq(slotsDefault.length, 24, "scan default 24");
        const slotsInvalidCount = Ch.buildScanSceneSlots(7200000, -3);
        eq(slotsInvalidCount.length, 24, "scan invalid falls back to 24");
        const slotsCapped = Ch.buildScanSceneSlots(7200000, 999);
        eq(slotsCapped.length, 60, "scan capped at 60");
        const slotsDedupe = Ch.buildScanSceneSlots(503, 24);
        eq(slotsDedupe, [0, 1, 2, 3], "scan dedupes short clips");

        // 6. generateQuickSkipChapters (5m / 10m / 15m, invalid, tiny step, max cap)
        const qs10 = Ch.generateQuickSkipChapters(1500000, 10);
        eq(qs10, [
          {{ start_ms: 0, title: "Chapter 01" }},
          {{ start_ms: 600000, title: "Chapter 02" }},
          {{ start_ms: 1200000, title: "Chapter 03" }},
        ], "quick skip 10m");
        eq(Ch.generateQuickSkipChapters(0, 10), [], "quick skip 0 dur");
        eq(Ch.generateQuickSkipChapters(100000, 0), [], "quick skip 0 interval");
        eq(Ch.generateQuickSkipChapters(100000, 0.0000001), [], "quick skip zero step");

        // 7. ensureZeroStartChapter (TV-player 00:00:00 guard)
        eq(Ch.ensureZeroStartChapter([]), [], "zero start empty");
        eq(
          Ch.ensureZeroStartChapter([{{ start_ms: 60000, title: "Act I" }}]),
          [{{ start_ms: 0, title: "Intro" }}, {{ start_ms: 60000, title: "Act I" }}],
          "zero start prepends Intro"
        );
        eq(
          Ch.ensureZeroStartChapter([{{ start_ms: 0, title: "Cold Open" }}, {{ start_ms: 60000, title: "Act I" }}]),
          [{{ start_ms: 0, title: "Cold Open" }}, {{ start_ms: 60000, title: "Act I" }}],
          "zero start preserves existing 0ms"
        );

        // 8. sortAndNormalizeChapters (sorting, clamping, invalid entries, blank title fill, 500 cap, 200 char cap)
        const messy = [
          null,
          {{ start_ms: -10, title: "Bad" }},
          {{ start_ms: "abc", title: "Bad2" }},
          {{ start_ms: 999999, title: "  " + "X".repeat(250) }},
          {{ start_ms: 1000, title: "" }},
          {{ start_ms: 1000, title: "Second At 1s" }},
        ];
        const norm = Ch.sortAndNormalizeChapters(messy, 50000);
        eq(norm.length, 3, "norm filters invalid");
        eq(norm[0], {{ start_ms: 1000, title: "Chapter 01" }}, "norm fills blank title");
        eq(norm[1], {{ start_ms: 1000, title: "Second At 1s" }}, "norm stable order on tie");
        eq(norm[2].start_ms, 50000, "norm clamps to duration");
        eq(norm[2].title.length, 200, "norm truncates title to 200");
        eq(Ch.sortAndNormalizeChapters(null), [], "norm null");

        // 9. renumberGenericChapters (generic only, preserves custom titles)
        const renum = Ch.renumberGenericChapters([
          {{ start_ms: 0, title: "Intro" }},
          {{ start_ms: 60000, title: "Chapter 99" }},
          {{ start_ms: 120000, title: "chapter 3" }},
          {{ start_ms: 180000, title: "Intermission" }},
          {{ start_ms: 240000, title: "" }},
        ]);
        eq(renum, [
          {{ start_ms: 0, title: "Intro" }},
          {{ start_ms: 60000, title: "Chapter 02" }},
          {{ start_ms: 120000, title: "Chapter 03" }},
          {{ start_ms: 180000, title: "Intermission" }},
          {{ start_ms: 240000, title: "Chapter 05" }},
        ], "renumber generic only");

        // 10. shiftAllChapters (positive, negative clamp to 0, duration clamp, invalid delta, empty)
        eq(Ch.shiftAllChapters([], 5000), [], "shift empty");
        eq(
          Ch.shiftAllChapters([{{ start_ms: 2000, title: "A" }}, {{ start_ms: 9500, title: "B" }}], 1000, 10000),
          [{{ start_ms: 3000, title: "A" }}, {{ start_ms: 10000, title: "B" }}],
          "shift +1s clamped to duration"
        );
        eq(
          Ch.shiftAllChapters([{{ start_ms: 2000, title: "A" }}, {{ start_ms: 8000, title: "B" }}], -5000),
          [{{ start_ms: 0, title: "A" }}, {{ start_ms: 3000, title: "B" }}],
          "shift -5s clamped to 0"
        );
        eq(
          Ch.shiftAllChapters([{{ start_ms: 2000, title: "A" }}], NaN),
          [{{ start_ms: 2000, title: "A" }}],
          "shift NaN delta is 0"
        );

        // 11. buildVideoFingerprint
        eq(Ch.buildVideoFingerprint(null), "0", "fp null");
        eq(
          Ch.buildVideoFingerprint({{ id: 12, size_bytes: 4096, mtime: 1700000000.4, duration_us: 9000000 }}),
          "12-4096-1700000000-9000000",
          "fp full"
        );

        // 12. areChapterListsEqual
        if (!Ch.areChapterListsEqual(
          [{{ start_ms: 1000, title: "B" }}, {{ start_ms: 0, title: "A" }}],
          [{{ start_ms: 0, title: "A" }}, {{ start_ms: 1000, title: "B" }}]
        )) throw new Error("Expected equal after normalization");
        if (Ch.areChapterListsEqual([{{ start_ms: 0, title: "A" }}], [])) throw new Error("Expected unequal lengths");
        if (Ch.areChapterListsEqual([{{ start_ms: 0, title: "A" }}], [{{ start_ms: 0, title: "B" }}])) throw new Error("Expected unequal titles");

        // 13. exportChaptersAsOggText & exportChaptersAsXml (empty, escaping, invalid lang fallback)
        eq(Ch.exportChaptersAsOggText([]), "", "export ogg empty");
        eq(Ch.exportChaptersAsXml([]), "", "export xml empty");
        const sampleExport = [
          {{ start_ms: 0, title: "Tom & Jerry's <Intro>" }},
          {{ start_ms: 90500, title: 'Scene "Two"' }},
        ];
        const oggText = Ch.exportChaptersAsOggText(sampleExport);
        const xmlTextEng = Ch.exportChaptersAsXml(sampleExport, "eng");
        const xmlTextFallback = Ch.exportChaptersAsXml(sampleExport, "invalid-code");
        if (!xmlTextFallback.includes("<ChapterLanguage>und</ChapterLanguage>")) {{
          throw new Error("Expected und fallback in XML");
        }}

        console.log(JSON.stringify({{ oggText, xmlTextEng }}));
        """
        out = _run_node_script(script)
        exported = json.loads(out.strip())

        # Cross-verify JS-exported OGG and XML directly with Python backend parser!
        expected = [
            ChapterEntry(start_ms=0, title="Tom & Jerry's <Intro>"),
            ChapterEntry(start_ms=90_500, title='Scene "Two"'),
        ]
        fmt_ogg, parsed_ogg, warnings_ogg = parse_chapters_text(exported["oggText"])
        assert fmt_ogg == "ogg_text"
        assert parsed_ogg == expected
        assert warnings_ogg == []

        fmt_xml, parsed_xml, warnings_xml = parse_chapters_text(exported["xmlTextEng"])
        assert fmt_xml == "matroska_xml"
        assert parsed_xml == expected
        assert warnings_xml == []


class TestEditorJsChaptersIntegration:
    """Verify `editor.js` chapter state, instant-edit parity, summary, and Pydantic payload parity."""

    def test_editor_state_instant_edit_and_pydantic_payload_contract(self) -> None:
        js_dir = str(STATIC_JS_DIR)
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(js_dir)};

        function loadEditor(includeChaptersJs) {{
          const ctx = {{ window: null, globalThis: null }};
          ctx.window = ctx;
          ctx.globalThis = ctx;
          vm.createContext(ctx);
          const files = includeChaptersJs
            ? ["constants.js", "chapters.js", "editor.js"]
            : ["constants.js", "editor.js"];
          for (const f of files) {{
            vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
          }}
          return ctx;
        }}

        const file = {{
          id: 99,
          filename: "Dune.mkv",
          filepath: "Movies/Dune.mkv",
          folder: "/Movies",
          container_title: "",
          has_global_tags: false,
          has_track_tags: false,
          attachments_count: 0,
          chapters_count: 2,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
            {{ id: 1, type: "audio", codec: "EAC3", audio_channels: 6, audio_index: 0, language: "eng", default_track: true }},
          ],
        }};

        // Verify both WITH chapters.js and WITHOUT chapters.js (graceful standalone fallback)
        const payloads = {{}};
        for (const withChapters of [true, false]) {{
          const ctx = loadEditor(withChapters);
          const Ed = ctx.MKVForgeEditor;

          // Guard null state calls
          if (Ed.resolveEffectiveChaptersAction(null) !== "keep") throw new Error("null action");
          Ed.setStripChapters(null, true);
          Ed.applyChapterEdits(null, []);

          const state = Ed.createEditorState(file, {{
            default_output_mode: "replace",
            default_remove_all_metadata: false,
          }});
          if (state.chaptersAction !== "keep" || state.stripChapters !== false || state.chaptersLoaded !== false) {{
            throw new Error("Bad initial chapter state");
          }}
          if (Ed.hasPendingChanges(state, file)) throw new Error("Untouched state should have no pending changes");
          if (Ed.isInstantHeaderEditEligible(state, file)) throw new Error("Untouched state should not be instant-eligible");

          // Untouched payload keeps chapters_action='keep', chapters=null, strip_chapters=false
          const payloadKeep = Ed.buildJobRequestPayload(state, file);
          if (payloadKeep.config.container.chapters_action !== "keep" || payloadKeep.config.container.chapters !== null) {{
            throw new Error("Untouched payload must keep chapters untouched");
          }}

          // Simulate loading original chapters from GET /chapters
          state.originalChapters = [
            {{ start_ms: 0, title: "Intro" }},
            {{ start_ms: 60000, title: "Act I" }},
          ];
          state.chaptersLoaded = true;

          // Applying identical chapters -> stays 'keep' (no phantom dirty state!)
          Ed.applyChapterEdits(state, [
            {{ start_ms: 60000, title: "Act I" }},
            {{ start_ms: 0, title: "Intro" }},
          ]);
          if (state.chaptersAction !== "keep" || Ed.hasPendingChanges(state, file)) {{
            throw new Error("Identical chapters must stay keep");
          }}

          // Applying modified chapters -> 'replace', instant-edit eligible on MKV replace!
          Ed.applyChapterEdits(state, [
            {{ start_ms: 0, title: "Cold Open" }},
            {{ start_ms: 60000, title: "Act I" }},
            {{ start_ms: 120000, title: "" }},
          ]);
          if (state.chaptersAction !== "replace" || state.stripChapters !== false) {{
            throw new Error("Expected replace action");
          }}
          if (!Ed.hasPendingChanges(state, file)) throw new Error("Expected pending changes for replace");
          if (!Ed.isInstantHeaderEditEligible(state, file)) {{
            throw new Error("MKV replace chapter edit MUST be instant-edit eligible");
          }}
          const sumReplace = Ed.buildPendingSummary(state, file);
          if (!sumReplace.includes("Update 3 Chapters")) {{
            throw new Error("Expected Update 3 Chapters summary, got: " + sumReplace);
          }}
          const payloadReplace = Ed.buildJobRequestPayload(state, file);

          // Single chapter summary singular check
          Ed.applyChapterEdits(state, [{{ start_ms: 0, title: "Only One" }}]);
          const sumOne = Ed.buildPendingSummary(state, file);
          if (!sumOne.includes("Update 1 Chapter")) {{
            throw new Error("Expected singular Update 1 Chapter summary, got: " + sumOne);
          }}

          // Clearing chapters -> 'clear' & stripChapters=true & STILL instant-edit eligible!
          Ed.applyChapterEdits(state, []);
          if (state.chaptersAction !== "clear" || state.stripChapters !== true) {{
            throw new Error("Clearing non-empty chapters must set clear + stripChapters=true");
          }}
          if (!Ed.isInstantHeaderEditEligible(state, file)) {{
            throw new Error("MKV replace chapter clear MUST be instant-edit eligible");
          }}
          const sumClear = Ed.buildPendingSummary(state, file);
          if (!sumClear.includes("Clear Chapters") || sumClear.includes("Strip Metadata")) {{
            throw new Error("Unexpected clear summary: " + sumClear);
          }}
          const payloadClear = Ed.buildJobRequestPayload(state, file);

          // Unchecking stripChapters restores 'keep' and originalChapters
          Ed.setStripChapters(state, false);
          if (state.chaptersAction !== "keep" || state.stripChapters !== false || state.chapters.length !== 2) {{
            throw new Error("Unchecking stripChapters should restore keep + originalChapters");
          }}

          // Checking stripChapters sets 'clear' + empty list
          Ed.setStripChapters(state, true);
          if (state.chaptersAction !== "clear" || state.stripChapters !== true || state.chapters.length !== 0) {{
            throw new Error("Checking stripChapters should set clear");
          }}

          // File that started with 0 chapters: applying [] stays 'keep' (no phantom clear!)
          state.originalChapters = [];
          Ed.applyChapterEdits(state, []);
          if (state.chaptersAction !== "keep" || state.stripChapters !== false) {{
            throw new Error("Empty -> empty chapters must remain keep");
          }}

          // Unloaded originalChapters + setStripChapters(false)
          state.originalChapters = null;
          Ed.setStripChapters(state, true);
          Ed.setStripChapters(state, false);
          if (state.chaptersAction !== "keep" || state.chapters.length !== 0) {{
            throw new Error("Unloaded clear->keep must leave empty chapters");
          }}

          // Keep Both with chapters -> NOT instant-edit eligible
          Ed.applyChapterEdits(state, [{{ start_ms: 0, title: "Start" }}]);
          state.outputMode = "keep_both";
          if (Ed.isInstantHeaderEditEligible(state, file)) {{
            throw new Error("Keep Both must not be instant-eligible");
          }}

          // Standalone fallback edge checks (non-array + unequal length)
          Ed.applyChapterEdits(state, null);
          state.originalChapters = [{{ start_ms: 0, title: "A" }}];
          Ed.applyChapterEdits(state, [{{ start_ms: 0, title: "A" }}, {{ start_ms: 1000, title: "B" }}]);

          if (withChapters) {{
            payloads.keep = payloadKeep;
            payloads.replace = payloadReplace;
            payloads.clear = payloadClear;
          }}
        }}

        console.log(JSON.stringify(payloads));
        """
        out = _run_node_script(script)
        payloads = json.loads(out.strip())

        # Cross-validate all 3 frontend payloads against real backend Pydantic JobCreateRequest!
        req_keep = JobCreateRequest.model_validate(payloads["keep"])
        assert req_keep.config.container.chapters_action == "keep"
        assert req_keep.config.container.strip_chapters is False
        assert req_keep.config.container.chapters is None

        req_replace = JobCreateRequest.model_validate(payloads["replace"])
        assert req_replace.config.container.chapters_action == "replace"
        assert req_replace.config.container.strip_chapters is False
        assert req_replace.config.container.chapters == [
            ChapterEntry(start_ms=0, title="Cold Open"),
            ChapterEntry(start_ms=60_000, title="Act I"),
            ChapterEntry(start_ms=120_000, title="Chapter 03"),
        ]

        req_clear = JobCreateRequest.model_validate(payloads["clear"])
        assert req_clear.config.container.chapters_action == "clear"
        assert req_clear.config.container.strip_chapters is True
        assert req_clear.config.container.chapters is None
