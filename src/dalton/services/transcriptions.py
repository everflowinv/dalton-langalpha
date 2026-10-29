"""Backend service for audio transcription jobs, ticket credentials, and bridge synchronization.

Enforces:
- Internal/admin tier check (`get_user_tier` in `{'admin', 'internal'}`) on every operation.
- Per-job HMAC-SHA256 task tickets bound to `(job_id, user_id, workspace_id, thread_id,
  source_object_key, source_size_bytes, source_sha256, ops, exp)`.
- Accounting identity strictly derived from the authenticated caller / bound ticket,
  never from sandbox-mutable `X-Dalton-*` headers or request body caller fields.
- Result and status visibility restricted to the job creator (while holding workspace
  access) or users with read access to the workspace; cross-user lookups without
  workspace access return HTTP 404.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import httpx
from fastapi import HTTPException

from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import verify_workspace_access
from src.dalton.database import transcriptions as db
from src.dalton.database.transcriptions import (
    RETRYABLE_TRANSCRIPTION_STATUSES,
    TERMINAL_TRANSCRIPTION_STATUSES,
    TranscriptionJobStatus,
)
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_dalton_settings,
    get_dalton_transcription_ticket_hmac_key,
)
from src.utils.storage import is_storage_enabled
from src.utils.storage.s3_compatible import (
    abort_multipart_upload,
    complete_multipart_upload,
    delete_object,
    list_uploaded_parts,
    object_size,
    open_multipart_upload,
    sign_upload_part,
)

logger = logging.getLogger(__name__)

DEFAULT_TICKET_TTL_S: int = 7200
MAX_TRANSCRIPTION_AUDIO_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GiB
TRANSCRIPTION_UPLOAD_PART_BYTES: int = 16 * 1024 * 1024  # 16 MiB
MAX_TRANSCRIPTION_UPLOAD_PARTS: int = 128  # 128 * 16 MiB = 2 GiB
TRANSCRIPTION_UPLOAD_URL_EXPIRES_S: int = 900  # 15 minutes
TRANSCRIPTION_RECONCILE_INTERVAL_S: float = 300.0  # 5 minutes
TRANSCRIPTION_RECONCILE_BATCH_LIMIT: int = 50
BRIDGE_TRANSCRIPTION_TIMEOUT_S: float = 30.0
ALLOWED_TICKET_OPERATIONS: tuple[str, ...] = (
    "upload",
    "submit",
    "status",
    "result",
    "retry",
    "cancel",
)

EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE = (
    "外部成员不能使用录音转写（转写功能仅管理员与内部成员可用）；"
    "请如实告知用户这一身份限制，不要尝试绕过。"
)

PROVIDER_CANCEL_UNVERIFIED_NOTICE = (
    "已停止后续未提交的分片；已提交给火山 Seed-ASR 的分片能否在供应商侧取消"
    "及是否仍计费未核实，已提交分片无法保证取消或免除计费。"
)

_TRANSCRIPTION_IDEMPOTENCY_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FORBIDDEN_KEY_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\\;$()`|&<>'\"!*?\[\]{}~#]")

# Non-decreasing progress order used when syncing non-terminal states from bridge
_STATUS_PROGRESS_ORDER: dict[str, int] = {
    TranscriptionJobStatus.AWAITING_UPLOAD.value: 0,
    TranscriptionJobStatus.QUEUED.value: 1,
    TranscriptionJobStatus.DOWNLOADING.value: 2,
    TranscriptionJobStatus.PROBING.value: 3,
    TranscriptionJobStatus.SPLITTING.value: 4,
    TranscriptionJobStatus.SUBMITTING.value: 5,
    TranscriptionJobStatus.TRANSCRIBING.value: 6,
    TranscriptionJobStatus.ASSEMBLING.value: 7,
    TranscriptionJobStatus.INDETERMINATE.value: 8,
    TranscriptionJobStatus.FAILED.value: 9,
    TranscriptionJobStatus.CANCELLED.value: 9,
    TranscriptionJobStatus.SUCCEEDED.value: 10,
}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def normalize_hotwords(hotwords: list[str] | str | None) -> list[str]:
    """Normalize a list or comma-separated string of hotwords into a deduplicated list."""
    if hotwords is None:
        return []
    raw_items: list[str]
    if isinstance(hotwords, str):
        raw_items = [part.strip() for part in hotwords.split(",")]
    elif isinstance(hotwords, list):
        raw_items = [str(item).strip() for item in hotwords]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        if item and item not in seen:
            seen.add(item)
            out.append(item[:100])
        if len(out) >= 100:
            break
    return out


def derive_transcription_idempotency_key(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str | None,
    source_sha256: str,
    language: str | None = None,
    hotwords: list[str] | None = None,
) -> str:
    """Compute a deterministic UUID5 idempotency key for a transcription request."""
    norm_lang = (language or "").strip().lower()
    norm_hw = ",".join(sorted(normalize_hotwords(hotwords)))
    seed = (
        f"dalton-asr:{user_id.strip()}:{workspace_id.strip()}:"
        f"{(thread_id or '').strip()}:{source_sha256.strip().lower()}:"
        f"{norm_lang}:{norm_hw}"
    )
    return str(uuid5(_TRANSCRIPTION_IDEMPOTENCY_NAMESPACE, seed))


def build_transcription_source_object_key(
    workspace_id: str,
    job_id: str,
    filename: str,
) -> str:
    """Build the canonical object storage key for a job's source audio."""
    base_name = Path(filename or "audio.bin").name
    safe_name = _SAFE_FILENAME_RE.sub("_", base_name).strip("._") or "audio.bin"
    return f"transcriptions/{workspace_id.strip()}/{job_id.strip()}/source/{safe_name}"


def validate_source_object_key(
    source_object_key: str,
    *,
    workspace_id: str,
) -> str:
    """Validate that `source_object_key` is a safe workspace-scoped object key (never a URL or host path)."""
    cleaned = str(source_object_key or "").strip()
    ws_clean = str(workspace_id or "").strip()
    if not cleaned or not ws_clean:
        raise HTTPException(
            status_code=400, detail="source_object_key and workspace_id are required"
        )
    if (
        "://" in cleaned
        or cleaned.startswith("/")
        or cleaned.startswith(".")
        or _FORBIDDEN_KEY_CHARS_RE.search(cleaned)
    ):
        raise HTTPException(
            status_code=400,
            detail="source_object_key must be a relative storage object key, not a URL or host path",
        )
    parts = [p for p in cleaned.split("/") if p]
    if ".." in parts or "." in parts:
        raise HTTPException(
            status_code=400,
            detail="Path traversal is not allowed in source_object_key",
        )
    allowed_prefixes = (
        f"transcriptions/{ws_clean}/",
        f"uploads/{ws_clean}/",
    )
    if not cleaned.startswith(allowed_prefixes):
        raise HTTPException(
            status_code=403,
            detail="source_object_key does not belong to the target workspace",
        )
    return cleaned


def issue_transcription_ticket(
    *,
    job_id: str,
    user_id: str,
    workspace_id: str,
    thread_id: str | None,
    source_object_key: str,
    source_size_bytes: int,
    source_sha256: str,
    allowed_operations: tuple[str, ...] | list[str] = ALLOWED_TICKET_OPERATIONS,
    ttl_s: int = DEFAULT_TICKET_TTL_S,
    now_ts: int | None = None,
) -> tuple[str, int]:
    """Issue a time-limited HMAC-SHA256 ticket bound to a single transcription job.

    Returns `(ticket_token, expires_at_unix)`.
    """
    hmac_key = get_dalton_transcription_ticket_hmac_key()
    if not hmac_key:
        raise HTTPException(
            status_code=503,
            detail="Transcription ticket signing key is not configured",
        )
    iat = int(now_ts if now_ts is not None else time.time())
    exp = iat + max(1, int(ttl_s))
    ops = [op for op in allowed_operations if op in ALLOWED_TICKET_OPERATIONS]
    if not ops:
        raise ValueError("At least one valid operation is required for transcription ticket")

    payload = {
        "v": 1,
        "typ": "dalton_asr_ticket",
        "job_id": str(job_id).strip(),
        "user_id": str(user_id).strip(),
        "workspace_id": str(workspace_id).strip(),
        "thread_id": str(thread_id or "").strip(),
        "source_object_key": str(source_object_key).strip(),
        "source_size_bytes": int(source_size_bytes),
        "source_sha256": str(source_sha256).strip().lower(),
        "ops": ops,
        "iat": iat,
        "exp": exp,
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(hmac_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}", exp


def is_transcription_ticket_token(token: str | None) -> bool:
    """Return True if `token` looks like a 2-segment Dalton transcription ticket (not a 3-segment JWT)."""
    if not token or not isinstance(token, str):
        return False
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    try:
        raw = _b64url_decode(parts[0])
        parsed = json.loads(raw.decode("utf-8"))
        return isinstance(parsed, dict) and parsed.get("typ") == "dalton_asr_ticket"
    except Exception:
        return False


def verify_transcription_ticket(
    token: str,
    *,
    required_op: str | None = None,
    expected_job_id: str | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Verify a transcription ticket's signature, expiration, operation scope, and job binding."""
    hmac_key = get_dalton_transcription_ticket_hmac_key()
    if not hmac_key:
        raise HTTPException(
            status_code=503,
            detail="Transcription ticket signing key is not configured",
        )
    cleaned = str(token or "").strip()
    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=401, detail="Invalid transcription ticket")

    payload_b64, sig_b64 = parts[0], parts[1]
    expected_sig = hmac.new(
        hmac_key, payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        supplied_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid transcription ticket signature"
        ) from exc

    if not hmac.compare_digest(supplied_sig, expected_sig):
        raise HTTPException(
            status_code=401, detail="Invalid transcription ticket signature"
        )

    try:
        claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Malformed transcription ticket payload"
        ) from exc

    if (
        not isinstance(claims, dict)
        or claims.get("v") != 1
        or claims.get("typ") != "dalton_asr_ticket"
    ):
        raise HTTPException(
            status_code=401, detail="Unsupported transcription ticket version"
        )

    now = int(now_ts if now_ts is not None else time.time())
    exp = claims.get("exp")
    if not isinstance(exp, int) or now >= exp:
        raise HTTPException(
            status_code=401, detail="Transcription ticket has expired"
        )

    for str_key in ("job_id", "user_id", "workspace_id", "source_object_key", "source_sha256"):
        val = claims.get(str_key)
        if not isinstance(val, str) or not val.strip():
            raise HTTPException(
                status_code=401, detail=f"Ticket missing required claim '{str_key}'"
            )

    size_val = claims.get("source_size_bytes")
    if not isinstance(size_val, int) or isinstance(size_val, bool) or size_val <= 0:
        raise HTTPException(
            status_code=401, detail="Ticket has invalid source_size_bytes"
        )

    ops = claims.get("ops")
    if not isinstance(ops, list) or not all(isinstance(x, str) for x in ops):
        raise HTTPException(status_code=401, detail="Ticket has invalid ops claim")

    if required_op is not None and required_op not in ops:
        raise HTTPException(
            status_code=403,
            detail=f"Ticket does not permit operation '{required_op}'",
        )

    if expected_job_id is not None and claims["job_id"] != str(expected_job_id).strip():
        # Cross-job ticket query returns 404 so a ticket cannot probe other job IDs
        raise HTTPException(status_code=404, detail="Transcription job not found")

    return claims


