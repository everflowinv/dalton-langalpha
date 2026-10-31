"""Sector layer P3: subject identity (d027), sector automations, and publish-side room.

Locks the P3 contract the codex reviews kept hitting:

- d027 relaxes ``ticker`` to nullable on ``dalton_watch_state`` / ``dalton_publications``
  and adds ``unit_id`` + ``subject_kind`` with a CHECK; downgrade refuses while
  sector rows exist. Real upgrade/downgrade against live Postgres is the lead's
  to verify — here the migration's Python executes against a recording ``op``
  so statement shape and ordering are proven, not just text-scraped.
- Sector automations carry ``subject_kind``/``unit_id``/``dalton_kind`` metadata and
  never yield a ticker by name-guessing; names are four-segment so the compat
  ticker regex cannot read one out either.
- Sector agenda due-ness follows the sector doc's ``research_tz`` (fallback: user
  timezone, then deployment default, recorded in the watch state); offset-carrying
  moments keep their offset; company due-ness is untouched when ``due_tz`` is absent.
- Pausing a sector touches only ``dalton-sector-<slug>-*``; member companies'
  ``dalton-<ticker>-*`` rows are never matched.
- With the flag off, sector paths do not query the registry and the company /
  publish paths behave as before.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from src.dalton.tools.director_tools import build_director_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d027_subject_identity.py"
)
SECTOR_DELTA_SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-sector-delta"
    / "scripts"
    / "collect_sector.py"
)

UNIT_ID = "11111111-1111-4111-8111-111111111111"
WS_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "user-1"
WS_ID_B = "33333333-3333-4333-8333-333333333333"

# upstream-main 的公司 INSERT（含 _PUB_COLS 展开），空白按 " ".join(split())
# 规范化后逐字锁定（codex r2 #10）：公司行不得依赖 d027 新列。
_UPSTREAM_COMPANY_INSERT_SQL = (
    "INSERT INTO dalton_publications ( publish_request_id, user_id, workspace_id, ticker, "
    "kind, title, published_date, files, bundle_sha256, sources, audit_summary, reviewer, "
    "approver_user_id, approved_at, thesis_version_no, supersedes_publication_id ) "
    "VALUES ( %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s ) "
    "RETURNING publication_id, publish_request_id, user_id, workspace_id, ticker, kind, title, "
    "published_date, files, bundle_sha256, sources, audit_summary, reviewer, approver_user_id, "
    "approved_at, thesis_version_no, supersedes_publication_id, created_at"
)


def _sector_unit_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "unit_id": UNIT_ID,
        "kind": "sector",
        "owner_user_id": USER_ID,
        "slug": "ai-infrastructure",
        "display_name": "AI Infrastructure",
        "workspace_id": WS_ID,
        "ticker": None,
        "status": "active",
        "research_tz": None,
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# d027 迁移：真实执行迁移 Python（记录 op.execute 发出的语句），不只比对文本。
# 升降级在真实 Postgres 上的效果由 lead 在沙箱外验证。
# ---------------------------------------------------------------------------


def _run_migration(fn_name: str) -> list[str]:
    import alembic.op as alembic_op

    executed: list[str] = []

    def _record(sql: Any, *args: Any, **kwargs: Any) -> None:
        executed.append(" ".join(str(sql).split()))

    recorder = create_autospec(alembic_op.execute, side_effect=_record)
    original = alembic_op.execute
    alembic_op.execute = recorder  # type: ignore[method-assign]
    try:
        spec = importlib.util.spec_from_file_location("d027_subject_identity", MIGRATION_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        getattr(module, fn_name)()
    finally:
        alembic_op.execute = original  # type: ignore[method-assign]
    return executed


def test_d027_upgrade_executes_and_emits_expected_statements() -> None:
    stmts = _run_migration("upgrade")
    joined = "\n".join(stmts)
    # 两张表都放开 NOT NULL、加可空 unit_id、加回填 'company' 的 subject_kind、加 CHECK
    for table in ("dalton_watch_state", "dalton_publications"):
        assert f"ALTER TABLE {table} ALTER COLUMN ticker DROP NOT NULL" in stmts
        assert f"ALTER TABLE {table} ADD COLUMN unit_id UUID" in stmts
        assert (
            f"ALTER TABLE {table} ADD COLUMN subject_kind TEXT NOT NULL DEFAULT 'company'" in stmts
        )
    # CHECK 的主体：company ⇒ ticker；sector ⇒ unit_id 且无 ticker
    assert joined.count("ADD CONSTRAINT ck_dalton_watch_state_subject CHECK") == 1
    assert joined.count("ADD CONSTRAINT ck_dalton_publications_subject CHECK") == 1
    check_body = "(subject_kind = 'company' AND ticker IS NOT NULL) OR (subject_kind = 'sector' AND ticker IS NULL AND unit_id IS NOT NULL)"
    assert joined.count(check_body) == 2
    # 回填走 ADD COLUMN DEFAULT（PG11+ 目录级变更），不得发 UPDATE——
    # dalton_publications 有 BEFORE UPDATE OR DELETE 的不可变触发器
    assert not re.search(r"\bUPDATE\b", joined)
    assert "SET LOCAL lock_timeout = '5s'" in stmts[0]


def test_d027_downgrade_refuses_sector_rows_before_touching_schema() -> None:
    stmts = _run_migration("downgrade")
    joined = "\n".join(stmts)
    # 先拒绝再动结构：DO 块用 RAISE EXCEPTION USING MESSAGE 说明原因，不静默删数据
    do_idx = joined.find("DO $$")
    assert do_idx >= 0
    assert "RAISE EXCEPTION USING MESSAGE" in joined
    assert "dalton_publications still holds sector-subject rows" in joined
    assert "dalton_watch_state still holds sector-subject rows" in joined
    assert joined.find("DROP CONSTRAINT", do_idx) > do_idx
    # 拒绝块之后才恢复 NOT NULL；恢复前删掉列与约束
    for table in ("dalton_watch_state", "dalton_publications"):
        assert f"ALTER TABLE {table} ALTER COLUMN ticker SET NOT NULL" in stmts
    assert not re.search(r"\bDELETE\b|\bDROP TABLE\b", joined)


# ---------------------------------------------------------------------------
# watch_state 主体身份（d027 写入路径）：行业行 NULL ticker + unit_id，
# 公司行 SQL 与 P3 前逐字一致。
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


def _recording_conn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    from psycopg import AsyncConnection

    from src.dalton.database import watch as watch_db

    executed: list[tuple[str, tuple[Any, ...]]] = []
    cur = create_autospec(_ExecCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: Any, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec
    cur.fetchone.return_value = {"workspace_id": WS_ID, "ticker": None, "state": {}, "updated_at": None}
    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    ctx = _ConnCtx(conn)
    monkeypatch.setattr(
        watch_db,
        "get_db_connection",
        create_autospec(watch_db.get_db_connection, side_effect=lambda: ctx),
    )
    return executed


@pytest.mark.asyncio
async def test_upsert_watch_state_company_sql_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.database import watch as watch_db

    executed = _recording_conn(monkeypatch)
    await watch_db.upsert_watch_state(WS_ID, "NVDA", {"a": 1})
    sql, params = executed[0]
    assert sql == (
        "INSERT INTO dalton_watch_state (workspace_id, ticker, state, updated_at) "
        "VALUES (%s, %s, %s, NOW()) "
        "ON CONFLICT (workspace_id) DO UPDATE "
        "SET ticker = EXCLUDED.ticker, state = EXCLUDED.state, updated_at = NOW() "
        "RETURNING workspace_id, ticker, state, updated_at"
    )
    assert params[1] == "NVDA"
    # 公司分支不触碰新列
    assert "subject_kind" not in sql and "unit_id" not in sql


@pytest.mark.asyncio
async def test_upsert_watch_state_sector_row_carries_unit_not_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from src.dalton.database import watch as watch_db

    executed = _recording_conn(monkeypatch)
    await watch_db.upsert_watch_state(
        WS_ID, None, {"sector_due_tz": {"tz": "Asia/Shanghai", "source": "user"}},
        subject_kind="sector", unit_id=UNIT_ID,
    )
    sql, params = executed[0]
    assert "INSERT INTO dalton_watch_state (workspace_id, ticker, state, updated_at, unit_id, subject_kind)" in sql
    assert "VALUES (%s, NULL, %s, NOW(), %s::uuid, 'sector')" in sql
    assert "subject_kind = 'sector'" in sql  # 冲突更新也钉住 sector 身份
    # 无类型上下文的参数显式转型（codex P2 r1 #1），无字面量 %
    assert "::uuid" in sql and "%" not in sql.replace("%s", "").replace("%%", "")
    # Jsonb 不实现 __eq__，取 .obj 比较载荷
    assert params[0] == uuid.UUID(WS_ID)
    assert params[1].obj == {"sector_due_tz": {"tz": "Asia/Shanghai", "source": "user"}}
    assert params[2] == UNIT_ID


@pytest.mark.asyncio
async def test_upsert_watch_state_sector_refuses_ticker_or_missing_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import watch as watch_db

    executed = _recording_conn(monkeypatch)
    with pytest.raises(ValueError, match="unit_id"):
        await watch_db.upsert_watch_state(WS_ID, None, {}, subject_kind="sector", unit_id="  ")
    with pytest.raises(ValueError, match="ticker"):
        await watch_db.upsert_watch_state(WS_ID, "AI", {}, subject_kind="sector", unit_id=UNIT_ID)
    assert executed == []


# ---------------------------------------------------------------------------
# 行业到期时区：sector.research_tz → 用户时区 → 部署默认（写进跟踪状态）
# ---------------------------------------------------------------------------


def test_sector_due_timezone_prefers_doc_then_user_then_default() -> None:
    from src.dalton.autonomy.sector import (
        SECTOR_TZ_SOURCE_DEFAULT,
        SECTOR_TZ_SOURCE_DOC,
        SECTOR_TZ_SOURCE_USER,
        sector_due_timezone,
    )

    assert sector_due_timezone({"research_tz": "America/New_York"}, "Asia/Shanghai") == (
        "America/New_York",
        SECTOR_TZ_SOURCE_DOC,
    )
    # 不可解析的文档值按缺省处理，不炸探针
    assert sector_due_timezone({"research_tz": "Mars/Olympus"}, "Asia/Hong_Kong") == (
        "Asia/Hong_Kong",
        SECTOR_TZ_SOURCE_USER,
    )
    assert sector_due_timezone({}, "Asia/Hong_Kong") == ("Asia/Hong_Kong", SECTOR_TZ_SOURCE_USER)
    # 用户值也无效时退回部署默认
    assert sector_due_timezone({}, "not-a-zone", SECTOR_TZ_SOURCE_USER) == (
        "Asia/Shanghai",
        SECTOR_TZ_SOURCE_DEFAULT,
    )


@pytest.mark.asyncio
async def test_resolve_sector_due_timezone_composes_doc_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.autonomy.sector as sector_mod

    real_fetch = sector_mod.fetch_sector_doc_content
    real_user_tz = sector_mod.resolve_user_timezone_name

    async def _doc(workspace_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        return {"research_tz": "America/New_York"}

    async def _user(user_id: str, *, conn: Any = None) -> tuple[str, str]:
        return ("Asia/Hong_Kong", "user")

    monkeypatch.setattr(sector_mod, "fetch_sector_doc_content", create_autospec(real_fetch, side_effect=_doc))
    monkeypatch.setattr(sector_mod, "resolve_user_timezone_name", create_autospec(real_user_tz, side_effect=_user))
    assert await sector_mod.resolve_sector_due_timezone(workspace_id=WS_ID, user_id=USER_ID) == (
        "America/New_York",
        "sector_doc",
    )

    async def _no_doc(workspace_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(sector_mod, "fetch_sector_doc_content", create_autospec(real_fetch, side_effect=_no_doc))
    assert await sector_mod.resolve_sector_due_timezone(workspace_id=WS_ID, user_id=USER_ID) == (
        "Asia/Hong_Kong",
        "user",
    )


# ---------------------------------------------------------------------------
# 议程探针的 due_tz：行业用自己的时钟；带偏移的时刻原样生效；公司路径不变
# ---------------------------------------------------------------------------


AGENDA_DOC = {
    "next_validations": [
        {"id": "V-001", "status": "open", "due": "2026-10-27", "what": "北美云厂商 capex 季度核对"},
    ],
}


@pytest.mark.asyncio
async def test_probe_agenda_due_follows_sector_tz_not_exchange_guess() -> None:
    from src.dalton.autonomy.probes.agenda import probe_agenda

    now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    # 同一日期型 due：上海口径（10:00 UTC 已过）判到期，美东口径（22:00 UTC 未到）不到期
    sigs_sh, _ = await probe_agenda(
        workspace_id=None, ticker="AI Infrastructure", watch_state={},
        agenda_doc=AGENDA_DOC, now=now, due_tz=ZoneInfo("Asia/Shanghai"),
    )
    assert [s.signal_key for s in sigs_sh] == ["validation:V-001:2026-10-27"]
    sigs_et, _ = await probe_agenda(
        workspace_id=None, ticker="AI Infrastructure", watch_state={},
        agenda_doc=AGENDA_DOC, now=now, due_tz=ZoneInfo("America/New_York"),
    )
    assert sigs_et == []


@pytest.mark.asyncio
async def test_probe_agenda_prefers_offset_carrying_moment() -> None:
    from src.dalton.autonomy.probes.agenda import probe_agenda

    now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    doc = {
        "next_validations": [
            {"id": "V-002", "status": "open", "due": "2026-10-27T16:30:00-04:00", "what": "政策听证会"},
        ]
    }
    # 带偏移的时刻按自身时刻（20:30 UTC）判定，不受 due_tz 影响：15:00 未到
    for tz in (ZoneInfo("Asia/Shanghai"), ZoneInfo("America/New_York")):
        sigs, _ = await probe_agenda(
            workspace_id=None, ticker="AI Infrastructure", watch_state={},
            agenda_doc=doc, now=now, due_tz=tz,
        )
        assert sigs == []


@pytest.mark.asyncio
async def test_probe_agenda_company_default_due_unchanged_without_due_tz() -> None:
    from src.dalton.autonomy.probes.agenda import probe_agenda

    now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    # 公司调用不传 due_tz：按 ticker 交易所口径（NVDA → 美东），22:00 UTC 未到
    sigs, _ = await probe_agenda(
        workspace_id=None, ticker="NVDA", watch_state={},
        agenda_doc=AGENDA_DOC, now=now,
    )
    assert sigs == []
    # 0700.HK → 港时区，10:00 UTC 已过 → 到期
    hk_doc = {
        "next_validations": [
            {"id": "V-003", "status": "open", "due": "2026-10-27", "what": "季报核对"},
        ]
    }
    sigs_hk, _ = await probe_agenda(
        workspace_id=None, ticker="0700.HK", watch_state={},
        agenda_doc=hk_doc, now=now,
    )
    assert [s.signal_key for s in sigs_hk] == ["validation:V-003:2026-10-27"]


# ---------------------------------------------------------------------------
# 预检：sector 种类、禁猜 ticker、watch state 主体身份、证券探针缺席
# ---------------------------------------------------------------------------


def test_extract_ticker_never_guesses_for_sector_rows() -> None:
    from src.dalton.autonomy.preflight import _determine_kind, _extract_ticker

    meta_row = {
        "name": "dalton-sector-ai-infrastructure-agenda",
        "metadata": {"subject_kind": "sector", "unit_id": UNIT_ID, "dalton_kind": "sector_watch"},
    }
    assert _extract_ticker(meta_row) is None
    assert _determine_kind(meta_row) == "sector_watch"
    # 元数据丢失时名称前缀也不得猜出 "SECTOR" 这类伪 ticker
    name_only = {"name": "dalton-sector-ai-infrastructure-delta", "metadata": {}}
    assert _extract_ticker(name_only) is None
    assert _determine_kind(name_only) == "sector_delta"
    # 公司行照旧
    company_row = {"name": "dalton-nvda-watch", "metadata": {}}
    assert _extract_ticker(company_row) == "NVDA"
    assert _determine_kind(company_row) == "company_watch"


def test_sector_names_cannot_be_mistaken_for_company_automations() -> None:
    from src.dalton.coverage.identity import _TICKER_FROM_AUTO_NAME_RE

    from src.dalton.autonomy.schedule import build_sector_watch_specs

    specs = build_sector_watch_specs(
        unit_id=UNIT_ID, slug="ai-infrastructure", display_name="AI Infrastructure",
        workspace_id=WS_ID, timezone_name="America/New_York",
    )
    assert [s["name"] for s in specs] == [
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    ]
    for spec in specs:
        name = spec["name"]
        assert _TICKER_FROM_AUTO_NAME_RE.match(name) is None
        meta = spec["metadata"]
        assert meta["subject_kind"] == "sector"
        assert meta["unit_id"] == UNIT_ID
        assert meta["dalton_kind"] in ("sector_watch", "sector_delta")
        assert "ticker" not in meta
    # 行业议程任务用研究时区；资料增量对齐公司 Watch 档的每周北京时间
    assert specs[0]["timezone"] == "America/New_York"
    assert specs[0]["cron_expression"] == "0 8-18/2 * * 1-5"
    assert specs[1]["cron_expression"] == "0 11 * * 6"


@pytest.mark.asyncio
async def test_sector_watch_preflight_runs_only_agenda_and_persists_sector_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.autonomy.preflight as preflight_mod
    import src.dalton.autonomy.sector as sector_mod
    from src.dalton.autonomy.preflight import PreflightResult, _VerifiedAutomationOwnership
    from src.dalton.database import watch as watch_db
    from src.dalton.autonomy.probes import ProbeSignal

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_bound_unit_row(monkeypatch)

    async def _tz(workspace_id: str, *, user_id: str, sector_content: Any = None, conn: Any = None):
        return ("America/New_York", "sector_doc")

    monkeypatch.setattr(
        sector_mod, "resolve_sector_due_timezone",
        create_autospec(sector_mod.resolve_sector_due_timezone, side_effect=_tz),
    )
    mock_agenda = create_autospec(
        preflight_mod.probe_agenda,
        return_value=(
            [ProbeSignal(probe="agenda", signal_key="validation:V-001:2026-10-27", priority="P0", summary="行业验证项到期")],
            {"sector_due_tz": {"tz": "America/New_York", "source": "sector_doc"}},
        ),
    )
    monkeypatch.setattr(preflight_mod, "probe_agenda", mock_agenda)
    for probe_name in ("probe_sec", "probe_cnhk_filings", "probe_earnings"):
        mock_probe = create_autospec(getattr(preflight_mod, probe_name))
        monkeypatch.setattr(preflight_mod, probe_name, mock_probe)

    mock_upsert_ws = create_autospec(watch_db.upsert_watch_state, return_value={})
    monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
    mock_upsert_sig = create_autospec(
        watch_db.upsert_signal,
        return_value=({"signal_id": "s-1", "woken_execution_id": None, "resolved_at": None, "expired_at": None}, True),
    )
    monkeypatch.setattr(watch_db, "upsert_signal", mock_upsert_sig)
    monkeypatch.setattr(watch_db, "claim_signals_woken", create_autospec(watch_db.claim_signals_woken, return_value=[{"signal_id": "s-1"}]))
    monkeypatch.setattr(watch_db, "insert_preflight_log", create_autospec(watch_db.insert_preflight_log, return_value={}))
    monkeypatch.setattr(watch_db, "get_watch_state", create_autospec(watch_db.get_watch_state, return_value={}))
    monkeypatch.setattr(watch_db, "expire_old_signals", create_autospec(watch_db.expire_old_signals, return_value=[]))

    from src.dalton.budget.service import get_budget_today

    monkeypatch.setattr(
        "src.dalton.budget.service.get_budget_today",
        create_autospec(get_budget_today, return_value={"cap_usd": 0.0}),
    )
    mock_sync = create_autospec(preflight_mod._sync_home_coverage_for_preflight, return_value=None)
    monkeypatch.setattr(preflight_mod, "_sync_home_coverage_for_preflight", mock_sync)

    automation = {
        "automation_id": "a-1",
        "name": "dalton-sector-ai-infrastructure-agenda",
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "trigger_type": "cron",
        "metadata": {
            "dalton_managed": True,
            "dalton_kind": "sector_watch",
            "subject_kind": "sector",
            "unit_id": UNIT_ID,
            "sector_slug": "ai-infrastructure",
            "sector_name": "AI Infrastructure",
            "research_tz": "America/New_York",
        },
    }
    res = await preflight_mod._preflight_check_inner(
        automation,
        "exec-1",
        _VerifiedAutomationOwnership(user_id=USER_ID, workspace_id=WS_ID),
        agenda_doc_override=AGENDA_DOC,
        persist=True,
        home_synced_in_outer=True,
    )
    assert isinstance(res, PreflightResult)
    assert res.decision == "wake"
    # 只跑议程探针，due_tz 是研究时区；证券探针一次都不跑
    assert mock_agenda.await_count == 1
    _, kwargs = mock_agenda.await_args
    assert kwargs["due_tz"] == ZoneInfo("America/New_York")
    assert kwargs["ticker"] == "AI Infrastructure"
    assert kwargs["agenda_doc"] == AGENDA_DOC
    for probe_name in ("probe_sec", "probe_cnhk_filings", "probe_earnings"):
        assert getattr(preflight_mod, probe_name).await_count == 0
    # 跟踪状态按行业身份落库：ticker NULL + unit_id + subject_kind
    assert mock_upsert_ws.await_count == 1
    up_args, up_kwargs = mock_upsert_ws.await_args
    assert up_args[1] is None
    assert up_kwargs == {"subject_kind": "sector", "unit_id": UNIT_ID}
    persisted_state = up_args[2]
    assert persisted_state["sector_due_tz"] == {"tz": "America/New_York", "source": "sector_doc"}
    # 信号不携带 ticker
    assert mock_upsert_sig.await_count == 1
    assert mock_upsert_sig.await_args.kwargs["ticker"] is None


@pytest.mark.asyncio
async def test_sector_delta_preflight_is_pure_schedule_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.autonomy.preflight as preflight_mod
    from src.dalton.autonomy.preflight import _VerifiedAutomationOwnership
    from src.dalton.database import watch as watch_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_bound_unit_row(monkeypatch)
    mock_agenda = create_autospec(preflight_mod.probe_agenda, return_value=([], {}))
    monkeypatch.setattr(preflight_mod, "probe_agenda", mock_agenda)
    mock_upsert_ws = create_autospec(watch_db.upsert_watch_state, return_value={})
    monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
    monkeypatch.setattr(watch_db, "insert_preflight_log", create_autospec(watch_db.insert_preflight_log, return_value={}))

    automation = {
        "automation_id": "a-2",
        "name": "dalton-sector-ai-infrastructure-delta",
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "trigger_type": "cron",
        "metadata": {
            "dalton_managed": True,
            "dalton_kind": "sector_delta",
            "subject_kind": "sector",
            "unit_id": UNIT_ID,
            "sector_slug": "ai-infrastructure",
            "sector_name": "AI Infrastructure",
        },
    }
    res = await preflight_mod._preflight_check_inner(
        automation,
        "exec-2",
        _VerifiedAutomationOwnership(user_id=USER_ID, workspace_id=WS_ID),
        persist=False,
    )
    assert res.decision == "wake"
    assert res.signals and res.signals[0]["probe"] == "schedule"
    assert res.signals[0]["payload"]["kind"] == "sector_delta"
    # 计划唤醒不跑议程探针；persist=False 不写跟踪状态
    assert mock_agenda.await_count == 0
    assert mock_upsert_ws.await_count == 0


def test_home_coverage_sync_skips_sector_kinds() -> None:
    from src.dalton.autonomy.preflight import _sync_home_coverage_for_preflight

    async def _forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("sector automations must not sync Home coverage")


    monkey = pytest.MonkeyPatch()
    monkey.setattr("src.dalton.coverage.render_coverage_yaml", _forbidden)
    try:
        # sector_watch/sector_delta 没有 ticker，但不是 Home 任务：
        # 不能落进 "not ticker ⇒ home" 的旧判据
        asyncio.run(
            _sync_home_coverage_for_preflight(
                user_id=USER_ID, kind="sector_watch", ticker=None, persist=True,
            )
        )
        asyncio.run(
            _sync_home_coverage_for_preflight(
                user_id=USER_ID, kind="sector_delta", ticker=None, persist=True,
            )
        )
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# 跟踪：行业状态 / 开启 / 暂停范围；开关关闭不查注册表
# ---------------------------------------------------------------------------


def _install_sector_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_rows: list[dict[str, Any]] | None = None,
    sector_tz: tuple[str, str] = ("America/New_York", "sector_doc"),
    workspace_ids: tuple[str, ...] = (WS_ID,),
) -> dict[str, Any]:
    """行业跟踪的内存自动化仓：真实 store 语义，DB 边界全部 create_autospec 替身。

    ``workspace_ids`` 是这套替身认得的行业工作区（换绑场景给 A、B 两个），
    行字段与生产 ``get_workspace`` 投影一致（workspace_id/user_id/name/status）。
    """
    from psycopg import AsyncConnection

    import src.dalton.autonomy.schedule as schedule_mod
    import src.dalton.autonomy.sector as sector_mod
    import src.dalton.autonomy.tracking as tracking_mod
    from src.server.database import automation as auto_db
    from src.server.database import workspace as ws_db

    store: list[dict[str, Any]] = list(initial_rows or [])
    lock_conn = create_autospec(AsyncConnection, instance=True)
    tx_lock = asyncio.Lock()

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        async with tx_lock:
            yield lock_conn

    lock_conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _fake_get_db_conn(*args: Any, **kwargs: Any):
        yield lock_conn

    monkeypatch.setattr(
        schedule_mod, "get_db_connection",
        create_autospec(schedule_mod.get_db_connection, side_effect=_fake_get_db_conn),
    )
    monkeypatch.setattr(
        auto_db, "lock_user_automations",
        create_autospec(auto_db.lock_user_automations, return_value=None),
    )

    async def _get_ws(ws_id: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if str(ws_id) in workspace_ids:
            return {
                "workspace_id": str(ws_id),
                "user_id": USER_ID,
                "name": "AI 基础设施",  # 非 ticker 形状
                "status": "running",
            }
        return None

    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, side_effect=_get_ws))

    async def _list_all(uid: str, *, name_prefix: str | None = None, conn: Any = None, **kwargs: Any):
        rows = [dict(r) for r in store if str(r.get("user_id") or uid) == str(uid)]
        if name_prefix:
            pfx = name_prefix.strip().lower()
            rows = [r for r in rows if str(r.get("name") or "").strip().lower().startswith(pfx)]
        return rows

    monkeypatch.setattr(auto_db, "list_all_automations", create_autospec(auto_db.list_all_automations, side_effect=_list_all))

    async def _update(auto_id: str, uid: str, **patch: Any):
        for r in store:
            if str(r.get("automation_id")) == str(auto_id):
                for k, v in patch.items():
                    if v is None and k != "next_run_at":
                        continue
                    r[k] = v
                return dict(r)
        return None

    monkeypatch.setattr(auto_db, "update_automation", create_autospec(auto_db.update_automation, side_effect=_update))

    async def _helper(name, spec=None, **kwargs):
        # 与生产 helper 同语义的最小 upsert：按 名字 + （行工作区为空或等于
        # 目标工作区）找已有行，否则在目标工作区新建——换绑后新工作区的
        # 同名任务是新建行，不回改旧工作区的行。
        assert name is not None
        target_ws = str(kwargs.get("workspace_id") or "").strip()
        for r in store:
            if str(r.get("name")) != str(name):
                continue
            row_ws = str(r.get("workspace_id") or "").strip()
            if target_ws and row_ws and row_ws != target_ws:
                continue
            if spec.get("status"):
                r["status"] = spec["status"]
            r["metadata"] = {**(r.get("metadata") or {}), **(spec.get("metadata") or {})}
            if spec.get("status") == "paused":
                r["next_run_at"] = None
            return {"ok": True, "action": "updated", "name": name}
        store.append(
            {
                "automation_id": f"auto-{len(store) + 1}",
                "user_id": kwargs.get("user_id"),
                "workspace_id": kwargs.get("workspace_id"),
                "name": name,
                "status": spec.get("status") or "active",
                "cron_expression": spec.get("cron_expression"),
                "trigger_type": spec.get("trigger_type"),
                "next_run_at": spec.get("next_run_at"),
                "metadata": dict(spec.get("metadata") or {}),
            }
        )
        return {"ok": True, "action": "created", "name": name}

    monkeypatch.setattr(
        tracking_mod, "schedule_dalton_automation_helper",
        create_autospec(tracking_mod.schedule_dalton_automation_helper, side_effect=_helper),
    )

    async def _tz(workspace_id: str, *, user_id: str, sector_content: Any = None, conn: Any = None):
        return sector_tz

    monkeypatch.setattr(
        sector_mod, "resolve_sector_due_timezone",
        create_autospec(sector_mod.resolve_sector_due_timezone, side_effect=_tz),
    )
    return {"store": store}


def _install_sector_unit(monkeypatch: pytest.MonkeyPatch, unit: dict[str, Any] | None = None) -> Any:
    from src.dalton.database import coverage_units as cu_db

    mock = create_autospec(
        cu_db.get_active_unit_by_workspace,
        return_value=unit if unit is not None else _sector_unit_row(),
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", mock)
    return mock


def _install_bound_unit_row(
    monkeypatch: pytest.MonkeyPatch,
    unit: dict[str, Any] | None = None,
) -> Any:
    """绑定复查（preflight）用的 `get_coverage_unit` 桩：默认返回绑定在本测试
    工作区上的 active sector 单元。"""
    from src.dalton.database import coverage_units as cu_db

    row = unit if unit is not None else _sector_unit_row()
    mock = create_autospec(cu_db.get_coverage_unit, return_value=row)
    monkeypatch.setattr(cu_db, "get_coverage_unit", mock)
    return mock


@pytest.mark.asyncio
async def test_sector_tracking_status_enable_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_sector_unit(monkeypatch)
    fixtures = _install_sector_store(monkeypatch)
    from src.dalton.autonomy.tracking import (
        disable_tracking,
        enable_tracking,
        get_tracking_status,
    )

    # 初始：任务不存在 → disabled（行业标签，无 ticker）
    status = await get_tracking_status(USER_ID, WS_ID)
    assert status["status"] == "disabled"
    assert status["subject_kind"] == "sector"
    assert status["unit_id"] == UNIT_ID
    assert status["ticker"] == ""
    assert status["timezone"] == "America/New_York"
    assert status["timezone_source"] == "sector_doc"
    assert status["label"].startswith("行业自动跟踪")

    # 开启：创建两类任务，metadata 主体身份齐全
    enabled = await enable_tracking(USER_ID, WS_ID)
    assert enabled["status"] == "enabled"
    names = {r["name"] for r in fixtures["store"]}
    assert names == {
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    }
    for row in fixtures["store"]:
        meta = row["metadata"]
        assert meta["subject_kind"] == "sector"
        assert meta["unit_id"] == UNIT_ID
        assert "ticker" not in meta
        assert row["status"] == "active"

    # 暂停：只停本行业两类任务
    paused = await disable_tracking(USER_ID, WS_ID)
    assert paused["status"] == "disabled"
    assert paused["paused_by_user"] is True
    assert all(r["status"] == "paused" for r in fixtures["store"])
    assert all((r["metadata"] or {}).get("paused_by_user") is True for r in fixtures["store"])


@pytest.mark.asyncio
async def test_pausing_sector_never_touches_member_company_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_sector_unit(monkeypatch)
    company_rows = [
        {
            "automation_id": "auto-nvda-1",
            "user_id": USER_ID,
            "workspace_id": "ws-nvda",
            "name": "dalton-nvda-watch",
            "status": "active",
            "metadata": {"dalton_managed": True, "ticker": "NVDA"},
        },
        {
            "automation_id": "auto-nvda-2",
            "user_id": USER_ID,
            "workspace_id": "ws-nvda",
            "name": "dalton-nvda-delta",
            "status": "active",
            "metadata": {"dalton_managed": True, "ticker": "NVDA"},
        },
    ]
    fixtures = _install_sector_store(monkeypatch, initial_rows=list(company_rows))
    from src.dalton.autonomy.tracking import disable_tracking

    await disable_tracking(USER_ID, WS_ID)
    paused_names = {r["name"] for r in fixtures["store"] if r["status"] == "paused"}
    assert paused_names == {
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    }
    # 成员公司任务原样保持 active，metadata 不被打上 paused_by_user
    for r in fixtures["store"]:
        if r["name"].startswith("dalton-nvda-"):
            assert r["status"] == "active"
            assert not (r["metadata"] or {}).get("paused_by_user")


@pytest.mark.asyncio
async def test_sector_tracking_flag_off_never_queries_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    _install_sector_store(monkeypatch)

    from src.dalton.database import coverage_units as cu_db

    async def _forbidden(ws: str, *, conn: Any = None):
        raise AssertionError("flag off must not query dalton_coverage_units")

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, side_effect=_forbidden))
    from src.dalton.autonomy.tracking import get_tracking_status

    status = await get_tracking_status(USER_ID, WS_ID)
    assert status["status"] == "not_applicable"
    assert "subject_kind" not in status


@pytest.mark.asyncio
async def test_sector_tracking_refuses_overlong_slug_upfront(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_sector_unit(monkeypatch, _sector_unit_row(slug="a" * 60, display_name="超长 slug 行业"))
    _install_sector_store(monkeypatch)
    from src.dalton.autonomy.tracking import enable_tracking

    with pytest.raises(ValueError, match="invalid automation name"):
        await enable_tracking(USER_ID, WS_ID)


# ---------------------------------------------------------------------------
# 系统任务：sector 范围；开关关闭无 sector 组、不查单元表
# ---------------------------------------------------------------------------


def test_classify_system_job_key_sector_scope() -> None:
    from src.dalton.autonomy.system_jobs import classify_system_job_key

    assert classify_system_job_key("sector:ai-infrastructure") == ("sector", "ai-infrastructure")
    assert classify_system_job_key("SECTOR:11111111-1111-4111-8111-111111111111") == (
        "sector",
        "11111111-1111-4111-8111-111111111111",
    )
    with pytest.raises(ValueError, match="缺少 slug 或 unit_id"):
        classify_system_job_key("sector:")
    # 裸 slug 仍属公司分支（按 ticker 解析），不误入行业
    assert classify_system_job_key("nvda") == ("company", "nvda")


@pytest.mark.asyncio
async def test_list_system_jobs_sector_group_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.autonomy.system_jobs as sj

    async def _no_rows(uid: str, *, name_prefix: str | None = None, conn: Any = None, **kw: Any):
        return []

    monkeypatch.setattr(sj, "list_all_user_automations", create_autospec(sj.list_all_user_automations, side_effect=_no_rows))

    @asynccontextmanager
    async def _conn():
        class _Cur:
            async def execute(self, *a: Any, **k: Any) -> None:
                raise AssertionError("company workspace discovery must not query DB here")

            async def fetchall(self) -> list[Any]:
                return []

        yield type("Conn", (), {"cursor": lambda self, *a, **k: _Cur()})()

    monkeypatch.setattr(sj, "get_db_connection", create_autospec(sj.get_db_connection, side_effect=_conn))
    monkeypatch.setattr(sj, "_fetch_host_conversation_archive_job", create_autospec(sj._fetch_host_conversation_archive_job, return_value={"key": "conversation-archive"}))
    monkeypatch.setattr(sj, "_fetch_ops_jobs", create_autospec(sj._fetch_ops_jobs, return_value=[]))

    sector_status = {
        "status": "enabled",
        "subject_kind": "sector",
        "label": "行业自动跟踪：开启",
        "timezone": "America/New_York",
        "tasks": [],
    }
    monkeypatch.setattr(sj, "get_tracking_status", create_autospec(sj.get_tracking_status, return_value=sector_status))

    # 开关关闭：不查单元表，无 sector 组
    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    out_off = await sj.list_system_jobs(USER_ID)
    assert "sector" not in out_off["groups"]
    assert all(j.get("scope") != "sector" for j in out_off["jobs"])

    # 开关打开：bound sector 单元进入 sector 组，key/主体身份正确
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(
        cu_db, "list_coverage_units",
        create_autospec(cu_db.list_coverage_units, return_value=[_sector_unit_row()]),
    )
    out_on = await sj.list_system_jobs(USER_ID)
    sector_jobs = out_on["groups"].get("sector") or []
    assert [j["key"] for j in sector_jobs] == ["sector:ai-infrastructure"]
    job = sector_jobs[0]
    assert job["subject_kind"] == "sector"
    assert job["ticker"] is None
    assert job["unit_id"] == UNIT_ID
    assert job["status"] == "on"


# ---------------------------------------------------------------------------
# 发布侧：行业主体明确拒绝（开关打开），数据库层可容纳无 ticker 行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_publish_rejects_sector_subject_before_pseudo_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    import src.dalton.publish.service as pub_mod
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.database import knowledge as db_knowledge

    ws_row = {"workspace_id": WS_ID, "user_id": USER_ID, "name": "AI 基础设施", "status": "running"}
    monkeypatch.setattr(
        pub_mod, "verify_workspace_access",
        create_autospec(pub_mod.verify_workspace_access, return_value=ws_row),
    )

    class _Backend:
        async def aread_text(self, path: str) -> str:
            return "# 行业备忘\n一些内容。\n"

    monkeypatch.setattr(
        db_knowledge, "list_all_latest_for_workspace",
        create_autospec(
            db_knowledge.list_all_latest_for_workspace,
            return_value={"thesis": {"content": {"one_liner": "x"}, "version_no": 1}},
        ),
    )
    monkeypatch.setattr(
        cu_db, "get_active_unit_by_workspace",
        create_autospec(cu_db.get_active_unit_by_workspace, return_value=_sector_unit_row()),
    )
    mock_create_req = create_autospec(pub_mod.db_publications.get_or_create_request)
    monkeypatch.setattr(pub_mod.db_publications, "get_or_create_request", mock_create_req)

    res = await pub_mod.run_publish(
        user_id=USER_ID, workspace_id=WS_ID, thread_id="t-1", tool_call_id="tc-1",
        draft_path="research/note.md", title="行业备忘", kind="topic", slug="ai-infra-note",
        backend=_Backend(),
    )
    assert res["ok"] is False
    assert res["status"] == "sector_publish_not_supported"
    assert "AI Infrastructure" in res["message"]
    assert "行业" in res["message"]
    # 拒绝发生在创建任何发布请求之前
    assert mock_create_req.await_count == 0


@pytest.mark.asyncio
async def test_run_publish_flag_off_keeps_company_path_and_skips_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    import src.dalton.publish.service as pub_mod
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.database import knowledge as db_knowledge

    async def _forbidden(ws: str, *, conn: Any = None):
        raise AssertionError("flag off must not query the units registry")

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, side_effect=_forbidden))

    ws_row = {"workspace_id": WS_ID, "user_id": USER_ID, "name": "AI 基础设施", "status": "running"}
    monkeypatch.setattr(
        pub_mod, "verify_workspace_access",
        create_autospec(pub_mod.verify_workspace_access, return_value=ws_row),
    )

    class _Backend:
        async def aread_text(self, path: str) -> str:
            return "# draft\n内容。\n"

    monkeypatch.setattr(
        db_knowledge, "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )
    # 已发布的请求 → 快速返回分支，消息里仍是 slug 前缀伪造的公司 ticker（P3 前行为）
    published_row = {
        "publish_request_id": "req-1",
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "status": "published",
        "publication_id": "pub-1",
        "audit": None,
    }
    monkeypatch.setattr(
        pub_mod.db_publications, "get_or_create_request",
        create_autospec(pub_mod.db_publications.get_or_create_request, return_value=(published_row, False)),
    )
    monkeypatch.setattr(
        pub_mod.db_publications, "get_publication",
        create_autospec(pub_mod.db_publications.get_publication, return_value={"files": []}),
    )

    res = await pub_mod.run_publish(
        user_id=USER_ID, workspace_id=WS_ID, thread_id="t-1", tool_call_id="tc-1",
        draft_path="research/note.md", title="备忘", kind="topic", slug="ai-infra-note",
        backend=_Backend(),
    )
    assert res["ok"] is True
    assert res["status"] == "published"
    assert "Published AI" in res["message"]  # slug 前缀伪 ticker 的旧行为原样保留


@pytest.mark.asyncio
async def test_insert_publication_sector_and_company_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    from psycopg import AsyncConnection

    from src.dalton.database import publications as pub_db

    executed: list[tuple[str, tuple[Any, ...]]] = []
    cur = create_autospec(_ExecCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: Any, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = [
        {"workspace_id": WS_ID, "status": "running"},  # FOR SHARE 工作区锁
        None,  # 幂等检查：无已有行
        {"publication_id": "pub-1", "files": []},  # INSERT RETURNING
    ]
    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur

    @asynccontextmanager
    async def _tx():
        yield conn

    conn.transaction.side_effect = _tx
    monkeypatch.setattr(
        pub_db, "get_db_connection",
        create_autospec(pub_db.get_db_connection, side_effect=lambda: _ConnCtx2(conn)),
    )

    common = dict(
        publish_request_id="req-1", user_id=USER_ID, workspace_id=WS_ID,
        kind="topic", title="行业备忘", published_date="2026-10-31",
        files=[], file_blobs=[], bundle_sha256="0" * 64, sources=[],
        audit_summary={}, reviewer={}, approver_user_id="pm-1",
        approved_at="2026-10-31T00:00:00+00:00",
    )
    # 行业行：ticker NULL + subject_kind + unit_id（executed[2] 是 INSERT：[0] FOR SHARE、[1] 幂等查）
    await pub_db.insert_publication(ticker=None, subject_kind="sector", unit_id=UNIT_ID, **common)
    sql, params = executed[2]
    assert "INSERT INTO dalton_publications" in sql
    assert "subject_kind, unit_id" in sql
    assert "%s::uuid" in sql
    assert params[-2:] == ("sector", UNIT_ID)
    assert params[3] is None  # ticker NULL

    executed.clear()
    cur.fetchone.side_effect = [
        {"workspace_id": WS_ID, "status": "running"},
        None,
        {"publication_id": "pub-2", "files": []},
    ]
    # 公司行：INSERT 与 upstream-main 逐字一致（codex r2 #10）——不写新列，
    # 靠 d027 列默认值落 subject_kind='company'；未升级 d027 的库同样可用。
    await pub_db.insert_publication(ticker="NVDA", **common)
    sql2, params2 = executed[2]
    assert sql2 == _UPSTREAM_COMPANY_INSERT_SQL
    assert "subject_kind" not in sql2 and "unit_id" not in sql2
    assert len(params2) == 16  # upstream 的 16 个参数，一个不多
    assert params2[3] == "NVDA"

    # 参数校验：行业行带 ticker / 缺 unit_id、公司行缺 ticker 都在触库前拒绝
    with pytest.raises(ValueError, match="never carries a ticker"):
        await pub_db.insert_publication(ticker="AI", subject_kind="sector", unit_id=UNIT_ID, **common)
    with pytest.raises(ValueError, match="requires the coverage unit_id"):
        await pub_db.insert_publication(ticker=None, subject_kind="sector", **common)
    with pytest.raises(ValueError, match="requires a ticker"):
        await pub_db.insert_publication(ticker=None, **common)


class _ConnCtx2:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# 行业资料增量采集脚本：检索词、去重、行业边界（无 SEC/一致预期/股价）
# ---------------------------------------------------------------------------


def _load_sector_collector():
    spec = importlib.util.spec_from_file_location("collect_sector", SECTOR_DELTA_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sector_collector_terms_from_doc_and_members() -> None:
    mod = _load_sector_collector()
    doc = {
        "name": "AI Infrastructure",
        "name_zh": "AI 基础设施",
        "members": [
            {"name": "Mega Tech", "ticker": "MEGA"},
            {"name": "AI 基础设施"},
        ],
    }
    assert mod.build_sector_terms(doc) == ["AI Infrastructure", "AI 基础设施", "Mega Tech"]


def test_sector_collector_dedupes_and_marks_matched_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_sector_collector()

    calls: list[tuple[str, dict[str, Any]]] = []

    def _bridge(tool: str, args: dict[str, Any], timeout: int = 120):
        calls.append((tool, dict(args)))
        route = args.get("route")
        if route in ("sales-notes", "tmtb", "funda-ai"):
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "results": [
                        {"document_id": "doc-1", "title": "行业供需转折", "date": "2026-10-30"},
                        {"document_id": "doc-2", "title": "无关图片", "date": "2026-10-29"},
                    ]
                },
            }
        if route == "guidepoint":
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "results": [
                        {"id": "gp-9", "question": "capex?", "answer": "上行", "date": "2026-10-30"},
                    ]
                },
            }
        return {"ok": True, "status": "ok", "data": {"results": []}}

    monkeypatch.setattr(mod, "call_bridge_jsonrpc", create_autospec(mod.call_bridge_jsonrpc, side_effect=_bridge))

    frag, status = mod.collect_sector_sources(
        ["AI Infrastructure"], "2026-10-23", "2026-10-30", seen=set()
    )
    new = frag["new"]
    # 同一 doc id 跨检索词/来源只保留一条；guidepoint 归一化 id；image: 前缀跳过
    assert set(new.keys()) == {"doc-1", "doc-2", "guidepoint:gp-9"}
    assert new["doc-1"]["route"] == "sales-notes"
    assert new["guidepoint:gp-9"]["qa"] == [{"question": "capex?", "answer": "上行"}]
    # 行业边界：不发任何证券/一致预期请求
    assert all(args.get("route") in mod.SECTOR_ROUTES for _, args in calls)
    # 已 seen 的文档不再出现
    frag2, _ = mod.collect_sector_sources(
        ["AI Infrastructure"], "2026-10-23", "2026-10-30", seen={"doc-1", "doc-2", "guidepoint:gp-9"}
    )
    assert frag2["new"] == {}


def test_sector_collector_failure_summary_and_skip_list(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_sector_collector()

    def _bridge(tool: str, args: dict[str, Any], timeout: int = 120):
        if args.get("route") == "tmtb":
            return {"ok": False, "status": "exception", "error": "timeout"}
        return {"ok": True, "status": "ok", "data": {"results": []}}

    monkeypatch.setattr(mod, "call_bridge_jsonrpc", create_autospec(mod.call_bridge_jsonrpc, side_effect=_bridge))
    frag, _ = mod.collect_sector_sources(["AI Infrastructure"], "2026-10-23", "2026-10-30", seen=set())
    failures = mod.summarize_sector_failures(frag["status"])
    assert [f["source"] for f in failures] == ["tmtb"]
    assert "timeout" in failures[0]["error"]
    # 设计性不采清单固定覆盖证券类来源，理由可读
    skipped = {s["source"]: s["reason"] for s in mod.SKIPPED_SOURCES}
    assert {"sec", "cnhk", "consensus", "price", "alphaengine"} <= set(skipped)
    assert all(skipped.values())


# ===========================================================================
# 第 2 轮（codex r2）：单元身份归属与绑定复查、采集完整性、Guidepoint 合并、
# sector: HTTP 入口、工具支持行业、users.timezone、催化剂窗口、identity 回退
# ===========================================================================


# --- r2 #1：行业任务按 unit_id 归属；换绑后暂停覆盖旧工作区 ---------------


@pytest.mark.asyncio
async def test_sector_pause_after_rebind_reaches_old_workspace_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 开启 → 单元改绑 B → 在 B 关闭：A 上的任务也被暂停（codex r2 #1）。

    归属键是 unit_id：同一用户另一个行业（unit_id 不同、名字同为
    dalton-sector- 前缀）的任务一个都不许碰。
    """
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    unit_mock = _install_sector_unit(monkeypatch)
    other_sector_rows = [
        {
            "automation_id": "auto-other-1",
            "user_id": USER_ID,
            "workspace_id": "ws-other",
            "name": "dalton-sector-digital-ads-agenda",
            "status": "active",
            "metadata": {"subject_kind": "sector", "unit_id": "44444444-4444-4444-8444-444444444444"},
        },
        {
            "automation_id": "auto-other-2",
            "user_id": USER_ID,
            "workspace_id": "ws-other",
            "name": "dalton-sector-digital-ads-delta",
            "status": "active",
            "metadata": {"subject_kind": "sector", "unit_id": "44444444-4444-4444-8444-444444444444"},
        },
    ]
    fixtures = _install_sector_store(
        monkeypatch, workspace_ids=(WS_ID, WS_ID_B), initial_rows=list(other_sector_rows)
    )
    from src.dalton.autonomy.tracking import disable_tracking, enable_tracking

    # A 上开启：两个标准任务落在 A
    await enable_tracking(USER_ID, WS_ID)
    rows_on_a = [r for r in fixtures["store"] if r.get("workspace_id") == WS_ID]
    assert {r["name"] for r in rows_on_a} == {
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    }
    assert all(r["status"] == "active" for r in rows_on_a)

    # 单元改绑 B（既有绑定接口之后的状态）
    unit_mock.return_value = _sector_unit_row(workspace_id=WS_ID_B)

    disabled = await disable_tracking(USER_ID, WS_ID_B)
    assert disabled["status"] == "disabled"
    assert disabled["paused_by_user"] is True
    # 另一个行业的任务（同为 dalton-sector- 前缀）原样保持 active
    for r in other_sector_rows:
        assert r["status"] == "active"
        assert not (r["metadata"] or {}).get("paused_by_user")
    # A 的两行也被暂停（unit_id 归属，不看工作区），并带 stale 标记
    for r in rows_on_a:
        assert r["status"] == "paused", r
        assert (r["metadata"] or {}).get("paused_by_user") is True
        assert (r["metadata"] or {}).get("stale_unit_binding") is True
    # B 上补建 paused 标准任务
    rows_on_b = [r for r in fixtures["store"] if r.get("workspace_id") == WS_ID_B]
    assert {r["name"] for r in rows_on_b} == {
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    }
    assert all(r["status"] == "paused" for r in rows_on_b)


