"""Dalton-owned archive lifecycle orchestration, span sanitization, and claim-age fencing.

Extracted out of upstream computer_manager / workspace_manager files so upstream
call sites stay 1-5 line thin delegates (see standalone-core.md §7.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from opentelemetry import trace as otel_trace

from ptc_agent.core.sandbox.providers.ssh_errors import SSHConnectError, connect_failure
from ptc_agent.core.sandbox.runtime import (
    RuntimeState,
    SandboxGoneError,
    SandboxTransientError,
)
from src.server.database.computer import (
    get_archiving_computers_by_status,
    get_computer_for_workspace,
    get_live_workspaces_for_archive_snapshot,
    reset_abandoned_archiving_computer,
)
from src.server.models.computer import ComputerStatus
from src.server.services.computer_errors import (
    ArchivePostSubmitGoneError,
    ArchiveProviderError,
    ArchiveScopeChangedError,
    ArchiveUnsupportedError,
    ComputerArchivingError,
    ComputerBusyError,
    ComputerNotStoppedError,
)
from src.server.services.path_lock_coordination import (
    STOP_HEARTBEAT_TTL_S,
    STOP_HEARTBEAT_UNAVAILABLE_RECOVERY_S,
    computer_stop_heartbeat_present,
)

logger = logging.getLogger(__name__)

_SUPPRESS_DAYTONA_RAW_SPANS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_suppress_daytona_raw_spans", default=False
)
_DAYTONA_OTEL_LOCK = threading.Lock()

_PASSTHROUGH_ARCHIVE_ERRORS = (
    ArchiveUnsupportedError,
    ArchiveScopeChangedError,
    ComputerArchivingError,
    ComputerBusyError,
    ComputerNotStoppedError,
    ArchiveProviderError,
)


def _install_daytona_otel_guard() -> None:
    """Idempotently wrap ``daytona._utils.otel_decorator.get_tracer`` with a ContextVar gate.

    Locked Daytona SDK ``@with_instrumentation()`` methods (``get``, ``archive``,
    ``refresh_data``) record ``str(error)`` and tracebacks into their own child spans
    before callers can catch the exception. Returning ``NoOpTracer`` only while the
    current async task is inside ``suppress_daytona_raw_spans()`` prevents raw provider
    bodies from reaching any OTLP span without affecting unrelated tasks.
    """
    try:
        import daytona._utils.otel_decorator as daytona_otel
    except Exception:
        return
    if getattr(daytona_otel.get_tracer, "_dalton_raw_span_guard", False):
        return
    with _DAYTONA_OTEL_LOCK:
        if getattr(daytona_otel.get_tracer, "_dalton_raw_span_guard", False):
            return
        orig_get_tracer = daytona_otel.get_tracer

        def _guarded_get_tracer():
            if _SUPPRESS_DAYTONA_RAW_SPANS.get():
                return otel_trace.NoOpTracer()
            return orig_get_tracer()

        _guarded_get_tracer._dalton_raw_span_guard = True  # type: ignore[attr-defined]
        daytona_otel.get_tracer = _guarded_get_tracer


@contextlib.contextmanager
def suppress_daytona_raw_spans():
    """Suppress Daytona SDK child spans in the current task so raw errors never enter spans."""
    _install_daytona_otel_guard()
    token = _SUPPRESS_DAYTONA_RAW_SPANS.set(True)
    try:
        yield
    finally:
        _SUPPRESS_DAYTONA_RAW_SPANS.reset(token)


def _is_daytona_post_submit_refresh_404(exc: BaseException) -> bool:
    """Detect when Daytona SDK's ``archive()`` submitted successfully and its internal ``refresh_data()`` got 404."""
    from daytona import DaytonaNotFoundError

    if not isinstance(exc, DaytonaNotFoundError):
        return False
    # AsyncSandbox.archive wraps AsyncSandbox.refresh_data's DaytonaNotFoundError
    # via @intercept_errors(message_prefix="Failed to archive sandbox: "), setting
    # __cause__ to refresh_data's DaytonaNotFoundError (message starts with
    # "Failed to refresh sandbox data:").
    if isinstance(exc.__cause__, DaytonaNotFoundError):
        return True
    return "Failed to refresh sandbox data:" in str(exc)


