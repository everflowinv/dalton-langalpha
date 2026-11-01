"""Coverage-unit registry (d023, sector layer P0): flag default, service validation,
unique-violation mapping, and a source-level lock on the migration's constraints.

The table-level guarantees (sector has no ticker; one ACTIVE binding per
workspace; owner+kind+slug uniqueness) are Postgres constraints — the migration
test below locks their SQL, and real upgrade/downgrade runs against Postgres
are the lead's out-of-sandbox verification.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import errors as pg_errors

import src.dalton.coverage.units as units_mod
import src.dalton.database.coverage_units as cu_db
from src.dalton.coverage.units import (
    archive_coverage_unit,
    bind_coverage_unit_workspace,
    register_coverage_unit,
    unbind_coverage_unit_workspace,
)
from src.dalton.database.coverage_units import _friendly_unique_error
from src.dalton.settings import DaltonConfig

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d023_coverage_units.py"
)


# ---------------------------------------------------------------------------
# 开关：默认关闭
# ---------------------------------------------------------------------------


def test_coverage_units_flag_defaults_off() -> None:
    from src.dalton.settings import is_coverage_units_enabled

    assert DaltonConfig().coverage_units.enabled is False
    # dalton.yaml 的提交默认也是 false（环境变量未设时走 yaml）。
    assert is_coverage_units_enabled() is False


def test_coverage_units_flag_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.settings import is_coverage_units_enabled

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    assert is_coverage_units_enabled() is True
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")
    assert is_coverage_units_enabled() is False


# ---------------------------------------------------------------------------
# 迁移约束锁（SQL 级；真实 Postgres 升降级由 lead 在沙箱外验证）
# ---------------------------------------------------------------------------


def test_d023_migration_locks_identity_constraints() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    # kind 只有 company / sector；状态只有 active / archived
    assert "CHECK (kind IN ('company', 'sector'))" in sql
    assert "status IN ('active', 'archived')" in sql
    # sector 永远没有 ticker（数据库层 CHECK，不靠服务层自觉）
    assert "CHECK (kind <> 'sector' OR ticker IS NULL)" in sql
    # slug 是稳定标识：同一所有者 + kind 下唯一
    assert "UNIQUE (owner_user_id, kind, slug)" in sql
    # 一个工作区最多绑定一个 active 单元：部分唯一索引（NULL 互不相等，
    # 未绑定的 active 单元不受限；归档即释放槽位）
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_coverage_units_active_workspace",
        sql,
    )
    assert "WHERE status = 'active'" in sql
    # 只新增表和索引：不建关联表/引用索引（P2），不动 watch_state/publications（P3），
    # 也不搬运、不改任何存量数据
    assert "dalton_watch_state" not in sql
    assert "dalton_publications" not in sql
    assert "INSERT INTO" not in sql
    assert "UPDATE " not in sql
    assert "DELETE FROM" not in sql
    assert "ALTER TABLE" not in sql


def test_d023_migration_downgrade_drops_only_the_new_objects() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    downgrade = sql.split("def downgrade")[1]
    assert "DROP TABLE IF EXISTS dalton_coverage_units" in downgrade
    assert downgrade.count("DROP") == 4  # 3 个索引 + 1 张表
    assert "workspaces" not in downgrade
    assert "dalton_knowledge_versions" not in downgrade


# ---------------------------------------------------------------------------
# 登记服务：输入校验（非法输入在触库前拒绝）
# ---------------------------------------------------------------------------


def _install_fake_insert(monkeypatch: pytest.MonkeyPatch, **row: Any) -> Any:
    mock = create_autospec(
        units_mod.cu_db.insert_coverage_unit,
        return_value={"unit_id": "11111111-1111-1111-1111-111111111111", **row},
    )
    monkeypatch.setattr(units_mod.cu_db, "insert_coverage_unit", mock)
    return mock


@pytest.mark.asyncio
async def test_register_sector_with_ticker_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_insert = _install_fake_insert(monkeypatch)
    with pytest.raises(ValueError, match="sector"):
        await register_coverage_unit(
            "user-1", kind="sector", slug="ai-infrastructure", ticker="AI"
        )
    mock_insert.assert_not_called()


@pytest.mark.asyncio
async def test_register_normalizes_slug_and_company_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_insert = _install_fake_insert(monkeypatch)
    await register_coverage_unit(
        "user-1",
        kind="Company",
        slug="  Mega-Tech ",
        ticker=" nvda ",
        research_tz="America/New_York",
    )
    mock_insert.assert_awaited_once()
    _, kwargs = mock_insert.call_args
    assert kwargs["kind"] == "company"
    assert kwargs["slug"] == "mega-tech"
    assert kwargs["ticker"] == "NVDA"
    assert kwargs["display_name"] == "mega-tech"  # 未给展示名时回退 slug
    assert kwargs["research_tz"] == "America/New_York"


@pytest.mark.asyncio
async def test_register_rejects_bad_kind_slug_tz_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_insert = _install_fake_insert(monkeypatch)
    with pytest.raises(ValueError, match="kind"):
        await register_coverage_unit("user-1", kind="theme", slug="ai")
    with pytest.raises(ValueError, match="slug"):
        await register_coverage_unit("user-1", kind="sector", slug="AI Infra!")  # 大写/空格
    with pytest.raises(ValueError, match="slug"):
        await register_coverage_unit("user-1", kind="sector", slug="")
    with pytest.raises(ValueError, match="research_tz"):
        await register_coverage_unit("user-1", kind="sector", slug="ai", research_tz="Mars/Olympus")
    with pytest.raises(ValueError, match="owner"):
        await register_coverage_unit("  ", kind="sector", slug="ai")
    mock_insert.assert_not_called()


@pytest.mark.asyncio
async def test_register_rejects_ticker_that_is_not_ticker_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_insert = _install_fake_insert(monkeypatch)
    with pytest.raises(ValueError, match="ticker"):
        await register_coverage_unit(
            "user-1", kind="company", slug="mega-tech", ticker="AI Infrastructure"
        )
    mock_insert.assert_not_called()


@pytest.mark.asyncio
async def test_archive_and_unbind_raise_when_unit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_status = create_autospec(
        units_mod.cu_db.set_coverage_unit_status, return_value=None
    )
    monkeypatch.setattr(units_mod.cu_db, "set_coverage_unit_status", mock_status)
    mock_unbind = create_autospec(
        units_mod.cu_db.unbind_coverage_unit_workspace, return_value=None
    )
    monkeypatch.setattr(units_mod.cu_db, "unbind_coverage_unit_workspace", mock_unbind)
    with pytest.raises(ValueError, match="not found"):
        await archive_coverage_unit("11111111-1111-1111-1111-111111111111", "user-1")
    with pytest.raises(ValueError, match="not found"):
        await unbind_coverage_unit_workspace("11111111-1111-1111-1111-111111111111", "user-1")


@pytest.mark.asyncio
async def test_bind_rejects_non_uuid_before_touching_the_db(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_bind = create_autospec(units_mod.cu_db.bind_coverage_unit_workspace)
    monkeypatch.setattr(units_mod.cu_db, "bind_coverage_unit_workspace", mock_bind)
    with pytest.raises(ValueError, match="unit_id"):
        await bind_coverage_unit_workspace("not-a-uuid", "ws", "user-1")
    with pytest.raises(ValueError, match="workspace_id"):
        await bind_coverage_unit_workspace(
            "11111111-1111-1111-1111-111111111111", "not-a-uuid", "user-1"
        )
    mock_bind.assert_not_called()


# ---------------------------------------------------------------------------
# 绑定事务的语句顺序（codex r2 #2）：同一事务里先 FOR UPDATE 锁并确认
# 单元归属，再谈工作区。替身按 SQL 语义求值（预检 / 工作区校验 / 占用
# 检查 / UPDATE 都真实走到），对撤修后的旧顺序同样成立——旧顺序在相同
# 输入上会先给出工作区侧错误，下面的断言即失败。
# ---------------------------------------------------------------------------

_BIND_USER = "user-1"
_BIND_OTHER_USER = "user-2"
_BIND_UNIT_OWN = "11111111-1111-1111-1111-111111111111"
_BIND_UNIT_OCCUPIER = "33333333-3333-3333-3333-333333333333"
_BIND_UNIT_FOREIGN = "44444444-4444-4444-4444-444444444444"
_BIND_WS = "55555555-5555-5555-5555-555555555555"


class _BindCursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704


def _bind_unit_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "unit_id": _BIND_UNIT_OWN,
        "kind": "company",
        "owner_user_id": _BIND_USER,
        "slug": "mega-tech",
        "display_name": "Mega Tech",
        "workspace_id": None,
        "ticker": "MSFT",
        "status": "active",
        "research_tz": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


class _ExecLog(list):
    """按序执行的语句（str 列表），附带语义替身的可变状态。

    `params` 与语句一一对应；`locks` 是按序取过的 advisory 锁 key；
    `members` 是 `dalton_sector_members` 的当前行（DELETE/INSERT 即时求值）。
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.params: list[tuple[Any, ...]] = []
        self.locks: list[str] = []
        self.members: list[dict[str, Any]] = []


