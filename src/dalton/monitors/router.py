"""FastAPI router for Dalton Monitors (``/api/v1/dalton/monitors/*``)."""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.config import env
from src.dalton.auth import is_dalton_service_request
from src.dalton.monitors import service as monitor_service
from src.dalton.monitors.models import (
    DataJobMonitorPublishRequest,
    MonitorDataPutRequest,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
)
from src.dalton.settings import get_dalton_settings
from src.server.preview_proxy.artifacts import ArtifactFileUnavailable
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


def require_monitor_service_token(request: Request) -> None:
    """Validate ``X-Service-Token`` header via ``is_dalton_service_request``."""
    sent = request.headers.get("X-Service-Token", "")
    configured = os.getenv("INTERNAL_SERVICE_TOKEN") or getattr(
        env, "INTERNAL_SERVICE_TOKEN", ""
    )
    if not is_dalton_service_request(
        sent_token=sent, configured_token=configured or ""
    ):
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Service-Token"
        )


router = APIRouter(
    prefix="/api/v1/dalton/monitors",
    tags=["Dalton Monitors"],
    dependencies=[Depends(_require_dalton_enabled)],
)


# ---------------------------------------------------------------------------
# User-authenticated routes (must register static/prefixed paths before /{slug})
# ---------------------------------------------------------------------------


@router.get("")
@handle_api_exceptions("list monitors", logger)
async def list_monitors_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    items = await monitor_service.list_monitors_for_user(user_id)
    return {"monitors": items}


@router.get("/company-members")
@handle_api_exceptions("list company members for monitor ACL", logger)
async def list_company_members_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    members = await monitor_service.list_company_members_for_user(user_id)
    return {"members": members}


@router.get("/by-artifact/{artifact_id}")
@handle_api_exceptions("get monitor by artifact id", logger)
async def get_monitor_by_artifact_endpoint(
    artifact_id: str, user_id: CurrentUserId
) -> dict[str, Any]:
    monitor = await monitor_service.get_monitor_by_artifact(
        artifact_id, viewer_id=user_id
    )
    return {"monitor": monitor}


from src.dalton.colleague_outbound_guard import (
    NON_SESSION_CLEAR_FORBIDDEN_DETAIL,
    is_interactive_owner_session,
)


def _interactive_session_for_clear(
    request: Request, user_id: CurrentUserId
) -> bool:
    from src.dalton.settings import is_dalton_sharing_auth_enabled

    if not is_dalton_sharing_auth_enabled():
        return False
    return is_interactive_owner_session(request, expected_user_id=user_id)


@router.post("")
@handle_api_exceptions("publish workspace monitor", logger)
async def publish_monitor_endpoint(
    body: PublishMonitorRequest,
    user_id: CurrentUserId,
    interactive_session: bool = Depends(_interactive_session_for_clear),
) -> dict[str, Any]:
    from src.dalton.settings import is_dalton_sharing_auth_enabled

    sharing_auth_enabled = is_dalton_sharing_auth_enabled()
    can_clear_pause = sharing_auth_enabled and interactive_session is True
    if sharing_auth_enabled and body.confirm_pause_token and not can_clear_pause:
        raise HTTPException(
            status_code=403, detail=NON_SESSION_CLEAR_FORBIDDEN_DETAIL
        )
    return await monitor_service.publish_workspace_monitor(
        body, caller_id=user_id, allow_clear_pause=can_clear_pause
    )


# ---------------------------------------------------------------------------
# Grant-gated routes for sandboxed iframes (Origin: null; _URL_CREDENTIAL_ROUTES)
# ---------------------------------------------------------------------------


def _to_served_response(served: Any) -> Response:
    headers = dict(served.headers)
    response = Response(
        content=served.body, status_code=served.status, headers=headers
    )
    if "content-length" not in headers and "content-length" in response.headers:
        del response.headers["content-length"]
    return response


