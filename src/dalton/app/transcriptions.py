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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.services.transcriptions import (
    cancel_transcription_job,
    get_transcription_job_result,
    get_transcription_job_status,
    is_transcription_ticket_token,
    prepare_transcription_job,
    retry_transcription_job,
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
