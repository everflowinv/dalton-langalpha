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

import src.dalton.access_tiers as _tiers_mod
import src.dalton.coverage.units as units_mod
import src.dalton.database.coverage_units as cu_db
import src.dalton.invites as _invites_mod
import src.dalton.settings as _settings_mod
import src.dalton.sharing as _sharing_mod
from src.dalton.coverage.units import (
    archive_coverage_unit,
    bind_coverage_unit_workspace,
    register_coverage_unit,
    unbind_coverage_unit_workspace,
)
from src.dalton.database.coverage_units import _friendly_unique_error
from src.dalton.settings import DaltonConfig

_ORIG_GET_DB_CONN = cu_db.get_db_connection
_ORIG_IS_INVITE_PM = _invites_mod.is_invite_pm
_ORIG_IS_INTERNAL_OR_ADMIN = _tiers_mod.is_internal_or_admin
_ORIG_CLASSIFY_OWNER_TIER = _tiers_mod.classify_owner_tier
_ORIG_REQUIRE_WS_ACCESS = _sharing_mod.require_workspace_access
_ORIG_SUPABASE_AUTH_ENABLED = _settings_mod.is_dalton_supabase_auth_enabled

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d023_coverage_units.py"
)


# ---------------------------------------------------------------------------
# 开关：YAML 默认为 true；单测文件显式开启，defaults 测试绕过环境变量验 YAML
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_coverage_units_for_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly enable coverage_units for this module over the suite-wide default-off fixture."""
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")


def test_coverage_units_flag_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.settings import get_dalton_config, is_coverage_units_enabled

    assert DaltonConfig().coverage_units.enabled is False
    # dalton.yaml 的提交默认已在 d041 同步改为 true（读 YAML 默认，不受环境变量影响）。
    get_dalton_config.cache_clear()
    assert get_dalton_config().coverage_units.enabled is True
    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    assert is_coverage_units_enabled() is True


def test_coverage_units_flag_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.settings import is_coverage_units_enabled

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    assert is_coverage_units_enabled() is True
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "0")
    assert is_coverage_units_enabled() is False
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
    from psycopg import AsyncConnection

    conn = create_autospec(AsyncConnection, instance=True)
    conn.transaction.return_value = _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
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
    from psycopg import AsyncConnection

    conn = create_autospec(AsyncConnection, instance=True)
    conn.transaction.return_value = _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        units_mod.cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=None),
    )
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
    rowcount: int

    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


def _bind_unit_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "unit_id": _BIND_UNIT_OWN,
        "kind": "company",
        "owner_user_id": _BIND_USER,
        "created_by_user_id": _BIND_USER,
        "lead_analyst_user_id": _BIND_USER,
        "slug": "mega-tech",
        "display_name": "Mega Tech",
        "workspace_id": None,
        "ticker": "MSFT",
        "ticker_key": "MSFT",
        "status": "active",
        "research_tz": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    if "lead_analyst_user_id" not in overrides:
        row["lead_analyst_user_id"] = row["owner_user_id"]
    if "created_by_user_id" not in overrides:
        row["created_by_user_id"] = row["owner_user_id"]
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
    colleague_rw_by_user: dict[str, set[str]] | None = None,
    internal_users: set[str] | None = None,
    pm_users: set[str] | None = None,
    patch_require_access: bool = True,
) -> _ExecLog:
    """按 SQL 语义求值的绑定/解绑/归档/转交事务替身。

    语句按生产 SQL 的形状分派并即时落效（UPDATE 改 unit_rows、DELETE/
    INSERT 改 members），对真实 `sync_projection_to_latest_doc` /
    `sync_members_projection` 同样可求值——它们跑的是真代码，只有游标是替身。
    """
    from fastapi import HTTPException
    from psycopg import AsyncConnection
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.invites as invites_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    sector_doc_by_ws = sector_doc_by_ws or {}
    rw_map = colleague_rw_by_user or {}
    pm_set = pm_users or set()
    internal_set = internal_users

    monkeypatch.setattr(
        invites_mod,
        "is_invite_pm",
        create_autospec(_ORIG_IS_INVITE_PM, side_effect=lambda uid: str(uid) in pm_set),
    )
    if internal_set is not None:
        monkeypatch.setattr(
            access_tiers_mod,
            "is_internal_or_admin",
            create_autospec(
                _ORIG_IS_INTERNAL_OR_ADMIN,
                side_effect=lambda uid: str(uid) in internal_set,
            ),
        )
    if patch_require_access:
        sb_true = create_autospec(_ORIG_SUPABASE_AUTH_ENABLED, return_value=True)
        monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", sb_true)
        monkeypatch.setattr(sharing_mod, "is_dalton_supabase_auth_enabled", sb_true)
        monkeypatch.setattr(
            access_tiers_mod,
            "classify_owner_tier",
            create_autospec(
                _ORIG_CLASSIFY_OWNER_TIER,
                return_value="internal",
            ),
        )

    async def _require_access(
        ws_id_or_row: Any,
        viewer_uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        ws = workspace if workspace is not None else ws_id_or_row
        if isinstance(ws, dict):
            wid = str(ws.get("workspace_id") or "").lower()
            w_owner = str(ws.get("user_id") or "")
        else:
            wid = str(ws or "").lower()
            w_owner = str((ws_rows.get(wid) or {}).get("user_id") or "")
        if w_owner == str(viewer_uid):
            return {"workspace_id": wid, "user_id": w_owner}
        if wid in {w.lower() for w in rw_map.get(str(viewer_uid), set())}:
            return {"workspace_id": wid, "user_id": w_owner}
        raise HTTPException(status_code=403, detail="forbidden")

    if patch_require_access:
        monkeypatch.setattr(
            sharing_mod,
            "require_workspace_access",
            create_autospec(_ORIG_REQUIRE_WS_ACCESS, side_effect=_require_access),
        )

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

    async def _fetchall() -> list[dict[str, Any]]:
        sql, p = state["sql"], state["params"]
        if "FROM dalton_coverage " in sql:
            return []
        if "FROM automations" in sql:
            return []
        if "FROM workspaces" in sql and "ANY(%s::uuid[])" in sql:
            wanted = [str(w).lower() for w in (p[0] or [])]
            found = []
            for wid in sorted(set(wanted)):
                ws = ws_rows.get(wid)
                if ws is not None:
                    found.append(dict(ws))
            return found
        raise AssertionError(f"unexpected fetchall SQL in bind double: {sql}")

    async def _fetchone() -> Any:
        sql, p = state["sql"], state["params"]
        if (
            "FROM dalton_coverage_units" in sql
            and "WHERE unit_id = %s::uuid" in sql
        ):
            active_only = "status = 'active'" in sql
            return next(
                (
                    dict(u)
                    for u in unit_rows
                    if str(u["unit_id"]) == str(p[0])
                    and (not active_only or u["status"] == "active")
                ),
                None,
            )
        if "FROM workspaces" in sql:
            ws = ws_rows.get(str(p[0]).lower())
            if ws is None:
                return None
            return dict(ws)
        if "FROM dalton_knowledge_versions" in sql:
            doc = sector_doc_by_ws.get(str(p[0]))
            return dict(doc) if doc else None
        if "kind = 'sector'" in sql and "FROM dalton_coverage_units" in sql:
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
            other = active_binding_by_ws.get(str(p[0]).lower())
            if len(p) >= 2:
                return {"slug": other} if other and str(other) != str(p[1]) else None
            return {"slug": other} if other else None
        if sql.startswith("INSERT INTO dalton_coverage_units"):
            return None
        if sql.startswith("UPDATE dalton_coverage_units"):
            if "SET status" in sql:
                for u in unit_rows:
                    if str(u["unit_id"]) == str(p[1]):
                        u["status"] = str(p[0])
                        return dict(u)
                return None
            if "SET workspace_id = NULL" in sql:
                for u in unit_rows:
                    if str(u["unit_id"]) == str(p[0]):
                        u["workspace_id"] = None
                        return dict(u)
                return None
            if "SET lead_analyst_user_id" in sql:
                for u in unit_rows:
                    if str(u["unit_id"]) == str(p[2]) and u["status"] == "active":
                        u["lead_analyst_user_id"] = str(p[0])
                        u["owner_user_id"] = str(p[1])
                        return dict(u)
                return None
            for u in unit_rows:
                if str(u["unit_id"]) == str(p[3]) and u["status"] == "active":
                    u["workspace_id"] = str(p[0]).lower()
                    u["lead_analyst_user_id"] = str(p[1])
                    u["owner_user_id"] = str(p[2])
                    return dict(u)
            return None
        raise AssertionError(f"unexpected SQL in bind double: {sql}")

    cur.rowcount = 0
    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone
    cur.fetchall.side_effect = _fetchall

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.return_value = _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(
            _ORIG_GET_DB_CONN, side_effect=lambda conn=None: ctx
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
    assert "FROM dalton_coverage_units" in precheck
    assert "FOR UPDATE" in precheck
    assert "unit_id = %s::uuid AND status = 'active'" in precheck
    assert any(
        "FROM workspaces" in s and "ORDER BY w.created_at, w.workspace_id FOR UPDATE" in s
        for s in executed
    )
    update_sql = next(s for s in executed if s.startswith("UPDATE dalton_coverage_units"))
    assert "unit_id = %s::uuid AND status = 'active'" in update_sql


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
async def test_rebind_refused_in_phase1_and_same_workspace_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Item 2: Phase 1 refuses binding an already-bound unit to a different workspace (409),
    while idempotent same-workspace bind succeeds without changing lead analyst or projections."""
    _install_units_flag(monkeypatch, True)
    ws_new = "00000000-0000-0000-0000-0000000000a2"
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

    same = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_A, _BIND_WS, _BIND_USER)
    assert same["workspace_id"] == _BIND_WS
    assert executed.locks == []

    with pytest.raises(ValueError, match="请先解绑再绑定到新的工作区"):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_SECTOR_A, ws_new, _BIND_USER)
    assert executed.locks == []


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
        raise _friendly_unique_error(_unique_violation("uq_dalton_coverage_units_active_kind_slug"))
    with pytest.raises(ValueError, match="ticker"):
        raise _friendly_unique_error(_unique_violation("uq_dalton_coverage_units_active_ticker_key"))
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
    conn.transaction.return_value = _ConnCtx(conn)
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
    assert params == (
        "company",
        "user-1",
        "user-1",
        "user-1",
        "mega-tech",
        "Mega Tech",
        None,
        "MSFT",
        "MSFT",
        None,
    )

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


