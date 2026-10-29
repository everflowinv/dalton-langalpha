"""Database ledger queries for audio transcription jobs (`dalton_transcription_jobs`).

Every state transition uses a conditional `WHERE job_id = %s AND version = %s`
update so concurrent backend workers never overwrite each other's state changes,
and no worker relies on an in-process job dictionary.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import strip_pg_nul_str

logger = logging.getLogger(__name__)


class TranscriptionJobStatus(StrEnum):
    """Canonical state machine for Dalton transcription jobs (Backlog A1 §5)."""

    AWAITING_UPLOAD = "awaiting_upload"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROBING = "probing"
    SPLITTING = "splitting"
    SUBMITTING = "submitting"
    TRANSCRIBING = "transcribing"
    ASSEMBLING = "assembling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


ALL_TRANSCRIPTION_STATUSES: frozenset[str] = frozenset(
    s.value for s in TranscriptionJobStatus
)
TERMINAL_TRANSCRIPTION_STATUSES: frozenset[str] = frozenset(
    {
        TranscriptionJobStatus.SUCCEEDED.value,
        TranscriptionJobStatus.FAILED.value,
        TranscriptionJobStatus.CANCELLED.value,
    }
)
RETRYABLE_TRANSCRIPTION_STATUSES: frozenset[str] = frozenset(
    {
        TranscriptionJobStatus.FAILED.value,
        TranscriptionJobStatus.INDETERMINATE.value,
    }
)

_JOB_COLS = """
    job_id,
    created_by_user_id,
    workspace_id,
    thread_id,
    source_object_key,
    source_size_bytes,
    source_sha256,
    language,
    hotwords,
    status,
    stage,
    segment_count,
    completed_segments,
    provider_request_ids,
    usage,
    result_object_key,
    error_category,
    error_message,
    idempotency_key,
    version,
    created_at,
    updated_at,
    completed_at
