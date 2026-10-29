"""FastAPI router for Dalton audio transcription jobs (`/api/v1/dalton/transcriptions*`).

Follows `src/server/AGENTS.md` layering: router orchestrates service calls only;
all SQL lives in `src/dalton/database/transcriptions.py`.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.services.transcriptions import (
    abort_transcription_upload,
    cancel_transcription_job,
    complete_transcription_upload,
    get_transcription_job_result,
    get_transcription_job_status,
    get_transcription_usage_report,
    is_transcription_ticket_token,
    prepare_transcription_job,
    retry_transcription_job,
    sign_transcription_upload_part,
    start_transcription_upload,
    stream_transcription_job_raw_result,
    submit_transcription_job,
    verify_transcription_ticket,
)
from src.dalton.settings import get_dalton_settings
from src.server.utils.api import get_current_user_id, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dalton/transcriptions", tags=["Dalton Transcriptions"])

_optional_bearer = HTTPBearer(auto_error=False)


class CreateOrSubmitTranscriptionRequest(BaseModel):
    """Request body for `POST /api/v1/dalton/transcriptions`.

    Caller identity fields (if any are smuggled in by a client) are ignored;
    accounting identity always comes from the verified ticket or backend auth.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: str | None = None
    workspace_id: str | None = None
    thread_id: str | None = None
    source_object_key: str | None = None
    source_size_bytes: int | None = Field(default=None, gt=0)
    source_sha256: str | None = None
    language: str | None = None
    hotwords: list[str] | str | None = None
    submit: bool = True


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