def test_batch_lookup_propagates_unmigrated_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 7 Item 5: load_active_units_by_workspace fails closed (propagates
    UndefinedTable) instead of silently degrading when d023/registry fails."""
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
    with pytest.raises(pg_errors.UndefinedTable):
        asyncio.run(cu_db.load_active_units_by_workspace(["ws-1"]))


def test_normalize_ticker_key_rules() -> None:
    from src.dalton.coverage.identity import normalize_ticker_key

    assert normalize_ticker_key(" 600519.SH ") == "600519.SS"
    assert normalize_ticker_key("600519") == "600519.SS"
    assert normalize_ticker_key("000858.sz") == "000858.SZ"
    assert normalize_ticker_key("700") == "00700"
    assert normalize_ticker_key("0700.HK") == "00700"
    assert normalize_ticker_key("00700.hk") == "00700"
    assert normalize_ticker_key("BRK-B") == "BRK.B"
    assert normalize_ticker_key("brk.b") == "BRK.B"
    assert normalize_ticker_key("  nvda ") == "NVDA"
    assert normalize_ticker_key("") is None
    assert normalize_ticker_key(None) is None


def test_d041_migration_constraints_backfill_and_prechecks() -> None:
    import re as _re
    from src.dalton.coverage.identity import normalize_ticker_key
    from src.dalton.migrations.versions import d041_team_coverage_units as d041

    assert d041.down_revision == "d040"
    src = Path(d041.__file__).read_text(encoding="utf-8")
    assert "created_by_user_id" in src
    assert "uq_dalton_coverage_units_active_kind_slug" in src
    assert "uq_dalton_coverage_units_active_ticker_key" in src
    assert "ck_dalton_coverage_units_ticker_key_sync" in src
    assert "idx_dalton_coverage_units_lead_kind" in src
    assert "bindings_version" not in src
    assert ("pending_" + "cleanup") not in src
    assert ("idx_dalton_coverage_units_pending_" + "cleanup") not in src
    # Items 1, 7, 9, 11 SQL checks: automations.scope only (never dalton- prefix), POSIX ^\s*$ whitespace check
    assert "a.scope IN ('research_tracking', 'company')" in src
    assert "LEFT(LOWER(" not in src
    assert "a.dalton_scope" not in src
    assert r"~ '^\\s*$'" in src

    # Verify runtime SQL emitted by backfill_d041 contains literal PostgreSQL regex ~ '^\s*$'
    emitted_sql: list[str] = []

    class _SqlCheckResult:
        def fetchall(self) -> list[Any]:
            return []

    class _SqlCheckConn:
        def execute(self, stmt: Any, params: Any = None) -> _SqlCheckResult:
            emitted_sql.append(str(getattr(stmt, "text", stmt)))
            return _SqlCheckResult()

    d041.backfill_d041(_SqlCheckConn())
    assert any(r"~ '^\s*$'" in s for s in emitted_sql)

    # Item 9: whitespace-only tickers (including tabs/newlines) agree across Python & SQL regex
    sql_ws_re = _re.compile(r"^\s*$")
    ws_samples = ["", "   ", "\t", "\n", " \t\r\n "]
    for sample in ws_samples:
        assert normalize_ticker_key(sample) is None
        assert sql_ws_re.match(sample) is not None
        bf = d041.compute_backfill_rows(
            [{"unit_id": "ws-u", "kind": "company", "owner_user_id": "u1", "ticker": sample, "workspace_id": None, "status": "active"}]
        )
        assert bf == [
            {
                "unit_id": "ws-u",
                "created_by_user_id": "u1",
                "lead_analyst_user_id": "u1",
                "ticker": None,
                "ticker_key": None,
            }
        ]

    # Backfill: prefers active workspace owner for lead_analyst_user_id while preserving creator in created_by_user_id
    rows = [
        {
            "unit_id": "u-1",
            "kind": "company",
            "owner_user_id": "creator",
            "ticker": "0700.HK",
            "workspace_id": "ws-1",
            "status": "active",
        },
        {
            "unit_id": "u-2",
            "kind": "sector",
            "owner_user_id": "creator",
            "ticker": None,
            "workspace_id": None,
            "status": "archived",
        },
        {
            "unit_id": "u-3",
            "kind": "company",
            "owner_user_id": "creator",
            "ticker": "\t  \n",
            "workspace_id": None,
            "status": "active",
        },
    ]
    backfilled = d041.compute_backfill_rows(
        rows,
        workspaces_by_id={"ws-1": {"user_id": "ws-owner", "status": "active"}},
    )
    assert backfilled == [
        {
            "unit_id": "u-1",
            "created_by_user_id": "creator",
            "lead_analyst_user_id": "ws-owner",
            "ticker": "0700.HK",
            "ticker_key": "00700",
        },
        {
            "unit_id": "u-2",
            "created_by_user_id": "creator",
            "lead_analyst_user_id": "creator",
            "ticker": None,
            "ticker_key": None,
        },
        {
            "unit_id": "u-3",
            "created_by_user_id": "creator",
            "lead_analyst_user_id": "creator",
            "ticker": None,
            "ticker_key": None,
        },
    ]

    # Upgrade conflict precheck: catches cross-user slug & ticker_key conflicts across active rows only
    slug_conf, ticker_conf = d041.detect_upgrade_conflicts(
        [
            {"unit_id": "1", "owner_user_id": "u1", "kind": "company", "slug": "tencent", "ticker": "0700.HK", "ticker_key": "00700", "status": "active", "workspace_id": "w1"},
            {"unit_id": "2", "owner_user_id": "u2", "kind": "company", "slug": "tencent", "ticker": "700", "ticker_key": "00700", "status": "active", "workspace_id": "w2"},
            {"unit_id": "3", "owner_user_id": "u3", "kind": "company", "slug": "tencent", "ticker": "700", "ticker_key": "00700", "status": "archived", "workspace_id": None},
        ]
    )
    assert {r["unit_id"] for r in slug_conf} == {"1", "2"}
    assert {r["unit_id"] for r in ticker_conf} == {"1", "2"}
    err = d041.format_upgrade_conflict_error(slug_conf, ticker_conf)
    assert err is not None and "tencent" in err and "00700" in err

    # Item 7 downgrade conflict precheck: blocks scope='research_tracking' / 'company', NEVER blocks scope='personal' even named 'dalton-*'
    dg = d041.detect_downgrade_conflicts(
        [
            {"unit_id": "u1", "owner_user_id": "alice", "lead_analyst_user_id": "alice", "kind": "sector", "slug": "semi", "status": "active", "workspace_id": None},
            {"unit_id": "u2", "owner_user_id": "alice", "lead_analyst_user_id": "alice", "kind": "sector", "slug": "semi", "status": "archived", "workspace_id": None},
            {"unit_id": "u3", "owner_user_id": "bob", "lead_analyst_user_id": "bob", "kind": "company", "slug": "nvda", "status": "active", "workspace_id": "ws-c"},
        ],
        workspaces_by_id={"ws-c": {"user_id": "carol", "status": "active"}},
        automations=[
            {"automation_id": "a1", "user_id": "bob", "workspace_id": "ws-c", "name": "research-watch", "scope": "research_tracking", "status": "active", "metadata": {}},
            {"automation_id": "a2", "user_id": "bob", "workspace_id": "ws-c", "name": "company-brief", "scope": "company", "status": "active", "metadata": {}},
            {"automation_id": "a-personal", "user_id": "bob", "workspace_id": "ws-c", "name": "dalton-nvda-stolen", "scope": "personal", "status": "active", "metadata": {}},
        ],
    )
    assert len(dg["owner_slug_duplicates"]) == 2
    assert len(dg["cross_owner_bindings"]) == 1
    assert {a["automation_id"] for a in dg["colleague_automations"]} == {"a1", "a2"}
    assert d041.format_downgrade_conflict_error(dg) is not None


@pytest.mark.asyncio
async def test_round9_unbind_and_archive_refuse_active_managed_tasks_and_unstarted_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Item 1: unbind/archive precheck & post-lock read-only recheck refuse 409
    when active/executing managed tasks or unstarted executions exist (including ticker-only & earn-*),
    without modifying any automations or executions."""
    from src.dalton.coverage.unit_errors import classify_coverage_unit_error

    _install_units_flag(monkeypatch, True)
    ws_a = "11111111-1111-1111-1111-111111111111"
    unit_row = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN,
        kind="company",
        slug="nvda",
        ticker="NVDA",
        workspace_id=ws_a,
    )
    blocking_rows: list[dict[str, Any]] = [
        {"user_id": _BIND_USER, "name": "dalton-nvda-watch", "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"}},
        {"user_id": _BIND_USER, "name": "dalton-nvda-delta", "metadata": {"dalton_kind": "weekly_delta", "ticker": "NVDA"}},
        {"user_id": _BIND_USER, "name": "dalton-nvda-move-up", "metadata": {"dalton_kind": "price_move_up", "ticker": "NVDA"}},
        {"user_id": _BIND_USER, "name": "dalton-nvda-move-down", "metadata": {"dalton_kind": "price_move_down", "ticker": "NVDA"}},
        {"user_id": _BIND_USER, "name": "dalton-nvda-earn-2026q4-t0", "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"}},
        {"user_id": _BIND_USER, "name": "custom-nvda-extra", "metadata": {"dalton_kind": "company_watch", "unit_id": _BIND_UNIT_OWN}},
    ]
    executed_sql: list[str] = []

    class _BlockCur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _BlockCur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            executed_sql.append(q)
            if "FROM dalton_coverage_units" in q and "WHERE unit_id = %s::uuid" in q:
                self._rows = [dict(unit_row)] if unit_row else []
            elif "FROM workspaces" in q:
                self._rows = [{"workspace_id": ws_a, "user_id": _BIND_USER, "status": "active", "config": {}}]
            elif "FROM automations" in q:
                assert "FOR SHARE" not in q
                assert "FOR UPDATE" not in q
                self._rows = list(blocking_rows[:6])
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return list(self._rows)

    from psycopg import AsyncConnection

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = lambda *a, **kw: _BlockCur()
    conn.transaction.side_effect = lambda *a, **kw: _ConnCtx(conn)
    monkeypatch.setattr(cu_db, "get_db_connection", create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn_arg=None: _ConnCtx(conn)))

    # 1. Precheck fast-fails 409 before FOR UPDATE, lists up to 5 names + disable_tracking hint
    with pytest.raises(ValueError) as unbind_exc:
        await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert classify_coverage_unit_error(unbind_exc.value) == ("active_tracking_blocks_unbind", 409)
    msg_u = str(unbind_exc.value)
    assert "请先关闭该单元的跟踪（会暂停相关任务），再解绑" in msg_u
    assert "dalton-nvda-watch" in msg_u and "dalton-nvda-earn-2026q4-t0" in msg_u
    assert "custom-nvda-extra" not in msg_u and " 等" in msg_u
    assert not any("UPDATE dalton_coverage_units" in s for s in executed_sql)
    assert not any("FOR UPDATE" in s for s in executed_sql)

    executed_sql.clear()
    with pytest.raises(ValueError) as archive_exc:
        await cu_db.set_coverage_unit_status(_BIND_UNIT_OWN, _BIND_USER, "archived")
    assert classify_coverage_unit_error(archive_exc.value) == ("active_tracking_blocks_unbind", 409)
    assert "请先关闭该单元的跟踪（会暂停相关任务），再归档" in str(archive_exc.value)
    assert not any("UPDATE dalton_coverage_units" in s for s in executed_sql)

    # 2. Race between precheck and FOR UPDATE is caught by read-only recheck inside FOR UPDATE tx
    call_count = {"auto": 0}

    class _RaceCur(_BlockCur):
        async def execute(self, sql: str, params: Any = None) -> None:
            await super().execute(sql, params)
            q = " ".join(str(sql).split())
            if "FROM automations" in q:
                call_count["auto"] += 1
                self._rows = (
                    []
                    if call_count["auto"] == 1
                    else [{"user_id": _BIND_USER, "name": "dalton-nvda-watch", "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"}}]
                )

    conn.cursor.side_effect = lambda *a, **kw: _RaceCur()
    executed_sql.clear()
    with pytest.raises(ValueError) as race_exc:
        await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert classify_coverage_unit_error(race_exc.value) == ("active_tracking_blocks_unbind", 409)
    assert call_count["auto"] == 2
    assert any("FROM dalton_coverage_units" in s and "FOR UPDATE" in s for s in executed_sql)
    assert not any("UPDATE dalton_coverage_units" in s for s in executed_sql)

    # 3. Once tracking is disabled (0 blocking rows), unbind succeeds without mutating automations
    blocking_rows.clear()
    conn.cursor.side_effect = lambda *a, **kw: _BlockCur()
    _install_bind_db(
        monkeypatch,
        unit_rows=[unit_row],
        ws_rows={ws_a: {"workspace_id": ws_a, "user_id": _BIND_USER, "status": "active", "config": {}}},
        active_binding_by_ws={},
    )
    unbound = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert unbound is not None and unbound["workspace_id"] is None