"""


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif k in ("hotwords", "provider_request_ids") and isinstance(v, str):
            try:
                parsed = json.loads(v)
                out[k] = parsed if isinstance(parsed, list) else []
            except Exception:
                out[k] = []
        elif k == "usage" and isinstance(v, str):
            try:
                parsed = json.loads(v)
                out[k] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                out[k] = {}
        else:
            out[k] = v
    return out


def _normalize_status(status: str | TranscriptionJobStatus) -> str:
    val = status.value if isinstance(status, TranscriptionJobStatus) else str(status).strip()
    if val not in ALL_TRANSCRIPTION_STATUSES:
        raise ValueError(f"Invalid transcription job status: {status!r}")
    return val


async def create_transcription_job(
    *,
    created_by_user_id: str,
    workspace_id: str,
    thread_id: str | None,
    source_object_key: str,
    source_size_bytes: int,
    source_sha256: str,
    idempotency_key: str,
    language: str | None = None,
    hotwords: list[str] | None = None,
    status: str | TranscriptionJobStatus = TranscriptionJobStatus.AWAITING_UPLOAD,
    stage: str | None = None,
    job_id: str | UUID | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert a transcription job or return the existing row on idempotency_key conflict."""
    resolved_id = strip_pg_nul_str(str(job_id or uuid4()))
    clean_user = strip_pg_nul_str(created_by_user_id).strip()
    clean_ws = strip_pg_nul_str(workspace_id).strip()
    clean_thread = strip_pg_nul_str(thread_id or "").strip()
    clean_obj_key = strip_pg_nul_str(source_object_key).strip()
    clean_sha = strip_pg_nul_str(source_sha256).strip().lower()
    clean_idemp = strip_pg_nul_str(idempotency_key).strip()
    clean_lang = strip_pg_nul_str(language).strip() if language else None
    clean_status = _normalize_status(status)
    clean_stage = strip_pg_nul_str(stage or clean_status).strip()
    clean_hotwords = [
        strip_pg_nul_str(str(w)).strip()
        for w in (hotwords or [])
        if str(w).strip()
    ]
    size_val = int(source_size_bytes)
    if size_val < 0:
        raise ValueError("source_size_bytes must be >= 0")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_transcription_jobs (
                    job_id,
                    created_by_user_id,
                    workspace_id,
                    thread_id,
                    source_object_key,
                    source_size_bytes,
                    source_sha256,
                    language,
                    hotwords,
                    status,
                    stage,
                    segment_count,
                    completed_segments,
                    provider_request_ids,
                    usage,
                    result_object_key,
                    error_category,
                    error_message,
                    idempotency_key,
                    version
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s::jsonb, %s, %s,
                    0, 0, '[]'::jsonb, '{{}}'::jsonb,
                    NULL, NULL, NULL, %s, 1
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING {_JOB_COLS}
                """,
                (
                    resolved_id,
                    clean_user,
                    clean_ws,
                    clean_thread,
                    clean_obj_key,
                    size_val,
                    clean_sha,
                    clean_lang,
                    json.dumps(clean_hotwords, ensure_ascii=False),
                    clean_status,
                    clean_stage,
                    clean_idemp,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    f"""
                    SELECT {_JOB_COLS}
                    FROM dalton_transcription_jobs
                    WHERE idempotency_key = %s
                    """,
                    (clean_idemp,),
                )
                row = await cur.fetchone()

    serialized = _serialize_row(row)
    if serialized is not None:
        if (
            serialized.get("created_by_user_id") != clean_user
            or serialized.get("workspace_id") != clean_ws
        ):
            raise PermissionError(
                "Idempotency key belongs to a different user or workspace"
            )
        return serialized

    return {
        "job_id": resolved_id,
        "created_by_user_id": clean_user,
        "workspace_id": clean_ws,
        "thread_id": clean_thread,
        "source_object_key": clean_obj_key,
        "source_size_bytes": size_val,
        "source_sha256": clean_sha,
        "language": clean_lang,
        "hotwords": clean_hotwords,
        "status": clean_status,
        "stage": clean_stage,
        "segment_count": 0,
        "completed_segments": 0,
        "provider_request_ids": [],
        "usage": {},
        "result_object_key": None,
        "error_category": None,
        "error_message": None,
        "idempotency_key": clean_idemp,
        "version": 1,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
    }


async def get_transcription_job(
    job_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single transcription job by `job_id`."""
    cleaned_id = strip_pg_nul_str(str(job_id or "")).strip()
    if not cleaned_id:
        return None
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE job_id = %s
                """,
                (cleaned_id,),
            )
            row = await cur.fetchone()
    return _serialize_row(row)


async def get_transcription_job_by_idempotency_key(
    idempotency_key: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single transcription job by `idempotency_key`."""
    cleaned_key = strip_pg_nul_str(str(idempotency_key or "")).strip()
    if not cleaned_key:
        return None
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE idempotency_key = %s
                """,
                (cleaned_key,),
            )
            row = await cur.fetchone()
    return _serialize_row(row)


async def update_transcription_job_versioned(
    job_id: str,
    *,
    expected_version: int,
    status: str | TranscriptionJobStatus,
    stage: str | None = None,
    segment_count: int | None = None,
    completed_segments: int | None = None,
    provider_request_ids: list[str] | None = None,
    usage: dict[str, Any] | None = None,
    result_object_key: str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    clear_error: bool = False,
    conn: Any = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Conditionally update a job row when `version = expected_version`.

    Returns `(updated, row)`:
    - `(True, updated_row)` when the conditional update matched `expected_version`
      and incremented `version` to `expected_version + 1`.
    - `(False, current_row)` when another worker already advanced `version` (or
      `(False, None)` if the job does not exist).
    """
    cleaned_id = strip_pg_nul_str(str(job_id or "")).strip()
    if not cleaned_id or int(expected_version) < 1:
        return False, None

    clean_status = _normalize_status(status)
    clean_stage = strip_pg_nul_str(stage or clean_status).strip()
    req_ids_json = (
        json.dumps([strip_pg_nul_str(str(r)) for r in provider_request_ids], ensure_ascii=False)
        if provider_request_ids is not None
        else None
    )
    usage_json = (
        json.dumps(usage, ensure_ascii=False)
        if usage is not None
        else None
    )
    clean_res_key = (
        strip_pg_nul_str(result_object_key).strip()
        if result_object_key is not None
        else None
    )
    clean_err_cat = (
        strip_pg_nul_str(error_category).strip()
        if error_category is not None
        else None
    )
    clean_err_msg = (
        strip_pg_nul_str(error_message).strip()
        if error_message is not None
        else None
    )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_transcription_jobs
                SET status = %s,
                    stage = %s,
                    segment_count = COALESCE(%s, segment_count),
                    completed_segments = COALESCE(%s, completed_segments),
                    provider_request_ids = COALESCE(%s::jsonb, provider_request_ids),
                    usage = COALESCE(%s::jsonb, usage),
                    result_object_key = COALESCE(%s, result_object_key),
                    -- A new attempt drops the previous attempt's error but keeps
                    -- whatever error its own first snapshot carries.
                    error_category = CASE
                        WHEN %s THEN %s
                        ELSE COALESCE(%s, error_category)
                    END,
                    error_message = CASE
                        WHEN %s THEN %s
                        ELSE COALESCE(%s, error_message)
                    END,
                    version = version + 1,
                    updated_at = NOW(),
                    completed_at = CASE
                        WHEN %s IN ('succeeded', 'failed', 'cancelled')
                            THEN COALESCE(completed_at, NOW())
                        WHEN %s IN ('queued', 'transcribing', 'downloading', 'probing', 'splitting', 'submitting', 'assembling')
                            THEN NULL
                        ELSE completed_at
                    END
                WHERE job_id = %s
                  AND version = %s
                RETURNING {_JOB_COLS}
                """,
                (
                    clean_status,
                    clean_stage,
                    int(segment_count) if segment_count is not None else None,
                    int(completed_segments) if completed_segments is not None else None,
                    req_ids_json,
                    usage_json,
                    clean_res_key,
                    bool(clear_error),
                    clean_err_cat,
                    clean_err_cat,
                    bool(clear_error),
                    clean_err_msg,
                    clean_err_msg,
                    clean_status,
                    clean_status,
                    cleaned_id,
                    int(expected_version),
                ),
            )
            row = await cur.fetchone()
            if row is not None:
                return True, _serialize_row(row)

            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE job_id = %s
                """,
                (cleaned_id,),
            )
            current = await cur.fetchone()
    return False, _serialize_row(current)


