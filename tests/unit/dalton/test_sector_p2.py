"""Sector layer P2: membership projection, cross-knowledge references, linkage
propagation, director coordination.

锁定的行为：
- d025：``dalton_sector_members`` 只是 ``sector.members`` 的查询投影，同事务重写，
  可从文档完全重建（verify 只读校验），downgrade 只删新增对象；
- 跨知识引用形状（``knowledgeRef``）进入 thesis 证据/支柱与 agenda 的
  ``source_ref``；公司侧既有 ``evidenceRef`` 校验逐字不变，旧数据照常通过；
- 行业支柱改版 → 采用它的公司 agenda 生成"待复核"；公司 contradicts 证据 →
  行业 agenda 生成"更新候选"；按 源工作区+文档+版本+条目+关系 去重；无权限
  （非同一所有者且无共享授权）只跳过不硬写；开关关闭时传播零调用；
- 看板 ``units``（仅开关开启）：三态 readable/delegatable/auto_tracked、行业
  成员与 ``workspace_created``；``companies`` 与全部旧查询路径不变；
- ``dalton_cross_unit_request``：创建/更新/列出，dispatched 必须带线程 id，
  fulfilled 必须带 result，失败/停止/无权限保留为 gap。

真实 schema 校验、ops 应用、传播与看板代码全程在测；DB 层用
``create_autospec`` / SQL-求值替身（不连库），真实 Postgres 升降级由 lead
在沙箱外验证。
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

import src.dalton.database.knowledge as _kv_real
import src.dalton.knowledge.cross_unit as cross_unit
import src.dalton.tools.director_tools as dt_mod
from src.dalton.database import coverage_units as cu_db
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import sector_members as sm_db
from src.dalton.knowledge import service as knowledge_service
from src.dalton.knowledge.validate import validate_schema

# 测试内会被 monkeypatch 的原函数，先于任何替身留存，供 create_autospec 使用
_REAL_KV: dict[str, Any] = {
    "_resolve_workspace_ticker": _kv_real._resolve_workspace_ticker,
    "get_db_connection": _kv_real.get_db_connection,
}
from src.dalton.database import cross_unit_requests as _cur_real  # noqa: E402

_REAL_CUR: dict[str, Any] = {
    "insert_request": _cur_real.insert_request,
    "list_requests": _cur_real.list_requests,
    "list_linkage_retry_batch": _cur_real.list_linkage_retry_batch,
    "update_request": _cur_real.update_request,
    "get_request": _cur_real.get_request,
    "get_db_connection": _cur_real.get_db_connection,
}
_REAL_DT: dict[str, Any] = {
    "verify_workspace_access": dt_mod.verify_workspace_access,
}
_REAL_CU: dict[str, Any] = {
    "get_coverage_unit": cu_db.get_coverage_unit,
}

MIGRATION_D025 = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d025_sector_members.py"
)
MIGRATION_D026 = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d026_cross_unit_requests.py"
)

WS_SECTOR = "11111111-1111-1111-1111-111111111111"
WS_COMPANY = "33333333-3333-3333-3333-333333333333"
UNIT_SECTOR = "22222222-2222-2222-2222-222222222222"
UNIT_COMPANY = "44444444-4444-4444-4444-444444444444"
USER = "user-1"

# ---------------------------------------------------------------------------
# 迁移锁（SQL 级；真实 Postgres 升降级由 lead 在沙箱外验证）
# ---------------------------------------------------------------------------


def test_d025_migration_locks_projection_shape() -> None:
    sql = MIGRATION_D025.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS dalton_sector_members" in sql
    # 投影的主键是 (行业单元, 成员 id)；成员身份、可选公司单元、关联范围、
    # 理由、有效期与投影来源版本都在列上
    for col in (
        "sector_unit_id",
        "member_id",
        "member_name",
        "ticker",
        "company_unit_id",
        "workspace_created",
        "scope",
        "reason",
        "valid_until",
        "synced_version_no",
    ):
        assert col in sql
    assert "PRIMARY KEY (sector_unit_id, member_id)" in sql
    # 只新增表和索引：不动文档版本表、不搬数据、不改存量
    assert "INSERT INTO" not in sql
    assert "UPDATE " not in sql
    assert "DELETE FROM" not in sql
    assert "ALTER TABLE" not in sql
    assert "dalton_knowledge_versions" not in sql
    # 迁移全文不含百分号字面量（psycopg 参数化缺陷的教训）
    assert "%" not in sql


def test_d025_migration_downgrade_drops_only_the_new_objects() -> None:
    downgrade = MIGRATION_D025.read_text(encoding="utf-8").split("def downgrade")[1]
    assert "DROP TABLE IF EXISTS dalton_sector_members" in downgrade
    assert downgrade.count("DROP") == 3  # 2 个索引 + 1 张表
    assert "workspaces" not in downgrade
    assert "dalton_knowledge_versions" not in downgrade
    assert "dalton_coverage_units" not in downgrade


def test_d026_migration_locks_request_ledger() -> None:
    sql = MIGRATION_D026.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS dalton_cross_unit_requests" in sql
    for col in (
        "owner_user_id",
        "kind",
        "sector_unit_id",
        "target_workspace_id",
        "question",
        "evidence_needed",
        "completion_condition",
        "status",
        "dispatch_thread_id",
        "result",
        "payload",
    ):
        assert col in sql
    # 五个状态；gap 是一等状态——失败/停止/无权限的依赖明确保留
    assert "CHECK (status IN ('open','dispatched','fulfilled','gap','cancelled'))" in sql
    # 两类行：总监的取证请求 / 联动失败自动记下的可重试缺口（codex r1 #4）
    assert "CHECK (kind IN ('research_request', 'linkage_retry'))" in sql
    assert "%" not in sql
    downgrade = sql.split("def downgrade")[1]
    assert downgrade.count("DROP") == 3  # 2 个索引 + 1 张表
    assert "INSERT INTO" not in sql


# ---------------------------------------------------------------------------
# 投影：纯函数 + 同事务同步 + 只读校验
# ---------------------------------------------------------------------------


def test_expected_member_rows_projects_scope_reason_validity() -> None:
    content = {
        "members": [
            {
                "id": "M1",
                "name": "某综合科技公司",
                "ticker": "example",
                "unit_id": UNIT_COMPANY,
                "workspace_created": True,
                "scope": "只计 AI 加速卡收入",
                "reason": "算力资本开支核心受益方",
                "valid_until": "2027-06-30",
            },
            {"id": "M2", "name": "未覆盖的封装厂商", "workspace_created": False},
            {"id": "", "name": "坏行"},  # 缺 id：跳过
            "not-a-dict",  # 非对象：跳过
        ]
    }
    rows = sm_db.expected_member_rows(content)
    assert len(rows) == 2
    assert rows[0] == {
        "member_id": "M1",
        "member_name": "某综合科技公司",
        "ticker": "EXAMPLE",
        "company_unit_id": UNIT_COMPANY,
        "workspace_created": True,
        "scope": "只计 AI 加速卡收入",
        "reason": "算力资本开支核心受益方",
        "valid_until": date(2027, 6, 30),
        "position": 0,
    }
    assert rows[1]["member_id"] == "M2"
    assert rows[1]["workspace_created"] is False
    assert rows[1]["valid_until"] is None
    assert sm_db.expected_member_rows(None) == []
    assert sm_db.expected_member_rows({"members": []}) == []


class _SyncCursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, *args: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


def _spec_cursor() -> Any:
    cur = create_autospec(_SyncCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    return cur


def _assert_no_stray_percent(sql: str, params: Any) -> None:
    """参数化 SQL 里不得有 %s/%(name)s 之外的字面量百分号（psycopg 会当格式符解析）。"""
    import re

    if params is None:
        return
    stray = re.sub(r"%(?:[sbt%]|\([A-Za-z_][A-Za-z0-9_]*\)[sbt])", "", sql)
    assert "%" not in stray, f"literal % in a parameterized query: {sql}"


class _CtxOf:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_sync_members_projection_rewrites_within_caller_transaction() -> None:
    executed: list[tuple[str, tuple[Any, ...]]] = []
    cur = _spec_cursor()

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec
    cur.fetchone.return_value = {"unit_id": UNIT_SECTOR, "owner_user_id": USER, "slug": "ai"}

    summary = await sm_db.sync_members_projection(
        cur,
        WS_SECTOR,
        {"members": [{"id": "M1", "name": "A", "workspace_created": False}]},
        version_no=4,
    )
    assert summary == {"sector_unit_id": UNIT_SECTOR, "members": 1, "synced_version_no": 4}
    sqls = [s for s, _ in executed]
    # 整体替换：先 DELETE 该单元旧行，再插入新行（同一调用者事务）
    assert sqls[0].startswith("SELECT unit_id::text AS unit_id")
    assert "FROM dalton_coverage_units" in sqls[0]
    assert sqls[1].startswith("DELETE FROM dalton_sector_members")
    assert "INSERT INTO dalton_sector_members" in sqls[2]
    assert executed[2][1][0] == UNIT_SECTOR
    assert executed[2][1][-1] == 4  # synced_version_no


@pytest.mark.asyncio
async def test_sync_members_projection_skips_non_sector_workspace() -> None:
    cur = create_autospec(_SyncCursorProto, instance=True)
    cur.fetchone.return_value = None
    executed: list[str] = []

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append(str(sql))

    cur.execute.side_effect = _exec
    assert (
        await sm_db.sync_members_projection(cur, WS_COMPANY, {"members": []}, version_no=2)
        is None
    )
    # 只查了单元、没动投影表
    assert len(executed) == 1
    assert "DELETE" not in executed[0]


@pytest.mark.asyncio
async def test_insert_version_projects_sector_doc_in_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """doc_key=sector 且 schema_valid 且开关开：INSERT 之后、同一事务内重写投影。"""
    import src.dalton.database.knowledge as kv_mod

    monkeypatch.setattr(kv_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        kv_mod,
        "_resolve_workspace_ticker",
        create_autospec(kv_mod._resolve_workspace_ticker, return_value=None),
    )
    sync_calls: list[tuple[Any, str, dict[str, Any], int]] = []

    async def _fake_sync(cur: Any, ws: str, content: Any, version_no: int) -> dict[str, Any]:
        sync_calls.append((cur, ws, content, version_no))
        # 在调用者事务里留一个标记语句，用于断言同步发生在 INSERT 之后
        await cur.execute("SELECT 1 /* projection sync marker */")
        return {"sector_unit_id": UNIT_SECTOR, "members": 0, "synced_version_no": version_no}

    monkeypatch.setattr(sm_db, "sync_members_projection", _fake_sync)

    _exec_log: list[str] = []
    cur = _spec_cursor()

    async def _exec(sql: str, params: Any = None) -> None:
        _exec_log.append(" ".join(str(sql).split()))

    cur.execute.side_effect = _exec
    # fetchone 序列：latest 版本行（v2，与 base 对上）→ INSERT RETURNING（v3）
    cur.fetchone.side_effect = [
        {"knowledge_version_id": "kv-t2", "version_no": 2},
        {"knowledge_version_id": "kv-t3", "version_no": 3},
    ]

    class _Tx:
        async def __aenter__(self) -> "_Tx":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Conn:
        def __init__(self) -> None:
            self._cur = cur

        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return self._cur

        def transaction(self, *args: Any) -> "_Tx":
            return _Tx()

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    conn = _Conn()
    monkeypatch.setattr(
        kv_mod,
        "get_db_connection",
        create_autospec(kv_mod.get_db_connection, return_value=conn),
    )

    content = {"schema_version": 1, "name": "AI Infrastructure", "members": []}
    await kv_mod.insert_version(
        user_id=USER,
        workspace_id=WS_SECTOR,
        doc_key="sector",
        base_version_no=2,
        content=content,
        content_sha256="abc",
        reason="update members",
    )
    assert sync_calls and sync_calls[0][1] == WS_SECTOR
    assert sync_calls[0][2] is content
    assert sync_calls[0][3] == 3  # 新版本号传给投影
    # 投影同步发生在 INSERT 之后（同一事务内提交或一起回滚）
    assert _exec_log.index(
        next(s for s in _exec_log if s.startswith("INSERT INTO dalton_knowledge_versions"))
    ) < len(_exec_log) - 1


@pytest.mark.asyncio
async def test_insert_version_skips_projection_when_flag_off_or_not_sector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.database.knowledge as kv_mod

    called: list[Any] = []

    async def _spy_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
        called.append(args)
        return None

    monkeypatch.setattr(sm_db, "sync_members_projection", _spy_sync)
    # 关闭开关 / 非 sector 文档 / 校验失败的版本：一次都不投影
    # （正向组合——开 + sector + valid——由上一个测试验证）
    for flag, doc_key, valid in (
        (False, "sector", True),
        (True, "thesis", True),
        (True, "sector", False),
    ):
        monkeypatch.setattr(kv_mod, "is_coverage_units_enabled", lambda f=flag: f)
        assert await _run_insert_version(
            kv_mod, monkeypatch, doc_key=doc_key, schema_valid=valid
        )
        assert not called, (flag, doc_key, valid)


async def _run_insert_version(
    kv_mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    doc_key: str,
    schema_valid: bool,
) -> bool:
    """Run the real insert_version against a fake conn; True when it returned a row.

    The two autospecs are built from the REAL functions passed in — building
    them from the module attribute would spec a Mock on the second call
    (the first call's monkeypatch is still in force within the same test)."""
    real_ticker_resolve = _REAL_KV.get("_resolve_workspace_ticker")
    real_pool = _REAL_KV.get("get_db_connection")
    monkeypatch.setattr(
        kv_mod,
        "_resolve_workspace_ticker",
        create_autospec(real_ticker_resolve, return_value=None),
    )
    cur = _spec_cursor()

    async def _exec(sql: str, params: Any = None) -> None:
        return None

    cur.execute.side_effect = _exec
    # fetchone 序列：无 latest（base=None 对上 CAS）→ INSERT RETURNING
    cur.fetchone.side_effect = [
        None,
        {"knowledge_version_id": "kv-1", "version_no": 1},
    ]

    class _Tx:
        async def __aenter__(self) -> "_Tx":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Conn:
        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return cur

        def transaction(self, *args: Any) -> "_Tx":
            return _Tx()

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(
        kv_mod,
        "get_db_connection",
        create_autospec(real_pool, return_value=_Conn()),
    )
    row = await kv_mod.insert_version(
        user_id=USER,
        workspace_id=WS_SECTOR,
        doc_key=doc_key,
        base_version_no=None,
        content={"schema_version": 1},
        content_sha256="abc",
        reason="r",
        schema_valid=schema_valid,
    )
    return row is not None


# ---------------------------------------------------------------------------
# Schema：跨知识引用形状；evidenceRef 逐字不变；旧数据照常通过
# ---------------------------------------------------------------------------

_EVIDENCE_REF_KEYS = {
    "kind",
    "provenance_record_id",
    "path",
    "cell",
    "evidence_id",
    "url",
    "accession",
    "form",
    "period",
    "section",
    "publication_id",
    "chain_id",
}


def _common_defs() -> dict[str, Any]:
    return json.loads(
        (Path(knowledge_service.__file__).parent / "schemas" / "common.schema.json").read_text(
            encoding="utf-8"
        )
    )["$defs"]


def test_evidence_ref_definition_is_unchanged() -> None:
    """公司侧既有 evidenceRef 校验逐字不变：属性集合与 required 原样。"""
    ev_ref = _common_defs()["evidenceRef"]
    assert set(ev_ref["properties"].keys()) == _EVIDENCE_REF_KEYS
    assert ev_ref["required"] == ["kind"]
    assert ev_ref.get("additionalProperties") is False


def test_knowledge_ref_shape_requires_locator_and_relation() -> None:
    ref_def = _common_defs()["knowledgeRef"]
    assert set(ref_def["properties"].keys()) == {
        "unit_id",
        "workspace_id",
        "doc_key",
        "version_no",
        "entry_id",
        "relation",
    }
    # unit_id 可省略（目标未登记覆盖单元时按 workspace_id 解析），
    # 其余定位字段与关系都是必填
    assert set(ref_def["required"]) == {"workspace_id", "doc_key", "version_no", "entry_id", "relation"}
    assert set(ref_def["properties"]["relation"]["enum"]) == {
        "supports",
        "contradicts",
        "assumes",
        "derived_from",
    }


def _pillar(**extra: Any) -> dict[str, Any]:
    return {
        "id": "P1",
        "claim": "行业资本开支保持高增",
        "priority": "core",
        "baseline": {"text": "FY2026 同比 +35%", "as_of": "2026-10-15"},
        "expected_path": "指引继续上调",
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
            "break": [],
        },
        "status": "untested",
        "provenance": "draft",
        "created_at": "2026-10-18T12:00:00Z",
        **extra,
    }


def _evidence(**extra: Any) -> dict[str, Any]:
    return {
        "id": "E-20261018-01",
        "at": "2026-10-18T12:00:00Z",
        "period": "2026-09",
        "source": {
            "type": "official_data",
            "identity": "协会月度统计",
            "tier": 4,
            "ref": {"kind": "url", "url": "https://example.org/m"},
        },
        "fact": "行业出货量同比 +24%",
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
        **extra,
    }