async def archive_daytona_sandbox(sdk_sandbox: Any, sandbox_id: str) -> None:
    """Call Daytona SDK ``archive()`` under raw-span suppression and normalize post-submit 404."""
    from daytona import DaytonaNotFoundError

    with suppress_daytona_raw_spans():
        try:
            await sdk_sandbox.archive()
        except DaytonaNotFoundError as exc:
            if _is_daytona_post_submit_refresh_404(exc):
                raise ArchivePostSubmitGoneError(
                    sandbox_id,
                    "Daytona sandbox not found after archive submission",
                ) from None
            raise


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    return None


def archive_claim_timestamp(computer: Mapping[str, Any] | None) -> datetime | None:
    """Return the durable archive claim timestamp (preferring ``config.archiving_claimed_at``)."""
    if not computer:
        return None
    cfg = computer.get("config") or {}
    if isinstance(cfg, Mapping):
        claimed_at = _parse_utc_timestamp(cfg.get("archiving_claimed_at"))
        if claimed_at is not None:
            return claimed_at
    return _parse_utc_timestamp(computer.get("updated_at"))


def archive_claim_owner(computer: Mapping[str, Any] | None) -> str | None:
    """Return the durable ``config.archiving_owner`` token if set."""
    if not computer:
        return None
    cfg = computer.get("config") or {}
    if isinstance(cfg, Mapping):
        owner = cfg.get("archiving_owner")
        if isinstance(owner, str) and owner:
            return owner
    return None


def archive_claim_in_grace_window(
    computer: Mapping[str, Any] | None,
    *,
    heartbeat: bool | None,
    now: datetime | None = None,
) -> bool:
    """Whether an archiving claim must not be taken over yet.

    Protects the window between committing ``status='stopping' + config.archiving=true``
    in PostgreSQL and establishing the Redis stop heartbeat:
    - ``heartbeat is True``: active worker holds Redis heartbeat -> do not take over.
    - ``heartbeat is False``: only take over once ``archiving_claimed_at`` (or
      ``updated_at``) is at least ``STOP_HEARTBEAT_TTL_S`` old.
    - ``heartbeat is None`` (Redis unreadable): only take over once the claim is at
      least ``STOP_HEARTBEAT_UNAVAILABLE_RECOVERY_S`` old.
    """
    if not computer:
        return False
    cfg = computer.get("config") or {}
    if not isinstance(cfg, Mapping) or not cfg.get("archiving"):
        return False
    if heartbeat is True:
        return True
    current = now or datetime.now(timezone.utc)
    claim_ts = archive_claim_timestamp(computer)
    if heartbeat is None:
        return claim_ts is None or (current - claim_ts) < timedelta(
            seconds=STOP_HEARTBEAT_UNAVAILABLE_RECOVERY_S
        )
    # For a 'stopping' claim, protect the gap between DB commit and Redis heartbeat
    # creation (STOP_HEARTBEAT_TTL_S). Once the row has already settled to 'stopped'
    # and no active heartbeat is present, probing the provider is safe because any
    # superseded worker re-checks status='stopping' via retain_computer_archive_claim.
    if computer.get("status") != ComputerStatus.STOPPING:
        return False
    if claim_ts is None:
        return False
    return (current - claim_ts) < timedelta(seconds=STOP_HEARTBEAT_TTL_S)


