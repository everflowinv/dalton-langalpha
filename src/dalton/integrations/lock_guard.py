"""Runtime self-check forbidding external HTTP calls while holding PostgreSQL advisory locks.

Every personal-integration path that talks to an upstream HTTP service (OAuth code
exchange, token refresh, 12-hour health patrol, single/full disconnect, remote
revocation) MUST release all PostgreSQL advisory locks and DB pool connections
before invoking any external client.

Usage:
- Wrap every PG advisory lock acquisition with ``track_advisory_lock(name, key)``.
- Call ``assert_no_advisory_lock_held(operation)`` right before external HTTP calls
  (both in concrete ``httpx`` clients and at service call sites before Protocol calls).
- Enabled automatically under pytest (``PYTEST_CURRENT_TEST``), via
  ``DALTON_INTEGRATION_LOCK_HTTP_GUARD=1``, or programmatically via
  ``advisory_lock_http_guard(True)`` / ``set_advisory_lock_http_guard(True)``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os

logger = logging.getLogger(__name__)

_held_advisory_locks: ContextVar[tuple[tuple[str, int], ...]] = ContextVar(
    "dalton_integration_held_advisory_locks",
    default=(),
)
_guard_override: ContextVar[bool | None] = ContextVar(
    "dalton_integration_lock_http_guard_override",
    default=None,
)
_default_guard_enabled: bool = False


class AdvisoryLockHeldDuringExternalCallError(RuntimeError):
    """Raised when an external HTTP client is invoked while holding a PG advisory lock."""

    def __init__(self, operation: str, held_locks: tuple[tuple[str, int], ...]) -> None:
        self.operation = operation
        self.held_locks = held_locks
        lock_names = ", ".join(f"{name}({key})" for name, key in held_locks)
        super().__init__(
            f"External client call '{operation}' attempted while holding PostgreSQL "
            f"advisory lock(s): {lock_names}"
        )


def set_advisory_lock_http_guard(enabled: bool) -> None:
    """Set the process-wide default for raising on external calls under advisory locks."""
    global _default_guard_enabled
    _default_guard_enabled = bool(enabled)


def is_advisory_lock_http_guard_enabled() -> bool:
    """Return True if the lock-vs-HTTP self-check should raise instead of only logging."""
    override = _guard_override.get()
    if override is not None:
        return override
    if _default_guard_enabled:
        return True
    env_flag = os.environ.get("DALTON_INTEGRATION_LOCK_HTTP_GUARD", "").strip().lower()
    if env_flag in ("1", "true", "yes", "on"):
        return True
    if env_flag in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


@contextmanager
def advisory_lock_http_guard(enabled: bool = True) -> Iterator[None]:
    """Context manager explicitly enabling or disabling the lock-vs-HTTP self-check."""
    token = _guard_override.set(bool(enabled))
    try:
        yield
    finally:
        _guard_override.reset(token)


@contextmanager
def track_advisory_lock(name: str, key: int) -> Iterator[None]:
    """Record that the current async context holds PG advisory lock ``(name, key)``."""
    prev = _held_advisory_locks.get()
    token = _held_advisory_locks.set(prev + ((name, int(key)),))
    try:
        yield
    finally:
        _held_advisory_locks.reset(token)


def held_advisory_locks() -> tuple[tuple[str, int], ...]:
    """Return all PG advisory locks currently held in the active async context."""
    return _held_advisory_locks.get()


def assert_no_advisory_lock_held(operation: str) -> None:
    """Verify no PostgreSQL advisory lock is held before making an external HTTP call."""
    held = _held_advisory_locks.get()
    if not held:
        return
    if is_advisory_lock_http_guard_enabled():
        raise AdvisoryLockHeldDuringExternalCallError(operation, held)
    logger.error(
        "[integration-lock-guard] external call '%s' attempted while holding PG advisory lock(s): %s",
        operation,
        ", ".join(f"{name}({key})" for name, key in held),
    )


__all__ = [
    "AdvisoryLockHeldDuringExternalCallError",
    "advisory_lock_http_guard",
    "assert_no_advisory_lock_held",
    "held_advisory_locks",
    "is_advisory_lock_http_guard_enabled",
    "set_advisory_lock_http_guard",
    "track_advisory_lock",
]
