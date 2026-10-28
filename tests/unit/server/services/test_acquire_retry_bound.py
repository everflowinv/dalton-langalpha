"""A3: acquiring a workspace must not recurse through the stop-retry cycle.

Drill finding: with the workspace row at ``running`` and its computer at
``stopped``, the attach's ``stamp_running_computer`` CAS fails
(``_MachineLeftRunning``), acquisition enters ``_await_stop_and_retry``, and
the re-entry lands in a stop wait again — forever. The bound is on the SECOND
stop-wait, counted separately from ``attempt`` (A3 review): a start-failure
retry bumps ``attempt`` without having waited on a stop, and must not turn the
first genuine stop-wait into an error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from src.server.database.computer import get_computer
from src.server.models.computer import ComputerStatus
from src.server.services.computer_manager import _lifecycle as lifecycle_mod
from src.server.services.computer_manager._lifecycle import SessionLifecycleMixin
from src.server.services.computer_manager._types import ComputerBinding


class _StubSelf(SessionLifecycleMixin):
    """Reaches the (patched) mixin methods; none of the real manager needed."""


_STUB_SELF = _StubSelf()


def _binding() -> ComputerBinding:
    return ComputerBinding(
        workspace_id="ws-1", computer_id="cmp-1", kind="docker", dir_name="ws-1"
    )


def _stopped_computer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lifecycle_mod,
        "get_computer",
        create_autospec(
            get_computer,
            return_value={"status": ComputerStatus.STOPPED},
        ),
    )


@pytest.mark.asyncio
async def test_second_stop_wait_raises_on_any_computer_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stopped_computer(monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        await SessionLifecycleMixin._await_stop_and_retry(
            _STUB_SELF,
            _binding(),
            user_id="u1",
            workspace_user_id="u1",
            on_state_observed=None,
            attempt=1,
            stop_waits=1,
        )
    assert "inconsistent" in str(exc_info.value)
    assert "stopped" in str(exc_info.value)


@pytest.mark.asyncio
async def test_running_workspace_stopped_computer_cycle_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drill's exact loop: attach fails its CAS, stop-wait retries, the
    re-entry stop-waits again — and the SECOND stop-wait raises instead of
    recursing further."""
    _stopped_computer(monkeypatch)

    stop_waits_seen: list[int] = []

    async def _fake_acquire_session(
        self: Any,
        workspace_id: str,
        *,
        user_id: str | None = None,
        on_state_observed: Any = None,
        _attempt: int = 0,
        _stop_waits: int = 0,
        **_: Any,
    ) -> Any:
        # The workspace row reads 'running' while the machine sits 'stopped':
        # every acquisition re-lands in the stop-wait path.
        stop_waits_seen.append(_stop_waits)
        if _stop_waits > 1:  # pragma: no cover - the fix must prevent this
            raise AssertionError("acquisition recursed past one stop-wait")
        return await SessionLifecycleMixin._await_stop_and_retry(
            self,
            _binding(),
            user_id=user_id,
            workspace_user_id=user_id,
            on_state_observed=on_state_observed,
            attempt=_attempt,
            stop_waits=_stop_waits,
        )

    monkeypatch.setattr(SessionLifecycleMixin, "_acquire_session", _fake_acquire_session)

    with pytest.raises(RuntimeError) as exc_info:
        await SessionLifecycleMixin._await_stop_and_retry(
            _STUB_SELF,
            _binding(),
            user_id="u1",
            workspace_user_id="u1",
            on_state_observed=None,
            attempt=0,
        )
    # Entering at the first stop-wait retries acquisition once (stop_waits=1);
    # that re-entry stop-waits again and the SECOND raises. Bounded, not
    # unbounded.
    assert stop_waits_seen == [1]
    assert "inconsistent" in str(exc_info.value)