def classify_archive_provider_error(
    exc: Exception, *, default_category: str = "archive_failed"
) -> tuple[str, int, str]:
    """Map any provider/runtime exception to a fixed ``(category, http_status, safe_message)``."""
    if isinstance(exc, ArchiveProviderError):
        return (
            exc.category,
            exc.http_status,
            exc.user_message or "Archive failed due to a sandbox provider error.",
        )
    if isinstance(exc, ArchiveUnsupportedError):
        return ("archive_unsupported", 400, "Provider does not support archiving")
    refused = connect_failure(exc)
    if isinstance(exc, SSHConnectError) or refused is not None:
        status_code = 422 if (refused is not None and refused.answered) else 503
        return (
            "ssh_connect_refused",
            status_code,
            "Computer connection was refused.",
        )
    if isinstance(exc, SandboxGoneError):
        return ("sandbox_gone", 503, "Sandbox is no longer available.")
    if isinstance(exc, (SandboxTransientError, TimeoutError)):
        return (
            "sandbox_transient",
            503,
            "Sandbox is temporarily unavailable. Please retry.",
        )
    try:
        from daytona import DaytonaError, DaytonaNotFoundError
    except Exception:
        DaytonaError = None  # type: ignore[assignment]
        DaytonaNotFoundError = None  # type: ignore[assignment]
    if DaytonaNotFoundError is not None and isinstance(exc, DaytonaNotFoundError):
        return ("sandbox_gone", 503, "Sandbox is no longer available.")
    if DaytonaError is not None and isinstance(exc, DaytonaError):
        status_code = getattr(exc, "status_code", None)
        if status_code in (408, 429, 500, 502, 503, 504):
            return (
                "sandbox_transient",
                503,
                "Sandbox is temporarily unavailable. Please retry.",
            )
    if isinstance(exc, RuntimeError) and "sandbox is in transient state" in str(exc):
        return (
            "sandbox_transient_state",
            503,
            "Sandbox is in a transient state. Please wait and try again.",
        )
    return (
        default_category,
        503,
        "Archive failed due to a sandbox provider error."
        if default_category == "archive_failed"
        else "Sandbox provider reconciliation failed.",
    )


SANITIZED_ERROR_CATEGORY_ATTR = "_dalton_sanitized_error_category"


def mark_sanitized_error(exc: Exception, category: str) -> str:
    """Annotate the active span with fixed ``error.category`` and tag ``exc`` so outer
    ``safe_aspan`` and HTTP boundaries record only whitelisted fields, never raw text.
    """
    span = otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("error.category", category)
        span.set_attribute("error.type", type(exc).__name__)
    try:
        setattr(exc, SANITIZED_ERROR_CATEGORY_ATTR, category)
    except (AttributeError, TypeError):
        pass
    return category


def sanitize_reconcile_exception(exc: Exception) -> ArchiveProviderError:
    """Annotate the active span with fixed ``error.category`` and return a sanitized error."""
    category, http_status, message = classify_archive_provider_error(
        exc, default_category="reconcile_failed"
    )
    sanitized = (
        exc
        if isinstance(exc, ArchiveProviderError)
        else ArchiveProviderError(category, http_status=http_status, message=message)
    )
    mark_sanitized_error(sanitized, category)
    return sanitized


async def reconcile_stopping_locked(
    manager: Any, computer_id: str
) -> dict[str, Any] | None:
    """Locked body of ``reconcile_stopping_computer`` with span/SDK exception containment.

    Only archiving rows convert provider exceptions into ``ArchiveProviderError``;
    ordinary stop/start reconciliations (e.g. Docker) preserve their original
    exception type and HTTP status mapping while tagging the fixed error category
    so outer ``safe_aspan`` and HTTP error boundaries log/record whitelisted fields only.
    """
    from src.server.services.computer_manager import _lifecycle

    computer = await _lifecycle.get_computer(computer_id)
    if not computer or computer.get("status") != ComputerStatus.STOPPING:
        return computer
    is_archiving = bool((computer.get("config") or {}).get("archiving"))
    with suppress_daytona_raw_spans():
        try:
            await manager._correct_stuck_stopping(
                manager._binding_from_computer("", computer)
            )
        except Exception as exc:
            if not is_archiving:
                category, _, _ = classify_archive_provider_error(
                    exc, default_category="reconcile_failed"
                )
                mark_sanitized_error(exc, category)
                raise
            sanitized = sanitize_reconcile_exception(exc)
            raise sanitized from None
    return await _lifecycle.get_computer(computer_id)


