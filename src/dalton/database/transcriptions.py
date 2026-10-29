"""Database ledger queries for audio transcription jobs (`dalton_transcription_jobs`).

Every state transition uses a conditional `WHERE job_id = %s AND version = %s`
update so concurrent backend workers never overwrite each other's state changes,
and no worker relies on an in-process job dictionary.
"""

from __future__ import annotations

import json
import logging
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
