"""Coordination guards for ``.wsfiles-relay-*`` restore staging files.

Prevents two production failure modes where relayed staging files vanish before
placement:
1. Ambient ``ProjectContext`` folding ``aupload_file_bytes`` paths into another
   workspace's folder when restore runs inside an active turn context.
2. Concurrent orphan sweeps (``pull_direct(..., sweep_orphans=True)``) unlinking
   unlocked ``.wsfiles-relay-*`` files uploaded by an in-flight ``_restore_relay``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ptc_agent.core import project_context as _project_context


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    waiters: int = 0


_TRANSFER_LOCKS: dict[tuple[str | int, str], _LockEntry] = {}
_HELD_TRANSFER_KEYS: ContextVar[frozenset[tuple[str | int, str]]] = ContextVar(
    "dalton_relay_transfer_lock_keys", default=frozenset()
)


def _transfer_key(sandbox: Any, workspace_root: str) -> tuple[str | int, str]:
    sandbox_id = getattr(sandbox, "sandbox_id", None)
    sb_key: str | int = (
        sandbox_id if isinstance(sandbox_id, str) and sandbox_id else id(sandbox)
    )
    return (sb_key, os.path.normpath(workspace_root))


@contextmanager
def without_ambient_project() -> Iterator[None]:
    """Temporarily clear ``current_project()`` so explicit layout paths do not fold."""
    token = _project_context._current.set(None)
    try:
        yield
    finally:
        _project_context._current.reset(token)


@asynccontextmanager
async def relay_transfer_lock(sandbox: Any, workspace_root: str) -> AsyncIterator[None]:
    """Serialize relay staging/placement with sweeping pulls on the same root.

    Re-entrant within the same asyncio task so ``_restore_relay`` can hold the
    lock across both staging uploads and its own ``pull_direct`` call without
    deadlocking while concurrent tasks' sweeping ``pull_direct`` calls wait.
    """
    key = _transfer_key(sandbox, workspace_root)
    held = _HELD_TRANSFER_KEYS.get()
    if key in held:
        yield
        return

    entry = _TRANSFER_LOCKS.get(key)
    if entry is None:
        entry = _LockEntry(lock=asyncio.Lock())
        _TRANSFER_LOCKS[key] = entry
    entry.waiters += 1
    try:
        await entry.lock.acquire()
    except BaseException:
        entry.waiters -= 1
        if entry.waiters == 0 and not entry.lock.locked() and _TRANSFER_LOCKS.get(key) is entry:
            _TRANSFER_LOCKS.pop(key, None)
        raise

    token = _HELD_TRANSFER_KEYS.set(held | {key})
    try:
        yield
    finally:
        _HELD_TRANSFER_KEYS.reset(token)
        entry.waiters -= 1
        entry.lock.release()
        if entry.waiters == 0 and not entry.lock.locked() and _TRANSFER_LOCKS.get(key) is entry:
            _TRANSFER_LOCKS.pop(key, None)