async def list_transcription_jobs_by_user(
    user_id: str,
    *,
    limit: int = 50,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch recent transcription jobs created by `user_id`."""
    cleaned_user = strip_pg_nul_str(str(user_id or "")).strip()
    if not cleaned_user:
        return []
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE created_by_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (cleaned_user, max(1, min(int(limit), 200))),
            )
            rows = await cur.fetchall()
    return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def list_non_terminal_transcription_jobs(
    *,
    limit: int = 50,
    after_job_id: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch submitted jobs that have not reached a terminal state (including `indeterminate`) ordered by `job_id` cursor."""
    clamped_limit = max(1, min(int(limit), 200))
    cleaned_cursor = strip_pg_nul_str(str(after_job_id or "")).strip()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE status NOT IN ('awaiting_upload', 'succeeded', 'failed', 'cancelled')
                  AND job_id > %s
                ORDER BY job_id
                LIMIT %s
                """,
                (cleaned_cursor, clamped_limit),
            )
            rows = await cur.fetchall()
    return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def list_transcription_jobs_for_usage(
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
    offset: int = 0,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Read-only query fetching transcription jobs for per-user / per-date usage aggregation.

    Uses `updated_at >= start_date` and `created_at < end_date + 1 day` as the coarse
    SQL filter so attempts recorded after midnight for jobs created the previous evening
    are included, then `summarize_transcription_usage` filters accurately by `recorded_at`.
    """
    where_clauses: list[str] = ["1 = 1"]
    params: list[Any] = []

    if user_id and str(user_id).strip():
        where_clauses.append("created_by_user_id = %s")
        params.append(strip_pg_nul_str(str(user_id)).strip())
    if workspace_id and str(workspace_id).strip():
        where_clauses.append("workspace_id = %s")
        params.append(strip_pg_nul_str(str(workspace_id)).strip())
    if start_date and str(start_date).strip():
        where_clauses.append("updated_at >= %s::date")
        params.append(strip_pg_nul_str(str(start_date)).strip()[:10])
    if end_date and str(end_date).strip():
        where_clauses.append("created_at < (%s::date + INTERVAL '1 day')")
        params.append(strip_pg_nul_str(str(end_date)).strip()[:10])

    clamped_limit = max(1, min(int(limit), 2000))
    clamped_offset = max(0, int(offset))
    params.extend([clamped_limit, clamped_offset])

    where_sql = " AND ".join(where_clauses)
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_JOB_COLS}
                FROM dalton_transcription_jobs
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
    return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