async def require_internal_transcription_caller(user_id: str) -> str:
    """Verify `user_id` is non-empty and belongs to `admin` or `internal` tier."""
    cleaned = str(user_id or "").strip()
    if not cleaned:
        raise HTTPException(status_code=401, detail="Authentication required")
    tier = await get_user_tier(cleaned)
    if tier == "external":
        raise HTTPException(
            status_code=403,
            detail=EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE,
        )
    return cleaned


async def verify_creator_workspace_access(
    workspace_id: str,
    user_id: str,
) -> None:
    """Verify the job creator currently has read access to `workspace_id` (always live, never skipped)."""
    try:
        await verify_workspace_access(
            workspace_id, user_id, need="read", channel="cos"
        )
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(
                status_code=403,
                detail="Workspace access denied or revoked",
            ) from exc
        raise


async def authorize_job_access(
    job: dict[str, Any],
    caller_user_id: str,
    *,
    need: str = "read",
) -> None:
    """Authorize `caller_user_id` against `job`.

    - Always re-checks that `caller_user_id` is currently `admin` or `internal`.
    - When `caller_user_id == job["created_by_user_id"]`, verifies the creator still
      holds access to `job["workspace_id"]` (revoked workspace access -> 403).
    - When `caller_user_id != job["created_by_user_id"]`, verifies the caller is the
      workspace owner or holds explicit `web` access on `job["workspace_id"]`;
      otherwise raises 404 Not Found so cross-user job IDs are invisible.
    """
    await require_internal_transcription_caller(caller_user_id)

    creator_id = str(job.get("created_by_user_id") or "").strip()
    workspace_id = str(job.get("workspace_id") or "").strip()

    if caller_user_id == creator_id:
        try:
            await verify_workspace_access(
                workspace_id, caller_user_id, need="read", channel="cos"
            )
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                raise HTTPException(
                    status_code=403,
                    detail="Workspace access has been revoked",
                ) from exc
            raise
        return

    # Non-creator: only workspace owner or explicit web-channel reader/writer may see the job
    try:
        await verify_workspace_access(
            workspace_id, caller_user_id, need=need, channel="web"
        )
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(
                status_code=404,
                detail="Transcription job not found",
            ) from exc
        raise


def _bridge_admin_headers() -> dict[str, str]:
    admin_token = get_dalton_bridge_admin_token()
    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="DALTON_BRIDGE_ADMIN_TOKEN is not configured on backend",
        )
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {admin_token}",
        "X-Dalton-Bridge-Token": admin_token,
    }