@pytest.mark.asyncio
async def test_sector_enable_after_rebind_pauses_stale_and_lands_on_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """换绑后在 B 开启：A 的旧任务被暂停（stale 标记），套件落在 B。"""
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    unit_mock = _install_sector_unit(monkeypatch)
    fixtures = _install_sector_store(monkeypatch, workspace_ids=(WS_ID, WS_ID_B))
    from src.dalton.autonomy.tracking import enable_tracking

    await enable_tracking(USER_ID, WS_ID)
    unit_mock.return_value = _sector_unit_row(workspace_id=WS_ID_B)
    enabled = await enable_tracking(USER_ID, WS_ID_B)

    assert enabled["status"] == "enabled"
    assert enabled["workspace_id"] == WS_ID_B
    assert enabled.get("paused_stale_tasks") == [
        "dalton-sector-ai-infrastructure-agenda",
        "dalton-sector-ai-infrastructure-delta",
    ]
    for r in fixtures["store"]:
        if r.get("workspace_id") == WS_ID:
            assert r["status"] == "paused"
            assert (r["metadata"] or {}).get("stale_unit_binding") is True
        else:
            assert r["status"] == "active"


@pytest.mark.asyncio
async def test_check_sector_automation_binding_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.autonomy.sector import check_sector_automation_binding
    from src.dalton.database import coverage_units as cu_db

    task = {"workspace_id": WS_ID, "metadata": {"unit_id": UNIT_ID, "subject_kind": "sector"}}
    mock_get = create_autospec(cu_db.get_coverage_unit, return_value=_sector_unit_row())
    monkeypatch.setattr(cu_db, "get_coverage_unit", mock_get)

    ok_reason, unit = await check_sector_automation_binding(task)
    assert ok_reason is None and unit is not None

    no_meta = {"workspace_id": WS_ID, "metadata": {}}
    reason, _ = await check_sector_automation_binding(no_meta)
    assert reason == "sector_unit_missing_unit_id"

    mock_get.side_effect = RuntimeError("db down")
    reason, _ = await check_sector_automation_binding(task)
    assert reason == "sector_unit_lookup_failed"

    mock_get.side_effect = None
    mock_get.return_value = None
    reason, _ = await check_sector_automation_binding(task)
    assert reason == "sector_unit_not_found"

    mock_get.return_value = _sector_unit_row(kind="company", ticker="NVDA")
    reason, _ = await check_sector_automation_binding(task)
    assert reason == "sector_unit_not_sector"

    mock_get.return_value = _sector_unit_row(status="archived")
    reason, _ = await check_sector_automation_binding(task)
    assert reason == "sector_unit_archived"

    mock_get.return_value = _sector_unit_row(workspace_id=WS_ID_B)
    reason, _ = await check_sector_automation_binding(task)
    assert reason == "sector_unit_rebound"