def _extract_ticket_from_request(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[bool, str]:
    """Return `(has_ticket_header, raw_ticket)` from `X-Dalton-Transcription-Ticket` or Bearer token."""
    if "x-dalton-transcription-token" in request.headers:
        return True, request.headers.get("x-dalton-transcription-token", "").strip()
    if "x-dalton-transcription-ticket" in request.headers:
        return True, request.headers.get("x-dalton-transcription-ticket", "").strip()
    if credentials is not None and credentials.credentials:
        raw = credentials.credentials.strip()
        if is_transcription_ticket_token(raw) or raw.count(".") == 1:
            return True, raw
    return False, ""


async def _resolve_caller_and_ticket(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    required_op: str,
    expected_job_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Authenticate caller via task ticket (if present) or standard backend user auth.

    Never reads `X-Dalton-User`, `X-Dalton-Workspace`, or `X-Dalton-Thread` headers.
    """
    _require_dalton_enabled()
    has_ticket, raw_ticket = _extract_ticket_from_request(request, credentials)
    if has_ticket:
        claims = verify_transcription_ticket(
            raw_ticket,
            required_op=required_op,
            expected_job_id=expected_job_id,
        )
        return str(claims["user_id"]), claims

    override = request.app.dependency_overrides.get(get_current_user_id)
    if callable(override):
        res = override()
        if inspect.isawaitable(res):
            res = await res
        user_id = str(res or "").strip()
    else:
        user_id = await get_current_user_id(request, credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id, None


@router.post("")
@handle_api_exceptions("create or submit transcription job", logger)
async def create_or_submit_transcription_endpoint(
    body: CreateOrSubmitTranscriptionRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="submit",
        expected_job_id=body.job_id if body.job_id else None,
    )

    if not body.submit:
        if ticket_claims is not None:
            raise HTTPException(
                status_code=403,
                detail="Task ticket cannot prepare a new transcription job",
            )
        if (
            not body.workspace_id
            or not body.source_object_key
            or body.source_size_bytes is None
            or not body.source_sha256
        ):
            raise HTTPException(
                status_code=400,
                detail="workspace_id, source_object_key, source_size_bytes, and source_sha256 are required",
            )
        job, _ticket, exp_ts = await prepare_transcription_job(
            user_id=caller_user_id,
            workspace_id=body.workspace_id,
            thread_id=body.thread_id,
            source_filename=Path(body.source_object_key).name,
            source_size_bytes=body.source_size_bytes,
            source_sha256=body.source_sha256,
            language=body.language,
            hotwords=body.hotwords,
            source_object_key=body.source_object_key,
        )
        return {"ok": True, "job": job, "ticket_expires_at": exp_ts}

    job = await submit_transcription_job(
        caller_user_id=caller_user_id,
        job_id=body.job_id,
        workspace_id=body.workspace_id,
        thread_id=body.thread_id,
        source_object_key=body.source_object_key,
        source_size_bytes=body.source_size_bytes,
        source_sha256=body.source_sha256,
        language=body.language,
        hotwords=body.hotwords,
        ticket_claims=ticket_claims,
    )
    return {"ok": True, "job": job}


@router.get("/usage")
@handle_api_exceptions("get transcription usage report", logger)
async def get_transcription_usage_endpoint(
    request: Request,
    user_id: str | None = None,
    workspace_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    _require_dalton_enabled()
    has_ticket, _raw_ticket = _extract_ticket_from_request(request, credentials)
    if has_ticket:
        raise HTTPException(
            status_code=403,
            detail="Task ticket cannot query aggregate usage reports",
        )
    override = request.app.dependency_overrides.get(get_current_user_id)
    if callable(override):
        res = override()
        if inspect.isawaitable(res):
            res = await res
        caller_user_id = str(res or "").strip()
    else:
        caller_user_id = await get_current_user_id(request, credentials)
    if not caller_user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    return await get_transcription_usage_report(
        caller_user_id=caller_user_id,
        target_user_id=user_id,
        workspace_id=workspace_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


async def _parse_optional_json_body(request: Request) -> dict[str, Any]:
    raw_bytes = await request.body()
    if not raw_bytes or not raw_bytes.strip():
        return {}
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return payload


async def _require_upload_ticket(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[str, dict[str, Any]]:
    caller_user_id, ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="upload",
        expected_job_id=job_id,
    )
    if ticket_claims is None:
        raise HTTPException(
            status_code=403,
            detail="Upload endpoints require a valid transcription task ticket",
        )
    return caller_user_id, ticket_claims


@router.post("/{job_id}/upload/start")
@handle_api_exceptions("start transcription multipart upload", logger)
async def start_transcription_upload_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, ticket_claims = await _require_upload_ticket(job_id, request, credentials)
    raw_body = await _parse_optional_json_body(request)
    return await start_transcription_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        raw_body=raw_body,
    )


@router.post("/{job_id}/upload/part")
@handle_api_exceptions("sign transcription multipart upload part", logger)
async def sign_transcription_upload_part_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, ticket_claims = await _require_upload_ticket(job_id, request, credentials)
    raw_body = await _parse_optional_json_body(request)
    if "part_number" not in raw_body:
        raise HTTPException(status_code=400, detail="part_number is required")
    return await sign_transcription_upload_part(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        part_number=raw_body.get("part_number"),
        upload_id=raw_body.get("upload_id"),
        raw_body=raw_body,
    )


@router.post("/{job_id}/upload/complete")
@handle_api_exceptions("complete transcription multipart upload", logger)
async def complete_transcription_upload_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, ticket_claims = await _require_upload_ticket(job_id, request, credentials)
    raw_body = await _parse_optional_json_body(request)
    if bool(raw_body.get("abort")):
        return await abort_transcription_upload(
            job_id=job_id,
            caller_user_id=caller_user_id,
            ticket_claims=ticket_claims,
            upload_id=raw_body.get("upload_id"),
            reason=str(raw_body.get("reason") or "").strip() or None,
            raw_body=raw_body,
        )
    return await complete_transcription_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        upload_id=raw_body.get("upload_id"),
        raw_body=raw_body,
    )


@router.post("/{job_id}/upload/abort")
@handle_api_exceptions("abort transcription multipart upload", logger)
async def abort_transcription_upload_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, ticket_claims = await _require_upload_ticket(job_id, request, credentials)
    raw_body = await _parse_optional_json_body(request)
    return await abort_transcription_upload(
        job_id=job_id,
        caller_user_id=caller_user_id,
        ticket_claims=ticket_claims,
        upload_id=raw_body.get("upload_id"),
        reason=str(raw_body.get("reason") or "").strip() or None,
        raw_body=raw_body,
    )


@router.get("/{job_id}")
@handle_api_exceptions("get transcription job status", logger)
async def get_transcription_status_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, _ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="status",
        expected_job_id=job_id,
    )
    return await get_transcription_job_status(
        job_id=job_id,
        caller_user_id=caller_user_id,
    )


@router.get("/{job_id}/result")
@handle_api_exceptions("get transcription job result", logger)
async def get_transcription_result_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, _ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="result",
        expected_job_id=job_id,
    )
    return await get_transcription_job_result(
        job_id=job_id,
        caller_user_id=caller_user_id,
    )


@router.get("/{job_id}/result/raw")
@handle_api_exceptions("stream transcription job raw result", logger)
async def get_transcription_raw_result_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> StreamingResponse:
    caller_user_id, _ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="result",
        expected_job_id=job_id,
    )
    byte_iter = await stream_transcription_job_raw_result(
        job_id=job_id,
        caller_user_id=caller_user_id,
    )
    return StreamingResponse(byte_iter, media_type="application/json")


@router.post("/{job_id}/retry")
@handle_api_exceptions("retry transcription job", logger)
async def retry_transcription_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, _ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="retry",
        expected_job_id=job_id,
    )
    return await retry_transcription_job(
        job_id=job_id,
        caller_user_id=caller_user_id,
    )


@router.post("/{job_id}/cancel")
@handle_api_exceptions("cancel transcription job", logger)
async def cancel_transcription_endpoint(
    job_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> dict[str, Any]:
    caller_user_id, _ticket_claims = await _resolve_caller_and_ticket(
        request,
        credentials,
        required_op="cancel",
        expected_job_id=job_id,
    )
    return await cancel_transcription_job(
        job_id=job_id,
        caller_user_id=caller_user_id,
    )


__all__ = ["router"]
