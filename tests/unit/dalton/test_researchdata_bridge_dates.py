"""Caller dates reach the upstream CLI as YYYY-MM-DD or come back as a parameter error.

Any other shape made the CLI exit before printing, which the agent read as
"upstream unavailable" and gave up on the route.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


@pytest.mark.parametrize(
    ("args", "start", "end"),
    [
        ({"start": "2026-09-01", "end": "2026-10-26"}, "2026-09-01", "2026-10-26"),
        ({"start": "2026-09-01T08:00:00Z"}, "2026-09-01", None),
        ({"start": "2026-09", "end": "2026-09"}, "2026-09-01", "2026-09-30"),
        ({"start": "2025Q4", "end": "2025Q4"}, "2025-10-01", "2025-12-31"),
        ({"start": "2026", "end": "20261026"}, "2026-01-01", "2026-10-26"),
        ({"date_from": "2026/2"}, "2026-02-01", None),
    ],
)
def test_research_dates_are_coerced(args, start, end):
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "英伟达", **args}
    )
    assert err is None
    assert normalized.get("start") == start
    assert normalized.get("end") == end


@pytest.mark.parametrize("bad", ["last week", "2026-13", "2026-02-30"])
def test_an_unreadable_date_is_a_parameter_error(bad):
    _, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "transcript", "ticker": "NVDA", "start": bad}
    )
    assert err is not None
    assert err["status"] == "invalid_arguments"
    assert err["error"].startswith("[参数错误]")