def _install_bind_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_rows: list[dict[str, Any]],
    ws_rows: dict[str, dict[str, Any]],
    active_binding_by_ws: dict[str, str],
    sector_doc_by_ws: dict[str, dict[str, Any]] | None = None,
    initial_members: list[dict[str, Any]] | None = None,
) -> _ExecLog:
    """按 SQL 语义求值的绑定/解绑/归档事务替身。

    语句按生产 SQL 的形状分派并即时落效（UPDATE 改 unit_rows、DELETE/
    INSERT 改 members），对真实 `sync_projection_to_latest_doc` /
    `sync_members_projection` 同样可求值——它们跑的是真代码，只有游标是替身。
    """
    from psycopg import AsyncConnection

    sector_doc_by_ws = sector_doc_by_ws or {}
    executed = _ExecLog()
    executed.members.extend(initial_members or [])
    state: dict[str, Any] = {"sql": "", "params": ()}

    cur = create_autospec(_BindCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        norm = " ".join(str(sql).split())
        p = tuple(params or ())
        executed.append(norm)
        executed.params.append(p)
        state["sql"], state["params"] = norm, p
        if norm.startswith("SELECT pg_advisory_xact_lock"):
            executed.locks.append(str(p[0]))
        elif norm.startswith("DELETE FROM dalton_sector_members"):
            target = str(p[0])
            executed.members[:] = [
                m for m in executed.members if str(m["sector_unit_id"]) != target
            ]
        elif norm.startswith("INSERT INTO dalton_sector_members"):
            executed.members.append(
                {
                    "sector_unit_id": str(p[0]),
                    "member_id": p[1],
                    "member_name": p[2],
                    "ticker": p[3],
                    "company_unit_id": p[4],
                    "workspace_created": p[5],
                    "scope": p[6],
                    "reason": p[7],
                    "valid_until": p[8],
                    "position": p[9],
                    "synced_version_no": p[10],
                }
            )

    async def _fetchone() -> Any:
        sql, p = state["sql"], state["params"]
        if sql.startswith("SELECT workspace_id::text AS workspace_id FROM dalton_coverage_units"):
            # bind 归属预检（同时读出原绑定），参数 (unit_id, owner_user_id)
            return next(
                (
                    {"workspace_id": str(u.get("workspace_id") or "") or None}
                    for u in unit_rows
                    if str(u["unit_id"]) == str(p[0])
                    and str(u["owner_user_id"]) == str(p[1])
                    and u["status"] == "active"
                ),
                None,
            )
        if sql.startswith("SELECT kind, workspace_id"):
            # unbind/archive 预读，参数 (unit_id, owner_user_id)，任意 status
            return next(
                (
                    {
                        "kind": u["kind"],
                        "workspace_id": str(u.get("workspace_id") or "") or None,
                    }
                    for u in unit_rows
                    if str(u["unit_id"]) == str(p[0])
                    and str(u["owner_user_id"]) == str(p[1])
                ),
                None,
            )
        if "FROM workspaces" in sql:
            # uuid 比较与大小写无关（Postgres 语义）；返回行里的 id 是规范化小写
            ws = ws_rows.get(str(p[0]).lower())
            if ws is None or ws.get("status") == "deleted":
                return None
            return dict(ws)
        if "FROM dalton_knowledge_versions" in sql:
            # sync_projection_to_latest_doc 的最新有效行业文档，参数 (workspace_id,)
            doc = sector_doc_by_ws.get(str(p[0]))
            return dict(doc) if doc else None
        if "kind = 'sector'" in sql and "FROM dalton_coverage_units" in sql:
            # sync_members_projection 的 active 行业单元查找，参数 (workspace_id,)
            return next(
                (
                    {
                        "unit_id": str(u["unit_id"]),
                        "owner_user_id": u["owner_user_id"],
                        "slug": u["slug"],
                    }
                    for u in unit_rows
                    if str(u.get("workspace_id") or "") == str(p[0])
                    and u["status"] == "active"
                    and u["kind"] == "sector"
                ),
                None,
            )
        if "SELECT slug FROM dalton_coverage_units" in sql:
            # 占用检查，参数 (workspace_id, unit_id)
            other = active_binding_by_ws.get(str(p[0]))
            return {"slug": other} if other and str(other) != str(p[1]) else None
        if sql.startswith("UPDATE dalton_coverage_units"):
            if "SET status" in sql:
                # set_coverage_unit_status，参数 (status, unit_id, owner_user_id)
                for u in unit_rows:
                    if (
                        str(u["unit_id"]) == str(p[1])
                        and str(u["owner_user_id"]) == str(p[2])
                    ):
                        u["status"] = str(p[0])
                        return dict(u)
                return None
            if "SET workspace_id = NULL" in sql:
                # unbind，参数 (unit_id, owner_user_id)
                for u in unit_rows:
                    if (
                        str(u["unit_id"]) == str(p[0])
                        and str(u["owner_user_id"]) == str(p[1])
                    ):
                        u["workspace_id"] = None
                        return dict(u)
                return None
            # bind 的 UPDATE，参数 (workspace_id, unit_id, owner_user_id)
            for u in unit_rows:
                if (
                    str(u["unit_id"]) == str(p[1])
                    and str(u["owner_user_id"]) == str(p[2])
                    and u["status"] == "active"
                ):
                    u["workspace_id"] = str(p[0]).lower()  # uuid 列存规范化形式
                    return dict(u)
            return None
        raise AssertionError(f"unexpected SQL in bind double: {sql}")

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.side_effect = lambda *a: _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(
            cu_db.get_db_connection, side_effect=lambda conn=None: ctx
        ),
    )
    return executed