async def call_bridge_submit_transcription(
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """POST `payload` to `{bridge_url}/transcriptions` using the bridge admin token."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    headers = _bridge_admin_headers()
    async with httpx.AsyncClient(
        timeout=BRIDGE_TRANSCRIPTION_TIMEOUT_S, trust_env=False
    ) as client:
        resp = await client.post(
            f"{bridge_url}/transcriptions", json=payload, headers=headers
        )
        return _parse_bridge_response(resp)


async def call_bridge_get_transcription(
    job_id: str,
) -> tuple[int, dict[str, Any]]:
    """GET `{bridge_url}/transcriptions/{job_id}` using the bridge admin token."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    headers = _bridge_admin_headers()
    async with httpx.AsyncClient(
        timeout=BRIDGE_TRANSCRIPTION_TIMEOUT_S, trust_env=False
    ) as client:
        resp = await client.get(
            f"{bridge_url}/transcriptions/{job_id}", headers=headers
        )
        return _parse_bridge_response(resp)


async def call_bridge_get_transcription_result(
    job_id: str,
) -> tuple[int, dict[str, Any]]:
    """GET `{bridge_url}/transcriptions/{job_id}/result` using the bridge admin token."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    headers = _bridge_admin_headers()
    async with httpx.AsyncClient(
        timeout=BRIDGE_TRANSCRIPTION_TIMEOUT_S, trust_env=False
    ) as client:
        resp = await client.get(
            f"{bridge_url}/transcriptions/{job_id}/result", headers=headers
        )
        return _parse_bridge_response(resp)


async def open_bridge_raw_result_stream(
    job_id: str,
) -> tuple[int, dict[str, Any] | AsyncIterator[bytes]]:
    """Open a streaming GET to `{bridge_url}/transcriptions/{job_id}/result/raw` using the bridge admin token."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    headers = _bridge_admin_headers()
    client = httpx.AsyncClient(
        timeout=BRIDGE_TRANSCRIPTION_TIMEOUT_S, trust_env=False
    )
    req = client.build_request(
        "GET", f"{bridge_url}/transcriptions/{job_id}/result/raw", headers=headers
    )
    try:
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise

    if resp.status_code != 200:
        try:
            raw_bytes = await resp.aread()
            data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                data = {"ok": False, "error": str(data)}
        except Exception:
            data = {
                "ok": False,
                "error": f"Bridge returned HTTP {resp.status_code}",
            }
        finally:
            await resp.aclose()
            await client.aclose()
        return resp.status_code, data

    async def _iter_bytes() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return 200, _iter_bytes()


async def call_bridge_action_transcription(
    job_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST `{bridge_url}/transcriptions/{job_id}/{action}` (`retry` or `cancel`)."""
    if action not in ("retry", "cancel"):
        raise ValueError(f"Unsupported bridge transcription action: {action!r}")
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    headers = _bridge_admin_headers()
    async with httpx.AsyncClient(
        timeout=BRIDGE_TRANSCRIPTION_TIMEOUT_S, trust_env=False
    ) as client:
        resp = await client.post(
            f"{bridge_url}/transcriptions/{job_id}/{action}",
            json=payload or {},
            headers=headers,
        )
        return _parse_bridge_response(resp)


def _parse_bridge_response(resp: httpx.Response) -> tuple[int, dict[str, Any]]:
    try:
        data = resp.json()
        if not isinstance(data, dict):
            data = {"ok": False, "status": "invalid_response", "error": str(data)}
    except Exception:
        data = {
            "ok": False,
            "status": "http_error",
            "error": f"Bridge returned HTTP {resp.status_code}",
        }
    return resp.status_code, data


def _extract_bridge_attempt_version(bridge_job: dict[str, Any]) -> tuple[int, int]:
    """Extract `(attempt, bridge_version)` from a bridge `state.json` snapshot."""
    raw_usage = (
        bridge_job.get("usage") if isinstance(bridge_job.get("usage"), dict) else {}
    )
    att_raw = bridge_job.get("attempt")
    if not isinstance(att_raw, int) or isinstance(att_raw, bool):
        att_raw = raw_usage.get("attempt")
    b_attempt = (
        int(att_raw)
        if isinstance(att_raw, int) and not isinstance(att_raw, bool) and att_raw > 0
        else 1
    )

    ver_raw = bridge_job.get("version")
    if not isinstance(ver_raw, int) or isinstance(ver_raw, bool):
        ver_raw = raw_usage.get("bridge_version")
    b_version = (
        int(ver_raw)
        if isinstance(ver_raw, int) and not isinstance(ver_raw, bool) and ver_raw > 0
        else 1
    )
    return b_attempt, b_version


def _merge_provider_request_ids(*sources: Any) -> list[str]:
    """Return order-preserving deduplicated union of provider request ID lists."""
    merged: list[str] = []
    seen: set[str] = set()
    for src in sources:
        if not isinstance(src, (list, tuple)):
            continue
        for raw in src:
            val = str(raw or "").strip()
            if val and val not in seen:
                seen.add(val)
                merged.append(val)
    return merged


def _extract_db_attempt_version(job: dict[str, Any]) -> tuple[int, int]:
    """Extract recorded `(attempt, bridge_version)` from a Postgres job row."""
    cur_status = str(job.get("status") or "")
    cur_usage = job.get("usage") if isinstance(job.get("usage"), dict) else {}
    att_raw = cur_usage.get("attempt", job.get("attempt"))
    ver_raw = cur_usage.get("bridge_version", job.get("bridge_version"))

    if (
        cur_status == TranscriptionJobStatus.AWAITING_UPLOAD.value
        and att_raw is None
        and ver_raw is None
    ):
        return 0, 0

    rec_attempt = (
        int(att_raw)
        if isinstance(att_raw, int) and not isinstance(att_raw, bool) and att_raw > 0
        else 1
    )
    if isinstance(ver_raw, int) and not isinstance(ver_raw, bool) and ver_raw > 0:
        rec_version = int(ver_raw)
    else:
        db_ver = job.get("version")
        rec_version = (
            int(db_ver)
            if isinstance(db_ver, int)
            and not isinstance(db_ver, bool)
            and db_ver > 0
            and ("attempt" in job or "attempt" in cur_usage)
            else 0
        )
    return rec_attempt, rec_version


async def _sync_job_from_bridge_state(
    job: dict[str, Any],
    bridge_job: dict[str, Any],
    *,
    allow_reset_from_terminal: bool = False,
) -> dict[str, Any]:
    """Conditionally update the Postgres job ledger from a bridge `state.json` snapshot.

    Enforces `(attempt, version)` ordering:
    - Once a newer `attempt` is recorded in the DB, any delayed response from an
      older `attempt` is discarded.
    - Within the same `attempt`, `version` must be strictly increasing (monotonic).
    """
    b_attempt, b_version = _extract_bridge_attempt_version(bridge_job)
    current = job
    for _ in range(3):
        cur_status = str(current.get("status") or "")
        new_status = str(bridge_job.get("status") or cur_status)
        if new_status not in _STATUS_PROGRESS_ORDER:
            return current

        rec_attempt, rec_version = _extract_db_attempt_version(current)

        # Discard any delayed response from an older attempt
        if b_attempt < rec_attempt:
            return current

        # Within the same attempt, bridge version must be strictly newer
        if b_attempt == rec_attempt and b_version <= rec_version:
            return current

        is_newer_attempt = b_attempt > rec_attempt and rec_attempt > 0
        can_reset_terminal = allow_reset_from_terminal or is_newer_attempt

        # Never regress a terminal row within the same attempt
        if (
            not can_reset_terminal
            and cur_status in TERMINAL_TRANSCRIPTION_STATUSES
            and new_status != cur_status
        ):
            return current

        # Within the same attempt, only `indeterminate -> transcribing` is allowed as a
        # non-terminal recovery transition once the bridge reconciles the task via query.
        is_indeterminate_recovery = (
            cur_status == TranscriptionJobStatus.INDETERMINATE.value
            and new_status == TranscriptionJobStatus.TRANSCRIBING.value
        )
        if (
            not can_reset_terminal
            and not is_indeterminate_recovery
            and _STATUS_PROGRESS_ORDER.get(new_status, 0)
            < _STATUS_PROGRESS_ORDER.get(cur_status, 0)
        ):
            return current

        new_stage = str(bridge_job.get("stage") or new_status)
        seg_cnt = bridge_job.get("segment_count")
        comp_cnt = bridge_job.get("completed_segments")
        req_ids = bridge_job.get("provider_request_ids")
        raw_usage = bridge_job.get("usage")
        res_key = bridge_job.get("result_object_key")
        err_cat = bridge_job.get("error_category")
        err_msg = bridge_job.get("error_message")

        merged_usage = (
            dict(current.get("usage"))
            if isinstance(current.get("usage"), dict)
            else {}
        )
        prev_attempts_list = [
            dict(a)
            for a in (merged_usage.get("attempts") or [])
            if isinstance(a, dict)
        ]
        extracted_prev_attempts = db.extract_job_usage_attempts(current)
        if isinstance(raw_usage, dict):
            for k, v in raw_usage.items():
                if k not in ("attempts", "summary"):
                    merged_usage[k] = v

        # Always enforce the A1b verified job creator identity; never trust sandbox or bridge caller strings
        auth_uid = str(current.get("created_by_user_id") or "").strip()
        auth_ws = str(current.get("workspace_id") or "").strip()
        auth_th = str(current.get("thread_id") or "").strip()
        if auth_uid:
            merged_usage["user_id"] = auth_uid
        if auth_ws:
            merged_usage["workspace_id"] = auth_ws
        merged_usage["thread_id"] = auth_th
        merged_usage["attempt"] = b_attempt
        merged_usage["bridge_version"] = b_version

        attempts_by_num: dict[int, dict[str, Any]] = {}
        for item in prev_attempts_list:
            att_n = int(item.get("attempt") or 0)
            att_st = str(item.get("status") or "").strip()
            if (
                att_n >= 1
                and att_st in ("succeeded", "failed", "cancelled")
                and att_n not in attempts_by_num
            ):
                fixed = dict(item)
                fixed.pop("date", None)
                fixed["job_id"] = str(current["job_id"])
                fixed["user_id"] = auth_uid
                fixed["workspace_id"] = auth_ws
                fixed["thread_id"] = auth_th
                attempts_by_num[att_n] = fixed

        for item in extracted_prev_attempts:
            att_n = int(item.get("attempt") or 0)
            if att_n >= 1 and att_n not in attempts_by_num:
                fixed = dict(item)
                fixed.pop("date", None)
                fixed["job_id"] = str(current["job_id"])
                fixed["user_id"] = auth_uid
                fixed["workspace_id"] = auth_ws
                fixed["thread_id"] = auth_th
                attempts_by_num[att_n] = fixed

        if isinstance(raw_usage, dict) and isinstance(raw_usage.get("attempts"), list):
            for item in raw_usage["attempts"]:
                if not isinstance(item, dict):
                    continue
                att_n = int(item.get("attempt") or 0)
                att_st = str(item.get("status") or "").strip()
                if (
                    att_n >= 1
                    and att_st in ("succeeded", "failed", "cancelled")
                    and att_n not in attempts_by_num
                ):
                    fixed = dict(item)
                    fixed.pop("date", None)
                    fixed["job_id"] = str(current["job_id"])
                    fixed["attempt"] = att_n
                    fixed["user_id"] = auth_uid
                    fixed["workspace_id"] = auth_ws
                    fixed["thread_id"] = auth_th
                    fixed["status"] = att_st
                    fixed["ok"] = att_st == "succeeded"
                    if att_st == "cancelled":
                        fixed["billing_verified"] = False
                    attempts_by_num[att_n] = fixed

        cancel_meta = (
            bridge_job.get("cancellation")
            if isinstance(bridge_job.get("cancellation"), dict)
            else (
                merged_usage.get("cancellation")
                if isinstance(merged_usage.get("cancellation"), dict)
                else {}
            )
        )
        same_attempt_job_req_ids = (
            current.get("provider_request_ids") if b_attempt == rec_attempt else None
        )
        existing_att = attempts_by_num.get(b_attempt)
        merged_req_ids = _merge_provider_request_ids(
            same_attempt_job_req_ids,
            (existing_att or {}).get("provider_request_ids"),
            req_ids if isinstance(req_ids, list) else None,
            cancel_meta.get("submitted_request_ids"),
        )
        cancelled_with_submitted = (
            new_status == TranscriptionJobStatus.CANCELLED.value
            and bool(
                merged_req_ids
                or int(cancel_meta.get("submitted_segments_count") or 0) > 0
                or float(merged_usage.get("submitted_seconds") or 0.0) > 0.0
            )
        )

        # Item 6: The first usage record for a given attempt is final; never overwrite
        # an existing `succeeded`/`failed`/`cancelled` record for `b_attempt`.
        if new_status in (
            TranscriptionJobStatus.SUCCEEDED.value,
            TranscriptionJobStatus.FAILED.value,
        ) or cancelled_with_submitted:
            if existing_att is None:
                eff_seg_cnt = int(
                    seg_cnt
                    if isinstance(seg_cnt, int)
                    else (current.get("segment_count") or 0)
                )
                eff_comp_cnt = int(
                    comp_cnt
                    if isinstance(comp_cnt, int)
                    else (current.get("completed_segments") or 0)
                )
                bill_raw = merged_usage.get("billable_seconds")
                bill_val = (
                    round(float(bill_raw), 3)
                    if isinstance(bill_raw, (int, float))
                    and not isinstance(bill_raw, bool)
                    else None
                )
                rec_ts = str(
                    cancel_meta.get("cancelled_at")
                    or bridge_job.get("completed_at")
                    or bridge_job.get("updated_at")
                    or current.get("updated_at")
                    or current.get("created_at")
                    or ""
                )
                att_record: dict[str, Any] = {
                    "job_id": str(current["job_id"]),
                    "attempt": b_attempt,
                    "user_id": auth_uid,
                    "workspace_id": auth_ws,
                    "thread_id": auth_th,
                    "status": new_status,
                    "ok": new_status == TranscriptionJobStatus.SUCCEEDED.value,
                    "duration_seconds": round(
                        float(merged_usage.get("duration_seconds") or 0.0), 3
                    ),
                    "submitted_seconds": round(
                        float(merged_usage.get("submitted_seconds") or 0.0), 3
                    ),
                    "completed_seconds": round(
                        float(merged_usage.get("completed_seconds") or 0.0), 3
                    ),
                    "billable_seconds": bill_val,
                    "segment_count": eff_seg_cnt,
                    "completed_segments": eff_comp_cnt,
                    "error_category": (
                        str(err_cat).strip()
                        if err_cat
                        and new_status != TranscriptionJobStatus.SUCCEEDED.value
                        else None
                    ),
                    "provider_request_ids": merged_req_ids,
                    "recorded_at": rec_ts,
                }
                if new_status == TranscriptionJobStatus.CANCELLED.value:
                    att_record["billing_verified"] = False
                attempts_by_num[b_attempt] = att_record
            else:
                merged_usage["status"] = str(existing_att.get("status") or cur_status)
                merged_usage["ok"] = bool(existing_att.get("ok"))

        if attempts_by_num:
            ordered_attempts = [
                attempts_by_num[k] for k in sorted(attempts_by_num)
            ]
            merged_usage["attempts"] = ordered_attempts
            merged_usage["summary"] = {
                "user_id": auth_uid,
                "workspace_id": auth_ws,
                "attempt_count": len(ordered_attempts),
                "succeeded_attempts": sum(
                    1
                    for a in ordered_attempts
                    if a.get("ok") is True or a.get("status") == "succeeded"
                ),
                "failed_attempts": sum(
                    1
                    for a in ordered_attempts
                    if not a.get("ok") and a.get("status") != "cancelled"
                ),
                "cancelled_attempts": sum(
                    1
                    for a in ordered_attempts
                    if a.get("status") == "cancelled"
                ),
                "duration_seconds": round(
                    sum(float(a.get("duration_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "submitted_seconds": round(
                    sum(float(a.get("submitted_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "completed_seconds": round(
                    sum(float(a.get("completed_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "billable_seconds": round(
                    sum(
                        float(a.get("billable_seconds") or 0.0)
                        for a in ordered_attempts
                        if isinstance(a.get("billable_seconds"), (int, float))
                    ),
                    3,
                ),
                "segment_count": sum(
                    int(a.get("segment_count") or 0) for a in ordered_attempts
                ),
                "completed_segments": sum(
                    int(a.get("completed_segments") or 0) for a in ordered_attempts
                ),
            }

        row_req_ids = (
            merged_req_ids
            if merged_req_ids
            else (req_ids if isinstance(req_ids, list) else None)
        )

        # Skip write if nothing meaningful changed
        if (
            new_status == cur_status
            and new_stage == str(current.get("stage") or "")
            and (seg_cnt is None or seg_cnt == current.get("segment_count"))
            and (comp_cnt is None or comp_cnt == current.get("completed_segments"))
            and (row_req_ids is None or row_req_ids == current.get("provider_request_ids"))
            and merged_usage == current.get("usage")
            and (res_key is None or res_key == current.get("result_object_key"))
            and (err_cat is None or err_cat == current.get("error_category"))
        ):
            return current

        updated, latest = await db.update_transcription_job_versioned(
            str(current["job_id"]),
            expected_version=int(current["version"]),
            status=new_status,
            stage=new_stage,
            segment_count=int(seg_cnt) if isinstance(seg_cnt, int) else None,
            completed_segments=int(comp_cnt) if isinstance(comp_cnt, int) else None,
            provider_request_ids=row_req_ids,
            usage=merged_usage,
            result_object_key=str(res_key) if isinstance(res_key, str) and res_key else None,
            error_category=str(err_cat) if isinstance(err_cat, str) and err_cat else None,
            error_message=str(err_msg) if isinstance(err_msg, str) and err_msg else None,
            clear_error=can_reset_terminal
            or new_status == TranscriptionJobStatus.SUCCEEDED.value,
        )
        if latest is None:
            return current
        if updated:
            return latest
        current = latest
    return current


async def prepare_transcription_job(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str | None,
    source_filename: str,
    source_size_bytes: int,
    source_sha256: str,
    language: str | None = None,
    hotwords: list[str] | str | None = None,
    source_object_key: str | None = None,
    workspace_access_verified: bool = False,
    ticket_ttl_s: int = DEFAULT_TICKET_TTL_S,
) -> tuple[dict[str, Any], str, int]:
    """Create (or idempotently reuse) a transcription job and issue its scoped ticket.

    Returns `(job_row, ticket_token, expires_at_unix)`.
    """
    clean_user = await require_internal_transcription_caller(user_id)
    clean_ws = str(workspace_id or "").strip()
    if not clean_ws or clean_ws.lower() == "home":
        raise HTTPException(status_code=400, detail="Valid workspace_id is required")

    if not workspace_access_verified:
        await verify_creator_workspace_access(clean_ws, clean_user)

    size_val = int(source_size_bytes)
    if size_val <= 0:
        raise HTTPException(
            status_code=400, detail="Audio file is empty (source_size_bytes must be > 0)"
        )
    if size_val > MAX_TRANSCRIPTION_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file exceeds 2 GiB limit ({size_val} bytes)",
        )

    clean_sha = str(source_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean_sha):
        raise HTTPException(
            status_code=400, detail="source_sha256 must be a 64-character hex SHA-256 digest"
        )

    norm_hw = normalize_hotwords(hotwords)
    clean_lang = str(language).strip() if language and str(language).strip() else None
    idemp_key = derive_transcription_idempotency_key(
        user_id=clean_user,
        workspace_id=clean_ws,
        thread_id=thread_id,
        source_sha256=clean_sha,
        language=clean_lang,
        hotwords=norm_hw,
    )

    candidate_job_id = str(uuid4())
    if source_object_key:
        obj_key = validate_source_object_key(source_object_key, workspace_id=clean_ws)
    else:
        obj_key = build_transcription_source_object_key(
            clean_ws, candidate_job_id, source_filename
        )

    job = await db.create_transcription_job(
        job_id=candidate_job_id,
        created_by_user_id=clean_user,
        workspace_id=clean_ws,
        thread_id=thread_id or "",
        source_object_key=obj_key,
        source_size_bytes=size_val,
        source_sha256=clean_sha,
        idempotency_key=idemp_key,
        language=clean_lang,
        hotwords=norm_hw,
        status=TranscriptionJobStatus.AWAITING_UPLOAD,
        stage=TranscriptionJobStatus.AWAITING_UPLOAD.value,
    )

    ticket_token, exp_ts = issue_transcription_ticket(
        job_id=str(job["job_id"]),
        user_id=str(job["created_by_user_id"]),
        workspace_id=str(job["workspace_id"]),
        thread_id=str(job.get("thread_id") or ""),
        source_object_key=str(job["source_object_key"]),
        source_size_bytes=int(job["source_size_bytes"]),
        source_sha256=str(job["source_sha256"]),
        ttl_s=ticket_ttl_s,
    )
    return job, ticket_token, exp_ts


def _reject_smuggled_upload_key_or_url(
    raw_body: Mapping[str, Any] | None,
) -> None:
    """Reject any request body that attempts to pass an object key or URL to an upload endpoint."""
    if not isinstance(raw_body, Mapping):
        return
    for forbidden_key in ("source_object_key", "object_key", "key", "url", "upload_url", "bucket"):
        val = raw_body.get(forbidden_key)
        if val is not None and str(val).strip() != "":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Request body field '{forbidden_key}' is not accepted; "
                    "object key is strictly taken from the ticket-bound source_object_key"
                ),
            )


async def _load_job_for_ticket_upload(
    *,
    job_id: str,
    caller_user_id: str,
    ticket_claims: dict[str, Any] | None,
    raw_body: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str, int, int]:
    """Verify ticket `upload` scope, object storage availability, `awaiting_upload` status, and bound object key."""
    if ticket_claims is None:
        raise HTTPException(
            status_code=403,
            detail="Audio upload endpoints require a task ticket with 'upload' operation",
        )
    _reject_smuggled_upload_key_or_url(raw_body)

    clean_caller = await require_internal_transcription_caller(caller_user_id)
    bound_job_id = str(ticket_claims["job_id"]).strip()
    if str(job_id or "").strip() != bound_job_id:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    if not is_storage_enabled():
        raise HTTPException(
            status_code=503,
            detail="Object storage is not enabled; audio upload cannot proceed",
        )

    job = await db.get_transcription_job(bound_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    await authorize_job_access(job, clean_caller, need="read")
    if str(job.get("created_by_user_id") or "") != clean_caller:
        raise HTTPException(
            status_code=403,
            detail="Only the job creator may upload audio for this transcription job",
        )

    cur_status = str(job.get("status") or "")
    if cur_status != TranscriptionJobStatus.AWAITING_UPLOAD.value:
        raise HTTPException(
            status_code=409,
            detail=f"Audio upload is only allowed when job status is 'awaiting_upload' (current: '{cur_status}')",
        )

    bound_key = str(ticket_claims["source_object_key"]).strip()
    job_key = str(job.get("source_object_key") or "").strip()
    if not bound_key or bound_key != job_key:
        raise HTTPException(
            status_code=403,
            detail="Ticket source_object_key does not match job record",
        )

    bound_size = int(ticket_claims["source_size_bytes"])
    job_size = int(job.get("source_size_bytes") or 0)
    if bound_size <= 0 or bound_size != job_size:
        raise HTTPException(
            status_code=403,
            detail="Ticket source_size_bytes does not match job record",
        )
    if bound_size > MAX_TRANSCRIPTION_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file exceeds 2 GiB limit ({bound_size} bytes)",
        )

    part_count = max(1, math.ceil(bound_size / TRANSCRIPTION_UPLOAD_PART_BYTES))
    if part_count > MAX_TRANSCRIPTION_UPLOAD_PARTS:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file requires {part_count} parts, exceeding {MAX_TRANSCRIPTION_UPLOAD_PARTS} parts limit",
        )

    return job, bound_key, bound_size, part_count


async def _inspect_existing_upload_state(
    *,
    job: dict[str, Any],
    bound_key: str,
    bound_size: int,
    part_count: int,
) -> dict[str, Any] | None:
    """Return an existing completed or resumable upload response from `job`, or None if a new upload is needed.

    Raises 503 when `job` records an unfinished, un-aborted upload whose parts cannot
    be listed: that must never be read as "no upload", or a concurrent writer's
    `upload_id` would be silently overwritten by a fresh one.
    """
    cur_usage = dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
    upload_state = (
        dict(cur_usage.get("upload"))
        if isinstance(cur_usage.get("upload"), dict)
        else {}
    )
    existing_upload_id = str(upload_state.get("upload_id") or "").strip()

    if bool(upload_state.get("completed")) and not upload_state.get("aborted"):
        actual_obj_size = await asyncio.to_thread(object_size, bound_key)
        if actual_obj_size == bound_size:
            return {
                "ok": True,
                "job_id": str(job["job_id"]),
                "upload_id": existing_upload_id,
                "part_size": TRANSCRIPTION_UPLOAD_PART_BYTES,
                "part_count": part_count,
                "source_size_bytes": bound_size,
                "size_bytes": actual_obj_size,
                "object_size": actual_obj_size,
                "expires_in": TRANSCRIPTION_UPLOAD_URL_EXPIRES_S,
                "uploaded_parts": [],
                "resumed": False,
                "completed": True,
                "already_completed": True,
            }

    if (
        existing_upload_id
        and not upload_state.get("aborted")
        and not upload_state.get("completed")
    ):
        try:
            listed = await asyncio.to_thread(
                list_uploaded_parts, bound_key, existing_upload_id
            )
        except Exception:
            listed = None
        if listed is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Object storage is temporarily unavailable while checking the "
                    "in-flight multipart upload; retry shortly"
                ),
            )
        uploaded_parts = [
            {
                "part_number": int(p["part_number"]),
                "size": int(p["size"]),
                "etag": str(p.get("etag") or ""),
            }
            for p in listed
            if isinstance(p, Mapping) and int(p.get("part_number") or 0) >= 1
        ]
        return {
            "ok": True,
            "job_id": str(job["job_id"]),
            "upload_id": existing_upload_id,
            "part_size": TRANSCRIPTION_UPLOAD_PART_BYTES,
            "part_count": part_count,
            "source_size_bytes": bound_size,
            "expires_in": TRANSCRIPTION_UPLOAD_URL_EXPIRES_S,
            "uploaded_parts": uploaded_parts,
            "resumed": True,
        }
    return None


async def start_transcription_upload(
    *,
    job_id: str,
    caller_user_id: str,
    ticket_claims: dict[str, Any] | None,
    content_type: str | None = None,
    raw_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start or resume a multipart upload for `ticket_claims['source_object_key']` when job is `awaiting_upload`."""
    job, bound_key, bound_size, part_count = await _load_job_for_ticket_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        raw_body=raw_body,
    )

    existing_resp = await _inspect_existing_upload_state(
        job=job,
        bound_key=bound_key,
        bound_size=bound_size,
        part_count=part_count,
    )
    if existing_resp is not None:
        return existing_resp

    eff_ct = (
        str(content_type).strip()
        if content_type and str(content_type).strip()
        else "application/octet-stream"
    )
    new_upload_id = await asyncio.to_thread(
        open_multipart_upload, bound_key, content_type=eff_ct
    )
    if not new_upload_id:
        raise HTTPException(
            status_code=502,
            detail="Failed opening multipart upload in object storage",
        )

    persisted = False
    for _ in range(3):
        if str(job.get("status") or "") != TranscriptionJobStatus.AWAITING_UPLOAD.value:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{job.get('status')}')"
                ),
            )

        try:
            concurrent_resp = await _inspect_existing_upload_state(
                job=job,
                bound_key=bound_key,
                bound_size=bound_size,
                part_count=part_count,
            )
        except HTTPException:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            raise
        if concurrent_resp is not None:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            return concurrent_resp

        merged_usage = (
            dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
        )
        merged_usage["upload"] = {
            "upload_id": new_upload_id,
            "part_size": TRANSCRIPTION_UPLOAD_PART_BYTES,
            "part_count": part_count,
            "source_size_bytes": bound_size,
            "completed": False,
            "aborted": False,
        }
        updated, latest = await db.update_transcription_job_versioned(
            str(job["job_id"]),
            expected_version=int(job["version"]),
            status=TranscriptionJobStatus.AWAITING_UPLOAD,
            stage=TranscriptionJobStatus.AWAITING_UPLOAD.value,
            usage=merged_usage,
        )
        if updated:
            persisted = True
            if latest is not None:
                job = latest
            break
        if (
            latest is None
            or str(latest.get("status") or "")
            != TranscriptionJobStatus.AWAITING_UPLOAD.value
        ):
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            cur_st = str(latest.get("status") or "") if latest else "not_found"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{cur_st}')"
                ),
            )
        try:
            concurrent_resp = await _inspect_existing_upload_state(
                job=latest,
                bound_key=bound_key,
                bound_size=bound_size,
                part_count=part_count,
            )
        except HTTPException:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            raise
        if concurrent_resp is not None:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, new_upload_id
            )
            return concurrent_resp
        job = latest

    if not persisted:
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, new_upload_id
        )
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict while starting multipart upload",
        )

    return {
        "ok": True,
        "job_id": str(job["job_id"]),
        "upload_id": new_upload_id,
        "part_size": TRANSCRIPTION_UPLOAD_PART_BYTES,
        "part_count": part_count,
        "source_size_bytes": bound_size,
        "expires_in": TRANSCRIPTION_UPLOAD_URL_EXPIRES_S,
        "uploaded_parts": [],
        "resumed": False,
    }