def _thesis_doc(pillars: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    hv = {
        "stance": "no_view",
        "conviction": "none",
        "summary": "行业资本开支上行",
        "updated_at": "2026-10-18T12:00:00Z",
    }
    return {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "one_liner": "算力供给节奏决定行业盈利周期",
        "aggregate_status": "untested",
        "house_view": hv,
        "house_view_history": [
            {"at": "2026-10-18T12:00:00Z", "to": dict(hv), "reason": "Initial"}
        ],
        "pillars": pillars,
        "evidence": evidence,
        "official": None,
    }


def _knowledge_ref(**override: Any) -> dict[str, Any]:
    ref = {
        "unit_id": UNIT_SECTOR,
        "workspace_id": WS_SECTOR,
        "doc_key": "thesis",
        "version_no": 2,
        "entry_id": "P1",
        "relation": "assumes",
    }
    ref.update(override)
    return {k: v for k, v in ref.items() if v is not ...}


def test_thesis_accepts_knowledge_refs_on_pillar_and_evidence() -> None:
    doc = _thesis_doc(
        pillars=[_pillar(knowledge_refs=[_knowledge_ref(relation="assumes")])],
        evidence=[_evidence(knowledge_refs=[_knowledge_ref(entry_id="E-20260930-01", relation="supports")])],
    )
    assert validate_schema("thesis", doc) == []


def test_thesis_without_knowledge_refs_still_validates() -> None:
    assert validate_schema("thesis", _thesis_doc([_pillar()], [_evidence()])) == []


def test_thesis_rejects_malformed_knowledge_ref() -> None:
    bad_relation = _thesis_doc(
        pillars=[_pillar(knowledge_refs=[_knowledge_ref(relation="causes")])],
        evidence=[],
    )
    assert any("/pillars/0/knowledge_refs" in e["path"] for e in validate_schema("thesis", bad_relation))

    missing_locator = _thesis_doc(
        pillars=[],
        evidence=[_evidence(knowledge_refs=[_knowledge_ref(workspace_id=...)])],
    )
    assert any(
        "/evidence/0/knowledge_refs" in e["path"]
        for e in validate_schema("thesis", missing_locator)
    )


def test_sector_member_scope_fields_validate() -> None:
    doc = {
        "schema_version": 1,
        "name": "AI Infrastructure",
        "definition": "算力供应链",
        "coverage_stage": "onboarding",
        "one_liner": "供给节奏决定盈利周期",
        "members": [
            {
                "id": "M1",
                "name": "某综合科技公司",
                "ticker": "EXAMPLE",
                "unit_id": UNIT_COMPANY,
                "workspace_created": True,
                "scope": "只计 AI 加速卡收入",
                "reason": "算力资本开支核心受益方",
                "valid_until": "2027-06-30",
                "added_at": "2026-10-18T12:00:00Z",
            }
        ],
    }
    assert validate_schema("sector", doc) == []


def _agenda_doc(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "open_questions": questions,
        "next_validations": [],
        "watch": {"events": []},
        "wake_conditions": [],
    }


def test_agenda_question_accepts_source_ref_and_new_origins() -> None:
    linked = {
        "id": "Q-001",
        "question": "待复核：行业支柱 P1 已更新",
        "why": "行业 thesis v5 改变支柱 P1",
        "value": "high",
        "urgency": "this_week",
        "origin": "sector",
        "owner": "analyst",
        "status": "open",
        "attempts": [],
        "created_at": "2026-10-18T12:00:00Z",
        "source_ref": _knowledge_ref(version_no=5),
    }
    assert validate_schema("agenda", _agenda_doc([linked])) == []
    company_origin = {**linked, "origin": "company", "source_ref": _knowledge_ref(workspace_id=WS_COMPANY, relation="contradicts", entry_id="E-20261018-01")}
    assert validate_schema("agenda", _agenda_doc([company_origin])) == []


def test_agenda_old_origins_still_validate() -> None:
    for origin in ("analyst", "pm", "director", "event"):
        q = {
            "id": "Q-001",
            "question": "q",
            "why": "w",
            "value": "medium",
            "urgency": "this_week",
            "origin": origin,
            "owner": "analyst",
            "status": "open",
            "attempts": [],
            "created_at": "2026-10-18T12:00:00Z",
        }
        assert validate_schema("agenda", _agenda_doc([q])) == []


# ---------------------------------------------------------------------------
# 传播：纯函数 + 扫描 + 去重 + 权限
# ---------------------------------------------------------------------------


def test_changed_pillar_ids_covers_edit_add_remove() -> None:
    old = {"pillars": [{"id": "P1", "claim": "a"}, {"id": "P2", "claim": "b"}]}
    new = {"pillars": [{"id": "P1", "claim": "a"}, {"id": "P2", "claim": "b2"}, {"id": "P3", "claim": "c"}]}
    assert cross_unit.changed_pillar_ids(old, new) == {"P2", "P3"}
    assert cross_unit.changed_pillar_ids(old, old) == set()
    removed = {"pillars": [{"id": "P1", "claim": "a"}]}
    assert cross_unit.changed_pillar_ids(old, removed) == {"P2"}


def test_new_evidence_entries_by_id() -> None:
    old = {"evidence": [{"id": "E-20261018-01"}]}
    new = {"evidence": [{"id": "E-20261018-01"}, {"id": "E-20261018-02"}]}
    entries = cross_unit.new_evidence_entries(old, new)
    assert [e["id"] for e in entries] == ["E-20261018-02"]


class _ScanConn:
    """find_thesis_refs_to_subject 的求值替身：按 SQL 语义执行。

    子查询先 DISTINCT ON 取每个工作区最新有效版本，外层再对【最新版本】
    做 strpos 预筛（codex r1 #7：顺序反过来会复活 v1 里已删除的引用）。
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.seen_sql: list[str] = []
        self.seen_params: list[tuple[Any, ...]] = []

    def cursor(self, *args: Any, **kwargs: Any) -> "_ScanConn":
        return self

    async def __aenter__(self) -> "_ScanConn":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: Any, params: Any = None) -> None:
        self.seen_sql.append(" ".join(str(sql).split()))
        self.seen_params.append(tuple(params or ()))

    async def fetchall(self) -> list[dict[str, Any]]:
        sql = self.seen_sql[-1]
        if "dalton_knowledge_versions" not in sql:
            return []
        rows = list(self.rows)
        if "DISTINCT ON (workspace_id)" in sql:
            latest: dict[str, dict[str, Any]] = {}
            for r in rows:
                ws = str(r["workspace_id"])
                if ws not in latest or int(r["version_no"]) > int(latest[ws]["version_no"]):
                    latest[ws] = r
            rows = list(latest.values())
        if "strpos" in sql:
            needles = self.seen_params[-1]
            import json as _json

            rows = [
                r
                for r in rows
                if any(
                    n in _json.dumps(r.get("content"), ensure_ascii=False, default=str)
                    for n in needles
                )
            ]
        return rows


def _install_scan_pool(monkeypatch: pytest.MonkeyPatch, conn: _ScanConn) -> None:
    import src.server.database.pool as pool_mod

    monkeypatch.setattr(
        pool_mod,
        "get_db_connection",
        create_autospec(pool_mod.get_db_connection, return_value=conn),
    )


@pytest.mark.asyncio
async def test_find_thesis_refs_matches_unit_or_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "workspace_id": WS_COMPANY,
            "version_no": 3,
            "content": _thesis_doc(
                pillars=[_pillar(knowledge_refs=[_knowledge_ref(version_no=2)])],
                evidence=[],
            ),
        },
        {  # 引用的是当前版本（未过期）：不算
            "workspace_id": "55555555-5555-5555-5555-555555555555",
            "version_no": 2,
            "content": _thesis_doc(
                pillars=[_pillar(knowledge_refs=[_knowledge_ref(version_no=5)])],
                evidence=[],
            ),
        },
        {  # 自身工作区的自引用：排除
            "workspace_id": WS_SECTOR,
            "version_no": 1,
            "content": _thesis_doc(
                pillars=[_pillar(knowledge_refs=[_knowledge_ref(version_no=1)])],
                evidence=[],
            ),
        },
    ]
    conn = _ScanConn(rows)
    _install_scan_pool(monkeypatch, conn)
    out = await cross_unit.find_thesis_refs_to_subject(
        unit_id=UNIT_SECTOR,
        workspace_id=WS_SECTOR,
        relation="assumes",
        entry_ids={"P1"},
        before_version=5,
    )
    assert list(out.keys()) == [WS_COMPANY]
    # 先取最新（子查询 DISTINCT ON）、再对最新版本做参数化 strpos 预筛
    sql = conn.seen_sql[0]
    assert "DISTINCT ON (workspace_id)" in sql
    assert sql.index("DISTINCT ON (workspace_id)") < sql.index("strpos")
    assert "strpos(latest_thesis.content::text, %s)" in sql
    assert "LIKE" not in sql.upper()
    _assert_no_stray_percent(sql, conn.seen_params[0])
    assert conn.seen_params[0] == (UNIT_SECTOR, WS_SECTOR)


@pytest.mark.asyncio
async def test_find_thesis_refs_ignores_deleted_refs_in_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #7：v1 引用行业、v2 删除了全部引用——按最新版本判断，不生成待复核。"""
    rows = [
        {
            "workspace_id": WS_COMPANY,
            "version_no": 1,
            "content": _thesis_doc(
                pillars=[_pillar(knowledge_refs=[_knowledge_ref(version_no=2)])],
                evidence=[],
            ),
        },
        {
            "workspace_id": WS_COMPANY,
            "version_no": 2,  # 最新版本：引用已全部删除
            "content": _thesis_doc([_pillar()], []),
        },
    ]
    conn = _ScanConn(rows)
    _install_scan_pool(monkeypatch, conn)
    out = await cross_unit.find_thesis_refs_to_subject(
        unit_id=UNIT_SECTOR,
        workspace_id=WS_SECTOR,
        relation="assumes",
        entry_ids={"P1"},
        before_version=5,
    )
    assert out == {}


@pytest.mark.asyncio
async def test_sector_pillar_change_creates_re_review_and_dedups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_doc = _thesis_doc([_pillar()], [])
    new_doc = _thesis_doc([_pillar(claim="行业资本开支同比 +30% 起步")], [])

    async def _fake_find(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {WS_COMPANY: [_knowledge_ref()]}

    monkeypatch.setattr(cross_unit, "find_thesis_refs_to_subject", _fake_find)
    monkeypatch.setattr(
        cross_unit, "actor_can_write_workspace", create_autospec(cross_unit.actor_can_write_workspace, return_value=True)
    )

    written: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        written.append(kwargs)
        return {"status": "created", "version_no": 2}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)
    effects = await cross_unit._propagate_sector_pillar_change(
        user_id=USER,
        workspace_id=WS_SECTOR,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=5,
        sector_unit={"unit_id": UNIT_SECTOR, "kind": "sector", "display_name": "AI Infrastructure"},
    )
    assert [e["status"] for e in effects] == ["created"]
    assert effects[0]["kind"] == "re_review"
    assert effects[0]["pillars"] == ["P1"]
    assert written[0]["origin"] == "sector"
    # 去重键：源（行业）工作区 + thesis + 新版本号 + 条目 + assumes
    assert cross_unit.source_ref_key(written[0]["source_ref"]) == (
        WS_SECTOR, "thesis", "5", "P1", "assumes"
    )
    assert written[0]["source_ref"]["unit_id"] == UNIT_SECTOR

    # 同一事件再次触发（同一版本）：_append_linkage_question 里的 agenda 检查
    # 负责去重——这里直接验证该检查逻辑。
    agenda_with_ref = {
        "open_questions": [
            {"id": "Q-001", "source_ref": written[0]["source_ref"], "status": "open"}
        ]
    }
    key = cross_unit.source_ref_key(written[0]["source_ref"])
    assert cross_unit._agenda_has_key(agenda_with_ref, key)
    assert not cross_unit._agenda_has_key(
        agenda_with_ref, cross_unit.source_ref_key({**written[0]["source_ref"], "version_no": 6})
    )


@pytest.mark.asyncio
async def test_re_review_skipped_without_write_access(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_find(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {WS_COMPANY: [_knowledge_ref()]}

    monkeypatch.setattr(cross_unit, "find_thesis_refs_to_subject", _fake_find)
    monkeypatch.setattr(
        cross_unit, "actor_can_write_workspace", create_autospec(cross_unit.actor_can_write_workspace, return_value=False)
    )
    append_calls: list[Any] = []

    async def _must_not_append(**kwargs: Any) -> dict[str, Any]:
        append_calls.append(kwargs)
        return {"status": "created"}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _must_not_append)
    effects = await cross_unit._propagate_sector_pillar_change(
        user_id=USER,
        workspace_id=WS_SECTOR,
        old_doc=_thesis_doc([_pillar()], []),
        new_doc=_thesis_doc([_pillar(claim="changed")], []),
        new_version_no=5,
        sector_unit={"unit_id": UNIT_SECTOR, "kind": "sector"},
    )
    assert [e["status"] for e in effects] == ["skipped_no_access"]
    assert not append_calls


@pytest.mark.asyncio
async def test_actor_can_write_workspace_maps_sharing_denial_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.auth as auth_mod

    async def _deny(ws: str, uid: str, **kwargs: Any) -> None:
        raise HTTPException(status_code=403, detail="Forbidden")

    async def _allow(ws: str, uid: str, **kwargs: Any) -> dict[str, Any]:
        return {"workspace_id": ws}

    monkeypatch.setattr(auth_mod, "verify_workspace_access", _deny)
    assert await cross_unit.actor_can_write_workspace(USER, WS_COMPANY) is False
    monkeypatch.setattr(auth_mod, "verify_workspace_access", _allow)
    assert await cross_unit.actor_can_write_workspace(USER, WS_COMPANY) is True


class _CasAgendaEnv:
    """apply_update(agenda) 的 CAS 求值替身（codex r1 #3）。

    latest_version 与 insert_version 都带 ``await asyncio.sleep(0)``，把并发
    窗口真实打开：两个协程都读到同一个 base 时，后写者必须被 CAS 拒绝并
    重读重试。source != 'tool' 时不做 CAS——正是旧实现丢待办的路径。
    """

    def __init__(self, agenda_content: dict[str, Any] | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        if agenda_content is not None:
            self.rows.append(
                {
                    "knowledge_version_id": "kv-agenda-1",
                    "workspace_id": WS_COMPANY,
                    "doc_key": "agenda",
                    "version_no": 1,
                    "content": agenda_content,
                    "content_sha256": "x",
                    "schema_valid": True,
                    "files_written_at": None,
                    "created_at": None,
                }
            )
        self.cas_rejections = 0

    def install(self, monkeypatch: pytest.MonkeyPatch, ws: str = WS_COMPANY) -> None:
        async def fake_latest(w: str, dk: str) -> dict[str, Any] | None:
            await asyncio.sleep(0)
            rows = [r for r in self.rows if r["doc_key"] == dk]
            return rows[-1] if rows else None

        async def fake_insert(**kw: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            rows = self.rows
            latest_no = rows[-1]["version_no"] if rows else None
            if kw.get("source") == "tool" and kw.get("base_version_no") != latest_no:
                self.cas_rejections += 1
                raise db_knowledge.VersionConflict(
                    kw["doc_key"], kw.get("base_version_no"), None
                )
            row = {
                "knowledge_version_id": f"kv-agenda-{len(rows) + 1}",
                "workspace_id": kw["workspace_id"],
                "doc_key": kw["doc_key"],
                "version_no": (latest_no or 0) + 1,
                "content": copy.deepcopy(kw["content"]),
                "content_sha256": kw["content_sha256"],
                "schema_valid": True,
                "files_written_at": None,
                "created_at": None,
            }
            rows.append(row)
            return row

        monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
        monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
        monkeypatch.setattr(
            db_knowledge,
            "insert_version",
            create_autospec(db_knowledge.insert_version, side_effect=fake_insert),
        )
        monkeypatch.setattr(
            db_knowledge,
            "list_all_latest_for_workspace",
            create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
        )
        monkeypatch.setattr(
            db_knowledge,
            "mark_files_written",
            create_autospec(db_knowledge.mark_files_written),
        )
        monkeypatch.setattr(knowledge_service, "verify_workspace_access", _allow_access)

        async def fake_profile(w: Any, conn: Any = None, **kwargs: Any) -> str:
            return "company"

        monkeypatch.setattr(knowledge_service, "resolve_workspace_profile", fake_profile)

        # _append_linkage_question 现在每次尝试前都复查权限（codex r2 #6）；
        # CAS 环境默认放行，专项测试自行覆盖为权限序列。
        async def _can_write(uid: str, ws: str) -> bool:
            return True

        monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _can_write)

    def agenda_questions(self) -> list[dict[str, Any]]:
        rows = self.rows
        return (rows[-1]["content"].get("open_questions") or []) if rows else []


@pytest.mark.asyncio
async def test_append_linkage_question_uses_cas_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发两个联动写同一目标 agenda：CAS 输家重读重试，两条待办都在。"""
    env = _CasAgendaEnv(
        agenda_content={
            "schema_version": 1,
            "as_of": "2026-10-18T12:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"events": []},
            "wake_conditions": [],
        }
    )
    env.install(monkeypatch)

    async def call(entry_id: str) -> dict[str, Any]:
        return await cross_unit._append_linkage_question(
            actor_user_id=USER,
            target_workspace_id=WS_COMPANY,
            question=f"待复核 {entry_id}",
            why="并发联动测试",
            origin="sector",
            source_ref=_knowledge_ref(version_no=5, entry_id=entry_id),
        )

    res_a, res_b = await asyncio.gather(call("P1"), call("P2"))
    assert res_a["status"] == "created" and res_b["status"] == "created"
    # 打开的并发窗口里至少一次 CAS 拒绝（撤掉 CAS 重试即失败：
    # 后写者会基于同一 base 覆盖，另一条待办消失）
    assert env.cas_rejections >= 1
    questions = env.agenda_questions()
    assert {q["question"] for q in questions} == {"待复核 P1", "待复核 P2"}


@pytest.mark.asyncio
async def test_append_linkage_question_passes_cas_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _CasAgendaEnv(
        agenda_content={
            "schema_version": 1,
            "as_of": "2026-10-18T12:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"events": []},
            "wake_conditions": [],
        }
    )
    env.install(monkeypatch)
    insert_mock = db_knowledge.insert_version

    res = await cross_unit._append_linkage_question(
        actor_user_id=USER,
        target_workspace_id=WS_COMPANY,
        question="待复核：行业支柱 P1 已更新",
        why="行业 thesis v5 改变支柱 P1",
        origin="sector",
        source_ref=_knowledge_ref(version_no=5),
    )
    assert res == {"status": "created", "version_no": 2}
    _, kwargs = insert_mock.call_args
    # CAS 源（tool）+ 基线版本。skip_access_check / skip_linkage_retry 是
    # apply_update 层的参数（不透传 insert_version），其行为在
    # test_linkage_write_skips_linkage_retry 里按调用效果锁定。
    assert kwargs["source"] == "tool"
    assert kwargs["base_version_no"] == 1
    assert kwargs["ops"][0]["path"] == "/open_questions"
    value = kwargs["ops"][0]["value"]
    assert value["origin"] == "sector"
    assert value["source_ref"]["version_no"] == 5


@pytest.mark.asyncio
async def test_no_auto_retry_on_agenda_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 决定：目标 agenda 的有效写入不再自动补发（曾与回合末对账互相
    覆盖、且跨所有者/直接编辑路径触发不到）——联动写与普通写都不触发 retry，
    补发只由总监 retry 显式发起。重新引入钩子即失败。"""
    import src.dalton.knowledge.service as svc_mod

    env = _CasAgendaEnv(
        agenda_content={
            "schema_version": 1,
            "as_of": "2026-10-18T12:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"events": []},
            "wake_conditions": [],
        }
    )
    env.install(monkeypatch)
    monkeypatch.setattr(svc_mod, "is_coverage_units_enabled", lambda: True)

    retry_calls: list[dict[str, Any]] = []

    async def _spy_retry(uid: str, **kwargs: Any) -> dict[str, Any]:
        retry_calls.append({"uid": uid, **kwargs})
        return {"results": [], "remaining": 0}

    monkeypatch.setattr(cross_unit, "retry_linkage_rows", _spy_retry)

    res = await cross_unit._append_linkage_question(
        actor_user_id=USER,
        target_workspace_id=WS_COMPANY,
        question="待复核：行业支柱 P1 已更新",
        why="行业 thesis v5 改变支柱 P1",
        origin="sector",
        source_ref=_knowledge_ref(version_no=5),
    )
    assert res["status"] == "created"

    normal = await svc_mod.apply_update(
        user_id=USER,
        workspace_id=WS_COMPANY,
        doc_key="agenda",
        ops=[
            {
                "op": "append_item",
                "path": "/open_questions",
                "value": {
                    "question": "普通议程问题",
                    "why": "分析师添加的一条普通问题",
                    "value": "medium",
                    "urgency": "this_week",
                    "origin": "analyst",
                    "status": "open",
                },
            }
        ],
        reason="普通议程更新",
        base_version=2,
        source="tool",
        backend=None,
        skip_access_check=True,
    )
    assert normal["ok"] is True
    assert "联动失败" not in normal["message"]
    assert not retry_calls  # 联动写与普通 agenda 写入都不自动补发


@pytest.mark.asyncio
async def test_append_linkage_question_skips_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = _knowledge_ref(version_no=5)
    agenda_latest = {
        "version_no": 7,
        "content": {"open_questions": [{"id": "Q-001", "source_ref": ref, "status": "open"}]},
    }
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(db_knowledge.latest_version, return_value=agenda_latest),
    )
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    called: list[Any] = []

    async def _must_not_apply(**kwargs: Any) -> dict[str, Any]:
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(knowledge_service, "apply_update", _must_not_apply)
    res = await cross_unit._append_linkage_question(
        actor_user_id=USER,
        target_workspace_id=WS_COMPANY,
        question="q",
        why="w",
        origin="sector",
        source_ref=ref,
    )
    assert res == {"status": "skipped_duplicate"}
    assert not called


_SECTOR_UNIT_ROW = {
    "unit_id": UNIT_SECTOR,
    "kind": "sector",
    "status": "active",
    "workspace_id": WS_SECTOR,
    "display_name": "AI Infrastructure",
}


def _install_contradiction_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sector_unit_row: dict[str, Any] | None = None,
    company_unit_row: dict[str, Any] | None = None,
    can_write: Any = None,
) -> list[dict[str, Any]]:
    """公司→行业传播的环境替身；返回 written（_append_linkage_question 收到的参数）。"""
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit, return_value=sector_unit_row or _SECTOR_UNIT_ROW
        ),
    )
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            cu_db.get_active_unit_by_workspace, return_value=company_unit_row
        ),
    )
    if can_write is None:
        can_write = True
    if isinstance(can_write, list):
        monkeypatch.setattr(
            cross_unit,
            "actor_can_write_workspace",
            create_autospec(cross_unit.actor_can_write_workspace, side_effect=can_write),
        )
    else:
        default_val = bool(can_write)

        async def _fixed_can_write(uid: str, ws: str) -> bool:
            return default_val

        monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _fixed_can_write)
    written: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        written.append(kwargs)
        return {"status": "created", "version_no": 3}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)
    return written