@pytest.mark.asyncio
async def test_bind_foreign_unit_with_occupied_workspace_reports_unit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #2：他人的 unit_id + 本人已被占用的工作区 → unit not found，而非 workspace conflict。"""
    from src.server.database.home_workspace import get_home_workspace_id

    assert _BIND_WS != get_home_workspace_id(_BIND_USER)  # 场景是普通工作区
    foreign = _bind_unit_row(
        unit_id=_BIND_UNIT_FOREIGN, owner_user_id=_BIND_OTHER_USER, slug="foreign"
    )
    occupier = _bind_unit_row(
        unit_id=_BIND_UNIT_OCCUPIER, slug="occupier", workspace_id=_BIND_WS
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[foreign, occupier],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={_BIND_WS: _BIND_UNIT_OCCUPIER},
    )

    with pytest.raises(
        ValueError,
        match=f"active coverage unit {_BIND_UNIT_FOREIGN} not found for this owner",
    ):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_FOREIGN, _BIND_WS, _BIND_USER)
    # 归属预检是第一条语句；工作区检查根本没被碰（撤修后第一条是
    # FROM workspaces，且错误会先变成 workspace conflict）。
    assert "FOR UPDATE" in executed[0]
    assert not any("FROM workspaces" in s for s in executed)


@pytest.mark.asyncio
async def test_bind_foreign_unit_on_home_reports_unit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #2：他人的 unit_id + Home → unit not found，而非 Home 拒绝。"""
    from src.server.database.home_workspace import get_home_workspace_id

    home_ws = get_home_workspace_id(_BIND_USER)
    foreign = _bind_unit_row(
        unit_id=_BIND_UNIT_FOREIGN, owner_user_id=_BIND_OTHER_USER, slug="foreign"
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[foreign],
        ws_rows={
            home_ws: {
                "workspace_id": home_ws,
                "user_id": _BIND_USER,
                "name": "Home",
                "status": "active",
            }
        },
        active_binding_by_ws={},
    )

    with pytest.raises(
        ValueError,
        match=f"active coverage unit {_BIND_UNIT_FOREIGN} not found for this owner",
    ):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_FOREIGN, home_ws, _BIND_USER)
    assert "FOR UPDATE" in executed[0]
    assert not any("FROM workspaces" in s for s in executed)


