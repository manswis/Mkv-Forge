"""Batch dry-run preview, batch job enqueueing, and reusable templates REST endpoints."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.jobs import _row_to_job_item
from app.config import AppConstants
from app.db.repositories import (
    JobRepository,
    MediaFileRepository,
    TemplateRepository,
)
from app.models.schemas import (
    BatchEnqueueRequest,
    BatchEnqueueResponse,
    BatchFilePlan,
    BatchPreviewRequest,
    BatchPreviewResponse,
    BatchRuleSpec,
    JobItemResponse,
    JobTemplateListResponse,
    JobTemplateResponse,
    JobTemplateUpsertRequest,
)
from app.services.batch_compiler import (
    build_batch_preview_response,
    compile_batch_plans,
)
from app.services.job_worker import DuplicateJobError, JobWorker
from app.services.user_settings import UserSettingsService
from app.utils.paths import PathSafetyError

router = APIRouter(tags=["batch-and-templates"])


def _effective_user_id(request: Request) -> int | None:
    current = getattr(request.state, "current_user", None)
    if current is None or getattr(current, "is_anonymous", True):
        return None
    return int(current.id)


def _row_to_template_response(row: dict[str, Any]) -> JobTemplateResponse:
    raw_rule = str(row.get("rule_json") or "{}")
    try:
        rule_spec = BatchRuleSpec.model_validate(json.loads(raw_rule))
    except Exception:
        rule_spec = BatchRuleSpec()
    tid = int(row["id"])
    return JobTemplateResponse(
        id=f"custom_{tid}",
        template_id=tid,
        name=str(row["name"]),
        description=str(row.get("description") or ""),
        is_builtin=False,
        rule=rule_spec,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


@router.get("/api/templates", response_model=JobTemplateListResponse)
async def list_templates(request: Request) -> JobTemplateListResponse:
    """Return all user-saved custom templates from SQLite (no hardcoded default templates)."""
    template_repo: TemplateRepository = request.app.state.template_repo
    custom_rows = await asyncio.to_thread(template_repo.list_templates)
    custom_items = [_row_to_template_response(row) for row in custom_rows]
    return JobTemplateListResponse(templates=custom_items)


@router.post(
    "/api/templates",
    response_model=JobTemplateResponse,
    status_code=AppConstants.HTTP_CREATED,
)
async def create_template(
    request: Request,
    payload: JobTemplateUpsertRequest,
) -> JobTemplateResponse:
    """Save a new reusable batch/editor template in SQLite."""
    template_repo: TemplateRepository = request.app.state.template_repo
    clean_name = payload.name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail="Template name must not be empty.",
        )

    try:
        row = await asyncio.to_thread(
            template_repo.create_template,
            name=clean_name,
            description=payload.description.strip(),
            rule_json=payload.rule.model_dump_json(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=f"A template named {clean_name!r} already exists.",
        ) from exc

    return _row_to_template_response(row)


@router.put("/api/templates/{template_id}", response_model=JobTemplateResponse)
async def update_template(
    request: Request,
    template_id: int,
    payload: JobTemplateUpsertRequest,
) -> JobTemplateResponse:
    """Update an existing user-saved template in SQLite."""
    template_repo: TemplateRepository = request.app.state.template_repo
    clean_name = payload.name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=AppConstants.HTTP_BAD_REQUEST,
            detail="Template name must not be empty.",
        )

    try:
        updated = await asyncio.to_thread(
            template_repo.update_template,
            template_id,
            name=clean_name,
            description=payload.description.strip(),
            rule_json=payload.rule.model_dump_json(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=f"A template named {clean_name!r} already exists.",
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Template {template_id} not found.",
        )
    return _row_to_template_response(updated)


@router.delete("/api/templates/{template_id}")
async def delete_template(
    request: Request,
    template_id: int,
) -> dict[str, Any]:
    """Delete a user-saved template by integer ID."""
    template_repo: TemplateRepository = request.app.state.template_repo
    deleted = await asyncio.to_thread(template_repo.delete_template, template_id)
    if not deleted:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail=f"Template {template_id} not found.",
        )
    return {"deleted": True, "template_id": template_id}


@router.post("/api/jobs/batch/preview", response_model=BatchPreviewResponse)
async def preview_batch_jobs(
    request: Request,
    payload: BatchPreviewRequest,
) -> BatchPreviewResponse:
    """Compile a declarative `BatchRuleSpec` against selected files or a folder (Dry-Run Preview)."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    job_repo: JobRepository = request.app.state.job_repo
    settings_service: UserSettingsService = request.app.state.user_settings_service
    storage_dir = request.app.state.settings.storage_dir
    uid = _effective_user_id(request)

    user_prefs = await asyncio.to_thread(settings_service.get, user_id=uid)

    if payload.file_ids and len(payload.file_ids) > 0:
        rows = await asyncio.to_thread(media_repo.list_by_ids, payload.file_ids)
        scope_label = f"{len(rows)} selected {'file' if len(rows) == 1 else 'files'}"
    else:
        raw_folder = (payload.folder or "/").strip()
        norm_folder = f"/{raw_folder.strip('/')}" if raw_folder.strip("/") else "/"
        rows = await asyncio.to_thread(
            media_repo.list_by_folder_scope,
            norm_folder,
            include_subfolders=bool(payload.include_subfolders),
        )
        suffix = " (including subfolders)" if payload.include_subfolders else ""
        scope_label = f"{len(rows)} {'file' if len(rows) == 1 else 'files'} in {norm_folder}{suffix}"

    active_filepaths = await asyncio.to_thread(job_repo.list_active_filepaths)

    return await asyncio.to_thread(
        build_batch_preview_response,
        rows,
        payload.rule,
        batch_scope_label=scope_label,
        active_job_filepaths=active_filepaths,
        storage_root=storage_dir,
        user_preferred_language=user_prefs.preferred_language,
        smart_fill_track_format=user_prefs.smart_fill_track_format,
        file_overrides=payload.file_overrides,
    )