def _company_thesis_with_contradictions(*evidence: dict[str, Any]) -> dict[str, Any]:
    old_doc = _thesis_doc([_pillar()], [])
    new_doc = _thesis_doc([_pillar()], [_evidence(id="E-20261018-01"), *evidence])
    return old_doc, new_doc


def _contradicts_ref(pillar: str, **override: Any) -> dict[str, Any]:
    return _knowledge_ref(
        unit_id=UNIT_SECTOR,
        workspace_id=WS_SECTOR,
        entry_id=pillar,
        relation="contradicts",
        **override,
    )


@pytest.mark.asyncio
async def test_company_contradiction_creates_sector_update_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _install_contradiction_env(
        monkeypatch,
        company_unit_row={"unit_id": UNIT_COMPANY, "kind": "company", "status": "active",
                          "workspace_id": WS_COMPANY},
    )
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_contradicts_ref("P1", version_no=3)])
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert [e["status"] for e in effects] == ["created"]
    assert effects[0]["kind"] == "update_candidate"
    assert effects[0]["target_workspace_id"] == WS_SECTOR
    assert written[0]["origin"] == "company"
    # 来源锚点指向公司侧证据条目（E-…），公司单元可解析时带上（codex r1 #10）
    src = written[0]["source_ref"]
    assert src["entry_id"] == "E-20261018-02"
    assert src["workspace_id"] == WS_COMPANY
    assert src["unit_id"] == UNIT_COMPANY
    # 被反驳的行业支柱在 target_ref；去重键 = 公司工作区+版本+支柱
    tgt = written[0]["target_ref"]
    assert tgt["entry_id"] == "P1"
    assert tgt["workspace_id"] == WS_SECTOR
    assert cross_unit.update_candidate_dedup_key(src, tgt) == (
        WS_COMPANY, "thesis", "4", "P1", "contradicts"
    )
    assert written[0]["evidence_ids"] == ["E-20261018-02"]


@pytest.mark.asyncio
async def test_company_contradictions_same_pillar_merge_into_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #10：同版本两条证据反驳同一支柱 → 一条通知列出全部证据；
    不同支柱 → 各一条（去重键互不相同）。"""
    written = _install_contradiction_env(monkeypatch)
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(id="E-20261018-03", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(id="E-20261018-04", knowledge_refs=[_contradicts_ref("P2")]),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert [e["status"] for e in effects] == ["created", "created"]
    by_pillar = {e["entry_id"]: e for e in effects}
    assert by_pillar["P1"]["evidence_ids"] == ["E-20261018-02", "E-20261018-03"]
    assert by_pillar["P2"]["evidence_ids"] == ["E-20261018-04"]
    # 通知正文列出全部证据 id
    p1_write = next(w for w in written if w["target_ref"]["entry_id"] == "P1")
    assert "E-20261018-02" in p1_write["question"] and "E-20261018-03" in p1_write["question"]
    keys = {cross_unit.update_candidate_dedup_key(w["source_ref"], w["target_ref"]) for w in written}
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_company_contradictions_check_permission_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #9：逐条检查权限——第一条放行后授权撤销，第二条跳过。"""
    written = _install_contradiction_env(monkeypatch, can_write=[True, False])
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(id="E-20261018-03", knowledge_refs=[_contradicts_ref("P2")]),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    by_status = {e["entry_id"]: e["status"] for e in effects}
    assert by_status == {"P1": "created", "P2": "skipped_no_access"}
    assert len(written) == 1  # 撤销后没有第二条写入


@pytest.mark.asyncio
async def test_company_contradiction_ignores_non_sector_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_calls: list[str] = []

    async def _fake_get(unit_id: str, **kwargs: Any) -> dict[str, Any] | None:
        get_calls.append(unit_id)
        return {"unit_id": unit_id, "kind": "company", "status": "active", "workspace_id": WS_COMPANY}

    monkeypatch.setattr(cu_db, "get_coverage_unit", _fake_get)
    new_doc = _thesis_doc(
        [_pillar()],
        [
            _evidence(
                id="E-20261018-02",
                knowledge_refs=[_knowledge_ref(unit_id=UNIT_COMPANY, workspace_id=WS_COMPANY, entry_id="P2", relation="contradicts")],
            )
        ],
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=_thesis_doc([_pillar()], []),
        new_doc=new_doc,
        new_version_no=4,
    )
    assert effects == []
    assert get_calls == [UNIT_COMPANY]


@pytest.mark.asyncio
async def test_company_thesis_without_contradictions_touches_no_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通公司 thesis 更新（开关开、无跨单元引用）：传播路径一次数据库都不查。"""
    import src.dalton.settings as settings_mod

    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no registry lookup expected for a plain thesis update")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _forbidden)
    effects = await cross_unit.propagate_thesis_change(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=_thesis_doc([_pillar()], []),
        new_doc=_thesis_doc([_pillar()], [_evidence()]),
        new_version_no=2,
        profile="company",
    )
    assert effects == []


# ---------------------------------------------------------------------------
# apply_update 挂钩：开关关闭零调用；开启时带旧/新文档与新版本号
# ---------------------------------------------------------------------------


class _KVStore:
    def __init__(self) -> None:
        self.store: dict[str, list[dict[str, Any]]] = {}

    async def latest(self, ws: str, doc_key: str) -> dict[str, Any] | None:
        rows = self.store.get(doc_key, [])
        return rows[-1] if rows else None


async def _allow_access(ws: str, uid: str, **kwargs: Any) -> dict[str, Any]:
    return {"workspace_id": ws}


@pytest.mark.asyncio
async def test_apply_update_propagation_gated_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    fx = _KVStore()
    fx.store["thesis"] = [
        {
            "knowledge_version_id": "kv-t1",
            "workspace_id": WS_SECTOR,
            "doc_key": "thesis",
            "version_no": 1,
            "content": _thesis_doc([_pillar()], []),
            "content_sha256": "x",
            "source": "tool",
            "schema_valid": True,
            "files_written_at": None,
            "created_at": None,
        }
    ]
    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        return await fx.latest(ws, dk)

    monkeypatch.setattr(knowledge_service, "verify_workspace_access", _allow_access)
    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "mark_files_written",
        create_autospec(db_knowledge.mark_files_written),
    )
    monkeypatch.setattr(
        db_knowledge,
        "insert_version",
        create_autospec(
            db_knowledge.insert_version,
            return_value={
                "knowledge_version_id": "kv-t2",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 2,
                "content": _thesis_doc([_pillar(claim="changed")], []),
                "schema_valid": True,
            },
        ),
    )

    prop_calls: list[dict[str, Any]] = []

    async def _spy_propagate(**kwargs: Any) -> list[dict[str, Any]]:
        prop_calls.append(kwargs)
        return []

    monkeypatch.setattr(cross_unit, "propagate_thesis_change", _spy_propagate)

    async def _run(profile: str) -> dict[str, Any]:
        async def _fake_profile(ws: Any, conn: Any = None, **kwargs: Any) -> str:
            return profile

        monkeypatch.setattr(knowledge_service, "resolve_workspace_profile", _fake_profile)
        return await knowledge_service.apply_update(
            user_id=USER,
            workspace_id=WS_SECTOR,
            doc_key="thesis",
            ops=[{"op": "set", "path": "/pillars/P1/claim", "value": "行业资本开支同比 +30% 起步"}],
            reason="行业观点更新",
            base_version=1,
            source="tool",
            backend=None,
        )

    # 开关关闭（默认）：传播入口一次都不进
    monkeypatch.setattr(knowledge_service, "is_coverage_units_enabled", lambda: False)
    res = await _run("sector")
    assert res["ok"] is True
    assert "cross_unit_effects" not in res
    assert not prop_calls

    # 开关开启：带着 旧文档 / 新文档 / 新版本号 / profile 进传播
    monkeypatch.setattr(knowledge_service, "is_coverage_units_enabled", lambda: True)
    fx.store["thesis"][-1]["version_no"] = 1
    res2 = await _run("sector")
    assert res2["ok"] is True
    assert len(prop_calls) == 1
    kwargs = prop_calls[0]
    assert kwargs["user_id"] == USER
    assert kwargs["workspace_id"] == WS_SECTOR
    assert kwargs["old_doc"]["pillars"][0]["claim"] == "行业资本开支保持高增"
    assert kwargs["new_doc"]["pillars"][0]["claim"] == "行业资本开支同比 +30% 起步"
    assert kwargs["new_version_no"] == 2
    assert kwargs["profile"] == "sector"


@pytest.mark.asyncio
async def test_failed_linkage_surfaces_in_message_and_persists_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #4：联动失败要写进工具 message（knowledge_update 只回传 message），
    并持久化为可重试的 gap 行。"""
    import src.dalton.database.cross_unit_requests as cur_db
    import src.dalton.knowledge.service as svc_mod

    monkeypatch.setattr(svc_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(svc_mod, "verify_workspace_access", _allow_access)

    async def fake_profile(ws: Any, conn: Any = None, **kwargs: Any) -> str:
        return "sector"

    monkeypatch.setattr(svc_mod, "resolve_workspace_profile", fake_profile)

    failed_effect = {
        "kind": "re_review",
        "status": "failed",
        "error": "doc_not_initialized",
        "target_workspace_id": WS_COMPANY,
        "sector_unit_id": UNIT_SECTOR,
        "origin": "sector",
        "question": "待复核：行业支柱 P1 已更新",
        "source_ref": _knowledge_ref(version_no=5),
    }

    async def _propagate_returning_failure(**kwargs: Any) -> list[dict[str, Any]]:
        return [failed_effect]

    monkeypatch.setattr(
        cross_unit, "propagate_thesis_change", _propagate_returning_failure
    )
    insert_spy = create_autospec(cur_db.insert_request, return_value={"request_id": "r1"})
    monkeypatch.setattr(cur_db, "insert_request", insert_spy)

    fx = _KVStore()
    fx.store["thesis"] = [
        {
            "knowledge_version_id": "kv-t1",
            "workspace_id": WS_SECTOR,
            "doc_key": "thesis",
            "version_no": 1,
            "content": _thesis_doc([_pillar()], []),
            "content_sha256": "x",
            "source": "tool",
            "schema_valid": True,
            "files_written_at": None,
            "created_at": None,
        }
    ]

    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        return await fx.latest(ws, dk)

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "mark_files_written",
        create_autospec(db_knowledge.mark_files_written),
    )
    monkeypatch.setattr(
        db_knowledge,
        "insert_version",
        create_autospec(
            db_knowledge.insert_version,
            return_value={
                "knowledge_version_id": "kv-t2",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 2,
                "content": _thesis_doc([_pillar(claim="changed")], []),
                "schema_valid": True,
            },
        ),
    )

    res = await svc_mod.apply_update(
        user_id=USER,
        workspace_id=WS_SECTOR,
        doc_key="thesis",
        ops=[{"op": "set", "path": "/pillars/P1/claim", "value": "changed"}],
        reason="行业观点更新",
        base_version=1,
        source="tool",
        backend=None,
    )
    assert res["ok"] is True
    # message 里可见：失败的条目、原因、以及如实写明的去向（不再承诺自动补发）
    assert "联动失败 1 条" in res["message"]
    assert WS_COMPANY in res["message"] and "doc_not_initialized" in res["message"]
    assert "已记为 gap" in res["message"] and "retry" in res["message"]
    assert res["cross_unit_effects"][0]["status"] == "failed"
    # 持久化为可重试缺口（kind=linkage_retry，初始状态 gap 由 DB 层决定）
    insert_spy.assert_awaited_once()
    _, ins_kwargs = insert_spy.call_args
    assert ins_kwargs["kind"] == "linkage_retry"
    assert ins_kwargs["target_workspace_id"] == WS_COMPANY
    assert ins_kwargs["question"].startswith("待复核")
    assert ins_kwargs["payload"]["source_ref"]["version_no"] == 5
    assert ins_kwargs["payload"]["error"] == "doc_not_initialized"

    # 记账本身失败：message 必须如实说"未能记账"，不得声称已记为 gap（codex r2 #2）
    async def _boom_insert(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(cur_db, "insert_request", _boom_insert)
    fx.store["thesis"][-1]["version_no"] = 1
    res2 = await svc_mod.apply_update(
        user_id=USER,
        workspace_id=WS_SECTOR,
        doc_key="thesis",
        ops=[{"op": "set", "path": "/pillars/P1/claim", "value": "changed-again"}],
        reason="行业观点更新",
        base_version=1,
        source="tool",
        backend=None,
    )
    assert res2["ok"] is True
    assert "联动失败 1 条" in res2["message"]
    assert "未能记账" in res2["message"]
    assert "已记为 gap" not in res2["message"]


@pytest.mark.asyncio
async def test_retry_linkage_rows_replays_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """总监显式重试：成功补发并把行标 fulfilled；权限被撤销则保留 gap。"""
    from src.dalton.database import cross_unit_requests as cur_db

    gap_row = {
        "request_id": "99999999-9999-9999-9999-999999999999",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "status": "gap",
        "target_workspace_id": WS_COMPANY,
        "question": "待复核：行业支柱 P1 已更新",
        "payload": {
            "origin": "sector",
            "source_ref": _knowledge_ref(version_no=5),
            "error": "doc_not_initialized",
        },
    }
    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(
            _REAL_CUR["list_linkage_retry_batch"],
            side_effect=[([gap_row], 1), ([], 0)],  # 第二次调用是 remaining 的重查
        ),
    )
    update_spy = create_autospec(
        _REAL_CUR["update_request"], return_value={**gap_row, "status": "fulfilled"}
    )
    monkeypatch.setattr(cur_db, "update_request", update_spy)
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    appended: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        appended.append(kwargs)
        return {"status": "created", "version_no": 4}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    outcome = await cross_unit.retry_linkage_rows(USER, target_workspace_id=WS_COMPANY)
    results = outcome["results"]
    assert [r["status"] for r in results] == ["created"]
    assert appended[0]["source_ref"]["version_no"] == 5
    assert appended[0]["origin"] == "sector"
    up_args, up_kwargs = update_spy.call_args
    assert up_args[0] == gap_row["request_id"] and up_args[1] == USER
    assert up_kwargs["status"] == "fulfilled"
    assert up_kwargs["result"][0]["retried"] is True
    assert outcome["remaining"] == 0  # 唯一一条补发成功后离开 gap

    # 权限被撤销：保留 gap，只记 skipped，remaining 不减
    async def _denied(uid: str, ws: str) -> bool:
        return False

    monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _denied)
    update_spy.reset_mock()
    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(
            _REAL_CUR["list_linkage_retry_batch"],
            return_value=([gap_row], 1),  # 行未离开 gap：重查计数仍是 1
        ),
    )
    outcome2 = await cross_unit.retry_linkage_rows(USER, target_workspace_id=WS_COMPANY)
    assert outcome2["results"][0]["status"] == "skipped_no_access"
    assert outcome2["remaining"] == 1
    update_spy.assert_not_called()


