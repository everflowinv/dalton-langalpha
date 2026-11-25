"""dalton_schedule_automation: price triggers are refused at the tool, and the docstring
states the contract the scheduler actually implements."""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import pytest

import src.dalton.tools.director_tools as director_tools_mod
from src.dalton.tools.director_tools import build_director_tools

USER = "user-sched-1"
WS = "11111111-1111-4111-8111-111111111111"


def _schedule_tool():
    return {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]


@pytest.mark.asyncio
async def test_schedule_automation_refuses_price_trigger_before_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = create_autospec(director_tools_mod.schedule_dalton_automation_helper)
    monkeypatch.setattr(director_tools_mod, "schedule_dalton_automation_helper", helper)
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    out = json.loads(
        await _schedule_tool().ainvoke(
            {
                "action": "upsert",
                "name": "dalton-meta-price",
                "trigger_type": "price",
                "instruction": "alert on move",
            },
            config=cfg,
        )
    )

    assert out["ok"] is False
    assert out["error"] == "price_trigger_unsupported"
    assert "dalton_coverage_tracking" in out["message"]
    assert "trigger_config" in out["message"]
    helper.assert_not_awaited()


def test_dalton_schedule_automation_docstring_matches_contract() -> None:
    sched = _schedule_tool()
    desc = sched.description or ""
    fields = sched.args_schema.model_fields if sched.args_schema else {}
    assert "llm_model" in desc and "llm_model" in fields
    assert "max_failures" not in desc and "max_failures" not in fields
    assert 'trigger_type: "cron" or "once"' in desc
    assert '"price"' not in desc
    assert "dalton_coverage_tracking" in desc
    assert "trigger_config" in desc
    assert "Asia/Shanghai" not in desc
    assert "deployment default" in desc
    assert "keep the existing timezone" in desc
    assert "does not move" in desc
    assert "Built-in Antigravity effort is determined by the task's model ID" in desc