async def reconcile_failed_stop_sanitized(
    manager: Any,
    binding: Any,
    *,
    computer_id: str,
    workspace_id: str | None,
    exc: Exception,
) -> bool:
    """Log a failed stop with whitelisted fields only and reconcile under raw-span suppression."""
    category, _, _ = classify_archive_provider_error(
        exc, default_category="stop_failed"
    )
    logger.error(
        "stop_computer_failed category=%s computer_id=%s workspace_id=%s exc_type=%s",
        category,
        computer_id,
        workspace_id or "",
        type(exc).__name__,
    )
    try:
        with suppress_daytona_raw_spans():
            return bool(await manager._reconcile_provider_stop(binding))
    except Exception as rec_exc:
        # Leave the durable stopping claim for the existing recovery
        # path once this owner releases its heartbeat.
        rec_category, _, _ = classify_archive_provider_error(
            rec_exc, default_category="reconcile_failed"
        )
        logger.warning(
            "stop_reconcile_failed category=%s computer_id=%s workspace_id=%s exc_type=%s",
            rec_category,
            computer_id,
            workspace_id or "",
            type(rec_exc).__name__,
        )
        return False


async def settle_machine_stop_impl(
    manager: Any,
    computer_id: str,
    status: str,
    *,
    clear_archiving: bool = True,
    expected_archiving_owner: str | None = None,
) -> None:
    """CAS from 'stopping' so a teardown's tail cannot stomp a peer's transition.

    ``clear_archiving`` defaults to True so ordinary stops and terminal
    reconciliations keep the upstream 2-argument call contract and clear
    any residual ``config.archiving`` fence. Pass ``clear_archiving=False``
    only while the provider still reports ``archiving``.
    """
    from src.server.services.computer_manager import _machines

    if status == ComputerStatus.STOPPED:
        # While the row still reads 'stopping', so no start can have minted
        # a token this would end.
        await manager._revoke_computer_tokens(computer_id)
        await manager._revoke_livefs(computer_id)
    kwargs: dict[str, Any] = {"expected": "stopping"}
    if clear_archiving:
        kwargs["drop_config_key"] = "archiving"
    if expected_archiving_owner is not None:
        kwargs["expected_archiving_owner"] = expected_archiving_owner
    settled = await _machines.update_computer_status(
        computer_id,
        status,
        **kwargs,
    )
    if settled is None:
        if clear_archiving and status == ComputerStatus.STOPPED:
            # Startup recovery or a peer may already have settled status to
            # 'stopped' while preserving config.archiving; a confirmed
            # terminal provider state still clears the fence under the same owner CAS.
            if expected_archiving_owner is not None:
                await _machines.clear_computer_archiving(
                    computer_id,
                    expected_archiving_owner=expected_archiving_owner,
                )
            else:
                await _machines.clear_computer_archiving(computer_id)
        logger.info(
            "Computer %s was moved out of 'stopping' by another worker; "
            "leaving it rather than writing %r",
            computer_id,
            status,
        )


