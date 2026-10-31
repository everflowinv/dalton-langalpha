"""Sector knowledge layer (sector layer P1): schema profile, sector doc, and the
company path staying byte-identical with the flag off.

锁定的行为：
- profile 由覆盖单元注册表决定：开关关闭一律公司（且不读注册表）；开启后 active
  的 sector 单元 → 行业 profile，company 单元/未登记 → 公司 profile；
- 行业工作区：sector/thesis/debate/drivers/lessons/agenda/models 可写，management
  与 company 被明确拒绝（错误说清行业语义），共享文档校验与不变量原样生效；
- 公司工作区：八份文档行为不变，sector 被明确拒绝；
- 渲染：sector.md 与 agent.md 托管块的行业标题；公司块内容不变；
- 完成契约：行业分支知识文档（knowledge_docs_if_sector）由工作区身份决定，
  与 params.subject_kind 无关，冲突取值被明确拒绝；
- d024：只扩 doc_key CHECK，downgrade 在有 sector 行时拒绝执行。

真实校验 / 渲染 / 路径代码全程在测（只有 DB 层用 create_autospec / monkeypatch 替身）。
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.knowledge.profiles as profiles_mod
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge import reconcile, service
from src.dalton.knowledge.ops import apply_ops, next_item_id
from src.dalton.knowledge.paths import (
    DOC_KEYS,
    RENDERED,
    SECTOR_DOC_KEYS,
    data_path,
    history_path,
    rendered_path,
)
from src.dalton.knowledge.profiles import (
    PROFILE_COMPANY,
    PROFILE_SECTOR,
    doc_keys_for_profile,
    doc_not_allowed_message,
    is_doc_allowed,
    resolve_workspace_profile,
)
from src.dalton.knowledge.render import render_agent_block, render_document_md
from src.dalton.knowledge.validate import check_all_schemas, validate_update
from src.dalton.workflows.contracts import (
    load_workflow_contract,
    validate_contract,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d024_sector_knowledge_doc.py"
)

WS = "11111111-1111-1111-1111-111111111111"
USER = "user-1"
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)

_SECTOR_UNIT = {
    "unit_id": "22222222-2222-2222-2222-222222222222",
    "kind": "sector",
    "owner_user_id": USER,
    "slug": "ai-infrastructure",
    "workspace_id": WS,
    "ticker": None,
    "status": "active",
}


def _sector_doc() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "AI Infrastructure",
        "name_zh": "AI 基础设施",
        "kind": "sector",
        "definition": "面向大规模训练与推理的算力、网络与平台层供应链",
        "boundaries": "纳入 GPU/ASIC 与先进封装；排除终端消费电子",
        "scope": {"regions": ["全球"], "markets": ["US equities"]},
        "value_chain": [{"stage": "上游芯片与先进封装"}, {"stage": "云与 IDC 平台"}],
        "research_tz": "America/New_York",
        "coverage_stage": "onboarding",
        "members": [
            {"name": "某综合科技公司", "ticker": "EXAMPLE", "workspace_created": False},
            {"name": "未覆盖的封装厂商"},
        ],
        "one_liner": "算力供给的节奏与瓶颈决定行业盈利周期",
    }


def _sector_thesis_doc() -> dict[str, Any]:
    """行业版 thesis：结构与公司 thesis 相同，但没有目标价等公司专属字段。"""
    hv = {
        "stance": "no_view",
        "conviction": "none",
        "summary": "行业处于资本开支上行段，预期差尚未形成",
        "updated_at": "2026-10-18T12:00:00Z",
    }
    return {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "one_liner": "算力供给瓶颈决定行业盈利周期位置",
        "aggregate_status": "untested",
        "house_view": hv,
        "house_view_history": [
            {"at": "2026-10-18T12:00:00Z", "to": copy.deepcopy(hv), "reason": "Initial"}
        ],
        "pillars": [
            {
                "id": "P1",
                "claim": "行业资本开支在 FY2027 保持同比 20% 以上增长",
                "priority": "core",
                "baseline": {"text": "FY2026 资本开支同比 +35%", "as_of": "2026-10-15"},
                "expected_path": "云厂商指引继续上调",
                "thresholds": {
                    "confirm": [
                        {
                            "id": "T1",
                            "metric": "行业资本开支同比增速",
                            "operator": ">=",
                            "value": 20.0,
                            "unit": "pct",
                            "period": "FY2027",
                            "label": "research_judgement",
                        }
                    ],
                    "warn": [],
                    "break": [
                        {
                            "id": "T2",
                            "metric": "行业资本开支同比增速",
                            "operator": "<",
                            "value": 5.0,
                            "unit": "pct",
                            "period": "FY2027",
                            "label": "research_judgement",
                        }
                    ],
                },
                "status": "untested",
                "provenance": "draft",
                "created_at": "2026-10-18T12:00:00Z",
            }
        ],
        "evidence": [
            {
                "id": "E-20261018-01",
                "at": "2026-10-18T12:00:00Z",
                "period": "2026-09",
                "source": {
                    "type": "official_data",
                    "identity": "行业协会月度统计",
                    "tier": 4,
                    "ref": {"kind": "url", "url": "https://example.org/semi-monthly"},
                },
                "fact": "2026-09 行业出货量同比增长 24%",
                "figures": [
                    {
                        "metric": "行业出货量同比增速",
                        "value": 24.0,
                        "unit": "pct",
                        "period": "2026-09",
                        "label": "reported",
                    }
                ],
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "medium",
            }
        ],
        "official": None,
    }


# ---------------------------------------------------------------------------
# Schema / 路径 / profile 纯函数
# ---------------------------------------------------------------------------


def test_sector_schema_registered_and_valid() -> None:
    schemas = check_all_schemas()
    assert schemas["sector"] == "https://dalton.local/schemas/sector"


def test_sector_doc_valid_and_company_keys_untouched() -> None:
    # 经 apply_ops 补齐成员 id/added_at/workspace_created 后整份过 schema。
    doc, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    assert validate_update("sector", None, doc, reason="初始化行业画像").ok
    # 公司文档集合不变；行业集合以 sector 替代 company/management。
    assert DOC_KEYS == (
        "company",
        "thesis",
        "debate",
        "drivers",
        "management",
        "lessons",
        "agenda",
        "models",
    )
    assert SECTOR_DOC_KEYS == (
        "sector",
        "thesis",
        "debate",
        "drivers",
        "lessons",
        "agenda",
        "models",
    )
    assert set(SECTOR_DOC_KEYS) & {"company", "management"} == set()
    assert data_path("sector") == "data/sector.json"
    assert rendered_path("sector") == "sector.md"
    assert (
        history_path("sector", 2, NOW)
        == "history/sector/20261018T120000Z-v0002.json"
    )


def test_sector_doc_rejects_fake_root_fields_and_bad_member() -> None:
    doc, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    doc["sector_ticker"] = "AI01"
    res = validate_update("sector", None, doc, reason="多写了根级字段")
    assert not res.ok
    assert any(
        "sector_ticker" in str(err["message"]) and err["path"] == "/"
        for err in res.errors
    )

    doc2, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    doc2["members"][0]["ticker"] = "not a ticker!"
    res2 = validate_update("sector", None, doc2, reason="成员 ticker 形状非法")
    assert not res2.ok


def test_member_ids_auto_assigned_and_roster_editable() -> None:
    doc, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    assert [m["id"] for m in doc["members"]] == ["M1", "M2"]
    assert doc["members"][1]["workspace_created"] is False
    assert doc["members"][1]["added_at"] == "2026-10-18T12:00:00Z"

    # 名单是活文档：set / remove 均可（不可变约束在 thesis，不在这里）。
    doc2, _ = apply_ops(
        "sector",
        doc,
        [
            {"op": "set", "path": "/members/M1/workspace_created", "value": True},
            {"op": "append", "path": "/members", "value": {"name": "新成员公司"}},
            {"op": "remove", "path": "/members/M2"},
        ],
        reason="成员维护",
        now=NOW,
    )
    assert doc2["members"][0]["workspace_created"] is True
    assert doc2["members"][1]["id"] == "M3"
    assert [m["name"] for m in doc2["members"]] == ["某综合科技公司", "新成员公司"]
    assert next_item_id("members", doc2["members"]) == "M4"


def test_member_auto_id_never_collides_with_existing_ids() -> None:
    """codex r1 #4：`[{"name":"A"},{"id":"M1","name":"B"}]` 里 A 不得被补成第二个 M1。"""
    doc, _ = apply_ops(
        "sector",
        None,
        [
            {
                "op": "create",
                "value": {
                    "schema_version": 1,
                    "name": "AI Infrastructure",
                    "definition": "定义",
                    "coverage_stage": "onboarding",
                    "one_liner": "一句话",
                    "members": [{"name": "A"}, {"id": "M1", "name": "B"}],
                },
            }
        ],
        now=NOW,
    )
    ids = [m["id"] for m in doc["members"]]
    assert ids == ["M2", "M1"]
    assert len(set(ids)) == 2
    assert validate_update("sector", None, doc, reason="自动编号不得撞已有 id").ok


