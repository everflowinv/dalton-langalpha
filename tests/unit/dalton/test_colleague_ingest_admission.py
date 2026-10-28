"""Integration B x C: ingest tools inside a colleague's admitted workspace.

A colleague turn keeps ``user_id`` = the dispatcher and runs in the owner's
workspace; START already admitted it there, so the ingest tools must not
re-apply the strict owner check to that one workspace — and must keep it for
any other workspace.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.dalton.tools import ingest_tools, wiki_tools

DISPATCHER = "u-internal-a"
OWNER_WS = "ws-owned-by-b"


def _config(**extra: Any) -> dict[str, Any]:
    configurable = {
        "user_id": DISPATCHER,
        "workspace_id": OWNER_WS,
        "thread_id": "th-1",
        "run_id": "r-1",
        **extra,
    }
    return {"configurable": configurable, "metadata": {}, "tags": [], "callbacks": None}


@pytest.fixture(autouse=True)
def _internal_caller(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        ingest_tools,
        "get_user_tier",
        create_autospec(ingest_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        ingest_tools,
        "get_workspace_auto_ingest",
        create_autospec(ingest_tools.get_workspace_auto_ingest, return_value=False),
    )
    strict = create_autospec(
        ingest_tools.verify_workspace_access,
        side_effect=HTTPException(status_code=403, detail="Workspace does not belong to current user"),
    )
    monkeypatch.setattr(ingest_tools, "verify_workspace_access", strict)
    return strict


ARGS = {
    "speaker": "expert_or_company",
    "target_type": "company",
    "target": "NVDA",
    "content_type": "专家访谈",
    "date": "2026-10-25",
    "content": "专家表示公司三季度收入增长稳健，资本开支节奏放缓。",
}


@pytest.mark.asyncio
async def test_ingest_in_the_turns_admitted_workspace_skips_the_owner_check(
    monkeypatch: pytest.MonkeyPatch, _internal_caller: Any
) -> None:
    bridge = create_autospec(
        wiki_tools.call_bridge_wiki_ingest,
        return_value=(200, {"ok": True, "status": "dry_run"}),
    )
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
    tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)

    out = json.loads(
        await tool.ainvoke(ARGS, config=_config(turn_admitted_workspace_id=OWNER_WS))
    )

    assert out["status"] == "dry_run"
    _internal_caller.assert_not_awaited()
    payload = bridge.call_args.args[0]
    assert payload["ingested_by"] == {"user_id": DISPATCHER}
    assert payload["ingested_via"] == {"workspace_id": OWNER_WS}


@pytest.mark.asyncio
async def test_any_other_workspace_keeps_the_strict_owner_check(
    monkeypatch: pytest.MonkeyPatch, _internal_caller: Any
) -> None:
    bridge = create_autospec(wiki_tools.call_bridge_wiki_ingest, return_value=(200, {"ok": True}))
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", bridge)
    tool = ingest_tools.build_ingest_research_tool(None, allow_home_workspace=False)

    for cfg in (_config(), _config(turn_admitted_workspace_id="ws-somewhere-else")):
        out = await tool.ainvoke(ARGS, config=cfg)
        assert out.startswith("ERROR: Workspace does not belong")
    bridge.assert_not_awaited()
