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