def test_duplicate_member_ids_rejected_by_invariant() -> None:
    doc, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    doc["members"][1]["id"] = "M1"  # 手工造出重复（直接编辑对账路径同此校验）
    res = validate_update("sector", None, doc, reason="重复成员 id")
    assert not res.ok
    assert any(err["code"] == "duplicate_member_id" for err in res.errors)


@pytest.mark.asyncio
async def test_profile_resolution_follows_registry_and_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import coverage_units as cu_db

    real_lookup = cu_db.get_active_unit_by_workspace

    # 开关关闭：公司 profile，注册表绝不被读。
    def _fail_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("flag off must never read dalton_coverage_units")

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _fail_read)
    assert await resolve_workspace_profile(WS) == PROFILE_COMPANY

    # 开关开启：sector 单元 → 行业 profile；company 单元/未登记 → 公司。
    monkeypatch.setattr(profiles_mod, "is_coverage_units_enabled", lambda: True)
    fake_lookup = create_autospec(real_lookup, return_value=dict(_SECTOR_UNIT))
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", fake_lookup)
    assert await resolve_workspace_profile(WS) == PROFILE_SECTOR
    fake_lookup.assert_awaited_once_with(WS, conn=None)

    fake_lookup2 = create_autospec(
        real_lookup,
        return_value={**_SECTOR_UNIT, "kind": "company", "ticker": "EXAMPLE"},
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", fake_lookup2)
    assert await resolve_workspace_profile(WS) == PROFILE_COMPANY

    fake_lookup3 = create_autospec(real_lookup, return_value=None)
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", fake_lookup3)
    assert await resolve_workspace_profile(WS) == PROFILE_COMPANY


def test_doc_gating_and_refusal_messages() -> None:
    assert doc_keys_for_profile(PROFILE_COMPANY) == DOC_KEYS
    assert doc_keys_for_profile(PROFILE_SECTOR) == SECTOR_DOC_KEYS
    assert is_doc_allowed("management", PROFILE_COMPANY)
    assert not is_doc_allowed("management", PROFILE_SECTOR)
    assert not is_doc_allowed("sector", PROFILE_COMPANY)
    assert is_doc_allowed("sector", PROFILE_SECTOR)

    msg = doc_not_allowed_message("management", PROFILE_SECTOR)
    assert "行业覆盖单元" in msg and "公司专属文档" in msg and "members" in msg
    msg2 = doc_not_allowed_message("sector", PROFILE_COMPANY)
    assert "不接受行业文档 sector" in msg2


# ---------------------------------------------------------------------------
# 服务管线（真实校验 / 渲染 / 路径；DB 层为替身）
# ---------------------------------------------------------------------------


class _MemBackend:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.writes: list[str] = []

    async def aread_text(self, path: str) -> str | None:
        return self.files.get(path)

    async def awrite_text(self, path: str, content: str) -> None:
        self.writes.append(path)
        self.files[path] = content


@pytest.fixture
def knowledge_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """DB 替身：版本行内存存储，其余函数 create_autospec。"""
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": WS, "user_id": USER},
        ),
    )
    store: dict[str, list[dict[str, Any]]] = {}

    async def fake_latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = store.get(doc_key, [])
        return rows[-1] if rows else None

    async def fake_latest_valid_version(
        workspace_id: str, doc_key: str
    ) -> dict[str, Any] | None:
        rows = [r for r in store.get(doc_key, []) if r.get("schema_valid", True)]
        return rows[-1] if rows else None

    async def fake_list_all_latest(
        workspace_id: str, valid_only: bool = False
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[k] = filtered[-1]
        return out

    mock_insert = create_autospec(db_knowledge.insert_version)

    async def _insert_impl(**kw: Any) -> dict[str, Any]:
        rows = store.setdefault(kw["doc_key"], [])
        row = {
            "knowledge_version_id": f"kv-{kw['doc_key']}-{len(rows) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": len(rows) + 1,
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "source": kw.get("source", "tool"),
            "reason": kw.get("reason"),
            "warnings": kw.get("warnings") or [],
            "schema_valid": kw.get("schema_valid", True),
            "edited_path": kw.get("edited_path"),
            "files_written_at": None,
            "created_at": NOW,
        }
        rows.append(row)
        return row

    mock_insert.side_effect = _insert_impl

    async def fake_mark_written(kv_id: str) -> None:
        for rows in store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = NOW

    mock_list_verdicts = create_autospec(db_verdicts.list_verdicts, return_value=[])

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest_version)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest_valid_version)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all_latest)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)
    monkeypatch.setattr(db_knowledge, "mark_files_written", fake_mark_written)
    monkeypatch.setattr(db_verdicts, "list_verdicts", mock_list_verdicts)
    return store