@pytest.mark.asyncio
async def test_round10_unbind_blocking_uses_python_ticker_alias_and_ignores_colleague_personal_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Items 1 & 2 (Codex H2 & M1):
    - SQL fetches workspace/lead candidates without ticker predicates; Python runs
      `is_managed_unit_automation_for_unit`, so `ticker: '600519'` blocks `600519.SS` unbind.
    - Colleague-owned tasks on the same workspace belong to their personal scope and do not block.
    """
    from psycopg import AsyncConnection
    from src.dalton.coverage.unit_errors import classify_coverage_unit_error

    _install_units_flag(monkeypatch, True)
    ws_cn = "11111111-1111-4111-8111-111111111111"
    unit_cn = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN,
        kind="company",
        slug="kweichow-moutai",
        ticker="600519.SS",
        workspace_id=ws_cn,
        owner_user_id=_BIND_USER,
    )
    unit_cn["ticker_key"] = "600519.SS"
    unit_cn["lead_analyst_user_id"] = _BIND_USER

    candidate_rows: list[dict[str, Any]] = [
        # Colleague personal lookalike / same-ticker task (different user_id) -> must not block
        {
            "automation_id": "a-colleague",
            "user_id": "u-colleague",
            "name": "dalton-600519-watch",
            "status": "active",
            "workspace_id": ws_cn,
            "metadata": {"dalton_kind": "company_watch", "ticker": "600519"},
        },
        # Lead analyst non-managed personal task -> must not block
        {
            "automation_id": "a-personal",
            "user_id": _BIND_USER,
            "name": "my-moutai-notes",
            "status": "active",
            "workspace_id": ws_cn,
            "metadata": {},
        },
        # Lead analyst ticker-alias managed task (`600519` without `.SS`) -> must block
        {
            "automation_id": "a-alias-600519",
            "user_id": _BIND_USER,
            "name": "dalton-600519-watch",
            "status": "active",
            "workspace_id": ws_cn,
            "metadata": {"ticker": "600519"},
        },
    ]
    executed_auto_sql: list[tuple[str, Any]] = []

    class _CnCur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _CnCur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            if "FROM dalton_coverage_units" in q and "WHERE unit_id = %s::uuid" in q:
                self._rows = [dict(unit_cn)]
            elif "FROM workspaces" in q:
                self._rows = [
                    {
                        "workspace_id": ws_cn,
                        "user_id": _BIND_USER,
                        "status": "active",
                        "config": {},
                    }
                ]
            elif "FROM automations" in q:
                executed_auto_sql.append((q, params))
                row_uid = str(params[1]) if params and len(params) > 1 else _BIND_USER
                self._rows = [
                    dict(r)
                    for r in candidate_rows
                    if str(r.get("user_id") or "") == row_uid or "u-colleague" in str(r.get("user_id"))
                ]
            elif q.startswith("UPDATE dalton_coverage_units"):
                unit_cn["workspace_id"] = None
                self._rows = [dict(unit_cn)]
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return list(self._rows)

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = lambda *a, **kw: _CnCur()
    conn.transaction.side_effect = lambda *a, **kw: _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn_arg=None: _ConnCtx(conn)),
    )

    with pytest.raises(cu_db.CoverageUnitConflictError) as exc_info:
        await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert classify_coverage_unit_error(exc_info.value) == ("active_tracking_blocks_unbind", 409)
    assert "dalton-600519-watch" in str(exc_info.value)
    assert exc_info.value.conflict["blocking_automations"] == ["dalton-600519-watch"]
    assert exc_info.value.conflict["blocking_automations_count"] == 1
    assert executed_auto_sql
    sql_q, sql_params = executed_auto_sql[0]
    assert "AND a.user_id = %s" in sql_q and sql_params == (ws_cn, _BIND_USER)
    assert "REGEXP_REPLACE" not in sql_q and "600519" not in sql_q

    # Removing lead analyst's alias task leaves colleague task + personal task -> unbind succeeds
    candidate_rows[:] = [r for r in candidate_rows if r["automation_id"] != "a-alias-600519"]
    unbound = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    assert unbound is not None and unbound["workspace_id"] is None


@pytest.mark.asyncio
async def test_round10_unbind_409_hides_task_names_without_web_read_and_normalizes_bind_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Items 6 & 7 (Codex M5 & L1):
    - PM without Web read access on a hidden workspace gets count-only 409 message & metadata.
    - Unhyphenated / uppercase UUID matching current binding is normalized and treated as idempotent.
    """
    from fastapi import HTTPException
    from psycopg import AsyncConnection
    import src.dalton.invites as invites_mod
    import src.dalton.settings as dalton_settings
    import src.dalton.sharing as sharing_mod
    from src.dalton.coverage import units as units_svc

    _install_units_flag(monkeypatch, True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    pm_user = "pm-admin"
    ws_hidden = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        invites_mod,
        "is_invite_pm",
        create_autospec(invites_mod.is_invite_pm, side_effect=lambda u: str(u) == pm_user),
    )

    unit_hidden = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN,
        kind="company",
        slug="nvda",
        ticker="NVDA",
        workspace_id=ws_hidden,
        owner_user_id="u-lead",
    )
    unit_hidden["ticker_key"] = "NVDA"
    unit_hidden["lead_analyst_user_id"] = "u-lead"

    class _HideCur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _HideCur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            if "FROM dalton_coverage_units" in q and "WHERE unit_id = %s::uuid" in q and "unit_id <>" not in q:
                self._rows = [dict(unit_hidden)]
            elif "FROM workspaces" in q:
                self._rows = [{"workspace_id": ws_hidden, "user_id": "u-lead", "status": "active", "config": {}}]
            elif "FROM automations" in q:
                self._rows = [
                    {"user_id": "u-lead", "name": "dalton-nvda-watch", "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"}},
                    {"user_id": "u-lead", "name": "dalton-nvda-earn-2026q4-t0", "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"}},
                ]
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return list(self._rows)

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = lambda *a, **kw: _HideCur()
    conn.transaction.side_effect = lambda *a, **kw: _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn_arg=None: _ConnCtx(conn)),
    )
    monkeypatch.setattr(
        cu_db,
        "_require_ws_write",
        create_autospec(cu_db._require_ws_write, return_value=None),
    )
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(
            sharing_mod.require_workspace_access,
            side_effect=HTTPException(status_code=404, detail="Workspace not found"),
        ),
    )

    for call in (
        cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, pm_user),
        cu_db.set_coverage_unit_status(_BIND_UNIT_OWN, pm_user, "archived"),
    ):
        with pytest.raises(cu_db.CoverageUnitConflictError) as exc_info:
            await call
        msg = str(exc_info.value)
        assert "该单元下仍有 2 个运行中的受管任务" in msg
        assert "dalton-nvda-watch" not in msg
        assert "dalton-nvda-earn-2026q4-t0" not in msg
        assert "blocking_automations" not in exc_info.value.conflict
        assert exc_info.value.conflict["blocking_automations_count"] == 2

    # Unhyphenated uppercase UUID matching current binding is normalized -> idempotent (no 409)
    unhyphenated = "11111111111141118111111111111111".upper()
    res_svc = await units_svc.bind_coverage_unit_workspace(_BIND_UNIT_OWN, unhyphenated, pm_user)
    assert res_svc["workspace_id"] is None  # redacted because PM has no Web read access
    res_db = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, unhyphenated, pm_user)
    assert res_db["workspace_id"] == ws_hidden


