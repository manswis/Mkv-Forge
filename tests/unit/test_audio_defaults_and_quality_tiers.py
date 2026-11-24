"""Exhaustive unit, integration & Node VM tests for generic Default Audio Codec, Quality Tiers & Custom Playback Profile codecs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository, TemplateRepository
from app.models.schemas import (
    BatchAudioRule,
    BatchRuleSpec,
    JobConfig,
    UserSettingsUpdate,
)
from app.services.batch_compiler import (
    compile_batch_rule_for_file,
    resolve_default_bitrate_for_codec,
)
from app.services.ffmpeg_runner import build_ffmpeg_command
from app.services.playback_profile import resolve_playback_profile
from app.services.user_settings import UserSettingsService

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_DIR = REPO_ROOT / "app" / "static" / "js"
INDEX_HTML_PATH = REPO_ROOT / "app" / "static" / "index.html"


def _run_node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


class TestFullChannelQualityBitrateMatrix:
    """Verify full 1.0..7.1 quality-tier coverage across all 5 codecs in Python, JS, and FFmpeg CLI."""

    @pytest.mark.parametrize("codec", ["eac3", "ac3", "aac", "opus", "flac"])
    @pytest.mark.parametrize("channels", [1, 2, 3, 4, 5, 6, 7, 8])
    @pytest.mark.parametrize("quality", ["high", "balanced", "compact"])
    def test_backend_quality_matrix_valid_for_ffmpeg_all_channels(
        self,
        tmp_path: Path,
        codec: str,
        channels: int,
        quality: str,
    ) -> None:
        bitrate = resolve_default_bitrate_for_codec(codec, channels, quality=quality)
        if codec == "flac":
            assert bitrate is None
        else:
            assert bitrate in AppConstants.AUDIO_CODEC_ALLOWED_BITRATES[codec]

        cmd = build_ffmpeg_command(
            tmp_path / "src.mkv",
            tmp_path / f"out.mkvforge.{AppConstants.TEMP_AUDIO_EXT_MAP[codec]}",
            audio_index=0,
            target_codec=codec,
            bitrate=bitrate,
            channels=channels,
        )
        assert "-c:a" in cmd
        if codec == "flac":
            assert "-b:a" not in cmd
        else:
            assert cmd[cmd.index("-b:a") + 1] == bitrate

    def test_negative_and_out_of_bounds_quality_and_channel_inputs_fail_safely(self) -> None:
        # Invalid quality falls back to high
        assert resolve_default_bitrate_for_codec("eac3", 6, quality="bogus") == "640k"
        assert resolve_default_bitrate_for_codec("opus", 8, quality="") == "448k"
        # Out-of-bounds channels: non-positive falls back to 5.1 surround; > max clamps to codec ceiling
        assert resolve_default_bitrate_for_codec("eac3", 0, quality="compact") == "384k"
        assert resolve_default_bitrate_for_codec("eac3", -3, quality="high") == "640k"
        assert resolve_default_bitrate_for_codec("eac3", 1, quality="compact") == "96k"
        assert resolve_default_bitrate_for_codec("eac3", 8, quality="high") == "640k"
        assert resolve_default_bitrate_for_codec("eac3", 12, quality="balanced") == "448k"
        assert resolve_default_bitrate_for_codec("opus", 12, quality="compact") == "256k"
        assert resolve_default_bitrate_for_codec("flac", 1, quality="compact") is None
        assert resolve_default_bitrate_for_codec("flac", 8, quality="high") is None

    def test_python_and_js_quality_matrix_exact_parity(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: {{}}, globalThis: {{}} }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "editor.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Ed = ctx.MKVForgeEditor;
        const codecs = ["eac3", "ac3", "aac", "opus", "flac"];
        const channels = [1, 2, 3, 4, 5, 6, 7, 8];
        const tiers = ["high", "balanced", "compact"];
        const results = {{}};
        const optionCoverageErrors = [];
        for (const c of codecs) {{
          results[c] = {{}};
          for (const ch of channels) {{
            results[c][ch] = {{}};
            const clampedCh = Ed.resolveTargetChannels(ch, c, null);
            const cfg = Ed.getBitrateConfigFor(c, clampedCh);
            for (const t of tiers) {{
              const br = Ed.resolveQualityBitrate(c, ch, t);
              results[c][ch][t] = br;
              if (c !== "flac" && !cfg.options.includes(br)) {{
                optionCoverageErrors.push(`${{c}} ch=${{ch}} (clamped=${{clampedCh}}) tier=${{t}} br=${{br}} not in ${{JSON.stringify(cfg.options)}}`);
              }}
            }}
          }}
        }}
        console.log(JSON.stringify({{ results, optionCoverageErrors }}));
        """
        out = _run_node_json(script)
        assert out["optionCoverageErrors"] == []
        for codec in ("eac3", "ac3", "aac", "opus", "flac"):
            for ch in (1, 2, 3, 4, 5, 6, 7, 8):
                for tier in ("high", "balanced", "compact"):
                    py_val = resolve_default_bitrate_for_codec(codec, ch, quality=tier)
                    js_val = out["results"][codec][str(ch)][tier]
                    assert js_val == py_val, f"Mismatch for {codec} {ch}ch {tier}: py={py_val} vs js={js_val}"