@router.api_route("/g/{grant}/v/{seq}/{path:path}", methods=["GET", "HEAD"])
async def serve_monitor_file_endpoint(
    grant: str,
    seq: str,
    path: str,
    request: Request,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        served = await monitor_service.serve_monitor_file(
            grant,
            seq,
            path,
            if_none_match=if_none_match,
            head=request.method == "HEAD",
        )
    except ArtifactFileUnavailable as exc:
        logger.warning("Monitor file could not be served: %s", exc)
        raise HTTPException(status_code=503, detail="File unavailable") from None
    return _to_served_response(served)


@router.get("/g/{grant}/data/{key}")
async def read_monitor_data_endpoint(
    grant: str,
    key: str,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    status_code, body, headers = await monitor_service.read_monitor_data_by_grant(
        grant, key, if_none_match=if_none_match
    )
    if status_code == 304:
        return Response(status_code=304, headers=headers)
    return JSONResponse(status_code=200, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Anonymous read-only public monitor routes (live visibility=='public' checks)
# ---------------------------------------------------------------------------


@router.api_route("/public/{slug}/v/{seq}", methods=["GET", "HEAD"])
@router.api_route("/public/{slug}/v/{seq}/", methods=["GET", "HEAD"])
async def serve_public_monitor_entry_endpoint(
    slug: str,
    seq: str,
    request: Request,
    root_nonce: str | None = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        served = await monitor_service.serve_public_monitor_file(
            slug,
            seq,
            "",
            if_none_match=if_none_match,
            head=request.method == "HEAD",
            root_nonce=root_nonce,
        )
    except ArtifactFileUnavailable as exc:
        logger.warning("Public monitor file could not be served: %s", exc)
        raise HTTPException(status_code=503, detail="File unavailable") from None
    return _to_served_response(served)


@router.api_route("/public/{slug}/v/{seq}/{path:path}", methods=["GET", "HEAD"])
async def serve_public_monitor_file_endpoint(
    slug: str,
    seq: str,
    path: str,
    request: Request,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        served = await monitor_service.serve_public_monitor_file(
            slug,
            seq,
            path,
            if_none_match=if_none_match,
            head=request.method == "HEAD",
        )
    except ArtifactFileUnavailable as exc:
        logger.warning("Public monitor file could not be served: %s", exc)
        raise HTTPException(status_code=503, detail="File unavailable") from None
    return _to_served_response(served)


@router.get("/public/{slug}/data/{key}")
async def read_public_monitor_data_endpoint(
    slug: str,
    key: str,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    status_code, body, headers = await monitor_service.read_public_monitor_data(
        slug, key, if_none_match=if_none_match
    )
    if status_code == 304:
        return Response(status_code=304, headers=headers)
    return JSONResponse(status_code=200, content=body, headers=headers)


@router.get("/public/{slug}")
@handle_api_exceptions("get public monitor detail", logger)
async def get_public_monitor_detail_endpoint(slug: str) -> dict[str, Any]:
    return await monitor_service.get_public_monitor_detail(slug)


# ---------------------------------------------------------------------------
# Internal service-authenticated routes (data_jobs writes & republish triggers)
# ---------------------------------------------------------------------------


@router.put(
    "/internal/{slug}/data/{key}",
    dependencies=[Depends(require_monitor_service_token)],
)
@handle_api_exceptions("write monitor data internal", logger)
async def put_monitor_data_internal_endpoint(
    slug: str,
    key: str,
    body: MonitorDataPutRequest,
    request: Request,
) -> dict[str, Any]:
    if request.headers.get("x-user-id") is not None:
        raise HTTPException(
            status_code=400, detail="Caller user identity override is not permitted."
        )
    return await monitor_service.write_monitor_data_internal(
        slug, key, body.data, as_of=body.as_of
    )


@router.post(
    "/internal/data-jobs/{job_key}/publish",
    dependencies=[Depends(require_monitor_service_token)],
)
@handle_api_exceptions("republish data-job monitor", logger)
async def republish_data_job_monitor_endpoint(
    job_key: str,
    body: DataJobMonitorPublishRequest,
    request: Request,
) -> dict[str, Any]:
    if request.headers.get("x-user-id") is not None:
        raise HTTPException(
            status_code=400, detail="Caller user identity override is not permitted."
        )
    return await monitor_service.republish_data_job_monitor(
        job_key=job_key,
        run_id=body.run_id,
    )


# ---------------------------------------------------------------------------
# Slug routes (must remain last)
# ---------------------------------------------------------------------------


@router.get("/{slug}")
@handle_api_exceptions("get monitor detail", logger)
async def get_monitor_detail_endpoint(
    slug: str, user_id: CurrentUserId
) -> dict[str, Any]:
    return await monitor_service.get_monitor_detail(slug, viewer_id=user_id)


@router.patch("/{slug}")
@handle_api_exceptions("update monitor", logger)
async def update_monitor_endpoint(
    slug: str, body: UpdateMonitorRequest, user_id: CurrentUserId
) -> dict[str, Any]:
    return await monitor_service.update_monitor_settings(
        slug, body, caller_id=user_id
    )


@router.post("/{slug}/republish")
@handle_api_exceptions("republish workspace monitor", logger)
async def republish_monitor_endpoint(
    slug: str,
    body: RepublishMonitorRequest,
    user_id: CurrentUserId,
    interactive_session: bool = Depends(_interactive_session_for_clear),
) -> dict[str, Any]:
    from src.dalton.settings import is_dalton_sharing_auth_enabled

    sharing_auth_enabled = is_dalton_sharing_auth_enabled()
    can_clear_pause = sharing_auth_enabled and interactive_session is True
    if sharing_auth_enabled and body.confirm_pause_token and not can_clear_pause:
        raise HTTPException(
            status_code=403, detail=NON_SESSION_CLEAR_FORBIDDEN_DETAIL
        )
    return await monitor_service.republish_workspace_monitor(
        slug, body, caller_id=user_id, allow_clear_pause=can_clear_pause
    )


@router.post("/{slug}/grant")
@handle_api_exceptions("mint monitor grant", logger)
async def mint_monitor_grant_endpoint(
    slug: str, user_id: CurrentUserId
) -> dict[str, Any]:
    return await monitor_service.mint_grant_for_monitor(slug, viewer_id=user_id)


@router.delete("/{slug}")
@handle_api_exceptions("delete monitor", logger)
async def delete_monitor_endpoint(
    slug: str, user_id: CurrentUserId
) -> dict[str, Any]:
    return await monitor_service.delete_monitor_by_slug(slug, caller_id=user_id)