async def sign_transcription_upload_part(
    *,
    job_id: str,
    caller_user_id: str,
    ticket_claims: dict[str, Any] | None,
    part_number: int,
    upload_id: str | None = None,
    raw_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Sign a 15-minute presigned PUT URL for part `part_number` of the ticket-bound `source_object_key`."""
    job, bound_key, bound_size, part_count = await _load_job_for_ticket_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        raw_body=raw_body,
    )

    pnum = int(part_number)
    if pnum < 1 or pnum > part_count:
        raise HTTPException(
            status_code=400,
            detail=f"part_number must be between 1 and {part_count} (got {pnum})",
        )

    cur_usage = dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
    upload_state = (
        dict(cur_usage.get("upload"))
        if isinstance(cur_usage.get("upload"), dict)
        else {}
    )
    active_upload_id = str(upload_state.get("upload_id") or "").strip()
    if not active_upload_id or upload_state.get("aborted"):
        raise HTTPException(
            status_code=409,
            detail="Multipart upload has not been started for this job",
        )
    if upload_id and str(upload_id).strip() != active_upload_id:
        raise HTTPException(
            status_code=409,
            detail="Supplied upload_id does not match active multipart upload",
        )

    if pnum < part_count:
        expected_part_size = TRANSCRIPTION_UPLOAD_PART_BYTES
    else:
        expected_part_size = bound_size - TRANSCRIPTION_UPLOAD_PART_BYTES * (
            part_count - 1
        )

    signed_url = await asyncio.to_thread(
        sign_upload_part,
        bound_key,
        active_upload_id,
        part_number=pnum,
        size=expected_part_size,
        expires_in=TRANSCRIPTION_UPLOAD_URL_EXPIRES_S,
        for_browser=False,
    )
    if not signed_url:
        raise HTTPException(
            status_code=502,
            detail=f"Failed signing upload URL for part {pnum}",
        )

    return {
        "ok": True,
        "job_id": str(job["job_id"]),
        "upload_id": active_upload_id,
        "part_number": pnum,
        "part_size": expected_part_size,
        "upload_url": signed_url,
        "expires_in": TRANSCRIPTION_UPLOAD_URL_EXPIRES_S,
    }


async def abort_transcription_upload(
    *,
    job_id: str,
    caller_user_id: str,
    ticket_claims: dict[str, Any] | None,
    upload_id: str | None = None,
    reason: str | None = None,
    raw_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Abort an in-progress multipart upload for `job_id` without completing it."""
    job, bound_key, _bound_size, _part_count = await _load_job_for_ticket_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        raw_body=raw_body,
    )

    cur_usage = dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
    upload_state = (
        dict(cur_usage.get("upload"))
        if isinstance(cur_usage.get("upload"), dict)
        else {}
    )
    active_upload_id = str(upload_id or upload_state.get("upload_id") or "").strip()
    if active_upload_id:
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, active_upload_id
        )

    persisted = False
    for _ in range(3):
        if str(job.get("status") or "") != TranscriptionJobStatus.AWAITING_UPLOAD.value:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{job.get('status')}')"
                ),
            )
        merged_usage = (
            dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
        )
        prev_up = (
            dict(merged_usage.get("upload"))
            if isinstance(merged_usage.get("upload"), dict)
            else {}
        )
        prev_up["upload_id"] = None
        prev_up["completed"] = False
        prev_up["aborted"] = True
        if reason:
            prev_up["abort_reason"] = str(reason).strip()
        merged_usage["upload"] = prev_up
        updated, latest = await db.update_transcription_job_versioned(
            str(job["job_id"]),
            expected_version=int(job["version"]),
            status=TranscriptionJobStatus.AWAITING_UPLOAD,
            stage=TranscriptionJobStatus.AWAITING_UPLOAD.value,
            usage=merged_usage,
        )
        if updated:
            persisted = True
            if latest is not None:
                job = latest
            break
        if (
            latest is None
            or str(latest.get("status") or "")
            != TranscriptionJobStatus.AWAITING_UPLOAD.value
        ):
            cur_st = str(latest.get("status") or "") if latest else "not_found"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{cur_st}')"
                ),
            )
        job = latest

    if not persisted:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict while aborting multipart upload",
        )

    return {
        "ok": True,
        "job_id": str(job["job_id"]),
        "aborted": True,
        "reason": str(reason).strip() if reason else None,
    }