class TestBackwardCompatibilityMigrationAndTemplates:
    """Verify legacy default_dolby_* migration, bidirectional API sync, Custom Playback Profile Opus/FLAC, and saved job_templates."""

    def test_legacy_sqlite_settings_migrate_cleanly_without_breaking_existing_rows(
        self, tmp_path: Path
    ) -> None:
        db = DatabaseManager(tmp_path / "test.db")
        db.initialize()
        state_repo = AppStateRepository(db)
        svc = UserSettingsService(state_repo=state_repo)

        # Case 1: Legacy row with ac3 + 448k (no default_audio_* keys)
        state_repo.set(
            AppConstants.STATE_KEY_USER_SETTINGS,
            json.dumps({"default_dolby_codec": "ac3", "default_dolby_bitrate": "448k"}),
        )
        migrated = svc.get()
        assert migrated.default_audio_codec == "ac3"
        assert migrated.default_audio_quality == "balanced"
        assert migrated.default_dolby_codec == "ac3"
        assert migrated.default_dolby_bitrate == "448k"

        # Case 2: Legacy row with eac3 + 384k -> default_audio_codec=auto, quality=compact
        state_repo.set(
            AppConstants.STATE_KEY_USER_SETTINGS,
            json.dumps({"default_dolby_codec": "eac3", "default_dolby_bitrate": "384k"}),
        )
        migrated_compact = svc.get()
        assert migrated_compact.default_audio_codec == "auto"
        assert migrated_compact.default_audio_quality == "compact"

        # Case 3: Legacy row with 640k -> quality=high
        state_repo.set(
            AppConstants.STATE_KEY_USER_SETTINGS,
            json.dumps({"default_dolby_codec": "eac3", "default_dolby_bitrate": "640k"}),
        )
        migrated_high = svc.get()
        assert migrated_high.default_audio_quality == "high"

        # Case 4: Updating new fields mirrors legacy aliases cleanly
        updated = svc.update(
            UserSettingsUpdate(default_audio_codec="opus", default_audio_quality="compact")
        )
        assert updated.default_audio_codec == "opus"
        assert updated.default_audio_quality == "compact"
        assert updated.default_dolby_bitrate == "384k"

        # Case 5: Updating legacy fields mirrors new fields cleanly
        legacy_updated = svc.update(
            UserSettingsUpdate(default_dolby_codec="ac3", default_dolby_bitrate="448k")
        )
        assert legacy_updated.default_audio_codec == "ac3"
        assert legacy_updated.default_audio_quality == "balanced"

    async def test_api_supports_new_audio_defaults_and_custom_playback_opus_flac(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/api/settings")
        assert resp.status_code == AppConstants.HTTP_OK
        body = resp.json()
        assert body["default_audio_codec"] == "auto"
        assert body["default_audio_quality"] == "high"

        # Custom Playback Profile now accepts opus and flac as playback_preferred_codec
        for pref_codec in ("opus", "flac"):
            patch_resp = await api_client.patch(
                "/api/settings",
                json={
                    "playback_profile_preset": "custom",
                    "playback_supported_codecs": ["eac3", "opus", "flac"],
                    "playback_max_channels": 8,
                    "playback_preferred_codec": pref_codec,
                    "default_audio_codec": "auto",
                    "default_audio_quality": "balanced",
                },
            )
            assert patch_resp.status_code == AppConstants.HTTP_OK
            data = patch_resp.json()
            assert data["playback_preferred_codec"] == pref_codec
            assert data["default_audio_codec"] == "auto"
            assert data["default_audio_quality"] == "balanced"
            assert data["default_dolby_bitrate"] == "448k"

            profile = api_app.state.user_settings_service.get_playback_profile()
            assert profile.preferred_target_codec == pref_codec

        # Invalid codec/quality rejected with 422
        assert (
            await api_client.patch("/api/settings", json={"playback_preferred_codec": "mp3"})
        ).status_code == 422
        assert (
            await api_client.patch("/api/settings", json={"default_audio_codec": "mp3"})
        ).status_code == 422
        assert (
            await api_client.patch("/api/settings", json={"default_audio_quality": "ultra"})
        ).status_code == 422

        # Existing saved job_templates continue to deserialize and compile untouched
        tpl_repo: TemplateRepository = api_app.state.template_repo
        legacy_rule = BatchRuleSpec(
            audio=BatchAudioRule(enabled=True, target_codec="eac3", bitrate="448k")
        )
        created = tpl_repo.create_template(
            name="Legacy Dolby 448k",
            description="Saved before Option A",
            rule_json=legacy_rule.model_dump_json(),
        )
        loaded = tpl_repo.get_by_id(created["id"])
        assert loaded is not None
        parsed_rule = BatchRuleSpec.model_validate_json(loaded["rule_json"])
        assert parsed_rule.audio.target_codec == "eac3"
        assert parsed_rule.audio.bitrate == "448k"

    def test_resolve_playback_profile_accepts_opus_and_flac_and_rejects_unknown(self) -> None:
        opus_prof = resolve_playback_profile(
            preset="custom",
            supported_codecs=["opus", "aac_20"],
            max_channels=8,
            preferred_target_codec="opus",
        )
        assert opus_prof.preferred_target_codec == "opus"
        assert opus_prof.configured is True

        flac_prof = resolve_playback_profile(
            preset="custom",
            supported_codecs=["flac", "pcm"],
            max_channels=8,
            preferred_target_codec="flac",
        )
        assert flac_prof.preferred_target_codec == "flac"

        bad_prof = resolve_playback_profile(
            preset="custom",
            supported_codecs=["eac3"],
            max_channels=6,
            preferred_target_codec="mp3",
        )
        assert bad_prof.preferred_target_codec == AppConstants.DEFAULT_PLAYBACK_PREFERRED_CODEC


class TestFrontendUiFlacStateAndCliTranslation:
    """Verify UI landmarks, FLAC disabled quality state, live readout, and Editor/Batch -> FFmpeg CLI translation."""

    def test_html_landmarks_replace_legacy_dolby_controls(self) -> None:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        assert "Default Target Codec" in html
        assert "Default Audio Quality" in html
        assert "Lossless · Bitrate N/A" in html
        assert 'x-text="audioQualityReadout"' in html
        assert 'x-text="defaultAudioCodecHelperText"' in html
        assert ':disabled="isDefaultAudioCodecLossless"' in html
        assert "Default Dolby Codec" not in html
        assert "Default Dolby Bitrate" not in html

    def test_frontend_auto_codec_flac_ui_state_and_end_to_end_cli_translation(
        self, tmp_path: Path
    ) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const patchedPayloads = [];
        const ctx = {{
          console, setTimeout, clearTimeout,
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{ this.addEventListener = () => {{}}; this.close = () => {{}}; }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/library", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "loading", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
          fetch: async (url, opts) => {{
            if (url === "/api/settings" && opts && opts.method === "PATCH") {{
              const body = JSON.parse(opts.body);
              patchedPayloads.push(body);
              return {{ ok: true, status: 200, json: async () => ({{ ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS, ...body }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{}}) }};
          }},
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of [
          "constants.js", "formatters.js", "api.js", "folder_tree.js",
          "playback_profile.js", "editor.js", "batch.js", "pull_refresh.js",
          "toast_stack.js", "library_return.js", "app.js"
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        (async () => {{
          const app = ctx.mkvForgeApp();
          const Ed = ctx.MKVForgeEditor;
          const B = ctx.MKVForgeBatch;
          const PP = ctx.MKVForgePlaybackProfile;

          // 1. Custom Playback Profile supports opus & flac
          const ppTargetIds = PP.PREFERRED_TARGET_OPTIONS.map((o) => o.codec);

          // 2. FLAC explicit disables quality tier & shows Lossless readout
          app.userSettings = {{
            ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS,
            default_audio_codec: "flac",
            default_audio_quality: "high",
          }};
          const flacExplicitLossless = app.isDefaultAudioCodecLossless;
          const flacExplicitReadout = app.audioQualityReadout;
          await app.setDefaultAudioQuality("compact"); // must be a no-op!
          const patchesAfterBlockedQualityClick = patchedPayloads.length;

          // 3. Auto mode following Custom FLAC Playback Profile ALSO marks lossless!
          app.userSettings = {{
            ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS,
            default_audio_codec: "auto",
            playback_profile_preset: "custom",
            playback_supported_codecs: ["flac"],
            playback_max_channels: 8,
            playback_preferred_codec: "flac",
          }};
          const flacAutoLossless = app.isDefaultAudioCodecLossless;
          const flacAutoHelper = app.defaultAudioCodecHelperText;

          // 4. FLAC on lossy AAC 5.1 must NOT recommend blocked AAC -> FLAC; lossless TrueHD 7.1 DOES!
          const lossyFile = {{
            id: 1, filepath: "m.mkv", filename: "m.mkv", folder: "/",
            tracks: [
              {{ id: 0, type: "video", codec: "HEVC", default_track: true }},
              {{ id: 1, type: "audio", codec: "AAC", audio_channels: 6, audio_index: 0, language: "eng", default_track: true }},
            ],
            attachments: [],
          }};
          app.editorFile = lossyFile;
          app.editor = Ed.createEditorState(lossyFile, app.userSettings);
          const lossyRecsUnderFlac = app.editorRecommendedAudioOptions.map((o) => o.codec);

          const losslessFile = {{
            id: 2, filepath: "uhd.mkv", filename: "uhd.mkv", folder: "/",
            tracks: [
              {{ id: 0, type: "video", codec: "HEVC", default_track: true }},
              {{ id: 1, type: "audio", codec: "TrueHD", audio_channels: 8, audio_index: 0, language: "eng", default_track: true }},
            ],
            attachments: [],
          }};
          app.editorFile = losslessFile;
          app.editor = Ed.createEditorState(losslessFile, app.userSettings);
          app.openCustomAddAudio();
          app.confirmAddAudio();
          const flacPayload = Ed.buildJobRequestPayload(app.editor, losslessFile);

          // 5. Auto + Streaming Clients (AAC) + Balanced quality on DTS 5.1 -> AAC 384k
          app.userSettings = {{
            ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS,
            default_audio_codec: "auto",
            default_audio_quality: "balanced",
            playback_profile_preset: "streaming_clients",
            playback_supported_codecs: ["eac3", "ac3", "aac_20", "aac_51", "opus", "flac"],
            playback_max_channels: 8,
            playback_preferred_codec: "aac",
          }};
          const aacBalancedReadout = app.audioQualityReadout;
          const dtsFile = {{
            id: 3, filepath: "dts.mkv", filename: "dts.mkv", folder: "/",
            tracks: [
              {{ id: 0, type: "video", codec: "HEVC", default_track: true }},
              {{ id: 1, type: "audio", codec: "DTS", audio_channels: 6, audio_index: 0, language: "eng", default_track: true }},
            ],
            attachments: [],
          }};
          app.editorFile = dtsFile;
          app.editor = Ed.createEditorState(dtsFile, app.userSettings);
          app.openCustomAddAudio();
          app.confirmAddAudio();
          const aacBalancedPayload = Ed.buildJobRequestPayload(app.editor, dtsFile);

          // 6. Explicit Opus + Compact quality on TrueHD 7.1 -> Opus 256k 7.1
          app.userSettings = {{
            ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS,
            default_audio_codec: "opus",
            default_audio_quality: "compact",
            playback_profile_preset: "neutral",
          }};
          const opusCompactReadout = app.audioQualityReadout;
          app.editorFile = losslessFile;
          app.editor = Ed.createEditorState(losslessFile, app.userSettings);
          app.openCustomAddAudio();
          app.confirmAddAudio();
          const opusCompactPayload = Ed.buildJobRequestPayload(app.editor, losslessFile);

          // 7. Batch Studio createDefaultBatchRule honors Auto + Custom Opus + Balanced quality
          const batchRuleOpusBalanced = B.createDefaultBatchRule({{
            ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS,
            default_audio_codec: "auto",
            default_audio_quality: "balanced",
            playback_profile_preset: "custom",
            playback_preferred_codec: "opus",
          }});

          console.log(JSON.stringify({{
            ppTargetIds,
            flacExplicitLossless,
            flacExplicitReadout,
            patchesAfterBlockedQualityClick,
            flacAutoLossless,
            flacAutoHelper,
            lossyRecsUnderFlac,
            flacPayload,
            aacBalancedReadout,
            aacBalancedPayload,
            opusCompactReadout,
            opusCompactPayload,
            batchRuleOpusBalanced,
          }}));
        }})();
        """
        out = _run_node_json(script)

        # Playback Profile Custom includes Opus & FLAC
        assert out["ppTargetIds"] == ["eac3", "ac3", "aac", "opus", "flac"]

        # FLAC UI state & lockout
        assert out["flacExplicitLossless"] is True
        assert out["flacAutoLossless"] is True
        assert out["patchesAfterBlockedQualityClick"] == 0
        assert "Lossless PCM · Bitrate N/A" in out["flacExplicitReadout"]
        assert "FLAC" in out["flacAutoHelper"]

        # Blocked AAC -> FLAC never promoted; TrueHD -> FLAC translates to `-c:a flac` without `-b:a`
        assert "flac" not in out["lossyRecsUnderFlac"]
        flac_cfg = JobConfig.model_validate(out["flacPayload"]["config"])
        assert flac_cfg.audio_conversion.target_codec == "flac"
        assert flac_cfg.audio_conversion.bitrate is None
        flac_cmd = build_ffmpeg_command(
            tmp_path / "uhd.mkv",
            tmp_path / "uhd.mkvforge.mka",
            audio_index=flac_cfg.audio_conversion.source_audio_index,
            target_codec=flac_cfg.audio_conversion.target_codec,
            bitrate=flac_cfg.audio_conversion.bitrate,
            channels=flac_cfg.audio_conversion.source_channels or 8,
            requested_channels=flac_cfg.audio_conversion.target_channels,
        )
        assert flac_cmd[flac_cmd.index("-c:a") + 1] == "flac"
        assert "-b:a" not in flac_cmd
        assert flac_cmd[flac_cmd.index("-ac") + 1] == "8"

        # Auto + Streaming Clients (AAC) + Balanced -> `-c:a aac -b:a 384k -ac 6`
        assert "1.0 128k" in out["aacBalancedReadout"]
        assert "2.0 192k" in out["aacBalancedReadout"]
        assert "5.1 384k" in out["aacBalancedReadout"]
        assert "7.1 512k" in out["aacBalancedReadout"]
        aac_cfg = JobConfig.model_validate(out["aacBalancedPayload"]["config"])
        assert aac_cfg.audio_conversion.target_codec == "aac"
        assert aac_cfg.audio_conversion.bitrate == "384k"
        aac_cmd = build_ffmpeg_command(
            tmp_path / "dts.mkv",
            tmp_path / "dts.mkvforge.mka",
            audio_index=0,
            target_codec=aac_cfg.audio_conversion.target_codec,
            bitrate=aac_cfg.audio_conversion.bitrate,
            channels=6,
        )
        assert aac_cmd[aac_cmd.index("-c:a") + 1] == "aac"
        assert aac_cmd[aac_cmd.index("-b:a") + 1] == "384k"

        # Explicit Opus + Compact on TrueHD 7.1 -> `-c:a libopus -b:a 256k -ac 8 -mapping_family 1`
        assert out["opusCompactReadout"] == "Opus: 1.0 64k · 2.0 96k · 5.1 192k · 7.1 256k"
        opus_cfg = JobConfig.model_validate(out["opusCompactPayload"]["config"])
        assert opus_cfg.audio_conversion.target_codec == "opus"
        assert opus_cfg.audio_conversion.bitrate == "256k"
        opus_cmd = build_ffmpeg_command(
            tmp_path / "uhd.mkv",
            tmp_path / "uhd.mkvforge.mka",
            audio_index=0,
            target_codec=opus_cfg.audio_conversion.target_codec,
            bitrate=opus_cfg.audio_conversion.bitrate,
            channels=8,
        )
        assert opus_cmd[opus_cmd.index("-c:a") + 1] == "libopus"
        assert opus_cmd[opus_cmd.index("-b:a") + 1] == "256k"
        assert opus_cmd[opus_cmd.index("-ac") + 1] == "8"
        assert opus_cmd[opus_cmd.index("-mapping_family") + 1] == "1"

        # Batch Studio rule -> compile -> FFmpeg CLI
        batch_rule = BatchRuleSpec.model_validate(out["batchRuleOpusBalanced"])
        assert batch_rule.audio.target_codec == "opus"
        assert batch_rule.audio.bitrate == "256k"
        assert batch_rule.audio.accept_existing_dolby is False
        raw_probe = {
            "container": {"properties": {"title": "", "duration": 60_000_000_000}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "MPEG-H/HEVC/H.265", "properties": {"default_track": True}},
                {"id": 1, "type": "audio", "codec": "AAC", "properties": {"audio_channels": 6, "language": "eng", "default_track": True}},
            ],
            "global_tags": [],
            "track_tags": [],
            "attachments": [],
            "chapters": [],
        }
        row = {
            "id": 99,
            "filepath": "Movies/Test.mkv",
            "filename": "Test.mkv",
            "folder": "/Movies",
            "size_bytes": 1024,
            "scan_status": AppConstants.FILE_SCAN_STATUS_READY,
            "container_title": "",
            "has_global_tags": False,
            "raw_json": json.dumps(raw_probe),
        }
        plan = compile_batch_rule_for_file(row, batch_rule, default_audio_quality="balanced")
        assert plan.job_config is not None
        assert plan.job_config.audio_conversion.target_codec == "opus"
        assert plan.job_config.audio_conversion.bitrate == "256k"