# ---------------------------------------------------------------------------
# codex r2 #1：传播逐目标 try/except —— 一个目标出错不中止其余目标
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propagation_survives_per_target_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    WS_A = "aaaaaaaa-0000-0000-0000-000000000001"
    WS_B = "aaaaaaaa-0000-0000-0000-000000000002"
    WS_C = "aaaaaaaa-0000-0000-0000-000000000003"

    async def _fake_find(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {
            WS_A: [_knowledge_ref(version_no=2, entry_id="P1")],
            WS_B: [_knowledge_ref(version_no=2, entry_id="P1")],
            WS_C: [_knowledge_ref(version_no=2, entry_id="P1")],
        }

    monkeypatch.setattr(cross_unit, "find_thesis_refs_to_subject", _fake_find)
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    written: list[str] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        target = kwargs["target_workspace_id"]
        written.append(target)
        if target == WS_B:
            raise RuntimeError("db hiccup on second target")
        return {"status": "created", "version_no": 3}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    effects = await cross_unit._propagate_sector_pillar_change(
        user_id=USER,
        workspace_id=WS_SECTOR,
        old_doc=_thesis_doc([_pillar()], []),
        new_doc=_thesis_doc([_pillar(claim="changed")], []),
        new_version_no=5,
        sector_unit={"unit_id": UNIT_SECTOR, "kind": "sector", "display_name": "AI"},
    )
    # 三个目标都处理了：第一个、第三个照常写入，第二个失败但不中止
    assert written == [WS_A, WS_B, WS_C]
    by_ws = {e["target_workspace_id"]: e for e in effects}
    assert by_ws[WS_A]["status"] == "created"
    assert by_ws[WS_C]["status"] == "created"
    failed = by_ws[WS_B]
    assert failed["status"] == "failed"
    assert "db hiccup" in failed["error"]
    # 失败 effect 带完整信息：question 非空、来源锚点、目标
    assert failed["question"].startswith("待复核")
    assert failed["source_ref"]["entry_id"] == "P1"
    assert failed["target_workspace_id"] == WS_B


# ---------------------------------------------------------------------------
# codex r2 #3：重试选取——目标条件下推 + 升序 + 游标，旧 gap 不被新 gap 挡住
# ---------------------------------------------------------------------------


class _BatchLedger:
    """list_linkage_retry_batch 的 SQL 语义替身：WHERE 下推的目标过滤、
    (created_at, request_id) 升序、LIMIT 与 keyset 游标、剩余计数。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = sorted(
            rows, key=lambda r: (str(r.get("created_at")), str(r.get("request_id")))
        )

    async def batch(
        self,
        owner: str,
        *,
        target_workspace_id: str | None = None,
        limit: int = 20,
        cursor: tuple[Any, str] | None = None,
        conn: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [
            r
            for r in self.rows
            if r.get("owner_user_id") == owner and r.get("status") == "gap"
        ]
        if target_workspace_id:
            rows = [
                r for r in rows if str(r.get("target_workspace_id")) == str(target_workspace_id)
            ]
        if cursor is not None:
            key = (str(cursor[0]), str(cursor[1]))
            rows = [
                r
                for r in rows
                if (str(r.get("created_at")), str(r.get("request_id"))) > key
            ]
        page = rows[:limit]
        return page, len(rows)


@pytest.mark.asyncio
async def test_retry_selection_pushes_target_filter_and_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #3 场景：20 条较新的 gap（目标 A，无权恢复）挡在前面；
    第 21 条较旧的 gap（目标 B，已可写）。按目标过滤下推 + 升序扫描，
    指定 B 的重试立刻取到它；不指定目标时分批也处理得到。"""
    from src.dalton.database import cross_unit_requests as cur_db

    def row(rid: str, target: str, day: str) -> dict[str, Any]:
        return {
            "request_id": rid,
            "owner_user_id": USER,
            "kind": "linkage_retry",
            "status": "gap",
            "target_workspace_id": target,
            "question": f"待复核 {rid}",
            "payload": {"origin": "sector", "source_ref": _knowledge_ref(version_no=5, entry_id=rid)},
            "created_at": day,
        }

    ws_a = "aaaaaaaa-0000-0000-0000-00000000000a"
    old_b = row("99999999-9999-9999-9999-999999999999", WS_COMPANY, "2026-10-01")
    new_a = [row(f"0000000{i}-0000-0000-0000-00000000000a", ws_a, f"2026-10-{i + 10:02d}") for i in range(20)]
    ledger = _BatchLedger([old_b, *new_a])
    monkeypatch.setattr(
        cur_db, "list_linkage_retry_batch", _BatchLedger.batch.__get__(ledger)
    )

    can_write_targets = {WS_COMPANY}

    async def _can_write(uid: str, ws: str) -> bool:
        return ws in can_write_targets

    monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _can_write)

    appended: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        appended.append(kwargs)
        return {"status": "created", "version_no": 3}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    async def _fulfill(request_id: str, owner: str, **kwargs: Any) -> dict[str, Any]:
        # 与生产一致：补发成功后行离开 gap（替身账本同步改状态，分批才推进）
        for r in _active_ledger().rows:
            if str(r.get("request_id")) == str(request_id):
                r["status"] = "fulfilled"
        return {"request_id": request_id, "status": "fulfilled"}

    ledger_holder: dict[str, Any] = {"ledger": ledger}

    def _active_ledger() -> Any:
        return ledger_holder["ledger"]

    monkeypatch.setattr(cur_db, "update_request", _fulfill)

    # 指定目标 B：SQL 过滤直接命中最旧的可恢复行（旧实现取最新 20 条 A、
    # Python 再筛，永远到不了它——撤修即失败）
    outcome = await cross_unit.retry_linkage_rows(USER, target_workspace_id=WS_COMPANY, limit=20)
    assert [r["status"] for r in outcome["results"]] == ["created"]
    assert outcome["results"][0]["request_id"] == old_b["request_id"]
    assert outcome["remaining"] == 0

    # 不指定目标、批大小 10：升序扫描让最旧的 B 第一批就被处理（旧实现
    # DESC 取最新 20 条、Python 再筛，B 永远排不进批次——撤修即失败），
    # 无权的 A 逐条 skipped_no_access 并保留 gap（remaining 如实反映存量）。
    ledger2 = _BatchLedger([row("99999999-9999-9999-9999-999999999999", WS_COMPANY, "2026-10-01"), *new_a])
    ledger_holder["ledger"] = ledger2
    monkeypatch.setattr(cur_db, "list_linkage_retry_batch", _BatchLedger.batch.__get__(ledger2))
    appended.clear()
    out1 = await cross_unit.retry_linkage_rows(USER, limit=10)
    assert len(out1["results"]) == 10
    assert out1["results"][0]["request_id"] == old_b["request_id"]
    assert out1["results"][0]["status"] == "created"
    assert all(
        r["status"] == "skipped_no_access" for r in out1["results"][1:]
    )
    assert out1["remaining"] == 20  # 20 条无权 A 仍是 gap
    # 停留在 gap 的行不会自己离开，翻批必须带游标（下一批从上一批末行之后
    # 开始）；remaining 是从游标起算的未扫存量
    out2 = await cross_unit.retry_linkage_rows(USER, limit=10, cursor=out1["next_cursor"])
    assert len(out2["results"]) == 10
    assert all(r["status"] == "skipped_no_access" for r in out2["results"])
    assert out2["remaining"] == 11  # 游标之后还剩 11 条
    out3 = await cross_unit.retry_linkage_rows(USER, limit=10, cursor=out2["next_cursor"])
    assert len(out3["results"]) == 1 and out3["results"][0]["status"] == "skipped_no_access"
    assert out3["remaining"] == 1
    assert out3["next_cursor"] is None  # 扫完


# ---------------------------------------------------------------------------
# codex r2 #5：台账 update 校验合并后的有效值
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_unit_request_update_validates_merged_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.server.database.conversation.threads_read as threads_read
    from src.dalton.database import cross_unit_requests as cur_db

    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    rid = "99999999-9999-9999-9999-999999999999"
    ws_x = "aaaaaaaa-0000-0000-0000-00000000000x"
    ws_y = "aaaaaaaa-0000-0000-0000-00000000000y"
    thread_x = "12345678-1234-1234-1234-123456789012"

    real_get_thread = threads_read.get_thread_by_id

    def existing_row() -> dict[str, Any]:
        return {
            "request_id": rid,
            "owner_user_id": USER,
            "status": "dispatched",
            "target_workspace_id": ws_x,
            "dispatch_thread_id": thread_x,
            "result": [],
        }

    # 场景 1：旧行 target=X/thread=T_X；更新只换 target=Y（可委派），
    # 省略线程——合并后 T_X 不属于 Y，必须拒绝
    monkeypatch.setattr(
        cur_db, "get_request", create_autospec(_REAL_CUR["get_request"], return_value=existing_row())
    )
    monkeypatch.setattr(
        threads_read,
        "get_thread_by_id",
        create_autospec(real_get_thread, return_value={"thread_id": thread_x, "workspace_id": ws_x}),
    )
    res = json.loads(
        await tool.ainvoke(
            {"action": "update", "request_id": rid, "target_workspace_id": ws_y},
            config=_CFG,
        )
    )
    assert res["ok"] is False and res["error"] == "invalid_dispatch_thread"

    # 场景 2：X 的委派权限已被撤销；更新只传属于 X 的线程 + dispatched，
    # 省略 target——合并后的有效目标 X 必须重查权限（403 传出）
    async def _deny_write(ws: str, uid: str, **kwargs: Any) -> None:
        if ws == ws_x:
            raise HTTPException(status_code=403, detail="Forbidden")
        return {}

    monkeypatch.setattr(dt_mod, "verify_workspace_access", _deny_write)
    with pytest.raises(HTTPException):
        await tool.ainvoke(
            {
                "action": "update",
                "request_id": rid,
                "status": "dispatched",
                "dispatch_thread_id": thread_x,
            },
            config=_CFG,
        )

    # 收尾类更新（fulfilled 带 result、不动派单关联）不被历史权限卡死
    monkeypatch.setattr(
        dt_mod, "verify_workspace_access", create_autospec(_REAL_DT["verify_workspace_access"])
    )
    monkeypatch.setattr(
        cur_db,
        "update_request",
        create_autospec(
            _REAL_CUR["update_request"], return_value={**existing_row(), "status": "fulfilled"}
        ),
    )
    res3 = json.loads(
        await tool.ainvoke(
            {"action": "update", "request_id": rid, "status": "fulfilled",
             "result": [{"completion": "done"}]},
            config=_CFG,
        )
    )
    assert res3["ok"] is True and res3["request"]["status"] == "fulfilled"


# ---------------------------------------------------------------------------
# codex r2 #6：每次 CAS 重试前复查权限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_rechecked_between_cas_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次写入遇 CAS 冲突 → 撤权 → 第二次尝试不写入。"""
    env = _CasAgendaEnv(
        agenda_content={
            "schema_version": 1,
            "as_of": "2026-10-18T12:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"events": []},
            "wake_conditions": [],
        }
    )
    env.install(monkeypatch)
    # 第一次 insert 制造并发冲突，后续走环境自身的正常 CAS 语义
    inner_insert = db_knowledge.insert_version.side_effect  # env 的 fake_insert
    calls = {"n": 0}

    async def _conflict_first(**kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise db_knowledge.VersionConflict("agenda", kw.get("base_version_no"), None)
        return await inner_insert(**kw)

    monkeypatch.setattr(db_knowledge, "insert_version", _conflict_first)
    # 权限序列：第一次尝试放行（撞 CAS），第二次拒绝（撤权）
    seq = [True, False]

    async def _seq_can_write(uid: str, ws: str) -> bool:
        return seq.pop(0) if seq else False

    monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _seq_can_write)

    res = await cross_unit._append_linkage_question(
        actor_user_id=USER,
        target_workspace_id=WS_COMPANY,
        question="待复核 P1",
        why="w",
        origin="sector",
        source_ref=_knowledge_ref(version_no=5),
    )
    assert res == {"status": "skipped_no_access"}
    # 第一次尝试真实发生（撞了 CAS），第二次因无权而止步，没有再写
    assert calls["n"] == 1
    assert env.agenda_questions() == []


@pytest.mark.asyncio
async def test_reconcile_pending_with_real_edit_records_and_preserves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r4 #1：待落盘 vN+1 期间文件另有真实修改 X → 按原逻辑记
    direct_edit，且下一回合 apply_pending 不覆盖 X。"""
    import hashlib as _hashlib

    import src.dalton.knowledge.reconcile as rec
    from src.dalton.knowledge.service import canonical_json

    async def fake_profile(ws: Any, conn: Any = None, **kwargs: Any) -> str:
        return "sector"

    monkeypatch.setattr(rec, "resolve_workspace_profile", fake_profile)
    monkeypatch.setattr(rec, "is_coverage_units_enabled", lambda: True)

    v1_doc = _thesis_doc([_pillar()], [])
    _, v1_sha = canonical_json(v1_doc)
    v2_doc = _thesis_doc([_pillar(claim="待落盘的联动版本")], [])
    _, v2_sha = canonical_json(v2_doc)
    # 文件里的真实修改 X：以 v1 为底改了支柱论断
    x_doc = _thesis_doc([_pillar(claim="文件里的真实修改 X")], [])
    x_text, _x_sha = canonical_json(x_doc)

    store: dict[str, list[dict[str, Any]]] = {
        "thesis": [
            {
                "knowledge_version_id": "kv-t1",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 1,
                "content": v1_doc,
                "content_sha256": v1_sha,
                "source": "tool",
                "schema_valid": True,
                "files_written_at": "2026-10-18T11:00:00+00:00",
                "created_at": "2026-10-18T11:00:00+00:00",
            },
            {
                "knowledge_version_id": "kv-t2",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 2,
                "content": v2_doc,
                "content_sha256": v2_sha,
                "source": "tool",
                "schema_valid": True,
                "files_written_at": None,  # 联动/retry 写入，尚未落盘
                "created_at": "2026-10-18T12:00:00+00:00",
            },
        ]
    }

    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        rows = store.get(dk, [])
        return rows[-1] if rows else None

    async def fake_list_all(ws: str, valid_only: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dk, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[dk] = filtered[-1]
        return out

    async def fake_insert(**kw: Any) -> dict[str, Any]:
        rows = store.setdefault(kw["doc_key"], [])
        row = {
            "knowledge_version_id": f"kv-{kw['doc_key']}-{len(rows) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": len(rows) + 1,
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "source": kw.get("source"),
            "schema_valid": kw.get("schema_valid", True),
            "edited_path": kw.get("edited_path"),
            "files_written_at": None,
            "created_at": None,
        }
        rows.append(row)
        return row

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all)
    monkeypatch.setattr(db_knowledge, "insert_version", fake_insert)

    async def fake_latest_written(ws: str, dk: str) -> dict[str, Any] | None:
        rows = [r for r in store.get(dk, []) if r.get("files_written_at") is not None]
        return rows[-1] if rows else None

    monkeypatch.setattr(
        db_knowledge,
        "latest_written_version",
        create_autospec(db_knowledge.latest_written_version, side_effect=fake_latest_written),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_written_valid_version",
        create_autospec(db_knowledge.latest_written_valid_version, return_value=None),
    )
    monkeypatch.setattr(
        db_knowledge, "mark_files_written", create_autospec(db_knowledge.mark_files_written)
    )
    from src.dalton.database import publications as db_publications

    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(db_publications.list_publication_files, return_value=[]),
    )

    async def fake_meta(ws: str) -> dict[str, Any]:
        return {
            "data/thesis.json": {
                "kind": "file",
                "content_hash": _hashlib.sha256(x_text.encode("utf-8")).hexdigest(),
            }
        }

    monkeypatch.setattr(rec, "get_file_metadata_for_sync", fake_meta)

    async def fake_get_file(ws: str, path: str, include_content: bool = False) -> dict[str, Any]:
        return {"path": path}

    monkeypatch.setattr(rec, "db_get_file", fake_get_file)

    async def fake_resolve_text(file_row: Any, *, user_id: str, context: str) -> str | None:
        return x_text if file_row and file_row["path"] == "data/thesis.json" else None

    monkeypatch.setattr(rec, "resolve_file_text_or_none", fake_resolve_text)

    result = await rec.detect_direct_edits(
        WS_SECTOR, USER, storage_owner_id=USER, skip_access_check=True
    )
    # 原逻辑照常检测：真实修改记为 direct_edit 版本（内容即 X）
    recorded = result["recorded_versions"]
    assert len(recorded) == 1
    assert recorded[0]["source"] == "direct_edit"
    assert recorded[0]["content"]["pillars"][0]["claim"] == "文件里的真实修改 X"

    # 下一回合 apply_pending：JSON direct_edit 分支不覆盖 data/thesis.json
    class _MemBackend:
        def __init__(self, files: dict[str, str]) -> None:
            self.files = dict(files)

        async def aread_text(self, path: str) -> str | None:
            return self.files.get(path)

        async def awrite_text(self, path: str, text: str) -> None:
            self.files[path] = text

    from src.dalton.database import verdicts as db_verdicts

    monkeypatch.setattr(
        db_verdicts,
        "list_verdicts",
        create_autospec(db_verdicts.list_verdicts, return_value=[]),
    )

    async def fake_mark_written(kv_id: str) -> None:
        for rows in store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = "2026-10-18T13:00:00+00:00"

    monkeypatch.setattr(db_knowledge, "mark_files_written", fake_mark_written)
    backend = _MemBackend({"data/thesis.json": x_text})
    await rec.apply_pending(
        WS_SECTOR, backend, user_id=USER, storage_owner_id=USER, skip_access_check=True
    )
    assert "文件里的真实修改 X" in backend.files["data/thesis.json"]  # X 仍在
    # direct_edit 版本已被记账落盘（history 快照写出了它的内容）
    assert any(
        r.get("files_written_at") is not None and r.get("source") == "direct_edit"
        for r in store["thesis"]
    )


@pytest.mark.asyncio
async def test_same_pillar_group_failure_is_one_gap_with_all_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r4 #2：E1、E2 同版反驳同一支柱 P1，整组解析失败 → 恰好一条
    可重放 gap 含 E1+E2，无部分通知；retry 补发的通知带全部证据。"""
    from src.dalton.database import cross_unit_requests as cur_db

    async def _raise_get(unit_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("transient registry failure")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _raise_get)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(cu_db.get_active_unit_by_workspace, return_value=None),
    )

    async def _must_not_append(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("解析失败的组不得发出部分通知")

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _must_not_append)

    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(id="E-20261018-03", knowledge_refs=[_contradicts_ref("P1")]),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert len(effects) == 1  # 一条 gap，不拆分
    gap = effects[0]
    assert gap["status"] == "failed"
    assert gap.get("replayable") is not False
    assert gap["evidence_ids"] == ["E-20261018-02", "E-20261018-03"]
    assert gap["target_workspace_id"] == WS_SECTOR
    assert gap["target_ref"]["entry_id"] == "P1"

    # 持久化拿到台账行（含 payload），再按该行 retry：通知带全部证据。
    # retry 会按 target_ref.unit_id 重新解析当前绑定（数据库已恢复）。
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=_SECTOR_UNIT_ROW),
    )
    insert_spy = create_autospec(cur_db.insert_request, return_value={"request_id": "r1"})
    monkeypatch.setattr(cur_db, "insert_request", insert_spy)
    assert await cross_unit.persist_failed_linkage(USER, gap) is True
    _, ins_kwargs = insert_spy.call_args
    ledger_row = {
        "request_id": "r1",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "status": "gap",
        "target_workspace_id": ins_kwargs["target_workspace_id"],
        "question": ins_kwargs["question"],
        "payload": ins_kwargs["payload"],
    }
    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(
            _REAL_CUR["list_linkage_retry_batch"], return_value=([ledger_row], 1)
        ),
    )
    monkeypatch.setattr(
        cur_db,
        "update_request",
        create_autospec(_REAL_CUR["update_request"], return_value={"status": "fulfilled"}),
    )
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    retried: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        retried.append(kwargs)
        return {"status": "created", "version_no": 5}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    outcome = await cross_unit.retry_linkage_rows(USER, target_workspace_id=WS_SECTOR)
    assert [r["status"] for r in outcome["results"]] == ["created"]
    assert retried[0]["evidence_ids"] == ["E-20261018-02", "E-20261018-03"]
    assert "E-20261018-02" in retried[0]["question"]
    assert "E-20261018-03" in retried[0]["question"]