@router.post(
    "/api/jobs/batch",
    response_model=BatchEnqueueResponse,
    status_code=AppConstants.HTTP_ACCEPTED,
)
async def enqueue_batch_jobs(
    request: Request,
    payload: BatchEnqueueRequest,
) -> BatchEnqueueResponse:
    """Compile and enqueue a batch of media files under a shared `batch_id`."""
    media_repo: MediaFileRepository = request.app.state.media_repo
    job_repo: JobRepository = request.app.state.job_repo
    job_worker: JobWorker = request.app.state.job_worker
    settings_service: UserSettingsService = request.app.state.user_settings_service
    uid = _effective_user_id(request)

    user_prefs = await asyncio.to_thread(settings_service.get, user_id=uid)
    rows = await asyncio.to_thread(media_repo.list_by_ids, payload.file_ids)
    if not rows:
        raise HTTPException(
            status_code=AppConstants.HTTP_NOT_FOUND,
            detail="None of the requested media files were found in the library.",
        )

    storage_dir = request.app.state.settings.storage_dir
    active_filepaths = await asyncio.to_thread(job_repo.list_active_filepaths)
    current_user = getattr(request.state, "current_user", None)
    created_by_user_id = (
        int(current_user.id)
        if current_user is not None and not getattr(current_user, "is_anonymous", True)
        else None
    )
    created_by_name = (
        str(current_user.display_name)
        if current_user is not None and getattr(current_user, "display_name", "")
        else AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME
    )

    # Compile all files in natural-sort order (preserving preview seq_index when rows are excluded)
    # and enforce Guardrail #3 collision checks on included items
    excluded_set = {int(fid) for fid in (payload.excluded_file_ids or [])}
    plans = await asyncio.to_thread(
        compile_batch_plans,
        rows,
        payload.rule,
        active_job_filepaths=active_filepaths,
        storage_root=storage_dir,
        user_preferred_language=user_prefs.preferred_language,
        smart_fill_track_format=user_prefs.smart_fill_track_format,
        file_overrides=payload.file_overrides,
        excluded_file_ids=excluded_set,
    )
    collisions = [p for p in plans if p.collision_error]
    if collisions:
        summary = "; ".join(
            f"{p.filename}: {p.collision_error}" for p in collisions[:3]
        )
        suffix = (
            f" (+{len(collisions) - 3} more)" if len(collisions) > 3 else ""
        )
        raise HTTPException(
            status_code=AppConstants.HTTP_CONFLICT,
            detail=f"Resolve output filename collisions before queuing: {summary}{suffix}",
        )

    batch_id = f"{AppConstants.BATCH_ID_PREFIX}{uuid.uuid4().hex[: AppConstants.BATCH_ID_HEX_LEN]}"
    enqueued_jobs: list[JobItemResponse] = []
    skipped_items: list[BatchFilePlan] = []

    for plan in plans:
        if plan.action != AppConstants.BATCH_PLAN_ACTION_ENQUEUE or plan.job_config is None:
            skipped_items.append(plan)
            continue

        try:
            job_row = await job_worker.enqueue_job(
                filepath=plan.filepath,
                config=plan.job_config,
                created_by_user_id=created_by_user_id,
                created_by_name=created_by_name,
                batch_id=batch_id,
            )
            active_filepaths.add(plan.filepath)
            enqueued_jobs.append(_row_to_job_item(job_row))
        except (DuplicateJobError, PathSafetyError):
            skipped_items.append(
                plan.model_copy(
                    update={
                        "action": AppConstants.BATCH_PLAN_ACTION_SKIP_ACTIVE_JOB,
                        "stage_plan": AppConstants.BATCH_STAGE_NONE,
                        "reason": "Skip (Already in Queue)",
                        "job_config": None,
                    }
                )
            )

    return BatchEnqueueResponse(
        batch_id=batch_id,
        enqueued_count=len(enqueued_jobs),
        skipped_count=len(skipped_items),
        enqueued_jobs=enqueued_jobs,
        skipped_items=skipped_items,
    )
