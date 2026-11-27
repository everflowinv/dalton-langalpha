"""Stop-path temporary sandbox attach for file backup on session-less workers.

When ``_finish_claimed_stop`` runs on a worker that does not hold a cached
session for the computer (multi-worker routing, process restart, or retired
idle session), upstream ``backup_project_files(strict=False)`` skips every
workspace on the computer. This module attaches a short-lived, running-only,
no-reconnect ``PTCSandbox``/``Session`` handle for the stop-time backup, enforces
a shared wall-clock deadline across the whole stop-backup pass, reserves an
independent short budget for releasing the provider/sandbox client, and falls
back cleanly while surfacing completed/unfinished (or unknown) workspace counts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ptc_agent.core.sandbox.attach_only import attach_only
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import RuntimeState
from ptc_agent.core.session import Session

from src.dalton.settings import (
    get_stop_backup_attach_timeout_s,
    get_stop_backup_close_timeout_s,
    get_stop_backup_total_timeout_s,
)

logger = logging.getLogger(__name__)

_mirror_stats_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "dalton_stop_backup_mirror_stats", default=None
)


@dataclass(frozen=True, slots=True)
class StopBackupOutcome:
    """Result of one computer stop-time backup pass."""

    mirrored: int
    skipped: int | None
    total: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    enumeration_failed: bool = False
    enumeration_error: str | None = None


def record_mirror_outcome(*, total: int, mirrored: int) -> None:
    """Record project mirror progress for an active ``run_stop_backup`` scope."""
    stats = _mirror_stats_var.get()
    if stats is None:
        return
    stats["total"] = int(total)
    stats["mirrored"] = int(mirrored)


def record_enumeration_failure(exc: BaseException) -> None:
    """Mark that enumerating workspaces on the computer failed during stop backup."""
    stats = _mirror_stats_var.get()
    if stats is None:
        return
    stats["enumeration_failed"] = True
    stats["enumeration_error"] = f"{type(exc).__name__}: {exc}"


def with_stop_backup_result(
    stop_result: Any,
    outcome: StopBackupOutcome | None,
) -> Any:
    """Attach ``backup_skipped_workspaces`` to a stop result dict when known."""
    if (
        outcome is not None
        and outcome.skipped is not None
        and isinstance(stop_result, dict)
    ):
        return {**stop_result, "backup_skipped_workspaces": outcome.skipped}
    return stop_result


def _is_running_state(state: Any) -> bool:
    return state == RuntimeState.RUNNING or getattr(state, "value", None) == "running"


async def _safe_close_provider(provider: Any, *, timeout_s: float) -> None:
    if provider is None:
        return
    try:
        async with asyncio.timeout(timeout_s):
            await provider.close()
    except TimeoutError:
        logger.warning(
            "Timed out closing temporary stop-backup provider after %.1fs",
            timeout_s,
        )
    except Exception:
        logger.debug("Ignoring error closing temporary stop-backup provider", exc_info=True)


async def _safe_close_sandbox(sandbox: Any, *, timeout_s: float) -> None:
    if sandbox is None:
        return
    try:
        async with asyncio.timeout(timeout_s):
            await sandbox.close()
    except TimeoutError:
        logger.warning(
            "Timed out closing temporary stop-backup sandbox after %.1fs",
            timeout_s,
        )
    except Exception:
        logger.debug("Ignoring error closing temporary stop-backup sandbox", exc_info=True)


@asynccontextmanager
async def attach_stop_backup_session(
    manager: Any,
    binding: Any,
    durable_sandbox_id: str | None,
    *,
    timeout_s: float | None = None,
    close_timeout_s: float | None = None,
) -> AsyncIterator[Session | None]:
    """Attach a temporary running-only, no-reconnect session for stop-time backup.

    Yields ``None`` when this worker already caches a matching session, when
    there is no durable sandbox, when the sandbox is not running, or when
    connecting fails/times out. Never starts a stopped sandbox, never triggers
    generic sandbox reconnect / MCP startup on backup I/O, and never registers
    the temporary handle in ``SessionManager`` or the manager cache.
    """
    computer_id = binding.computer_id
    cached = manager._cached_session(computer_id)
    if (
        cached is not None
        and getattr(cached, "sandbox", None) is not None
        and manager._session_sandbox_id(cached) == durable_sandbox_id
    ):
        yield None
        return

    if not durable_sandbox_id:
        yield None
        return

    effective_timeout = (
        float(timeout_s)
        if timeout_s is not None
        else get_stop_backup_attach_timeout_s()
    )
    effective_close_timeout = (
        float(close_timeout_s)
        if close_timeout_s is not None and close_timeout_s > 0
        else get_stop_backup_close_timeout_s()
    )
    provider: Any = None
    sandbox: PTCSandbox | None = None
    session: Session | None = None

    try:
        try:
            if effective_timeout <= 0:
                raise TimeoutError("stop-backup attach budget already exhausted")
            core_config = manager._core_config_for(binding)
            provider = manager._provider_for(binding)
            async with asyncio.timeout(effective_timeout):
                runtime = await provider.get(durable_sandbox_id)
                state = await runtime.get_state()
                is_running = _is_running_state(state)
                if is_running:
                    work_dir = (
                        await runtime.fetch_working_dir()
                        or binding.root_dir
                        or core_config.filesystem.working_directory
                    )
                else:
                    work_dir = None

            if not is_running:
                logger.info(
                    "Computer %s sandbox %s is %s (not running); skipping temporary stop-backup attach",
                    computer_id,
                    durable_sandbox_id,
                    getattr(state, "value", state),
                )
                to_close, provider = provider, None
                await _safe_close_provider(to_close, timeout_s=effective_close_timeout)
            else:
                sandbox = PTCSandbox(
                    core_config,
                    None,
                    resource_tier=binding.resource_tier,
                    host_unavailable_policy=None,
                    provider=provider,
                    allow_reconnect=False,
                )
                # Ownership of provider now transfers to sandbox.close().
                provider = None
                sandbox.runtime = runtime
                sandbox.sandbox_id = durable_sandbox_id
                sandbox._work_dir = work_dir

                session = Session(
                    binding.workspace_id or computer_id,
                    core_config,
                    computer_id=computer_id,
                    resource_tier=binding.resource_tier,
                )
                session.sandbox = sandbox
                session._initialized = True
                logger.info(
                    "Temporarily attached stop-backup session for computer %s (sandbox=%s)",
                    computer_id,
                    durable_sandbox_id,
                )
        except TimeoutError:
            logger.warning(
                "Computer %s temporary stop-backup attach timed out after %.1fs; "
                "continuing without session and keeping previous mirror",
                computer_id,
                max(0.0, effective_timeout),
            )
            s_close, sandbox = sandbox, None
            p_close, provider = provider, None
            await _safe_close_sandbox(s_close, timeout_s=effective_close_timeout)
            await _safe_close_provider(p_close, timeout_s=effective_close_timeout)
        except Exception as exc:  # noqa: BLE001 - best-effort attach must never block stop
            logger.warning(
                "Computer %s temporary stop-backup attach failed (%s: %s); "
                "continuing without session and keeping previous mirror",
                computer_id,
                type(exc).__name__,
                exc,
            )
            s_close, sandbox = sandbox, None
            p_close, provider = provider, None
            await _safe_close_sandbox(s_close, timeout_s=effective_close_timeout)
            await _safe_close_provider(p_close, timeout_s=effective_close_timeout)

        if session is None:
            yield None
        else:
            with attach_only():
                yield session
    finally:
        if session is not None:
            session.sandbox = None
            session._initialized = False
        s_close, sandbox = sandbox, None
        p_close, provider = provider, None
        await _safe_close_sandbox(s_close, timeout_s=effective_close_timeout)
        await _safe_close_provider(p_close, timeout_s=effective_close_timeout)


def _log_stop_backup_outcome(
    computer_id: str,
    outcome: StopBackupOutcome,
    *,
    total_timeout_s: float,
) -> None:
    if outcome.timed_out:
        if outcome.enumeration_failed:
            logger.warning(
                "Computer %s stop backup timed out after %.1fs (workspace enumeration failed: %s): "
                "mirrored %d folder(s), unfinished count unknown in %dms; continuing stop",
                computer_id,
                total_timeout_s,
                outcome.enumeration_error,
                outcome.mirrored,
                outcome.duration_ms,
            )
        elif outcome.total is not None and outcome.skipped is not None:
            logger.warning(
                "Computer %s stop backup timed out after %.1fs: "
                "mirrored %d of %d workspace(s), %d unfinished in %dms; continuing stop",
                computer_id,
                total_timeout_s,
                outcome.mirrored,
                outcome.total,
                outcome.skipped,
                outcome.duration_ms,
            )
        else:
            logger.warning(
                "Computer %s stop backup timed out after %.1fs before workspace enumeration completed: "
                "mirrored %d folder(s), unfinished count unknown in %dms; continuing stop",
                computer_id,
                total_timeout_s,
                outcome.mirrored,
                outcome.duration_ms,
            )
        return

    if outcome.enumeration_failed:
        logger.warning(
            "Computer %s stop backup workspace enumeration failed (%s): "
            "mirrored %d folder(s), skipped count unknown in %dms",
            computer_id,
            outcome.enumeration_error,
            outcome.mirrored,
            outcome.duration_ms,
        )
        return

    if outcome.skipped is None:
        logger.warning(
            "Computer %s stop backup mirrored %d folder(s), skipped count unknown in %dms",
            computer_id,
            outcome.mirrored,
            outcome.duration_ms,
        )
    elif outcome.skipped > 0:
        logger.warning(
            "Computer %s stop backup mirrored %d folder(s), skipped %d workspace(s) in %dms",
            computer_id,
            outcome.mirrored,
            outcome.skipped,
            outcome.duration_ms,
        )
    else:
        logger.info(
            "Computer %s stop backup mirrored %d folder(s), skipped %d workspace(s) in %dms",
            computer_id,
            outcome.mirrored,
            outcome.skipped,
            outcome.duration_ms,
        )


async def run_stop_backup(
    manager: Any,
    binding: Any,
    *,
    workspace_id: str | None,
    durable_sandbox_id: str | None,
    timeout_s: float | None = None,
    total_timeout_s: float | None = None,
    close_timeout_s: float | None = None,
) -> StopBackupOutcome:
    """Run stop-time file backup under a shared deadline with temporary attach."""
    computer_id = binding.computer_id
    effective_attach_timeout = (
        float(timeout_s)
        if timeout_s is not None and timeout_s > 0
        else get_stop_backup_attach_timeout_s()
    )
    effective_total_timeout = (
        float(total_timeout_s)
        if total_timeout_s is not None and total_timeout_s > 0
        else get_stop_backup_total_timeout_s()
    )
    effective_close_timeout = (
        float(close_timeout_s)
        if close_timeout_s is not None and close_timeout_s > 0
        else get_stop_backup_close_timeout_s()
    )

    started_mono = time.monotonic()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + effective_total_timeout

    stats: dict[str, Any] = {}
    token = _mirror_stats_var.set(stats)
    folder_count: int | None = None
    timed_out = False
    try:
        attach_budget = min(effective_attach_timeout, max(0.0, deadline - loop.time()))
        async with attach_stop_backup_session(
            manager,
            binding,
            durable_sandbox_id,
            timeout_s=attach_budget,
            close_timeout_s=effective_close_timeout,
        ) as temp_session:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
            else:
                kwargs: dict[str, Any] = {
                    "workspace_id": workspace_id,
                    "expected_sandbox_id": durable_sandbox_id,
                    "routine": True,
                }
                if temp_session is not None:
                    kwargs["session"] = temp_session
                try:
                    async with asyncio.timeout_at(deadline):
                        folder_count = await manager._backup_machine_files_to_db(
                            computer_id,
                            **kwargs,
                        )
                except TimeoutError:
                    timed_out = True
    finally:
        _mirror_stats_var.reset(token)

    duration_ms = round((time.monotonic() - started_mono) * 1000)
    mirrored = (
        int(folder_count)
        if folder_count is not None
        else int(stats.get("mirrored", 0))
    )
    enumeration_failed = bool(stats.get("enumeration_failed"))
    enumeration_error = stats.get("enumeration_error")
    raw_total = stats.get("total")
    if enumeration_failed or raw_total is None:
        total: int | None = None
        skipped: int | None = None
    else:
        total = int(raw_total)
        skipped = max(0, total - mirrored)

    outcome = StopBackupOutcome(
        mirrored=mirrored,
        skipped=skipped,
        total=total,
        duration_ms=duration_ms,
        timed_out=timed_out,
        enumeration_failed=enumeration_failed,
        enumeration_error=enumeration_error,
    )
    _log_stop_backup_outcome(
        computer_id,
        outcome,
        total_timeout_s=effective_total_timeout,
    )
    return outcome