# ---------------------------------------------------------------------------
# codex r3 #2：游标接入总监工具；#4：记账失败如实暴露
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_retry_threads_cursor_past_blocking_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从工具入口连续两次 retry：第二次带上第一次返回的 next_cursor，
    能处理到被前 20 条无权 gap 挡住的可恢复记录。服务层（retry_linkage_rows
    与真实 apply_update 链路）全程真实，只替换 DB 台账与权限两个 seam。"""
    from src.dalton.database import cross_unit_requests as cur_db

    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    ws_a = "aaaaaaaa-0000-0000-0000-00000000000a"

    def gap_row(rid: str, target: str, day: str) -> dict[str, Any]:
        return {
            "request_id": rid,
            "owner_user_id": USER,
            "kind": "linkage_retry",
            "status": "gap",
            "target_workspace_id": target,
            "question": f"待复核 {rid}",
            "payload": {
                "origin": "sector",
                "source_ref": _knowledge_ref(version_no=5),
                "error": "doc_not_initialized",
            },
            "created_at": day,
        }

    blocked = [
        gap_row(f"0000000{i}-0000-0000-0000-00000000000a", ws_a, f"2026-10-{i + 1:02d}")
        for i in range(20)
    ]
    recoverable = gap_row(
        "99999999-9999-9999-9999-999999999999", WS_COMPANY, "2026-10-31"
    )
    ledger = _BatchLedger([*blocked, recoverable])
    monkeypatch.setattr(cur_db, "list_linkage_retry_batch", _BatchLedger.batch.__get__(ledger))

    # 真实 _append_linkage_question → apply_update（CAS 环境）；install 会设
    # 一个默认放行的权限替身，之后必须用本测试的权限序列覆盖它
    env = _CasAgendaEnv(
        agenda_content={
            "schema_version": 1,
            "as_of": "2026-10-18T12:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"events": []},
            "wake_conditions": [],
        }
    )
    env.install(monkeypatch)

    async def _can_write(uid: str, ws: str) -> bool:
        return ws == WS_COMPANY

    monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _can_write)

    async def _fulfill(request_id: str, owner: str, **kwargs: Any) -> dict[str, Any]:
        for r in ledger.rows:
            if str(r.get("request_id")) == str(request_id):
                r["status"] = "fulfilled"
        return {"request_id": request_id, "status": "fulfilled"}

    monkeypatch.setattr(cur_db, "update_request", _fulfill)

    res1 = json.loads(await tool.ainvoke({"action": "retry", "limit": 20}, config=_CFG))
    assert res1["ok"] is True
    assert len(res1["results"]) == 20
    assert all(r["status"] == "skipped_no_access" for r in res1["results"])
    assert res1["remaining"] == 21  # 全量口径：20 条无权 + 1 条待翻页的可恢复行
    first_cursor = res1["next_cursor"]
    assert first_cursor and len(first_cursor) == 2

    res2 = json.loads(
        await tool.ainvoke(
            {"action": "retry", "limit": 20, "cursor": first_cursor}, config=_CFG
        )
    )
    assert [r["status"] for r in res2["results"]] == ["created"]
    assert res2["results"][0]["request_id"] == recoverable["request_id"]
    assert res2["remaining"] == 0
    assert res2["next_cursor"] is None
    # 真实写入落到了目标 agenda
    assert any("待复核" in q["question"] for q in env.agenda_questions())

    # 游标形状错误被拒
    res3 = json.loads(
        await tool.ainvoke({"action": "retry", "cursor": ["only-one"]}, config=_CFG)
    )
    assert res3["ok"] is False and res3["error"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_retry_reports_bookkeeping_failure_and_true_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r3 #4：补发成功但 update_request 失败 → 结果标"已补发但台账
    更新失败"，remaining 按重查（行仍为 gap）报 1，不误报 0。"""
    from src.dalton.database import cross_unit_requests as cur_db

    gap_row = {
        "request_id": "99999999-9999-9999-9999-999999999999",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "status": "gap",
        "target_workspace_id": WS_COMPANY,
        "question": "待复核：行业支柱 P1 已更新",
        "created_at": "2026-10-01",
        "payload": {
            "origin": "sector",
            "source_ref": _knowledge_ref(version_no=5),
            "error": "doc_not_initialized",
        },
    }
    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(
            _REAL_CUR["list_linkage_retry_batch"], return_value=([gap_row], 1)
        ),
    )

    async def _failing_update(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(cur_db, "update_request", _failing_update)
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        return {"status": "created", "version_no": 4}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    outcome = await cross_unit.retry_linkage_rows(USER, target_workspace_id=WS_COMPANY)
    entry = outcome["results"][0]
    assert entry["status"] == "created"
    assert entry["bookkeeping_failed"] is True
    assert entry["note"] == "已补发但台账更新失败"
    assert outcome["remaining"] == 1  # 台账没更新，行仍是 gap


# ---------------------------------------------------------------------------
# codex r3 #3：准备阶段异常——可重放的记 gap，定不了目标的绝不进台账
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradiction_ref_resolution_failure_records_replayable_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个行业目标：第二个 get_coverage_unit 抛异常但引用带 workspace_id
    → 目标可确定，按完整逐目标上下文记为可重放 gap；第一个照常写入。"""
    from src.dalton.database import cross_unit_requests as cur_db

    sector_1 = dict(_SECTOR_UNIT_ROW)
    sector_2 = {
        "unit_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "kind": "sector",
        "status": "active",
        "workspace_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "display_name": "Digital Ads",
    }
    unit_2 = sector_2["unit_id"]
    ws_2 = sector_2["workspace_id"]

    async def _flaky_get(unit_id: str, **kwargs: Any) -> dict[str, Any]:
        if str(unit_id) == unit_2:
            raise RuntimeError("transient registry failure")
        return sector_1

    monkeypatch.setattr(cu_db, "get_coverage_unit", _flaky_get)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(cu_db.get_active_unit_by_workspace, return_value=None),
    )
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    written: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        written.append(kwargs)
        return {"status": "created", "version_no": 3}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)

    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(
            id="E-20261018-02",
            knowledge_refs=[
                _knowledge_ref(
                    unit_id=unit_2, workspace_id=ws_2, entry_id="P1", relation="contradicts"
                )
            ],
        ),
        _evidence(
            id="E-20261018-03",
            knowledge_refs=[_contradicts_ref("P2", version_no=3)],
        ),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    by_target = {e.get("target_workspace_id"): e for e in effects}
    # 第一个行业照常写入
    assert by_target[WS_SECTOR]["status"] == "created"
    assert len(written) == 1
    # 第二个行业：解析失败但目标可确定 → 可重放 gap（完整上下文）
    failed = by_target[ws_2]
    assert failed["status"] == "failed"
    assert failed.get("replayable") is not False
    assert failed["target_workspace_id"] == ws_2
    assert failed["question"].startswith("更新候选")
    assert "准备阶段解析失败" in failed["question"]
    assert failed["source_ref"]["entry_id"] == "E-20261018-02"
    assert failed["target_ref"]["workspace_id"] == ws_2
    assert failed["target_ref"]["entry_id"] == "P1"

    # 该 effect 可被持久化为 gap（重放上下文完整）
    insert_spy = create_autospec(cur_db.insert_request, return_value={"request_id": "r1"})
    monkeypatch.setattr(cur_db, "insert_request", insert_spy)
    assert await cross_unit.persist_failed_linkage(USER, failed) is True
    _, ins_kwargs = insert_spy.call_args
    assert ins_kwargs["kind"] == "linkage_retry"
    assert ins_kwargs["target_workspace_id"] == ws_2
    assert ins_kwargs["question"].startswith("更新候选")


@pytest.mark.asyncio
async def test_preparation_failure_without_target_not_recorded_as_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只带 unit_id 的引用解析失败（目标无法确定）→ 不进台账；服务层
    message 如实写"联动准备失败…请重新保存该文档以再次触发"。"""
    import src.dalton.database.cross_unit_requests as cur_db
    import src.dalton.knowledge.service as svc_mod

    unit_only = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    async def _raise_get(unit_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("registry down")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _raise_get)
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(
            id="E-20261018-02",
            knowledge_refs=[
                {"unit_id": unit_only, "doc_key": "thesis", "version_no": 3,
                 "entry_id": "P1", "relation": "contradicts"}
            ],
        )
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert len(effects) == 1
    assert effects[0]["kind"] == "preparation_error"
    assert effects[0]["replayable"] is False
    # 不可重放：persist 拒绝写台账
    insert_spy = create_autospec(cur_db.insert_request, return_value={"request_id": "r1"})
    monkeypatch.setattr(cur_db, "insert_request", insert_spy)
    assert await cross_unit.persist_failed_linkage(USER, effects[0]) is False
    insert_spy.assert_not_called()

    # 服务层 message：如实写准备失败、不承诺 gap/retry。走真实的不可重放
    # 路径——行业侧引用扫描整体失败（find_thesis_refs_to_subject 抛异常，
    # 连目标集合都拿不到）。
    async def _raise_scan(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError("scan down")

    monkeypatch.setattr(cross_unit, "find_thesis_refs_to_subject", _raise_scan)
    # propagate_thesis_change 入口处还按 settings 复查开关（防未来调用方绕过）
    import src.dalton.settings as settings_mod

    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            cu_db.get_active_unit_by_workspace,
            return_value=dict(_SECTOR_UNIT_ROW, workspace_id=WS_SECTOR),
        ),
    )

    fx = _KVStore()
    fx.store["thesis"] = [
        {
            "knowledge_version_id": "kv-t1",
            "workspace_id": WS_SECTOR,
            "doc_key": "thesis",
            "version_no": 1,
            "content": _thesis_doc([_pillar()], []),
            "content_sha256": "x",
            "source": "tool",
            "schema_valid": True,
            "files_written_at": None,
            "created_at": None,
        }
    ]
    monkeypatch.setattr(svc_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(svc_mod, "verify_workspace_access", _allow_access)

    async def fake_profile_sector(ws: Any, conn: Any = None, **kwargs: Any) -> str:
        return "sector"

    monkeypatch.setattr(svc_mod, "resolve_workspace_profile", fake_profile_sector)

    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        return await fx.latest(ws, dk)

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )
    monkeypatch.setattr(
        db_knowledge, "mark_files_written", create_autospec(db_knowledge.mark_files_written)
    )
    monkeypatch.setattr(
        db_knowledge,
        "insert_version",
        create_autospec(
            db_knowledge.insert_version,
            return_value={
                "knowledge_version_id": "kv-t2",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 2,
                "content": _thesis_doc([_pillar(claim="changed")], []),
                "schema_valid": True,
            },
        ),
    )
    res = await svc_mod.apply_update(
        user_id=USER,
        workspace_id=WS_SECTOR,
        doc_key="thesis",
        ops=[{"op": "set", "path": "/pillars/P1/claim", "value": "changed"}],
        reason="行业观点更新",
        base_version=1,
        source="tool",
        backend=None,
    )
    assert res["ok"] is True
    assert "联动准备失败 1 条" in res["message"]
    assert "请人工跟进：检查相关行业 / 公司 agenda 是否需要补记" in res["message"]
    # codex r4 #3：不得再承诺"重新保存可再次触发"（增量检测对原样重存判无变化）
    assert "重新保存" not in res["message"]
    assert "已记为 gap" not in res["message"]
    insert_spy.assert_not_called()


# ---------------------------------------------------------------------------
# 看板 units：三态、成员、workspace_created；开关关闭时零新增查询
# ---------------------------------------------------------------------------


class _BCursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


