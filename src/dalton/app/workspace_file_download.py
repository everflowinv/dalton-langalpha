"""Authenticated workspace-file download via 60-second HMAC ``dl_token`` (§1.3 #4).

Browser navigations cannot carry ``Authorization: Bearer`` headers. When
``STORAGE_BROWSER_ENDPOINT_URL`` is unset, ``/files/download-url`` mints a
reusable 60-second ``dl_token`` for owner files ``> 8 MiB``; this route
verifies the ticket, runs the same caller checks as ``get_current_user_id``,
and delegates directly to upstream ``download_workspace_file`` so all
workspace access, redaction, Range, and streaming hooks execute identically.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from src.dalton.company_identity import reject_company_system_auth
from src.dalton.download_ticket import verify_download_ticket
from src.server.utils.api import (
    admit_caller,
    note_caller,
    refuse_holder_credential,
)

router = APIRouter(
    prefix="/api/v1/dalton/workspace-files",
    tags=["dalton-workspace-files"],
)


@router.get("/download")
async def download_workspace_file_with_ticket(
    request: Request,
    dl_token: str = Query(default="", description="Short-lived HMAC download ticket."),
) -> Response:
    """Redeem ``dl_token`` and stream the workspace file as an attachment."""
    from src.server.app.workspace_files.crud import download_workspace_file

    claims = verify_download_ticket(dl_token)
    reject_company_system_auth(claims.user_id)
    refuse_holder_credential(claims.user_id)
    await admit_caller(request, claims.user_id)
    note_caller(request, claims.user_id, "download_ticket")
    return await download_workspace_file(
        workspace_id=claims.workspace_id,
        x_user_id=claims.user_id,
        request=request,
        path=claims.path,
        attachment=True,
        backup_ok=claims.backup_ok,
    )
