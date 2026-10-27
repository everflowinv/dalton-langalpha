"""Caller dates reach the upstream CLI as YYYY-MM-DD or come back as a parameter error.

Any other shape made the CLI exit before printing, which the agent read as
"upstream unavailable" and gave up on the route.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


def _fanout_bridge() -> SimpleNamespace:
    """A minimal `self` for the fan-out arg builders (they only read the config)."""
    cfg = create_autospec(rb.BridgeConfig, instance=True)
    cfg.caller_budget_s = 300
    bridge = SimpleNamespace(cfg=cfg)
    bridge._compute_fanout_timeout = rb.ResearchDataBridge._compute_fanout_timeout.__get__(bridge)
    return bridge


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
        "research", {"route": "transcript", "ticker": "NVDA", **args}
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


def test_alphaengine_gets_its_date_window_as_a_pair():
    import datetime as dt

    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "英伟达", "start": "2026-09"}
    )
    assert err is None
    assert normalized["start"] == "2026-09-01"
    assert normalized["end"] == max(dt.date.today().isoformat(), "2026-09-01")

    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "英伟达", "end": "2026-10-26"}
    )
    assert err is None
    assert normalized["start"] == "2025-10-26"


def test_upstream_argument_codes_are_parameter_errors():
    category, _ = rb._classify_error_category_and_hint(
        "research",
        "alphaengine", "search_library", {"ok": False, "error": "E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供"}
    )
    assert category == "参数错误"


def test_an_end_after_the_hosts_today_stops_at_today(monkeypatch):
    # The agent's today (UTC / Asia/Shanghai) is a day ahead of this host's in
    # the US evening; the CLI refuses any date after its own today.
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "transcript", "ticker": "NVDA", "start": "2026-10-01", "end": "2026-10-27"}
    )
    assert err is None
    assert normalized["end"] == "2026-10-26"

    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "英伟达", "start": "2026-10-01"}
    )
    assert err is None
    assert normalized["end"] == "2026-10-26"


def test_a_start_after_today_is_a_parameter_error(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    _, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "transcript", "ticker": "NVDA", "start": "2026-10-27"}
    )
    assert err is not None and err["error"].startswith("[参数错误]")
    assert "晚于今天" in err["error"]


def test_nested_alphaengine_dates_are_normalized(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {"route": "alphaengine", "query": "英伟达", "params": {"date_from": "2026-09", "date_to": "2026-10-27T08:00:00"}},
    )
    assert err is None
    assert normalized["params"]["date_from"] == "2026-09-01"
    assert normalized["params"]["date_to"] == "2026-10-26"

    _, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "英伟达", "params": {"date_from": "soon"}}
    )
    assert err is not None and err["error"].startswith("[参数错误]")


def test_a_top_level_end_and_a_nested_start_already_make_a_pair(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {"route": "alphaengine", "query": "NVDA", "start": "2026-09-01", "params": {"date_to": "2026-09-30"}},
    )
    assert err is None
    assert "end" not in normalized or normalized["end"] is None


def test_a_reversed_window_is_a_parameter_error(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    _, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "alphaengine", "query": "NVDA", "start": "2026-09-30", "end": "2026-09-01"}
    )
    assert err is not None and "晚于结束日期" in err["error"]


def test_a_current_month_end_stops_at_today(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "transcript", "ticker": "NVDA", "start": "2026-10", "end": "2026-10"}
    )
    assert err is None
    assert (normalized["start"], normalized["end"]) == ("2026-10-01", "2026-10-26")


def test_alphaengine_fanout_closes_a_lone_start_on_bridge_today(monkeypatch):
    # Beijing is already on the 27th while this host is still on the 26th: a
    # UTC-derived end made start > end and the whole route='all' call came
    # back as "上游不可用".
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-27")
    ae_args = rb.ResearchDataBridge._build_alphaengine_fanout_args(
        _fanout_bridge(), {"query": "NVDA", "start": "2026-10-20"}
    )
    assert ae_args["start"] == "2026-10-20"
    assert ae_args["end"] == "2026-10-27"


def test_fanout_legs_truncate_an_end_after_bridge_today(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    ae_args = rb.ResearchDataBridge._build_alphaengine_fanout_args(
        _fanout_bridge(), {"query": "NVDA", "start": "2026-10-01", "end": "2026-10-30"}
    )
    assert (ae_args["start"], ae_args["end"]) == ("2026-10-01", "2026-10-26")
    gp_args = rb.ResearchDataBridge._build_guidepoint_fanout_args(
        _fanout_bridge(), {"query": "NVDA", "start": "2026-10-01", "end": "2026-10-30"}
    )
    assert (gp_args["start"], gp_args["end"]) == ("2026-10-01", "2026-10-26")


def test_guidepoint_fanout_keeps_a_lone_start_and_wraps_string_keywords(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    gp_args = rb.ResearchDataBridge._build_guidepoint_fanout_args(
        _fanout_bridge(),
        {"query": "Meta advertising", "start": "2026-10-01", "params": {"keywords": "reels"}},
    )
    assert gp_args["start"] == "2026-10-01"
    assert "end" not in gp_args  # Guidepoint, unlike AlphaEngine, takes half a window
    assert gp_args["params"]["keywords"] == ["reels"]


def test_a_future_as_of_is_a_parameter_error_not_truncated(monkeypatch):
    # as_of is a point in time: there is no "up to now" to clamp it to.
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    _, _, err = rb._normalize_and_validate_call_args(
        "finance",
        {"route": "sec", "ticker": "META", "operation": "facts", "concept": "Revenues", "as_of": "2026-10-27"},
    )
    assert err is not None
    assert err["error_category"] == "参数错误"
    assert "晚于今天" in err["error"]


def test_a_future_vintage_is_a_parameter_error(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    _, _, err = rb._normalize_and_validate_call_args(
        "macro",
        {"route": "fred", "series": "CPIAUCSL", "start": "2026-01-01", "end": "2026-09-30", "vintage": "2026-10-27"},
    )
    assert err is not None and err["error"].startswith("[参数错误]")
    assert "vintage" in err["error"]


def test_a_month_or_quarter_as_of_is_refused_for_a_point_in_time(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    _, _, err = rb._normalize_and_validate_call_args(
        "finance", {"route": "sec", "ticker": "META", "as_of": "2026Q3"}
    )
    assert err is not None and err["error"].startswith("[参数错误]")
    assert "YYYY-MM-DD" in err["error"]


def test_an_exact_as_of_or_vintage_still_passes(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "finance", {"route": "sec", "ticker": "META", "as_of": "2026-09-30"}
    )
    assert err is None
    assert normalized["as_of"] == "2026-09-30"


def test_guidepoint_nested_start_and_end_dates_are_normalized(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "guidepoint",
            "query": "Meta advertising",
            "params": {"start_date": "2026-09", "end_date": "2026-10-30T08:00:00"},
        },
    )
    assert err is None
    assert normalized["params"]["start_date"] == "2026-09-01"
    assert normalized["params"]["end_date"] == "2026-10-26"


def test_guidepoint_string_keywords_become_a_single_element_list():
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "guidepoint", "query": "Meta", "params": {"keywords": "reels"}}
    )
    assert err is None
    assert normalized["params"]["keywords"] == ["reels"]


def test_guidepoint_fanout_carries_normalized_nested_start_and_end_dates(monkeypatch):
    monkeypatch.setattr(rb, "_bridge_today", lambda: "2026-10-26")
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "all",
            "query": "Meta advertising",
            "params": {"start_date": "2026-09", "end_date": "2026-10-30T08:00:00"},
        },
    )
    assert err is None
    gp_args = rb.ResearchDataBridge._build_guidepoint_fanout_args(
        _fanout_bridge(), normalized
    )
    assert gp_args["params"]["start_date"] == "2026-09-01"
    assert gp_args["params"]["end_date"] == "2026-10-26"