@pytest.mark.asyncio
async def test_preflight_rebound_skip_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧工作区上的行业任务运行前复查绑定：换绑 → 只跳过。

    r3 lead 决定：不写 watch_state（不得覆盖旧工作区的主体行）、不暂停
    任务（tracking.disable 的职责）——预检日志是唯一的留痕。
    """
    import src.dalton.autonomy.preflight as preflight_mod
    from src.dalton.autonomy.preflight import _VerifiedAutomationOwnership
    from src.server.database import automation as auto_db
    from src.dalton.database import watch as watch_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_bound_unit_row(monkeypatch, _sector_unit_row(workspace_id=WS_ID_B))
    mock_agenda = create_autospec(preflight_mod.probe_agenda, return_value=([], {}))
    monkeypatch.setattr(preflight_mod, "probe_agenda", mock_agenda)
    mock_upsert_ws = create_autospec(watch_db.upsert_watch_state, return_value={})
    monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
    mock_get_ws = create_autospec(watch_db.get_watch_state, return_value={})
    monkeypatch.setattr(watch_db, "get_watch_state", mock_get_ws)
    mock_log = create_autospec(watch_db.insert_preflight_log, return_value={})
    monkeypatch.setattr(watch_db, "insert_preflight_log", mock_log)
    mock_update_auto = create_autospec(auto_db.update_automation, return_value={})
    monkeypatch.setattr(auto_db, "update_automation", mock_update_auto)

    automation = {
        "automation_id": "a-stale",
        "name": "dalton-sector-ai-infrastructure-agenda",
        "workspace_id": WS_ID,  # 任务还在 A；单元已绑 B
        "user_id": USER_ID,
        "trigger_type": "cron",
        "metadata": {
            "dalton_managed": True,
            "dalton_kind": "sector_watch",
            "subject_kind": "sector",
            "unit_id": UNIT_ID,
            "sector_slug": "ai-infrastructure",
            "sector_name": "AI Infrastructure",
        },
    }
    res = await preflight_mod._preflight_check_inner(
        automation,
        "exec-stale",
        _VerifiedAutomationOwnership(user_id=USER_ID, workspace_id=WS_ID),
        persist=True,
        home_synced_in_outer=True,
    )
    assert res.decision == "skip"
    assert res.reason == "sector_unit_rebound"
    # 探针不跑；除预检日志外零副作用：不暂停、不读写 watch state
    assert mock_agenda.await_count == 0
    assert mock_update_auto.await_count == 0
    assert mock_upsert_ws.await_count == 0
    assert mock_get_ws.await_count == 0
    assert mock_log.await_count == 1
    assert mock_log.await_args.kwargs["reason"] == "sector_unit_rebound"
    assert mock_log.await_args.kwargs["decision"] == "skip"


@pytest.mark.asyncio
async def test_preflight_lookup_failure_skips_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表暂时查不到只跳过本次，同样零副作用。"""
    import src.dalton.autonomy.preflight as preflight_mod
    from src.dalton.autonomy.preflight import _VerifiedAutomationOwnership
    from src.server.database import automation as auto_db
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.database import watch as watch_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    monkeypatch.setattr(
        cu_db, "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, side_effect=RuntimeError("connection refused")),
    )
    mock_upsert_ws = create_autospec(watch_db.upsert_watch_state, return_value={})
    monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
    mock_log = create_autospec(watch_db.insert_preflight_log, return_value={})
    monkeypatch.setattr(watch_db, "insert_preflight_log", mock_log)
    mock_update_auto = create_autospec(auto_db.update_automation, return_value={})
    monkeypatch.setattr(auto_db, "update_automation", mock_update_auto)

    automation = {
        "automation_id": "a-flaky",
        "name": "dalton-sector-ai-infrastructure-delta",
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "trigger_type": "cron",
        "metadata": {"dalton_kind": "sector_delta", "subject_kind": "sector", "unit_id": UNIT_ID},
    }
    res = await preflight_mod._preflight_check_inner(
        automation,
        "exec-flaky",
        _VerifiedAutomationOwnership(user_id=USER_ID, workspace_id=WS_ID),
        persist=True,
        home_synced_in_outer=True,
    )
    assert res.decision == "skip"
    assert res.reason == "sector_unit_lookup_failed"
    assert mock_update_auto.await_count == 0
    assert mock_upsert_ws.await_count == 0
    assert mock_log.await_count == 1