def _board_cursor(
    ws_rows: list[dict[str, Any]],
    kv_rows: list[dict[str, Any]],
    auto_rows: list[dict[str, Any]] | None = None,
    unit_rows: list[dict[str, Any]] | None = None,
    member_rows: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[str]]:
    """SQL 求值替身：按表名与条件返回对应行，并记录全部执行过的 SQL。"""
    executed: list[str] = []
    cur = create_autospec(_BCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    last: dict[str, Any] = {"sql": "", "params": None, "rows": []}

    async def _exec(sql: str, params: Any = None) -> None:
        compact = " ".join(str(sql).split())
        executed.append(compact)
        last["sql"] = compact
        last["params"] = params

    cur.execute.side_effect = _exec

    def _fetchall() -> list[dict[str, Any]]:
        sql = last["sql"]
        if "FROM workspaces" in sql:
            return ws_rows
        if "dalton_knowledge_versions" in sql:
            return kv_rows
        if "FROM automations" in sql:
            p = list(last["params"] or [])
            rows = list(auto_rows or [])
            if p and "user_id = %s" in sql:
                rows = [r for r in rows if str(r.get("user_id")) == str(p[0])]
            return rows
        if "dalton_sector_members" in sql:
            p = list(last["params"] or [])
            ids = {str(u) for u in (p[0] if p else [])}
            return [r for r in (member_rows or []) if str(r.get("sector_unit_id")) in ids]
        if "FROM dalton_coverage_units" in sql:
            p = list(last["params"] or [])
            rows = list(unit_rows or [])
            if "unit_id = ANY" in sql:
                # 成员公司单元反查（任意状态）
                ids = {str(u) for u in (p[0] if p else [])}
                return [r for r in rows if str(r.get("unit_id")) in ids]
            if "workspace_id = ANY" in sql:
                # 同事可见工作区上的单元，排除本人
                ws_ids = {str(u) for u in (p[0] if p else [])}
                rows = [r for r in rows if str(r.get("workspace_id")) in ws_ids]
                if len(p) >= 3 and p[2] is not None:
                    rows = [r for r in rows if str(r.get("owner_user_id")) != str(p[2])]
                return rows
            # 本人（或 user_id=None 时全部）active 单元清单
            if len(p) >= 2 and p[1] is not None:
                owner = str(p[1])
                rows = [r for r in rows if str(r.get("owner_user_id")) == owner]
            return rows
        return []

    cur.fetchall.side_effect = _fetchall
    cur.fetchone.return_value = {"cnt": 0}
    return cur, executed


class _Lease:
    """一次连接租借：退出上下文即归还，之后再用 cursor 就报错（codex r1 #2
    的替身要求——看板若在归还后的连接上查询，测试必须抓得住）。"""

    def __init__(self, cur: Any) -> None:
        self._cur = cur
        self._returned = False

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self._returned:
            raise RuntimeError("connection already returned to the pool")
        return self._cur

    def transaction(self, *args: Any) -> "_Lease":
        return self

    async def __aenter__(self) -> "_Lease":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._returned = True


def _install_board(monkeypatch: pytest.MonkeyPatch, cur: Any) -> None:
    """每次 get_db_connection() 租出一条新连接（同一 cursor 数据面）。"""
    import src.dalton.autonomy.board as board_mod

    monkeypatch.setattr(board_mod, "get_budget_today", lambda **kw: {"cap_usd": 10.0, "spent_usd": 1.0})
    monkeypatch.setattr(board_mod, "get_user_coverage", lambda uid: {})
    monkeypatch.setattr(board_mod, "get_db_connection", lambda: _Lease(cur))


_U1 = USER


def _ws_row(ws_id: str, name: str) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "user_id": _U1,
        "name": name,
        "description": "",
        "status": "active",
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_board_units_view_three_states_and_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.autonomy.board as board_mod

    monkeypatch.setattr(board_mod, "is_coverage_units_enabled", lambda: True)
    unit_rows = [
        {
            "unit_id": UNIT_SECTOR,
            "kind": "sector",
            "owner_user_id": _U1,
            "slug": "ai-infrastructure",
            "display_name": "AI Infrastructure",
            "workspace_id": WS_SECTOR,
            "ticker": None,
            "status": "active",
        },
        {
            "unit_id": UNIT_COMPANY,
            "kind": "company",
            "owner_user_id": _U1,
            "slug": "example",
            "display_name": "EXAMPLE",
            "workspace_id": WS_COMPANY,
            "ticker": "EXAMPLE",
            "status": "active",
        },
        {
            "unit_id": "66666666-6666-6666-6666-666666666666",
            "kind": "company",
            "owner_user_id": _U1,
            "slug": "unbound",
            "display_name": "Unbound",
            "workspace_id": None,
            "ticker": "UNBD",
            "status": "active",
        },
    ]
    # 身份解析走批量单元表（与生产一致的 seam），不靠替身的巧合
    monkeypatch.setattr(
        board_mod,
        "load_active_units_by_workspace",
        create_autospec(
            board_mod.load_active_units_by_workspace,
            return_value={u["workspace_id"]: dict(u) for u in unit_rows if u["workspace_id"]},
        ),
    )
    member_rows = [
        {
            "sector_unit_id": UNIT_SECTOR,
            "member_id": "M1",
            "member_name": "某综合科技公司",
            "ticker": "EXAMPLE",
            "company_unit_id": UNIT_COMPANY,
            "workspace_created": True,
            "scope": "只计 AI 加速卡收入",
            "reason": "",
            "valid_until": None,
            "position": 0,
            "synced_version_no": 3,
        },
        {
            "sector_unit_id": UNIT_SECTOR,
            "member_id": "M2",
            "member_name": "未覆盖的封装厂商",
            "ticker": None,
            "company_unit_id": None,
            "workspace_created": False,
            "scope": "",
            "reason": "",
            "valid_until": None,
            "position": 1,
            "synced_version_no": 3,
        },
    ]
    auto_rows = [
        {"user_id": _U1, "workspace_id": WS_COMPANY, "name": "dalton-example-watch", "metadata": {}}
    ]
    cur, executed = _board_cursor(
        [_ws_row(WS_SECTOR, "AI Infrastructure"), _ws_row(WS_COMPANY, "EXAMPLE")],
        [],
        auto_rows,
        unit_rows,
        member_rows,
    )
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=_U1)
    units = {u["unit_id"]: u for u in board["units"]}
    # codex r1 #1：参数出现在 IS NULL 这种无类型上下文里必须显式转型，
    # 否则生产 psycopg 发 unknown 类型、Postgres 推断不出参数类型
    unit_sqls = [s for s in executed if "FROM dalton_coverage_units" in s or "dalton_sector_members" in s]
    assert any("%s::text IS NULL" in s for s in unit_sqls)
    for s in unit_sqls:
        assert "IS NULL OR owner" not in s.replace("%s::text IS NULL OR owner", ""), s
        assert "%s IS NULL" not in s, f"untyped NULL param: {s}"
    # 行业单元：本人 → 可读可委派；成员带 workspace_created 与解析出的工作区
    sector = units[UNIT_SECTOR]
    assert sector["kind"] == "sector"
    assert sector["readable"] is True and sector["delegatable"] is True
    assert sector["auto_tracked"] is False  # 行业工作区没有本人的 dalton-* 自动化
    m1, m2 = sector["members"]
    assert m1["workspace_created"] is True
    assert m1["workspace_id"] == WS_COMPANY
    assert m1["scope"] == "只计 AI 加速卡收入"
    assert m2["workspace_created"] is False
    assert m2["workspace_id"] is None
    # 公司单元：有自动化 → 纳入本人自动调度
    company = units[UNIT_COMPANY]
    assert company["ticker"] == "EXAMPLE"
    assert company["readable"] is True and company["delegatable"] is True
    assert company["auto_tracked"] is True
    # 未绑定工作区的单元：可读、不可委派（没有可派单对象）
    unbound = units["66666666-6666-6666-6666-666666666666"]
    assert unbound["delegatable"] is False and unbound["auto_tracked"] is False
    # companies 字段照旧：公司单元的注册 ticker 照常出现在公司看板
    assert [c["ticker"] for c in board["companies"]] == ["EXAMPLE"]


@pytest.mark.asyncio
async def test_board_units_colleague_unit_readable_not_delegatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.autonomy.board as board_mod
    import src.dalton.sharing as sharing_mod

    monkeypatch.setattr(board_mod, "is_coverage_units_enabled", lambda: True)
    colleague_ws = "77777777-7777-7777-7777-777777777777"

    async def _fake_team(uid: str) -> list[dict[str, Any]]:
        return [
            {
                "user_id": "user-2",
                "workspaces": [
                    {
                        "workspace_id": colleague_ws,
                        "web_access": "ro",
                        "cos_access": "ro",
                    }
                ],
            }
        ]

    monkeypatch.setattr(sharing_mod, "list_team_workspaces", _fake_team)
    unit_rows = [
        {
            "unit_id": "88888888-8888-8888-8888-888888888888",
            "kind": "sector",
            "owner_user_id": "user-2",
            "slug": "digital-ads",
            "display_name": "Digital Ads",
            "workspace_id": colleague_ws,
            "ticker": None,
            "status": "active",
        }
    ]
    cur, _ = _board_cursor([], [], None, unit_rows, [])
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=_U1)
    units = board["units"]
    assert len(units) == 1
    # 可见（资料来源）但不可委派、不纳入本人调度
    assert units[0]["readable"] is True
    assert units[0]["delegatable"] is False
    assert units[0]["auto_tracked"] is False


@pytest.mark.asyncio
async def test_board_member_workspace_id_respects_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #5：成员解析不得绕过隐藏公司工作区的可见性过滤——
    无权读取时只给 workspace_created 与名称，不给 workspace_id。"""
    import src.dalton.autonomy.board as board_mod
    import src.dalton.sharing as sharing_mod

    monkeypatch.setattr(board_mod, "is_coverage_units_enabled", lambda: True)
    colleague_sector_ws = "77777777-7777-7777-7777-777777777777"
    hidden_company_ws = "88888888-8888-8888-8888-888888888888"  # 属 user-3，未共享

    async def _fake_team(uid: str) -> list[dict[str, Any]]:
        # 只看到 user-2 的行业工作区；user-3 的公司工作区对查看者隐藏
        return [
            {
                "user_id": "user-2",
                "workspaces": [{"workspace_id": colleague_sector_ws, "web_access": "ro", "cos_access": "ro"}],
            }
        ]

    monkeypatch.setattr(sharing_mod, "list_team_workspaces", _fake_team)
    hidden_company_unit = "99999999-9999-9999-9999-999999999999"
    unit_rows = [
        {
            "unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "kind": "sector",
            "owner_user_id": "user-2",
            "slug": "digital-ads",
            "display_name": "Digital Ads",
            "workspace_id": colleague_sector_ws,
            "ticker": None,
            "status": "active",
        },
        # 查看者自己的公司单元（成员可解析出工作区）
        {
            "unit_id": UNIT_COMPANY,
            "kind": "company",
            "owner_user_id": _U1,
            "slug": "example",
            "display_name": "EXAMPLE",
            "workspace_id": WS_COMPANY,
            "ticker": "EXAMPLE",
            "status": "active",
        },
        # 隐藏的公司单元：成员解析不得到它的 workspace_id
        {
            "unit_id": hidden_company_unit,
            "kind": "company",
            "owner_user_id": "user-3",
            "slug": "hidden",
            "display_name": "Hidden Co",
            "workspace_id": hidden_company_ws,
            "ticker": "HIDD",
            "status": "active",
        },
    ]
    member_rows = [
        {
            "sector_unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "member_id": "M1",
            "member_name": "某综合科技公司",
            "ticker": "EXAMPLE",
            "company_unit_id": UNIT_COMPANY,
            "workspace_created": True,
            "scope": "",
            "reason": "",
            "valid_until": None,
            "position": 0,
            "synced_version_no": 2,
        },
        {
            "sector_unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "member_id": "M2",
            "member_name": "未共享的公司",
            "ticker": "HIDD",
            "company_unit_id": hidden_company_unit,
            "workspace_created": True,
            "scope": "",
            "reason": "",
            "valid_until": None,
            "position": 1,
            "synced_version_no": 2,
        },
    ]
    cur, executed = _board_cursor([], [], None, unit_rows, member_rows)
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=_U1)
    sector = next(u for u in board["units"] if u["kind"] == "sector")
    members = {m["member_id"]: m for m in sector["members"]}
    # 自己的公司：id 可见
    assert members["M1"]["workspace_id"] == WS_COMPANY
    # 隐藏的公司：workspace_created 照实输出，但 id 不泄露
    assert members["M2"]["workspace_created"] is True
    assert members["M2"]["name"] == "未共享的公司"
    assert members["M2"]["workspace_id"] is None


@pytest.mark.asyncio
async def test_board_flag_off_has_no_units_and_no_new_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.autonomy.board as board_mod
    from src.dalton.database import coverage_units as cu_db_mod

    monkeypatch.setattr(board_mod, "is_coverage_units_enabled", lambda: False)
    mock_load = create_autospec(cu_db_mod.load_active_units_by_workspace, return_value={})
    monkeypatch.setattr(board_mod, "load_active_units_by_workspace", mock_load)

    cur, executed = _board_cursor(
        [_ws_row(WS_COMPANY, "EXAMPLE")],
        [
            {
                "workspace_id": WS_COMPANY,
                "doc_key": "company",
                "version_no": 1,
                "content": {"ticker": "EXAMPLE"},
                "created_at": None,
            }
        ],
    )
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=_U1)
    # 开关关闭：units 键缺席；companies 照旧
    assert "units" not in board
    assert [c["ticker"] for c in board["companies"]] == ["EXAMPLE"]
    mock_load.assert_not_called()
    # 一次都没查新表
    assert not any("dalton_sector_members" in s for s in executed)
    assert not any("dalton_coverage_units" in s for s in executed)


@pytest.mark.asyncio
async def test_verify_projection_reports_drift_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify 只读：文档与投影不一致时报告漂移侧，不做任何写。"""
    docs = [
        {
            "unit_id": UNIT_SECTOR,
            "workspace_id": WS_SECTOR,
            "version_no": 3,
            "content": {
                "members": [
                    {"id": "M1", "name": "A", "workspace_created": False},
                    {"id": "M2", "name": "B", "workspace_created": False},
                ]
            },
        }
    ]

    async def _fake_docs() -> list[dict[str, Any]]:
        return docs

    monkeypatch.setattr(sm_db, "_sector_docs_with_units", _fake_docs)

    # verify 的读循环走连接（游标只被已替身的 fetch 使用）
    cur = _spec_cursor()

    class _VConn:
        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return cur

        async def __aenter__(self) -> "_VConn":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(
        sm_db,
        "get_db_connection",
        lambda conn=None: _CtxOf(_VConn()),
    )

    async def _fake_fetch(cur: Any, ids: list[str]) -> list[dict[str, Any]]:
        # M1 内容一致；M2 文档有而投影没有；M3 投影有而文档没有
        return [
            {
                "sector_unit_id": UNIT_SECTOR,
                "member_id": "M1",
                "member_name": "A",
                "ticker": None,
                "company_unit_id": None,
                "workspace_created": False,
                "scope": "",
                "reason": "",
                "valid_until": None,
                "position": 0,
                "synced_version_no": 3,
            },
            {
                "sector_unit_id": UNIT_SECTOR,
                "member_id": "M3",
                "member_name": "C",
                "ticker": None,
                "company_unit_id": None,
                "workspace_created": False,
                "scope": "",
                "reason": "",
                "valid_until": None,
                "position": 1,
                "synced_version_no": 2,
            },
        ]

    monkeypatch.setattr(sm_db, "fetch_members_for_sectors", _fake_fetch)

    report = await sm_db.verify_sector_members_projection()
    assert report["checked_units"] == 1
    drifts = {(d["member_id"], d["side"]) for d in report["drifts"]}
    assert drifts == {("M2", "document_only"), ("M3", "projection_only")}
    assert report["ok"] is False

    async def _matching_fetch(cur: Any, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "sector_unit_id": UNIT_SECTOR,
                "member_id": mid,
                "member_name": name,
                "ticker": None,
                "company_unit_id": None,
                "workspace_created": False,
                "scope": "",
                "reason": "",
                "valid_until": None,
                "position": pos,
                "synced_version_no": 3,
            }
            for pos, (mid, name) in enumerate((("M1", "A"), ("M2", "B")))
        ]

    monkeypatch.setattr(sm_db, "fetch_members_for_sectors", _matching_fetch)
    ok_report = await sm_db.verify_sector_members_projection()
    assert ok_report["ok"] is True and ok_report["drifts"] == []


# ---------------------------------------------------------------------------
# codex r3 #1：最新版本尚未落盘时，对账不得把旧文件当直接编辑
# ---------------------------------------------------------------------------


async def _run_reconcile_with_pending_latest(
    monkeypatch: pytest.MonkeyPatch, *, flag_on: bool, baseline_written: bool = True
) -> dict[str, Any]:
    """真实 detect_direct_edits：v1 已落盘、v2（files_written_at=None）待落盘，
    文件镜像仍等于 v1。返回对账结果；store 供断言。"""
    import hashlib as _hashlib

    import src.dalton.knowledge.reconcile as rec
    from src.dalton.knowledge.service import canonical_json

    async def fake_profile(ws: Any, conn: Any = None, **kwargs: Any) -> str:
        return "sector"

    monkeypatch.setattr(rec, "resolve_workspace_profile", fake_profile)
    monkeypatch.setattr(
        rec, "is_coverage_units_enabled", lambda: flag_on
    )

    v1_doc = _thesis_doc([_pillar()], [])
    v1_text, v1_sha = canonical_json(v1_doc)
    v2_doc = _thesis_doc([_pillar(claim="待落盘的新支柱")], [])
    _, v2_sha = canonical_json(v2_doc)

    store: dict[str, list[dict[str, Any]]] = {
        "thesis": [
            {
                "knowledge_version_id": "kv-t1",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 1,
                "content": v1_doc,
                "content_sha256": v1_sha,
                "source": "tool",
                "schema_valid": True,
                "files_written_at": (
                    "2026-10-18T11:00:00+00:00" if baseline_written else None
                ),
                "created_at": "2026-10-18T11:00:00+00:00",
            },
            {
                # 联动/总监 retry 以 backend=None 写入的版本：尚未落盘
                "knowledge_version_id": "kv-t2",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 2,
                "content": v2_doc,
                "content_sha256": v2_sha,
                "source": "tool",
                "schema_valid": True,
                "files_written_at": None,
                "created_at": "2026-10-18T12:00:00+00:00",
            },
        ]
    }

    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        rows = store.get(dk, [])
        return rows[-1] if rows else None

    async def fake_list_all(ws: str, valid_only: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dk, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[dk] = filtered[-1]
        return out

    async def fake_insert(**kw: Any) -> dict[str, Any]:
        rows = store.setdefault(kw["doc_key"], [])
        row = {
            "knowledge_version_id": f"kv-{kw['doc_key']}-{len(rows) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": len(rows) + 1,
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "source": kw.get("source"),
            "schema_valid": kw.get("schema_valid", True),
            "edited_path": kw.get("edited_path"),
            "files_written_at": None,
            "created_at": None,
        }
        rows.append(row)
        return row

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all)
    monkeypatch.setattr(db_knowledge, "insert_version", fake_insert)
    monkeypatch.setattr(
        db_knowledge,
        "latest_written_valid_version",
        create_autospec(db_knowledge.latest_written_valid_version, return_value=None),
    )

    async def fake_latest_written(ws: str, dk: str) -> dict[str, Any] | None:
        # 与生产查询同义：files_written_at 非空的最高版本号（不筛 valid）
        rows = [
            r
            for r in store.get(dk, [])
            if r.get("files_written_at") is not None
        ]
        return rows[-1] if rows else None

    monkeypatch.setattr(
        db_knowledge,
        "latest_written_version",
        create_autospec(db_knowledge.latest_written_version, side_effect=fake_latest_written),
    )
    monkeypatch.setattr(
        db_knowledge, "mark_files_written", create_autospec(db_knowledge.mark_files_written)
    )
    from src.dalton.database import publications as db_publications

    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(db_publications.list_publication_files, return_value=[]),
    )

    async def fake_meta(ws: str) -> dict[str, Any]:
        # 文件镜像仍是 v1（回合进行中，v2 还没 apply_pending）
        return {
            "data/thesis.json": {
                "kind": "file",
                "content_hash": _hashlib.sha256(v1_text.encode("utf-8")).hexdigest(),
            }
        }

    monkeypatch.setattr(rec, "get_file_metadata_for_sync", fake_meta)

    async def fake_get_file(ws: str, path: str, include_content: bool = False) -> dict[str, Any]:
        return {"path": path}

    monkeypatch.setattr(rec, "db_get_file", fake_get_file)

    async def fake_resolve_text(file_row: Any, *, user_id: str, context: str) -> str | None:
        return v1_text if file_row and file_row["path"] == "data/thesis.json" else None

    monkeypatch.setattr(rec, "resolve_file_text_or_none", fake_resolve_text)

    result = await rec.detect_direct_edits(
        WS_SECTOR, USER, storage_owner_id=USER, skip_access_check=True
    )
    result["_store"] = store
    return result


@pytest.mark.asyncio
async def test_reconcile_skips_direct_edit_while_latest_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关打开：最新版本 files_written_at=None → 本轮跳过直接编辑检测，
    v2 不会被以 v2 为基线的"直接编辑"覆盖（codex r3 #1）。"""
    result = await _run_reconcile_with_pending_latest(monkeypatch, flag_on=True)
    assert result["recorded_versions"] == []
    # v2 完好保留为最新版本
    thesis_rows = result["_store"]["thesis"]
    assert len(thesis_rows) == 2
    assert thesis_rows[-1]["version_no"] == 2
    assert thesis_rows[-1]["content"]["pillars"][0]["claim"] == "待落盘的新支柱"


