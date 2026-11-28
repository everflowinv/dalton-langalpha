"""Unified lifespan background loop wrappers for personal integrations.

Keeps the upstream ``src/server/app/setup.py`` hook to a thin 4-line delegation
while injecting the concrete Google HTTP ``RemoteRevoker`` into the purge-resume
and remote-revoke retry loops.
"""

from __future__ import annotations

from src.dalton.integrations.oauth_clients import get_google_remote_revoker
from src.dalton.integrations.tracker_calendar import (
    register_default_tracker_purge_handler,
)
from src.dalton.services.integration_data_purge import (
    run_personal_purge_resume_loop as _run_personal_purge_resume_loop,
)
from src.dalton.services.integration_health_sweep import (
    run_integration_health_sweep_loop,
)
from src.dalton.services.integration_revoke import (
    run_integration_revoke_retry_loop as _run_integration_revoke_retry_loop,
)

register_default_tracker_purge_handler()


async def run_personal_purge_resume_loop(*, poll_interval_s: float = 60.0) -> None:
    """Resume incomplete local 2A purges ONLY so remote HTTP revocations never delay local purge or thread unblocking.

    Due remote Google revocations are executed independently by ``run_integration_revoke_retry_loop``
    once all local 2A purges for the same ``(user_id, provider)`` batch have finished.
    """
    await _run_personal_purge_resume_loop(
        revoker=None,
        poll_interval_s=poll_interval_s,
    )


async def run_integration_revoke_retry_loop(*, poll_interval_s: float = 60.0) -> None:
    """Retry due remote Google revocations with the concrete ``RemoteRevoker``."""
    await _run_integration_revoke_retry_loop(
        revoker=get_google_remote_revoker(),
        poll_interval_s=poll_interval_s,
    )


__all__ = [
    "run_integration_health_sweep_loop",
    "run_integration_revoke_retry_loop",
    "run_personal_purge_resume_loop",
]