# --- r2 #2：采集完整性——partial / has_more 不完整，不短路、不推进窗口 ----


def _bridge_returning(route: str, *, status: str = "ok", has_more: bool = False) -> Any:
    def _bridge(tool: str, args: dict[str, Any], timeout: int = 120):
        if args.get("route") != route:
            return {"ok": True, "status": "ok", "data": {"results": []}}
        data: dict[str, Any] = {
            "results": [
                {"document_id": f"{route}-1", "title": "已见的旧文档", "date": "2026-10-30"}
            ],
            "has_more": has_more,
        }
        if status != "ok":
            data["status"] = status
        return {"ok": True, "status": status, "data": data}

    return _bridge


def test_collect_partial_source_blocks_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_sector_collector()
    # 全部文档均已 seen：若只看 ok/新增会误判"无新增可短路"
    monkeypatch.setattr(
        mod, "call_bridge_jsonrpc",
        create_autospec(mod.call_bridge_jsonrpc, side_effect=_bridge_returning("tmtb", status="partial")),
    )
    frag, _ = mod.collect_sector_sources(["AI Infrastructure"], "2026-10-23", "2026-10-30", seen={"tmtb-1"})
    failures = mod.summarize_sector_failures(frag["status"])
    assert [f["source"] for f in failures] == ["tmtb"]
    assert "未扫完" in failures[0]["error"]
    # 撤修即失败：聚合必须把 partial 记为 ok=False / complete=False
    assert frag["status"]["tmtb"]["ok"] is False
    assert frag["status"]["tmtb"]["complete"] is False
    assert frag["status"]["tmtb"]["incomplete_queries"] == 1


