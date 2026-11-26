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
