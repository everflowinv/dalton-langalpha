"""Shared doubles for the dalton unit suite.

The publish audit routes its CPU phases through the killable subprocess runner
(``src.dalton/heavy_compute``). In unit tests that runner is replaced with an
off-loop executor: the phases' own logic (deadline checks included) still runs,
while the suite stays fast and hermetic. The runner itself has its own direct
tests in ``test_heavy_compute.py`` and one whole-audit test in
``test_audit_real_subprocess.py``, which DO NOT go through this fixture —
mark a test with ``@pytest.mark.real_heavy_subprocess`` to keep the real runner.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.heavy_compute import run_in_heavy_subprocess


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_heavy_subprocess: keep the real killable-subprocess runner (no inline double)",
    )


async def _run_off_loop(
    fn: Any,
    /,
    *args: Any,
    timeout_s: float | None = None,
    deadline: float | None = None,
    label: str = "",
    **kwargs: Any,
) -> Any:
    # In a thread, like the real runner's wait pool: the audit's hotspots must
    # stay off the event loop even under the double
    # (test_audit_config_and_offload asserts exactly that).
    return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.fixture(autouse=True)
def inline_heavy_compute(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("real_heavy_subprocess"):
        # The real runner: spawn, pickle, and child imports are exactly what
        # the marked test means to exercise.
        yield
        return
    runner = create_autospec(run_in_heavy_subprocess, side_effect=_run_off_loop)
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import service as service_mod

    monkeypatch.setattr(audit_mod, "run_in_heavy_subprocess", runner)
    monkeypatch.setattr(service_mod, "run_in_heavy_subprocess", runner)
    yield