async def complete_transcription_upload(
    *,
    job_id: str,
    caller_user_id: str,
    ticket_claims: dict[str, Any] | None,
    upload_id: str | None = None,
    abort: bool = False,
    raw_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete a multipart upload and verify `object_size == source_size_bytes` (aborting on mismatch)."""
    if abort:
        return await abort_transcription_upload(
            job_id=job_id,
            caller_user_id=caller_user_id,
            ticket_claims=ticket_claims,
            upload_id=upload_id,
            raw_body=raw_body,
        )

    job, bound_key, bound_size, part_count = await _load_job_for_ticket_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        raw_body=raw_body,
    )

    cur_usage = dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
    upload_state = (
        dict(cur_usage.get("upload"))
        if isinstance(cur_usage.get("upload"), dict)
        else {}
    )
    active_upload_id = str(upload_state.get("upload_id") or "").strip()

    # Item 3: Idempotent complete — if the job row already recorded `upload.completed=true`
    # and `object_size == source_size_bytes`, return immediately without listing parts.
    if bool(upload_state.get("completed")) and not upload_state.get("aborted"):
        if upload_id and active_upload_id and str(upload_id).strip() != active_upload_id:
            raise HTTPException(
                status_code=409,
                detail="Supplied upload_id does not match completed multipart upload",
            )
        verified_size = await asyncio.to_thread(object_size, bound_key)
        if verified_size == bound_size:
            return {
                "ok": True,
                "job_id": str(job["job_id"]),
                "upload_id": active_upload_id or (str(upload_id).strip() if upload_id else ""),
                "completed": True,
                "already_completed": True,
                "size_bytes": verified_size,
                "object_size": verified_size,
            }

    if not active_upload_id or upload_state.get("aborted"):
        raise HTTPException(
            status_code=409,
            detail="Multipart upload has not been started for this job",
        )
    if upload_id and str(upload_id).strip() != active_upload_id:
        raise HTTPException(
            status_code=409,
            detail="Supplied upload_id does not match active multipart upload",
        )

    uploaded = await asyncio.to_thread(
        list_uploaded_parts, bound_key, active_upload_id
    )
    if uploaded is None:
        raise HTTPException(
            status_code=502,
            detail="Failed listing uploaded parts from object storage",
        )

    parts_by_num: dict[int, dict[str, Any]] = {}
    for p in uploaded:
        if not isinstance(p, Mapping):
            continue
        pnum = int(p.get("part_number") or 0)
        if 1 <= pnum <= part_count:
            parts_by_num[pnum] = {
                "part_number": pnum,
                "size": int(p.get("size") or 0),
                "etag": str(p.get("etag") or ""),
            }

    total_uploaded_bytes = sum(int(item["size"]) for item in parts_by_num.values())
    missing_parts = [n for n in range(1, part_count + 1) if n not in parts_by_num]
    non_final_size_mismatch = any(
        int(parts_by_num[n]["size"]) != TRANSCRIPTION_UPLOAD_PART_BYTES
        for n in range(1, part_count)
        for _ in (0,)
        if n in parts_by_num
    )

    if (
        missing_parts
        or non_final_size_mismatch
        or total_uploaded_bytes != bound_size
    ):
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, active_upload_id
        )
        await abort_transcription_upload(
            job_id=job_id,
            caller_user_id=caller_user_id,
            ticket_claims=ticket_claims,
            upload_id=active_upload_id,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Multipart upload size or part verification failed "
                f"(uploaded_bytes={total_uploaded_bytes}, expected={bound_size}, "
                f"missing_parts={missing_parts}); upload aborted"
            ),
        )

    parts_to_complete = [
        (n, str(parts_by_num[n]["etag"])) for n in range(1, part_count + 1)
    ]
    completed_ok = await asyncio.to_thread(
        complete_multipart_upload,
        bound_key,
        active_upload_id,
        parts_to_complete,
    )
    if not completed_ok:
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, active_upload_id
        )
        raise HTTPException(
            status_code=502,
            detail="Failed completing multipart upload in object storage",
        )

    actual_obj_size = await asyncio.to_thread(object_size, bound_key)
    if actual_obj_size != bound_size:
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, active_upload_id
        )
        await asyncio.to_thread(delete_object, bound_key)
        await abort_transcription_upload(
            job_id=job_id,
            caller_user_id=caller_user_id,
            ticket_claims=ticket_claims,
            upload_id=active_upload_id,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Completed object size ({actual_obj_size}) does not match "
                f"expected source_size_bytes ({bound_size}); upload aborted"
            ),
        )

    persisted = False
    for _ in range(3):
        if str(job.get("status") or "") != TranscriptionJobStatus.AWAITING_UPLOAD.value:
            await asyncio.to_thread(
                abort_multipart_upload, bound_key, active_upload_id
            )
            await asyncio.to_thread(delete_object, bound_key)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{job.get('status')}')"
                ),
            )
        merged_usage = (
            dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
        )
        merged_usage["upload"] = {
            "upload_id": active_upload_id,
            "part_size": TRANSCRIPTION_UPLOAD_PART_BYTES,
            "part_count": part_count,
            "source_size_bytes": bound_size,
            "verified_size_bytes": actual_obj_size,
            "completed": True,
            "aborted": False,
        }
        updated, latest = await db.update_transcription_job_versioned(
            str(job["job_id"]),
            expected_version=int(job["version"]),
            status=TranscriptionJobStatus.AWAITING_UPLOAD,
            stage=TranscriptionJobStatus.AWAITING_UPLOAD.value,
            usage=merged_usage,
        )
        if updated:
            persisted = True
            if latest is not None:
                job = latest
            break
        if (
            latest is None
            or str(latest.get("status") or "")
            != TranscriptionJobStatus.AWAITING_UPLOAD.value
        ):
            cur_st = str(latest.get("status") or "") if latest else "not_found"
            # The synthesized object must never be deleted here: a `latest` past
            # `awaiting_upload` (e.g. `queued`) belongs to a submit that still has
            # to download it, and terminal states clean up their own source object.
            if (
                latest is not None
                and cur_st not in (
                    TranscriptionJobStatus.CANCELLED.value,
                    TranscriptionJobStatus.FAILED.value,
                )
                and await asyncio.to_thread(object_size, bound_key) == bound_size
            ):
                return {
                    "ok": True,
                    "job_id": str(latest["job_id"]),
                    "upload_id": active_upload_id,
                    "completed": True,
                    "already_completed": True,
                    "size_bytes": actual_obj_size,
                    "object_size": actual_obj_size,
                }
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Transcription upload is only allowed in status 'awaiting_upload' "
                    f"(current: '{cur_st}')"
                ),
            )
        latest_up = (
            (latest.get("usage") or {}).get("upload")
            if isinstance(latest.get("usage"), dict)
            and isinstance((latest.get("usage") or {}).get("upload"), dict)
            else {}
        )
        if bool(latest_up.get("completed")) and not latest_up.get("aborted"):
            if await asyncio.to_thread(object_size, bound_key) == bound_size:
                return {
                    "ok": True,
                    "job_id": str(latest["job_id"]),
                    "upload_id": str(latest_up.get("upload_id") or active_upload_id),
                    "completed": True,
                    "already_completed": True,
                    "size_bytes": actual_obj_size,
                    "object_size": actual_obj_size,
                }
        job = latest

    if not persisted:
        await asyncio.to_thread(
            abort_multipart_upload, bound_key, active_upload_id
        )
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict while completing multipart upload",
        )

    return {
        "ok": True,
        "job_id": str(job["job_id"]),
        "upload_id": active_upload_id,
        "completed": True,
        "already_completed": False,
        "size_bytes": actual_obj_size,
        "object_size": actual_obj_size,
    }


async def submit_transcription_job(
    *,
    caller_user_id: str,
    job_id: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    source_object_key: str | None = None,
    source_size_bytes: int | None = None,
    source_sha256: str | None = None,
    language: str | None = None,
    hotwords: list[str] | str | None = None,
    ticket_claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a prepared (or newly specified) transcription job to the host bridge.

    Accounting identity is always `caller_user_id` (from verified ticket or backend
    auth), never from `X-Dalton-*` headers or untrusted body fields.
    """
    clean_caller = await require_internal_transcription_caller(caller_user_id)

    if ticket_claims is not None:
        bound_job_id = str(ticket_claims["job_id"]).strip()
        if job_id and str(job_id).strip() != bound_job_id:
            raise HTTPException(status_code=404, detail="Transcription job not found")
        job_id = bound_job_id
        # Verify any supplied object/size/hash/workspace parameters match the ticket binding
        if workspace_id and str(workspace_id).strip() != ticket_claims["workspace_id"]:
            raise HTTPException(
                status_code=403, detail="workspace_id does not match ticket binding"
            )
        if (
            source_object_key
            and str(source_object_key).strip() != ticket_claims["source_object_key"]
        ):
            raise HTTPException(
                status_code=403,
                detail="source_object_key does not match ticket binding",
            )
        if (
            source_size_bytes is not None
            and int(source_size_bytes) != int(ticket_claims["source_size_bytes"])
        ):
            raise HTTPException(
                status_code=403,
                detail="source_size_bytes does not match ticket binding",
            )
        if (
            source_sha256
            and str(source_sha256).strip().lower() != ticket_claims["source_sha256"]
        ):
            raise HTTPException(
                status_code=403, detail="source_sha256 does not match ticket binding"
            )

    job: dict[str, Any] | None = None
    if job_id:
        job = await db.get_transcription_job(str(job_id).strip())
        if job is None:
            raise HTTPException(status_code=404, detail="Transcription job not found")
        await authorize_job_access(job, clean_caller, need="read")
        if str(job.get("created_by_user_id") or "") != clean_caller:
            raise HTTPException(
                status_code=403,
                detail="Only the job creator may submit this transcription job",
            )
    else:
        if not workspace_id or not source_object_key or source_size_bytes is None or not source_sha256:
            raise HTTPException(
                status_code=400,
                detail="Either job_id or (workspace_id, source_object_key, source_size_bytes, source_sha256) is required",
            )
        job, _tok, _exp = await prepare_transcription_job(
            user_id=clean_caller,
            workspace_id=workspace_id,
            thread_id=thread_id,
            source_filename=Path(source_object_key).name,
            source_size_bytes=int(source_size_bytes),
            source_sha256=source_sha256,
            language=language,
            hotwords=hotwords,
            source_object_key=source_object_key,
        )

    if str(job.get("status") or "") == TranscriptionJobStatus.CANCELLED.value:
        raise HTTPException(
            status_code=409, detail="Cancelled transcription job cannot be submitted"
        )

    # Item 1: Before submitting to the bridge, verify object storage is enabled and
    # the source audio object has completed upload with exact matching byte size.
    if not is_storage_enabled():
        raise HTTPException(
            status_code=503,
            detail="Object storage is not enabled; transcription cannot be submitted",
        )
    expected_obj_size = int(job["source_size_bytes"])
    actual_obj_size = await asyncio.to_thread(
        object_size, str(job["source_object_key"])
    )
    if actual_obj_size != expected_obj_size:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Source audio object upload is incomplete or missing in object storage "
                f"(actual_size={actual_obj_size}, expected_size={expected_obj_size})"
            ),
        )

    eff_lang = (
        str(language).strip()
        if language and str(language).strip()
        else job.get("language")
    )
    eff_hotwords = (
        normalize_hotwords(hotwords)
        if hotwords is not None
        else list(job.get("hotwords") or [])
    )

    bridge_payload: dict[str, Any] = {
        "job_id": str(job["job_id"]),
        "caller": {
            "user_id": str(job["created_by_user_id"]),
            "workspace_id": str(job["workspace_id"]),
            "thread_id": str(job.get("thread_id") or ""),
        },
        "source_object_key": str(job["source_object_key"]),
        "source_size_bytes": int(job["source_size_bytes"]),
        "source_sha256": str(job["source_sha256"]),
        "language": eff_lang,
        "hotwords": eff_hotwords,
        "idempotency_key": str(job["idempotency_key"]),
    }

    try:
        code, resp = await call_bridge_submit_transcription(bridge_payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Bridge transcription submit failed for job_id=%s: %s",
            job["job_id"],
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Bridge transcription service unreachable: {type(exc).__name__}",
        ) from exc

    if code >= 400 or resp.get("ok") is False:
        detail = str(resp.get("error") or f"Bridge returned HTTP {code}")
        raise HTTPException(status_code=502 if code >= 500 else code, detail=detail)

    bridge_job = resp.get("job") if isinstance(resp.get("job"), dict) else {
        "status": resp.get("status") or TranscriptionJobStatus.QUEUED.value,
        "stage": resp.get("stage") or TranscriptionJobStatus.QUEUED.value,
        "segment_count": resp.get("segment_count", 1),
        "completed_segments": resp.get("completed_segments", 0),
    }
    synced = await _sync_job_from_bridge_state(job, bridge_job)
    return synced