@pytest.mark.asyncio
async def test_bind_happy_path_locks_unit_before_workspace_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本人的 active 单元绑定空闲工作区：预检放行，UPDATE 条件保持 owner/status。"""
    own = _bind_unit_row(unit_id=_BIND_UNIT_OWN, slug="mega-tech")
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[own],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
    )

    row = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_WS, _BIND_USER)
    assert row["workspace_id"] == _BIND_WS
    precheck = executed[0]
    assert precheck.startswith("SELECT workspace_id::text AS workspace_id FROM dalton_coverage_units")
    assert "FOR UPDATE" in precheck
    # 显式转型：uuid 列带 ::uuid（owner_user_id 是 text，与文件既有写法一致）
    assert "unit_id = %s::uuid AND owner_user_id = %s AND status = 'active'" in precheck
    update_sql = next(s for s in executed if s.startswith("UPDATE dalton_coverage_units"))
    assert "owner_user_id = %s AND status = 'active'" in update_sql


# ---------------------------------------------------------------------------
# 绑定关系变化时同步成员投影（codex r3）：投影的定义是"该行业单元当前
# 绑定的工作区的最新 schema_valid 行业文档"。bind 在同一事务里（单元行锁
# → 工作区 advisory 锁）重同步；unbind/archive 清空。真实
# sync_projection_to_latest_doc / sync_members_projection 全程在测，只有
# 游标是语义替身。
# ---------------------------------------------------------------------------

_BIND_UNIT_SECTOR_A = "66666666-6666-6666-6666-666666666666"
_BIND_UNIT_SECTOR_B = "77777777-7777-7777-7777-777777777777"


def _install_units_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    import src.dalton.settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "is_coverage_units_enabled", lambda e=enabled: e
    )


def _sector_doc_2_members(version_no: int = 3) -> dict[str, Any]:
    return {
        "version_no": version_no,
        "content": {
            "members": [
                {"id": "M1", "name": "某综合科技公司", "ticker": "EXAMPLE"},
                {"id": "M2", "name": "未覆盖的封装厂商"},
            ]
        },
    }


def _member_row(unit_id: str, member_id: str) -> dict[str, Any]:
    return {
        "sector_unit_id": unit_id,
        "member_id": member_id,
        "member_name": member_id,
        "ticker": None,
        "company_unit_id": None,
        "workspace_created": False,
        "scope": "",
        "reason": "",
        "valid_until": None,
        "position": 0,
        "synced_version_no": 1,
    }


@pytest.mark.asyncio
async def test_archive_then_rebind_moves_projection_to_new_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档 A（绑 W）→ B 绑 W（W 的行业文档 2 成员）：B 投影 2 行，A 清空。"""
    _install_units_flag(monkeypatch, True)
    unit_a = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_A, kind="sector", slug="sector-a",
        workspace_id=_BIND_WS, ticker=None,
    )
    unit_b = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_B, kind="sector", slug="sector-b", ticker=None
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[unit_a, unit_b],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
        sector_doc_by_ws={_BIND_WS: _sector_doc_2_members()},
        initial_members=[
            _member_row(_BIND_UNIT_SECTOR_A, "M1"),
            _member_row(_BIND_UNIT_SECTOR_A, "M2"),
        ],
    )

    archived = await cu_db.set_coverage_unit_status(
        _BIND_UNIT_SECTOR_A, _BIND_USER, "archived"
    )
    assert archived is not None and archived["status"] == "archived"
    await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_B, _BIND_WS, _BIND_USER)

    # A 清空、B 接手：成员行全部挂在 B 名下
    assert {m["sector_unit_id"] for m in executed.members} == {_BIND_UNIT_SECTOR_B}
    assert sorted(m["member_id"] for m in executed.members) == ["M1", "M2"]
    assert all(m["synced_version_no"] == 3 for m in executed.members)
    # 两步各取一次锁，key 都是 insert_version 的那把
    assert executed.locks == [f"dalton:kv:{_BIND_WS}:sector"] * 2
    # bind 阶段：advisory 锁在投影同步（文档读取 / 写入）之前
    insert_idx = next(
        i for i, s in enumerate(executed) if s.startswith("INSERT INTO dalton_sector_members")
    )
    lock_idxs = [
        i for i, s in enumerate(executed) if s.startswith("SELECT pg_advisory_xact_lock")
    ]
    bind_lock = max(i for i in lock_idxs if i < insert_idx)
    assert "FROM dalton_knowledge_versions" in executed[bind_lock + 1]


