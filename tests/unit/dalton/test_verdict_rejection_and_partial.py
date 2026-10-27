"""knowledge_append_verdict 的前置 schema 说明与被拒/部分成功返回。

线上 12 小时里 5 次首写被拒都是模型在调用时看不到 verdict 字段约束（多出
evidence/basis/summary/evidence_refs、next_validation 写成 text/when、trigger 多
source）；本文件锁定：拒绝返回只列未知字段与缺少的字段并给最小示例，部分成功
（verdict 已存、agenda 未存）明确写出 verdict_saved/agenda_saved 与补救调用。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.knowledge.service import _verdict_rejection_message
from src.dalton.knowledge.validate import validate_verdict
import src.dalton.tools.knowledge_tools as kt_mod
from src.dalton.tools.knowledge_tools import create_knowledge_tools


def _incident_shaped_verdict() -> dict[str, Any]:
    """The exact misspellings the incident produced, on one payload."""
    return {
        "at": "2026-10-27T03:40:00Z",
        "trigger": {"kind": "internal_material", "source": "watch"},
        "event": "META 一致预期无实质变化",
        "verdict": "NO_CHANGE",
        "evidence": ["E-1"],
        "basis": "卖方无修正",
        "next_validation": {"text": "看 Q4 广告", "when": "2026-11-05"},
        "progress": "none",
    }


def test_rejection_lists_unknown_and_missing_fields_with_minimal_example() -> None:
    """未知字段、缺少字段按路径列出，附最小合规示例——不再倾倒期望结构。"""
    val = validate_verdict(_incident_shaped_verdict(), thesis_doc=None)
    assert not val.ok
    msg = _verdict_rejection_message(val.errors)

    assert "未知字段（schema 不认，删除后重试）：" in msg
    for unknown in (
        "/evidence",
        "/basis",
        "/trigger/source",
        "/next_validation/text",
        "/next_validation/when",
    ):
        assert unknown in msg, msg
    assert "缺少必填字段（补上后重试）：" in msg
    for missing in ("/rationale", "/next_validation/what", "/next_validation/due"):
        assert missing in msg, msg
    assert "最小合规示例：" in msg
    assert '"verdict":"NO_CHANGE"' in msg
    assert '"next_validation":{"what"' in msg


def test_rejection_message_surfaces_enum_and_other_problems() -> None:
    """枚举等非字段名问题仍列出，不吞掉。"""
    val = validate_verdict(
        {
            "at": "2026-10-27T03:40:00Z",
            "trigger": {"kind": "wire"},
            "event": "e",
            "verdict": "MAYBE",
            "rationale": "r",
            "next_validation": {"what": "w", "due": "2026-11-05"},
            "progress": "none",
        },
        thesis_doc=None,
    )
    assert not val.ok
    msg = _verdict_rejection_message(val.errors)
    assert "其他问题：" in msg
    assert "/trigger" in msg and "/verdict" in msg


@pytest.mark.asyncio
async def test_partial_success_returns_verdict_saved_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verdict 已入库、agenda 未更新时，返回里明确 verdict_saved=true、
    agenda_saved=false 与一条补救调用，模型不必重调 append。"""
    monkeypatch.setattr(
        kt_mod,
        "verify_workspace_access",
        create_autospec(kt_mod.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        kt_mod,
        "_resolve_thread_origin",
        create_autospec(kt_mod._resolve_thread_origin, return_value=(None, None)),
    )
    monkeypatch.setattr(
        kt_mod,
        "resolve_verdict_signals",
        create_autospec(kt_mod.resolve_verdict_signals, return_value=0),
    )

    failed_agenda_result: dict[str, Any] = {
        "ok": True,
        "verdict_id": "VD-20261027-01",
        "word": "NO_CHANGE",
        "agenda_version_no": None,
        "warnings": [],
        "agenda_update": "failed",
        "agenda_error": "ops 校验失败",
        "agenda_problems": [{"path": "/", "code": "schema_error", "message": "…"}],
        "message": (
            "Recorded verdict VD-20261027-01 (NO_CHANGE / 无变化); agenda_update: "
            "failed (verdict saved, but agenda was NOT updated — please retry with "
            'knowledge_update(doc="agenda", ...)):\nops 校验失败'
        ),
    }
    append_stub = create_autospec(kt_mod.append_verdict, return_value=failed_agenda_result)
    monkeypatch.setattr(kt_mod, "append_verdict", append_stub)

    tools = create_knowledge_tools(None)
    verdict_tool = {t.name: t for t in tools}["knowledge_append_verdict"]
    res = await verdict_tool.ainvoke(
        {
            "name": "knowledge_append_verdict",
            "id": "tc-partial-1",
            "type": "tool_call",
            "args": {
                "verdict": {
                    "verdict": "NO_CHANGE",
                    "event": "e",
                    "rationale": "r",
                    "progress": "none",
                    "trigger": {"kind": "internal_material", "ref": "sig-1"},
                    "next_validation": {"what": "w", "due": "2026-11-05"},
                },
                "agenda_ops": [{"op": "set", "path": "/watch/x", "value": 1}],
                "workspace_id": "ws-1",
            },
        },
        config={"configurable": {"user_id": "u-1", "workspace_id": "ws-1"}},
    )
    payload = json.loads(res.content if hasattr(res, "content") else str(res))
    assert payload["verdict_saved"] is True
    assert payload["agenda_saved"] is False
    assert payload["verdict_id"] == "VD-20261027-01"
    assert 'knowledge_update(doc="agenda"' in payload["recovery"]
    assert "不要重复调用 knowledge_append_verdict" in payload["recovery"]
    assert "ops 校验失败" in payload["detail"]