async def get_transcription_job_status(
    *,
    job_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Fetch status of `job_id` for `caller_user_id`, syncing non-terminal jobs from the bridge."""
    job = await db.get_transcription_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    await authorize_job_access(job, caller_user_id, need="read")

    bridge_sync_status = "settled"
    if str(job.get("status") or "") not in TERMINAL_TRANSCRIPTION_STATUSES and str(
        job.get("status") or ""
    ) != TranscriptionJobStatus.AWAITING_UPLOAD.value:
        try:
            code, resp = await call_bridge_get_transcription(str(job["job_id"]))
            if code == 200 and isinstance(resp.get("job"), dict):
                job = await _sync_job_from_bridge_state(job, resp["job"])
                bridge_sync_status = "ok"
            else:
                bridge_sync_status = "degraded"
        except Exception as exc:
            logger.warning(
                "Bridge status poll failed for job_id=%s (returning last confirmed DB state): %s",
                job_id,
                type(exc).__name__,
            )
            bridge_sync_status = "unreachable"

    return {
        "ok": True,
        "job": job,
        "bridge_sync_status": bridge_sync_status,
    }


async def get_transcription_job_result(
    *,
    job_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Fetch transcription result for `job_id` if authorized and `succeeded`."""
    status_res = await get_transcription_job_status(
        job_id=job_id, caller_user_id=caller_user_id
    )
    job = status_res["job"]
    cur_status = str(job.get("status") or "")
    if cur_status != TranscriptionJobStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Transcription result is not ready (current status: {cur_status})",
        )

    try:
        code, resp = await call_bridge_get_transcription_result(str(job["job_id"]))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Bridge result fetch failed for job_id=%s: %s", job_id, type(exc).__name__
        )
        raise HTTPException(
            status_code=502,
            detail=f"Bridge transcription result unavailable: {type(exc).__name__}",
        ) from exc

    if code == 404:
        raise HTTPException(status_code=404, detail="Transcription result not found")
    if code >= 400 or resp.get("ok") is False:
        detail = str(resp.get("error") or f"Bridge returned HTTP {code}")
        raise HTTPException(status_code=502 if code >= 500 else code, detail=detail)

    return {
        "ok": True,
        "job_id": str(job["job_id"]),
        "status": cur_status,
        "workspace_id": str(job["workspace_id"]),
        "created_by_user_id": str(job["created_by_user_id"]),
        "result_object_key": job.get("result_object_key") or resp.get("result_object_key"),
        "usage": job.get("usage") or resp.get("usage") or {},
        "result": resp.get("result") or {},
    }


async def stream_transcription_job_raw_result(
    *,
    job_id: str,
    caller_user_id: str,
) -> AsyncIterator[bytes]:
    """Authorize `caller_user_id` (`result` op) and stream the bridge's raw provider JSON for `job_id`."""
    status_res = await get_transcription_job_status(
        job_id=job_id, caller_user_id=caller_user_id
    )
    job = status_res["job"]
    cur_status = str(job.get("status") or "")
    if cur_status != TranscriptionJobStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Transcription raw result is not ready (current status: {cur_status})",
        )

    try:
        code, stream_or_err = await open_bridge_raw_result_stream(
            str(job["job_id"])
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Bridge raw result fetch failed for job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Bridge transcription raw result unavailable: {type(exc).__name__}",
        ) from exc

    if code == 404:
        raise HTTPException(
            status_code=404,
            detail="Raw provider transcription response is not available",
        )
    if code >= 400 or isinstance(stream_or_err, dict):
        err_dict = stream_or_err if isinstance(stream_or_err, dict) else {}
        detail = str(err_dict.get("error") or f"Bridge returned HTTP {code}")
        raise HTTPException(status_code=502 if code >= 500 else code, detail=detail)

    return stream_or_err


