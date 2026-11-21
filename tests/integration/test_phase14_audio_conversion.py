"""Integration tests for Phase 14 audiophile audio conversion matrix (API + real FFmpeg/mkvmerge)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileUpsert, utc_now_iso
from app.models.schemas import (
    AudioConversionConfig,
    ContainerMetadataConfig,
    JobConfig,
    JobCreateRequest,
    TrackEditConfig,
)
from app.services.ffmpeg_runner import (
    build_ffmpeg_command,
    compute_target_channels,
    resolve_effective_bitrate,
    run_ffmpeg_transcode,
)
from app.services.job_summary import summarize_job_changes
from app.services.mkv_builder import build_mkvmerge_command
from app.services.mkv_runner import execute_two_stage_pipeline
from app.services.preflight import (
    estimate_audio_size_bytes,
    validate_audio_conversion_pair,
)

_HAS_FFMPEG_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_HAS_MEDIA_TOOLS = bool(_HAS_FFMPEG_TOOLS and shutil.which("mkvmerge"))


def _seed_media_file(api_app, test_settings: Settings, rel_path: str) -> int:
    target = test_settings.storage_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic-media")
    return api_app.state.media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_path,
            folder=f"/{Path(rel_path).parent.as_posix()}",
            filename=Path(rel_path).name,
            size_bytes=target.stat().st_size,
            mtime=1700000000.0,
            duration_us=10_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json="[]",
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=False,
            has_ac3_eac3=False,
            raw_json="{}",
            scanned_at=utc_now_iso(),
        )
    )


def _make_job_request(file_id: int, audio_conversion: dict) -> dict:
    return {
        "file_id": file_id,
        "config": {
            "container": {"title": "", "strip_global_tags": False},
            "audio_conversion": audio_conversion,
            "tracks": [
                {"id": 0, "type": "video", "enabled": True, "default_track": True},
                {"id": 1, "type": "audio", "enabled": True, "default_track": False},
            ],
            "output_mode": "replace",
        },
    }


class TestPhase14JobsApiMatrix:
    """Verify POST /api/jobs accepts valid Phase 14 payloads and rejects invalid combinations."""

    @pytest.mark.parametrize(
        ("filename", "audio_cfg", "expected_change"),
        [
            (
                "Opus71.mkv",
                {
                    "enabled": True,
                    "source_track_id": 1,
                    "source_audio_index": 0,
                    "source_channels": 8,
                    "source_codec": "AAC",
                    "target_codec": "opus",
                    "target_channels": 8,
                    "bitrate": "448k",
                    "track_title": "Opus 7.1",
                    "language": "eng",
                    "set_as_default": True,
                },
                "+ Opus 7.1 (448k • eng) • Default",
            ),
            (
                "Flac71.mkv",
                {
                    "enabled": True,
                    "source_track_id": 1,
                    "source_audio_index": 0,
                    "source_channels": 8,
                    "source_codec": "TrueHD",
                    "target_codec": "flac",
                    "target_channels": 8,
                    "bitrate": None,
                    "track_title": "FLAC 7.1",
                    "language": "eng",
                    "set_as_default": True,
                },
                "+ FLAC 7.1 (Lossless • eng) • Default",
            ),
            (
                "Eac3768.mkv",
                {
                    "enabled": True,
                    "source_track_id": 1,
                    "source_audio_index": 0,
                    "source_channels": 8,
                    "source_codec": "TrueHD",
                    "target_codec": "eac3",
                    "target_channels": 6,
                    "bitrate": "768k",
                    "track_title": "EAC3 5.1",
                    "language": "eng",
                    "set_as_default": True,
                },
                "+ EAC3 5.1 (768k • eng) • Default",
            ),
            (
                "AacDownmix20.mkv",
                {
                    "enabled": True,
                    "source_track_id": 1,
                    "source_audio_index": 0,
                    "source_channels": 6,
                    "source_codec": "AAC",
                    "target_codec": "aac",
                    "target_channels": 2,
                    "bitrate": "256k",
                    "track_title": "AAC 2.0",
                    "language": "eng",
                    "set_as_default": False,
                },
                "+ AAC 2.0 (256k • eng)",
            ),
        ],
    )
    async def test_accepts_valid_phase14_conversion_jobs(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        test_settings: Settings,
        filename: str,
        audio_cfg: dict,
        expected_change: str,
    ) -> None:
        file_id = _seed_media_file(api_app, test_settings, f"movies/{filename}")
        resp = await api_client.post(
            "/api/jobs", json=_make_job_request(file_id, audio_cfg)
        )
        assert resp.status_code == AppConstants.HTTP_ACCEPTED, resp.text
        created = resp.json()
        labels = [c["label"] for c in created["changes"]]
        assert expected_change in labels

    @pytest.mark.parametrize(
        ("audio_cfg", "expected_status"),
        [
            # AC-3 rejects 768k (max 640k)
            (
                {
                    "enabled": True,
                    "source_channels": 6,
                    "source_codec": "AAC",
                    "target_codec": "ac3",
                    "target_channels": 6,
                    "bitrate": "768k",
                },
                422,
            ),
            # E-AC-3 rejects 8 channels (hard 5.1 cap)
            (
                {
                    "enabled": True,
                    "source_channels": 8,
                    "source_codec": "TrueHD",
                    "target_codec": "eac3",
                    "target_channels": 8,
                    "bitrate": "640k",
                },
                422,
            ),
            # Upmixing 2.0 -> 5.1 rejected
            (
                {
                    "enabled": True,
                    "source_channels": 2,
                    "source_codec": "AAC",
                    "target_codec": "opus",
                    "target_channels": 6,
                    "bitrate": "384k",
                },
                422,
            ),
            # Lossy AAC -> FLAC rejected synchronously with 400
            (
                {
                    "enabled": True,
                    "source_channels": 6,
                    "source_codec": "AAC",
                    "target_codec": "flac",
                    "target_channels": 6,
                    "bitrate": None,
                },
                AppConstants.HTTP_BAD_REQUEST,
            ),
        ],
    )
    async def test_rejects_invalid_phase14_conversion_jobs(
        self,
        api_client: httpx.AsyncClient,
        api_app,
        test_settings: Settings,
        audio_cfg: dict,
        expected_status: int,
    ) -> None:
        file_id = _seed_media_file(api_app, test_settings, "movies/BadCase.mkv")
        resp = await api_client.post(
            "/api/jobs", json=_make_job_request(file_id, audio_cfg)
        )
        assert resp.status_code == expected_status, resp.text

    def test_ui_modal_to_pydantic_to_ffmpeg_argv_translation_fidelity(
        self, tmp_path: Path
    ) -> None:
        """End-to-end translation check: real UI clicks -> buildJobRequestPayload -> JobCreateRequest -> FFmpeg argv."""
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
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
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}

        const app = ctx.mkvForgeApp();
        app.editorFile = {{
          id: 99,
          filepath: "movies/MultiTrack.mkv",
          filename: "MultiTrack.mkv",
          container_title: "",
          has_global_tags: false,
          tracks: [
            {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
            {{ id: 1, type: "audio", codec: "TrueHD", audio_channels: 8, language: "eng", default_track: true, audio_index: 0 }},
            {{ id: 2, type: "subtitles", codec: "SRT", language: "eng", default_track: false }},
            {{ id: 3, type: "audio", codec: "AC-3", audio_channels: 6, language: "spa", default_track: false, audio_index: 1 }},
          ],
          attachments: [],
        }};
        app.editor = ctx.MKVForgeEditor.createEditorState(app.editorFile);

        // Scenario 1: User selects Track #3 (AC-3 5.1, audio_index=1) -> Opus -> Downmix 2.0 -> 192k
        app.openCustomAddAudio();
        app.addAudioDraft.sourceTrackId = 3;
        app.onDraftBaseAudioChange();
        app.onDraftTargetCodecChange("opus");
        app.onDraftChannelsChange(2);
        app.onDraftBitrateChange("192k");
        app.confirmAddAudio();
        const p1 = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile);

        // Scenario 2: User selects Track #1 (TrueHD 7.1, audio_index=0) -> FLAC -> Match Source
        app.openCustomAddAudio();
        app.addAudioDraft.sourceTrackId = 1;
        app.onDraftBaseAudioChange();
        app.onDraftTargetCodecChange("flac");
        app.confirmAddAudio();
        const p2 = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile);

        // Scenario 3: User selects Track #1 (TrueHD 7.1, audio_index=0) -> E-AC-3 -> Match (5.1 Max) -> 768k
        app.openCustomAddAudio();
        app.addAudioDraft.sourceTrackId = 1;
        app.onDraftBaseAudioChange();
        app.onDraftTargetCodecChange("eac3");
        app.onDraftBitrateChange("768k");
        app.confirmAddAudio();
        const p3 = ctx.MKVForgeEditor.buildJobRequestPayload(app.editor, app.editorFile);

        console.log(JSON.stringify({{ p1, p2, p3 }}));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        payloads = json.loads(proc.stdout.strip())

        # Verify Scenario 1: AC-3 track (audio_index=1) -> Opus 2.0 @ 192k with -drc_scale 0 before -i
        req1 = JobCreateRequest.model_validate(payloads["p1"])
        ac1 = req1.config.audio_conversion
        cmd1 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / f"in.mkv.mkvforge.{AppConstants.TEMP_AUDIO_EXT_MAP[ac1.target_codec]}",
            audio_index=ac1.source_audio_index,
            target_codec=ac1.target_codec,
            bitrate=ac1.bitrate,
            channels=6,
            requested_channels=compute_target_channels(6, ac1.target_codec, ac1.target_channels),
            sample_rate=48_000,
            source_codec=ac1.source_codec,
        )
        assert cmd1[cmd1.index("-map") + 1] == "0:a:1"
        assert cmd1[cmd1.index("-c:a") + 1] == "libopus"
        assert cmd1[cmd1.index("-b:a") + 1] == "192k"
        assert cmd1[cmd1.index("-ac") + 1] == "2"
        assert cmd1.index("-drc_scale") < cmd1.index("-i")
        assert cmd1[-1].endswith(".mka")

        # Verify Scenario 2: TrueHD 7.1 (audio_index=0) -> FLAC 7.1 Match Source (no -b:a, preserves 96kHz)
        req2 = JobCreateRequest.model_validate(payloads["p2"])
        ac2 = req2.config.audio_conversion
        assert ac2.target_channels is None
        cmd2 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / f"in.mkv.mkvforge.{AppConstants.TEMP_AUDIO_EXT_MAP[ac2.target_codec]}",
            audio_index=ac2.source_audio_index,
            target_codec=ac2.target_codec,
            bitrate=ac2.bitrate,
            channels=8,
            requested_channels=compute_target_channels(8, ac2.target_codec, ac2.target_channels),
            sample_rate=96_000,
            source_codec=ac2.source_codec,
        )
        assert cmd2[cmd2.index("-map") + 1] == "0:a:0"
        assert cmd2[cmd2.index("-c:a") + 1] == "flac"
        assert "-b:a" not in cmd2
        assert "-ar" not in cmd2
        assert cmd2[cmd2.index("-ac") + 1] == "8"
        assert cmd2[cmd2.index("-channel_layout") + 1] == "7.1"
        assert cmd2[-1].endswith(".mka")

        # Verify Scenario 3: TrueHD 7.1 (audio_index=0) -> E-AC-3 5.1 (capped) @ 768k
        req3 = JobCreateRequest.model_validate(payloads["p3"])
        ac3 = req3.config.audio_conversion
        assert ac3.target_channels is None
        cmd3 = build_ffmpeg_command(
            tmp_path / "in.mkv",
            tmp_path / f"in.mkv.mkvforge.{AppConstants.TEMP_AUDIO_EXT_MAP[ac3.target_codec]}",
            audio_index=ac3.source_audio_index,
            target_codec=ac3.target_codec,
            bitrate=ac3.bitrate,
            channels=8,
            requested_channels=compute_target_channels(8, ac3.target_codec, ac3.target_channels),
            sample_rate=48_000,
            source_codec=ac3.source_codec,
        )
        assert cmd3[cmd3.index("-map") + 1] == "0:a:0"
        assert cmd3[cmd3.index("-c:a") + 1] == "eac3"
        assert cmd3[cmd3.index("-b:a") + 1] == "768k"
        assert cmd3[cmd3.index("-ac") + 1] == "6"
        assert cmd3[cmd3.index("-channel_layout") + 1] == "5.1"
        assert cmd3[-1].endswith(".eac3")

    def test_exhaustive_combinatorial_ui_to_ffmpeg_and_mkvmerge_contract(
        self, tmp_path: Path
    ) -> None:
        """Exhaustively enumerate ALL non-blocked UI combinations (13 source codecs x 9 channel layouts x all codecs/channels/bitrates) and verify 0 translation errors across Pydantic, preflight, FFmpeg argv, mkvmerge argv, and job summary."""
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        node_script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const ctx = {{
          URLSearchParams, setTimeout, clearTimeout,
          document: {{ addEventListener: () => {{}}, readyState: "complete" }},
          localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          sessionStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
          location: {{ hash: "#/files/1" }},
          history: {{ replaceState: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 1280, innerHeight: 800,
        }};
        ctx.window = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js","formatters.js","api.js","folder_tree.js","editor.js","pull_refresh.js","toast_stack.js","library_return.js","app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const Ed = ctx.MKVForgeEditor;
        const sourceCodecs = [
          "AAC", "E-AC-3", "AC-3", "TrueHD", "TrueHD Atmos",
          "DTS-HD Master Audio", "DTS-HD High Resolution Audio",
          "DTS", "FLAC", "Opus", "PCM", "ALAC", "MP3"
        ];
        const sourceChannels = [8, 7, 6, 5, 4, 3, 2, 1, null];
        const cases = [];
        for (const sc of sourceCodecs) {{
          for (const sch of sourceChannels) {{
            const app = ctx.mkvForgeApp();
            app.editorFile = {{
              id: 1, filepath: "movies/Test.mkv", filename: "Test.mkv",
              container_title: "", has_global_tags: false,
              tracks: [
                {{ id: 0, type: "video", codec: "HEVC", language: "und", default_track: true }},
                {{ id: 1, type: "audio", codec: sc, audio_channels: sch, language: "eng", default_track: true, audio_index: 0 }},
              ],
              attachments: [],
            }};
            app.editor = Ed.createEditorState(app.editorFile);
            app.openCustomAddAudio();
            for (const tc of app.editorDraftAvailableCodecs.map((c) => c.codec)) {{
              app.onDraftTargetCodecChange(tc);
              const chOpts = app.editorDraftChannelOptions.map((o) => ({{ value: o.value, resolved: o.resolvedChannels }}));
              for (const chOpt of chOpts) {{
                app.onDraftChannelsChange(chOpt.value);
                if (app.editorDraftBlockStatus.blocked) continue;
                const rateCfg = app.editorDraftBitrateConfig;
                const rates = rateCfg.options.length > 0 ? rateCfg.options : [null];
                for (const rate of rates) {{
                  if (rate !== null) app.onDraftBitrateChange(rate);
                  app.addAudioDraft.dropSourceTrack = (rate === rates[0]);
                  const expectedUi = {{
                    sourceChannels: sch,
                    targetCodec: tc,
                    resolvedCh: app.editorDraftResolvedChannels,
                    bitrate: app.addAudioDraft.bitrate,
                    dropSource: app.addAudioDraft.dropSourceTrack,
                  }};
                  app.confirmAddAudio();
                  cases.push({{ expectedUi, payload: Ed.buildJobRequestPayload(app.editor, app.editorFile) }});
                  app.editAddedAudio();
                }}
              }}
            }}
          }}
        }}
        console.log(JSON.stringify(cases));
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
        cases = json.loads(proc.stdout)
        assert len(cases) > 3000

        for item in cases:
            ui = item["expectedUi"]
            req = JobCreateRequest.model_validate(item["payload"])
            ac = req.config.audio_conversion
            validate_audio_conversion_pair(
                ac.source_codec,
                ac.target_codec,
                source_channels=ac.source_channels,
                target_channels=ac.target_channels,
            )
            assert (
                estimate_audio_size_bytes(
                    600_000_000,
                    ac.bitrate,
                    target_codec=ac.target_codec,
                    channels=ac.target_channels or ac.source_channels,
                )
                > 0
            )
            probe_channels_list = [1, 2, 6, 8] if ui["sourceChannels"] is None else [ui["sourceChannels"]]
            for probe_ch in probe_channels_list:
                resolved_ch = compute_target_channels(probe_ch, ac.target_codec, ac.target_channels)
                eff_br = resolve_effective_bitrate(ac.target_codec, ac.bitrate, resolved_ch)
                ext = AppConstants.TEMP_AUDIO_EXT_MAP[ac.target_codec]
                cmd = build_ffmpeg_command(
                    tmp_path / "in.mkv",
                    tmp_path / f"in.mkv.mkvforge.{ext}",
                    audio_index=ac.source_audio_index,
                    target_codec=ac.target_codec,
                    bitrate=eff_br,
                    channels=probe_ch,
                    requested_channels=resolved_ch,
                    sample_rate=48_000,
                    source_codec=ac.source_codec,
                )
                assert cmd[cmd.index("-c:a") + 1] == AppConstants.AUDIO_CODEC_FFMPEG_ENCODER_MAP[ui["targetCodec"]]
                assert int(cmd[cmd.index("-ac") + 1]) == resolved_ch
                if ui["sourceChannels"] is not None:
                    assert resolved_ch == ui["resolvedCh"]
                if ui["targetCodec"] == "flac":
                    assert "-b:a" not in cmd
                else:
                    assert cmd[cmd.index("-b:a") + 1] == eff_br
            mkv_cmd = build_mkvmerge_command(
                tmp_path / "in.mkv",
                tmp_path / "in.mkv.mkvforge.tmp",
                req.config,
                temp_audio_path=tmp_path / f"in.mkv.mkvforge.{AppConstants.TEMP_AUDIO_EXT_MAP[ac.target_codec]}",
            )
            if ui["dropSource"]:
                assert "--no-audio" in mkv_cmd
            _mode, changes = summarize_job_changes(req.config.model_dump_json(), filepath="movies/Test.mkv")
            assert len(changes) >= 1


@pytest.mark.skipif(not _HAS_FFMPEG_TOOLS, reason="ffmpeg/ffprobe not installed")
class TestPhase14RealFfmpegStage1:
    """Real FFmpeg Stage 1 verification for Opus 7.1, 24-bit FLAC 7.1, EAC3 768k, and AC3 DRC decode."""

    def _generate_synthetic_mkv(
        self,
        output_path: Path,
        *,
        audio_codec: str,
        channels: int,
        layout: str,
    ) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-filter_complex",
            f"[1:a]pan={layout}|c0=c0|c1=c0[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-ac",
            str(channels),
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"Synthetic MKV generation failed:\n{proc.stderr}"

    def _probe_audio_streams(self, path: Path) -> list[dict]:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_name,channels,sample_rate,bits_per_raw_sample",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout).get("streams", [])

    @pytest.mark.parametrize(
        ("src_codec", "src_ch", "src_layout", "target_codec", "target_ch", "bitrate", "expected_ffprobe_codec", "expected_out_ch"),
        [
            ("flac", 8, "7.1", "opus", 8, "448k", "opus", 8),
            ("pcm_s24le", 8, "7.1", "flac", 8, None, "flac", 8),
            ("flac", 8, "7.1", "eac3", 6, "768k", "eac3", 6),
            ("ac3", 6, "5.1", "aac", 2, "192k", "aac", 2),
            ("pcm_s16le", 4, "quad", "opus", None, "256k", "opus", 4),
            ("pcm_s16le", 5, "5.0", "opus", None, "256k", "opus", 5),
            ("aac", 2, "stereo", "eac3", None, "256k", "eac3", 2),
        ],
    )
    async def test_real_stage1_ffmpeg_transcode_outputs_verified_streams(
        self,
        tmp_path: Path,
        src_codec: str,
        src_ch: int,
        src_layout: str,
        target_codec: str,
        target_ch: int | None,
        bitrate: str | None,
        expected_ffprobe_codec: str,
        expected_out_ch: int,
    ) -> None:
        src = tmp_path / f"src_{src_codec}_{src_ch}_{target_codec}.mkv"
        self._generate_synthetic_mkv(
            src, audio_codec=src_codec, channels=src_ch, layout=src_layout
        )
        temp_ext = AppConstants.TEMP_AUDIO_EXT_MAP[target_codec]
        out_audio = tmp_path / f"src_{src_codec}_{src_ch}_{target_codec}.mkv.mkvforge.{temp_ext}"

        result = await run_ffmpeg_transcode(
            src,
            out_audio,
            audio_index=0,
            target_codec=target_codec,
            bitrate=bitrate,
            target_channels=target_ch,
            duration_us=1_000_000,
            expected_codec=src_codec,
            expected_channels=None if target_ch is None else src_ch,
        )
        assert out_audio.exists()
        assert result.channels == expected_out_ch
        streams = self._probe_audio_streams(out_audio)
        assert len(streams) == 1
        assert streams[0]["codec_name"] == expected_ffprobe_codec
        assert int(streams[0]["channels"]) == expected_out_ch
        if expected_ffprobe_codec == "opus":
            assert int(streams[0]["sample_rate"]) == 48_000
        if expected_ffprobe_codec == "flac":
            assert int(streams[0]["bits_per_raw_sample"]) == 24


@pytest.mark.skipif(not _HAS_MEDIA_TOOLS, reason="ffmpeg/ffprobe/mkvmerge not installed")
class TestPhase14RealPipelineTranscodeAndRemux:
    """Real FFmpeg + mkvmerge end-to-end verification for Opus 7.1, FLAC 7.1, EAC3 768k, and AC3 DRC decode."""

    def _generate_synthetic_mkv(
        self,
        output_path: Path,
        *,
        audio_codec: str,
        channels: int,
        layout: str,
    ) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration=1",
            "-filter_complex",
            f"[1:a]pan={layout}|c0=c0|c1=c0[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-ac",
            str(channels),
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"Synthetic MKV generation failed:\n{proc.stderr}"

    def _probe_audio_streams(self, path: Path) -> list[dict]:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_name,channels,sample_rate,bits_per_raw_sample",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout).get("streams", [])

    @pytest.mark.parametrize(
        ("src_codec", "src_ch", "src_layout", "target_codec", "target_ch", "bitrate", "expected_ffprobe_codec", "expected_out_ch"),
        [
            ("flac", 8, "7.1", "opus", 8, "448k", "opus", 8),
            ("pcm_s24le", 8, "7.1", "flac", 8, None, "flac", 8),
            ("flac", 8, "7.1", "eac3", 6, "768k", "eac3", 6),
            ("ac3", 6, "5.1", "aac", 2, "192k", "aac", 2),
        ],
    )
    async def test_real_two_stage_pipeline_produces_valid_mkv_tracks(
        self,
        test_settings: Settings,
        src_codec: str,
        src_ch: int,
        src_layout: str,
        target_codec: str,
        target_ch: int,
        bitrate: str | None,
        expected_ffprobe_codec: str,
        expected_out_ch: int,
    ) -> None:
        src = test_settings.storage_dir / f"src_{src_codec}_{target_codec}.mkv"
        self._generate_synthetic_mkv(
            src, audio_codec=src_codec, channels=src_ch, layout=src_layout
        )

        config = JobConfig(
            container=ContainerMetadataConfig(title="", strip_global_tags=True),
            audio_conversion=AudioConversionConfig(
                enabled=True,
                source_track_id=1,
                source_audio_index=0,
                source_channels=src_ch,
                source_codec=src_codec,
                target_codec=target_codec,  # type: ignore[arg-type]
                target_channels=target_ch,
                bitrate=bitrate,
                track_title=f"{target_codec.upper()} Test",
                language="eng",
                set_as_default=True,
                drop_source_track=False,
            ),
            tracks=[
                TrackEditConfig(id=0, type="video", enabled=True, default_track=True),
                TrackEditConfig(id=1, type="audio", enabled=True, default_track=False),
            ],
            output_mode="keep_both",
        )

        result = await execute_two_stage_pipeline(
            src,
            config,
            storage_root=test_settings.storage_dir,
            duration_us=1_000_000,
        )
        assert result.output_path.exists()
        streams = self._probe_audio_streams(result.output_path)
        assert len(streams) == 2
        # Track 0 is the newly muxed converted audio track
        assert streams[0]["codec_name"] == expected_ffprobe_codec
        assert int(streams[0]["channels"]) == expected_out_ch
        if expected_ffprobe_codec == "opus":
            assert int(streams[0]["sample_rate"]) == 48_000
        if expected_ffprobe_codec == "flac":
            assert int(streams[0]["bits_per_raw_sample"]) == 24
