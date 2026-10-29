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

import base64
import hashlib
import hmac
import json
import logging
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
    get_dalton_transcription_ticket_hmac_key,
)

logger = logging.getLogger(__name__)

DEFAULT_TICKET_TTL_S: int = 7200
MAX_TRANSCRIPTION_AUDIO_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GiB
BRIDGE_TRANSCRIPTION_TIMEOUT_S: float = 30.0
ALLOWED_TICKET_OPERATIONS: tuple[str, ...] = (
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

        if (
            not can_reset_terminal
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
        if isinstance(raw_usage, dict):
            merged_usage.update(raw_usage)
        merged_usage["attempt"] = b_attempt
        merged_usage["bridge_version"] = b_version

        # Skip write if nothing meaningful changed
        if (
            new_status == cur_status
            and new_stage == str(current.get("stage") or "")
            and (seg_cnt is None or seg_cnt == current.get("segment_count"))
            and (comp_cnt is None or comp_cnt == current.get("completed_segments"))
            and (req_ids is None or req_ids == current.get("provider_request_ids"))
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
            provider_request_ids=req_ids if isinstance(req_ids, list) else None,
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
    """Cancel a non-succeeded transcription job."""
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
        return {"ok": True, "job": job}

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
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Bridge transcription cancel failed: {type(exc).__name__}",
            ) from exc

    for _ in range(3):
        updated, latest = await db.update_transcription_job_versioned(
            str(job["job_id"]),
            expected_version=int(job["version"]),
            status=TranscriptionJobStatus.CANCELLED,
            stage=TranscriptionJobStatus.CANCELLED.value,
        )
        if latest is None:
            break
        if updated or str(latest.get("status") or "") == TranscriptionJobStatus.CANCELLED.value:
            return {"ok": True, "job": latest}
        if str(latest.get("status") or "") == TranscriptionJobStatus.SUCCEEDED.value:
            raise HTTPException(
                status_code=409,
                detail="Succeeded transcription job cannot be cancelled",
            )
        job = latest

    return {"ok": True, "job": job}


__all__ = [
    "ALLOWED_TICKET_OPERATIONS",
    "DEFAULT_TICKET_TTL_S",
    "EXTERNAL_TRANSCRIPTION_FORBIDDEN_MESSAGE",
    "MAX_TRANSCRIPTION_AUDIO_BYTES",
    "authorize_job_access",
    "build_transcription_source_object_key",
    "call_bridge_action_transcription",
    "call_bridge_get_transcription",
    "call_bridge_get_transcription_result",
    "call_bridge_submit_transcription",
    "cancel_transcription_job",
    "derive_transcription_idempotency_key",
    "get_transcription_job_result",
    "get_transcription_job_status",
    "is_transcription_ticket_token",
    "issue_transcription_ticket",
    "normalize_hotwords",
    "prepare_transcription_job",
    "require_internal_transcription_caller",
    "retry_transcription_job",
    "submit_transcription_job",
    "validate_source_object_key",
    "verify_creator_workspace_access",
    "verify_transcription_ticket",
]