@pytest.mark.asyncio
async def test_rebind_locks_previous_and_target_workspace_in_sorted_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 从 W1 改绑 W2：两个工作区的写文档锁都取，按排序固定顺序（codex r3 #1）。

    只锁 W2 时，W1 上已读到 A 的写文档事务可在本事务之后提交，把 W1 的成员
    写回 A 名下；锁住 W1 让它要么先提交（被这里覆盖），要么等本事务提交后再读。
    """
    _install_units_flag(monkeypatch, True)
    ws_new = "00000000-0000-0000-0000-0000000000a2"  # 排序在 _BIND_WS 之前
    unit_a = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_A, kind="sector", slug="sector-a",
        workspace_id=_BIND_WS, ticker=None,
    )
    ws = {"user_id": _BIND_USER, "name": "Mine", "status": "active"}
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[unit_a],
        ws_rows={
            _BIND_WS: {"workspace_id": _BIND_WS, **ws},
            ws_new: {"workspace_id": ws_new, **ws},
        },
        active_binding_by_ws={},
        sector_doc_by_ws={ws_new: _sector_doc_2_members()},
    )

    await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_A, ws_new, _BIND_USER)

    assert executed.locks == [
        f"dalton:kv:{ws_new}:sector",
        f"dalton:kv:{_BIND_WS}:sector",
    ]
    assert {m["sector_unit_id"] for m in executed.members} == {_BIND_UNIT_SECTOR_A}


@pytest.mark.asyncio
async def test_bind_lock_key_uses_normalized_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大写 UUID 绑定：锁 key 用库里返回的规范化 id，与写文档路径是同一把锁（codex r3 #2）。"""
    _install_units_flag(monkeypatch, True)
    unit_b = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_B, kind="sector", slug="sector-b", ticker=None
    )
    ws_upper = "AAAAAAAA-0000-0000-0000-000000000001"
    ws_norm = ws_upper.lower()
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[unit_b],
        ws_rows={
            ws_norm: {
                "workspace_id": ws_norm,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
        sector_doc_by_ws={ws_norm: _sector_doc_2_members()},
    )

    await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_B, ws_upper, _BIND_USER)

    assert executed.locks == [f"dalton:kv:{ws_norm}:sector"]
    assert len(executed.members) == 2