async def archive_machine_impl(
    manager: Any,
    computer_id: str,
    *,
    workspace_id: str | None = None,
    expected_workspaces: Any = None,
    expected_workspace_id: str | None = None,
) -> dict[str, Any]:
    """Archive moves the machine's disk to storage; transactional DB fences guard it."""
    from src.server.services.computer_manager import _lifecycle

    async with manager._observed_lock(computer_id, "computer.archive"):
        # Gate across the machine so archive cannot freeze a sibling's live run.
        if await manager._machine_has_active_tasks(
            computer_id, workspace_id=workspace_id
        ):
            raise ComputerBusyError(
                "Cannot archive a computer that still has work running"
            )

        computer = await manager._claim_machine_for_archive(
            computer_id,
            expected_workspaces=expected_workspaces,
            expected_workspace_id=expected_workspace_id,
        )
        claim_owner = archive_claim_owner(computer)
        binding = manager._binding_from_computer(workspace_id or "", computer)
        sandbox_id = computer["provider_ref"]

        archive_state = "archiving"
        async with _lifecycle.computer_stop_heartbeat(computer_id):
            try:
                # Re-verify durable claim ownership after establishing the heartbeat
                # and before contacting the provider so a superseded claim never
                # issues a provider archive call.
                if claim_owner is not None:
                    retained = await manager._retain_machine_archive_claim(
                        computer_id, archiving_owner=claim_owner
                    )
                    if not retained:
                        raise ComputerBusyError(
                            "Computer archive claim was lost before contacting provider; retry"
                        )

                with suppress_daytona_raw_spans():
                    async with manager._detached_runtime(
                        sandbox_id, binding=binding
                    ) as runtime:
                        if not runtime.archives:
                            raise ArchiveUnsupportedError(
                                "Provider does not support archiving"
                            )
                        post_state: RuntimeState | None
                        try:
                            await runtime.archive()
                        except ArchivePostSubmitGoneError:
                            # Daytona SDK archive() submitted and its post-submit
                            # refresh got 404 -> sandbox is already gone (terminal).
                            post_state = None
                        else:
                            try:
                                post_state = await runtime.refresh_state()
                            except SandboxGoneError:
                                # Sandbox disappeared after archive was issued -> terminal gone.
                                post_state = None

                if post_state is None or post_state == RuntimeState.ARCHIVED:
                    settle_kwargs: dict[str, Any] = {"clear_archiving": True}
                    if claim_owner is not None:
                        settle_kwargs["expected_archiving_owner"] = claim_owner
                    await manager._settle_machine_stop(
                        computer_id, ComputerStatus.STOPPED, **settle_kwargs
                    )
                    archive_state = "archived"
                elif post_state == RuntimeState.ARCHIVING:
                    # Daytona archive runs asynchronously: settle status to 'stopped'
                    # while keeping config.archiving=True until reconciliation confirms
                    # a terminal provider state.
                    await manager._settle_machine_stop(
                        computer_id, ComputerStatus.STOPPED, clear_archiving=False
                    )
                    archive_state = "archiving"
                else:
                    raise ArchiveProviderError(
                        "archive_unexpected_state",
                        http_status=503,
                        message="Archive did not reach archived state; please retry.",
                    )
            except Exception as exc:
                category, status_code, safe_msg = classify_archive_provider_error(exc)
                span = otel_trace.get_current_span()
                if span is not None and span.is_recording():
                    span.set_attribute("error.category", category)

                logger.warning(
                    "archive_machine_failed category=%s computer_id=%s workspace_id=%s exc_type=%s",
                    category,
                    computer_id,
                    workspace_id or "",
                    type(exc).__name__,
                )
                if not isinstance(exc, ComputerBusyError):
                    try:
                        with suppress_daytona_raw_spans():
                            await manager._reconcile_provider_stop(binding)
                    except Exception as rec_exc:
                        logger.warning(
                            "archive_reconcile_failed computer_id=%s workspace_id=%s exc_type=%s",
                            computer_id,
                            workspace_id or "",
                            type(rec_exc).__name__,
                        )

                if isinstance(exc, _PASSTHROUGH_ARCHIVE_ERRORS):
                    mark_sanitized_error(exc, category)
                    raise

                sanitized = ArchiveProviderError(
                    category,
                    http_status=status_code,
                    message=safe_msg,
                )
                mark_sanitized_error(sanitized, category)
                raise sanitized from None

        logger.info(
            "archive_machine_completed computer_id=%s archive_state=%s",
            computer_id,
            archive_state,
        )
        result = dict(await manager._stop_result(computer_id, workspace_id))
        result["archive_state"] = archive_state
        return result


async def reconcile_stopped_archiving_locked_impl(
    manager: Any, computer: dict[str, Any]
) -> dict[str, Any]:
    """Probe provider for a stopped computer carrying ``config.archiving=true``."""
    from src.server.services.computer_manager import _machines

    computer_id = str(computer["computer_id"])
    if not (computer.get("config") or {}).get("archiving"):
        return computer
    heartbeat = await computer_stop_heartbeat_present(computer_id)
    if archive_claim_in_grace_window(computer, heartbeat=heartbeat):
        return computer

    claim_owner = archive_claim_owner(computer)

    async def _clear_fence() -> None:
        if claim_owner is not None:
            await _machines.clear_computer_archiving(
                computer_id, expected_archiving_owner=claim_owner
            )
        else:
            await _machines.clear_computer_archiving(computer_id)

    sandbox_id = computer.get("provider_ref")
    if not sandbox_id:
        await _clear_fence()
        return await _machines.get_computer(computer_id) or computer
    binding = manager._binding_from_computer("", computer)
    if not binding.provider.recreatable:
        await _clear_fence()
        return await _machines.get_computer(computer_id) or computer
    try:
        with suppress_daytona_raw_spans():
            async with manager._detached_runtime(sandbox_id, binding=binding) as runtime:
                actual_state = await runtime.get_state()
    except SandboxGoneError:
        await _clear_fence()
        return await _machines.get_computer(computer_id) or computer
    except Exception as exc:
        logger.warning(
            "archive_fence_reconcile_probe_failed computer_id=%s exc_type=%s",
            computer_id,
            type(exc).__name__,
        )
        return computer

    state = actual_state.value
    if state == "archiving":
        return computer
    if state in ("archived", "stopped", "error"):
        await _clear_fence()
        return await _machines.get_computer(computer_id) or computer
    if state == "running":
        await _machines.update_computer_status(
            computer_id,
            ComputerStatus.RUNNING,
            expected=ComputerStatus.STOPPED,
            drop_config_key="archiving",
        )
        await _machines.update_computer_activity(computer_id)
        return await _machines.get_computer(computer_id) or computer
    return computer