def test_collect_has_more_blocks_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_sector_collector()
    monkeypatch.setattr(
        mod, "call_bridge_jsonrpc",
        create_autospec(mod.call_bridge_jsonrpc, side_effect=_bridge_returning("sales-notes", has_more=True)),
    )
    frag, _ = mod.collect_sector_sources(["AI Infrastructure"], "2026-10-23", "2026-10-30", seen={"sales-notes-1"})
    failures = mod.summarize_sector_failures(frag["status"])
    assert [f["source"] for f in failures] == ["sales-notes"]
    assert "未扫完" in failures[0]["error"]


def test_collect_commit_keeps_window_when_incomplete(tmp_path: Any) -> None:
    mod = _load_sector_collector()
    ws = tmp_path / "ws"
    (ws / "research" / "tracker").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "data" / "sector.json").write_text('{"name": "AI Infrastructure"}', encoding="utf-8")
    P = mod.sector_tracker_paths(ws)
    mod.dump_json(P["last_run"], {"until": "2026-10-23", "committed_at": "old"})
    run_dir = P["tracker"] / "r1"
    run_dir.mkdir()
    mod.dump_json(
        run_dir / "packet.json",
        {
            "subject_kind": "sector",
            "sector": "AI Infrastructure",
            "until": "2026-10-30",
            "all_sources_ok": False,
            "internal": {"new": {"doc-1": {"route": "tmtb"}}},
        },
    )
    rc = mod.cmd_commit(
        argparse_namespace(ws=str(ws), run_id="r1", material="no", summary="来源未扫完")
    )
    assert rc == 0
    last_run = mod.load_json(P["last_run"], {})
    # 不完整：until 保持旧值，窗口未推进；seen 仍并入（真实见过的文档）
    assert last_run["until"] == "2026-10-23"
    assert last_run["window_advanced"] is False
    assert last_run["incomplete_run_id"] == "r1"
    assert "doc-1" in mod.load_json(P["seen"], [])

    # 完整包：窗口推进
    run_ok = P["tracker"] / "r2"
    run_ok.mkdir()
    mod.dump_json(
        run_ok / "packet.json",
        {
            "subject_kind": "sector",
            "sector": "AI Infrastructure",
            "until": "2026-10-30",
            "all_sources_ok": True,
            "internal": {"new": {}},
        },
    )
    assert mod.cmd_commit(argparse_namespace(ws=str(ws), run_id="r2", material="no", summary="无新增")) == 0
    last_run2 = mod.load_json(P["last_run"], {})
    assert last_run2["until"] == "2026-10-30"
    assert last_run2["window_advanced"] is True