@pytest.mark.asyncio
async def test_sector_workspace_full_chain(
    knowledge_store: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行业工作区：init（sector/thesis）→ 维护成员 → topic-research 式回写 → 拒绝公司专属文档。"""
    backend = _MemBackend()

    # profile 解析走真实函数（注册表替身返回 sector 单元）。
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(profiles_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            cu_db.get_active_unit_by_workspace, return_value=dict(_SECTOR_UNIT)
        ),
    )

    # 1. 初始化 sector 文档。
    res1 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="sector",
        ops=[{"op": "create", "value": _sector_doc()}],
        reason="初始化行业覆盖画像",
        base_version=None,
        backend=backend,
    )
    assert res1["ok"], res1.get("message")
    assert res1["version_no"] == 1
    assert backend.files["data/sector.json"]
    assert "AI 基础设施" in backend.files["sector.md"]
    assert "成员公司清单" in backend.files["sector.md"]
    assert "某综合科技公司" in backend.files["sector.md"]
    assert "AI 基础设施（行业 · 入职迁移）" in backend.files["agent.md"]

    # 2. 行业 thesis：无目标价，结构与不变量同公司。
    res2 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="thesis",
        ops=[{"op": "create", "value": _sector_thesis_doc()}],
        reason="行业论点初始化（不含公司目标价）",
        base_version=None,
        backend=backend,
    )
    assert res2["ok"], res2.get("message")
    thesis_md = backend.files["thesis.md"]
    assert "AI Infrastructure" in thesis_md and "核心支柱" in thesis_md

    # 3. 议程（行业语义）与 drivers（行业指标）。
    res3 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="agenda",
        ops=[
            {
                "op": "create",
                "value": {
                    "schema_version": 1,
                    "as_of": "2026-10-18T12:00:00Z",
                    "open_questions": [
                        {
                            "id": "Q-001",
                            "question": "先进封装产能瓶颈何时缓解？",
                            "why": "决定 FY2027 供给弹性",
                            "value": "high",
                            "urgency": "this_quarter",
                            "origin": "analyst",
                            "owner": "analyst",
                            "status": "open",
                            "attempts": [],
                            "created_at": "2026-10-18T12:00:00Z",
                        }
                    ],
                    "next_validations": [
                        {
                            "id": "V-001",
                            "what": "行业协会 2026-11 月度出货",
                            "due": "2026-12-01",
                            "expected": "同比 >= 20%",
                            "status": "pending",
                        }
                    ],
                    "watch": {
                        "events": [
                            {
                                "id": "W1",
                                "type": "macro",
                                "date": None,
                                "date_status": "unknown",
                                "plan": "月度数据点评",
                            }
                        ]
                    },
                    "wake_conditions": [],
                },
            }
        ],
        reason="行业议程初始化",
        base_version=None,
        backend=backend,
    )
    assert res3["ok"], res3.get("message")

    res4 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="drivers",
        ops=[
            {
                "op": "create",
                "value": {
                    "schema_version": 1,
                    "as_of": "2026-10-18T12:00:00Z",
                    "drivers": [
                        {
                            "id": "K1",
                            "name": "行业资本开支",
                            "kind": "capex",
                            "description": "云厂商与运营商资本开支合计",
                            "leading_indicators": [
                                {
                                    "id": "K1.1",
                                    "name": "行业协会月度出货",
                                    "source": "行业协会",
                                    "frequency": "monthly",
                                    "good_direction": "up",
                                    "readings": [
                                        {
                                            "value": 24.0,
                                            "unit": "pct",
                                            "period": "2026-09",
                                            "as_of": "2026-10-18T12:00:00Z",
                                            "ref": {
                                                "kind": "url",
                                                "url": "https://example.org/s",
                                            },
                                            "label": "reported",
                                        }
                                    ],
                                }
                            ],
                            "updated_at": "2026-10-18T12:00:00Z",
                        }
                    ],
                },
            }
        ],
        reason="行业指标登记",
        base_version=None,
        backend=backend,
    )
    assert res4["ok"], res4.get("message")

    # 4. topic-research 式回写：成员建工作区后更新清单 + thesis 追加证据。
    res5 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="sector",
        ops=[{"op": "set", "path": "/members/M1/workspace_created", "value": True}],
        reason="Home 已为成员公司建立工作区",
        base_version=1,
        backend=backend,
    )
    assert res5["ok"], res5.get("message")

    res6 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="thesis",
        ops=[
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "period": "2026-10",
                    "source": {
                        "type": "official_data",
                        "identity": "行业协会周度库存",
                        "tier": 4,
                        "ref": {"kind": "url", "url": "https://example.org/w"},
                    },
                    "fact": "2026-10 渠道库存天数降至 4 天",
                    "figures": [
                        {
                            "metric": "渠道库存天数",
                            "value": 4.0,
                            "unit": "count",
                            "period": "2026-10",
                            "label": "reported",
                        }
                    ],
                    "pillar_ids": ["P1"],
                    "signal": "confirming",
                    "quality": "high",
                },
            }
        ],
        reason="专题研究取证：供给趋紧",
        base_version=1,
        backend=backend,
    )
    assert res6["ok"], res6.get("message")

    # 5. 公司专属文档被拒，说明行业语义。
    for doc in ("management", "company"):
        res_bad = await service.apply_update(
            user_id=USER,
            workspace_id=WS,
            doc_key=doc,
            ops=[{"op": "create", "value": {}}],
            reason="误用公司文档",
            base_version=None,
        )
        assert res_bad["ok"] is False
        assert res_bad["error"] == "doc_not_allowed_for_profile"
        assert "行业覆盖单元" in res_bad["message"]
        assert "公司专属文档" in res_bad["message"]
    assert "management" not in knowledge_store
    assert "company" not in knowledge_store

    # 6. 不变量在行业 profile 原样生效：thesis 证据只追加。
    latest = knowledge_store["thesis"][-1]["content"]
    res7 = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="thesis",
        ops=[{"op": "remove", "path": "/evidence/E-20261018-01"}],
        reason="试图删除旧证据",
        base_version=2,
    )
    assert res7["ok"] is False
    assert "append_only_violation" in str(res7.get("message"))
    assert knowledge_store["thesis"][-1]["content"] == latest


@pytest.mark.asyncio
async def test_company_workspace_unchanged_and_sector_refused(
    knowledge_store: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭（默认）：公司 profile 全链路不变，sector 被明确拒绝。"""
    # 开关默认关闭（conftest 不设环境变量）；再加一道防线：读注册表即失败。
    from src.dalton.database import coverage_units as cu_db

    def _fail_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("flag off must never read dalton_coverage_units")

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _fail_read)

    backend = _MemBackend()
    res_m = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="management",
        ops=[
            {
                "op": "create",
                "value": {
                    "schema_version": 1,
                    "as_of": "2026-10-18T12:00:00Z",
                    "guidance_style": {"bias": "mixed", "summary": "指引风格初步观察"},
                    "guidance_history": [
                        {
                            "id": "G-001",
                            "metric": "FY2026 revenue",
                            "period": "FY2026",
                            "issued_at": "2026-10-15",
                            "guide": {"point": 50.0, "unit": "USD_bn"},
                            "guide_ref": {"kind": "file", "path": "sources/guide.md"},
                            "actual": None,
                            "outcome": "pending",
                        }
                    ],
                },
            }
        ],
        reason="公司工作区照常写 management",
        base_version=None,
        backend=backend,
    )
    assert res_m["ok"], res_m.get("message")
    assert "management.md" in backend.files

    res_s = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="sector",
        ops=[{"op": "create", "value": _sector_doc()}],
        reason="公司在行业文档",
        base_version=None,
    )
    assert res_s["ok"] is False
    assert res_s["error"] == "doc_not_allowed_for_profile"
    assert "不接受行业文档 sector" in res_s["message"]
    assert "sector" not in knowledge_store

    res_unknown = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="nonsense",
        ops=[],
        reason="未知文档",
        base_version=None,
    )
    assert res_unknown["ok"] is False
    assert res_unknown["error"] == "invalid_doc_key"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_sector_md_and_agent_block() -> None:
    doc, _ = apply_ops(
        "sector", None, [{"op": "create", "value": _sector_doc()}], now=NOW
    )
    md = render_document_md("sector", doc, version_no=1, at=NOW)
    assert md and md.startswith("<!-- 由 data/sector.json 渲染")
    assert "AI Infrastructure" in md and "成员公司清单" in md
    assert "M1" in md and "某综合科技公司" in md

    thesis_doc = _sector_thesis_doc()
    block = render_agent_block(
        ticker="",
        sector_doc=_sector_doc(),
        thesis_doc=thesis_doc,
        thesis_version_no=1,
        agenda_doc={
            "next_validations": [
                {"id": "V-001", "due": "2026-12-01", "what": "月度出货", "status": "pending"}
            ]
        },
    )
    assert "AI 基础设施（行业 · 入职迁移）" in block
    assert "management.md" not in block
    assert "sector.md" in block

    # 公司块逐字段保持原样：无 sector_doc 时标题与文件清单与旧渲染一致。
    company_block = render_agent_block(
        ticker="META",
        company_doc={"ticker": "META", "name": "Meta", "coverage_tier": "core"},
        models_doc={},
    )
    assert "META（Meta · Core，覆盖中）" in company_block
    assert (
        "thesis.md · debate.md · drivers.md · management.md · lessons.md · agenda.md · "
        "verdicts.md · model/（暂无正式模型）· published/INDEX.md" in company_block
    )