def _extract_iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return "unknown"


def extract_job_usage_attempts(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract deduplicated per-attempt usage records from a `dalton_transcription_jobs` row.

    Enforces that every attempt record's `user_id` is `job["created_by_user_id"]`
    (the verified A1b job creator), never any untrusted sandbox identity.
    """
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return []
    authoritative_user_id = str(job.get("created_by_user_id") or "").strip()
    authoritative_workspace_id = str(job.get("workspace_id") or "").strip()
    authoritative_thread_id = str(job.get("thread_id") or "").strip()
    job_status = str(job.get("status") or "").strip()
    raw_usage = job.get("usage") if isinstance(job.get("usage"), Mapping) else {}
    job_date = _extract_iso_date(
        job.get("completed_at") or job.get("updated_at") or job.get("created_at")
    )

    by_attempt: dict[int, dict[str, Any]] = {}
    raw_attempts = raw_usage.get("attempts")
    if isinstance(raw_attempts, list):
        for item in raw_attempts:
            if not isinstance(item, Mapping):
                continue
            att_num = int(item.get("attempt") or 0)
            att_status = str(item.get("status") or "").strip()
            if att_num < 1 or att_status not in ("succeeded", "failed", "cancelled"):
                continue
            rec_at = str(
                item.get("recorded_at")
                or job.get("completed_at")
                or job.get("updated_at")
                or job.get("created_at")
                or ""
            )
            bill_raw = item.get("billable_seconds")
            att_entry: dict[str, Any] = {
                "job_id": job_id,
                "attempt": att_num,
                "user_id": authoritative_user_id,
                "workspace_id": authoritative_workspace_id,
                "thread_id": authoritative_thread_id,
                "status": att_status,
                "ok": att_status == "succeeded",
                "duration_seconds": round(float(item.get("duration_seconds") or 0.0), 3),
                "submitted_seconds": round(float(item.get("submitted_seconds") or 0.0), 3),
                "completed_seconds": round(float(item.get("completed_seconds") or 0.0), 3),
                "billable_seconds": (
                    round(float(bill_raw), 3)
                    if isinstance(bill_raw, (int, float)) and not isinstance(bill_raw, bool)
                    else None
                ),
                "segment_count": int(
                    item.get("segment_count")
                    if item.get("segment_count") is not None
                    else (job.get("segment_count") or 0)
                ),
                "completed_segments": int(
                    item.get("completed_segments")
                    if item.get("completed_segments") is not None
                    else (job.get("completed_segments") or 0)
                ),
                "error_category": (
                    str(item.get("error_category")).strip()
                    if item.get("error_category") and att_status != "succeeded"
                    else None
                ),
                "provider_request_ids": (
                    list(
                        dict.fromkeys(
                            str(r).strip()
                            for r in (item.get("provider_request_ids") or [])
                            if str(r).strip()
                        )
                    )
                    if item.get("provider_request_ids") is not None
                    else [
                        str(r).strip()
                        for r in (job.get("provider_request_ids") or [])
                        if str(r).strip()
                    ]
                ),
                "recorded_at": rec_at,
                "date": _extract_iso_date(rec_at) if rec_at else job_date,
            }
            if att_status == "cancelled":
                att_entry["billing_verified"] = False
            elif "billing_verified" in item:
                att_entry["billing_verified"] = bool(item.get("billing_verified"))
            if att_num not in by_attempt:
                by_attempt[att_num] = att_entry

    # Fallback if a terminal job row (`succeeded`, `failed`, or `cancelled` with submitted segments) has top-level `usage` without `attempts`
    cancel_obj = (
        raw_usage.get("cancellation")
        if isinstance(raw_usage.get("cancellation"), Mapping)
        else {}
    )
    cancel_req_ids = list(
        dict.fromkeys(
            str(r).strip()
            for src in (
                job.get("provider_request_ids") or [],
                cancel_obj.get("submitted_request_ids") or [],
            )
            if isinstance(src, (list, tuple))
            for r in src
            if str(r).strip()
        )
    )
    cancelled_has_submitted = job_status == "cancelled" and bool(
        cancel_req_ids
        or int(cancel_obj.get("submitted_segments_count") or 0) > 0
        or float(raw_usage.get("submitted_seconds") or 0.0) > 0.0
    )
    if job_status in ("succeeded", "failed") or cancelled_has_submitted:
        cur_att = int(raw_usage.get("attempt") or 1)
        if cur_att >= 1 and cur_att not in by_attempt:
            rec_at = str(
                cancel_obj.get("cancelled_at")
                or job.get("completed_at")
                or job.get("updated_at")
                or job.get("created_at")
                or ""
            )
            bill_raw = raw_usage.get("billable_seconds")
            err_cat = job.get("error_category") or raw_usage.get("error_category")
            fallback_entry: dict[str, Any] = {
                "job_id": job_id,
                "attempt": cur_att,
                "user_id": authoritative_user_id,
                "workspace_id": authoritative_workspace_id,
                "thread_id": authoritative_thread_id,
                "status": job_status,
                "ok": job_status == "succeeded",
                "duration_seconds": round(
                    float(raw_usage.get("duration_seconds") or 0.0), 3
                ),
                "submitted_seconds": round(
                    float(raw_usage.get("submitted_seconds") or 0.0), 3
                ),
                "completed_seconds": round(
                    float(raw_usage.get("completed_seconds") or 0.0), 3
                ),
                "billable_seconds": (
                    round(float(bill_raw), 3)
                    if isinstance(bill_raw, (int, float)) and not isinstance(bill_raw, bool)
                    else None
                ),
                "segment_count": int(job.get("segment_count") or 0),
                "completed_segments": int(job.get("completed_segments") or 0),
                "error_category": (
                    str(err_cat).strip()
                    if err_cat and job_status != "succeeded"
                    else None
                ),
                "provider_request_ids": cancel_req_ids,
                "recorded_at": rec_at,
                "date": _extract_iso_date(rec_at) if rec_at else job_date,
            }
            if job_status == "cancelled":
                fallback_entry["billing_verified"] = False
            by_attempt[cur_att] = fallback_entry

    return [by_attempt[k] for k in sorted(by_attempt)]


def reconcile_transcription_usage(
    jobs: Sequence[Mapping[str, Any]],
    bridge_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile backend `dalton_transcription_jobs` usage records with bridge JSONL records by `(job_id, attempt)`.

    - Deduplicates by `(job_id, attempt)` so an attempt present in both bridge JSONL
      and backend DB is counted at most once.
    - Enforces backend `created_by_user_id` as the authoritative caller identity whenever
      a backend job row exists.
    """
    job_creator_by_id: dict[str, tuple[str, str, str]] = {}
    backend_by_key: dict[tuple[str, int], dict[str, Any]] = {}

    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        jid = str(job.get("job_id") or "").strip()
        if jid:
            job_creator_by_id[jid] = (
                str(job.get("created_by_user_id") or "").strip(),
                str(job.get("workspace_id") or "").strip(),
                str(job.get("thread_id") or "").strip(),
            )
        for att_rec in extract_job_usage_attempts(job):
            key = (att_rec["job_id"], int(att_rec["attempt"]))
            backend_by_key[key] = att_rec

    bridge_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for brec in bridge_records or []:
        if not isinstance(brec, Mapping):
            continue
        jid = str(brec.get("job_id") or "").strip()
        att = int(brec.get("attempt") or 0)
        b_status = str(brec.get("status") or "").strip()
        if not jid or att < 1 or b_status not in ("succeeded", "failed", "cancelled"):
            continue
        key = (jid, att)
        if key in bridge_by_key:
            continue
        rec_at = str(brec.get("recorded_at") or "")
        bill_raw = brec.get("billable_seconds")
        # If the backend job row is known, enforce its verified `created_by_user_id`
        auth_uid, auth_ws, auth_th = job_creator_by_id.get(
            jid,
            (
                str(brec.get("user_id") or "").strip(),
                str(brec.get("workspace_id") or "").strip(),
                str(brec.get("thread_id") or "").strip(),
            ),
        )
        br_entry: dict[str, Any] = {
            "job_id": jid,
            "attempt": att,
            "user_id": auth_uid or str(brec.get("user_id") or "").strip(),
            "workspace_id": auth_ws or str(brec.get("workspace_id") or "").strip(),
            "thread_id": auth_th or str(brec.get("thread_id") or "").strip(),
            "status": b_status,
            "ok": b_status == "succeeded",
            "duration_seconds": round(float(brec.get("duration_seconds") or 0.0), 3),
            "submitted_seconds": round(float(brec.get("submitted_seconds") or 0.0), 3),
            "completed_seconds": round(float(brec.get("completed_seconds") or 0.0), 3),
            "billable_seconds": (
                round(float(bill_raw), 3)
                if isinstance(bill_raw, (int, float)) and not isinstance(bill_raw, bool)
                else None
            ),
            "segment_count": int(brec.get("segment_count") or 0),
            "completed_segments": int(brec.get("completed_segments") or 0),
            "error_category": (
                str(brec.get("error_category")).strip()
                if brec.get("error_category") and b_status != "succeeded"
                else None
            ),
            "provider_request_ids": [
                str(r).strip()
                for r in (brec.get("provider_request_ids") or [])
                if str(r).strip()
            ],
            "recorded_at": rec_at,
            "date": _extract_iso_date(rec_at),
            "_raw_bridge_user_id": str(brec.get("user_id") or "").strip(),
        }
        if b_status == "cancelled":
            br_entry["billing_verified"] = False
        elif "billing_verified" in brec:
            br_entry["billing_verified"] = bool(brec.get("billing_verified"))
        bridge_by_key[key] = br_entry

    all_keys = sorted(set(backend_by_key) | set(bridge_by_key))
    matched_keys: list[dict[str, Any]] = []
    backend_only_keys: list[dict[str, Any]] = []
    bridge_only_keys: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    merged_attempts: list[dict[str, Any]] = []

    for key in all_keys:
        in_db = key in backend_by_key
        in_br = key in bridge_by_key
        if in_db and in_br:
            db_r = backend_by_key[key]
            br_r = bridge_by_key[key]
            matched_keys.append({"job_id": key[0], "attempt": key[1]})
            diffs: dict[str, Any] = {}
            if db_r["status"] != br_r["status"]:
                diffs["status"] = {"backend": db_r["status"], "bridge": br_r["status"]}
            if abs(float(db_r["duration_seconds"]) - float(br_r["duration_seconds"])) > 0.01:
                diffs["duration_seconds"] = {
                    "backend": db_r["duration_seconds"],
                    "bridge": br_r["duration_seconds"],
                }
            if int(db_r["segment_count"]) != int(br_r["segment_count"]):
                diffs["segment_count"] = {
                    "backend": db_r["segment_count"],
                    "bridge": br_r["segment_count"],
                }
            raw_br_uid = br_r.get("_raw_bridge_user_id") or br_r["user_id"]
            if db_r["user_id"] and raw_br_uid and db_r["user_id"] != raw_br_uid:
                diffs["user_id"] = {
                    "backend": db_r["user_id"],
                    "bridge": raw_br_uid,
                }
            if diffs:
                mismatches.append(
                    {
                        "job_id": key[0],
                        "attempt": key[1],
                        "differences": diffs,
                    }
                )
            merged_attempts.append(dict(db_r))
        elif in_db:
            backend_only_keys.append({"job_id": key[0], "attempt": key[1]})
            merged_attempts.append(dict(backend_by_key[key]))
        else:
            bridge_only_keys.append({"job_id": key[0], "attempt": key[1]})
            clean_br = dict(bridge_by_key[key])
            clean_br.pop("_raw_bridge_user_id", None)
            merged_attempts.append(clean_br)

    return {
        "attempts": merged_attempts,
        "matched_count": len(matched_keys),
        "matched": matched_keys,
        "backend_only_count": len(backend_only_keys),
        "backend_only": backend_only_keys,
        "bridge_only_count": len(bridge_only_keys),
        "bridge_only": bridge_only_keys,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "deduplicated_attempt_count": len(merged_attempts),
    }


def summarize_transcription_usage(
    jobs: Sequence[Mapping[str, Any]],
    *,
    bridge_records: Sequence[Mapping[str, Any]] | None = None,
    user_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    truncated: bool | None = None,
) -> dict[str, Any]:
    """Produce read-only usage summaries grouped by user, by date, and by `(user_id, date)`.

    Reconciles backend job rows with optional `bridge_records` by `(job_id, attempt)`
    without double counting, attributes every attempt to the verified job creator,
    counts `cancelled_attempts` separately, and reports truncation (`truncated`,
    `jobs_scanned`, `limit`, `offset`) when the scan hits `limit`.
    """
    jobs_list = [j for j in jobs if isinstance(j, Mapping)]
    if limit is not None:
        clamped_limit = max(1, min(int(limit), 2000))
        if len(jobs_list) > clamped_limit:
            jobs_list = jobs_list[:clamped_limit]
            is_truncated = True
        else:
            is_truncated = (
                bool(truncated)
                if truncated is not None
                else (len(jobs_list) >= clamped_limit)
            )
    else:
        clamped_limit = None
        is_truncated = bool(truncated) if truncated is not None else False
    jobs_scanned = len(jobs_list)

    reconciled = reconcile_transcription_usage(jobs_list, bridge_records)
    attempts: list[dict[str, Any]] = []
    clean_uid_filter = str(user_id).strip() if user_id and str(user_id).strip() else None
    clean_start = str(start_date).strip()[:10] if start_date and str(start_date).strip() else None
    clean_end = str(end_date).strip()[:10] if end_date and str(end_date).strip() else None

    for rec in reconciled["attempts"]:
        if clean_uid_filter and rec.get("user_id") != clean_uid_filter:
            continue
        rec_date = str(rec.get("date") or "unknown")
        if clean_start and rec_date != "unknown" and rec_date < clean_start:
            continue
        if clean_end and rec_date != "unknown" and rec_date > clean_end:
            continue
        attempts.append(rec)

    def _new_bucket(**extra: Any) -> dict[str, Any]:
        return {
            **extra,
            "attempt_count": 0,
            "job_ids": set(),
            "succeeded_attempts": 0,
            "failed_attempts": 0,
            "cancelled_attempts": 0,
            "duration_seconds": 0.0,
            "submitted_seconds": 0.0,
            "completed_seconds": 0.0,
            "billable_seconds": 0.0,
            "segment_count": 0,
            "completed_segments": 0,
            "error_categories": {},
        }

    def _accumulate(bucket: dict[str, Any], rec: Mapping[str, Any]) -> None:
        bucket["attempt_count"] += 1
        bucket["job_ids"].add(str(rec.get("job_id") or ""))
        att_status = str(rec.get("status") or "")
        if rec.get("ok") or att_status == "succeeded":
            bucket["succeeded_attempts"] += 1
        elif att_status == "cancelled":
            bucket["cancelled_attempts"] += 1
        else:
            bucket["failed_attempts"] += 1
        bucket["duration_seconds"] = round(
            bucket["duration_seconds"] + float(rec.get("duration_seconds") or 0.0), 3
        )
        bucket["submitted_seconds"] = round(
            bucket["submitted_seconds"] + float(rec.get("submitted_seconds") or 0.0), 3
        )
        bucket["completed_seconds"] = round(
            bucket["completed_seconds"] + float(rec.get("completed_seconds") or 0.0), 3
        )
        bill_s = rec.get("billable_seconds")
        if isinstance(bill_s, (int, float)) and not isinstance(bill_s, bool):
            bucket["billable_seconds"] = round(
                bucket["billable_seconds"] + float(bill_s), 3
            )
        bucket["segment_count"] += int(rec.get("segment_count") or 0)
        bucket["completed_segments"] += int(rec.get("completed_segments") or 0)
        err_cat = rec.get("error_category")
        if err_cat:
            cat_str = str(err_cat)
            bucket["error_categories"][cat_str] = (
                int(bucket["error_categories"].get(cat_str, 0)) + 1
            )

    def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        job_ids_set = bucket.pop("job_ids", set())
        bucket["job_count"] = len(job_ids_set)
        bucket["pricing_configured"] = False
        bucket["cost_note"] = "Seed-ASR 单价未配置，仅按秒汇总时长（不显示 0 元）"
        return bucket

    totals_bucket = _new_bucket()
    by_user_map: dict[str, dict[str, Any]] = {}
    by_date_map: dict[str, dict[str, Any]] = {}
    by_user_date_map: dict[tuple[str, str], dict[str, Any]] = {}

    for rec in attempts:
        uid = str(rec.get("user_id") or "unknown")
        dt = str(rec.get("date") or "unknown")
        _accumulate(totals_bucket, rec)

        if uid not in by_user_map:
            by_user_map[uid] = _new_bucket(user_id=uid)
        _accumulate(by_user_map[uid], rec)

        if dt not in by_date_map:
            by_date_map[dt] = _new_bucket(date=dt)
        _accumulate(by_date_map[dt], rec)

        ud_key = (uid, dt)
        if ud_key not in by_user_date_map:
            by_user_date_map[ud_key] = _new_bucket(user_id=uid, date=dt)
        _accumulate(by_user_date_map[ud_key], rec)

    return {
        "jobs_scanned": jobs_scanned,
        "truncated": is_truncated,
        "limit": clamped_limit if clamped_limit is not None else jobs_scanned,
        "offset": max(0, int(offset)),
        "totals": _finalize_bucket(totals_bucket),
        "by_user": [_finalize_bucket(by_user_map[k]) for k in sorted(by_user_map)],
        "by_date": [_finalize_bucket(by_date_map[k]) for k in sorted(by_date_map)],
        "by_user_date": [
            _finalize_bucket(by_user_date_map[k]) for k in sorted(by_user_date_map)
        ],
        "attempts": attempts,
        "reconciliation": {
            "matched_count": reconciled["matched_count"],
            "matched": reconciled["matched"],
            "backend_only_count": reconciled["backend_only_count"],
            "backend_only": reconciled["backend_only"],
            "bridge_only_count": reconciled["bridge_only_count"],
            "bridge_only": reconciled["bridge_only"],
            "mismatch_count": reconciled["mismatch_count"],
            "mismatches": reconciled["mismatches"],
            "deduplicated_attempt_count": len(attempts),
        },
    }