class argparse_namespace:
    """collect_sector.cmd_commit 只读这四个字段的 argparse 替身。"""

    def __init__(self, ws: str, run_id: str, material: str, summary: str) -> None:
        self.workspace_dir = ws
        self.run_id = run_id
        self.material = material
        self.summary = summary


# --- r2 #3：Guidepoint 多条问答合并 + packet.md 渲染 qa -------------------


def test_collect_merges_guidepoint_qas_and_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_sector_collector()

    # 生产里 guidepoint 的每行带 transcript 级 document_id + 行级 id：
    # 文档 id 归一自前者，问答去重键用后者
    gp_rows = [
        {"document_id": "gp-9", "id": "qa-1", "question": "capex 趋势?", "answer": "上行", "date": "2026-10-30"},
        {"document_id": "gp-9", "id": "qa-2", "question": "供给瓶颈?", "answer": "缓解中", "date": "2026-10-30"},
        {"document_id": "gp-9", "id": "qa-3", "question": "价格?", "answer": "持平", "date": "2026-10-30"},
    ]
    calls: list[str] = []

    def _bridge(tool: str, args: dict[str, Any], timeout: int = 120):
        calls.append(args.get("route") or "")
        if args.get("route") == "guidepoint":
            return {"ok": True, "status": "ok", "data": {"results": list(gp_rows)}}
        return {"ok": True, "status": "ok", "data": {"results": []}}

    monkeypatch.setattr(mod, "call_bridge_jsonrpc", create_autospec(mod.call_bridge_jsonrpc, side_effect=_bridge))
    frag, _ = mod.collect_sector_sources(
        ["AI Infrastructure", "Mega Tech"], "2026-10-23", "2026-10-30", seen=set()
    )
    entry = frag["new"].get("guidepoint:gp-9")
    assert entry is not None
    # 三条问答都保留在同一文档下，两个检索词都记为命中
    assert [qa["question"] for qa in entry["qa"]] == ["capex 趋势?", "供给瓶颈?", "价格?"]
    assert entry["matched_terms"] == ["AI Infrastructure", "Mega Tech"]
    # guidepoint 被两个检索词各查了一次
    assert calls.count("guidepoint") == 2

    md = mod.render_packet_md(
        {
            "sector": "AI Infrastructure",
            "run_id": "r1",
            "since": "2026-10-23",
            "until": "2026-10-30",
            "terms": ["AI Infrastructure"],
            "skipped": [],
            "failed_sources": [],
            "internal": {"new": {"guidepoint:gp-9": entry}},
        }
    )
    # packet.md 必须把问答渲染出来（前 3 条 + 计数）
    assert "问答（3 条" in md
    assert "capex 趋势?" in md and "供给瓶颈?" in md and "价格?" in md


# --- r2 #4：sector: 的 HTTP 入口授权 --------------------------------------


def _install_router_doubles(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import src.dalton.app.router as router_mod
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    observed: dict[str, Any] = {"company_resolve": 0}

    async def _forbidden_company(uid: str, target: str) -> str:
        observed["company_resolve"] += 1
        raise AssertionError(f"sector key must not hit the company resolver, got target={target!r}")

    from src.dalton.autonomy.system_jobs import _resolve_company_workspace_id

    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs._resolve_company_workspace_id",
        create_autospec(_resolve_company_workspace_id, side_effect=_forbidden_company),
    )
    monkeypatch.setattr(
        cu_db, "list_coverage_units",
        create_autospec(cu_db.list_coverage_units, return_value=[_sector_unit_row()]),
    )
    mock_owner = create_autospec(
        router_mod._resolve_tracking_workspace_owner, return_value=(USER_ID, True)
    )
    monkeypatch.setattr(router_mod, "_resolve_tracking_workspace_owner", mock_owner)
    return {"observed": observed, "mock_owner": mock_owner}


