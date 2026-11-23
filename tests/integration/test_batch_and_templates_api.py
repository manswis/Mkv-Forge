"""Integration tests for Batch Preview, Batch Enqueue, and Reusable Templates APIs (Phase 18)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.repositories import MediaFileUpsert, utc_now_iso
from app.main import create_app
from app.models.schemas import BatchRuleSpec


def _make_settings(tmp_path: Path) -> Settings:
    storage_dir = tmp_path / "storage"
    data_dir = tmp_path / "data"
    static_dir = Path(__file__).resolve().parents[2] / "app" / "static"
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        storage_dir=storage_dir,
        data_dir=data_dir,
        static_dir=static_dir,
        health_timeout_sec=5.0,
        scan_timeout_sec=10.0,
        log_level="INFO",
        watch_mode="off",
    )


def _seed_episode(
    app: Any,
    storage_dir: Path,
    *,
    rel_filepath: str,
    folder: str,
    filename: str,
    audio_codec: str = "AAC",
    audio_channels: int = 6,
    audio_lang: str = "eng",
    container_title: str = "",
) -> int:
    disk_path = storage_dir / rel_filepath
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 2048)

    raw_probe = {
        "container": {"properties": {"title": container_title} if container_title else {}},
        "tracks": [
            {
                "id": 0,
                "type": "video",
                "codec": "HEVC",
                "properties": {
                    "language": "und",
                    "default_track": True,
                    "forced_track": False,
                    "pixel_dimensions": "1920x1080",
                },
            },
            {
                "id": 1,
                "type": "audio",
                "codec": audio_codec,
                "properties": {
                    "language": audio_lang,
                    "default_track": True,
                    "forced_track": False,
                    "audio_channels": audio_channels,
                },
            },
        ],
        "global_tags": [],
        "track_tags": [],
        "attachments": [],
        "chapters": [],
    }
    is_aac51 = audio_codec.upper() == "AAC" and audio_channels == 6
    is_dolby = audio_codec.upper() in ("AC-3", "E-AC-3", "AC3", "EAC3")
    summary = [
        {
            "codec": "EAC3" if audio_codec == "E-AC-3" else ("AC3" if audio_codec == "AC-3" else audio_codec),
            "channels": audio_channels,
            "channel_layout": "5.1" if audio_channels == 6 else "2.0",
            "language": audio_lang,
        }
    ]
    return app.state.media_repo.upsert(
        MediaFileUpsert(
            filepath=rel_filepath,
            folder=folder,
            filename=filename,
            size_bytes=2048,
            mtime=1700000000.0,
            duration_us=60_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json=json.dumps(summary),
            subtitle_count=0,
            container_title=container_title,
            has_global_tags=False,
            has_aac_51=is_aac51,
            has_ac3_eac3=is_dolby,
            raw_json=json.dumps(raw_probe),
            scanned_at=utc_now_iso(),
        )
    )


class TestBatchAndTemplatesApi:
    """End-to-end REST tests for `/api/templates`, `/api/jobs/batch/preview`, and `/api/jobs/batch`."""

    def test_templates_crud_lifecycle(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            # 1. GET /api/templates returns empty list initially (no hardcoded built-in defaults)
            res = client.get("/api/templates")
            assert res.status_code == 200
            body = res.json()
            assert body["templates"] == []

            # 2. POST /api/templates creates a custom saved template
            rule = BatchRuleSpec().model_dump()
            create_res = client.post(
                "/api/templates",
                json={
                    "name": "Anime Season Cleanup",
                    "description": "EAC3 5.1 + clean track names",
                    "rule": rule,
                },
            )
            assert create_res.status_code == 201
            created = create_res.json()
            assert created["is_builtin"] is False
            assert created["name"] == "Anime Season Cleanup"
            tid = created["template_id"]
            assert isinstance(tid, int)

            # Duplicate name returns 409 Conflict
            dup_res = client.post(
                "/api/templates",
                json={"name": "Anime Season Cleanup", "description": "dup", "rule": rule},
            )
            assert dup_res.status_code == 409

            # 3. PUT /api/templates/{id} updates the template
            upd_res = client.put(
                f"/api/templates/{tid}",
                json={
                    "name": "Anime Season v2",
                    "description": "Updated description",
                    "rule": rule,
                },
            )
            assert upd_res.status_code == 200
            assert upd_res.json()["name"] == "Anime Season v2"

            # 4. DELETE /api/templates/{id} removes it
            del_res = client.delete(f"/api/templates/{tid}")
            assert del_res.status_code == 200
            assert del_res.json()["deleted"] is True

            # Deleting again returns 404
            assert client.delete(f"/api/templates/{tid}").status_code == 404

    def test_batch_preview_and_enqueue_mixed_season_with_active_job_guard(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            # Stop background worker loop so queued jobs stay in 'queued' status for deterministic inspection
            app.state.job_worker._stop_event.set()

            id1 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Season 1/S01E01.mkv",
                folder="/Season 1",
                filename="S01E01.mkv",
                audio_codec="AAC",
                audio_channels=6,
                container_title="Show.S01E01.WEB-DL",
            )
            id2 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Season 1/S01E02.mkv",
                folder="/Season 1",
                filename="S01E02.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                container_title="Show.S01E02.WEB-DL",
            )
            id3 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Season 1/S01E03.mkv",
                folder="/Season 1",
                filename="S01E03.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                container_title="",
            )
            id4 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Season 1/S01E04.mkv",
                folder="/Season 1",
                filename="S01E04.mkv",
                audio_codec="AAC",
                audio_channels=6,
                container_title="Already.Queued",
            )

            # Put Episode 4 into the job queue first to verify Guardrail #3
            app.state.job_repo.create_job(
                job_id="existing-job-4",
                filepath="Season 1/S01E04.mkv",
                config_json="{}",
            )

            # Preview by folder scope (Guardrail #4)
            prev_res = client.post(
                "/api/jobs/batch/preview",
                json={
                    "folder": "/Season 1",
                    "include_subfolders": False,
                    "rule": {},
                },
            )
            assert prev_res.status_code == 200
            preview = prev_res.json()
            assert preview["inventory"]["total_files"] == 4
            assert preview["inventory"]["aac_51_count"] == 2
            assert preview["inventory"]["dolby_count"] == 2
            assert preview["enqueue_count"] == 2
            assert preview["transcode_count"] == 1  # E01
            assert preview["remux_only_count"] == 1  # E02
            assert preview["skip_compliant_count"] == 1  # E03
            assert preview["skip_active_job_count"] == 1  # E04
            assert preview["disk_space_ok"] is True

            # Enqueue the batch for all 4 file IDs -> only E01 and E02 should be enqueued
            enq_res = client.post(
                "/api/jobs/batch",
                json={
                    "file_ids": [id1, id2, id3, id4],
                    "rule": {},
                },
            )
            assert enq_res.status_code == 202
            enq = enq_res.json()
            assert enq["batch_id"].startswith("batch_")
            assert enq["enqueued_count"] == 2
            assert enq["skipped_count"] == 2
            enqueued_paths = [j["filepath"] for j in enq["enqueued_jobs"]]
            assert enqueued_paths == ["Season 1/S01E01.mkv", "Season 1/S01E02.mkv"]
            assert all(j["batch_id"] == enq["batch_id"] for j in enq["enqueued_jobs"])

    def test_batch_preview_and_enqueue_honor_file_overrides_and_majority_lang(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.job_worker._stop_event.set()

            id1 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Anime/E01.mkv",
                folder="/Anime",
                filename="E01.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                audio_lang="jpn",
                container_title="",
            )
            id2 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Anime/E02.mkv",
                folder="/Anime",
                filename="E02.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                audio_lang="jpn",
                container_title="",
            )
            id3 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Anime/E03.mkv",
                folder="/Anime",
                filename="E03.mkv",
                audio_codec="AAC",
                audio_channels=6,
                audio_lang="und",
                container_title="Messy.Release",
            )

            # 1. Preview detects majority audio language = 'jpn' and returns selectable chips on compliant E01/E02
            prev = client.post(
                "/api/jobs/batch/preview",
                json={"file_ids": [id1, id2, id3], "rule": {}},
            )
            assert prev.status_code == 200
            body = prev.json()
            assert body["inventory"]["detected_majority_audio_language"] == "jpn"
            items_by_id = {it["file_id"]: it for it in body["items"]}
            assert items_by_id[id1]["action"] == "skip_already_compliant"
            assert any(c["chip_id"] == "audio_track:1" for c in items_by_id[id1]["chips"])
            assert any(c["chip_id"] == "tag:smart_fill" for c in items_by_id[id1]["chips"])

            # 2. Override E01 (opt-in smart_fill_track_names) & E03 (opt-out audio conversion)
            overrides = {
                str(id1): {"smart_fill_track_names": True},
                str(id3): {"audio_convert_enabled": False},
            }
            prev2 = client.post(
                "/api/jobs/batch/preview",
                json={"file_ids": [id1, id2, id3], "rule": {}, "file_overrides": overrides},
            )
            assert prev2.status_code == 200
            body2 = prev2.json()
            assert body2["enqueue_count"] == 2
            assert body2["transcode_count"] == 0
            assert body2["remux_only_count"] == 2

            # 3. Enqueue with same file_overrides -> stored JobConfig reflects both overrides accurately
            enq = client.post(
                "/api/jobs/batch",
                json={"file_ids": [id1, id3], "rule": {}, "file_overrides": overrides},
            )
            assert enq.status_code == 202
            enq_body = enq.json()
            assert enq_body["enqueued_count"] == 2
            jobs_by_path = {
                j["filepath"]: app.state.job_repo.get_by_id(j["id"])
                for j in enq_body["enqueued_jobs"]
            }
            e3_cfg = json.loads(jobs_by_path["Anime/E03.mkv"]["config_json"])
            assert e3_cfg["audio_conversion"]["enabled"] is False
            assert e3_cfg["container"]["title"] == ""

    def test_batch_rename_save_as_and_409_collision_enforcement(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.job_worker._stop_event.set()

            id10 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Show/Ep10.mkv",
                folder="/Show",
                filename="Ep10.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                container_title="",
            )
            id2 = _seed_episode(
                app,
                settings.storage_dir,
                rel_filepath="Show/Ep2.mkv",
                folder="/Show",
                filename="Ep2.mkv",
                audio_codec="E-AC-3",
                audio_channels=6,
                container_title="",
            )

            # Guardrail #3: Intra-batch duplicate target returns 409 Conflict on enqueue!
            dup_payload = {
                "file_ids": [id10, id2],
                "rule": {"audio": {"enabled": False}, "output_mode": "save_as", "destination_folder": "/Out"},
                "file_overrides": {
                    str(id10): {"output_filename": "Same.mkv"},
                    str(id2): {"output_filename": "Same.mkv"},
                },
            }
            prev_dup = client.post("/api/jobs/batch/preview", json=dup_payload)
            assert prev_dup.status_code == 200
            assert prev_dup.json()["collision_count"] == 2

            enq_dup = client.post("/api/jobs/batch", json=dup_payload)
            assert enq_dup.status_code == 409
            assert "collisions" in enq_dup.json()["detail"].lower()

            # Valid batch rename with Replace Original -> atomic save_as + delete_original=True in natural order!
            ok_payload = {
                "file_ids": [id10, id2],
                "rule": {
                    "audio": {"enabled": False},
                    "rename": {"enabled": True, "pattern": "Clean_S1_", "start_number": 1},
                    "output_mode": "replace",
                },
            }
            enq_ok = client.post("/api/jobs/batch", json=ok_payload)
            assert enq_ok.status_code == 202
            enq_body = enq_ok.json()
            assert enq_body["enqueued_count"] == 2
            # Natural sort order: Ep2 before Ep10!
            assert [j["filepath"] for j in enq_body["enqueued_jobs"]] == [
                "Show/Ep2.mkv",
                "Show/Ep10.mkv",
            ]
            ep2_row = app.state.job_repo.get_by_id(enq_body["enqueued_jobs"][0]["id"])
            ep2_cfg = json.loads(ep2_row["config_json"])
            assert ep2_cfg["output_mode"] == "save_as"
            assert ep2_cfg["output_path"] == "Show/Clean_S1_02.mkv"
            assert ep2_cfg["delete_original"] is True

    def test_frontend_batch_studio_node_vm_guardrails(self) -> None:
        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        const apiCalls = [];
        let previewCallNum = 0;
        let savedTemplatePayload = null;
        let updatedTemplatePayload = null;
        let enqueuedPayload = null;

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
          innerWidth: 1440, innerHeight: 900,
          fetch: async (url, opts = {{}}) => {{
            const method = (opts.method || "GET").toUpperCase();
            const parsedBody = opts.body ? JSON.parse(opts.body) : null;
            apiCalls.push({{ method, url }});

            if (url === "/api/templates" && method === "GET") {{
              return {{ ok: true, status: 200, json: async () => ({{ templates: [] }}) }};
            }}
            if (url === "/api/templates" && method === "POST") {{
              savedTemplatePayload = parsedBody;
              return {{
                ok: true, status: 201,
                json: async () => ({{
                  id: "custom_1", template_id: 1, name: parsedBody.name,
                  description: parsedBody.description || "", is_builtin: false,
                  rule: parsedBody.rule,
                }}),
              }};
            }}
            if (url === "/api/templates/1" && method === "PUT") {{
              updatedTemplatePayload = parsedBody;
              return {{
                ok: true, status: 200,
                json: async () => ({{
                  id: "custom_1", template_id: 1, name: parsedBody.name,
                  description: parsedBody.description || "", is_builtin: false,
                  rule: parsedBody.rule,
                }}),
              }};
            }}
            if (url === "/api/jobs/batch/preview" && method === "POST") {{
              const n = ++previewCallNum;
              // Call 1: initial open with all-und batch (verify 'und' auto-selected, never 'eng')
              if (n === 1) {{
                return {{
                  ok: true, status: 200,
                  json: async () => ({{
                    batch_scope_label: "2 selected files",
                    inventory: {{ total_files: 2, total_bytes: 2000, aac_51_count: 1, dolby_count: 1, other_audio_count: 0, has_metadata_count: 0, untagged_audio_count: 2, detected_majority_audio_language: "und", detected_audio_languages: [] }},
                    items: [
                      {{
                        file_id: 1, filepath: "A.mkv", filename: "A.mkv", folder: "/", size_bytes: 1000,
                        action: "enqueue", stage_plan: "transcode_and_remux", reason: "Convert",
                        chips: [
                          {{ chip_id: "audio_track:1", category: "audio", label: "#1 AAC 5.1", selected: true, default_selected: true, overridden: false, override_key: "track_enabled", track_id: 1, disabled: true, disabled_reason: "Only audio" }},
                          {{ chip_id: "sub_track:2", category: "subtitle", label: "#2 SRT · fra", selected: true, default_selected: true, overridden: false, override_key: "track_enabled", track_id: 2, disabled: false }},
                        ],
                        has_overrides: false, warnings: [],
                      }},
                    ],
                    enqueue_count: 1, transcode_count: 1, remux_only_count: 0, skip_compliant_count: 0, skip_active_job_count: 0, skip_other_count: 0,
                    disk_free_bytes: 100000, disk_required_bytes: 2000, disk_space_ok: true, disk_space_warning: null,
                  }}),
                }};
              }}
              // Call 2 (slow, stale) vs Call 3 (fast, fresh) -> monotonic seq counter test!
              if (n === 2) {{
                await new Promise((r) => setTimeout(r, 45));
                return {{
                  ok: true, status: 200,
                  json: async () => ({{
                    batch_scope_label: "STALE_RESPONSE",
                    inventory: {{ total_files: 1, total_bytes: 1000, detected_majority_audio_language: "jpn", detected_audio_languages: ["jpn"] }},
                    items: [], enqueue_count: 0, transcode_count: 0, remux_only_count: 0, skip_compliant_count: 0, skip_active_job_count: 0, skip_other_count: 0,
                    disk_free_bytes: 100000, disk_required_bytes: 0, disk_space_ok: true, disk_space_warning: null,
                  }}),
                }};
              }}
              await new Promise((r) => setTimeout(r, 5));
              return {{
                ok: true, status: 200,
                json: async () => ({{
                  batch_scope_label: "FRESH_RESPONSE",
                  inventory: {{ total_files: 1, total_bytes: 1000, detected_majority_audio_language: "jpn", detected_audio_languages: ["jpn"] }},
                  items: [
                    {{
                      file_id: 1, filepath: "A.mkv", filename: "A.mkv", folder: "/", size_bytes: 1000,
                      action: "enqueue", stage_plan: "remux_only", reason: "Fast Remux Only",
                      chips: [
                        {{ chip_id: "sub_track:2", category: "subtitle", label: "#2 SRT · fra", selected: false, default_selected: true, overridden: true, override_key: "track_enabled", track_id: 2, disabled: false }},
                      ],
                      has_overrides: true, warnings: [],
                    }},
                  ],
                  enqueue_count: 1, transcode_count: 0, remux_only_count: 1, skip_compliant_count: 0, skip_active_job_count: 0, skip_other_count: 0,
                  disk_free_bytes: 100000, disk_required_bytes: 1000, disk_space_ok: true, disk_space_warning: null,
                }}),
              }};
            }}
            if (url === "/api/jobs/batch" && method === "POST") {{
              enqueuedPayload = parsedBody;
              return {{ ok: true, status: 202, json: async () => ({{ batch_id: "batch_1", enqueued_count: 1, skipped_count: 0, enqueued_jobs: [], skipped_items: [] }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{ jobs: [], files: [], total: 0, stats: {{}} }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "toast_stack.js", "batch.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        const app = ctx.mkvForgeApp();
        app.selectedFileIds = [1];

        (async () => {{
          await app.openBatchModalForSelection();
          const initialSelectedTemplate = app.batchModal.selectedTemplateId;
          const initialRetagLang = app.batchModal.rule.track_filter.retag_und_target_language;
          const undOptionLabel = app.batchRetagLanguageOptions[0].label;

          // Disabled chip cannot create an override
          const disabledChip = app.batchModal.preview.items[0].chips[0];
          const subChip = app.batchModal.preview.items[0].chips[1];
          const overridesAfterDisabled = ctx.MKVForgeBatch.toggleFileChipOverride({{}}, 1, disabledChip);

          // Rapid out-of-order preview requests: call 2 (slow) then call 3 (fast)
          const p2 = app.refreshBatchPreview();
          app.toggleBatchFileChip(1, subChip); // triggers call 3 immediately
          await p2;
          await new Promise((r) => setTimeout(r, 70));
          const labelAfterOutOfOrder = app.batchModal.preview.batch_scope_label;
          const overridesBeforeQueue = app.batchModal.fileOverrides;

          // Save a new template, then modify bitrate -> dirty=true -> Queue auto-updates template via PUT!
          app.batchModal.saveTemplateName = "My Custom Preset";
          await app.submitSaveBatchTemplate();
          // Mock batchTemplates list containing the saved template (since GET /api/templates mock returned [])
          app.batchTemplates = [{{
            id: "custom_1", template_id: 1, name: "My Custom Preset",
            description: "", is_builtin: false, rule: savedTemplatePayload.rule,
          }}];
          const dirtyBeforeEdit = app.isBatchTemplateDirty;
          app.batchModal.rule.audio.bitrate = "448k";
          const dirtyAfterEdit = app.isBatchTemplateDirty;
          const updateCheckedByDefault = app.batchModal.updateTemplateOnQueue;

          await app.submitBatchEnqueue();

          // Verify target-aware Apply to options and normalization across Dolby, Opus, AAC, FLAC
          const applyToEac3 = ctx.MKVForgeBatch.getAudioApplyToOptions("eac3");
          const applyToOpus = ctx.MKVForgeBatch.getAudioApplyToOptions("opus");
          const applyToAac = ctx.MKVForgeBatch.getAudioApplyToOptions("aac");
          const applyToFlac = ctx.MKVForgeBatch.getAudioApplyToOptions("flac");
          const normRule = {{ target_codec: "opus", match_condition: "incompatible_or_non_dolby", accept_existing_dolby: true, bitrate: "640k" }};
          ctx.MKVForgeBatch.normalizeAudioRuleForTargetCodec(normRule);
          const opusBitrates = ctx.MKVForgeBatch.getBitrateOptionsForCodec("opus");
          const defaultRule = ctx.MKVForgeBatch.createDefaultBatchRule();
          const defaultSmartFill = defaultRule.container.smart_fill_track_names;
          const defaultStripAttachments = defaultRule.container.strip_attachments;
          const clonedSmartFillFalse = ctx.MKVForgeBatch.cloneBatchRule({{ container: {{ smart_fill_track_names: false }} }}).container.smart_fill_track_names;
          const clonedStripAttFalse = ctx.MKVForgeBatch.cloneBatchRule({{ container: {{ strip_attachments: false }} }}).container.strip_attachments;
          const clonedStripAttOmitted = ctx.MKVForgeBatch.cloneBatchRule({{ container: {{}} }}).container.strip_attachments;
          const metaSummaryDefault = ctx.MKVForgeBatch.formatMetadataTabSummary(defaultRule);
          const metaSummaryNoAtt = ctx.MKVForgeBatch.formatMetadataTabSummary({{
            ...defaultRule,
            container: {{ ...defaultRule.container, strip_attachments: false }},
          }});
          const metaSummaryNone = ctx.MKVForgeBatch.formatMetadataTabSummary({{
            container: {{
              title_action: "keep",
              strip_global_tags: false,
              strip_track_tags: false,
              smart_fill_track_names: false,
              strip_attachments: false,
            }},
            track_filter: {{ retag_und_audio_to_preferred: false, keep_subtitle_mode: "all" }},
          }});

          // Verify selectBatchTab positive + negative/edge cases
          app.batchModal = {{ activeTab: "audio" }};
          let scrolledOpts = null;
          app.selectBatchTab("metadata", {{ scrollIntoView: (opts) => {{ scrolledOpts = opts; }} }});
          const tabAfterValid = app.batchModal.activeTab;
          app.selectBatchTab("invalid_tab", {{ scrollIntoView: () => {{ scrolledOpts = "WRONG"; }} }});
          const tabAfterInvalid = app.batchModal.activeTab;
          app.selectBatchTab(null, null);
          const tabAfterNull = app.batchModal.activeTab;
          app.selectBatchTab("output", null);
          const tabAfterNullEl = app.batchModal.activeTab;
          app.batchModal = null;
          app.selectBatchTab("audio", null); // must not throw when batchModal is null

          const outOv = ctx.MKVForgeBatch.setFileOutputFilenameOverride({{}}, 1, "Custom_01.mkv");
          const outTabSummary = ctx.MKVForgeBatch.formatOutputTabSummary({{
            output_mode: "save_as",
            rename: {{ enabled: true, pattern: "Show_S1_" }},
          }});

          console.log(JSON.stringify({{
            initialSelectedTemplate,
            initialRetagLang,
            undOptionLabel,
            overridesAfterDisabled,
            labelAfterOutOfOrder,
            overridesBeforeQueue,
            dirtyBeforeEdit,
            dirtyAfterEdit,
            updateCheckedByDefault,
            updatedTemplateBitrate: updatedTemplatePayload ? updatedTemplatePayload.rule.audio.bitrate : null,
            enqueuedOverrides: enqueuedPayload ? enqueuedPayload.file_overrides : null,
            putBeforeBatchEnqueue:
              apiCalls.findIndex((c) => c.method === "PUT" && c.url === "/api/templates/1") <
              apiCalls.findIndex((c) => c.method === "POST" && c.url === "/api/jobs/batch"),
            applyToEac3Len: applyToEac3.length,
            applyToOpusLen: applyToOpus.length,
            applyToAacLen: applyToAac.length,
            applyToFlacLen: applyToFlac.length,
            normOpusMatch: normRule.match_condition,
            normOpusAcceptDolby: normRule.accept_existing_dolby,
            normOpusBitrate: normRule.bitrate,
            opusAllows640k: opusBitrates.includes("640k"),
            outOv,
            outTabSummary,
            defaultSmartFill,
            defaultStripAttachments,
            clonedSmartFillFalse,
            clonedStripAttFalse,
            clonedStripAttOmitted,
            metaSummaryDefault,
            metaSummaryNoAtt,
            metaSummaryNone,
            tabAfterValid,
            tabAfterInvalid,
            tabAfterNull,
            tabAfterNullEl,
            scrolledOpts,
          }}));
        }})().catch((err) => {{
          console.error(err);
          process.exit(1);
        }});
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["initialSelectedTemplate"] == ""
        assert out["initialRetagLang"] == "und"
        assert out["undOptionLabel"] == "Keep untagged (und)"
        assert out["overridesAfterDisabled"] == {}
        assert out["labelAfterOutOfOrder"] == "FRESH_RESPONSE"
        assert out["overridesBeforeQueue"] == {"1": {"track_enabled": {"2": False}}}
        assert out["dirtyBeforeEdit"] is False
        assert out["dirtyAfterEdit"] is True
        assert out["updateCheckedByDefault"] is True
        assert out["updatedTemplateBitrate"] == "448k"
        assert out["enqueuedOverrides"] == {"1": {"track_enabled": {"2": False}}}
        assert out["putBeforeBatchEnqueue"] is True
        assert out["applyToEac3Len"] == 3
        assert out["applyToOpusLen"] == 2
        assert out["applyToAacLen"] == 1
        assert out["applyToFlacLen"] == 0
        assert out["normOpusMatch"] == "any_non_target"
        assert out["normOpusAcceptDolby"] is False
        assert out["normOpusBitrate"] == "320k"
        assert out["opusAllows640k"] is False
        assert out["outOv"] == {"1": {"output_filename": "Custom_01.mkv"}}
        assert out["outTabSummary"] == "Save As · Rename"
        assert out["defaultSmartFill"] is True
        assert out["defaultStripAttachments"] is True
        assert out["clonedSmartFillFalse"] is False
        assert out["clonedStripAttFalse"] is False
        assert out["clonedStripAttOmitted"] is True
        assert out["metaSummaryDefault"] == "4 active"
        assert out["metaSummaryNoAtt"] == "3 active"
        assert out["metaSummaryNone"] == "Default"
        assert out["tabAfterValid"] == "metadata"
        assert out["tabAfterInvalid"] == "metadata"
        assert out["tabAfterNull"] == "metadata"
        assert out["tabAfterNullEl"] == "output"
        assert out["scrolledOpts"] == {
            "behavior": "smooth",
            "inline": "center",
            "block": "nearest",
        }