def test_rendered_map_covers_sector_and_watched_globs() -> None:
    from src.dalton.knowledge.paths import WATCHED_GLOBS

    assert RENDERED["sector"] == "sector.md"
    assert "sector.md" in WATCHED_GLOBS


@pytest.mark.asyncio
async def test_company_rendering_unchanged_with_residual_sector_row(
    knowledge_store: dict[str, list[dict[str, Any]]],
) -> None:
    """codex r1 #3：开关关闭（公司 profile）时，库里残留的 sector 行不得改变公司渲染。"""
    # 残留 sector 行：行业单元曾绑定此工作区后归档/开关关闭。
    knowledge_store["sector"] = [
        {
            "knowledge_version_id": "kv-sector-residual",
            "workspace_id": WS,
            "doc_key": "sector",
            "version_no": 1,
            "content": _sector_doc(),
            "content_sha256": "deadbeef",
            "source": "tool",
            "warnings": [],
            "schema_valid": True,
            "files_written_at": NOW,
            "created_at": NOW,
        }
    ]
    backend = _MemBackend()
    res = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="company",
        ops=[
            {
                "op": "create",
                "value": {
                    "schema_version": 1,
                    "ticker": "EXAMPLE",
                    "exchange": "NASDAQ",
                    "name": "Example Corp",
                    "coverage_tier": "core",
                    "coverage_stage": "initial",
                    "one_liner": "公司工作区一句话",
                },
            }
        ],
        reason="公司覆盖照常初始化",
        base_version=None,
        backend=backend,
    )
    assert res["ok"], res.get("message")
    agent_md = backend.files["agent.md"]
    # 公司标题与文件清单（含 management.md），没有行业分支。
    assert "EXAMPLE（Example Corp · Core，初步覆盖）" in agent_md
    assert "management.md" in agent_md
    assert "行业 ·" not in agent_md
    assert "sector.md ·" not in agent_md
    # 渲染标题用公司 ticker，不用残留 sector 名。
    assert backend.files["data/company.json"]