@pytest.mark.asyncio
async def test_first_stop_wait_still_waits_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first stop-wait behaves as before: settle, then one retry."""
    _stopped_computer(monkeypatch)
    retried: list[tuple[str, int]] = []

    async def _fake_acquire_session(
        self: Any,
        workspace_id: str,
        *,
        user_id: str | None = None,
        on_state_observed: Any = None,
        _attempt: int = 0,
        _stop_waits: int = 0,
        **_: Any,
    ) -> Any:
        retried.append((workspace_id, _stop_waits))
        return "session"

    monkeypatch.setattr(SessionLifecycleMixin, "_acquire_session", _fake_acquire_session)
    result = await SessionLifecycleMixin._await_stop_and_retry(
        _STUB_SELF,
        _binding(),
        user_id="u1",
        workspace_user_id="u1",
        on_state_observed=None,
        attempt=0,
    )
    assert result == "session"
    # The retry carries the incremented stop-wait count.
    assert retried == [("ws-1", 1)]


@pytest.mark.asyncio
async def test_stop_wait_after_a_start_failure_retry_still_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3 review: `_await_in_flight_start`'s owner-failure retry bumps
    ``attempt`` to 1 with no stop waited. The FIRST genuine stop-wait on that
    chain must still wait and retry — only the second one errors."""
    _stopped_computer(monkeypatch)
    retried: list[int] = []

    async def _fake_acquire_session(
        self: Any,
        workspace_id: str,
        *,
        user_id: str | None = None,
        on_state_observed: Any = None,
        _attempt: int = 0,
        _stop_waits: int = 0,
        **_: Any,
    ) -> Any:
        retried.append(_attempt)
        return "session"

    monkeypatch.setattr(SessionLifecycleMixin, "_acquire_session", _fake_acquire_session)
    result = await SessionLifecycleMixin._await_stop_and_retry(
        _STUB_SELF,
        _binding(),
        user_id="u1",
        workspace_user_id="u1",
        on_state_observed=None,
        attempt=1,  # bumped by the start-failure retry, not by a stop-wait
        stop_waits=0,
    )
    assert result == "session"
    assert retried == [2]


@pytest.mark.asyncio
async def test_in_flight_start_attach_failure_uses_first_stop_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full chain: start finishes 'running', the attach loses its CAS
    (`_MachineLeftRunning`), and the resulting stop-wait is the chain's first —
    it waits (machine already settled) and retries acquisition once."""
    _stopped_computer(monkeypatch)

    async def _fake_wait_start(self: Any, workspace_id: str) -> dict[str, Any]:
        return {"status": "running"}

    async def _fake_attach(*args: Any, **kwargs: Any) -> Any:
        raise lifecycle_mod._MachineLeftRunning("cmp-1")

    def _fake_lock(computer_id: str, reason: str, **kwargs: Any):
        class _NullLock:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _NullLock()

    retried: list[tuple[int, int]] = []

    async def _fake_acquire_session(
        self: Any,
        workspace_id: str,
        *,
        user_id: str | None = None,
        on_state_observed: Any = None,
        _attempt: int = 0,
        _stop_waits: int = 0,
        **_: Any,
    ) -> Any:
        retried.append((_attempt, _stop_waits))
        return "session"

    monkeypatch.setattr(
        SessionLifecycleMixin, "_wait_for_start_completion", _fake_wait_start
    )
    monkeypatch.setattr(SessionLifecycleMixin, "_attach_running_session", _fake_attach)
    # _observed_lock lives on another mixin: patch it on the stub instance
    # (raising=False — the stub itself never had it), where the plain function
    # binds nothing (matches the call shape).
    monkeypatch.setattr(_STUB_SELF, "_observed_lock", _fake_lock, raising=False)
    monkeypatch.setattr(SessionLifecycleMixin, "_acquire_session", _fake_acquire_session)

    result = await SessionLifecycleMixin._await_in_flight_start(
        _STUB_SELF,
        _binding(),
        user_id="u1",
        workspace_user_id="u1",
        on_state_observed=None,
        mark=lambda name: None,
        attempt=1,  # already bumped by the owner-failure retry
        stop_waits=0,
    )
    assert result == "session"
    # First stop-wait waited (no error) and re-entered acquisition with
    # stop_waits=1 — the start-retry's attempt did NOT consume the stop budget.
    assert retried == [(2, 1)]
