"""FastAPI router for chat composer large-file uploads (`/api/v1/dalton/chat-uploads*`).

Follows `src/server/AGENTS.md` layering: routes orchestrate service calls only;
all SQL lives in `src/dalton/database/chat_uploads.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.services.chat_uploads import (
    cancel_chat_upload,
    complete_chat_upload,
    get_chat_upload_status,
    initiate_chat_upload,
    receive_chat_upload_part,
)
from src.dalton.settings import get_dalton_settings
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


router = APIRouter(
    prefix="/api/v1/dalton/chat-uploads",
    tags=["Dalton Chat Uploads"],
    dependencies=[Depends(_require_dalton_enabled)],
)


class InitiateChatUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workspace_id: str | None = None
    filename: str = Field(..., min_length=1, max_length=512)
    size_bytes: int
    mime_type: str | None = Field(default=None, max_length=256)
    sha256: str | None = Field(default=None, max_length=128)


class CompleteChatUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha256: str | None = Field(default=None, max_length=128)


@router.post("")
@handle_api_exceptions("initiate chat upload", logger)
async def create_chat_upload_endpoint(
    payload: InitiateChatUploadRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await initiate_chat_upload(
        user_id=user_id,
        workspace_id=payload.workspace_id,
        filename=payload.filename,
        size_bytes=payload.size_bytes,
        mime_type=payload.mime_type,
        expected_sha256=payload.sha256,
    )


@router.put("/{upload_id}/parts/{part_number}")
@handle_api_exceptions("upload chat file part", logger)
async def upload_chat_part_endpoint(
    upload_id: str,
    part_number: int,
    request: Request,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await receive_chat_upload_part(
        upload_id=upload_id,
        part_number=part_number,
        user_id=user_id,
        stream=request.stream(),
        content_length_header=request.headers.get("content-length"),
        expected_part_sha256=request.headers.get("x-part-sha256"),
    )


@router.post("/{upload_id}/complete")
@handle_api_exceptions("complete chat upload", logger)
async def complete_chat_upload_endpoint(
    upload_id: str,
    user_id: CurrentUserId,
    payload: CompleteChatUploadRequest | None = None,
) -> dict[str, Any]:
    return await complete_chat_upload(
        upload_id=upload_id,
        user_id=user_id,
        expected_sha256=payload.sha256 if payload else None,
    )


@router.get("/{upload_id}")
@handle_api_exceptions("get chat upload status", logger)
async def get_chat_upload_endpoint(
    upload_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await get_chat_upload_status(upload_id=upload_id, user_id=user_id)


@router.delete("/{upload_id}")
@handle_api_exceptions("cancel chat upload", logger)
async def cancel_chat_upload_endpoint(
    upload_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await cancel_chat_upload(upload_id=upload_id, user_id=user_id)
