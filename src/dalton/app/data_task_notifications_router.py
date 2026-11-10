"""Internal service-authenticated Dalton routes for data-task report delivery plans and artifacts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.data_jobs.notification_payloads import (
    DataTaskArtifactError,
    DataTaskArtifactNotFoundError,
    DataTaskNotificationError,
)
from src.dalton.database.data_task_notifications import (
    authorize_data_task_notification_plan,
    load_authorized_data_task_artifact,
)
from src.dalton.services.channel_vault_credentials import (
    ChannelVaultCredentialError,
    load_dedicated_feishu_vault_credentials,
)

_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

data_task_notifications_router = APIRouter(
    prefix="/channels/internal/data-task-notifications",
    tags=["Dalton Channel Data Task Notifications"],
    dependencies=[Depends(require_channel_service_token)],
)


class DataTaskNotificationPlanRequest(BaseModel):
    """Strict request identifying only ``run_id`` and ``target`` (arbitrary content/paths forbidden)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=64)
    target: str = Field(..., min_length=1, max_length=255)


class DedicatedFeishuVaultCredentialsRequest(BaseModel):
    """Empty body schema rejecting caller-supplied ``user_id`` or secret-name lists."""

    model_config = ConfigDict(extra="forbid")


@data_task_notifications_router.post("/feishu-credentials")
async def internal_dedicated_feishu_vault_credentials_endpoint(
    request: Request,
    _body: DedicatedFeishuVaultCredentialsRequest | None = Body(default=None),
) -> JSONResponse:
    """Return the primary administrator's two dedicated Feishu Vault credentials for gateway startup."""
    if request.query_params:
        return JSONResponse(
            status_code=400,
            content={
                "code": "invalid_request",
                "message": "Query parameters are not permitted.",
            },
            headers=_NO_STORE_HEADERS,
        )
    if request.headers.get("x-user-id") is not None:
        return JSONResponse(
            status_code=400,
            content={
                "code": "invalid_request",
                "message": "Caller user identity override is not permitted.",
            },
            headers=_NO_STORE_HEADERS,
        )

    try:
        credentials = await load_dedicated_feishu_vault_credentials()
    except ChannelVaultCredentialError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "missing": list(exc.missing),
            },
            headers=_NO_STORE_HEADERS,
        )

    return JSONResponse(
        status_code=200,
        content={"credentials": credentials},
        headers=_NO_STORE_HEADERS,
    )


@data_task_notifications_router.post("/plan")
async def internal_data_task_notification_plan_endpoint(
    body: DataTaskNotificationPlanRequest,
) -> dict[str, Any]:
    """Authorize ``(run_id, target)`` and return the persisted, redacted delivery plan."""
    return await authorize_data_task_notification_plan(
        run_id=body.run_id,
        target=body.target,
    )


@data_task_notifications_router.get("/runs/{run_id}/artifacts/{artifact_id}")
async def internal_data_task_run_artifact_endpoint(
    run_id: str,
    artifact_id: str,
) -> Response:
    """Serve bounded, SHA256-verified report artifact bytes from ``.dalton-runs/<run_id>/``."""
    try:
        artifact = await load_authorized_data_task_artifact(
            run_id=run_id,
            artifact_id=artifact_id,
        )
    except DataTaskArtifactNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except DataTaskArtifactError as exc:
        status_code = 403 if exc.code in (
            "preview_run_forbidden",
            "task_disabled",
            "run_not_succeeded",
            "symlink_rejected",
            "path_traversal",
            "cross_run_denied",
            "url_rejected",
        ) else 422
        if exc.code == "invalid_run_id":
            status_code = 400
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except DataTaskNotificationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    return Response(
        content=artifact.content,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "Content-Length": str(artifact.size_bytes),
            "X-Artifact-Sha256": artifact.sha256,
            "X-Artifact-Filename": artifact.filename,
            "Cache-Control": "no-store",
        },
    )