async def ensure_not_archiving_locked_impl(
    manager: Any, computer: dict[str, Any]
) -> dict[str, Any]:
    """Shared archive-fence reconciliation for computer start and workspace start/session acquire."""
    from src.server.services.computer_manager import _machines

    computer_id = str(computer["computer_id"])
    if not (computer.get("config") or {}).get("archiving"):
        return computer
    status = computer.get("status")
    if status == ComputerStatus.STOPPING:
        with suppress_daytona_raw_spans():
            try:
                await manager._correct_stuck_stopping(
                    manager._binding_from_computer("", computer)
                )
            except Exception as exc:
                raise sanitize_reconcile_exception(exc) from None
        computer = await _machines.get_computer(computer_id) or computer
        status = computer.get("status")
    if (
        status == ComputerStatus.STOPPED
        and (computer.get("config") or {}).get("archiving")
    ):
        computer = await manager._reconcile_stopped_archiving_locked(computer)
    if (computer.get("config") or {}).get("archiving"):
        err = ComputerArchivingError(
            "This computer is currently being archived. Wait for archiving to finish before starting it."
        )
        mark_sanitized_error(err, "computer_archiving")
        raise err
    return computer


async def ensure_computer_not_archiving_impl(manager: Any, computer_id: str) -> bool:
    """Reconcile any ``config.archiving`` fence on ``computer_id`` and refuse start while active."""
    from src.server.services.computer_manager import _machines

    computer = await _machines.get_computer(computer_id)
    if computer is None or not (computer.get("config") or {}).get("archiving"):
        return False
    async with manager._observed_lock(computer_id, "computer.archiving.reconcile"):
        fresh = await _machines.get_computer(computer_id)
        if fresh is None or not (fresh.get("config") or {}).get("archiving"):
            return False
        await manager._ensure_not_archiving_locked(fresh)
        return True


async def reconcile_archiving_computer_impl(
    manager: Any, computer_id: str
) -> dict[str, Any] | None:
    """Probe provider and clear ``config.archiving`` once archiving reached a terminal state."""
    from src.server.services.computer_manager import _machines

    async with manager._observed_lock(computer_id, "computer.archiving.reconcile"):
        computer = await _machines.get_computer(computer_id)
        if computer is None:
            return None
        if not (computer.get("config") or {}).get("archiving"):
            return computer
        status = computer.get("status")
        if status == ComputerStatus.STOPPING:
            with suppress_daytona_raw_spans():
                try:
                    await manager._correct_stuck_stopping(
                        manager._binding_from_computer("", computer)
                    )
                except Exception as exc:
                    raise sanitize_reconcile_exception(exc) from None
            computer = await _machines.get_computer(computer_id)
            if computer is None or not (computer.get("config") or {}).get("archiving"):
                return computer
            status = computer.get("status")
        if status == ComputerStatus.STOPPED:
            return await manager._reconcile_stopped_archiving_locked(computer)
        if status == ComputerStatus.RUNNING:
            claim_owner = archive_claim_owner(computer)
            if claim_owner is not None:
                await _machines.clear_computer_archiving(
                    computer_id, expected_archiving_owner=claim_owner
                )
            else:
                await _machines.clear_computer_archiving(computer_id)
            return await _machines.get_computer(computer_id)
        return computer


