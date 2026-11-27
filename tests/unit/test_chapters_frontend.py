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
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node script exited {proc.returncode}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
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


class TestChaptersControllerLifecycle:
    """Exhaustive Node VM tests for `chapters_controller.js` serial fetch, abort, revoke, and actions."""

    def test_chapters_controller_serial_fetch_abort_revoke_and_all_actions(self) -> None:
        js_dir = str(STATIC_JS_DIR)
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const path = require("path");
        const jsDir = {json.dumps(js_dir)};

        const storage = new Map();
        const revokedUrls = [];
        let createdSeq = 0;
        let inFlight = 0;
        let maxInFlight = 0;
        let abortEventsCount = 0;
        let nextFrameBehavior = "ok";
        let chaptersGetBehavior = "ok";
        let parsePostBehavior = "ok";
        const downloadedFiles = [];

        const ctx = {{
          window: null,
          globalThis: null,
          URLSearchParams,
          encodeURIComponent,
          AbortController,
          Blob,
          setTimeout,
          clearTimeout,
          localStorage: {{
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => storage.set(k, String(v)),
          }},
          URL: {{
            createObjectURL: () => `blob:test-${{++createdSeq}}`,
            revokeObjectURL: (u) => revokedUrls.push(u),
          }},
          document: {{
            createElement: () => ({{ click() {{ downloadedFiles.push({{ href: this.href, download: this.download }}); }} }}),
            body: {{ appendChild() {{}}, removeChild() {{}} }},
          }},
          fetch: async (url, opts = {{}}) => {{
            if (url.endsWith("/chapters")) {{
              if (chaptersGetBehavior === "conflict") {{
                return {{ ok: false, status: 409, json: async () => ({{ detail: "Active replace job" }}) }};
              }}
              if (chaptersGetBehavior === "error") {{
                return {{ ok: false, status: 500, json: async () => ({{ detail: "Server boom" }}) }};
              }}
              return {{
                ok: true,
                status: 200,
                json: async () => ({{
                  chapters: [
                    {{ start_ms: 0, title: "Intro" }},
                    {{ start_ms: 60000, title: "Chapter 9" }},
                  ],
                  has_complex_chapters: true,
                }}),
              }};
            }}
            if (url.endsWith("/chapters/parse")) {{
              if (parsePostBehavior === "empty") {{
                return {{ ok: true, status: 200, json: async () => ({{ detected_format: "youtube_text", chapters: [], warnings: ["No entries"] }}) }};
              }}
              if (parsePostBehavior === "error") {{
                return {{ ok: false, status: 400, json: async () => ({{ detail: "Bad chapter payload" }}) }};
              }}
              return {{
                ok: true,
                status: 200,
                json: async () => ({{
                  detected_format: "youtube_text",
                  chapters: [{{ start_ms: 30000, title: "Parsed Scene" }}],
                  warnings: [],
                }}),
              }};
            }}
            // Frame fetch: enforce strict serial concurrency (max 1 non-aborted in-flight request)
            let released = false;
            const release = () => {{
              if (!released) {{
                released = true;
                inFlight -= 1;
              }}
            }};
            inFlight += 1;
            if (inFlight > maxInFlight) maxInFlight = inFlight;
            try {{
              await new Promise((resolve, reject) => {{
                if (opts.signal && opts.signal.aborted) {{
                  release();
                  const err = new Error("Aborted");
                  err.name = "AbortError";
                  return reject(err);
                }}
                const onAbort = () => {{
                  clearTimeout(timer);
                  release();
                  abortEventsCount += 1;
                  const err = new Error("Aborted");
                  err.name = "AbortError";
                  reject(err);
                }};
                const timer = setTimeout(() => {{
                  if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
                  resolve();
                }}, 2);
                if (opts.signal) opts.signal.addEventListener("abort", onAbort, {{ once: true }});
              }});
              if (nextFrameBehavior === "conflict") {{
                return {{ ok: false, status: 409, json: async () => ({{ detail: "Busy editing file" }}) }};
              }}
              if (nextFrameBehavior === "conflict_bad_json") {{
                return {{ ok: false, status: 409, json: async () => {{ throw new Error("bad json"); }} }};
              }}
              if (nextFrameBehavior === "204") {{
                return {{ ok: true, status: 204 }};
              }}
              if (nextFrameBehavior === "neterr") {{
                throw new Error("Network offline");
              }}
              return {{ ok: true, status: 200, blob: async () => new Blob(["jpg"]) }};
            }} finally {{
              release();
            }}
          }},
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "api.js", "chapters.js", "editor.js", "chapters_controller.js"]) {{
          vm.runInContext(fs.readFileSync(path.join(jsDir, f), "utf8"), ctx, {{ filename: f }});
        }}

        const Ed = ctx.MKVForgeEditor;
        const ChCtrl = ctx.MKVForgeChaptersController;
        const file = {{
          id: 55,
          filename: "Inception.mkv",
          filepath: "Movies/Inception.mkv",
          folder: "/Movies",
          size_bytes: 5000000,
          mtime: 1700000000,
          duration_us: 120000000, // 120s
          chapters_count: 2,
          tracks: [{{ id: 0, type: "video", codec: "HEVC", default_track: true }}],
        }};

        function makeApp() {{
          return {{
            editorFile: file,
            editor: Ed.createEditorState(file, {{ default_output_mode: "replace", default_remove_all_metadata: false }}),
            userSettings: {{ preferred_language: "eng" }},
            chaptersModal: null,
            ...ChCtrl.createMethods(),
          }};
        }}

        (async () => {{
          const app = makeApp();
          // 1. Open modal -> loads chapters, sets hasComplexChapters, fetches ZERO frames
          await app.openChaptersModal();
          if (createdSeq !== 0) throw new Error("Must fetch ZERO frames on modal open");
          if (!app.chaptersModal.hasComplexChapters) throw new Error("Expected hasComplexChapters=true");
          if (app.chaptersModal.chapters.length !== 2) throw new Error("Expected 2 loaded chapters");

          // 2. Scrubber tap #1 AND immediate tap #2 under Alpine-like reactive Proxy!
          const proxiedApp = new Proxy(app, {{
            set(target, prop, val) {{
              if (val && typeof val === "object") {{
                target[prop] = new Proxy(val, {{}});
              }} else {{
                target[prop] = val;
              }}
              return true;
            }},
          }});
          // Rapid double-tap on scrubber: tap #1 MUST abort cleanly without Proxy TypeError and tap #2 MUST load all 3 frames!
          proxiedApp.onChapterScrubberClick({{
            clientX: 25,
            currentTarget: {{ getBoundingClientRect: () => ({{ left: 0, width: 100 }}) }},
          }});
          await new Promise((r) => setTimeout(r, 3));
          proxiedApp.onChapterScrubberClick({{
            clientX: 50,
            currentTarget: {{ getBoundingClientRect: () => ({{ left: 0, width: 100 }}) }},
          }});
          await new Promise((r) => setTimeout(r, 40));
          if (abortEventsCount < 1) throw new Error("Expected tap #1 AbortSignal to fire cleanly under Alpine Proxy");
          if (maxInFlight !== 1) throw new Error("Frame fetches MUST be strictly serial (maxInFlight=" + maxInFlight + ")");
          if (
            !app.chaptersModal.draftPeek ||
            !app.chaptersModal.draftPeek.frames[0].url ||
            !app.chaptersModal.draftPeek.frames[1].url ||
            !app.chaptersModal.draftPeek.frames[2].url
          ) {{
            throw new Error("Second tap under Alpine Proxy MUST load all 3 Draft Peek frames!");
          }}

          // 3. Nudge Draft Peek + debounced accurate refresh + quick title + commit (stays pinned, zero layout jump!)
          app.nudgeChapterDraftPeek(5);
          app.setDraftPeekQuickTitle("Dream Level 1");
          await new Promise((r) => setTimeout(r, 220));
          app.commitChapterDraftPeek();
          if (app.chaptersModal.chapters.length !== 3) throw new Error("Expected 3 chapters after adding Draft Peek");
          if (!app.chaptersModal.draftPeek || app.chaptersModal.draftPeek.added !== true) {{
            throw new Error("Draft Peek must stay pinned with added=true to prevent layout shift");
          }}
          // Duplicate click while added=true is ignored; nudging re-enables adding
          app.commitChapterDraftPeek();
          if (app.chaptersModal.chapters.length !== 3) throw new Error("Duplicate commit while added=true should be no-op");
          app.nudgeChapterDraftPeek(5);
          if (app.chaptersModal.draftPeek.added !== false) throw new Error("Nudging must clear added=true");
          app.closeChapterDraftPeek();
          if (app.chaptersModal.draftPeek !== null) throw new Error("Explicit close should dismiss Draft Peek");

          // 4. Undo restores 2 chapters
          app.undoChapterEdit();
          if (app.chaptersModal.chapters.length !== 2) throw new Error("Undo failed");

          // 5. Inline vertical-rail preview ([👁]) + nudge + quick title + delete
          const firstKey = app.chaptersModal.chapters[0]._key;
          await app.toggleChapterRowPreview(firstKey);
          if (app.chaptersModal.expandedChapterKey !== firstKey || !app.chaptersModal.expandedFrameUrl) {{
            throw new Error("Expected expanded accurate frame");
          }}
          app.nudgeChapterRow(firstKey, 1);
          await new Promise((r) => setTimeout(r, 220));
          app.setChapterRowQuickTitle(firstKey, "Opening");
          await app.toggleChapterRowPreview(firstKey); // collapse
          if (app.chaptersModal.expandedChapterKey !== null) throw new Error("Expected collapsed");

          // 6. Opt-in Show Previews serial stream -> Hide Previews revokes all thumbs
          await app.toggleChapterRailPreviews();
          if (!app.chaptersModal.previewsActive || app.chaptersModal.previewsLoadedCount !== 2) {{
            throw new Error("Expected 2 rail previews loaded");
          }}
          await app.toggleChapterRailPreviews(); // hide
          if (app.chaptersModal.previewsActive) throw new Error("Expected previews hidden");

          // 7. Scan Scenes 1-level grid (12 / 24 / 40) + localStorage persistence + multi-select + add
          storage.set("mkvforge.scene_scan_density", "12");
          app.toggleChapterDrawer("scan_scenes");
          await new Promise((r) => setTimeout(r, 120));
          app.selectScanScenesDensity(12);
          await new Promise((r) => setTimeout(r, 120));
          if (storage.get("mkvforge.scene_scan_density") !== "12") throw new Error("Density not persisted");
          if (app.chaptersModal.scanTiles.length !== 12) throw new Error("Expected 12 scan tiles");
          app.toggleScanTileSelection(app.chaptersModal.scanTiles[1].tsMs);
          app.addSelectedScanTiles();
          if (app.chaptersModal.drawer !== "main") throw new Error("Expected return to main drawer");

          // 8. Quick Skip, Renumber, Shift All, Export .txt & .xml, Paste/Upload, Reset, Clear
          app.applyQuickSkipInterval(1);
          if (app.chaptersModal.chapters.length !== 2) throw new Error("Expected 2 quick-skip chapters for 120s/1m");
          app.renumberChapters();
          app.chaptersModal.shiftDraftSec = "5";
          app.applyShiftAllChapters();
          if (app.chaptersModal.chapters[0].start_ms !== 5000) throw new Error("Expected shifted to 5000ms");
          app.exportChaptersFile("txt");
          app.exportChaptersFile("xml");
          if (downloadedFiles.length !== 2) throw new Error("Expected 2 exported downloads");

          app.toggleChapterDrawer("paste");
          await app.onChapterFileSelected({{ target: {{ files: [{{ size: 600000, name: "huge.txt" }}] }} }});
          if (!app.chaptersModal.pasteError) throw new Error("Expected oversized file rejection");
          await app.onChapterFileSelected({{ target: {{ files: [{{ size: 50, name: "ch.txt", text: async () => "00:30 Parsed" }}] }} }});
          await app.parseAndApplyPastedChapters("replace");
          if (app.chaptersModal.chapters.length !== 2) throw new Error("Expected auto-00:00:00 + parsed chapter");

          // 9. 409 Conflict handling on frame fetch
          nextFrameBehavior = "conflict";
          await app.openChapterDraftPeek(10000);
          if (!app.chaptersModal.conflictMessage) throw new Error("Expected 409 conflict banner");
          nextFrameBehavior = "ok";

          // 10. Commit edits -> syncs editor.chaptersAction === 'replace' AND revokes ALL ObjectURLs
          app.commitChaptersModal();
          if (app.chaptersModal !== null) throw new Error("Modal should close on commit");
          if (app.editor.chaptersAction !== "replace") throw new Error("Expected editor chaptersAction=replace");
          if (revokedUrls.length !== createdSeq) {{
            throw new Error(`ObjectURL leak! created=${{createdSeq}} revoked=${{revokedUrls.length}}`);
          }}

          // 11. Re-opening when already loaded skips GET /chapters and loads edited list
          await app.openChaptersModal();
          app.resetChaptersToOriginal();
          app.clearAllChapters();
          app.commitChaptersModal();
          if (app.editor.chaptersAction !== "clear" || !app.editor.stripChapters) {{
            throw new Error("Expected clear + stripChapters=true after Clear All");
          }}

          console.log("CHAPTERS_CONTROLLER_OK");
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        out = _run_node_script(script)
        assert "CHAPTERS_CONTROLLER_OK" in out

    def test_chapters_controller_edge_branches_and_100pct_v8_coverage(self) -> None:
        js_dir = str(STATIC_JS_DIR)
        script = f"""
        const fs = require("fs");
        const path = require("path");
        const vm = require("vm");
        const inspector = require("inspector");

        const session = new inspector.Session();
        session.connect();
        const post = (m, p = {{}}) =>
          new Promise((res, rej) => session.post(m, p, (e, r) => (e ? rej(e) : res(r))));

        (async () => {{
          await post("Profiler.enable");
          await post("Profiler.startPreciseCoverage", {{ callCount: true, detailed: true }});

          const storage = new Map();
          let storageThrows = false;
          let revokeThrows = false;
          let createdSeq = 0;
          let frameBehavior = "ok";
          let getBehavior = "ok";
          let parseBehavior = "ok";
          let abortCallback = null;

          global.window = global;
          global.localStorage = {{
            getItem: (k) => {{
              if (storageThrows) throw new Error("blocked");
              return storage.has(k) ? storage.get(k) : null;
            }},
            setItem: (k, v) => {{
              if (storageThrows) throw new Error("blocked");
              storage.set(k, String(v));
            }},
          }};
          global.URL = {{
            createObjectURL: () => `blob:cov-${{++createdSeq}}`,
            revokeObjectURL: () => {{
              if (revokeThrows) throw new Error("revoke fail");
            }},
          }};
          global.document = {{
            createElement: () => ({{ click() {{}} }}),
            body: {{ appendChild() {{}}, removeChild() {{}} }},
          }};
          global.fetch = async (url, opts = {{}}) => {{
            if (url.endsWith("/chapters")) {{
              if (getBehavior === "conflict") {{
                return {{ ok: false, status: 409, json: async () => ({{ detail: "Busy" }}) }};
              }}
              if (getBehavior === "error") {{
                return {{ ok: false, status: 500, json: async () => ({{ detail: "Err" }}) }};
              }}
              return {{
                ok: true,
                status: 200,
                json: async () => ({{
                  chapters: [
                    {{ start_ms: 0, title: "Intro" }},
                    {{ start_ms: 60000, title: "Chapter 2" }},
                  ],
                  has_complex_chapters: false,
                }}),
              }};
            }}
            if (url.endsWith("/chapters/parse")) {{
              if (parseBehavior === "empty") {{
                return {{ ok: true, status: 200, json: async () => ({{ chapters: [], warnings: [] }}) }};
              }}
              if (parseBehavior === "error") {{
                return {{ ok: false, status: 400, json: async () => ({{ detail: "Bad" }}) }};
              }}
              return {{
                ok: true,
                status: 200,
                json: async () => ({{ chapters: [{{ start_ms: 10000, title: "P" }}], warnings: [] }}),
              }};
            }}
            if (frameBehavior === "slow_abort_after_fetch") {{
              await new Promise((r) => setTimeout(r, 1));
              if (abortCallback) abortCallback();
              return {{ ok: true, status: 200, blob: async () => new Blob(["x"]) }};
            }}
            if (frameBehavior === "slow_abort_after_blob") {{
              return {{
                ok: true,
                status: 200,
                blob: async () => {{
                  if (abortCallback) abortCallback();
                  return new Blob(["x"]);
                }},
              }};
            }}
            if (frameBehavior === "delay") {{
              await new Promise((r) => setTimeout(r, 15));
              if (opts.signal && opts.signal.aborted) {{
                const e = new Error("Aborted");
                e.name = "AbortError";
                throw e;
              }}
              return {{ ok: true, status: 200, blob: async () => new Blob(["jpg"]) }};
            }}
            if (frameBehavior === "conflict") {{
              return {{ ok: false, status: 409, json: async () => ({{ detail: "409!" }}) }};
            }}
            if (frameBehavior === "conflict_bad_json") {{
              return {{ ok: false, status: 409, json: async () => {{ throw new Error("bad"); }} }};
            }}
            if (frameBehavior === "204") {{
              return {{ ok: true, status: 204 }};
            }}
            if (frameBehavior === "abort_throw") {{
              const e = new Error("Aborted");
              e.name = "AbortError";
              throw e;
            }}
            if (frameBehavior === "neterr") {{
              throw new Error("offline");
            }}
            return {{ ok: true, status: 200, blob: async () => new Blob(["jpg"]) }};
          }};

          const jsDir = {json.dumps(js_dir)};
          for (const f of ["constants.js", "api.js", "chapters.js", "editor.js", "chapters_controller.js"]) {{
            const abs = path.join(jsDir, f);
            vm.runInThisContext(fs.readFileSync(abs, "utf8"), {{ filename: abs }});
          }}

          const Ed = global.MKVForgeEditor;
          const ChCtrl = global.MKVForgeChaptersController;
          const file = {{
            id: 1,
            filename: "M.mkv",
            filepath: "M.mkv",
            folder: "/",
            duration_us: 120000000,
            chapters_count: 2,
            tracks: [{{ id: 0, type: "video", codec: "HEVC", default_track: true }}],
          }};
          const makeApp = (f = file) => ({{
            editorFile: f,
            editor: f
              ? Ed.createEditorState(f, {{ default_output_mode: "replace", default_remove_all_metadata: false }})
              : null,
            userSettings: {{ preferred_language: "eng" }},
            chaptersModal: null,
            ...ChCtrl.createMethods(),
          }});

          // A. Main flow + Draft Peek debounced accurate refresh + collapse + error frame + mid-load stop
          const app = makeApp();
          storage.set("mkvforge.scene_scan_density", "40");
          await app.openChaptersModal();
          app.onChapterScrubberClick({{
            clientX: 50,
            currentTarget: {{ getBoundingClientRect: () => ({{ left: 0, width: 100 }}) }},
          }});
          await new Promise((r) => setTimeout(r, 20));
          // Nudge and WAIT full debounce so _refreshDraftPeekCenterAccurate executes and revokes old center URL
          app.nudgeChapterDraftPeek(5);
          await new Promise((r) => setTimeout(r, 210));
          // Stale runId guard before fetch & stale close mid-fetch in _refreshDraftPeekCenterAccurate
          await app._refreshDraftPeekCenterAccurate(-1);
          frameBehavior = "delay";
          app.nudgeChapterDraftPeek(1);
          await new Promise((r) => setTimeout(r, 195));
          app.openChapterDraftPeek(60000); // bumps peekRunSeq while accurate fetch is in-flight
          await new Promise((r) => setTimeout(r, 30));
          frameBehavior = "ok";
          await app.openChapterDraftPeek(60000);
          app.chaptersModal.draftPeek.timeText = "00:01:05";
          app.commitChapterDraftPeekTime();
          app.chaptersModal.draftPeek.timeText = "bad";
          app.commitChapterDraftPeekTime();
          app.selectDraftPeekFrame(0);
          await new Promise((r) => setTimeout(r, 20));
          app.setDraftPeekQuickTitle("Song");
          app.commitChapterDraftPeek();
          app.closeChapterDraftPeek();
          app.undoChapterEdit();
          app.addManualChapterRow();

          // Row preview expand, collapse, 204 error frame, nudge + debounced refresh (success & error)
          const k0 = app.chaptersModal.chapters[0]._key;
          await app.toggleChapterRowPreview(k0);
          await app.toggleChapterRowPreview(k0); // collapse branch
          frameBehavior = "204";
          await app.toggleChapterRowPreview(k0); // expandedFrameError on open
          app.nudgeChapterRow(k0, 2);
          await new Promise((r) => setTimeout(r, 210)); // expandedFrameError on debounced refresh
          frameBehavior = "ok";
          app.nudgeChapterRow(k0, 1);
          await new Promise((r) => setTimeout(r, 210)); // success on debounced refresh (revokes previous)
          app.nudgeChapterRow(k0, 1);
          await new Promise((r) => setTimeout(r, 210)); // revokes previous expandedFrameUrl
          // Close modal mid-fetch to exercise stale-modal guards
          frameBehavior = "delay";
          const k1 = app.chaptersModal.chapters[1]._key;
          const pRow = app.toggleChapterRowPreview(k1);
          app.chaptersModal.expandedChapterKey = "other";
          await pRow;
          app.chaptersModal.expandedChapterKey = k1;
          const pRef = app._refreshExpandedChapterFrame(k1);
          app.chaptersModal.expandedChapterKey = "other";
          await pRef;
          frameBehavior = "ok";

          // Row commit time (valid + invalid) + quick title + rail previews + stop mid-load + conflict stop
          app.chaptersModal.chapters[0].timeText = "00:00:10";
          app.commitChapterRowTime(k0);
          app.chaptersModal.chapters[0].timeText = "bad";
          app.commitChapterRowTime(k0);
          app.setChapterRowQuickTitle(k0, "Intro");

          // Start rail previews then stop mid-load
          frameBehavior = "delay";
          const pPrev = app.toggleChapterRailPreviews();
          await new Promise((r) => setTimeout(r, 3));
          await app.toggleChapterRailPreviews(); // stop while loading
          await pPrev;
          // Conflict during rail previews
          app._clearChapterRailThumbUrls();
          frameBehavior = "conflict";
          await app.toggleChapterRailPreviews();
          // Full rail previews + delete currently expanded row (exercises lines 707-708!) + hide
          app._clearChapterRailThumbUrls();
          frameBehavior = "ok";
          await app.toggleChapterRailPreviews();
          await app.toggleChapterRowPreview(k0);
          app.deleteChapterRow(k0);
          await app.toggleChapterRailPreviews(); // hide

          // Scan Scenes: start, switch drawer mid-scan (triggers stopScanScenesGrid), conflict mid-scan, full scan, density switch (revokes tile URLs!)
          frameBehavior = "delay";
          app.toggleChapterDrawer("scan_scenes");
          await new Promise((r) => setTimeout(r, 3));
          app.toggleChapterDrawer("main"); // triggers stopScanScenesGrid
          frameBehavior = "conflict";
          app.chaptersModal.scanTiles = [];
          await app.loadScanScenesGrid();
          frameBehavior = "ok";
          app.selectScanScenesDensity(12);
          await new Promise((r) => setTimeout(r, 40));
          app.selectScanScenesDensity(24); // revokes loaded 12 tile URLs!
          await new Promise((r) => setTimeout(r, 40));
          app.toggleScanTileSelection(app.chaptersModal.scanTiles[0].tsMs);
          app.toggleScanTileSelection(app.chaptersModal.scanTiles[0].tsMs);
          app.toggleScanTileSelection(app.chaptersModal.scanTiles[1].tsMs);
          app.addSelectedScanTiles();
          app.fineTuneScanTile(1000);
          await new Promise((r) => setTimeout(r, 20));
          // Draft peek conflict + abort mid-loop (both before and during fetch)
          frameBehavior = "conflict";
          await app.openChapterDraftPeek(2000);
          frameBehavior = "slow_abort_after_fetch";
          abortCallback = () => {{
            app._abortChapterFrameRequests();
          }};
          await app.openChapterDraftPeek(2500);
          abortCallback = null;
          frameBehavior = "delay";
          const pPeek = app.openChapterDraftPeek(3000);
          await new Promise((r) => setTimeout(r, 17));
          app._abortChapterFrameRequests();
          await pPeek;
          frameBehavior = "ok";

          // Quick skip, renumber, shift, export (with and without document)
          app.applyQuickSkipInterval(1);
          app.renumberChapters();
          app.chaptersModal.shiftDraftSec = "2";
          app.applyShiftAllChapters();
          app.exportChaptersFile("txt");
          app.exportChaptersFile("xml");
          const savedDoc = global.document;
          global.document = null;
          app.exportChaptersFile("txt"); // triggers _triggerBlobDownload false branch
          global.document = savedDoc;

          // Paste drawer: empty text, oversized, read error, ok, empty parse, parse error, append
          app.toggleChapterDrawer("paste");
          app.chaptersModal.pasteText = "   ";
          await app.parseAndApplyPastedChapters("replace");
          await app.onChapterFileSelected({{ target: {{ files: [{{ size: 999999, name: "big.txt" }}] }} }});
          await app.onChapterFileSelected({{
            target: {{ files: [{{ size: 10, name: "bad.txt", text: async () => {{ throw new Error("read"); }} }}] }},
          }});
          await app.onChapterFileSelected({{
            target: {{ files: [{{ size: 10, name: "ok.txt", text: async () => "00:10 P" }}] }},
          }});
          parseBehavior = "empty";
          await app.parseAndApplyPastedChapters("replace");
          parseBehavior = "error";
          await app.parseAndApplyPastedChapters("replace");
          parseBehavior = "ok";
          await app.parseAndApplyPastedChapters("append");

          // Commit non-empty edited chapters and re-open (exercises cached replace branch!)
          app.commitChaptersModal();
          await app.openChaptersModal();
          app.resetChaptersToOriginal();
          app.clearAllChapters();
          app.commitChaptersModal();

          // Re-open when already loaded as clear (exercises cached clear branch!)
          await app.openChaptersModal();
          app.closeChaptersModal();

          // No-video + abort after fetch/blob + storage/revoke throws + GET conflict/error
          storageThrows = true;
          revokeThrows = true;
          const appNoVid = makeApp({{ ...file, duration_us: 0 }});
          await appNoVid._fetchChapterFrameObjectUrl(0, "fast", null); // !modal branch
          getBehavior = "conflict";
          await appNoVid.openChaptersModal();
          await appNoVid._fetchChapterFrameObjectUrl(0, "fast", null); // !modal.hasVideo branch
          appNoVid.closeChaptersModal();
          getBehavior = "error";
          await appNoVid.openChaptersModal();
          appNoVid.closeChaptersModal();
          getBehavior = "ok";

          const app3 = makeApp();
          await app3.openChaptersModal();
          const c1 = new AbortController();
          abortCallback = () => c1.abort();
          frameBehavior = "slow_abort_after_fetch";
          await app3._fetchChapterFrameObjectUrl(0, "fast", c1.signal);
          const c2 = new AbortController();
          abortCallback = () => c2.abort();
          frameBehavior = "slow_abort_after_blob";
          await app3._fetchChapterFrameObjectUrl(0, "fast", c2.signal);
          abortCallback = null;
          frameBehavior = "conflict";
          await app3._fetchChapterFrameObjectUrl(0, "fast", new AbortController().signal);
          frameBehavior = "conflict_bad_json";
          await app3._fetchChapterFrameObjectUrl(0, "fast", new AbortController().signal);
          frameBehavior = "204";
          await app3._fetchChapterFrameObjectUrl(0, "fast", new AbortController().signal);
          frameBehavior = "abort_throw";
          await app3._fetchChapterFrameObjectUrl(0, "fast", new AbortController().signal);
          frameBehavior = "neterr";
          await app3._fetchChapterFrameObjectUrl(0, "fast", new AbortController().signal);
          // Ensure _refreshExpandedChapterFrame guard on missing row / modal
          app3.chaptersModal.expandedChapterKey = "missing";
          await app3._refreshExpandedChapterFrame("missing");
          app3._refreshExpandedChapterFrame("other");
          app3.closeChaptersModal();

          const {{ result }} = await post("Profiler.takePreciseCoverage");
          const abs = path.resolve(jsDir, "chapters_controller.js");
          const src = fs.readFileSync(abs, "utf8");
          const lines = src.split("\\n");
          const offsets = [];
          let pos = 0;
          for (const l of lines) {{
            offsets.push([pos, pos + l.length]);
            pos += l.length + 1;
          }}
          const cov = result.find(
            (r) => r.url === abs || r.url.endsWith("/app/static/js/chapters_controller.js")
          );
          if (!cov) throw new Error("Missing V8 coverage entry for chapters_controller.js");
          const uncovered = [];
          for (const fn of cov.functions) {{
            for (const r of fn.ranges) {{
              if (r.count === 0) uncovered.push([r.startOffset, r.endOffset]);
            }}
          }}
          let total = 0;
          const missed = [];
          for (let i = 0; i < lines.length; i++) {{
            const t = lines[i].trim();
            if (
              !t ||
              t.startsWith("//") ||
              t.startsWith("/*") ||
              t === "{{" ||
              t === "}}" ||
              t === "}});" ||
              t === "]);"
            ) {{
              continue;
            }}
            total++;
            const [ls, le] = offsets[i];
            if (uncovered.some(([us, ue]) => us <= ls && ue >= le)) {{
              missed.push(`${{i + 1}}: ${{t}}`);
            }}
          }}
          session.disconnect();
          if (missed.length > 0) {{
            throw new Error(
              `chapters_controller.js coverage below 100% (${{total - missed.length}}/${{total}}):\\n` +
                missed.join("\\n")
            );
          }}
          console.log(`CHAPTERS_CTRL_V8_100PCT:${{total}}/${{total}}`);
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        out = _run_node_script(script)
        assert "CHAPTERS_CTRL_V8_100PCT:" in out