# ---------------------------------------------------------------------------
# 完成契约
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_done_sector_docs_follow_identity_not_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #1/#5：行业分支知识文档由工作区身份决定，不读 params.subject_kind。

    检查时省略 subject_kind 也要按身份加查；冲突取值明确报错；身份查询失败
    fail-closed；公司身份不加查（原有 drivers/debate 二选一保持）。
    """
    import src.dalton.tools.check_done_tools as cdt

    contract = {
        "name": "t",
        "closing": {
            "knowledge_docs_any": ["drivers", "debate"],
            "knowledge_docs_if_sector": ["thesis", "agenda", "drivers"],
        },
    }
    found: dict[str, bool] = {}

    async def fake_found(workspace_id: str, docs: list[str], since: datetime) -> dict[str, bool]:
        return {doc: (doc in found) for doc in docs}

    monkeypatch.setattr(cdt, "_check_knowledge_versions", fake_found)

    real_resolve = cdt.resolve_workspace_profile

    async def run(params: dict[str, Any], profile: str | Exception) -> list[str]:
        if isinstance(profile, Exception):
            fake_resolve = create_autospec(
                real_resolve, side_effect=profile
            )
        else:
            fake_resolve = create_autospec(real_resolve, return_value=profile)
        monkeypatch.setattr(cdt, "resolve_workspace_profile", fake_resolve)
        missing, _warnings, _label = await cdt._check_closing(
            contract,
            params,
            backend=None,
            workspace_id=WS,
            user_id=USER,
            since=NOW,
            since_gaps=[],
        )
        return missing

    # 1) 行业身份 + 检查时省略 subject_kind：thesis/agenda/drivers 逐个点名。
    missing = await run({}, PROFILE_SECTOR)
    for doc in ("thesis", "agenda", "drivers"):
        assert any(doc in m and "source=tool" in m for m in missing), (doc, missing)
    found.update({"drivers": True, "thesis": True, "agenda": True})
    assert await run({}, PROFILE_SECTOR) == []
    # 与身份一致的声明不改变结果。
    assert await run({"subject_kind": "sector"}, PROFILE_SECTOR) == []

    # 2) 冲突声明：明确报错拒绝（行业身份传 company；公司身份传 sector）。
    missing_conflict = await run({"subject_kind": "company"}, PROFILE_SECTOR)
    found.update({"thesis": True, "agenda": True, "drivers": True})
    assert any("冲突" in m and "company" in m for m in missing_conflict)
    missing_conflict2 = await run({"subject_kind": "sector"}, PROFILE_COMPANY)
    assert any("冲突" in m and "sector" in m for m in missing_conflict2)

    # 3) 公司身份 + 省略：不加查行业文档（docs_any 满足即过）。
    found.clear()
    found.update({"debate": True})
    assert await run({}, PROFILE_COMPANY) == []
    # 全缺时仍报 docs_any。
    found.clear()
    assert any("drivers、debate" in m for m in await run({}, PROFILE_COMPANY))

    # 4) 身份查询失败：无法核对，fail-closed（不回退公司）。
    missing_fail = await run({}, RuntimeError("registry timeout"))
    assert any("身份" in m and "请重试" in m for m in missing_fail)


def test_topic_research_contract_declares_sector_rule() -> None:
    contract = load_workflow_contract("topic-research")
    assert validate_contract(contract) == []
    # codex r1 #5：行业分支必须论点（thesis）、议程（agenda）、指标（drivers）齐写。
    assert contract["closing"]["knowledge_docs_if_sector"] == [
        "thesis",
        "agenda",
        "drivers",
    ]
    # 公司分支保持 drivers/debate 二选一。
    assert contract["closing"]["knowledge_docs_any"] == ["drivers", "debate"]


# ---------------------------------------------------------------------------
# 迁移 d024（执行迁移函数、捕获 SQL 断言行为与顺序；真实 Postgres 升降级由
# lead 在沙箱外验证——沙箱无数据库，无法连库执行）
# ---------------------------------------------------------------------------


def _load_d024() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "d024_sector_knowledge_doc", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_d024_upgrade_swaps_doc_key_constraint_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_d024()
    from alembic import op

    stmts: list[str] = []
    monkeypatch.setattr(op, "execute", lambda sql: stmts.append(str(sql)))

    mod.upgrade()
    assert len(stmts) == 3
    assert "lock_timeout" in stmts[0]
    assert "DROP CONSTRAINT IF EXISTS dalton_knowledge_versions_doc_key_check" in stmts[1]
    assert "ADD CONSTRAINT dalton_knowledge_versions_doc_key_check" in stmts[2]
    assert "'company','sector','thesis','debate','drivers','management','lessons','agenda','models'" in stmts[2]
    # 只扩约束：不改行、不建表、不动触发器。
    all_sql = "\n".join(stmts)
    assert "DELETE FROM" not in all_sql
    assert "CREATE TABLE" not in all_sql
    assert "TRIGGER" not in all_sql
    assert 'revision: str = "d024"' in MIGRATION_PATH.read_text(encoding="utf-8")


def test_d024_downgrade_guard_precedes_restore_and_uses_valid_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_d024()
    from alembic import op

    stmts: list[str] = []
    monkeypatch.setattr(op, "execute", lambda sql: stmts.append(str(sql)))

    mod.downgrade()
    assert len(stmts) == 3
    guard = stmts[0]
    # 拒绝守卫先于约束恢复执行；存在 sector 行时抛异常中断，后两条不会跑。
    assert "doc_key = 'sector'" in guard
    # lead 修正后的合法语法（RAISE ... USING MESSAGE），锁住不被回退成
    # PL/pgSQL 不支持的字符串拼接形式。
    assert "RAISE EXCEPTION USING MESSAGE" in guard
    assert "refused" in guard and "append-only" in guard
    # 拒绝执行而不是删数据；恢复值是 d003 原有的八个 doc key。
    assert "DELETE FROM" not in guard
    assert "DROP CONSTRAINT IF EXISTS dalton_knowledge_versions_doc_key_check" in stmts[1]
    assert (
        "CHECK (doc_key IN ('company','thesis','debate','drivers','management','lessons','agenda','models'))"
        in stmts[2]
    )
    # 迁移全文不含百分号字面量（psycopg 参数化缺陷的教训）。
    assert "%" not in MIGRATION_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 对账（codex r1 #2）：身份查询失败不回退公司；行业 profile 下的不适用文档
# ---------------------------------------------------------------------------


class _MirrorFixture:
    """detect_direct_edits 的 DB/镜像替身：内存版本表 + 可控文件镜像。"""

    def __init__(self) -> None:
        self.store: dict[str, list[dict[str, Any]]] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.file_texts: dict[str, str] = {}
        # 原始 insert_version（被 fixture 替换前留存），供测试建 autospec。
        self.real_insert_version: Any = None


@pytest.fixture
def reconcile_env(monkeypatch: pytest.MonkeyPatch) -> _MirrorFixture:
    from src.dalton.database import publications as db_publications
    from src.dalton.knowledge import reconcile

    fx = _MirrorFixture()
    # 先留存未被替身覆盖的原始函数，供测试在其上建 create_autospec。
    fx.real_insert_version = db_knowledge.insert_version

    async def fake_latest(ws: str, doc_key: str) -> dict[str, Any] | None:
        rows = fx.store.get(doc_key, [])
        return rows[-1] if rows else None

    async def fake_latest_valid(ws: str, doc_key: str) -> dict[str, Any] | None:
        rows = [r for r in fx.store.get(doc_key, []) if r.get("schema_valid", True)]
        return rows[-1] if rows else None

    async def fake_list_all(ws: str, valid_only: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, rows in fx.store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[k] = filtered[-1]
        return out

    async def fake_insert(**kw: Any) -> dict[str, Any]:
        rows = fx.store.setdefault(kw["doc_key"], [])
        row = {
            "knowledge_version_id": f"kv-{kw['doc_key']}-{len(rows) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": len(rows) + 1,
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "source": kw.get("source", "direct_edit"),
            "reason": kw.get("reason"),
            "warnings": kw.get("warnings") or [],
            "schema_valid": kw.get("schema_valid", True),
            "edited_path": kw.get("edited_path"),
            "files_written_at": None,
            "created_at": NOW,
        }
        rows.append(row)
        return row

    mock_insert = create_autospec(db_knowledge.insert_version, side_effect=fake_insert)

    async def fake_mark(kv_id: str) -> None:
        for rows in fx.store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = NOW

    async def fake_get_version(ws: str, doc_key: str, version_no: int) -> dict[str, Any] | None:
        rows = fx.store.get(doc_key, [])
        for r in rows:
            if r["version_no"] == version_no:
                return r
        return None

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest_valid)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all)
    monkeypatch.setattr(db_knowledge, "get_version", fake_get_version)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)
    monkeypatch.setattr(db_knowledge, "mark_files_written", fake_mark)
    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(
            db_publications.list_publication_files, return_value=[]
        ),
    )
    monkeypatch.setattr(
        db_verdicts,
        "list_verdicts",
        create_autospec(db_verdicts.list_verdicts, return_value=[]),
    )
    monkeypatch.setattr(
        reconcile,
        "get_file_metadata_for_sync",
        create_autospec(
            reconcile.get_file_metadata_for_sync, return_value=fx.meta
        ),
    )
    monkeypatch.setattr(
        reconcile,
        "db_get_file",
        create_autospec(
            reconcile.db_get_file,
            side_effect=lambda ws, path, include_content=False: {
                "path": path
            },
        ),
    )

    async def fake_resolve_text(file_row: Any, *, user_id: str, context: str) -> str | None:
        return fx.file_texts.get(file_row["path"]) if file_row else None

    monkeypatch.setattr(
        reconcile, "resolve_file_text_or_none", fake_resolve_text
    )
    return fx


def _mirror_file(fx: _MirrorFixture, path: str, text: str) -> None:
    import hashlib as _hashlib

    fx.file_texts[path] = text
    fx.meta[path] = {
        "kind": "file",
        "content_hash": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_detect_direct_edits_scans_shared_docs_when_identity_fails(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #1：身份查询失败时共享文档（两 profile 校验一致）照常检测；
    只有身份专属文档（company/management/sector）跳过——绝不按公司回退。"""
    from src.dalton.knowledge import reconcile

    # v1 thesis 已写；镜像上有一处未记录编辑 A。
    v1 = {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "pillars": [],
        "evidence": [],
    }
    reconcile_env.store["thesis"] = [
        {
            "knowledge_version_id": "kv-thesis-1",
            "workspace_id": WS,
            "doc_key": "thesis",
            "version_no": 1,
            "content": v1,
            "content_sha256": "hash-1",
            "source": "tool",
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "files_written_at": NOW,
            "created_at": NOW,
        }
    ]
    edited = dict(v1)
    edited["as_of"] = "2026-10-19T12:00:00Z"
    _mirror_file(reconcile_env, "data/thesis.json", json.dumps(edited))
    # 身份专属文档也在镜像上出现：本轮不扫描（也不误按公司记为有效）。
    _mirror_file(
        reconcile_env,
        "data/management.json",
        json.dumps({"schema_version": 1, "ticker": "EXAMPLE"}),
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(
            reconcile.resolve_workspace_profile,
            side_effect=RuntimeError("registry timeout"),
        ),
    )

    result = await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    recorded = result["recorded_versions"]
    assert [v["doc_key"] for v in recorded] == ["thesis"]
    assert recorded[0]["source"] == "direct_edit"
    assert "management" not in reconcile_env.store
    assert result["skipped"] == "coverage-identity-unavailable"
    assert set(result["skipped_docs"]) == {"company", "management", "sector"}