@pytest.mark.asyncio
async def test_register_with_workspace_and_historical_duplicate_and_savepoint_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register + workspace_id locks workspace FOR UPDATE, rejects duplicate, and returns redacted 409 on SAVEPOINT violation."""
    from fastapi import HTTPException
    from psycopg import AsyncConnection
    import src.dalton.coverage.service as cov_svc
    import src.dalton.sharing as sharing_mod

    orig_req_ws_access = sharing_mod.require_workspace_access

    _install_units_flag(monkeypatch, True)
    dup_ws = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    executed = _install_bind_db(
        monkeypatch,
        unit_rows=[],
        ws_rows={
            _BIND_WS: {"workspace_id": _BIND_WS, "user_id": _BIND_USER, "name": "Mine", "status": "active", "config": {}},
            dup_ws: {
                "workspace_id": dup_ws,
                "user_id": _BIND_USER,
                "name": "Dup",
                "status": "active",
                "config": {"dalton_historical_duplicate_of": _BIND_WS},
            },
        },
        active_binding_by_ws={},
    )
    sync_cov = create_autospec(cov_svc.sync_lead_analyst_personal_coverage)
    monkeypatch.setattr(cov_svc, "sync_lead_analyst_personal_coverage", sync_cov)

    with pytest.raises(ValueError, match="historical duplicate workspace"):
        await register_coverage_unit(
            _BIND_USER,
            kind="company",
            slug="anthropic",
            workspace_id=dup_ws,
        )

    # Mock cursor for insert + SAVEPOINT UniqueViolation
    cur = create_autospec(_BindCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    last: dict[str, Any] = {"sql": ""}

    class _Diag:
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    class _FakeUniqueViolation(pg_errors.UniqueViolation):
        @property
        def diag(self) -> Any:  # type: ignore[override]
            return _Diag("uq_dalton_coverage_units_active_ticker_key")

    async def _exec(sql: str, params: Any = None) -> None:
        norm = " ".join(str(sql).split())
        last["sql"] = norm
        if norm.startswith("INSERT INTO dalton_coverage_units"):
            raise _FakeUniqueViolation("duplicate key value violates unique constraint")

    async def _fetchone() -> Any:
        if "FROM dalton_coverage_units" in last["sql"]:
            return {
                "unit_id": _BIND_UNIT_OWN,
                "lead_analyst_user_id": _BIND_OTHER_USER,
                "workspace_id": "99999999-9999-9999-9999-999999999999",
            }
        return None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone
    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.return_value = _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )

    async def _deny_read(ws_id: str, uid: str, **kw: Any) -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(orig_req_ws_access, side_effect=_deny_read),
    )

    with pytest.raises(cu_db.CoverageUnitConflictError) as exc_info:
        await cu_db.insert_coverage_unit(
            kind="company",
            owner_user_id=_BIND_USER,
            slug="nvda",
            ticker="NVDA",
        )
    assert exc_info.value.conflict == {
        "unit_id": _BIND_UNIT_OWN,
        "lead_analyst_user_id": _BIND_OTHER_USER,
    }
    assert "workspace_id" not in exc_info.value.conflict
    assert executed is not None


@pytest.mark.asyncio
async def test_cross_user_bind_rebind_transfer_and_pm_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Colleague rw bind, rebind needing old+target write, and PM override rules."""
    import src.dalton.coverage.service as cov_svc

    _install_units_flag(monkeypatch, True)
    ws_old = "11111111-1111-1111-1111-111111111111"
    ws_new = "22222222-2222-2222-2222-222222222222"
    unit = _bind_unit_row(unit_id=_BIND_UNIT_OWN, kind="company", slug="nvda", ticker="NVDA")
    _install_bind_db(
        monkeypatch,
        unit_rows=[unit],
        ws_rows={
            ws_old: {"workspace_id": ws_old, "user_id": "owner-a", "status": "active", "config": {}},
            ws_new: {"workspace_id": ws_new, "user_id": "owner-b", "status": "active", "config": {}},
        },
        active_binding_by_ws={},
        colleague_rw_by_user={
            _BIND_USER: {ws_old, ws_new},
            "recipient-rw": {ws_new},
            "ro-user": set(),
        },
        internal_users={_BIND_USER, "recipient-rw", "ro-user", "owner-b", "pm-user"},
        pm_users={"pm-user"},
    )
    sync_cov = create_autospec(cov_svc.sync_lead_analyst_personal_coverage)
    monkeypatch.setattr(cov_svc, "sync_lead_analyst_personal_coverage", sync_cov)

    # First bind to colleague rw workspace sets lead_analyst = ws owner ("owner-a") and syncs §6 #2
    bound = await bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_old, _BIND_USER)
    assert bound["lead_analyst_user_id"] == "owner-a"
    sync_cov.assert_awaited()

    # Non-writable user cannot bind / rebind
    with pytest.raises(ValueError, match="workspace write permission required"):
        await bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, "ro-user")

    # Binding an already-bound unit to a different workspace is refused (409) in Phase 1
    with pytest.raises(ValueError, match="请先解绑再绑定到新的工作区"):
        await bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, _BIND_USER)

    # Item 3: duplicate bind to SAME workspace is idempotent and preserves lead analyst ("owner-a")
    same_bind = await bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_old, _BIND_USER)
    assert same_bind["lead_analyst_user_id"] == "owner-a"

    # Unbind first, then current lead ("owner-a") or PM binds to ws_new, transitioning lead to "owner-b"
    await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_USER)
    bound_new = await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, "pm-user")
    assert bound_new["workspace_id"] == ws_new
    assert bound_new["lead_analyst_user_id"] == "owner-b"

    # Item 3 & Item 6: when previous workspace is deleted, unrelated rw collaborator cannot hijack unit,
    # while original creator (`created_by_user_id`, even after owner_user_id shifted to ws_old owner) CAN unbind then bind!
    ws_rows_deleted = {
        ws_old: {"workspace_id": ws_old, "user_id": "owner-a", "status": "deleted", "config": {}},
        ws_new: {"workspace_id": ws_new, "user_id": "owner-b", "status": "active", "config": {}},
    }
    hijack_unit = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN,
        owner_user_id="owner-a",
        created_by_user_id="orig-creator",
        lead_analyst_user_id="owner-a",
        workspace_id=ws_old,
    )
    _install_bind_db(
        monkeypatch,
        unit_rows=[hijack_unit],
        ws_rows=ws_rows_deleted,
        active_binding_by_ws={},
        colleague_rw_by_user={_BIND_USER: {ws_new}, "orig-creator": {ws_new}},
        internal_users={_BIND_USER, "orig-creator", "owner-a", "owner-b"},
        pm_users=set(),
    )

    with pytest.raises(ValueError) as del_bind_exc:
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, _BIND_USER)
    assert str(del_bind_exc.value) == f"active coverage unit {_BIND_UNIT_OWN} not found for this owner"

    # Original creator (`created_by_user_id`) CAN unbind away from deleted old workspace (first bind only allows current lead/PM)
    unbound_deleted = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, "orig-creator")
    assert unbound_deleted is not None and unbound_deleted["workspace_id"] is None

    # First bind (unbound unit) allows ONLY current lead_analyst_user_id or PM (§2.2), NOT stale creator/owner
    unbound_transferred = _bind_unit_row(
        unit_id=_BIND_UNIT_OWN,
        owner_user_id="old-owner",
        created_by_user_id="orig-creator",
        lead_analyst_user_id="current-lead",
        workspace_id=None,
    )
    _install_bind_db(
        monkeypatch,
        unit_rows=[unbound_transferred],
        ws_rows={ws_new: {"workspace_id": ws_new, "user_id": "owner-b", "status": "active", "config": {}}},
        active_binding_by_ws={},
        colleague_rw_by_user={"orig-creator": {ws_new}, "old-owner": {ws_new}},
        internal_users={"orig-creator", "old-owner", "current-lead", "owner-b"},
        pm_users=set(),
    )
    with pytest.raises(ValueError, match="not found for this owner"):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, "orig-creator")
    with pytest.raises(ValueError, match="not found for this owner"):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, "old-owner")

    # Restore unit with active ws_new for PM unbind check
    _install_bind_db(
        monkeypatch,
        unit_rows=[_bind_unit_row(unit_id=_BIND_UNIT_OWN, workspace_id=ws_new)],
        ws_rows={ws_new: {"workspace_id": ws_new, "user_id": "owner-b", "status": "active", "config": {}}},
        active_binding_by_ws={},
        pm_users={"pm-user"},
    )
    # PM override can unbind even without direct ws sharing
    unbound = await cu_db.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, "pm-user")
    assert unbound["workspace_id"] is None