@pytest.mark.asyncio
async def test_reconcile_pending_without_written_baseline_uses_original_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关打开但不存在任何已落盘版本 → 没有基线可比，按原逻辑检测
    （codex r4 #1 的"没有已落盘的版本时按原逻辑处理"分支）。"""
    result = await _run_reconcile_with_pending_latest(
        monkeypatch, flag_on=True, baseline_written=False
    )
    recorded = result["recorded_versions"]
    assert len(recorded) == 1
    assert recorded[0]["source"] == "direct_edit"
    assert recorded[0]["edited_path"] == "data/thesis.json"


@pytest.mark.asyncio
async def test_reconcile_pending_gate_inert_with_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：同样状态走原有路径——旧文件被记为 direct_edit 版本
    （与改动前行为一致；撤掉开关门控、让跳过无条件生效即失败）。"""
    result = await _run_reconcile_with_pending_latest(monkeypatch, flag_on=False)
    recorded = result["recorded_versions"]
    assert len(recorded) == 1
    assert recorded[0]["source"] == "direct_edit"
    assert recorded[0]["edited_path"] == "data/thesis.json"
    assert recorded[0]["content"]["pillars"][0]["claim"] == "行业资本开支保持高增"


# ---------------------------------------------------------------------------
# reconcile：直接编辑产生的有效 thesis 版本也触发联动（codex r1 #6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_valid_thesis_direct_edit_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib as _hashlib

    import src.dalton.knowledge.reconcile as rec
    from src.dalton.knowledge.service import canonical_json

    async def fake_profile(ws: Any, conn: Any = None, **kwargs: Any) -> str:
        return "sector"

    monkeypatch.setattr(rec, "resolve_workspace_profile", fake_profile)
    monkeypatch.setattr(rec, "is_coverage_units_enabled", lambda: True)

    old_doc = _thesis_doc([_pillar()], [])
    old_text, old_sha = canonical_json(old_doc)
    edited_doc = _thesis_doc([_pillar(claim="直接改过的支柱论断")], [])
    edited_text, edited_sha = canonical_json(edited_doc)

    store: dict[str, list[dict[str, Any]]] = {
        "thesis": [
            {
                "knowledge_version_id": "kv-t1",
                "workspace_id": WS_SECTOR,
                "doc_key": "thesis",
                "version_no": 1,
                "content": old_doc,
                "content_sha256": old_sha,
                "source": "tool",
                "schema_valid": True,
                "files_written_at": "2026-10-18T11:00:00+00:00",
                "created_at": "2026-10-18T11:00:00+00:00",
            }
        ]
    }

    async def fake_latest(ws: str, dk: str) -> dict[str, Any] | None:
        rows = store.get(dk, [])
        return rows[-1] if rows else None

    async def fake_list_all(ws: str, valid_only: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dk, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[dk] = filtered[-1]
        return out

    async def fake_insert(**kw: Any) -> dict[str, Any]:
        rows = store.setdefault(kw["doc_key"], [])
        row = {
            "knowledge_version_id": f"kv-{kw['doc_key']}-{len(rows) + 1}",
            "workspace_id": kw["workspace_id"],
            "doc_key": kw["doc_key"],
            "version_no": len(rows) + 1,
            "content": copy.deepcopy(kw["content"]),
            "content_sha256": kw["content_sha256"],
            "source": kw.get("source"),
            "schema_valid": kw.get("schema_valid", True),
            "files_written_at": None,
            "created_at": None,
        }
        rows.append(row)
        return row

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all)
    monkeypatch.setattr(db_knowledge, "insert_version", fake_insert)
    monkeypatch.setattr(
        db_knowledge, "mark_files_written", create_autospec(db_knowledge.mark_files_written)
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_written_valid_version",
        create_autospec(db_knowledge.latest_written_valid_version, return_value=None),
    )
    from src.dalton.database import publications as db_publications

    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(db_publications.list_publication_files, return_value=[]),
    )

    async def fake_meta(ws: str) -> dict[str, Any]:
        return {
            "data/thesis.json": {
                "kind": "file",
                "content_hash": _hashlib.sha256(edited_text.encode("utf-8")).hexdigest(),
            }
        }

    monkeypatch.setattr(rec, "get_file_metadata_for_sync", fake_meta)

    async def fake_get_file(ws: str, path: str, include_content: bool = False) -> dict[str, Any]:
        return {"path": path}

    monkeypatch.setattr(rec, "db_get_file", fake_get_file)

    async def fake_resolve_text(file_row: Any, *, user_id: str, context: str) -> str | None:
        return edited_text if file_row and file_row["path"] == "data/thesis.json" else None

    monkeypatch.setattr(rec, "resolve_file_text_or_none", fake_resolve_text)

    prop_calls: list[dict[str, Any]] = []

    async def _spy_propagate(**kwargs: Any) -> list[dict[str, Any]]:
        prop_calls.append(kwargs)
        return []

    monkeypatch.setattr(cross_unit, "propagate_thesis_change", _spy_propagate)

    result = await rec.detect_direct_edits(
        WS_SECTOR, USER, storage_owner_id=USER, skip_access_check=True
    )
    assert result["recorded_versions"], "直接编辑应被记录为版本"
    assert len(prop_calls) == 1
    kwargs = prop_calls[0]
    assert kwargs["old_doc"]["pillars"][0]["claim"] == "行业资本开支保持高增"
    assert kwargs["new_doc"]["pillars"][0]["claim"] == "直接改过的支柱论断"
    assert kwargs["new_version_no"] == 2
    assert kwargs["profile"] == "sector"

    # 开关关闭：一次都不调用
    prop_calls.clear()
    monkeypatch.setattr(rec, "is_coverage_units_enabled", lambda: False)
    store["thesis"] = store["thesis"][:1]  # 重置到 v1，重新触发一次直接编辑记录
    await rec.detect_direct_edits(
        WS_SECTOR, USER, storage_owner_id=USER, skip_access_check=True
    )
    assert not prop_calls


# ---------------------------------------------------------------------------
# rebuild：与 insert_version 同锁、锁内读最新文档（codex r1 #8）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_takes_insert_version_lock_and_reads_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, tuple[Any, ...]]] = []
    doc = {"members": [{"id": "M1", "name": "A", "workspace_created": False}]}

    cur = _spec_cursor()

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec

    async def _fetchone() -> dict[str, Any]:
        # 最近一条 SQL 决定返回：锁→无、文档→v3 内容
        sql = executed[-1][0]
        if "dalton_knowledge_versions" in sql:
            return {"version_no": 3, "content": doc}
        return {"unit_id": UNIT_SECTOR, "owner_user_id": USER, "slug": "ai"}

    cur.fetchone.side_effect = _fetchone

    async def _fetchall() -> list[dict[str, Any]]:
        sql = executed[-1][0]
        if "status = 'active' AND kind = 'sector'" in sql:
            return [{"unit_id": UNIT_SECTOR, "workspace_id": WS_SECTOR}]
        return []

    cur.fetchall.side_effect = _fetchall

    class _Tx:
        async def __aenter__(self) -> "_Tx":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Conn:
        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return cur

        def transaction(self) -> "_Tx":
            return _Tx()

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(
        sm_db,
        "get_db_connection",
        create_autospec(sm_db.get_db_connection, return_value=_Conn()),
    )

    out = await sm_db.rebuild_sector_members_projection()
    assert out["rebuilt_units"] == 1
    sqls = [s for s, _ in executed]
    # 同一把 advisory lock（与 insert_version 的 dalton:kv:<ws>:sector 一致）
    lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
    assert executed[lock_idx][1] == (f"dalton:kv:{WS_SECTOR}:sector",)
    # 锁内先读最新有效文档，再重写投影
    doc_idx = next(i for i, s in enumerate(sqls) if "dalton_knowledge_versions" in s)
    delete_idx = next(i for i, s in enumerate(sqls) if s.startswith("DELETE FROM dalton_sector_members"))
    assert lock_idx < doc_idx < delete_idx
    # 投影行带 v3
    insert_params = [p for s, p in executed if s.startswith("INSERT INTO dalton_sector_members")]
    assert insert_params and insert_params[0][-1] == 3


# ---------------------------------------------------------------------------
# dalton_cross_unit_request 工具
# ---------------------------------------------------------------------------


def _director_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # 跨层请求工具只在覆盖单元开关开启时挂载（公司路径不多一个入口）
    monkeypatch.setattr(dt_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        dt_mod, "verify_workspace_access", create_autospec(dt_mod.verify_workspace_access)
    )
    tools = dt_mod.build_director_tools(role="chief_of_staff")
    return {t.name: t for t in tools}


_CFG = {"configurable": {"user_id": USER, "thread_id": "t-1"}}


def test_cross_unit_request_tool_only_mounts_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dt_mod, "is_coverage_units_enabled", lambda: False)
    names_off = [t.name for t in dt_mod.build_director_tools(role="chief_of_staff")]
    assert "dalton_cross_unit_request" not in names_off
    monkeypatch.setattr(dt_mod, "is_coverage_units_enabled", lambda: True)
    names_on = [t.name for t in dt_mod.build_director_tools(role="chief_of_staff")]
    # 挂载位置紧跟看板与计划记录；旧工具一个不少
    assert names_on[2] == "dalton_cross_unit_request"
    assert set(names_off) < set(names_on)
    # 分析师角色两种开关下都不挂载
    assert all(
        "dalton_cross_unit_request" not in [t.name for t in dt_mod.build_director_tools(role="analyst")]
        for _ in (0,)
    )


@pytest.mark.asyncio
async def test_cross_unit_request_tool_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.database.cross_unit_requests as cur_db

    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    assert "dalton_cross_unit_request" in tools

    created_row = {
        "request_id": "99999999-9999-9999-9999-999999999999",
        "owner_user_id": USER,
        "status": "open",
        "question": "取证：某综合科技公司 AI 加速卡出货节奏",
    }
    mock_insert = create_autospec(cur_db.insert_request, return_value=created_row)
    monkeypatch.setattr(cur_db, "insert_request", mock_insert)

    res = json.loads(
        await tool.ainvoke(
            {
                "action": "create",
                "question": "取证：某综合科技公司 AI 加速卡出货节奏",
                "sector_workspace_id": WS_SECTOR,
                "evidence_needed": ["季度出货量", "客户结构"],
                "completion_condition": "拿到两季度出货数据并入库",
            },
            config=_CFG,
        )
    )
    assert res["ok"] is True
    _, kwargs = mock_insert.call_args
    assert kwargs["owner_user_id"] == USER
    assert kwargs["evidence_needed"] == [{"what": "季度出货量"}, {"what": "客户结构"}]

    # dispatched 必须带线程 id
    monkeypatch.setattr(
        cur_db,
        "get_request",
        create_autospec(cur_db.get_request, return_value={**created_row, "dispatch_thread_id": None, "result": []}),
    )
    res2 = json.loads(
        await tool.ainvoke(
            {"action": "update", "request_id": created_row["request_id"], "status": "dispatched"},
            config=_CFG,
        )
    )
    assert res2["ok"] is False and res2["error"] == "missing_dispatch_thread"

    # fulfilled 必须带 result
    res3 = json.loads(
        await tool.ainvoke(
            {"action": "update", "request_id": created_row["request_id"], "status": "fulfilled"},
            config=_CFG,
        )
    )
    assert res3["ok"] is False and res3["error"] == "missing_result"

    # gap 允许且保留
    mock_update = create_autospec(
        cur_db.update_request, return_value={**created_row, "status": "gap"}
    )
    monkeypatch.setattr(cur_db, "update_request", mock_update)
    res4 = json.loads(
        await tool.ainvoke(
            {
                "action": "update",
                "request_id": created_row["request_id"],
                "status": "gap",
                "result": [{"note": "委派被停止，证据缺口保留"}],
            },
            config=_CFG,
        )
    )
    assert res4["ok"] is True
    _, u_kwargs = mock_update.call_args
    assert u_kwargs["status"] == "gap"
    assert u_kwargs["result"] == [{"note": "委派被停止，证据缺口保留"}]

    # list
    monkeypatch.setattr(
        cur_db,
        "list_requests",
        create_autospec(cur_db.list_requests, return_value=[{**created_row, "status": "gap"}]),
    )
    res5 = json.loads(await tool.ainvoke({"action": "list"}, config=_CFG))
    assert res5["ok"] is True
    assert res5["status_counts"] == {"gap": 1}


@pytest.mark.asyncio
async def test_cross_unit_request_tool_validates_input(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.database.cross_unit_requests as cur_db

    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    mock_insert = create_autospec(cur_db.insert_request)
    monkeypatch.setattr(cur_db, "insert_request", mock_insert)

    res = json.loads(await tool.ainvoke({"action": "create"}, config=_CFG))
    assert res["ok"] is False and res["error"] == "invalid_input"
    mock_insert.assert_not_called()

    res2 = json.loads(await tool.ainvoke({"action": "frobnicate"}, config=_CFG))
    assert res2["ok"] is False and res2["error"] == "invalid_action"

    monkeypatch.setattr(cur_db, "get_request", create_autospec(cur_db.get_request, return_value=None))
    res3 = json.loads(
        await tool.ainvoke(
            {"action": "update", "request_id": "99999999-9999-9999-9999-999999999999", "status": "gap"},
            config=_CFG,
        )
    )
    assert res3["ok"] is False and res3["error"] == "request_not_found"


@pytest.mark.asyncio
async def test_cross_unit_request_tool_validates_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #11：sector_unit / target / dispatch_thread 的归属校验。"""
    import src.server.database.conversation.threads_read as threads_read
    from src.dalton.database import cross_unit_requests as cur_db

    real_get_thread = threads_read.get_thread_by_id

    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    rid = "99999999-9999-9999-9999-999999999999"
    foreign_sector_ws = "77777777-7777-7777-7777-777777777777"

    # 1) 他人的 sector 单元且未绑定工作区 → 无法核实访问权，拒绝
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            return_value={
                "unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "kind": "sector",
                "status": "active",
                "owner_user_id": "user-2",
                "workspace_id": None,
            },
        ),
    )
    res = json.loads(
        await tool.ainvoke(
            {"action": "create", "question": "取证", "sector_unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            config=_CFG,
        )
    )
    assert res["ok"] is False and res["error"] == "invalid_sector_unit"

    # 2) 他人的 sector 单元、绑定的工作区读不到 → 访问检查照常拒绝（HTTP 403）
    async def _deny_read(ws: str, uid: str, **kwargs: Any) -> None:
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            _REAL_CU["get_coverage_unit"],
            return_value={
                "unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "kind": "sector",
                "status": "active",
                "owner_user_id": "user-2",
                "workspace_id": foreign_sector_ws,
            },
        ),
    )
    monkeypatch.setattr(dt_mod, "verify_workspace_access", _deny_read)
    with pytest.raises(HTTPException):
        await tool.ainvoke(
            {"action": "create", "question": "取证",
             "sector_unit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            config=_CFG,
        )

    # 3) dispatch_thread_id 不属于目标工作区 → invalid_dispatch_thread
    monkeypatch.setattr(
        dt_mod, "verify_workspace_access", create_autospec(dt_mod.verify_workspace_access)
    )
    monkeypatch.setattr(
        cur_db,
        "get_request",
        create_autospec(
            cur_db.get_request,
            return_value={
                "request_id": rid,
                "owner_user_id": USER,
                "status": "open",
                "target_workspace_id": WS_COMPANY,
                "dispatch_thread_id": None,
                "result": [],
            },
        ),
    )
    monkeypatch.setattr(
        threads_read,
        "get_thread_by_id",
        create_autospec(
            threads_read.get_thread_by_id,
            return_value={"thread_id": "t-x", "workspace_id": WS_SECTOR},  # 属别的行业工作区
        ),
    )
    res3 = json.loads(
        await tool.ainvoke(
            {
                "action": "update",
                "request_id": rid,
                "status": "dispatched",
                "dispatch_thread_id": "12345678-1234-1234-1234-123456789012",
            },
            config=_CFG,
        )
    )
    assert res3["ok"] is False and res3["error"] == "invalid_dispatch_thread"

    # 4) 线程确实属于目标工作区 → 通过，状态可标 dispatched
    monkeypatch.setattr(
        threads_read,
        "get_thread_by_id",
        create_autospec(
            real_get_thread,
            return_value={"thread_id": "t-ok", "workspace_id": WS_COMPANY},
        ),
    )
    monkeypatch.setattr(
        cur_db,
        "update_request",
        create_autospec(
            _REAL_CUR["update_request"], return_value={"request_id": rid, "status": "dispatched"}
        ),
    )
    res4 = json.loads(
        await tool.ainvoke(
            {
                "action": "update",
                "request_id": rid,
                "status": "dispatched",
                "dispatch_thread_id": "12345678-1234-1234-1234-123456789012",
            },
            config=_CFG,
        )
    )
    assert res4["ok"] is True and res4["request"]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_cross_unit_request_tool_retry_action(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _director_tools(monkeypatch)
    tool = tools["dalton_cross_unit_request"]
    monkeypatch.setattr(
        cross_unit,
        "retry_linkage_rows",
        create_autospec(
            cross_unit.retry_linkage_rows,
            return_value={
                "results": [{"request_id": "r1", "status": "created"}],
                "remaining": 0,
            },
        ),
    )
    res = json.loads(
        await tool.ainvoke(
            {"action": "retry", "target_workspace_id": WS_COMPANY, "limit": 5},
            config=_CFG,
        )
    )
    assert res["ok"] is True
    assert res["results"][0]["status"] == "created"
    assert res["remaining"] == 0
    retry_fn = cross_unit.retry_linkage_rows
    _, r_kwargs = retry_fn.call_args
    assert r_kwargs["target_workspace_id"] == WS_COMPANY
    assert r_kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_cross_unit_requests_db_layer_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.database.cross_unit_requests as cur_db

    executed: list[tuple[str, tuple[Any, ...]]] = []
    cur = _spec_cursor()

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec
    cur.fetchone.return_value = {
        "request_id": "99999999-9999-9999-9999-999999999999",
        "owner_user_id": USER,
        "status": "open",
    }

    class _Conn:
        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return cur

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def transaction(self) -> "_Conn":
            return self

    monkeypatch.setattr(
        cur_db,
        "get_db_connection",
        create_autospec(cur_db.get_db_connection, return_value=_Conn()),
    )
    row = await cur_db.insert_request(
        owner_user_id=USER,
        question="取证请求",
        sector_workspace_id=WS_SECTOR,
        evidence_needed=[{"what": "出货量"}],
    )
    assert row["status"] == "open"
    sql, params = executed[0]
    assert sql.startswith("INSERT INTO dalton_cross_unit_requests")
    assert "dalton_knowledge_versions" not in sql
    _assert_no_stray_percent(sql, params)
    assert params[0] == USER

    with pytest.raises(ValueError, match="status"):
        await cur_db.list_requests(USER, status="bogus")


@pytest.mark.asyncio
async def test_contradiction_groups_that_resolve_to_one_target_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r5：两个引用分组解析到同一目标（大小写不同的同一 UUID，或单元
    换绑后新旧工作区 id）时合并为一条通知，不得后组覆盖前组、静默丢证据。"""
    written = _install_contradiction_env(monkeypatch)
    old_ws = "11111111-2222-3333-4444-555555555555"
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_knowledge_ref(unit_id=UNIT_SECTOR, workspace_id="ABCDEF00-0000-4000-8000-0000000000AA", entry_id="P1", relation="contradicts")]),
        _evidence(id="E-20261018-03", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(id="E-20261018-04", knowledge_refs=[_knowledge_ref(unit_id=UNIT_SECTOR, workspace_id=old_ws, entry_id="P1", relation="contradicts")]),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert [e["status"] for e in effects] == ["created"]
    assert effects[0]["evidence_ids"] == ["E-20261018-02", "E-20261018-03", "E-20261018-04"]
    assert len(written) == 1
    for eid in ("E-20261018-02", "E-20261018-03", "E-20261018-04"):
        assert eid in written[0]["question"]



@pytest.mark.asyncio
async def test_contradiction_refs_to_one_unit_fail_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r6：同一行业单元经新旧工作区 id 被引用，解析失败时整组一条 gap、
    不拆成"一条只含 E1 的通知 + 一条只含 E2 的 gap"。"""
    written = _install_contradiction_env(monkeypatch)

    async def _boom(unit_id: str, **kwargs: Any) -> dict[str, Any] | None:
        raise RuntimeError("coverage lookup down")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _boom)
    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(id="E-20261018-02", knowledge_refs=[_contradicts_ref("P1")]),
        _evidence(
            id="E-20261018-03",
            knowledge_refs=[_knowledge_ref(unit_id=UNIT_SECTOR, workspace_id="11111111-2222-4333-8444-555555555555", entry_id="P1", relation="contradicts")],
        ),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    assert [e["status"] for e in effects] == ["failed"]
    assert effects[0]["evidence_ids"] == ["E-20261018-02", "E-20261018-03"]
    assert written == []


# ---------------------------------------------------------------------------
# codex r7 #1：retry 按单元重新解析当前绑定；#2：duplicate 只在全覆盖时关 gap
# ---------------------------------------------------------------------------


def _gap_row_for_retry(**payload_overrides: Any) -> dict[str, Any]:
    payload = {
        "origin": "company",
        "source_ref": {
            "workspace_id": WS_COMPANY,
            "doc_key": "thesis",
            "version_no": 4,
            "entry_id": "E-20261018-02",
            "relation": "contradicts",
        },
        "target_ref": {
            "unit_id": UNIT_SECTOR,
            "workspace_id": WS_SECTOR,
            "doc_key": "thesis",
            "version_no": 3,
            "entry_id": "P1",
            "relation": "contradicts",
        },
        "evidence_ids": ["E-20261018-02"],
        "error": "transient registry failure",
    }
    payload.update(payload_overrides)
    return {
        "request_id": "99999999-9999-9999-9999-999999999999",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "status": "gap",
        "target_workspace_id": payload["target_ref"]["workspace_id"],
        "question": "更新候选：公司证据 E-20261018-02 与行业条目 P1 相矛盾",
        "payload": payload,
    }


def _install_retry_env(monkeypatch: pytest.MonkeyPatch, gap_row: dict[str, Any]) -> None:
    from src.dalton.database import cross_unit_requests as cur_db

    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(_REAL_CUR["list_linkage_retry_batch"], return_value=([gap_row], 1)),
    )