@pytest.mark.asyncio
async def test_bind_sector_unit_to_workspace_without_doc_clears_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行业单元绑到无有效文档的工作区：其旧投影被清空（投影=文档，文档无则无）。"""
    _install_units_flag(monkeypatch, True)
    unit_b = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_B, kind="sector", slug="sector-b", ticker=None
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[unit_b],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
        sector_doc_by_ws={},  # W 没有行业文档
        initial_members=[
            _member_row(_BIND_UNIT_SECTOR_B, "M1"),
            _member_row(_BIND_UNIT_SECTOR_B, "M2"),
        ],
    )

    row = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_B, _BIND_WS, _BIND_USER)
    assert row["workspace_id"] == _BIND_WS
    assert executed.members == []
    assert executed.locks == [f"dalton:kv:{_BIND_WS}:sector"]
    assert any("FROM dalton_knowledge_versions" in s for s in executed)


@pytest.mark.asyncio
async def test_bind_company_unit_never_touches_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司单元绑定：不取 advisory 锁、不碰 dalton_sector_members（kind 门控）。"""
    _install_units_flag(monkeypatch, True)
    company = _bind_unit_row(unit_id=_BIND_UNIT_OWN, kind="company", slug="mega-tech")
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[company],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
        sector_doc_by_ws={_BIND_WS: _sector_doc_2_members()},
        initial_members=[_member_row(_BIND_UNIT_SECTOR_A, "M1")],
    )

    await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_WS, _BIND_USER)
    assert not any(s.startswith("SELECT pg_advisory_xact_lock") for s in executed)
    assert not any("dalton_sector_members" in s for s in executed)
    assert not any("dalton_knowledge_versions" in s for s in executed)
    assert len(executed.members) == 1  # 预置行原样