@pytest.mark.asyncio
async def test_db_delete_workspace_rejects_active_coverage_unit_under_for_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6: server.database.workspace.delete_workspace calls has_active_coverage_unit under FOR UPDATE."""
    from psycopg import AsyncConnection
    from src.dalton.database import publications as pub_db
    from src.server.database import egress_grants as egress_db
    from src.server.database import workspace as ws_db

    orig_ws_get_db_conn = ws_db.get_db_connection
    executed: list[str] = []
    cur = create_autospec(_BindCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append(" ".join(str(sql).split()))

    async def _fetchone() -> Any:
        sql = executed[-1]
        if sql.startswith("SELECT user_id FROM workspaces WHERE workspace_id"):
            return {"user_id": _BIND_USER}
        if "FROM workspaces WHERE workspace_id = %s FOR UPDATE" in sql:
            return {"workspace_id": _BIND_WS, "computer_id": None}
        if "FROM dalton_coverage_units" in sql:
            return {"has_unit": True}
        return None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.return_value = _ConnCtx(conn)

    ctx = _ConnCtx(conn)
    monkeypatch.setattr(ws_db, "get_db_connection", create_autospec(orig_ws_get_db_conn, side_effect=lambda conn=None: ctx))
    monkeypatch.setattr(egress_db, "lock_user_egress_state", create_autospec(egress_db.lock_user_egress_state))
    monkeypatch.setattr(ws_db, "workspace_has_live_runs", create_autospec(ws_db.workspace_has_live_runs, return_value=False))
    monkeypatch.setattr(ws_db, "workspace_is_moving", create_autospec(ws_db.workspace_is_moving, return_value=False))
    monkeypatch.setattr(pub_db, "has_published_reports", create_autospec(pub_db.has_published_reports, return_value=False))

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    with pytest.raises(ws_db.WorkspaceBoundToCoverageUnitError, match=ws_db.ACTIVE_COVERAGE_UNIT_DELETE_REFUSAL):
        await ws_db.delete_workspace(_BIND_WS)
    assert any("FROM dalton_coverage_units" in s for s in executed)


@pytest.mark.asyncio
async def test_check_order_write_before_historical_duplicate_and_auth_off_real_sharing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8 & Item 13: unauthorized user gets write error before duplicate refusal, and closed-auth real require_workspace_access blocks non-owners even if PM."""
    import src.dalton.access_tiers as tiers_mod
    import src.dalton.settings as settings_mod
    from src.server.database import workspace as ws_db

    # 1) Write check runs BEFORE historical duplicate refusal on hidden/unowned dup workspace
    dup_ws = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    _install_bind_db(
        monkeypatch,
        unit_rows=[
            _bind_unit_row(
                unit_id=_BIND_UNIT_OWN,
                owner_user_id="stranger",
                lead_analyst_user_id="stranger",
            )
        ],
        ws_rows={
            dup_ws: {
                "workspace_id": dup_ws,
                "user_id": "owner-other",
                "status": "active",
                "config": {"dalton_historical_duplicate_of": _BIND_WS},
            }
        },
        active_binding_by_ws={},
        colleague_rw_by_user={},
        pm_users=set(),
    )
    with pytest.raises(ValueError, match="workspace write permission required"):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, dup_ws, "stranger")

    import src.dalton.sharing as sharing_mod

    # 2) Closed auth: patch BOTH settings_mod and sharing_mod is_dalton_supabase_auth_enabled -> False, run REAL sharing.require_workspace_access!
    monkeypatch.setattr(sharing_mod, "require_workspace_access", _ORIG_REQUIRE_WS_ACCESS)
    sb_false = create_autospec(_ORIG_SUPABASE_AUTH_ENABLED, return_value=False)
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", sb_false)
    monkeypatch.setattr(sharing_mod, "is_dalton_supabase_auth_enabled", sb_false)
    _install_bind_db(
        monkeypatch,
        unit_rows=[
            _bind_unit_row(
                unit_id=_BIND_UNIT_OWN,
                owner_user_id="pm-user",
                lead_analyst_user_id="pm-user",
                workspace_id=None,
            )
        ],
        ws_rows={
            _BIND_WS: {"workspace_id": _BIND_WS, "user_id": "owner-other", "status": "active", "config": {}}
        },
        active_binding_by_ws={},
        internal_users={"pm-user", "owner-other"},
        pm_users={"pm-user"},
        patch_require_access=False,
    )
    monkeypatch.setattr(tiers_mod, "classify_owner_tier", create_autospec(_ORIG_CLASSIFY_OWNER_TIER, return_value="internal"))
    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, return_value={"workspace_id": _BIND_WS, "user_id": "owner-other", "status": "active"}))

    with pytest.raises(ValueError, match="workspace belongs to another user"):
        await cu_db.bind_coverage_unit_workspace(_BIND_UNIT_OWN, _BIND_WS, "pm-user")


