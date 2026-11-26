"""Local exception capture and whitelisted structured logging for archive HTTP routes."""

from __future__ import annotations

import contextlib
import logging
from typing import Literal

from fastapi import HTTPException

from ptc_agent.core.sandbox.providers.ssh_errors import SSHConnectError, connect_failure
from ptc_agent.core.sandbox.runtime import SandboxGoneError, SandboxTransientError
from src.server.services.computer_errors import (
    ArchiveProviderError,
    ArchiveScopeChangedError,
    ArchiveUnsupportedError,
    ComputerArchivingError,
    ComputerBusyError,
    ComputerNotStoppedError,
    NeedsComputer,
)
from src.server.utils.error_sanitization import sandbox_failure_http

logger = logging.getLogger(__name__)

_PROVIDER_ERROR_MESSAGES: dict[str, str] = {
    "sandbox_gone": "Sandbox is no longer available.",
    "sandbox_transient": "Sandbox is temporarily unavailable. Please try again in a moment.",
    "ssh_connect_refused": "Computer connection was refused.",
    "archive_unexpected_state": "Archive did not reach archived state. Please try again.",
    "archive_failed": "Sandbox provider failed while archiving. Please try again.",
}


@contextlib.asynccontextmanager
async def archive_http_errors(
    *,
    target_kind: Literal["workspace", "computer"],
    workspace_id: str | None = None,
    computer_id: str | None = None,
):
    """Handle archive route exceptions locally without leaking raw provider text.

    Logs only fixed error categories and whitelisted structured fields, and
    converts sandbox failures directly into their HTTP responses so they never
    reach the global ``_sandbox_failure_handler``.
    """
    ws_id = workspace_id or ""
    cid = computer_id or ""
    try:
        yield
    except (HTTPException, NeedsComputer):
        raise
    except ArchiveScopeChangedError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "archive_scope_changed",
            cid,
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_changed",
                "message": "情况已变化，请重新发起",
            },
        ) from None
    except ComputerArchivingError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "computer_archiving",
            cid,
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "computer_archiving",
                "message": "This computer is already being archived.",
            },
        ) from None
    except ComputerBusyError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "computer_busy",
            cid,
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "busy",
                "message": "This computer is busy. Try again in a moment.",
            },
        ) from None
    except ArchiveUnsupportedError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "archive_unsupported",
            cid,
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "archive_unsupported",
                "message": "This computer provider does not support archiving.",
            },
        ) from None
    except ComputerNotStoppedError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "computer_not_stopped",
            cid,
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_stopped",
                "message": "Only stopped computers can be archived.",
            },
        ) from None
    except ArchiveProviderError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            exc.category,
            cid,
            ws_id,
            type(exc).__name__,
        )
        default_msg = (
            "Failed to archive computer"
            if target_kind == "computer"
            else "Failed to archive workspace"
        )
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": exc.category,
                "message": exc.user_message
                or _PROVIDER_ERROR_MESSAGES.get(exc.category, default_msg),
            },
        ) from None
    except (SandboxGoneError, SandboxTransientError, SSHConnectError) as exc:
        refused = connect_failure(exc)
        if refused is not None:
            category = "ssh_connect_refused"
            ssh_code = refused.code
        elif isinstance(exc, SandboxGoneError):
            category = "sandbox_gone"
            ssh_code = None
        else:
            category = "sandbox_transient"
            ssh_code = None
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s ssh_code=%s",
            category,
            cid,
            ws_id,
            type(exc).__name__,
            ssh_code,
        )
        raise sandbox_failure_http(exc) from None
    except ValueError as exc:
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "archive_not_found",
            cid,
            ws_id,
            type(exc).__name__,
        )
        detail = (
            "Computer not found"
            if target_kind == "computer"
            else "Workspace not found"
        )
        raise HTTPException(status_code=404, detail=detail) from None
    except RuntimeError as exc:
        refused = connect_failure(exc)
        if refused is not None:
            logger.warning(
                "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s ssh_code=%s",
                "ssh_connect_refused",
                cid,
                ws_id,
                type(exc).__name__,
                refused.code,
            )
            raise sandbox_failure_http(exc) from None
        logger.warning(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "archive_refused",
            cid,
            ws_id,
            type(exc).__name__,
        )
        detail = (
            "Cannot archive computer"
            if target_kind == "computer"
            else "Cannot archive workspace"
        )
        raise HTTPException(status_code=400, detail=detail) from None
    except Exception as exc:
        logger.error(
            "archive_http_error category=%s action=archive computer_id=%s workspace_id=%s exc_type=%s",
            "archive_unexpected_error",
            cid,
            ws_id,
            type(exc).__name__,
        )
        detail = (
            "Failed to archive computer"
            if target_kind == "computer"
            else "Failed to archive workspace"
        )
        raise HTTPException(status_code=500, detail=detail) from None


