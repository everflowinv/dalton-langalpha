"""Per-call caller identity for sandbox code jobs (B5).

The researchdata bridge stamps every AlphaEngine call it serves into a usage
ledger, keyed by who asked for it. The only hop that knows the caller is the
tool call itself (its ``RunnableConfig``); the job's environment is composed
deep inside the sandbox layer, which is generic upstream code with no notion
of a user. The identity therefore rides a ``ContextVar`` — the same pattern
``ProjectContext`` uses for the workspace folder — set around the code run
and read where the job's env is built, so it reaches the sandbox as
``DALTON_CALLER_*`` variables and from there as ``X-Dalton-*`` request
headers on the sandbox's builtin-http MCP calls.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from typing import Any

__all__ = ["bound_caller", "caller_env"]

_current: contextvars.ContextVar[Mapping[str, str] | None] = contextvars.ContextVar(
    "dalton_caller_identity", default=None
)

_ENV_NAMES: tuple[tuple[str, str], ...] = (
    ("user", "DALTON_CALLER_USER"),
    ("workspace", "DALTON_CALLER_WORKSPACE"),
    ("thread", "DALTON_CALLER_THREAD"),
)


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    # A value HTTP cannot frame, or that could smuggle another header, is
    # dropped rather than trimmed into something the caller never said.
    if any(ch in text for ch in "\r\n\t "):
        return ""
    return text


@contextlib.contextmanager
def bound_caller(user_id: Any, workspace_id: Any, thread_id: Any) -> Iterator[None]:
    """Bind the caller's identity for the awaited body, best effort.

    Never raises and never widens: an unset identity binds nothing, and the
    reset is unconditional so one call cannot leak into the next.
    """
    values = {
        key: cleaned
        for key, raw in (("user", user_id), ("workspace", workspace_id), ("thread", thread_id))
        if (cleaned := _clean(raw))
    }
    if not values:
        yield
        return
    token = _current.set(values)
    try:
        yield
    finally:
        _current.reset(token)


def caller_env() -> dict[str, str]:
    """The bound identity as the job env's ``DALTON_CALLER_*`` variables."""
    current = _current.get() or {}
    return {
        env_name: current[key]
        for key, env_name in _ENV_NAMES
        if current.get(key)
    }