@pytest.mark.asyncio
async def test_unbind_and_archive_clear_sector_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解绑 / 归档行业单元：同一事务内对原工作区取锁后清空投影。"""
    _install_units_flag(monkeypatch, True)
    sector = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_A, kind="sector", slug="sector-a",
        workspace_id=_BIND_WS, ticker=None,
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[sector],
        ws_rows={},
        active_binding_by_ws={},
        initial_members=[
            _member_row(_BIND_UNIT_SECTOR_A, "M1"),
            _member_row(_BIND_UNIT_SECTOR_A, "M2"),
        ],
    )

    unbound = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_SECTOR_A, _BIND_USER)
    assert unbound is not None and unbound["workspace_id"] is None
    assert executed.members == []
    assert executed.locks == [f"dalton:kv:{_BIND_WS}:sector"]

    # 归档路径同样清空（重新放两行再归档）。此时单元已被解绑、原工作区为空，
    # 按契约不取锁、直接删——locks 不再增长。
    executed.members.extend(
        [_member_row(_BIND_UNIT_SECTOR_A, "M1"), _member_row(_BIND_UNIT_SECTOR_A, "M2")]
    )
    archived = await cu_db.set_coverage_unit_status(
        _BIND_UNIT_SECTOR_A, _BIND_USER, "archived"
    )
    assert archived is not None and archived["status"] == "archived"
    assert executed.members == []
    assert executed.locks == [f"dalton:kv:{_BIND_WS}:sector"]


@pytest.mark.asyncio
async def test_unbind_company_unit_does_not_touch_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司单元解绑：预读只判 kind，不取锁、不碰投影。"""
    _install_units_flag(monkeypatch, True)
    company = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN, kind="company", slug="mega-tech", workspace_id=_BIND_WS
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[company],
        ws_rows={},
        active_binding_by_ws={},
        initial_members=[_member_row(_BIND_UNIT_SECTOR_A, "M1")],
    )

    row = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert row is not None and row["workspace_id"] is None
    assert not any(s.startswith("SELECT pg_advisory_xact_lock") for s in executed)
    assert not any("dalton_sector_members" in s for s in executed)
    assert len(executed.members) == 1


