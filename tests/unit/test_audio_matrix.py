"""Exhaustive Node VM unit tests for Phase 14 frontend audio conversion matrix & modal state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JS_DIR = PROJECT_ROOT / "app" / "static" / "js"


def _run_node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


class TestPhase14FrontendAudioMatrix:
    """100% positive, negative, and edge-case tests for constants.js + editor.js Phase 14 helpers."""

    def test_channel_labels_lossless_and_object_audio_detection(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: {{}}, globalThis: {{}} }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "editor.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Ed = ctx.MKVForgeEditor;

        console.log(JSON.stringify({{
          labels: {{
            ch8: Ed.formatChannelLabel(8),
            ch7: Ed.formatChannelLabel(7),
            ch6: Ed.formatChannelLabel(6),
            ch5: Ed.formatChannelLabel(5),
            ch4: Ed.formatChannelLabel(4),
            ch3: Ed.formatChannelLabel(3),
            ch2: Ed.formatChannelLabel(2),
            ch1: Ed.formatChannelLabel(1),
            ch0: Ed.formatChannelLabel(0),
            chNull: Ed.formatChannelLabel(null),
            chNeg: Ed.formatChannelLabel(-3),
          }},
          lossless: {{
            truehd: Ed.isLosslessAudioTrack({{ type: "audio", codec: "TrueHD", track_name: "English TrueHD 7.1" }}),
            dtsHdMa: Ed.isLosslessAudioTrack({{ type: "audio", codec: "DTS-HD Master Audio" }}),
            dtsHdHra: Ed.isLosslessAudioTrack({{ type: "audio", codec: "DTS-HD High Resolution Audio" }}),
            flac: Ed.isLosslessAudioTrack({{ type: "audio", codec: "FLAC" }}),
            pcm: Ed.isLosslessAudioTrack({{ type: "audio", codec: "PCM" }}),
            alac: Ed.isLosslessAudioTrack({{ type: "audio", codec: "ALAC" }}),
            aac: Ed.isLosslessAudioTrack({{ type: "audio", codec: "AAC" }}),
            eac3: Ed.isLosslessAudioTrack({{ type: "audio", codec: "E-AC-3" }}),
            videoTrack: Ed.isLosslessAudioTrack({{ type: "video", codec: "TrueHD" }}),
            nullTrack: Ed.isLosslessAudioTrack(null),
          }},
          objectAudio: {{
            atmosTitle: Ed.hasObjectAudioMetadata({{ type: "audio", codec: "TrueHD", track_name: "TrueHD Atmos 7.1" }}),
            jocCodec: Ed.hasObjectAudioMetadata({{ type: "audio", codec: "E-AC-3 JOC", track_name: "English" }}),
            dtsx: Ed.hasObjectAudioMetadata({{ type: "audio", codec: "DTS-HD Master Audio", track_name: "DTS:X 7.1" }}),
            regular: Ed.hasObjectAudioMetadata({{ type: "audio", codec: "TrueHD", track_name: "English 7.1" }}),
            nullTrack: Ed.hasObjectAudioMetadata(null),
          }},
        }}));
        """
        out = _run_node_json(script)
        assert out["labels"] == {
            "ch8": "7.1",
            "ch7": "6.1",
            "ch6": "5.1",
            "ch5": "5.0",
            "ch4": "4.0",
            "ch3": "3.0",
            "ch2": "2.0",
            "ch1": "1.0",
            "ch0": "5.1",
            "chNull": "5.1",
            "chNeg": "5.1",
        }
        assert out["lossless"] == {
            "truehd": True,
            "dtsHdMa": True,
            "dtsHdHra": False,
            "flac": True,
            "pcm": True,
            "alac": True,
            "aac": False,
            "eac3": False,
            "videoTrack": False,
            "nullTrack": False,
        }
        assert out["objectAudio"] == {
            "atmosTitle": True,
            "jocCodec": True,
            "dtsx": True,
            "regular": False,
            "nullTrack": False,
        }

    def test_recommendations_codecs_channels_bitrates_and_notices_matrix(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: {{}}, globalThis: {{}} }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "editor.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Ed = ctx.MKVForgeEditor;

        function makeState(audioTrack) {{
          return Ed.createEditorState({{
            id: 1,
            filepath: "movies/test.mkv",
            filename: "test.mkv",
            container_title: "",
            has_global_tags: false,
            tracks: [
              {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
              audioTrack,
            ],
            attachments: [],
          }});
        }}

        const aac51 = {{ id: 1, type: "audio", codec: "AAC", audio_channels: 6, language: "eng", default_track: true, audio_index: 0 }};
        const aac71 = {{ id: 1, type: "audio", codec: "AAC", audio_channels: 8, language: "eng", default_track: true, audio_index: 0 }};
        const truehdAtmos71 = {{ id: 1, type: "audio", codec: "TrueHD", audio_channels: 8, language: "eng", track_name: "TrueHD Atmos 7.1", default_track: true, audio_index: 0 }};
        const eac351 = {{ id: 1, type: "audio", codec: "E-AC-3", audio_channels: 6, language: "eng", default_track: true, audio_index: 0 }};
        const ac351 = {{ id: 1, type: "audio", codec: "AC-3", audio_channels: 6, language: "eng", default_track: true, audio_index: 0 }};
        const stereo20 = {{ id: 1, type: "audio", codec: "AAC", audio_channels: 2, language: "eng", default_track: true, audio_index: 0 }};

        const recs = {{
          aac51: Ed.getRecommendedAddAudioOptions(makeState(aac51)).map((o) => [o.codec, o.targetChannels, o.badge]),
          aac71: Ed.getRecommendedAddAudioOptions(makeState(aac71)).map((o) => [o.codec, o.targetChannels, o.badge]),
          truehd71: Ed.getRecommendedAddAudioOptions(makeState(truehdAtmos71)).map((o) => [o.codec, o.targetChannels, o.badge]),
          eac351: Ed.getRecommendedAddAudioOptions(makeState(eac351)).map((o) => [o.codec, o.targetChannels, o.badge]),
          ac351: Ed.getRecommendedAddAudioOptions(makeState(ac351)).map((o) => [o.codec, o.targetChannels, o.badge]),
          stereo20: Ed.getRecommendedAddAudioOptions(makeState(stereo20)).map((o) => [o.codec, o.targetChannels, o.badge]),
        }};

        // Available target codecs: FLAC must ONLY appear for lossless sources
        const codecsForAac = Ed.getAvailableTargetCodecs(aac51).map((c) => c.codec);
        const codecsForTruehd = Ed.getAvailableTargetCodecs(truehdAtmos71).map((c) => c.codec);

        // Channel options: Match + downmixes, capped for Dolby, never upmixes
        const chOptsTruehdOpus = Ed.getAvailableChannelOptions(truehdAtmos71, "opus").map((c) => [c.value, c.resolvedChannels, c.label]);
        const chOptsTruehdEac3 = Ed.getAvailableChannelOptions(truehdAtmos71, "eac3").map((c) => [c.value, c.resolvedChannels, c.label]);
        const chOptsStereoAac = Ed.getAvailableChannelOptions(stereo20, "aac").map((c) => [c.value, c.resolvedChannels, c.label]);

        // Bitrate ladders
        const brEac351 = Ed.getBitrateConfigFor("eac3", 6);
        const brAc351 = Ed.getBitrateConfigFor("ac3", 6);
        const brOpus71 = Ed.getBitrateConfigFor("opus", 8);
        const brFlac71 = Ed.getBitrateConfigFor("flac", 8);

        // Blocking matrix
        const blocks = {{
          sameCodecSameCh: Ed.isConversionCombinationBlocked(aac51, "aac", null),
          sameCodecDownmix: Ed.isConversionCombinationBlocked(aac51, "aac", 2),
          ac3ToEac3SameCh: Ed.isConversionCombinationBlocked(ac351, "eac3", null),
          ac3ToEac3Downmix: Ed.isConversionCombinationBlocked(ac351, "eac3", 2),
          lossyToFlac: Ed.isConversionCombinationBlocked(aac51, "flac", null),
          losslessToFlac: Ed.isConversionCombinationBlocked(truehdAtmos71, "flac", null),
          aac71ToOpus71: Ed.isConversionCombinationBlocked(aac71, "opus", null),
          nullTrack: Ed.isConversionCombinationBlocked(null, "eac3", null),
        }};

        // Audiophile contextual notices
        const notices = {{
          atmosToEac3: Ed.getConversionNotices(truehdAtmos71, "eac3", null),
          atmosToFlac: Ed.getConversionNotices(truehdAtmos71, "flac", null),
          aac71ToOpus: Ed.getConversionNotices(aac71, "opus", null),
          aac71ToAac20: Ed.getConversionNotices(aac71, "aac", 2),
          aac51ToEac351: Ed.getConversionNotices(aac51, "eac3", null),
        }};

        console.log(JSON.stringify({{
          recs,
          codecsForAac,
          codecsForTruehd,
          chOptsTruehdOpus,
          chOptsTruehdEac3,
          chOptsStereoAac,
          brEac351,
          brAc351,
          brOpus71,
          brFlac71,
          blocks,
          notices,
        }}));
        """
        out = _run_node_json(script)

        # Source-aware recommendations
        assert [r[0] for r in out["recs"]["aac51"]] == ["eac3", "ac3"]
        assert [r[0] for r in out["recs"]["aac71"]] == ["eac3", "opus", "ac3"]
        assert [r[0] for r in out["recs"]["truehd71"]] == ["flac", "eac3", "opus"]
        assert out["recs"]["truehd71"][0][2] == "Lossless"
        assert [r[0] for r in out["recs"]["eac351"]] == ["ac3", "aac"]
        assert out["recs"]["eac351"][1][1] == 2  # Stereo downmix suggestion
        assert [r[0] for r in out["recs"]["ac351"]] == ["aac", "opus"]
        assert [r[0] for r in out["recs"]["stereo20"]] == ["eac3", "ac3"]

        # FLAC visibility
        assert "flac" not in out["codecsForAac"]
        assert out["codecsForTruehd"] == ["eac3", "ac3", "aac", "opus", "flac"]

        # Channel options (Match + canonical downmix steps 5.1/2.0/1.0, Dolby capped at 5.1, no upmix)
        assert [c[1] for c in out["chOptsTruehdOpus"]] == [8, 6, 2, 1]
        assert out["chOptsTruehdOpus"][0][0] is None  # Match Source value is null
        assert [c[1] for c in out["chOptsTruehdEac3"]] == [6, 2, 1]  # Capped at 5.1!
        assert out["chOptsTruehdEac3"][0][2] == "5.1 (Max)"
        assert [c[1] for c in out["chOptsStereoAac"]] == [2, 1]  # Never offers 5.1/7.1

        # Bitrate ladders
        assert "768k" in out["brEac351"]["options"]
        assert out["brEac351"]["defaultBitrate"] == "640k"
        assert "768k" not in out["brAc351"]["options"]
        assert out["brAc351"]["defaultBitrate"] == "448k"
        assert out["brOpus71"]["defaultBitrate"] == "448k"
        assert out["brFlac71"] == {"options": [], "defaultBitrate": None}

        # Blocking logic
        assert out["blocks"]["sameCodecSameCh"]["blocked"] is True
        assert out["blocks"]["sameCodecDownmix"]["blocked"] is False
        assert out["blocks"]["ac3ToEac3SameCh"]["blocked"] is True
        assert out["blocks"]["ac3ToEac3Downmix"]["blocked"] is False
        assert out["blocks"]["lossyToFlac"]["blocked"] is True
        assert out["blocks"]["losslessToFlac"]["blocked"] is False
        assert out["blocks"]["aac71ToOpus71"]["blocked"] is False
        assert out["blocks"]["nullTrack"]["blocked"] is True

        # Dynamic audiophile notices
        atmos_eac3_msgs = " ".join(n["message"] for n in out["notices"]["atmosToEac3"])
        assert "Atmos" in atmos_eac3_msgs
        assert "5.1" in atmos_eac3_msgs
        atmos_flac_msgs = " ".join(n["message"] for n in out["notices"]["atmosToFlac"])
        assert "Atmos" in atmos_flac_msgs
        assert "Lossless" in atmos_flac_msgs
        opus_71_msgs = " ".join(n["message"] for n in out["notices"]["aac71ToOpus"])
        assert "LPCM" in opus_71_msgs
        downmix_msgs = " ".join(n["message"] for n in out["notices"]["aac71ToAac20"])
        assert "Downmixing" in downmix_msgs
        assert out["notices"]["aac51ToEac351"] == []  # Optimal path = zero noise

    def test_app_modal_progressive_disclosure_and_job_payload_serialization(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          URLSearchParams,
          setTimeout,
          clearTimeout,
          document: {{ addEventListener: () => {{}}, readyState: "complete" }},
          localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          location: {{ hash: "#/files/1" }},
          history: {{ replaceState: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280,
          innerHeight: 800,
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of [
          "constants.js", "formatters.js", "api.js", "folder_tree.js",
          "editor.js", "pull_refresh.js", "toast_stack.js", "library_return.js", "app.js"
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const app = ctx.mkvForgeApp();
        app.editorFile = {{
          id: 7,
          filepath: "movies/Atmos.mkv",
          filename: "Atmos.mkv",
          container_title: "",
          has_global_tags: false,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
            {{ id: 1, type: "audio", codec: "TrueHD", audio_channels: 8, language: "eng", track_name: "TrueHD Atmos 7.1", default_track: true, audio_index: 0 }},
            {{ id: 2, type: "audio", codec: "AAC", audio_channels: 6, language: "spa", track_name: "Spanish AAC 5.1", default_track: false, audio_index: 1 }},
          ],
          attachments: [],
        }};
        app.editor = ctx.MKVForgeEditor.createEditorState(app.editorFile);

        // 1. Quick recommendation opens with Advanced COLLAPSED
        const firstRec = app.editorRecommendedAudioOptions[0];
        app.openAddAudioChoice(firstRec);
        const quickOpenState = {{
          advancedOpen: app.addAudioDraft.advancedOpen,
          codec: app.addAudioDraft.targetCodec,
          summary: app.editorDraftAdvancedSummary,
        }};

        // 2. Custom conversion opens with Advanced EXPANDED
        app.openCustomAddAudio();
        const customOpenState = {{
          advancedOpen: app.addAudioDraft.advancedOpen,
        }};

        // 3. Switch to FLAC on TrueHD 7.1 -> bitrate becomes null, summary shows Lossless
        app.onDraftTargetCodecChange("flac");
        const flacState = {{
          codec: app.addAudioDraft.targetCodec,
          bitrate: app.addAudioDraft.bitrate,
          bitrateConfig: app.editorDraftBitrateConfig,
          title: app.addAudioDraft.trackTitle,
          summary: app.editorDraftAdvancedSummary,
          resolvedCh: app.editorDraftResolvedChannels,
        }};

        // 4. Confirm FLAC 7.1 -> check job payload & virtual display track row
        app.confirmAddAudio();
        const flacPayload = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile);
        const flacVirtualRow = ctx.MKVForgeEditor.buildDisplayTracks(app.editor).find((r) => r.isVirtual);

        // 5. Re-open edit modal & switch base track to lossy Spanish AAC 5.1 -> auto-escapes FLAC!
        app.editAddedAudio();
        app.addAudioDraft.sourceTrackId = 2;
        app.onDraftBaseAudioChange();
        const switchedToLossyState = {{
          codec: app.addAudioDraft.targetCodec,
          bitrate: app.addAudioDraft.bitrate,
          language: app.addAudioDraft.language,
          blocked: app.editorDraftBlockStatus.blocked,
        }};

        // 6. Switch to Opus and explicitly downmix to 2.0 stereo -> confirm and check payload
        app.onDraftTargetCodecChange("opus");
        app.onDraftChannelsChange(2);
        const opusDownmixDraft = {{
          codec: app.addAudioDraft.targetCodec,
          targetChannels: app.addAudioDraft.targetChannels,
          bitrate: app.addAudioDraft.bitrate,
          title: app.addAudioDraft.trackTitle,
          summary: app.editorDraftAdvancedSummary,
        }};
        app.confirmAddAudio();
        const opusPayload = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile);

        console.log(JSON.stringify({{
          quickOpenState,
          customOpenState,
          flacState,
          flacPayload: flacPayload.config.audio_conversion,
          flacVirtualRow: {{
            codecBadge: flacVirtualRow.codecBadge,
            bitrate: flacVirtualRow.bitrate,
            language: flacVirtualRow.language,
          }},
          switchedToLossyState,
          opusDownmixDraft,
          opusPayload: opusPayload.config.audio_conversion,
        }}));
        """
        out = _run_node_json(script)

        assert out["quickOpenState"]["advancedOpen"] is False
        assert out["quickOpenState"]["codec"] == "flac"
        assert "FLAC" in out["quickOpenState"]["summary"]
        assert "7.1" in out["quickOpenState"]["summary"]

        assert out["customOpenState"]["advancedOpen"] is True

        assert out["flacState"]["codec"] == "flac"
        assert out["flacState"]["bitrate"] is None
        assert out["flacState"]["bitrateConfig"] == {"options": [], "defaultBitrate": None}
        assert "FLAC 7.1" in out["flacState"]["title"]
        assert "Lossless" in out["flacState"]["summary"]
        assert out["flacState"]["resolvedCh"] == 8

        assert out["flacPayload"]["target_codec"] == "flac"
        # Match Source MUST emit target_channels: null so backend resolves against ffprobe's authoritative channels
        assert out["flacPayload"]["target_channels"] is None
        assert out["flacPayload"]["bitrate"] is None
        assert out["flacVirtualRow"] == {
            "codecBadge": "FLAC 7.1",
            "bitrate": "Lossless",
            "language": "eng",
        }

        assert out["switchedToLossyState"]["codec"] != "flac"
        assert out["switchedToLossyState"]["bitrate"] is not None
        assert out["switchedToLossyState"]["language"] == "spa"
        assert out["switchedToLossyState"]["blocked"] is False

        assert out["opusDownmixDraft"]["codec"] == "opus"
        assert out["opusDownmixDraft"]["targetChannels"] == 2
        assert out["opusDownmixDraft"]["bitrate"] == "192k"
        assert "Opus 2.0" in out["opusDownmixDraft"]["title"]
        assert "2.0" in out["opusDownmixDraft"]["summary"]
        assert out["opusPayload"]["target_codec"] == "opus"
        assert out["opusPayload"]["target_channels"] == 2
        assert out["opusPayload"]["bitrate"] == "192k"

    def test_ui_translation_invariants_match_source_null_settings_sync_and_lossless_escape(
        self,
    ) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          URLSearchParams,
          setTimeout,
          clearTimeout,
          document: {{ addEventListener: () => {{}}, readyState: "complete" }},
          localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          location: {{ hash: "#/files/1" }},
          history: {{ replaceState: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280,
          innerHeight: 800,
        }};
        ctx.window = ctx;
        ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of [
          "constants.js", "formatters.js", "api.js", "folder_tree.js",
          "editor.js", "pull_refresh.js", "toast_stack.js", "library_return.js", "app.js"
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const app = ctx.mkvForgeApp();
        // User saved 448k as preferred Dolby 5.1 bitrate in Settings
        app.userSettings.default_dolby_bitrate = "448k";

        // Case A: Dropdown bitrate badge MUST match modal draft bitrate & payload bitrate
        app.editorFile = {{
          id: 10,
          filepath: "movies/MissingCh.mkv",
          filename: "MissingCh.mkv",
          container_title: "",
          has_global_tags: false,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
            {{ id: 1, type: "audio", codec: "AAC", audio_channels: null, language: "eng", default_track: true, audio_index: 0 }},
            {{ id: 2, type: "audio", codec: "TrueHD", audio_channels: 8, language: "eng", default_track: false, audio_index: 1 }},
            {{ id: 3, type: "audio", codec: "E-AC-3", audio_channels: 6, language: "fra", default_track: false, audio_index: 2 }},
          ],
          attachments: [],
        }};
        app.editor = ctx.MKVForgeEditor.createEditorState(app.editorFile);

        const rec = app.editorRecommendedAudioOptions.find((o) => o.codec === "eac3");
        app.openAddAudioChoice(rec);
        const dropdownBitrate = rec.defaultBitrate;
        const modalBitrate = app.addAudioDraft.bitrate;
        // Switch base track to Track 1 (audio_channels === null) and confirm Match Source
        app.addAudioDraft.sourceTrackId = 1;
        app.onDraftBaseAudioChange();
        app.confirmAddAudio();
        const nullChannelMatchPayload = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile).config.audio_conversion;

        // Case B: Switching from TrueHD 7.1 (FLAC) to lossy E-AC-3 5.1 MUST escape FLAC to a NON-blocked codec
        app.openCustomAddAudio();
        app.addAudioDraft.sourceTrackId = 2;
        app.onDraftBaseAudioChange();
        app.onDraftTargetCodecChange("flac");
        app.addAudioDraft.sourceTrackId = 3; // E-AC-3 5.1!
        app.onDraftBaseAudioChange();
        const escapedFromFlacToEac3Track = {{
          codec: app.addAudioDraft.targetCodec,
          blocked: app.editorDraftBlockStatus.blocked,
          sourceAudioIndex: app.editorDraftSourceTrack.audio_index,
        }};

        console.log(JSON.stringify({{
          dropdownBitrate,
          modalBitrate,
          nullChannelMatchPayload,
          escapedFromFlacToEac3Track,
        }}));
        """
        out = _run_node_json(script)
        assert out["dropdownBitrate"] == "448k"
        assert out["modalBitrate"] == "448k"
        # Missing MKV channel count + Match Source MUST send null (NOT fabricated 6!)
        assert out["nullChannelMatchPayload"]["source_channels"] is None
        assert out["nullChannelMatchPayload"]["target_channels"] is None
        assert out["nullChannelMatchPayload"]["source_audio_index"] == 0
        # Escaping FLAC onto an E-AC-3 track must land on a valid non-blocked codec (not eac3!)
        assert out["escapedFromFlacToEac3Track"]["codec"] not in ("flac", "eac3")
        assert out["escapedFromFlacToEac3Track"]["blocked"] is False
        assert out["escapedFromFlacToEac3Track"]["sourceAudioIndex"] == 2