@pytest.mark.asyncio
async def test_sector_workspace_flags_company_only_doc_as_violation(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行业 profile 下出现 data/management.json：记不适用文档违规（无效版本），不出现在扫描集。"""
    from src.dalton.knowledge import reconcile

    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(
            reconcile.resolve_workspace_profile, return_value=PROFILE_SECTOR
        ),
    )
    _mirror_file(
        reconcile_env,
        "data/management.json",
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-10-18T12:00:00Z",
                "guidance_style": {"bias": "mixed", "summary": "x"},
                "guidance_history": [],
            },
            ensure_ascii=False,
        ),
    )

    result = await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    rows = [v for v in result["recorded_versions"] if v["doc_key"] == "management"]
    assert len(rows) == 1
    assert rows[0]["schema_valid"] is False
    assert rows[0]["edited_path"] == "data/management.json"
    assert rows[0]["warnings"][0]["code"] == "doc_not_allowed_for_profile"
    assert "公司专属文档" in rows[0]["warnings"][0]["message"]

    # 告警经现有机制浮出（agent.md 的 ⚠ 提示）。
    alerts = await reconcile.collect_workspace_alerts(WS)
    assert any(
        "data/management.json" in a and "公司专属文档" in a for a in alerts
    ), alerts

    # 同一内容第二次对账不再叠行（按 content_sha256 去重）。
    result2 = await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    assert [
        v for v in result2["recorded_versions"] if v["doc_key"] == "management"
    ] == []


@pytest.mark.asyncio
async def test_apply_pending_identity_failure_writes_shared_only(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #1：身份未知时共享文档的待写版本照常落盘；身份专属文档的
    待写版本留待下一轮（版本行已在库，不丢）。"""
    from src.dalton.knowledge import reconcile

    def _row(
        doc_key: str, v_no: int, written: bool, content: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "knowledge_version_id": f"kv-{doc_key}-{v_no}",
            "workspace_id": WS,
            "doc_key": doc_key,
            "version_no": v_no,
            "content": content,
            "content_sha256": f"hash-{doc_key}-{v_no}",
            "source": "tool",
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "files_written_at": NOW if written else None,
            "created_at": NOW,
        }

    agenda_v1 = {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "open_questions": [],
        "next_validations": [],
        "watch": {},
        "wake_conditions": [],
    }
    agenda_v2 = copy.deepcopy(agenda_v1)
    agenda_v2["last_session"] = {"at": "2026-10-19T12:00:00Z", "summary": "B"}

    reconcile_env.store["agenda"] = [
        _row("agenda", 1, True, agenda_v1),
        _row("agenda", 2, False, agenda_v2),
    ]
    reconcile_env.store["sector"] = [
        _row(
            "sector",
            1,
            False,
            {
                "schema_version": 1,
                "name": "AI Infrastructure",
                "definition": "定义",
                "coverage_stage": "onboarding",
                "one_liner": "一句话",
            },
        )
    ]
    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(
            reconcile.resolve_workspace_profile,
            side_effect=RuntimeError("registry timeout"),
        ),
    )
    backend = _MemBackend()
    result = await reconcile.apply_pending(WS, backend, skip_access_check=True)
    # 共享文档 agenda 的待写版本已落盘；身份专属的 sector 仍待写。
    assert reconcile_env.store["agenda"][-1]["files_written_at"] is not None
    assert reconcile_env.store["sector"][-1]["files_written_at"] is None
    assert result["skipped"] == "coverage-identity-unavailable"
    assert set(result["skipped_docs"]) == {"company", "management", "sector"}