@pytest.mark.asyncio
async def test_retry_follows_unit_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex r7 #1：单元已换绑、gap 记的是旧工作区 → retry 投递到新工作区，
    旧工作区不写，台账 fulfilled 时同步目标。"""
    from src.dalton.database import cross_unit_requests as cur_db

    old_ws = "11111111-2222-3333-4444-555555555555"
    new_ws = "66666666-6666-6666-6666-666666666666"
    gap_row = _gap_row_for_retry()
    gap_row["target_workspace_id"] = old_ws
    gap_row["payload"]["target_ref"]["workspace_id"] = old_ws
    _install_retry_env(monkeypatch, gap_row)
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            return_value={**_SECTOR_UNIT_ROW, "workspace_id": new_ws},
        ),
    )
    access_calls: list[str] = []

    async def _can_write(uid: str, ws: str) -> bool:
        access_calls.append(ws)
        return True

    monkeypatch.setattr(cross_unit, "actor_can_write_workspace", _can_write)
    appended: list[dict[str, Any]] = []

    async def _fake_append(**kwargs: Any) -> dict[str, Any]:
        appended.append(kwargs)
        return {"status": "created", "version_no": 7}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _fake_append)
    update_spy = create_autospec(
        _REAL_CUR["update_request"], return_value={"status": "fulfilled"}
    )
    monkeypatch.setattr(cur_db, "update_request", update_spy)

    outcome = await cross_unit.retry_linkage_rows(USER)
    assert [r["status"] for r in outcome["results"]] == ["created"]
    # 投递到新绑定，旧工作区零写入、零权限检查
    assert [a["target_workspace_id"] for a in appended] == [new_ws]
    assert access_calls == [new_ws]
    assert appended[0]["target_ref"]["workspace_id"] == new_ws  # target_ref 已同步
    up_args, up_kwargs = update_spy.call_args
    assert up_kwargs["status"] == "fulfilled"
    assert up_kwargs["target_workspace_id"] == new_ws  # 台账目标同步到新绑定


@pytest.mark.asyncio
async def test_retry_keeps_gap_when_unit_binding_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r7 #1：单元解析失败 / 已非 active sector → 保留 gap 并写明原因。"""
    from src.dalton.database import cross_unit_requests as cur_db

    _install_retry_env(monkeypatch, _gap_row_for_retry())
    update_spy = create_autospec(_REAL_CUR["update_request"])
    monkeypatch.setattr(cur_db, "update_request", update_spy)
    appended: list[dict[str, Any]] = []

    async def _must_not_append(**kwargs: Any) -> dict[str, Any]:
        appended.append(kwargs)
        return {"status": "created"}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _must_not_append)

    async def _boom(unit_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("registry down")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _boom)
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    outcome = await cross_unit.retry_linkage_rows(USER)
    assert outcome["results"][0]["status"] == "failed"
    assert "单元绑定解析失败" in outcome["results"][0]["error"]

    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            return_value={**_SECTOR_UNIT_ROW, "status": "archived"},
        ),
    )
    outcome2 = await cross_unit.retry_linkage_rows(USER)
    assert outcome2["results"][0]["status"] == "failed"
    assert "不是 active 的 sector" in outcome2["results"][0]["error"]
    update_spy.assert_not_called()  # 两种失败都保留 gap


@pytest.mark.asyncio
async def test_retry_supplements_missing_evidence_on_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r7 #2 混合定位场景：E1 带单元解析失败 → gap[E1]；E2 只带工作区
    解析成功 → 通知只含 E2。retry 命中支柱级去重键但未覆盖 E1 → 追加一条
    只列 E1 的"补充反证"待办（独立去重键），然后才关闭 gap。"""
    from src.dalton.database import cross_unit_requests as cur_db

    # 阶段一（真实传播）：混合定位，带单元的组失败、纯工作区的组成功
    _install_contradiction_env(monkeypatch)

    async def _boom(unit_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("transient registry failure")

    monkeypatch.setattr(cu_db, "get_coverage_unit", _boom)

    async def _ws_lookup(ws: str, **kwargs: Any) -> dict[str, Any]:
        return _SECTOR_UNIT_ROW

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _ws_lookup)

    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(
            id="E-20261018-02",
            knowledge_refs=[_contradicts_ref("P1")],  # 带 unit_id，解析失败
        ),
        _evidence(
            id="E-20261018-03",
            knowledge_refs=[  # 只带工作区，解析成功
                {
                    "workspace_id": WS_SECTOR,
                    "doc_key": "thesis",
                    "version_no": 3,
                    "entry_id": "P1",
                    "relation": "contradicts",
                }
            ],
        ),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    by_status = {(e["status"], tuple(e.get("evidence_ids") or [])) for e in effects}
    assert ("created", ("E-20261018-03",)) in by_status  # 通知只含 E2
    gap_effect = next(e for e in effects if e["status"] == "failed")
    assert gap_effect["evidence_ids"] == ["E-20261018-02"]

    # 阶段二：E1 落 gap（真实 persist），数据库恢复
    insert_spy = create_autospec(cur_db.insert_request, return_value={"request_id": "r1"})
    monkeypatch.setattr(cur_db, "insert_request", insert_spy)
    assert await cross_unit.persist_failed_linkage(USER, gap_effect) is True
    _, ins_kwargs = insert_spy.call_args
    ledger_row = {
        "request_id": "r1",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "status": "gap",
        "target_workspace_id": ins_kwargs["target_workspace_id"],
        "question": ins_kwargs["question"],
        "payload": ins_kwargs["payload"],
    }
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=_SECTOR_UNIT_ROW),
    )

    # 已有议程：E2 的通知命中同一支柱级去重键，evidence_ids 只有 E2
    main_key = cross_unit.update_candidate_dedup_key(
        ledger_row["payload"]["source_ref"], ledger_row["payload"]["target_ref"]
    )
    existing_agenda = {
        "schema_version": 1,
        "open_questions": [
            {
                "id": "Q-001",
                "source_ref": {
                    "workspace_id": WS_COMPANY,
                    "doc_key": "thesis",
                    "version_no": 4,
                    "entry_id": "E-20261018-03",
                    "relation": "contradicts",
                },
                "target_ref": {
                    "workspace_id": WS_SECTOR,
                    "doc_key": "thesis",
                    "version_no": 3,
                    "entry_id": "P1",
                    "relation": "contradicts",
                },
                "evidence_ids": ["E-20261018-03"],
                "question": "更新候选（已存在，只含 E2）",
            }
        ],
    }
    assert main_key in cross_unit.question_dedup_keys(existing_agenda["open_questions"][0])
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(
            db_knowledge.latest_version,
            return_value={"version_no": 9, "content": existing_agenda},
        ),
    )

    # 阶段三：retry —— 主键 duplicate → 覆盖检查缺 E1 → 补充待办 → 关 gap
    append_calls: list[dict[str, Any]] = []

    async def _append_by_key(**kwargs: Any) -> dict[str, Any]:
        append_calls.append(kwargs)
        if len(kwargs["dedup_key"]) == 5:
            return {"status": "skipped_duplicate"}
        return {"status": "created", "version_no": 10}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _append_by_key)
    _install_retry_env(monkeypatch, ledger_row)
    update_spy = create_autospec(
        _REAL_CUR["update_request"], return_value={"status": "fulfilled"}
    )
    monkeypatch.setattr(cur_db, "update_request", update_spy)

    outcome = await cross_unit.retry_linkage_rows(USER)
    entry = outcome["results"][0]
    # 主键命中 duplicate，但补充待办补齐了缺失证据——状态如实保留 duplicate
    assert entry["status"] == "skipped_duplicate" and entry["supplemented"] is True
    # 两次追加：主键（duplicate）+ 补充键（只列缺失的 E1）
    assert len(append_calls) == 2
    supp = append_calls[1]
    assert supp["evidence_ids"] == ["E-20261018-02"]
    assert "E-20261018-02" in supp["question"]
    assert "E-20261018-03" not in supp["question"]
    assert len(supp["dedup_key"]) == 6  # 原键 + 缺失集合摘要
    up_args, up_kwargs = update_spy.call_args
    assert up_kwargs["status"] == "fulfilled"
    assert up_kwargs["result"][0]["supplemented"] is True


@pytest.mark.asyncio
async def test_retry_duplicate_fully_covered_closes_gap_without_supplement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有待办覆盖 gap 全部证据 → 直接 fulfilled，不追加补充待办。"""
    from src.dalton.database import cross_unit_requests as cur_db

    gap_row = _gap_row_for_retry()
    _install_retry_env(monkeypatch, gap_row)
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=_SECTOR_UNIT_ROW),
    )
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )
    payload = gap_row["payload"]
    existing_q = {
        "id": "Q-001",
        "source_ref": dict(payload["source_ref"]),
        "target_ref": dict(payload["target_ref"]),
        "evidence_ids": ["E-20261018-02"],
        "question": "更新候选（已含全部证据）",
    }
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(
            db_knowledge.latest_version,
            return_value={"version_no": 9, "content": {"open_questions": [existing_q]}},
        ),
    )
    append_calls: list[dict[str, Any]] = []

    async def _only_main(**kwargs: Any) -> dict[str, Any]:
        append_calls.append(kwargs)
        return {"status": "skipped_duplicate"}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _only_main)
    update_spy = create_autospec(
        _REAL_CUR["update_request"], return_value={"status": "fulfilled"}
    )
    monkeypatch.setattr(cur_db, "update_request", update_spy)

    outcome = await cross_unit.retry_linkage_rows(USER)
    entry = outcome["results"][0]
    assert entry["status"] == "skipped_duplicate"
    assert "supplemented" not in entry
    assert len(append_calls) == 1  # 全覆盖：没有补充待办
    update_spy.assert_called_once()


# ---------------------------------------------------------------------------
# codex r7 #3：大小写归一——同一字母 UUID 两种写法、不带 unit_id → 一组一通知
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradiction_workspace_case_folding_groups_without_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一含字母 UUID 大写/小写两种写法、均不带 unit_id（走工作区分组）→
    只解析一次、只产生一组一条通知，包含两条证据。撤掉分组键上的
    .lower() 即拆成两组、解析两次，本测试失败。"""
    written = _install_contradiction_env(monkeypatch)
    ws_upper = "AAAAAAA0-0000-4000-8000-0000000000AB"
    ws_lower = ws_upper.lower()
    sector_calls: list[str] = []

    async def _recording_ws_lookup(ws: str, **kwargs: Any) -> dict[str, Any]:
        sector_calls.append(ws)
        return _SECTOR_UNIT_ROW

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _recording_ws_lookup)

    old_doc, new_doc = _company_thesis_with_contradictions(
        _evidence(
            id="E-20261018-02",
            knowledge_refs=[
                {
                    "workspace_id": ws_upper,
                    "doc_key": "thesis",
                    "version_no": 3,
                    "entry_id": "P1",
                    "relation": "contradicts",
                }
            ],
        ),
        _evidence(
            id="E-20261018-03",
            knowledge_refs=[
                {
                    "workspace_id": ws_lower,
                    "doc_key": "thesis",
                    "version_no": 3,
                    "entry_id": "P1",
                    "relation": "contradicts",
                }
            ],
        ),
    )
    effects = await cross_unit._propagate_company_contradictions(
        user_id=USER,
        workspace_id=WS_COMPANY,
        old_doc=old_doc,
        new_doc=new_doc,
        new_version_no=4,
    )
    # 一条通知、两条证据都在
    assert [e["status"] for e in effects] == ["created"]
    assert effects[0]["evidence_ids"] == ["E-20261018-02", "E-20261018-03"]
    assert len(written) == 1
    # 同一工作区（两种写法）只解析一次（公司单元查询是另一个工作区，另计）
    assert [c for c in sector_calls if c.lower() == ws_lower] == [ws_lower]


def test_supplement_key_is_derivable_from_the_persisted_item() -> None:
    """codex r8 #1：补充待办的六元组去重键能从持久化条目还原——并发 retry 在
    CAS 冲突后重读时才认得出已有的补充条目，不会再追加一条。"""
    gap_row = _gap_row_for_retry()
    payload = gap_row["payload"]
    missing = ["E-20261018-02"]
    pillar_key = cross_unit.update_candidate_dedup_key(payload["source_ref"], payload["target_ref"])
    supplement_key = (*pillar_key, cross_unit._missing_evidence_digest(missing))
    persisted = {
        "id": "Q-002",
        "source_ref": dict(payload["source_ref"]),
        "target_ref": dict(payload["target_ref"]),
        "evidence_ids": list(missing),
        "question": "补充反证",
    }
    assert supplement_key in cross_unit.question_dedup_keys(persisted)


@pytest.mark.asyncio
async def test_coverage_read_failure_keeps_gap_and_continues_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r8 #2：一条 gap 的覆盖检查读库失败 → 该条保留 gap，批次继续处理下一条。"""
    from src.dalton.database import cross_unit_requests as cur_db

    first, second = _gap_row_for_retry(), _gap_row_for_retry()
    second["request_id"] = "22222222-0000-4000-8000-000000000002"
    monkeypatch.setattr(
        cur_db,
        "list_linkage_retry_batch",
        create_autospec(_REAL_CUR["list_linkage_retry_batch"], return_value=([first, second], 2)),
    )
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=_SECTOR_UNIT_ROW),
    )
    monkeypatch.setattr(
        cross_unit,
        "actor_can_write_workspace",
        create_autospec(cross_unit.actor_can_write_workspace, return_value=True),
    )

    async def _dup(**kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped_duplicate"}

    monkeypatch.setattr(cross_unit, "_append_linkage_question", _dup)
    coverage = create_autospec(
        cross_unit._existing_evidence_for_key,
        side_effect=[RuntimeError("read timed out"), {"E-20261018-02"}],
    )
    monkeypatch.setattr(cross_unit, "_existing_evidence_for_key", coverage)
    update_spy = create_autospec(_REAL_CUR["update_request"], return_value={"status": "fulfilled"})
    monkeypatch.setattr(cur_db, "update_request", update_spy)

    outcome = await cross_unit.retry_linkage_rows(USER)
    statuses = [r["status"] for r in outcome["results"]]
    assert statuses == ["failed", "skipped_duplicate"]
    assert "gap kept" in outcome["results"][0]["error"]
    assert coverage.call_count == 2
    update_spy.assert_called_once()