@pytest.mark.asyncio
async def test_flag_off_bind_does_not_sync_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：绑定不同步投影——不取锁、不读文档、不碰成员表（与 knowledge 同门控）。"""
    _install_units_flag(monkeypatch, False)
    sector = _bind_unit_row(
        unit_id=_BIND_UNIT_SECTOR_A, kind="sector", slug="sector-a", ticker=None
    )
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[sector],
        ws_rows={
            _BIND_WS: {
                "workspace_id": _BIND_WS,
                "user_id": _BIND_USER,
                "name": "Mine",
                "status": "active",
            }
        },
        active_binding_by_ws={},
        sector_doc_by_ws={_BIND_WS: _sector_doc_2_members()},
        initial_members=[_member_row(_BIND_UNIT_SECTOR_A, "M1")],
    )

    row = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_A, _BIND_WS, _BIND_USER)
    assert row["workspace_id"] == _BIND_WS
    assert not any(s.startswith("SELECT pg_advisory_xact_lock") for s in executed)
    assert not any("dalton_sector_members" in s for s in executed)
    assert not any("dalton_knowledge_versions" in s for s in executed)
    assert len(executed.members) == 1


# ---------------------------------------------------------------------------
# 唯一约束冲突 → 可读错误（表上的索引才是真约束；这里只锁映射）
# ---------------------------------------------------------------------------


def _unique_violation(constraint: str) -> pg_errors.UniqueViolation:
    exc = create_autospec(pg_errors.UniqueViolation, instance=True)
    exc.diag.constraint_name = constraint
    return exc


def test_unique_violation_maps_to_named_value_errors() -> None:
    with pytest.raises(ValueError, match="slug"):
        raise _friendly_unique_error(_unique_violation("uq_dalton_coverage_units_owner_kind_slug"))
    with pytest.raises(ValueError, match="already bound"):
        raise _friendly_unique_error(_unique_violation("uq_dalton_coverage_units_active_workspace"))
    with pytest.raises(ValueError, match="constraint"):
        raise _friendly_unique_error(_unique_violation("some_other_constraint"))


# ---------------------------------------------------------------------------
# INSERT 语句本身（codex r1 #1）：不传 unit_id 时主键列必须整个缺席，
# 让 DEFAULT gen_random_uuid() 生效——显式 NULL 不会触发默认值，登记必然
# NotNullViolation。真实执行 insert_coverage_unit 的 SQL 组装，只在 DB 边界
# （连接/游标）替换；仓库没有对迁移跑真实 Postgres 的测试设施，升级/降级与
# 真实插入由 lead 在临时 Postgres 上验证。
# ---------------------------------------------------------------------------


class _ExecCursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704


class _ConnCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_insert_without_unit_id_omits_the_pk_column_from_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from psycopg import AsyncConnection

    executed: list[tuple[str, tuple[Any, ...]]] = []

    cur = create_autospec(_ExecCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec
    cur.fetchone.return_value = {
        "unit_id": "99999999-9999-9999-9999-999999999999",
        "kind": "company",
        "owner_user_id": "user-1",
        "slug": "mega-tech",
        "ticker": "MSFT",
        "status": "active",
    }

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.side_effect = lambda *a: _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    mock_conn = create_autospec(cu_db.get_db_connection, side_effect=lambda conn=None: ctx)
    monkeypatch.setattr(cu_db, "get_db_connection", mock_conn)

    await cu_db.insert_coverage_unit(
        kind="company",
        owner_user_id="user-1",
        slug="mega-tech",
        display_name="Mega Tech",
        ticker="MSFT",
    )

    assert len(executed) == 1
    sql, params = executed[0]
    assert sql.startswith("INSERT INTO dalton_coverage_units")
    cols = sql.split("dalton_coverage_units (", 1)[1].split(")", 1)[0]
    # 主键列缺席 + 参数无 None 占位 → DEFAULT gen_random_uuid() 生效
    assert "unit_id" not in cols
    assert params == ("company", "user-1", "mega-tech", "Mega Tech", None, "MSFT", None)

    executed.clear()
    explicit = "11111111-1111-1111-1111-111111111111"
    await cu_db.insert_coverage_unit(
        kind="company",
        owner_user_id="user-1",
        slug="mega-tech",
        ticker="MSFT",
        unit_id=explicit,
    )
    sql2, params2 = executed[0]
    cols2 = sql2.split("dalton_coverage_units (", 1)[1].split(")", 1)[0]
    assert "unit_id" in cols2
    assert params2[0] == explicit
    assert len(params2) == len(params) + 1


def test_batch_lookup_tolerates_unmigrated_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开但 d023 未跑：批量查询返回空并记日志，看板/探针回退兼容规则。"""
    import asyncio

    class _Cur:
        async def __aenter__(self) -> "_Cur":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: Any, params: Any = None) -> None:
            raise pg_errors.UndefinedTable("relation does not exist")

        async def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def cursor(self, *args: Any, **kwargs: Any) -> "_Cur":
            return _Cur()

    ctx = _Ctx(_Conn())
    mock_conn = create_autospec(cu_db.get_db_connection, side_effect=lambda conn=None: ctx)
    monkeypatch.setattr(cu_db, "get_db_connection", mock_conn)
    assert asyncio.run(cu_db.load_active_units_by_workspace(["ws-1"])) == {}


class _Ctx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None
