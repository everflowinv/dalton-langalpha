"""One whole audit through the REAL killable-subprocess runner.

The conftest double covers speed for the suite; this test keeps the real
runner (marker opts out of the double) so the things a double can never catch
— unpicklable arguments, a task function that is not importable from a module
top level, a spawn-time import failure in the child — fail HERE instead of in
production. Small draft, two sources, no LLM (``use_llm_judge=False``), and
the result must equal what the off-loop double path produces.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.heavy_compute import run_in_heavy_subprocess
from src.dalton.publish import audit as audit_mod
from src.dalton.publish.audit import aaudit_draft

_DRAFT = """# 草稿

2024 财年 Revenue 为 $1,234 million[^1]，净利润为 $456 million[^2]。

[^1]: prov:p1
[^2]: prov:p2
"""

_PROV: dict[str, dict[str, Any]] = {
    "p1": {
        "record_id": "p1",
        "source_type": "web_fetch",
        "identifier": "https://example.com/results",
        "body": "The company reported Revenue of $1,234 million in FY2024.",
    },
    "p2": {
        "record_id": "p2",
        "source_type": "web_fetch",
        "identifier": "https://example.com/net-income",
        "body": "Net income came in at $456 million for fiscal 2024.",
    },
}


def _summaries(audit: Any) -> tuple[Any, ...]:
    return (
        audit.figures,
        audit.matched,
        len(audit.unmatched),
        len(audit.uncertain),
        audit.passed,
        tuple(sorted(m["raw"] for m in audit.matches)),
    )


@pytest.mark.real_heavy_subprocess
@pytest.mark.asyncio
async def test_small_audit_runs_real_subprocess_and_matches_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = time.monotonic()
    real = await aaudit_draft(
        _DRAFT, provenance_records=_PROV, use_llm_judge=False, user_id="real-subprocess-user"
    )
    elapsed = time.monotonic() - t0

    # Both figures verified through the prov: footnotes matched both numbers.
    assert _summaries(real) == (2, 2, 0, 0, True, ("$1,234 million", "$456 million"))
    assert real.inputs_sha256  # the identity hash also survived the round trip
    assert elapsed < 20.0, f"real-subprocess audit took {elapsed:.1f}s; too slow for CI"

    # The off-loop double path (what the rest of the suite runs) must produce
    # the same audit for the same inputs.
    async def _off_loop(
        fn: Any,
        /,
        *args: Any,
        timeout_s: float | None = None,
        deadline: float | None = None,
        label: str = "",
        **kwargs: Any,
    ) -> Any:
        import asyncio

        return await asyncio.to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(
        audit_mod,
        "run_in_heavy_subprocess",
        create_autospec(run_in_heavy_subprocess, side_effect=_off_loop),
    )
    doubled = await aaudit_draft(
        _DRAFT, provenance_records=_PROV, use_llm_judge=False, user_id="real-subprocess-user"
    )
    assert _summaries(doubled) == _summaries(real)
    assert doubled.inputs_sha256 == real.inputs_sha256