async def archive_computer_impl(
    manager: Any,
    computer_id: str,
    *,
    expected_workspaces: Any = None,
) -> dict[str, Any] | None:
    snapshot = (
        expected_workspaces
        if expected_workspaces is not None
        else await get_live_workspaces_for_archive_snapshot(computer_id)
    )
    return await manager._archive_machine(computer_id, expected_workspaces=snapshot)


async def archive_workspace_impl(
    manager: Any,
    workspace_id: str,
    *,
    expected_computer_id: str | None = None,
    expected_workspaces: Any = None,
) -> dict[str, Any]:
    computer = await get_computer_for_workspace(workspace_id)
    if computer is None:
        raise ArchiveScopeChangedError("情况已变化，请重新发起")
    computer_id = str(computer["computer_id"])
    if expected_computer_id is not None and computer_id != str(expected_computer_id):
        raise ArchiveScopeChangedError("情况已变化，请重新发起")
    snapshot = (
        expected_workspaces
        if expected_workspaces is not None
        else await get_live_workspaces_for_archive_snapshot(computer_id)
    )
    return await manager._archive_machine(
        computer_id,
        workspace_id=workspace_id,
        expected_workspaces=snapshot,
        expected_workspace_id=workspace_id,
    )


async def reap_stopped_archiving_computers_impl(manager: Any) -> int:
    """Probe stopped computers carrying a residual ``config.archiving`` fence."""
    from src.server.services import workspace_manager as wm_mod

    cleared = 0
    for computer in await get_archiving_computers_by_status(
        "stopped", limit=1000
    ):
        computer_id = str(computer["computer_id"])
        try:
            async with asyncio.timeout(wm_mod.STUCK_STOP_RECONCILE_TIMEOUT_S):
                after = await manager.reconcile_archiving_computer(computer_id)
        except Exception as exc:
            logger.warning(
                "archive_fence_sweep_failed computer_id=%s exc_type=%s",
                computer_id,
                type(exc).__name__,
            )
            continue
        if after is not None and not (after.get("config") or {}).get("archiving"):
            logger.info(
                "archive_fence_cleared_by_sweep computer_id=%s status=%s",
                computer_id,
                after.get("status"),
            )
            cleared += 1
    return cleared


async def recover_archiving_computers_on_startup_impl() -> int:
    """Reset abandoned ``stopping`` archiving computers back to ``stopped`` on startup.

    Only takes over rows with NO active heartbeat whose durable claim timestamp
    (``config.archiving_claimed_at``, falling back to ``updated_at``) is past
    ``STOP_HEARTBEAT_TTL_S`` (or ``STOP_HEARTBEAT_UNAVAILABLE_RECOVERY_S`` when
    Redis is unreadable), and CAS-matches the exact ``archiving_owner`` and
    ``updated_at`` so a fresh claim committed before its Redis heartbeat is
    created is never stomped.
    """
    recovered = 0
    for computer in await get_archiving_computers_by_status(
        "stopping", limit=1000
    ):
        computer_id = str(computer["computer_id"])
        try:
            heartbeat = await computer_stop_heartbeat_present(computer_id)
            if archive_claim_in_grace_window(computer, heartbeat=heartbeat):
                reason = (
                    "owned"
                    if heartbeat is True
                    else (
                        "heartbeat_unavailable"
                        if heartbeat is None
                        else "claim_grace_window"
                    )
                )
                logger.info(
                    "startup_skip_archiving_computer_%s computer_id=%s",
                    reason,
                    computer_id,
                )
                continue
            claim_owner = archive_claim_owner(computer)
            reset_kwargs: dict[str, Any] = {
                "expected_updated_at": computer.get("updated_at"),
            }
            if claim_owner is not None:
                reset_kwargs["expected_archiving_owner"] = claim_owner
            updated = await reset_abandoned_archiving_computer(
                computer_id,
                **reset_kwargs,
            )
            if updated is not None:
                recovered += 1
                logger.info(
                    "startup_reset_archiving_computer computer_id=%s",
                    computer_id,
                )
        except Exception as exc:
            logger.warning(
                "startup_reset_archiving_computer_failed computer_id=%s exc_type=%s",
                computer_id,
                type(exc).__name__,
            )
    return recovered
