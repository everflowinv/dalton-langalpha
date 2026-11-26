"""Stop-path temporary sandbox attach for file backup on session-less workers.

When ``_finish_claimed_stop`` runs on a worker that does not hold a cached
session for the computer (multi-worker routing, process restart, or retired
idle session), upstream ``backup_project_files(strict=False)`` skips every
workspace on the computer. This module attaches a short-lived, running-only
``PTCSandbox``/``Session`` handle for the stop-time backup, releases it as soon
as backup finishes, and falls back to existing skip behavior on timeout,
non-running state, or connect error while surfacing the skipped workspace count.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ptc_agent.core.sandbox.attach_only import attach_only
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import RuntimeState
from ptc_agent.core.session import Session

from src.dalton.settings import get_stop_backup_attach_timeout_s

logger = logging.getLogger(__name__)

_mirror_stats_var: ContextVar[dict[str, int] | None] = ContextVar(
    "dalton_stop_backup_mirror_stats", default=None
)


@dataclass(frozen=True, slots=True)
class StopBackupOutcome:
    """Result of one computer stop-time backup pass."""

    mirrored: int
    skipped: int
    total: int | None = None


def record_mirror_outcome(*, total: int, mirrored: int) -> None:
    """Record project mirror counts for an active ``run_stop_backup`` scope."""
    stats = _mirror_stats_var.get()
    if stats is None:
        return
    stats["total"] = int(total)
    stats["mirrored"] = int(mirrored)
    stats["skipped"] = max(0, int(total) - int(mirrored))


def _is_running_state(state: Any) -> bool:
    return state == RuntimeState.RUNNING or getattr(state, "value", None) == "running"


async def _safe_close_provider(provider: Any) -> None:
    if provider is None:
        return
    try:
        await provider.close()
    except Exception:
        logger.debug("Ignoring error closing temporary stop-backup provider", exc_info=True)


async def _safe_close_sandbox(sandbox: Any) -> None:
    if sandbox is None:
        return
    try:
        await sandbox.close()
    except Exception:
        logger.debug("Ignoring error closing temporary stop-backup sandbox", exc_info=True)


@asynccontextmanager
async def attach_stop_backup_session(
    manager: Any,
    binding: Any,
    durable_sandbox_id: str | None,
    *,
    timeout_s: float | None = None,
) -> AsyncIterator[Session | None]:
    """Attach a temporary running-only session for stop-time backup if needed.

    Yields ``None`` when this worker already caches a matching session, when
    there is no durable sandbox, when the sandbox is not running, or when
    connecting fails/times out. Never starts a stopped sandbox and never
    registers the temporary handle in ``SessionManager`` or the manager cache.
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
        if timeout_s is not None and timeout_s > 0
        else get_stop_backup_attach_timeout_s()
    )
    provider: Any = None
    sandbox: PTCSandbox | None = None
    session: Session | None = None

    try:
        core_config = manager._core_config_for(binding)
        provider = manager._provider_for(binding)
        async with asyncio.timeout(effective_timeout):
            runtime = await provider.get(durable_sandbox_id)
            state = await runtime.get_state()
            if not _is_running_state(state):
                logger.info(
                    "Computer %s sandbox %s is %s (not running); skipping temporary stop-backup attach",
                    computer_id,
                    durable_sandbox_id,
                    getattr(state, "value", state),
                )
                await _safe_close_provider(provider)
                provider = None
                yield None
                return
            work_dir = (
                await runtime.fetch_working_dir()
                or binding.root_dir
                or core_config.filesystem.working_directory
            )

        sandbox = PTCSandbox(
            core_config,
            None,
            resource_tier=binding.resource_tier,
            host_unavailable_policy=None,
            provider=provider,
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
            effective_timeout,
        )
        await _safe_close_provider(provider)
        provider = None
        yield None
        return
    except Exception as exc:  # noqa: BLE001 - best-effort attach must never block stop
        logger.warning(
            "Computer %s temporary stop-backup attach failed (%s: %s); "
            "continuing without session and keeping previous mirror",
            computer_id,
            type(exc).__name__,
            exc,
        )
        await _safe_close_provider(provider)
        provider = None
        yield None
        return

    try:
        with attach_only():
            yield session
    finally:
        if session is not None:
            session.sandbox = None
            session._initialized = False
        await _safe_close_sandbox(sandbox)
        await _safe_close_provider(provider)


async def run_stop_backup(
    manager: Any,
    binding: Any,
    *,
    workspace_id: str | None,
    durable_sandbox_id: str | None,
    timeout_s: float | None = None,
) -> StopBackupOutcome:
    """Run stop-time file backup with temporary attach on session-less workers."""
    computer_id = binding.computer_id
    stats: dict[str, int] = {}
    token = _mirror_stats_var.set(stats)
    try:
        async with attach_stop_backup_session(
            manager,
            binding,
            durable_sandbox_id,
            timeout_s=timeout_s,
        ) as temp_session:
            kwargs: dict[str, Any] = {
                "workspace_id": workspace_id,
                "expected_sandbox_id": durable_sandbox_id,
                "routine": True,
            }
            if temp_session is not None:
                kwargs["session"] = temp_session
            folder_count = await manager._backup_machine_files_to_db(
                computer_id,
                **kwargs,
            )
    finally:
        _mirror_stats_var.reset(token)

    mirrored = int(folder_count or 0)
    total = stats.get("total")
    skipped = max(0, total - mirrored) if total is not None else 0
    return StopBackupOutcome(mirrored=mirrored, skipped=skipped, total=total)