async def execute_workspace_archive_route(
    workspace_id: str,
    request: object,
    body: object | None,
    principal: object,
):
    """Dalton-owned implementation for ``POST /api/v1/workspaces/{workspace_id}/archive``.

    Looks up collaborators on ``src.server.app.workspaces`` so upstream route tests
    patching that module keep working identically while keeping the upstream route
    file a 1-5 line thin delegate.
    """
    from src.server.app import workspaces as ws_app
    from src.server.database.computer import (
        get_computer_for_workspace,
        get_live_workspaces_for_archive_snapshot,
    )
    from src.server.dependencies.principal import forbidden
    from src.server.models.workspace import WorkspaceActionResponse

    computer_id_for_log = (
        getattr(body, "expected_computer_id", None) if body is not None else None
    )
    async with archive_http_errors(
        target_kind="workspace",
        workspace_id=workspace_id,
        computer_id=computer_id_for_log,
    ):
        workspace = await ws_app.db_get_workspace(workspace_id)
        await ws_app.workspace_access(workspace, principal.user_id, manage=True)

        expected_computer_id = (
            getattr(body, "expected_computer_id", None) if body is not None else None
        )
        body_workspaces = (
            getattr(body, "expected_workspaces", None) if body is not None else None
        )
        expected_workspaces = (
            [w.model_dump() for w in body_workspaces]
            if body_workspaces is not None
            else None
        )

        if getattr(principal, "computer_id", None) is not None:
            # Read-only computer resolution: never adopt an unbound workspace
            # onto another computer, and always run self-archive + cli_approval.
            computer = await get_computer_for_workspace(workspace_id)
            if computer is None:
                raise ArchiveScopeChangedError("情况已变化，请重新发起")
            computer_id = str(computer["computer_id"])
            if (
                expected_computer_id is not None
                and str(expected_computer_id) != computer_id
            ):
                raise ArchiveScopeChangedError("情况已变化，请重新发起")
            if computer_id == str(principal.computer_id):
                raise forbidden(
                    "A computer cannot stop, archive or remove itself"
                )
            if expected_workspaces is None:
                expected_workspaces = await get_live_workspaces_for_archive_snapshot(
                    computer_id
                )
            await ws_app.cli_approval.computer_archive(
                request,
                principal,
                computer,
                workspace_id=workspace_id,
                body=body,
                expected_workspaces=expected_workspaces,
            )
            expected_computer_id = computer_id

        manager = ws_app.WorkspaceManager.get_instance()
        with ws_app.computer_cause("archive"):
            if expected_computer_id is not None or expected_workspaces is not None:
                archived = await manager.archive_workspace(
                    workspace_id,
                    expected_computer_id=expected_computer_id,
                    expected_workspaces=expected_workspaces,
                )
            else:
                archived = await manager.archive_workspace(workspace_id)

        in_progress = (
            isinstance(archived, dict)
            and archived.get("archive_state") == "archiving"
        )
        logger.info(
            "archive_workspace_completed workspace_id=%s archive_state=%s",
            workspace_id,
            "archiving" if in_progress else "archived",
        )
        return WorkspaceActionResponse(
            workspace_id=workspace_id,
            status="stopped",
            message=(
                "Workspace archiving in progress"
                if in_progress
                else "Workspace archived successfully"
            ),
        )


async def execute_computer_archive_route(
    computer_id: str,
    request: object,
    body: object | None,
    principal: object,
):
    """Dalton-owned implementation for ``POST /api/v1/computers/{computer_id}/archive``."""
    from src.server.app import computers as comp_app
    from src.server.database.computer import get_live_workspaces_for_archive_snapshot
    from src.server.dependencies.principal import forbidden
    from src.server.models.computer import ComputerActionResponse

    async with archive_http_errors(
        target_kind="computer",
        computer_id=computer_id,
    ):
        computer = await comp_app._reached_computer(
            computer_id, principal.user_id, manage=True
        )
        expected_cid = (
            getattr(body, "expected_computer_id", None) if body is not None else None
        )
        if expected_cid is not None and str(expected_cid) != str(computer_id):
            raise ArchiveScopeChangedError("情况已变化，请重新发起")
        body_workspaces = (
            getattr(body, "expected_workspaces", None) if body is not None else None
        )
        expected_workspaces = (
            [w.model_dump() for w in body_workspaces]
            if body_workspaces is not None
            else None
        )
        if getattr(principal, "computer_id", None) is not None:
            if str(computer_id) == str(principal.computer_id):
                raise forbidden(
                    "A computer cannot stop, archive or remove itself"
                )
            if expected_workspaces is None:
                expected_workspaces = await get_live_workspaces_for_archive_snapshot(
                    computer_id
                )
            await comp_app.cli_approval.computer_archive(
                request,
                principal,
                computer,
                body=body,
                expected_workspaces=expected_workspaces,
            )
        manager = comp_app._computer_manager()
        with comp_app.computer_cause("archive"):
            if expected_workspaces is not None:
                updated = await manager.archive_computer(
                    computer_id,
                    expected_workspaces=expected_workspaces,
                )
            else:
                updated = await manager.archive_computer(computer_id)
        in_progress = (
            isinstance(updated, dict)
            and updated.get("archive_state") == "archiving"
        )
        logger.info(
            "archive_computer_completed computer_id=%s archive_state=%s",
            computer_id,
            "archiving" if in_progress else "archived",
        )
        return ComputerActionResponse(
            computer_id=computer_id,
            status=(updated or {}).get("status") or "stopped",
            message=(
                "Computer archiving in progress"
                if in_progress
                else "Computer archived successfully"
            ),
        )