# ---------------------------------------------------------------------------
# 覆盖前先检测（codex r2 #1）：身份故障期间漏扫的直接编辑不得被覆盖丢失
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_update_refuses_profile_docs_while_identity_unknown(
    knowledge_store: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份未知期间：专属文档拒绝（fail-closed），共享文档照常。"""
    import src.dalton.knowledge.service as service_mod

    monkeypatch.setattr(
        service_mod,
        "resolve_workspace_profile",
        create_autospec(
            service_mod.resolve_workspace_profile,
            side_effect=RuntimeError("registry timeout"),
        ),
    )
    res_refused = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="management",
        ops=[{"op": "create", "value": {}}],
        reason="身份未知",
        base_version=None,
    )
    assert res_refused["ok"] is False
    assert res_refused["error"] == "coverage_identity_unavailable"
    assert "身份专属文档" in res_refused["message"]

    backend = _MemBackend()
    res_shared = await service.apply_update(
        user_id=USER,
        workspace_id=WS,
        doc_key="lessons",
        ops=[{"op": "create", "value": {"schema_version": 1, "lessons": []}}],
        reason="共享文档不受身份故障影响",
        base_version=None,
        backend=backend,
    )
    assert res_shared["ok"], res_shared.get("message")


# ---------------------------------------------------------------------------
# 违规去重考虑违规状态（codex r2 #3）与告警解除（codex r2 #4）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_violation_recorded_over_stale_valid_company_doc(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭时期留下的有效 management 版本：打开开关、身份为 sector 后，
    即使内容未变也要记一条违规（否则文件在、告警无）。"""
    from src.dalton.knowledge import reconcile

    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(reconcile.resolve_workspace_profile, return_value=PROFILE_SECTOR),
    )
    mgmt = {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "guidance_style": {"bias": "mixed", "summary": "x"},
        "guidance_history": [],
    }
    from src.dalton.knowledge.service import canonical_json

    text = canonical_json(mgmt)[0]
    import hashlib as _h

    reconcile_env.store["management"] = [
        {
            "knowledge_version_id": "kv-management-1",
            "workspace_id": WS,
            "doc_key": "management",
            "version_no": 1,
            "content": copy.deepcopy(mgmt),
            "content_sha256": _h.sha256(text.encode()).hexdigest(),
            "source": "tool",
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "files_written_at": NOW,
            "created_at": NOW,
        }
    ]
    _mirror_file(reconcile_env, "data/management.json", text)

    result = await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    violations = [
        v for v in result["recorded_versions"] if v["doc_key"] == "management"
    ]
    assert len(violations) == 1
    assert violations[0]["schema_valid"] is False
    assert violations[0]["warnings"][0]["code"] == "doc_not_allowed_for_profile"
    # 原有效版本仍在历史。
    assert reconcile_env.store["management"][0]["schema_valid"] is True

    # 同内容违规记录 → 去重，不逐轮叠行。
    result2 = await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    assert [
        v for v in result2["recorded_versions"] if v["doc_key"] == "management"
    ] == []