async def retry_transcription_job(
    *,
    job_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Retry a `failed` or `indeterminate` transcription job (creator only).

    Only the job's original creator (`created_by_user_id`) may retry a job so
    a new attempt is never billed to another user; non-creators (including
    colleagues with workspace write access) receive HTTP 404 Not Found.
    """
    clean_caller = await require_internal_transcription_caller(caller_user_id)
    job = await db.get_transcription_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    if str(job.get("created_by_user_id") or "").strip() != clean_caller:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    await authorize_job_access(job, clean_caller, need="read")

    cur_status = str(job.get("status") or "")
    if cur_status not in RETRYABLE_TRANSCRIPTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Transcription job in status '{cur_status}' cannot be retried",
        )

    try:
        code, resp = await call_bridge_action_transcription(
            str(job["job_id"]),
            "retry",
            payload={
                "caller": {
                    "user_id": str(job["created_by_user_id"]),
                    "workspace_id": str(job["workspace_id"]),
                    "thread_id": str(job.get("thread_id") or ""),
                }
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Bridge transcription retry failed: {type(exc).__name__}",
        ) from exc

    if code >= 400 or resp.get("ok") is False:
        detail = str(resp.get("error") or f"Bridge returned HTTP {code}")
        raise HTTPException(status_code=502 if code >= 500 else code, detail=detail)

    prev_att, prev_bver = _extract_db_attempt_version(job)
    bridge_job = (
        resp.get("job")
        if isinstance(resp.get("job"), dict)
        else {
            "status": TranscriptionJobStatus.QUEUED.value,
            "stage": TranscriptionJobStatus.QUEUED.value,
            "attempt": max(1, prev_att) + 1,
            "version": prev_bver + 1,
        }
    )
    updated = await _sync_job_from_bridge_state(
        job, bridge_job, allow_reset_from_terminal=True
    )
    return {"ok": True, "job": updated}


async def cancel_transcription_job(
    *,
    job_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Cancel a non-succeeded transcription job.

    Stops unsubmitted segments and reports honest cancellation/billing semantics
    for any segments already submitted to Volcengine Seed-ASR.
    """
    job = await db.get_transcription_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    await authorize_job_access(job, caller_user_id, need="write")

    cur_status = str(job.get("status") or "")
    if cur_status == TranscriptionJobStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=409,
            detail="Succeeded transcription job cannot be cancelled",
        )
    if cur_status == TranscriptionJobStatus.CANCELLED.value:
        cur_usage = job.get("usage") if isinstance(job.get("usage"), dict) else {}
        return {
            "ok": True,
            "job": job,
            "cancellation": cur_usage.get("cancellation")
            or {
                "submitted_segments_count": len(job.get("provider_request_ids") or []),
                "submitted_request_ids": list(job.get("provider_request_ids") or []),
                "unsubmitted_cancelled_count": 0,
                "provider_cancel_verified": False,
                "billing_verified": False,
                "notice": PROVIDER_CANCEL_UNVERIFIED_NOTICE,
            },
        }

    bridge_cancellation: dict[str, Any] | None = None
    bridge_job_snapshot: dict[str, Any] | None = None
    if cur_status != TranscriptionJobStatus.AWAITING_UPLOAD.value:
        try:
            code, resp = await call_bridge_action_transcription(
                str(job["job_id"]), "cancel"
            )
            if code >= 400 and code != 404:
                detail = str(resp.get("error") or f"Bridge returned HTTP {code}")
                raise HTTPException(
                    status_code=502 if code >= 500 else code, detail=detail
                )
            if isinstance(resp.get("job"), dict):
                bridge_job_snapshot = dict(resp["job"])
            if isinstance(resp.get("cancellation"), dict):
                bridge_cancellation = dict(resp["cancellation"])
            elif bridge_job_snapshot is not None and isinstance(
                bridge_job_snapshot.get("cancellation"), dict
            ):
                bridge_cancellation = dict(bridge_job_snapshot["cancellation"])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Bridge transcription cancel failed: {type(exc).__name__}",
            ) from exc

    if bridge_cancellation is None:
        req_ids = [
            str(r).strip()
            for r in (job.get("provider_request_ids") or [])
            if str(r).strip()
        ]
        total_segs = int(job.get("segment_count") or 0)
        comp_segs = int(job.get("completed_segments") or 0)
        unsub_cnt = max(0, total_segs - comp_segs - len(req_ids))
        bridge_cancellation = {
            "submitted_segments_count": len(req_ids),
            "submitted_request_ids": req_ids,
            "unsubmitted_cancelled_count": unsub_cnt,
            "completed_segments_count": comp_segs,
            "provider_cancel_verified": False,
            "billing_verified": False,
            "notice": PROVIDER_CANCEL_UNVERIFIED_NOTICE,
        }

    for _ in range(3):
        auth_uid = str(job.get("created_by_user_id") or "")
        auth_ws = str(job.get("workspace_id") or "")
        auth_th = str(job.get("thread_id") or "")
        merged_usage = (
            dict(job.get("usage")) if isinstance(job.get("usage"), dict) else {}
        )
        prev_attempts_list = [
            dict(a)
            for a in (merged_usage.get("attempts") or [])
            if isinstance(a, dict)
        ]
        extracted_prev_attempts = db.extract_job_usage_attempts(job)
        if bridge_job_snapshot is not None and isinstance(
            bridge_job_snapshot.get("usage"), dict
        ):
            for k, v in bridge_job_snapshot["usage"].items():
                if k not in (
                    "user_id",
                    "workspace_id",
                    "thread_id",
                    "upload",
                    "attempts",
                    "summary",
                ):
                    merged_usage[k] = v
        merged_usage["user_id"] = auth_uid
        merged_usage["workspace_id"] = auth_ws
        merged_usage["thread_id"] = auth_th
        merged_usage["cancellation"] = dict(bridge_cancellation)

        cur_att = int(
            (bridge_job_snapshot or {}).get("attempt")
            or merged_usage.get("attempt")
            or 1
        )
        attempts_by_num: dict[int, dict[str, Any]] = {}
        for item in prev_attempts_list:
            att_n = int(item.get("attempt") or 0)
            att_st = str(item.get("status") or "").strip()
            if (
                att_n >= 1
                and att_st in ("succeeded", "failed", "cancelled")
                and att_n not in attempts_by_num
            ):
                fixed = dict(item)
                fixed.pop("date", None)
                fixed["job_id"] = str(job["job_id"])
                fixed["user_id"] = auth_uid
                fixed["workspace_id"] = auth_ws
                fixed["thread_id"] = auth_th
                attempts_by_num[att_n] = fixed

        for item in extracted_prev_attempts:
            att_n = int(item.get("attempt") or 0)
            if att_n >= 1 and att_n not in attempts_by_num:
                fixed = dict(item)
                fixed.pop("date", None)
                fixed["job_id"] = str(job["job_id"])
                fixed["user_id"] = auth_uid
                fixed["workspace_id"] = auth_ws
                fixed["thread_id"] = auth_th
                attempts_by_num[att_n] = fixed

        if (
            bridge_job_snapshot is not None
            and isinstance(bridge_job_snapshot.get("usage"), dict)
            and isinstance(bridge_job_snapshot["usage"].get("attempts"), list)
        ):
            for item in bridge_job_snapshot["usage"]["attempts"]:
                if not isinstance(item, dict):
                    continue
                att_n = int(item.get("attempt") or 0)
                att_st = str(item.get("status") or "").strip()
                if (
                    att_n >= 1
                    and att_st in ("succeeded", "failed", "cancelled")
                    and att_n not in attempts_by_num
                ):
                    fixed = dict(item)
                    fixed.pop("date", None)
                    fixed["job_id"] = str(job["job_id"])
                    fixed["attempt"] = att_n
                    fixed["user_id"] = auth_uid
                    fixed["workspace_id"] = auth_ws
                    fixed["thread_id"] = auth_th
                    fixed["status"] = att_st
                    fixed["ok"] = att_st == "succeeded"
                    if att_st == "cancelled":
                        fixed["billing_verified"] = False
                    attempts_by_num[att_n] = fixed

        existing_att = attempts_by_num.get(cur_att)
        bridge_seg_req_ids = [
            s.get("provider_request_id")
            for s in ((bridge_job_snapshot or {}).get("segments") or [])
            if isinstance(s, dict) and s.get("provider_request_id")
        ]
        # Item 5: Merge request IDs via order-preserving deduplicated union
        # (existing job row / attempt IDs ∪ bridge snapshot / cancellation IDs).
        sub_req_ids = _merge_provider_request_ids(
            job.get("provider_request_ids"),
            (existing_att or {}).get("provider_request_ids"),
            (bridge_job_snapshot or {}).get("provider_request_ids"),
            bridge_seg_req_ids,
            bridge_cancellation.get("submitted_request_ids"),
        )
        sub_seg_cnt = int(
            bridge_cancellation.get("submitted_segments_count") or len(sub_req_ids)
        )
        sub_secs = float(merged_usage.get("submitted_seconds") or 0.0)

        # Item 6: The first usage record for a given attempt is final. If `cur_att`
        # already has a `succeeded`/`failed`/`cancelled` record, preserve its status,
        # date, and counts; only record `status="cancelled"` when `cur_att` has no record yet.
        if existing_att is not None:
            merged_usage["status"] = str(existing_att.get("status") or cur_status)
            merged_usage["ok"] = bool(existing_att.get("ok"))
        elif sub_seg_cnt > 0 or sub_req_ids or sub_secs > 0.0:
            rec_ts = str(
                bridge_cancellation.get("cancelled_at")
                or (bridge_job_snapshot or {}).get("completed_at")
                or (bridge_job_snapshot or {}).get("updated_at")
                or job.get("updated_at")
                or job.get("created_at")
                or ""
            )
            attempts_by_num[cur_att] = {
                "job_id": str(job["job_id"]),
                "attempt": cur_att,
                "user_id": auth_uid,
                "workspace_id": auth_ws,
                "thread_id": auth_th,
                "status": TranscriptionJobStatus.CANCELLED.value,
                "ok": False,
                "billing_verified": False,
                "duration_seconds": round(
                    float(merged_usage.get("duration_seconds") or 0.0), 3
                ),
                "submitted_seconds": round(sub_secs, 3),
                "completed_seconds": round(
                    float(merged_usage.get("completed_seconds") or 0.0), 3
                ),
                "billable_seconds": None,
                "segment_count": int(
                    (bridge_job_snapshot or {}).get("segment_count")
                    or job.get("segment_count")
                    or max(1, sub_seg_cnt)
                ),
                "completed_segments": int(
                    bridge_cancellation.get("completed_segments_count")
                    or (bridge_job_snapshot or {}).get("completed_segments")
                    or job.get("completed_segments")
                    or 0
                ),
                "error_category": None,
                "provider_request_ids": sub_req_ids,
                "recorded_at": rec_ts,
            }

        if attempts_by_num:
            ordered_attempts = [
                attempts_by_num[k] for k in sorted(attempts_by_num)
            ]
            merged_usage["attempts"] = ordered_attempts
            merged_usage["summary"] = {
                "user_id": auth_uid,
                "workspace_id": auth_ws,
                "attempt_count": len(ordered_attempts),
                "succeeded_attempts": sum(
                    1
                    for a in ordered_attempts
                    if a.get("ok") is True or a.get("status") == "succeeded"
                ),
                "failed_attempts": sum(
                    1
                    for a in ordered_attempts
                    if not a.get("ok") and a.get("status") != "cancelled"
                ),
                "cancelled_attempts": sum(
                    1
                    for a in ordered_attempts
                    if a.get("status") == "cancelled"
                ),
                "duration_seconds": round(
                    sum(float(a.get("duration_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "submitted_seconds": round(
                    sum(float(a.get("submitted_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "completed_seconds": round(
                    sum(float(a.get("completed_seconds") or 0.0) for a in ordered_attempts),
                    3,
                ),
                "billable_seconds": round(
                    sum(
                        float(a.get("billable_seconds") or 0.0)
                        for a in ordered_attempts
                        if isinstance(a.get("billable_seconds"), (int, float))
                    ),
                    3,
                ),
                "segment_count": sum(
                    int(a.get("segment_count") or 0) for a in ordered_attempts
                ),
                "completed_segments": sum(
                    int(a.get("completed_segments") or 0) for a in ordered_attempts
                ),
            }

        updated, latest = await db.update_transcription_job_versioned(
            str(job["job_id"]),
            expected_version=int(job["version"]),
            status=TranscriptionJobStatus.CANCELLED,
            stage=TranscriptionJobStatus.CANCELLED.value,
            provider_request_ids=sub_req_ids if sub_req_ids else None,
            usage=merged_usage,
        )
        if latest is None:
            break
        if updated or str(latest.get("status") or "") == TranscriptionJobStatus.CANCELLED.value:
            return {
                "ok": True,
                "job": latest,
                "cancellation": bridge_cancellation,
            }
        if str(latest.get("status") or "") == TranscriptionJobStatus.SUCCEEDED.value:
            raise HTTPException(
                status_code=409,
                detail="Succeeded transcription job cannot be cancelled",
            )
        job = latest

    return {
        "ok": True,
        "job": job,
        "cancellation": bridge_cancellation,
    }


def is_transcription_enabled() -> bool:
    """Return True only when the transcription master switch is on and object storage is enabled.

    `DALTON_TRANSCRIPTION_ENABLED` defaults to off; only "1"/"true"/"yes"/"on"
    (case-insensitive) turn it on. Production must configure MinIO and the
    Volcengine ASR/TOS credentials and verify them before any transcription
    surface (tool, skill, API, reconciliation) becomes visible.
    """
    flag = os.getenv("DALTON_TRANSCRIPTION_ENABLED", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return is_storage_enabled()


def is_transcription_reconciliation_enabled() -> bool:
    """Return True when transcription is enabled and background reconciliation is not switched off.

    `DALTON_TRANSCRIPTION_RECONCILE_ENABLED` keeps its own default-on kill
    switch; the transcription master switch (and Dalton mode) gate it from above.
    """
    if not is_transcription_enabled():
        return False
    if not get_dalton_settings().enabled:
        return False
    env_flag = os.getenv("DALTON_TRANSCRIPTION_RECONCILE_ENABLED", "1").strip().lower()
    return env_flag not in ("0", "false", "no", "off")


async def reconcile_active_transcription_jobs_once(
    *,
    limit: int = TRANSCRIPTION_RECONCILE_BATCH_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One periodic pass syncing up to `limit` (50) non-terminal jobs (including `indeterminate`) from the bridge.

    Rotates through non-terminal jobs using a `job_id` cursor (`job_id > cursor`).
    When fewer than `limit` rows are returned after `cursor`, wraps around from the
    beginning (`after_job_id=""`) to top up the batch and resets the cursor to `""`.
    """
    use_default_cursor = cursor is None
    active_cursor = (
        str(getattr(reconcile_active_transcription_jobs_once, "_cursor", "") or "")
        if use_default_cursor
        else str(cursor or "")
    )
    if not is_transcription_reconciliation_enabled():
        return {
            "scanned": 0,
            "synced": 0,
            "failed": 0,
            "skipped_bridge_unreachable": False,
            "next_cursor": active_cursor,
        }

    clamped_limit = max(1, min(int(limit), 200))
    jobs = await db.list_non_terminal_transcription_jobs(
        limit=clamped_limit, after_job_id=active_cursor
    )
    if len(jobs) < clamped_limit:
        if active_cursor:
            remaining = clamped_limit - len(jobs)
            head_jobs = await db.list_non_terminal_transcription_jobs(
                limit=remaining, after_job_id=""
            )
            seen_ids = {str(j.get("job_id") or "") for j in jobs}
            for hj in head_jobs:
                hjid = str(hj.get("job_id") or "")
                if hjid and hjid not in seen_ids:
                    seen_ids.add(hjid)
                    jobs.append(hj)
        next_cursor = ""
    else:
        next_cursor = str(jobs[-1].get("job_id") or "") if jobs else ""

    if use_default_cursor:
        setattr(reconcile_active_transcription_jobs_once, "_cursor", next_cursor)

    synced_count = 0
    failed_count = 0
    skipped_unreachable = False

    for job in jobs:
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            continue
        try:
            code, resp = await call_bridge_get_transcription(job_id)
        except (httpx.RequestError, ConnectionError, TimeoutError, OSError) as exc:
            logger.warning(
                "Transcription reconciliation pass skipped remaining jobs (bridge unreachable on job_id=%s): %s",
                job_id,
                type(exc).__name__,
            )
            skipped_unreachable = True
            break
        except HTTPException as exc:
            if exc.status_code in (502, 503, 504):
                logger.warning(
                    "Transcription reconciliation pass skipped remaining jobs (bridge HTTP %d on job_id=%s)",
                    exc.status_code,
                    job_id,
                )
                skipped_unreachable = True
                break
            failed_count += 1
            continue
        except Exception as exc:
            logger.warning(
                "Transcription reconciliation poll failed for job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
            failed_count += 1
            continue

        if code in (502, 503, 504):
            skipped_unreachable = True
            break

        if code == 200 and isinstance(resp.get("job"), dict):
            try:
                await _sync_job_from_bridge_state(job, resp["job"])
                synced_count += 1
            except Exception as exc:
                logger.warning(
                    "Transcription reconciliation sync failed for job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                failed_count += 1
        elif code >= 400 and code != 404:
            failed_count += 1

    return {
        "scanned": len(jobs),
        "synced": synced_count,
        "failed": failed_count,
        "skipped_bridge_unreachable": skipped_unreachable,
        "next_cursor": next_cursor,
    }


async def run_transcription_reconciliation_loop(
    *,
    interval_s: float = TRANSCRIPTION_RECONCILE_INTERVAL_S,
    batch_limit: int = TRANSCRIPTION_RECONCILE_BATCH_LIMIT,
) -> None:
    """Background loop syncing non-terminal transcription jobs every `interval_s` (5 minutes)."""
    wait_s = max(1.0, float(interval_s))
    cursor = ""
    while True:
        try:
            await asyncio.sleep(wait_s)
            if not is_transcription_reconciliation_enabled():
                continue
            res = await reconcile_active_transcription_jobs_once(
                limit=batch_limit, cursor=cursor
            )
            cursor = str(res.get("next_cursor") or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Transcription periodic reconciliation loop error: %s",
                type(exc).__name__,
            )


async def get_transcription_usage_report(
    *,
    caller_user_id: str,
    target_user_id: str | None = None,
    workspace_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    bridge_records: list[dict[str, Any]] | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """Read-only usage summary by user and date from `dalton_transcription_jobs.usage` (with optional bridge JSONL reconciliation).

    - Internal users may query their own usage (`target_user_id` defaults to `caller_user_id`).
    - Admin users may query all users (`target_user_id=None`) or any specific `target_user_id`.
    """
    clean_caller = await require_internal_transcription_caller(caller_user_id)
    tier = await get_user_tier(clean_caller)

    if tier != "admin":
        if target_user_id and str(target_user_id).strip() != clean_caller:
            raise HTTPException(
                status_code=403,
                detail="Only admin users may query another user's transcription usage",
            )
        effective_user_id: str | None = clean_caller
    else:
        effective_user_id = (
            str(target_user_id).strip()
            if target_user_id and str(target_user_id).strip()
            else None
        )

    clamped_limit = max(1, min(int(limit), 2000))
    clamped_offset = max(0, int(offset))
    jobs = await db.list_transcription_jobs_for_usage(
        user_id=effective_user_id,
        workspace_id=workspace_id,
        start_date=start_date,
        end_date=end_date,
        limit=clamped_limit,
        offset=clamped_offset,
    )
    report = db.summarize_transcription_usage(
        jobs,
        bridge_records=bridge_records,
        user_id=effective_user_id,
        start_date=start_date,
        end_date=end_date,
        limit=clamped_limit,
        offset=clamped_offset,
    )
    return {
        "ok": True,
        **report,
    }


__all__ = [
    "ALLOWED_TICKET_OPERATIONS",
    "DEFAULT_TICKET_TTL_S",
    "EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE",
    "MAX_TRANSCRIPTION_AUDIO_BYTES",
    "MAX_TRANSCRIPTION_UPLOAD_PARTS",
    "PROVIDER_CANCEL_UNVERIFIED_NOTICE",
    "TRANSCRIPTION_RECONCILE_BATCH_LIMIT",
    "TRANSCRIPTION_RECONCILE_INTERVAL_S",
    "TRANSCRIPTION_UPLOAD_PART_BYTES",
    "TRANSCRIPTION_UPLOAD_URL_EXPIRES_S",
    "abort_transcription_upload",
    "authorize_job_access",
    "build_transcription_source_object_key",
    "call_bridge_action_transcription",
    "call_bridge_get_transcription",
    "call_bridge_get_transcription_result",
    "call_bridge_submit_transcription",
    "cancel_transcription_job",
    "complete_transcription_upload",
    "derive_transcription_idempotency_key",
    "get_transcription_job_result",
    "get_transcription_job_status",
    "get_transcription_usage_report",
    "is_transcription_enabled",
    "is_transcription_reconciliation_enabled",
    "is_transcription_ticket_token",
    "issue_transcription_ticket",
    "normalize_hotwords",
    "open_bridge_raw_result_stream",
    "prepare_transcription_job",
    "reconcile_active_transcription_jobs_once",
    "require_internal_transcription_caller",
    "retry_transcription_job",
    "run_transcription_reconciliation_loop",
    "sign_transcription_upload_part",
    "start_transcription_upload",
    "stream_transcription_job_raw_result",
    "submit_transcription_job",
    "validate_source_object_key",
    "verify_creator_workspace_access",
    "verify_transcription_ticket",
]