@pytest.mark.asyncio
async def test_units_single_tx_rollback_and_reactivate_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 & Item 9: personal coverage failure propagates in same tx (no partial commit); reactivate UniqueViolation -> 409."""
    from psycopg import AsyncConnection
    import src.dalton.coverage.service as cov_svc
    import src.dalton.database.coverage as cov_db
    import src.dalton.invites as invites_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    orig_req_ws_access = sharing_mod.require_workspace_access

    # Single transaction shares the exact same connection between insert_coverage_unit and sync_lead_analyst_personal_coverage
    tx_events: list[str] = []

    class _TxTrack:
        def __init__(self, c: Any) -> None:
            self._c = c

        async def __aenter__(self) -> Any:
            tx_events.append("enter")
            return self._c

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            tx_events.append("rollback" if exc_type else "commit")

    def _enter_tx() -> _TxTrack:
        return _TxTrack(conn)

    def _get_tx_conn(c: Any = None) -> _ConnCtx:
        return ctx

    conn = create_autospec(AsyncConnection, instance=True)
    conn.transaction.side_effect = _enter_tx
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=_get_tx_conn),
    )

    seen_conns: list[Any] = []

    async def _fake_insert(**kw: Any) -> dict[str, Any]:
        seen_conns.append(kw.get("conn"))
        return _bind_unit_row(workspace_id=_BIND_WS, ticker="NVDA")

    async def _failing_sync(
        unit: Any, *, conn: Any = None, acquire_lock: bool = True
    ) -> None:
        seen_conns.append(conn)
        raise RuntimeError("personal coverage write failed")

    monkeypatch.setattr(
        cov_db,
        "acquire_coverage_advisory_lock",
        create_autospec(cov_db.acquire_coverage_advisory_lock),
    )
    monkeypatch.setattr(cu_db, "insert_coverage_unit", create_autospec(cu_db.insert_coverage_unit, side_effect=_fake_insert))
    monkeypatch.setattr(cov_svc, "sync_lead_analyst_personal_coverage", create_autospec(cov_svc.sync_lead_analyst_personal_coverage, side_effect=_failing_sync))

    with pytest.raises(RuntimeError, match="personal coverage write failed"):
        await register_coverage_unit(_BIND_USER, kind="company", slug="nvda", ticker="NVDA", workspace_id=_BIND_WS)
    assert seen_conns == [conn, conn]
    assert tx_events == ["enter", "rollback"]

    # Item 9: set_coverage_unit_status('active') maps UniqueViolation -> CoverageUnitConflictError
    cur = create_autospec(_BindCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    last: dict[str, Any] = {"sql": ""}

    class _Diag:
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    class _ReactivateViolation(pg_errors.UniqueViolation):
        @property
        def diag(self) -> Any:  # type: ignore[override]
            return _Diag("uq_dalton_coverage_units_active_kind_slug")

    async def _exec(sql: str, params: Any = None) -> None:
        norm = " ".join(str(sql).split())
        last["sql"] = norm
        if norm.startswith("UPDATE dalton_coverage_units"):
            raise _ReactivateViolation("duplicate active slug")

    async def _fetchone() -> Any:
        if "FOR UPDATE" in last["sql"] and "FROM dalton_coverage_units" in last["sql"]:
            return {
                "unit_id": _BIND_UNIT_OWN,
                "kind": "company",
                "slug": "nvda",
                "ticker": "NVDA",
                "ticker_key": "NVDA",
                "owner_user_id": _BIND_USER,
                "created_by_user_id": _BIND_USER,
                "lead_analyst_user_id": _BIND_USER,
                "workspace_id": None,
                "status": "archived",
            }
        if "FROM dalton_coverage_units" in last["sql"]:
            return {
                "unit_id": _BIND_UNIT_OCCUPIER,
                "lead_analyst_user_id": _BIND_OTHER_USER,
                "workspace_id": None,
            }
        return None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone
    conn2 = create_autospec(AsyncConnection, instance=True)
    ctx2 = _ConnCtx(conn2)

    def _enter_tx2() -> _ConnCtx:
        return _ConnCtx(conn2)

    def _get_tx_conn2(c: Any = None) -> _ConnCtx:
        return ctx2

    conn2.cursor.return_value = cur
    conn2.transaction.side_effect = _enter_tx2
    monkeypatch.setattr(cu_db, "get_db_connection", create_autospec(_ORIG_GET_DB_CONN, side_effect=_get_tx_conn2))
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(invites_mod, "is_invite_pm", create_autospec(_ORIG_IS_INVITE_PM, return_value=False))
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", create_autospec(_ORIG_SUPABASE_AUTH_ENABLED, return_value=True))
    monkeypatch.setattr(sharing_mod, "require_workspace_access", create_autospec(orig_req_ws_access))

    with pytest.raises(cu_db.CoverageUnitConflictError) as conflict_exc:
        await cu_db.set_coverage_unit_status(_BIND_UNIT_OWN, _BIND_USER, "active")
    assert conflict_exc.value.conflict == {
        "unit_id": _BIND_UNIT_OCCUPIER,
        "lead_analyst_user_id": _BIND_OTHER_USER,
    }


@pytest.mark.asyncio
async def test_units_uses_in_lock_state_and_sorted_advisory_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 3 & 4: service uses locked pre-state from FOR UPDATE (not stale pre-lock read) and acquires dalton_coverage locks in sorted uid order."""
    from psycopg import AsyncConnection
    import src.dalton.coverage.service as cov_svc
    import src.dalton.database.coverage as cov_db

    events: list[tuple[str, str]] = []
    conn = create_autospec(AsyncConnection, instance=True)

    def _enter_tx() -> _ConnCtx:
        return _ConnCtx(conn)

    def _get_conn(c: Any = None) -> _ConnCtx:
        return _ConnCtx(conn)

    conn.transaction.side_effect = _enter_tx
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=_get_conn),
    )

    async def _lock_cov(c: Any, uid: str) -> None:
        events.append(("lock", uid))

    async def _clear_cov(unit: Any, *, conn: Any = None, acquire_lock: bool = True) -> None:
        lead = str(unit.get("lead_analyst_user_id") or "")
        events.append(("clear", lead))

    async def _sync_cov(unit: Any, *, conn: Any = None, acquire_lock: bool = True) -> None:
        lead = str(unit.get("lead_analyst_user_id") or "")
        events.append(("sync", lead))

    # Pre-lock get_coverage_unit must not be consulted for personal coverage cleanup
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            side_effect=AssertionError("pre-lock get_coverage_unit must not be called"),
        ),
    )
    monkeypatch.setattr(
        cov_db,
        "acquire_coverage_advisory_lock",
        create_autospec(cov_db.acquire_coverage_advisory_lock, side_effect=_lock_cov),
    )
    monkeypatch.setattr(
        cov_svc,
        "clear_unit_from_personal_coverage",
        create_autospec(cov_svc.clear_unit_from_personal_coverage, side_effect=_clear_cov),
    )
    monkeypatch.setattr(
        cov_svc,
        "sync_lead_analyst_personal_coverage",
        create_autospec(cov_svc.sync_lead_analyst_personal_coverage, side_effect=_sync_cov),
    )

    # Bind (unbound -> bound) uses locked row from FOR UPDATE and locks personal coverage for new lead "amy"
    async def _bind_to_amy(
        unit_id: str,
        workspace_id: str,
        actor_user_id: str,
        *,
        conn: Any = None,
        previous_out: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if previous_out is not None:
            previous_out.update(
                _bind_unit_row(
                    unit_id=unit_id,
                    owner_user_id="zoe",
                    lead_analyst_user_id="zoe",
                    workspace_id=None,
                    ticker="NVDA",
                )
            )
        return _bind_unit_row(
            unit_id=unit_id,
            owner_user_id="amy",
            lead_analyst_user_id="amy",
            workspace_id=workspace_id,
            ticker="NVDA",
        )

    orig_bind_db = cu_db.bind_coverage_unit_workspace
    monkeypatch.setattr(
        cu_db,
        "bind_coverage_unit_workspace",
        create_autospec(orig_bind_db, side_effect=_bind_to_amy),
    )
    ws_new = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    await units_mod.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_new, "zoe")
    assert events == [
        ("lock", "amy"),
        ("sync", "amy"),
    ]

    # Sorted advisory lock helper orders ("zoe", "amy") -> ("amy", "zoe")
    events.clear()
    await units_mod._acquire_sorted_personal_coverage_locks(conn, ["zoe", "amy"])
    assert events == [
        ("lock", "amy"),
        ("lock", "zoe"),
    ]

    # Unbind uses locked previous_out from FOR UPDATE (not stale pre-lock read) and clears "zoe"
    events.clear()

    async def _unbind_zoe(
        unit_id: str,
        actor_user_id: str,
        *,
        conn: Any = None,
        previous_out: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if previous_out is not None:
            previous_out.update(
                _bind_unit_row(
                    unit_id=unit_id,
                    owner_user_id="zoe",
                    lead_analyst_user_id="zoe",
                    workspace_id=_BIND_WS,
                    ticker="NVDA",
                )
            )
        return _bind_unit_row(
            unit_id=unit_id,
            owner_user_id="zoe",
            lead_analyst_user_id="zoe",
            workspace_id=None,
            ticker="NVDA",
        )

    monkeypatch.setattr(
        cu_db,
        "unbind_coverage_unit_workspace",
        create_autospec(cu_db.unbind_coverage_unit_workspace, side_effect=_unbind_zoe),
    )
    await units_mod.unbind_coverage_unit_workspace(_BIND_UNIT_OWN, "zoe")
    assert events == [
        ("lock", "zoe"),
        ("clear", "zoe"),
    ]


@pytest.mark.asyncio
async def test_two_tx_concurrent_register_bind_and_soft_delete_mutual_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 13 (C6): two concurrent real transactions (register+workspace vs delete_workspace) serialize on workspaces FOR UPDATE in both winner orders."""
    import asyncio
    import src.dalton.access_tiers as tiers_mod
    import src.dalton.database.publications as pub_db
    import src.dalton.invites as invites_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod
    from src.server.database import egress_grants as egress_db
    from src.server.database import workspace as ws_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(invites_mod, "is_invite_pm", create_autospec(_ORIG_IS_INVITE_PM, return_value=False))
    monkeypatch.setattr(tiers_mod, "classify_owner_tier", create_autospec(_ORIG_CLASSIFY_OWNER_TIER, return_value="internal"))
    sb_true = create_autospec(_ORIG_SUPABASE_AUTH_ENABLED, return_value=True)
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", sb_true)
    monkeypatch.setattr(sharing_mod, "is_dalton_supabase_auth_enabled", sb_true)
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(_ORIG_REQUIRE_WS_ACCESS, return_value={"workspace_id": _BIND_WS, "user_id": _BIND_USER}),
    )
    monkeypatch.setattr(egress_db, "lock_user_egress_state", create_autospec(egress_db.lock_user_egress_state))
    monkeypatch.setattr(egress_db, "retire_workspace_grants", create_autospec(egress_db.retire_workspace_grants))
    monkeypatch.setattr(ws_db, "drop_workspace_links", create_autospec(ws_db.drop_workspace_links))
    monkeypatch.setattr(ws_db, "publish_status_change", create_autospec(ws_db.publish_status_change))
    monkeypatch.setattr(ws_db, "workspace_has_live_runs", create_autospec(ws_db.workspace_has_live_runs, return_value=False))
    monkeypatch.setattr(ws_db, "workspace_is_moving", create_autospec(ws_db.workspace_is_moving, return_value=False))
    monkeypatch.setattr(pub_db, "has_published_reports", create_autospec(pub_db.has_published_reports, return_value=False))

    async def _run_scenario(delete_first: bool) -> None:
        ws_state = {
            "workspace_id": _BIND_WS,
            "user_id": _BIND_USER,
            "status": "active",
            "config": {},
            "computer_id": None,
            "dir_name": None,
        }
        units_committed: list[dict[str, Any]] = []
        ws_row_lock = asyncio.Lock()
        first_entered = asyncio.Event()

        class _TxConn:
            def __init__(self, role: str) -> None:
                self._role = role
                self._held_lock = False
                self._sql = ""
                self._params: tuple[Any, ...] = ()
                self._pending_unit: dict[str, Any] | None = None
                self._pending_deleted = False

            def cursor(self, **kw: Any) -> "_TxConn":
                return self

            def transaction(self) -> "_TxConn":
                return self

            async def __aenter__(self) -> "_TxConn":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                if exc_type is None and self._held_lock:
                    if self._pending_unit is not None:
                        units_committed.append(self._pending_unit)
                        self._pending_unit = None
                    if self._pending_deleted:
                        ws_state["status"] = "deleted"
                        self._pending_deleted = False
                if exc_type is not None or self._held_lock:
                    if self._held_lock and exc_type is not None:
                        self._pending_unit = None
                        self._pending_deleted = False

            async def execute(self, sql: str, params: Any = None) -> None:
                norm = " ".join(str(sql).split())
                self._sql = norm
                self._params = tuple(params or ())
                if "FROM workspaces" in norm and "FOR UPDATE" in norm:
                    if self._role == ("delete" if delete_first else "register"):
                        await ws_row_lock.acquire()
                        self._held_lock = True
                        first_entered.set()
                    else:
                        await first_entered.wait()
                        await ws_row_lock.acquire()
                        self._held_lock = True
                elif norm.startswith("INSERT INTO dalton_coverage_units"):
                    self._pending_unit = {
                        "unit_id": _BIND_UNIT_OWN,
                        "lead_analyst_user_id": _BIND_USER,
                        "workspace_id": _BIND_WS,
                        "status": "active",
                    }
                elif norm.startswith("UPDATE workspaces SET status = 'deleted'"):
                    self._pending_deleted = True

            async def fetchone(self) -> Any:
                s = self._sql
                if "FROM workspaces" in s:
                    return dict(ws_state)
                if "SELECT EXISTS" in s and "FROM dalton_coverage_units" in s:
                    active = [
                        u for u in units_committed if u.get("workspace_id") == _BIND_WS and u.get("status") == "active"
                    ]
                    return {"has_unit": bool(active)}
                if "FROM dalton_coverage_units" in s:
                    active = [
                        u for u in units_committed if u.get("workspace_id") == _BIND_WS and u.get("status") == "active"
                    ]
                    return dict(active[0]) if active else None
                if "UPDATE workspaces SET status = 'deleted'" in s:
                    return {"workspace_id": _BIND_WS, "computer_id": None}
                if self._pending_unit is not None:
                    return dict(self._pending_unit)
                return None

        class _OuterConn:
            def __init__(self, role: str) -> None:
                self._inner = _TxConn(role)
                self._tx_depth = 0

            def cursor(self, **kw: Any) -> "_TxConn":
                return self._inner

            def transaction(self) -> "_OuterConn":
                return self

            async def __aenter__(self) -> Any:
                self._tx_depth += 1
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                self._tx_depth -= 1
                if self._tx_depth > 0:
                    if exc_type is not None:
                        self._inner._pending_unit = None
                        self._inner._pending_deleted = False
                    return None
                try:
                    await self._inner.__aexit__(exc_type, exc, tb)
                finally:
                    if self._inner._held_lock and ws_row_lock.locked():
                        self._inner._held_lock = False
                        ws_row_lock.release()

        reg_conn = _OuterConn("register")
        del_conn = _OuterConn("delete")

        async def _run_register_tx() -> dict[str, Any]:
            async with reg_conn.transaction():
                return await cu_db.insert_coverage_unit(
                    kind="company",
                    owner_user_id=_BIND_USER,
                    slug="nvda",
                    ticker="NVDA",
                    workspace_id=_BIND_WS,
                    conn=reg_conn,
                )

        reg_task = asyncio.create_task(_run_register_tx())
        del_task = asyncio.create_task(
            ws_db.delete_workspace(_BIND_WS, conn=del_conn)
        )
        res = await asyncio.wait_for(
            asyncio.gather(reg_task, del_task, return_exceptions=True),
            timeout=10.0,
        )
        if delete_first:
            assert isinstance(res[0], ValueError)
            assert "does not exist or is deleted" in str(res[0])
            assert res[1] is True
            assert units_committed == []
        else:
            assert isinstance(res[0], dict)
            assert isinstance(res[1], ws_db.WorkspaceBoundToCoverageUnitError)
            assert ws_state["status"] == "active"

    await _run_scenario(delete_first=True)
    await _run_scenario(delete_first=False)


class _Ctx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_error_messages_never_leak_workspace_uuid_and_keep_404_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 1: refused bind/register errors drop workspace UUIDs while keeping 404 classification."""
    from fastapi import HTTPException
    import src.dalton.access_tiers as tiers_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod
    from src.dalton.coverage.unit_errors import classify_coverage_unit_error

    hidden_ws = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        settings_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(_ORIG_SUPABASE_AUTH_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        tiers_mod,
        "classify_owner_tier",
        create_autospec(_ORIG_CLASSIFY_OWNER_TIER, return_value="unknown"),
    )

    with pytest.raises(ValueError) as deleted_exc:
        await cu_db._require_ws_write(
            {"workspace_id": hidden_ws, "user_id": "u-other", "status": "deleted"},
            "u-actor",
        )
    assert hidden_ws not in str(deleted_exc.value)
    assert classify_coverage_unit_error(deleted_exc.value) == ("workspace_not_found", 404)

    with pytest.raises(ValueError) as unknown_tier_exc:
        await cu_db._require_ws_write(
            {"workspace_id": hidden_ws, "user_id": "u-other", "status": "stopped"},
            "u-actor",
        )
    assert hidden_ws not in str(unknown_tier_exc.value)
    assert classify_coverage_unit_error(unknown_tier_exc.value) == ("workspace_not_found", 404)

    monkeypatch.setattr(
        tiers_mod,
        "classify_owner_tier",
        create_autospec(_ORIG_CLASSIFY_OWNER_TIER, return_value="internal"),
    )

    async def _not_found(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        raise HTTPException(status_code=404, detail="workspace hidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(_ORIG_REQUIRE_WS_ACCESS, side_effect=_not_found),
    )
    with pytest.raises(ValueError) as not_found_exc:
        await cu_db._require_ws_write(
            {"workspace_id": hidden_ws, "user_id": "u-other", "status": "stopped"},
            "u-actor",
        )
    assert hidden_ws not in str(not_found_exc.value)
    assert classify_coverage_unit_error(not_found_exc.value) == ("workspace_not_found", 404)


@pytest.mark.asyncio
async def test_require_ws_write_rejects_home_stand_in_workspace() -> None:
    """Round 4 Item 6: home_stand_in workspaces (is_reserved_row) cannot bind coverage units."""
    stand_in_ws = {
        "workspace_id": "bbbbbbbb-1111-2222-3333-444444444444",
        "user_id": "u-actor",
        "status": "stopped",
        "config": {"home_stand_in": True},
    }
    with pytest.raises(ValueError, match="never binds"):
        await cu_db._require_ws_write(stand_in_ws, "u-actor")


def test_d041_backfill_clears_non_company_ticker_and_ticker_key() -> None:
    """Round 4 Item 7: d041.backfill_d041 clears stale ticker/ticker_key on sector rows before CHECK."""
    from src.dalton.migrations.versions import d041_team_coverage_units as d041

    executed_sql: list[str] = []

    class _FakeRes:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Any]:
            return list(self._rows)

    class _FakeConn:
        def execute(self, stmt: Any, params: Any = None) -> _FakeRes:
            sql = " ".join(str(stmt).split())
            executed_sql.append(sql)
            return _FakeRes([])

    d041.backfill_d041(_FakeConn())
    assert any(
        "UPDATE dalton_coverage_units SET ticker = NULL, ticker_key = NULL WHERE kind <> 'company' AND (ticker IS NOT NULL OR ticker_key IS NOT NULL)"
        in sql
        for sql in executed_sql
    )


def test_multi_workspace_for_update_queries_include_order_by() -> None:
    """Item 3: multi-row workspace FOR UPDATE locks must specify ORDER BY to prevent AB-BA deadlocks."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    targets = [
        root / "src/dalton/sharing.py",
        root / "src/dalton/database/coverage_units.py",
    ]
    sql_block_pattern = re.compile(r'"""(.*?)"""', re.DOTALL)
    pattern = re.compile(
        r"SELECT\s+[^;]*?FROM\s+workspaces\b[^;]*?FOR\s+UPDATE",
        re.IGNORECASE | re.DOTALL,
    )
    checked = 0
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for block in sql_block_pattern.finditer(text):
            block_text = block.group(1)
            for match in pattern.finditer(block_text):
                stmt = " ".join(match.group(0).split())
                # Single-row lookup by primary key (`WHERE ... workspace_id = %s ... FOR UPDATE`
                # or single-row `JOIN ins ON w.workspace_id = ins.workspace_id`) cannot reorder two locks.
                is_single_pk = (
                    (
                        "workspace_id = %s" in stmt
                        or "JOIN ins ON w.workspace_id = ins.workspace_id" in stmt
                    )
                    and "ANY(" not in stmt
                    and "workspace_id IN" not in stmt
                )
                if not is_single_pk:
                    checked += 1
                    assert re.search(
                        r"ORDER BY\s+(?:w\.)?created_at,\s*(?:w\.)?workspace_id\s+FOR\s+UPDATE",
                        stmt,
                        re.IGNORECASE,
                    ), (
                        f"{path.name}: multi-workspace FOR UPDATE must use upstream "
                        f"(created_at, workspace_id) lock order: {stmt}"
                    )
    assert checked >= 2


def test_d041_backfill_cleans_python_stripped_ticker_and_edge_cases() -> None:
    """Item 6 (GLM Low 4): backfill_d041 writes Python str.strip() into ticker and clears NULL/tab/whitespace-only rows."""
    from types import SimpleNamespace
    from src.dalton.migrations.versions import d041_team_coverage_units as d041

    executed: list[tuple[str, Any]] = []

    class _FakeRes:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Any]:
            return list(self._rows)

    class _FakeConn:
        def execute(self, stmt: Any, params: Any = None) -> _FakeRes:
            sql = " ".join(str(stmt).split())
            executed.append((sql, params))
            if sql.startswith("SELECT unit_id, ticker FROM dalton_coverage_units"):
                return _FakeRes(
                    [
                        SimpleNamespace(unit_id="u-null", ticker=None),
                        SimpleNamespace(unit_id="u-ws", ticker=" \t \n "),
                        SimpleNamespace(unit_id="u-tab", ticker="\tNVDA\t"),
                        SimpleNamespace(unit_id="u-brk", ticker=" brk-b "),
                    ]
                )
            return _FakeRes([])

    d041.backfill_d041(_FakeConn())
    updates_null = [
        p for sql, p in executed if "SET ticker = NULL, ticker_key = NULL WHERE unit_id = :unit_id" in sql
    ]
    updates_clean = [
        p for sql, p in executed if "SET ticker = :ticker, ticker_key = :ticker_key WHERE unit_id = :unit_id" in sql
    ]
    assert {p["unit_id"] for p in updates_null} == {"u-null", "u-ws"}
    assert updates_clean == [
        {"ticker": "NVDA", "ticker_key": "NVDA", "unit_id": "u-tab"},
        {"ticker": "brk-b", "ticker_key": "BRK.B", "unit_id": "u-brk"},
    ]


@pytest.mark.asyncio
async def test_db_delete_workspace_skips_coverage_unit_guard_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: with coverage units disabled, db delete_workspace does not query dalton_coverage_units."""
    from psycopg import AsyncConnection
    from src.dalton.database import publications as pub_db
    from src.server.database import egress_grants as egress_db
    from src.server.database import workspace as ws_db

    orig_ws_get_db_conn = ws_db.get_db_connection
    executed: list[str] = []
    cur = create_autospec(_BindCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append(" ".join(str(sql).split()))

    async def _fetchone() -> Any:
        sql = executed[-1]
        if sql.startswith("SELECT user_id FROM workspaces WHERE workspace_id"):
            return {"user_id": _BIND_USER}
        if "FROM workspaces WHERE workspace_id = %s FOR UPDATE" in sql:
            return {"workspace_id": _BIND_WS, "computer_id": None}
        if "UPDATE workspaces SET status = 'deleted'" in sql:
            return {"workspace_id": _BIND_WS, "computer_id": None}
        return None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.transaction.return_value = _ConnCtx(conn)
    ctx = _ConnCtx(conn)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")
    monkeypatch.setattr(ws_db, "get_db_connection", create_autospec(orig_ws_get_db_conn, side_effect=lambda conn=None: ctx))
    monkeypatch.setattr(egress_db, "lock_user_egress_state", create_autospec(egress_db.lock_user_egress_state))
    monkeypatch.setattr(egress_db, "retire_workspace_grants", create_autospec(egress_db.retire_workspace_grants))
    monkeypatch.setattr(ws_db, "drop_workspace_links", create_autospec(ws_db.drop_workspace_links))
    monkeypatch.setattr(ws_db, "publish_status_change", create_autospec(ws_db.publish_status_change))
    monkeypatch.setattr(ws_db, "workspace_has_live_runs", create_autospec(ws_db.workspace_has_live_runs, return_value=False))
    monkeypatch.setattr(ws_db, "workspace_is_moving", create_autospec(ws_db.workspace_is_moving, return_value=False))
    monkeypatch.setattr(pub_db, "has_published_reports", create_autospec(pub_db.has_published_reports, return_value=False))

    assert await ws_db.delete_workspace(_BIND_WS) is True
    assert not any("dalton_coverage_units" in s for s in executed)


@pytest.mark.asyncio
async def test_bind_redaction_runs_after_commit_never_on_the_write_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind/rebind response redaction runs after the unit transaction commits, on its own
    connection: never a second pool connection while the write holds row locks, and a failed
    read check cannot turn into a silently rolled-back COMMIT."""
    import src.dalton.sharing as sharing_mod
    from src.server.database import workspace as ws_db

    _install_units_flag(monkeypatch, True)
    ws_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    unit_row = _bind_unit_row(unit_id=_BIND_UNIT_OWN, kind="company", slug="nvda", ticker="NVDA", workspace_id=ws_id)
    from psycopg import AsyncConnection

    tx_conn = create_autospec(AsyncConnection, instance=True)
    tx_conn.transaction.return_value = _ConnCtx(tx_conn)
    ctx = _ConnCtx(tx_conn)

    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        cu_db,
        "bind_coverage_unit_workspace",
        create_autospec(cu_db.bind_coverage_unit_workspace, return_value=unit_row),
    )
    monkeypatch.setattr(
        "src.dalton.coverage.service.sync_lead_analyst_personal_coverage",
        create_autospec(
            __import__("src.dalton.coverage.service", fromlist=["sync_lead_analyst_personal_coverage"]).sync_lead_analyst_personal_coverage,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        "src.dalton.database.coverage.acquire_coverage_advisory_lock",
        create_autospec(
            __import__("src.dalton.database.coverage", fromlist=["acquire_coverage_advisory_lock"]).acquire_coverage_advisory_lock,
            return_value=None,
        ),
    )

    seen_conns: list[Any] = []
    real_get_ws = ws_db.get_workspace

    async def _get_ws_with_conn(wid: str, *, conn: Any = None, include_deleted: bool = False) -> dict[str, Any] | None:
        seen_conns.append(conn)
        if conn is tx_conn:
            raise AssertionError("redaction must not read on the write transaction's connection")
        return {"workspace_id": wid, "user_id": _BIND_USER, "status": "running", "config": {}}

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(real_get_ws, side_effect=_get_ws_with_conn),
    )
    monkeypatch.setattr(sharing_mod, "is_dalton_supabase_auth_enabled", lambda: False)

    out = await units_mod.bind_coverage_unit_workspace(_BIND_UNIT_OWN, ws_id, _BIND_USER)
    assert out["workspace_id"] == ws_id
    assert seen_conns and tx_conn not in seen_conns


@pytest.mark.asyncio
async def test_write_response_is_redacted_only_after_its_own_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Web-read check behind a write's response runs after COMMIT, never on the write's connection."""
    import src.dalton.coverage.units as units_mod
    from src.dalton.database import coverage_units as cu_db_mod

    events: list[str] = []

    class _Tx:
        async def __aenter__(self):
            events.append("begin")
            return self

        async def __aexit__(self, *exc):
            events.append("commit")
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cu_db_mod, "get_db_connection", lambda *a, **k: _Acquire())

    async def _can_read(ws: str, uid: str, *, conn=None) -> bool:
        events.append(f"read-check conn={conn!r}")
        return False

    monkeypatch.setattr(cu_db_mod, "_can_read_workspace", create_autospec(cu_db_mod._can_read_workspace, side_effect=_can_read))

    row = {"unit_id": "u1", "workspace_id": "ws-hidden", "owner_user_id": "o", "lead_analyst_user_id": "l", "created_by_user_id": "c"}

    async def _write(c):
        events.append("write")
        return await units_mod._redact_unit_workspace_for_actor(row, "pm", conn=c)

    out = await units_mod._run_in_unit_tx(None, _write)
    assert events == ["begin", "write", "commit", "read-check conn=None"]
    assert all(out[k] is None for k in ("workspace_id", "owner_user_id", "lead_analyst_user_id", "created_by_user_id"))


@pytest.mark.asyncio
async def test_failed_redaction_check_after_commit_hides_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.coverage.units as units_mod
    from src.dalton.database import coverage_units as cu_db_mod

    monkeypatch.setattr(
        cu_db_mod,
        "_can_read_workspace",
        create_autospec(cu_db_mod._can_read_workspace, side_effect=RuntimeError("pool timeout")),
    )
    pending = units_mod._PendingRedaction({"workspace_id": "ws", "owner_user_id": "o"}, "pm")
    out = await units_mod._resolve_redaction(pending)
    assert out["workspace_id"] is None and out["owner_user_id"] is None


@pytest.mark.asyncio
async def test_phase1_c2_disable_tracking_and_row8_classification_uses_lead_analyst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.2 Row 8 & C2 & Round 9 Item 4:
    - disable_tracking pauses all unit-managed rows (standard ticker-only, earn-*, renamed unit_id), skips unstarted executions, and leaves personal lookalike rows untouched.
    - classification & SQL predicate check COALESCE(lead_analyst_user_id, owner_user_id).
    """
    from contextlib import asynccontextmanager
    from src.dalton.autonomy import classification as cls_mod
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.autonomy import tracking as trk_mod
    from src.server.database import automation as auto_db
    from src.server.database import workspace as ws_db

    ws_id = "11111111-1111-1111-1111-111111111111"
    rows_by_user: dict[str, list[dict[str, Any]]] = {
        "u-old": [
            {
                "automation_id": "a-watch",
                "user_id": "u-old",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "workspace_id": ws_id,
                "status": "active",
                "metadata": {"dalton_kind": "company_watch", "tier": "core", "ticker": "NVDA"},
            },
            {
                "automation_id": "a-earn",
                "user_id": "u-old",
                "name": "dalton-nvda-earn-fy26q3-t0",
                "trigger_type": "once",
                "workspace_id": ws_id,
                "status": "active",
                "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"},
            },
            {
                "automation_id": "a-personal-lookalike",
                "user_id": "u-old",
                "name": "my-nvda-personal-scan",
                "trigger_type": "cron",
                "workspace_id": ws_id,
                "status": "active",
                "metadata": {},
            },
            {
                "automation_id": "a-personal",
                "user_id": "u-old",
                "name": "my-custom-note",
                "trigger_type": "cron",
                "workspace_id": ws_id,
                "status": "active",
                "metadata": {},
            },
            {
                "automation_id": "a-renamed",
                "user_id": "u-old",
                "name": "custom-nvda-focus",
                "trigger_type": "cron",
                "workspace_id": ws_id,
                "status": "active",
                "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA", "unit_id": "u-unit-1"},
            },
        ]
    }
    updates: list[tuple[str, str, str]] = []
    skipped_exec_sql: list[tuple[str, Any]] = []

    class _TxFakeConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self):
            class _Cur:
                rowcount = 2

                async def execute(self_c, sql, params=None):
                    q = " ".join(str(sql).split())
                    self_c._is_now = "clock_timestamp" in q
                    if not self_c._is_now:
                        skipped_exec_sql.append((q, params))

                async def fetchone(self_c):
                    if getattr(self_c, "_is_now", False):
                        from datetime import UTC, datetime

                        return (datetime.now(UTC),)
                    return None

            yield _Cur()

    @asynccontextmanager
    async def _lock(_uid, conn=None):
        yield _TxFakeConn()

    @asynccontextmanager
    async def _exec_conn(conn=None):
        yield _TxFakeConn()

    async def _list_autos(uid, name_prefix=None, conn=None):
        all_rows = list(rows_by_user.get(uid, []))
        if name_prefix:
            return [r for r in all_rows if str(r.get("name") or "").startswith(name_prefix)]
        return all_rows

    async def _update_auto(aid, uid, *, status=None, next_run_at=None, metadata=None, conn=None):
        updates.append((aid, uid, str(status)))
        for r in rows_by_user.get(uid, []):
            if r["automation_id"] == aid:
                r["status"] = status
                if metadata is not None:
                    r["metadata"] = dict(metadata)
        return {"automation_id": aid, "status": status, "metadata": metadata}

    from src.server.database import automation_executions as exec_db_mod

    monkeypatch.setattr(_settings_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(exec_db_mod, "get_db_connection", _exec_conn)
    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, return_value={"workspace_id": ws_id, "user_id": "u-owner", "name": "NVDA", "status": "active"}))
    monkeypatch.setattr(trk_mod, "get_user_tier", create_autospec(trk_mod.get_user_tier, return_value="core"))
    monkeypatch.setattr(trk_mod, "user_automations_lock", _lock)
    monkeypatch.setattr(trk_mod, "list_all_user_automations", create_autospec(trk_mod.list_all_user_automations, side_effect=_list_autos))
    monkeypatch.setattr(auto_db, "update_automation", create_autospec(auto_db.update_automation, side_effect=_update_auto))
    async def _sched_helper(name, spec=None, **kwargs):
        rows_by_user.setdefault("u-old", []).append(
            {
                "automation_id": f"auto-created-{name}",
                "user_id": kwargs.get("user_id") or "u-old",
                "name": name,
                "trigger_type": (spec or {}).get("trigger_type") or "cron",
                "workspace_id": kwargs.get("workspace_id") or ws_id,
                "status": (spec or {}).get("status") or "paused",
                "metadata": dict((spec or {}).get("metadata") or {}),
            }
        )
        return {"ok": True, "name": name}

    monkeypatch.setattr(trk_mod, "schedule_dalton_automation_helper", create_autospec(sched_mod.schedule_dalton_automation_helper, side_effect=_sched_helper))

    unit = {
        "unit_id": "u-unit-1",
        "workspace_id": ws_id,
        "owner_user_id": "u-owner",
        "lead_analyst_user_id": "u-old",
        "status": "active",
        "kind": "company",
        "ticker": "NVDA",
    }
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, return_value=unit))

    res = await trk_mod.disable_tracking("u-old", ws_id)
    assert res["status"] == "disabled"
    assert res["skipped_executions"] == 2
    assert ("a-watch", "u-old", "paused") in updates
    assert ("a-earn", "u-old", "paused") in updates
    assert ("a-renamed", "u-old", "paused") in updates
    assert all(u[0] not in ("a-personal", "a-personal-lookalike") for u in updates)
    assert len(skipped_exec_sql) == 1
    sql_txt, sql_params = skipped_exec_sql[0]
    assert "UPDATE automation_executions SET status = 'skipped'" in sql_txt
    assert sql_params[0] == "disabled"
    assert set(sql_params[2]) == {"a-watch", "a-earn", "a-renamed"}

    # Row 8: classification uses lead_analyst_user_id over owner_user_id
    auto_row = {
        "user_id": "u-new",
        "name": "dalton-nvda-custom-scan",
        "workspace_id": ws_id,
        "metadata": {},
    }
    assert (
        cls_mod.classify_automation_scope(
            auto_row,
            coverage_unit={
                "workspace_id": ws_id,
                "owner_user_id": "u-owner",
                "lead_analyst_user_id": "u-new",
                "kind": "company",
                "status": "active",
            },
        )
        == "research_tracking"
    )
    assert (
        cls_mod.classify_automation_scope(
            {**auto_row, "user_id": "u-owner"},
            coverage_unit={
                "workspace_id": ws_id,
                "owner_user_id": "u-owner",
                "lead_analyst_user_id": "u-new",
                "kind": "company",
                "status": "active",
            },
        )
        == "personal"
    )
    sql_pred = cls_mod.build_research_tracking_sql_predicate("a")
    assert "lead_analyst_user_id" in sql_pred