@pytest.mark.asyncio
async def test_violation_alert_clears_when_file_deleted(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除不适用文档后活动告警解除（违规版本保留为历史），提示给出可执行
    的修复动作（删除文件 / 移入 sector·thesis），不再指向必被拒的
    knowledge_update。"""
    from src.dalton.knowledge import reconcile

    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(reconcile.resolve_workspace_profile, return_value=PROFILE_SECTOR),
    )
    _mirror_file(
        reconcile_env,
        "data/management.json",
        json.dumps({"schema_version": 1, "ticker": "EXAMPLE"}),
    )
    await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    alerts = await reconcile.collect_workspace_alerts(WS)
    active = [a for a in alerts if "data/management.json" in a]
    assert len(active) == 1
    assert "删除 data/management.json" in active[0]
    assert "sector / thesis" in active[0]
    assert "knowledge_update 修正" not in active[0]

    # 删除文件后：告警解除，违规版本仍保留为历史。
    del reconcile_env.meta["data/management.json"]
    del reconcile_env.file_texts["data/management.json"]
    alerts_after = await reconcile.collect_workspace_alerts(WS)
    assert not any("data/management.json" in a for a in alerts_after)
    assert any(
        r["warnings"] and r["warnings"][0]["code"] == "doc_not_allowed_for_profile"
        for r in reconcile_env.store["management"]
    )


@pytest.mark.asyncio
async def test_agent_md_alert_cleared_after_violation_file_deleted(
    reconcile_env: _MirrorFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r3 #5 完整时序：对账让告警写进 agent.md → 删除 management.json →
    再对账 → agent.md 不再显示该告警；之后无变化的一轮不再写 agent.md。"""
    from src.dalton.knowledge import reconcile

    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(reconcile.resolve_workspace_profile, return_value=PROFILE_SECTOR),
    )
    _mirror_file(
        reconcile_env,
        "data/management.json",
        json.dumps({"schema_version": 1, "ticker": "EXAMPLE"}),
    )
    backend = _MemBackend()

    # 第一轮：违规入账 + 告警写进 agent.md。
    await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    await reconcile.apply_pending(WS, backend, skip_access_check=True)
    agent_md = backend.files.get("agent.md", "")
    assert "data/management.json" in agent_md and "**提示**" in agent_md
    agent_writes_after_round1 = backend.writes.count("agent.md")
    assert agent_writes_after_round1 >= 1

    # 删除违规文件（镜像同步删除），再对账：无新版本、告警集合为空，但托管块
    # 里还挂着旧告警 → 必须重写清除。
    del reconcile_env.meta["data/management.json"]
    del reconcile_env.file_texts["data/management.json"]
    await reconcile.detect_direct_edits(WS, USER, skip_access_check=True)
    await reconcile.apply_pending(WS, backend, skip_access_check=True)
    agent_md2 = backend.files["agent.md"]
    assert "data/management.json" not in agent_md2
    assert "**提示**" not in agent_md2
    assert backend.writes.count("agent.md") > agent_writes_after_round1

    # 第三轮：没有任何变化（无待写版本、告警集合与块内一致）→ 不再写 agent.md。
    writes_before = list(backend.writes)
    await reconcile.apply_pending(WS, backend, skip_access_check=True)
    assert backend.writes == writes_before
    assert backend.files["agent.md"] == agent_md2


@pytest.mark.asyncio
async def test_company_alert_collection_makes_no_file_metadata_lookup(
    knowledge_store: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r4 #1：公司 profile 收集告警时不查 workspace_files 元数据——
    只有行业违规告警需要核实文件是否仍在。"""
    lookup = create_autospec(
        reconcile.get_file_metadata_for_sync,
        side_effect=AssertionError("company profile must not look up file metadata"),
    )
    monkeypatch.setattr(reconcile, "get_file_metadata_for_sync", lookup)
    alerts = await reconcile.collect_workspace_alerts(WS, profile="company")
    assert alerts == []
    lookup.assert_not_called()