@pytest.mark.asyncio
async def test_authorize_system_job_mutation_sector_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.app.router import _authorize_system_job_mutation

    fixtures = _install_router_doubles(monkeypatch)
    # slug 与 unit_id 两种 key 都解析到单元当前绑定的工作区做权限校验
    for key in ("sector:ai-infrastructure", f"sector:{UNIT_ID}"):
        owner = await _authorize_system_job_mutation(key, USER_ID)
        assert owner == USER_ID
        own_call = fixtures["mock_owner"].await_args
        assert own_call.args[0] == WS_ID
    # slug 没被当 ticker / 工作区 ID 走公司解析器
    assert fixtures["observed"]["company_resolve"] == 0

    # 未知 slug → 404（不再被当 workspace UUID 校验）
    with pytest.raises(Exception) as exc_info:
        await _authorize_system_job_mutation("sector:no-such-sector", USER_ID)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_system_jobs_toggle_endpoint_sector(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /system-jobs/sector:<slug> 走 sector 分支并到达 toggle 服务。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.dalton.app import dalton_router
    from src.server.utils.api import get_current_user_id
    import src.dalton.autonomy.system_jobs as sj

    _install_router_doubles(monkeypatch)
    captured: dict[str, Any] = {}

    async def _toggle(uid: str, key: str, *, enabled: bool, force: bool, respect_user_pause: bool):
        captured.update({"uid": uid, "key": key, "enabled": enabled})
        return {"ok": True, "job": {"key": key, "scope": "sector"}}

    monkeypatch.setattr(sj, "toggle_system_job", create_autospec(sj.toggle_system_job, side_effect=_toggle))

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/v1/dalton/system-jobs/sector:ai-infrastructure", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["job"]["scope"] == "sector"
    assert captured == {"uid": USER_ID, "key": "sector:ai-infrastructure", "enabled": False}


# --- r2 #5：跟踪工具支持行业工作区 ----------------------------------------


def _install_sector_tool_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    _install_sector_unit(monkeypatch)
    _install_sector_store(monkeypatch)


@pytest.mark.asyncio
async def test_coverage_tracking_analyst_sector_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """行业分析师在自己工作区 enable/status/disable 全部走行业实现。"""
    _install_sector_tool_doubles(monkeypatch)
    import src.dalton.tools.director_tools as dt_mod

    monkeypatch.setattr(
        dt_mod, "verify_workspace_access",
        create_autospec(dt_mod.verify_workspace_access, return_value={"workspace_id": WS_ID, "user_id": USER_ID}),
    )
    tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_coverage_tracking"]
    cfg = {"configurable": {"user_id": USER_ID, "workspace_id": WS_ID}}

    res_enable = json.loads(await tool.ainvoke({"action": "enable"}, config=cfg))
    assert res_enable["ok"] is True
    assert res_enable["subject_kind"] == "sector"
    assert res_enable["status"] == "enabled"
    assert res_enable["unit_id"] == UNIT_ID

    res_status = json.loads(await tool.ainvoke({"action": "status"}, config=cfg))
    assert res_status["ok"] is True
    assert res_status["status"] == "enabled"

    res_disable = json.loads(await tool.ainvoke({"action": "disable"}, config=cfg))
    assert res_disable["ok"] is True
    assert res_disable["status"] == "disabled"
    assert res_disable["paused_by_user"] is True

    # 行业工作区传 ticker → 明确拒绝（不给"猜一个 ticker"留口）
    res_ticker = json.loads(await tool.ainvoke({"action": "status", "ticker": "AI"}, config=cfg))
    assert res_ticker["ok"] is False
    assert res_ticker["error"] == "forbidden_workspace"
    assert "没有公司 ticker" in res_ticker["message"]


@pytest.mark.asyncio
async def test_coverage_tracking_cos_sector_by_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sector_tool_doubles(monkeypatch)
    import src.dalton.tools.director_tools as dt_mod

    monkeypatch.setattr(
        dt_mod, "verify_workspace_access",
        create_autospec(dt_mod.verify_workspace_access, return_value={"workspace_id": WS_ID, "user_id": USER_ID}),
    )
    tool = {t.name: t for t in build_director_tools(role="chief_of_staff")}["dalton_coverage_tracking"]
    res = json.loads(
        await tool.ainvoke(
            {"action": "enable", "workspace_id": WS_ID},
            config={"configurable": {"user_id": USER_ID, "workspace_id": "home"}},
        )
    )
    assert res["ok"] is True
    assert res["subject_kind"] == "sector"


@pytest.mark.asyncio
async def test_system_jobs_tool_analyst_sector_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sector_tool_doubles(monkeypatch)
    import src.dalton.tools.director_tools as dt_mod
    import src.dalton.autonomy.system_jobs as sj

    monkeypatch.setattr(
        dt_mod, "verify_workspace_access",
        create_autospec(dt_mod.verify_workspace_access, return_value={"workspace_id": WS_ID, "user_id": USER_ID}),
    )
    captured: dict[str, Any] = {}

    async def _status(uid: str, key: str):
        captured["status"] = (uid, key)
        return {"ok": True, "job": {"key": key, "scope": "sector"}}

    async def _run(uid: str, key: str):
        captured["run"] = (uid, key)
        return {"ok": True, "status": "triggered"}

    monkeypatch.setattr(sj, "get_system_job_status", create_autospec(sj.get_system_job_status, side_effect=_status))
    monkeypatch.setattr(sj, "run_system_job_now", create_autospec(sj.run_system_job_now, side_effect=_run))

    tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_system_jobs"]
    cfg = {"configurable": {"user_id": USER_ID, "workspace_id": WS_ID}}

    # 自己行业的 key：status / run_now 都放行，且 key 原样到达服务层
    res_status = json.loads(await tool.ainvoke({"action": "status", "key": "sector:ai-infrastructure"}, config=cfg))
    assert res_status["ok"] is True
    assert captured["status"] == (USER_ID, "sector:ai-infrastructure")

    res_list = json.loads(await tool.ainvoke({"action": "list"}, config=cfg))
    assert res_list["ok"] is True
    assert captured["status"] == (USER_ID, "sector:ai-infrastructure")

    res_run = json.loads(await tool.ainvoke({"action": "run_now", "key": f"sector:{UNIT_ID}"}, config=cfg))
    assert res_run["ok"] is True
    # run_now 用当前单元的规范 key（slug 形态）到达服务层
    assert captured["run"] == (USER_ID, "sector:ai-infrastructure")

    # 别人的行业 / 非 sector key → 拒绝
    res_other = json.loads(await tool.ainvoke({"action": "status", "key": "sector:other-sector"}, config=cfg))
    assert res_other["ok"] is False
    assert res_other["error"] == "forbidden_workspace"

    res_home = json.loads(await tool.ainvoke({"action": "status", "key": "dalton-data-health"}, config=cfg))
    assert res_home["ok"] is False
    assert res_home["error"] == "forbidden_scope"


@pytest.mark.asyncio
async def test_system_jobs_tool_analyst_company_workspace_rejects_sector_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司工作区的 analyst 拿 sector key → 拒绝（行业任务只能在行业工作区管理）。"""
    from src.server.database import workspace as ws_db

    async def _get_nvda(ws_id: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if str(ws_id) == "ws-nvda":
            return {"workspace_id": "ws-nvda", "user_id": "user-1", "name": "NVDA", "status": "running"}
        return None

    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, side_effect=_get_nvda))
    tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_system_jobs"]
    res = json.loads(
        await tool.ainvoke(
            {"action": "status", "key": "sector:ai-infrastructure"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert res["ok"] is False
    assert res["error"] == "forbidden_workspace"


# --- r2 #6：缺省时区读 users.timezone --------------------------------------


@pytest.mark.asyncio
async def test_resolve_user_timezone_reads_users_table(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.autonomy.sector as sector_mod
    from src.server.database import automation as auto_db

    real_get_tz = auto_db.get_user_timezone
    monkeypatch.setattr(
        auto_db, "get_user_timezone",
        create_autospec(real_get_tz, return_value="America/New_York"),
    )
    assert await sector_mod.resolve_user_timezone_name(USER_ID) == ("America/New_York", "user")
    # 无效时区值按缺省处理
    monkeypatch.setattr(
        auto_db, "get_user_timezone",
        create_autospec(real_get_tz, return_value="Mars/Olympus"),
    )
    tz_name, source = await sector_mod.resolve_user_timezone_name(USER_ID)
    assert source == "default"
    assert tz_name == "Asia/Shanghai"
    # users 行缺失 → 部署默认
    monkeypatch.setattr(
        auto_db, "get_user_timezone",
        create_autospec(real_get_tz, return_value=None),
    )
    assert await sector_mod.resolve_user_timezone_name(USER_ID) == ("Asia/Shanghai", "default")


# --- r2 #7：行业催化剂窗口按研究时区 ---------------------------------------


@pytest.mark.asyncio
async def test_probe_agenda_event_window_uses_sector_tz() -> None:
    from src.dalton.autonomy.probes.agenda import probe_agenda

    # 纽约 10-27 20:30 = 上海 10-28 08:30：同一时刻，两个日历的"今天"差一天
    now = datetime(2026, 10, 28, 0, 30, tzinfo=timezone.utc)
    doc = {
        "watch": {
            "events": [
                {"id": "E-31", "status": "confirmed", "date": "2026-10-31", "title": "政策节点", "type": "policy"},
            ]
        }
    }
    # 纽约口径：today=10-27，窗口 [10-25, 10-30] → 31 日不进窗口
    sigs_ny, _ = await probe_agenda(
        workspace_id=None, ticker="AI Infrastructure", watch_state={},
        agenda_doc=doc, now=now, due_tz=ZoneInfo("America/New_York"),
    )
    assert sigs_ny == []
    # 上海口径：today=10-28，窗口 [10-26, 10-31] → 31 日进窗口
    sigs_sh, _ = await probe_agenda(
        workspace_id=None, ticker="AI Infrastructure", watch_state={},
        agenda_doc=doc, now=now, due_tz=ZoneInfo("Asia/Shanghai"),
    )
    assert [s.signal_key for s in sigs_sh] == ["catalyst:E-31:2026-10-31"]
    # 公司调用（不传 due_tz）在两个口径下都按部署时区（上海默认）取今天
    sigs_default, _ = await probe_agenda(
        workspace_id=None, ticker="NVDA", watch_state={},
        agenda_doc=doc, now=now,
    )
    assert [s.signal_key for s in sigs_default] == ["catalyst:E-31:2026-10-31"]


# --- r2 #8：identity 兼容回退跳过 sector 行 --------------------------------


def test_compat_fallback_skips_sector_automation_rows() -> None:
    from src.dalton.coverage.identity import (
        _TICKER_FROM_AUTO_NAME_RE,
        ticker_from_automation_rows,
    )

    # earn-* 形状的合法行业 slug 也不得产出伪 ticker "SECTOR"
    sector_rows = [
        {"name": "dalton-sector-earn-ai-agenda", "workspace_id": WS_ID,
         "metadata": {"subject_kind": "sector", "unit_id": UNIT_ID}},
        {"name": "dalton-sector-earn-ai-delta", "workspace_id": WS_ID,
         "metadata": {"subject_kind": "sector", "unit_id": UNIT_ID}},
    ]
    assert _TICKER_FROM_AUTO_NAME_RE.match("dalton-sector-earn-ai-agenda") is None
    assert ticker_from_automation_rows(WS_ID, sector_rows) is None
    # metadata 丢失时靠名称前缀兜底跳过
    assert ticker_from_automation_rows(
        WS_ID, [{"name": "dalton-sector-earn-ai-delta", "workspace_id": WS_ID, "metadata": {}}]
    ) is None
    # 公司行（含 earn-* 一次性任务）不受影响
    assert ticker_from_automation_rows(
        WS_ID, [{"name": "dalton-nvda-watch", "workspace_id": WS_ID, "metadata": {}}]
    ) == "NVDA"
    assert ticker_from_automation_rows(
        WS_ID, [{"name": "dalton-nvda-earn-2026q3-t0", "workspace_id": WS_ID, "metadata": {}}]
    ) == "NVDA"
    assert ticker_from_automation_rows(
        WS_ID, [{"name": "dalton-nvda-watch", "workspace_id": "", "metadata": {"ticker": "NVDA"}}]
    ) == "NVDA"


# ===========================================================================
# 第 3 轮（codex r3）：preflight 只跳过不处置（无暂停 / 无 watch state 写入）、
# 行业分支开关门控、外层超时按 skip 处理、首次不完整窗口、工具零新增查询
# ===========================================================================


_SECTOR_AUTOMATION = {
    "automation_id": "a-r3",
    "name": "dalton-sector-ai-infrastructure-agenda",
    "workspace_id": WS_ID,
    "user_id": USER_ID,
    "trigger_type": "cron",
    "metadata": {
        "dalton_managed": True,
        "dalton_kind": "sector_watch",
        "subject_kind": "sector",
        "unit_id": UNIT_ID,
        "sector_slug": "ai-infrastructure",
        "sector_name": "AI Infrastructure",
    },
}


def _install_outer_preflight_doubles(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """外层 ``preflight_check`` 的最小替身：所有权校验与 Home 同步短路。"""
    import src.dalton.autonomy.preflight as preflight_mod
    from src.dalton.database import watch as watch_db

    monkeypatch.setattr(
        preflight_mod, "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": WS_ID, "user_id": USER_ID, "status": "running"},
        ),
    )
    monkeypatch.setattr(
        preflight_mod, "_sync_home_coverage_for_preflight",
        create_autospec(preflight_mod._sync_home_coverage_for_preflight, return_value=None),
    )
    doubles: dict[str, Any] = {}
    for fn_name, default in (
        ("upsert_watch_state", {}),
        ("get_watch_state", {}),
        ("insert_preflight_log", {}),
        ("expire_old_signals", []),
    ):
        mock = create_autospec(getattr(watch_db, fn_name), return_value=default)
        monkeypatch.setattr(watch_db, fn_name, mock)
        doubles[fn_name] = mock
    return doubles


@pytest.mark.asyncio
async def test_preflight_flag_off_skips_sector_task_with_zero_state_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：遗留 sector_* 任务直接 skip，不查注册表、不读写任何状态。"""
    import src.dalton.autonomy.preflight as preflight_mod
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    doubles = _install_outer_preflight_doubles(monkeypatch)

    async def _forbidden(uid: str, *, conn: Any = None):
        raise AssertionError("flag off must not query the coverage-unit registry")

    monkeypatch.setattr(cu_db, "get_coverage_unit", create_autospec(cu_db.get_coverage_unit, side_effect=_forbidden))

    res = await preflight_mod.preflight_check(dict(_SECTOR_AUTOMATION), "exec-r3-flag-off", persist=True)
    assert res.decision == "skip"
    assert res.reason == "coverage_units_disabled"
    assert res.should_wake is False
    # 注册表零访问（替身即断言）；watch state 读 / 写 / 过期清理零调用；
    # 预检日志是唯一留痕
    assert doubles["get_watch_state"].await_count == 0
    assert doubles["upsert_watch_state"].await_count == 0
    assert doubles["expire_old_signals"].await_count == 0
    assert doubles["insert_preflight_log"].await_count == 1
    assert doubles["insert_preflight_log"].await_args.kwargs["reason"] == "coverage_units_disabled"


@pytest.mark.asyncio
async def test_preflight_timeout_sector_task_skips_via_outer_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """绑定复查挂到外层超时：行业任务按 skip 返回（不保守唤醒）。

    走生产外层 ``preflight_check``（r3 lead 决定 3），不直接调 inner。
    """
    import src.dalton.autonomy.preflight as preflight_mod
    import src.dalton.autonomy.sector as sector_mod

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    doubles = _install_outer_preflight_doubles(monkeypatch)

    async def _hang(automation: Any) -> tuple[str, Any]:
        await asyncio.sleep(5)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(
        sector_mod, "check_sector_automation_binding",
        create_autospec(sector_mod.check_sector_automation_binding, side_effect=_hang),
    )

    res = await preflight_mod.preflight_check(
        dict(_SECTOR_AUTOMATION), "exec-r3-timeout", persist=True, timeout_s=0.05
    )
    assert res.decision == "skip"
    assert res.reason == "preflight_timeout_sector"
    assert res.should_wake is False
    # 超时也只留预检日志，不写任何状态
    assert doubles["upsert_watch_state"].await_count == 0
    assert doubles["insert_preflight_log"].await_count == 1
    assert doubles["insert_preflight_log"].await_args.kwargs["reason"] == "preflight_timeout_sector"


@pytest.mark.asyncio
async def test_preflight_timeout_company_task_still_wakes_via_outer_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对照：公司任务超时仍保守唤醒（P3 前行为逐字不变）。"""
    import src.dalton.autonomy.preflight as preflight_mod

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    _install_outer_preflight_doubles(monkeypatch)

    async def _hang_probe(**kwargs: Any):
        await asyncio.sleep(5)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(preflight_mod, "probe_agenda", create_autospec(preflight_mod.probe_agenda, side_effect=_hang_probe))

    from src.dalton.budget.service import get_budget_today

    monkeypatch.setattr(
        "src.dalton.budget.service.get_budget_today",
        create_autospec(get_budget_today, return_value={"cap_usd": 0.0}),
    )

    company = {
        "automation_id": "a-co",
        "name": "dalton-nvda-watch",
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "trigger_type": "cron",
        "metadata": {"dalton_managed": True, "dalton_kind": "company_watch", "ticker": "NVDA"},
    }
    res = await preflight_mod.preflight_check(company, "exec-r3-co-timeout", persist=True, timeout_s=0.05)
    assert res.decision == "wake"
    assert res.reason == "preflight_timeout"
    assert res.should_wake is True


def test_collect_commit_first_incomplete_keeps_window_at_since(tmp_path: Any) -> None:
    """首次采集即不完整：until 记为本轮 since（窗口起点前视为已完整覆盖），下轮从本轮窗口起点重扫。"""
    mod = _load_sector_collector()
    ws = tmp_path / "ws"
    (ws / "research" / "tracker").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "data" / "sector.json").write_text('{"name": "AI Infrastructure"}', encoding="utf-8")
    P = mod.sector_tracker_paths(ws)
    # 首次采集：没有 last_run
    assert not P["last_run"].exists()
    run_dir = P["tracker"] / "first"
    run_dir.mkdir()
    mod.dump_json(
        run_dir / "packet.json",
        {
            "subject_kind": "sector",
            "sector": "AI Infrastructure",
            "since": "2026-10-23",
            "until": "2026-10-30",
            "all_sources_ok": False,
            "internal": {"new": {}},
        },
    )
    assert mod.cmd_commit(argparse_namespace(ws=str(ws), run_id="first", material="no", summary="来源失败")) == 0
    last_run = mod.load_json(P["last_run"], {})
    # 撤修即失败：回退实现会把 until 记成 packet.until（10-30）
    assert last_run["until"] == "2026-10-23"
    assert last_run["window_advanced"] is False
    # 下轮 since = until - 1 天 → 10-22，重扫整个本轮窗口
    assert P["last_run"].exists()


@pytest.mark.asyncio
async def test_tool_company_path_flag_off_makes_no_extra_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：公司 analyst 的两个工具入口零新增查询（r3 #6）。

    coverage_tracking 的原公司路径本就读一次工作区（_resolve_workspace_ticker），
    行业身份探测不得再加一次；system_jobs 的原路径在工具层不读工作区。
    """
    import src.dalton.autonomy.system_jobs as sj
    import src.dalton.autonomy.tracking as tracking_mod
    import src.dalton.tools.director_tools as dt_mod
    from src.dalton.database import knowledge as db_knowledge
    from src.server.database import automation as auto_db
    from src.server.database import workspace as ws_db

    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)

    async def _forbidden(ws_id: str, *, conn: Any = None):
        raise AssertionError("flag off must not resolve sector identity")

    monkeypatch.setattr(
        tracking_mod, "_resolve_workspace_sector",
        create_autospec(tracking_mod._resolve_workspace_sector, side_effect=_forbidden),
    )

    ws_calls = {"count": 0}

    async def _get_ws(ws_id: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        ws_calls["count"] += 1
        if str(ws_id) == "ws-nvda":
            return {"workspace_id": "ws-nvda", "user_id": "user-1", "name": "NVDA", "status": "running"}
        return None

    real_get_ws = ws_db.get_workspace
    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(real_get_ws, side_effect=_get_ws))
    # 开关关闭的兼容回退：无 metadata ticker、无知识文档、无自动化 → 名称即 ticker
    monkeypatch.setattr(
        db_knowledge, "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )
    monkeypatch.setattr(
        auto_db, "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[]),
    )
    monkeypatch.setattr(
        tracking_mod, "get_user_tier",
        create_autospec(tracking_mod.get_user_tier, return_value=None),
    )
    monkeypatch.setattr(
        dt_mod, "verify_workspace_access",
        create_autospec(dt_mod.verify_workspace_access, return_value={"workspace_id": "ws-nvda", "user_id": "user-1"}),
    )
    monkeypatch.setattr(
        sj, "get_system_job_status",
        create_autospec(sj.get_system_job_status, return_value={"ok": True, "job": {}}),
    )

    cfg = {"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}}
    tracking_tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_coverage_tracking"]
    res = json.loads(await tracking_tool.ainvoke({"action": "status"}, config=cfg))
    assert res["ok"] is True
    assert res["ticker"] == "NVDA"
    # 原公司路径恰好读两次工作区（工具内身份解析一次 + get_tracking_status
    # 再解析一次，upstream 既有行为）；行业身份探测一次都没发生（替身即
    # 断言）——第 2 轮的无门控实现会是三次。
    assert ws_calls["count"] == 2

    ws_calls["count"] = 0
    jobs_tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_system_jobs"]
    res_jobs = json.loads(await jobs_tool.ainvoke({"action": "list"}, config=cfg))
    assert res_jobs["ok"] is True
    # system_jobs 的工具层原本不读工作区：行业探测关闭后仍为零
    assert ws_calls["count"] == 0


@pytest.mark.asyncio
async def test_preflight_autonomy_disabled_still_gates_sector_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r3（P3）：``autonomy.enabled=false`` 的外层直通不得绕过行业门控——
    开关关闭时遗留 sector 任务仍 skip；公司任务照旧 passthrough。"""
    import src.dalton.autonomy.preflight as preflight_mod

    monkeypatch.delenv("DALTON_COVERAGE_UNITS_ENABLED", raising=False)
    doubles = _install_outer_preflight_doubles(monkeypatch)
    real_settings = preflight_mod.get_dalton_settings()
    off = real_settings.model_copy(
        update={"autonomy": real_settings.autonomy.model_copy(update={"enabled": False})}
    )
    monkeypatch.setattr(
        preflight_mod,
        "get_dalton_settings",
        create_autospec(preflight_mod.get_dalton_settings, return_value=off),
    )

    res = await preflight_mod.preflight_check(dict(_SECTOR_AUTOMATION), "exec-r4-autonomy-off", persist=True)
    assert res.decision == "skip"
    assert res.reason == "coverage_units_disabled"
    assert res.should_wake is False
    assert doubles["insert_preflight_log"].await_args.kwargs["reason"] == "coverage_units_disabled"


@pytest.mark.asyncio
async def test_preflight_autonomy_disabled_sector_gate_honours_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r4（P3）：autonomy 关闭时外层门控也在超时保护内——绑定查询卡住
    按 preflight_timeout_sector 跳过，不无限等待。"""
    import src.dalton.autonomy.preflight as preflight_mod
    import src.dalton.autonomy.sector as sector_mod

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    doubles = _install_outer_preflight_doubles(monkeypatch)
    real_settings = preflight_mod.get_dalton_settings()
    off = real_settings.model_copy(
        update={"autonomy": real_settings.autonomy.model_copy(update={"enabled": False})}
    )
    monkeypatch.setattr(
        preflight_mod,
        "get_dalton_settings",
        create_autospec(preflight_mod.get_dalton_settings, return_value=off),
    )

    async def _hang(automation: Any) -> tuple[str, Any]:
        await asyncio.sleep(5)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(
        sector_mod,
        "check_sector_automation_binding",
        create_autospec(sector_mod.check_sector_automation_binding, side_effect=_hang),
    )
    res = await asyncio.wait_for(
        preflight_mod.preflight_check(
            dict(_SECTOR_AUTOMATION), "exec-r5-timeout", persist=True, timeout_s=0.05
        ),
        timeout=2,
    )
    assert res.decision == "skip"
    assert res.reason == "preflight_timeout_sector"
    assert doubles["insert_preflight_log"].await_args.kwargs["reason"] == "preflight_timeout_sector"
