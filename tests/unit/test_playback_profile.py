"""Exhaustive unit tests for Phase 15 Global Playback Profile & 3-Step Onboarding."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.config import AppConstants
from app.services.playback_profile import (
    PLAYBACK_PRESETS,
    classify_track_codec_key,
    evaluate_file_needs_conversion,
    is_track_compatible,
    resolve_playback_profile,
    sanitize_supported_codecs,
)

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


class TestBackendPlaybackProfileEngine:
    """100% positive, negative, and edge-case coverage for playback_profile.py."""

    def test_all_presets_strictly_use_supported_default_target_codecs(self) -> None:
        assert len(PLAYBACK_PRESETS) == 5
        for preset_id, defn in PLAYBACK_PRESETS.items():
            assert defn.preferred_target_codec in AppConstants.ALLOWED_PLAYBACK_PREFERRED_CODECS, (
                f"Preset {preset_id} must only default to eac3/ac3/aac, got {defn.preferred_target_codec}"
            )
            assert defn.max_channels in AppConstants.ALLOWED_PLAYBACK_MAX_CHANNELS
            assert len(defn.supported_codecs) > 0

    @pytest.mark.parametrize(
        ("codec", "channels", "expected"),
        [
            ("AAC", 6, "aac_51"),
            ("AAC", 8, "aac_51"),
            ("AAC", 2, "aac_20"),
            ("AAC", 1, "aac_20"),
            ("AAC", None, "aac_20"),
            ("EAC3", 6, "eac3"),
            ("E-AC-3", 6, "eac3"),
            ("AC3", 6, "ac3"),
            ("AC-3", 6, "ac3"),
            ("TrueHD", 8, "truehd"),
            ("DTS-HD", 8, "dts_hd_ma"),
            ("DTS-HD Master Audio", 8, "dts_hd_ma"),
            ("DTS-HD High Resolution Audio", 8, "dts"),
            ("DTS", 6, "dts"),
            ("FLAC", 8, "flac"),
            ("Opus", 8, "opus"),
            ("PCM", 6, "pcm"),
            ("ALAC", 2, "alac"),
            ("MP3", 2, "mp3"),
            ("unknown_codec", 2, None),
            (None, 6, None),
            ("   ", 6, None),
        ],
    )
    def test_classify_track_codec_key(
        self, codec: str | None, channels: int | None, expected: str | None
    ) -> None:
        assert classify_track_codec_key(codec, channels) == expected

    def test_anti_stereo_commentary_false_negative_protection(self) -> None:
        dolby_arc = resolve_playback_profile(preset="dolby_arc")
        optical = resolve_playback_profile(preset="optical_legacy")
        earc_avr = resolve_playback_profile(preset="earc_avr")
        stereo = resolve_playback_profile(preset="stereo_mobile")
        neutral = resolve_playback_profile(preset="neutral")

        # CRITICAL: AAC 5.1 main + AAC 2.0 commentary MUST still flag needs_conversion on 5.1/7.1 profiles!
        aac51_with_commentary = [
            {"codec": "AAC", "channels": 6},
            {"codec": "AAC", "channels": 2},
        ]
        assert evaluate_file_needs_conversion(aac51_with_commentary, dolby_arc) is True
        assert evaluate_file_needs_conversion(aac51_with_commentary, optical) is True
        assert evaluate_file_needs_conversion(aac51_with_commentary, earc_avr) is True
        # On 2.0 Stereo profile, the AAC 2.0 track IS compatible -> False!
        assert evaluate_file_needs_conversion(aac51_with_commentary, stereo) is False
        # On neutral (unconfigured), NEVER flags -> False!
        assert evaluate_file_needs_conversion(aac51_with_commentary, neutral) is False

        # Movie with AAC 5.1 + E-AC-3 5.1 + AAC 2.0 commentary: compatible on ARC/eARC, incompatible on Optical
        with_eac3 = [
            {"codec": "AAC", "channels": 6},
            {"codec": "EAC3", "channels": 6},
            {"codec": "AAC", "channels": 2},
        ]
        assert evaluate_file_needs_conversion(with_eac3, dolby_arc) is False
        assert evaluate_file_needs_conversion(with_eac3, earc_avr) is False
        assert evaluate_file_needs_conversion(with_eac3, optical) is True

        # TrueHD 7.1 only: compatible on eARC (7.1), incompatible on ARC (5.1) & Optical
        truehd_71 = [{"codec": "TrueHD", "channels": 8}]
        assert evaluate_file_needs_conversion(truehd_71, earc_avr) is False
        assert evaluate_file_needs_conversion(truehd_71, dolby_arc) is True
        assert evaluate_file_needs_conversion(truehd_71, optical) is True

        # Pure stereo movie (AAC 2.0 only): compatible on ARC, Optical, Stereo
        stereo_only = [{"codec": "AAC", "channels": 2}]
        assert evaluate_file_needs_conversion(stereo_only, dolby_arc) is False
        assert evaluate_file_needs_conversion(stereo_only, optical) is False
        assert evaluate_file_needs_conversion(stereo_only, stereo) is False

        # Multichannel-only file on Stereo 2.0 profile (DTS 5.1) -> needs downmix!
        dts_51 = [{"codec": "DTS", "channels": 6}]
        assert evaluate_file_needs_conversion(dts_51, stereo) is True

    def test_edge_cases_and_malformed_inputs_fail_safely(self) -> None:
        dolby_arc = resolve_playback_profile(preset="dolby_arc")
        assert evaluate_file_needs_conversion([], dolby_arc) is False
        assert evaluate_file_needs_conversion(None, dolby_arc) is False
        assert (
            evaluate_file_needs_conversion(
                [{"codec": "AAC", "channels": 6}],
                dolby_arc,
                scan_status=AppConstants.FILE_SCAN_STATUS_SCANNING,
            )
            is False
        )
        assert evaluate_file_needs_conversion([{"codec": "", "channels": 6}], dolby_arc) is False

        # Invalid preset degrades to neutral
        bogus = resolve_playback_profile(preset="nonexistent_preset")
        assert bogus.preset == "neutral"
        assert bogus.configured is False
        assert is_track_compatible("AAC", 6, bogus) is True

        # Empty custom codecs -> unconfigured
        empty_custom = resolve_playback_profile(preset="custom", supported_codecs=[])
        assert empty_custom.configured is False

        # Sanitizer strips bogus entries and deduplicates
        assert sanitize_supported_codecs(["EAC3", "eac3", "bogus", "", "aac_51"]) == [
            "eac3",
            "aac_51",
        ]


class TestFrontendPlaybackProfileAndOnboardingWizard:
    """Node VM tests for playback_profile.js, editor.js profile ranking, and app.js 3-step wizard."""

    def test_frontend_presets_customizer_and_editor_recommendation_ranking(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{ window: {{}}, globalThis: {{}} }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "playback_profile.js", "editor.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const PP = ctx.MKVForgePlaybackProfile;
        const Ed = ctx.MKVForgeEditor;

        const presetTargets = PP.PRESETS.map((p) => [p.id, p.preferredCodec]);
        let draft = PP.createProfileDraftFromSettings(null);
        draft = PP.applyPresetToDraft(draft, "optical_legacy");
        const opticalPatch = PP.buildSettingsPatchFromDraft(draft);
        const toggled = PP.toggleCodecInDraft(draft, "dts");
        const customPatch = PP.buildSettingsPatchFromDraft(toggled);
        const neutralPatch = PP.buildSettingsPatchFromDraft(PP.applyPresetToDraft(draft, "neutral"));

        // Editor recommendations adapt to Optical profile (promotes AC-3 to #1 with 'Your System')
        const stateAac51 = Ed.createEditorState({{
          id: 1, filepath: "m.mkv", filename: "m.mkv",
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", default_track: true }},
            {{ id: 1, type: "audio", codec: "AAC", audio_channels: 6, language: "eng", default_track: true, audio_index: 0 }},
          ],
          attachments: [],
        }});
        const opticalRecs = Ed.getRecommendedAddAudioOptions(stateAac51, {{
          preset: "optical_legacy",
          maxChannels: 6,
          preferredCodec: "ac3",
        }}).map((o) => [o.codec, o.badge]);

        // Stereo 2.0 profile on DTS 5.1 promotes AAC 2.0 downmix to #1!
        const stateDts51 = Ed.createEditorState({{
          id: 2, filepath: "d.mkv", filename: "d.mkv",
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", default_track: true }},
            {{ id: 1, type: "audio", codec: "DTS", audio_channels: 6, language: "eng", default_track: true, audio_index: 0 }},
          ],
          attachments: [],
        }});
        const stereoRecs = Ed.getRecommendedAddAudioOptions(stateDts51, {{
          preset: "stereo_mobile",
          maxChannels: 2,
          preferredCodec: "aac",
        }}).map((o) => [o.codec, o.resolvedChannels, o.badge]);

        console.log(JSON.stringify({{
          presetTargets,
          opticalPatch,
          customPatch,
          neutralPatch,
          opticalRecs,
          stereoRecs,
        }}));
        """
        out = _run_node_json(script)
        for _pid, target in out["presetTargets"]:
            assert target in ("eac3", "ac3", "aac")
        assert out["opticalPatch"]["playback_profile_preset"] == "optical_legacy"
        assert out["opticalPatch"]["playback_preferred_codec"] == "ac3"
        assert out["customPatch"]["playback_profile_preset"] == "custom"
        assert "dts" in out["customPatch"]["playback_supported_codecs"]
        assert out["neutralPatch"] == {
            "playback_profile_preset": "neutral",
            "playback_supported_codecs": [],
        }
        assert out["opticalRecs"][0] == ["ac3", "Your System"]
        assert out["stereoRecs"][0] == ["aac", 2, "Your System"]

    def test_app_three_step_onboarding_transitions_and_existing_install_skip(self) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const apiCalls = [];
        const ctx = {{
          URLSearchParams, setTimeout, clearTimeout,
          AbortController,
          EventSource: class {{ addEventListener() {{}} close() {{}} }},
          fetch: async (url, opts) => {{
            const body = opts && opts.body ? JSON.parse(opts.body) : null;
            apiCalls.push([url, (opts && opts.method) || "GET", body]);
            if (url === "/api/auth/setup") {{
              return {{
                ok: true,
                status: 200,
                json: async () => ({{
                  auth_mode: body.mode,
                  authenticated: true,
                  needs_setup: false,
                  user: {{
                    id: 1,
                    username: body.username || "anonymous",
                    display_name: body.display_name || "Anonymous",
                    is_anonymous: body.mode === "anonymous",
                  }},
                }}),
              }};
            }}
            if (url === "/api/settings") {{
              return {{
                ok: true,
                status: 200,
                json: async () => ({{ ...ctx.MKVForgeConstants.DEFAULT_USER_SETTINGS, ...(body || {{}}) }}),
              }};
            }}
            if (url.startsWith("/api/scan")) {{
              return {{ ok: true, status: 202, json: async () => ({{ status: "started" }}) }};
            }}
            if (url === "/api/dashboard") {{
              return {{ ok: true, status: 200, json: async () => ({{ stats: {{ total_files: 0, aac51_only: 0, has_metadata: 0, needs_conversion: 0 }}, analytics: {{}} }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{ active: null, queued: [], history: [] }}) }};
          }},
          document: {{ addEventListener: () => {{}}, readyState: "complete" }},
          localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          location: {{ hash: "#/" }},
          history: {{ replaceState: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of [
          "constants.js", "formatters.js", "api.js", "folder_tree.js",
          "editor.js", "pull_refresh.js", "toast_stack.js", "library_return.js",
          "playback_profile.js", "app.js"
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        async function run() {{
          // 1. Fresh install -> enters 3-step onboarding at Step 1
          const app = ctx.mkvForgeApp();
          app._applyAuthStatus({{
            auth_mode: "unconfigured",
            authenticated: true,
            needs_setup: true,
            storage_path: "/storage",
            storage_writable: true,
          }});
          const freshRequired = app.isOnboardingRequired;
          const stepStart = app.onboardingWizardStep;

          // Validation blocks short password on Step 1
          app.onboardingForm.username = "admin";
          app.onboardingForm.password = "123";
          app.onboardingForm.confirmPassword = "123";
          app.onboardingNextFromAccess();
          const blockedOnStep1 = app.onboardingWizardStep === 1 && Boolean(app.authError);

          // Valid local credentials advance smoothly to Step 2
          app.onboardingForm.password = "secret123";
          app.onboardingForm.confirmPassword = "secret123";
          app.onboardingNextFromAccess();
          const stepAfterAccess = app.onboardingWizardStep;
          const dirFwd = app.onboardingDirection;

          // Pick preset on Step 2 and advance to Step 3
          app.onboardingSelectPreset("dolby_arc");
          app.onboardingNextFromSoundSystem();
          const step3 = app.onboardingWizardStep;

          // Back button smoothly glides back to Step 2 with reverse direction
          app.onboardingBack();
          const backToStep2 = app.onboardingWizardStep;
          const dirBack = app.onboardingDirection;

          // Advance to Step 3 and commit (always triggers initial background scan)
          app.onboardingNextFromSoundSystem();
          await app.finishOnboarding();
          const requiredAfterFinish = app.isOnboardingRequired;

          // 2. Anonymous one-tap flow via onboardingContinueAnonymous() skips credential validation
          const anonApp = ctx.mkvForgeApp();
          const mockBodyEl = {{ scrollTop: 180 }};
          anonApp.$refs = {{ onboardingBody: mockBodyEl }};
          anonApp._applyAuthStatus({{
            auth_mode: "unconfigured",
            authenticated: true,
            needs_setup: true,
          }});
          anonApp.onboardingContinueAnonymous();
          const anonStep2 = anonApp.onboardingWizardStep;
          const anonChoice = anonApp.onboardingAuthChoice;
          const scrollResetForward = mockBodyEl.scrollTop === 0;

          // Returning Back to Step 1 resets scroll and clicking Create enforces local validation again
          mockBodyEl.scrollTop = 240;
          anonApp.onboardingBack();
          const scrollResetBack = mockBodyEl.scrollTop === 0;
          anonApp.onboardingForm.username = "a";
          anonApp.onboardingNextFromAccess();
          const localValidationRestored =
            anonApp.onboardingWizardStep === 1 &&
            anonApp.onboardingAuthChoice === "local" &&
            Boolean(anonApp.authError);

          // Re-choosing Continue Without Sign-In clears error and finishes as anonymous (mandatory scan fires)
          anonApp.onboardingContinueAnonymous();
          const anonErrorCleared = anonApp.authError === null && anonApp.onboardingWizardStep === 2;
          mockBodyEl.scrollTop = 160;
          anonApp.onboardingSkipSoundSystem();
          const scrollResetSkip = mockBodyEl.scrollTop === 0;
          const scanCountBeforeAnonFinish = apiCalls.filter((c) => c[0].startsWith("/api/scan") && c[1] === "POST").length;
          await anonApp.finishOnboarding();
          const scanCountAfterAnonFinish = apiCalls.filter((c) => c[0].startsWith("/api/scan") && c[1] === "POST").length;
          const anonTriggeredScan = scanCountAfterAnonFinish === scanCountBeforeAnonFinish + 1;
          const anonSetupCall = apiCalls.filter((c) => c[0] === "/api/auth/setup").pop();

          // 3. Existing configured install -> NEVER triggers onboarding
          const existingApp = ctx.mkvForgeApp();
          existingApp._applyAuthStatus({{
            auth_mode: "anonymous",
            authenticated: true,
            needs_setup: false,
          }});
          const existingRequired = existingApp.isOnboardingRequired;

          console.log(JSON.stringify({{
            freshRequired,
            stepStart,
            blockedOnStep1,
            stepAfterAccess,
            dirFwd,
            step3,
            backToStep2,
            dirBack,
            requiredAfterFinish,
            anonStep2,
            anonChoice,
            scrollResetForward,
            scrollResetBack,
            scrollResetSkip,
            localValidationRestored,
            anonErrorCleared,
            anonTriggeredScan,
            anonSetupMode: anonSetupCall ? anonSetupCall[2].mode : null,
            existingRequired,
            apiCalls,
          }}));
        }}
        run();
        """
        out = _run_node_json(script)
        assert out["freshRequired"] is True
        assert out["stepStart"] == 1
        assert out["blockedOnStep1"] is True
        assert out["stepAfterAccess"] == 2
        assert out["dirFwd"] == "forward"
        assert out["step3"] == 3
        assert out["backToStep2"] == 2
        assert out["dirBack"] == "back"
        assert out["requiredAfterFinish"] is False
        assert out["anonStep2"] == 2
        assert out["anonChoice"] == "anonymous"
        assert out["scrollResetForward"] is True
        assert out["scrollResetBack"] is True
        assert out["scrollResetSkip"] is True
        assert out["localValidationRestored"] is True
        assert out["anonErrorCleared"] is True
        assert out["anonTriggeredScan"] is True
        assert out["anonSetupMode"] == "anonymous"
        assert out["existingRequired"] is False
        setup_call = next(c for c in out["apiCalls"] if c[0] == "/api/auth/setup")
        settings_call = next(c for c in out["apiCalls"] if c[0] == "/api/settings")
        scan_call = next(c for c in out["apiCalls"] if c[0].startswith("/api/scan"))
        assert setup_call[2]["mode"] == "local"
        assert settings_call[2]["playback_profile_preset"] == "dolby_arc"
        assert scan_call[1] == "POST"

    def test_frontend_factory_reset_clears_client_storage_and_opens_onboarding_step1(
        self,
    ) -> None:
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const localMap = new Map([
          ["mkvforge.themeMode", "light"],
          ["mkvforge.accentColor", "rose"],
          ["mkvforge.libraryView", "list"],
        ]);
        const sessionMap = new Map([
          ["mkvforge.libraryReturnState", '{{"hash":"#/library?page=3"}}'],
        ]);
        const apiCalls = [];
        const ctx = {{
          URLSearchParams, setTimeout, clearTimeout,
          AbortController,
          EventSource: class {{ addEventListener() {{}} close() {{}} }},
          fetch: async (url, opts) => {{
            const body = opts && opts.body ? JSON.parse(opts.body) : null;
            apiCalls.push([url, (opts && opts.method) || "GET", body]);
            if (url === "/api/maintenance/factory-reset") {{
              return {{ ok: true, status: 200, json: async () => ({{ status: "reset", removed_db_files: 3, removed_uploads: 1, message: "Reset" }}) }};
            }}
            if (url === "/api/auth/status") {{
              return {{ ok: true, status: 200, json: async () => ({{ auth_mode: "unconfigured", authenticated: true, needs_setup: true, storage_path: "/storage", storage_writable: true }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{ stats: {{ total_files: 0, aac51_only: 0, has_metadata: 0, needs_conversion: 0 }}, analytics: {{}} }}) }};
          }},
          document: {{ addEventListener: () => {{}}, readyState: "complete" }},
          localStorage: {{
            getItem: (k) => (localMap.has(k) ? localMap.get(k) : null),
            setItem: (k, v) => localMap.set(k, String(v)),
            removeItem: (k) => localMap.delete(k),
          }},
          sessionStorage: {{
            getItem: (k) => (sessionMap.has(k) ? sessionMap.get(k) : null),
            setItem: (k, v) => sessionMap.set(k, String(v)),
            removeItem: (k) => sessionMap.delete(k),
          }},
          location: {{ hash: "#/settings" }},
          history: {{ replaceState: (_s, _t, u) => {{ ctx.location.hash = u; }} }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of [
          "constants.js", "formatters.js", "api.js", "folder_tree.js",
          "editor.js", "pull_refresh.js", "toast_stack.js", "library_return.js",
          "playback_profile.js", "app.js"
        ]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(JS_DIR))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        async function run() {{
          const app = ctx.mkvForgeApp();
          app.authReady = true;
          app.authMode = "anonymous";
          app.settingsModalOpen = true;
          app.openFactoryResetModal();

          const blockedEmpty = app.canSubmitFactoryReset;
          app.factoryResetConfirmText = "reset";
          const blockedLowercase = app.canSubmitFactoryReset;
          app.factoryResetConfirmText = "RESET";
          const allowedReset = app.canSubmitFactoryReset;

          await app.submitFactoryReset();

          console.log(JSON.stringify({{
            blockedEmpty,
            blockedLowercase,
            allowedReset,
            modalClosed: !app.factoryResetModalOpen && !app.settingsModalOpen,
            onboardingStep: app.onboardingWizardStep,
            onboardingRequired: app.isOnboardingRequired,
            authMode: app.authMode,
            remainingLocalKeys: Array.from(localMap.keys()),
            remainingSessionKeys: Array.from(sessionMap.keys()),
            resetHash: ctx.location.hash,
            resetApiCall: apiCalls.find((c) => c[0] === "/api/maintenance/factory-reset"),
          }}));
        }}
        run();
        """
        out = _run_node_json(script)
        assert out["blockedEmpty"] is False
        assert out["blockedLowercase"] is False
        assert out["allowedReset"] is True
        assert out["modalClosed"] is True
        assert out["onboardingStep"] == 1
        assert out["onboardingRequired"] is True
        assert out["authMode"] == "unconfigured"
        assert out["remainingLocalKeys"] == []
        assert out["remainingSessionKeys"] == []
        assert out["resetHash"] == "#/"
        assert out["resetApiCall"] == [
            "/api/maintenance/factory-reset",
            "POST",
            {"confirm_text": "RESET"},
        ]
