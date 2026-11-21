"""Company workspace deduplication before workspace creation.

Covers matching rules (coverage unit ticker/name/aliases, normalized workspace
name, ticker-shaped workspace names), exclusions (historical duplicate, Home,
deleted, unbound, sector units, caller's own exact name_key), ordering/cap
(coverage unit first, then activity timestamp, max 3), readability redaction
(`workspace_id` omitted when unreadable), `allow_duplicate_company`, feature flag
off, DB error passthrough, REST 409 response, and `manage_workspaces` create
response. All test doubles use `create_autospec`.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec, patch

import psycopg
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import src.dalton.coverage.dedup as dedup_mod
import src.dalton.database.coverage_units as cu_db
import src.dalton.sharing as sharing_mod
from src.dalton.coverage.dedup import (
    DuplicateMatch,
    DuplicateWorkspaceCandidate,
    find_existing_company_workspace,
    normalize_company_name,
)
from src.dalton.coverage.identity import SOURCE_HISTORICAL_DUPLICATE
from src.server.database.home_workspace import (
    HOME_STAND_IN_KEY,
    get_home_stand_in_id,
    get_home_workspace_id,
)
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary.chief_of_staff import chief_of_staff_workspaces
from src.tools.secretary.tools import hitl_confirm, workspaces_create
from tests.conftest import create_test_app

_REAL_FIND_DEDUP = dedup_mod.find_existing_company_workspace
_REAL_CAN_READ = cu_db._can_read_workspace
_REAL_REQ_ACCESS = sharing_mod.require_workspace_access

USER_CALLER = "user-caller"
USER_RICHARD = "user-richard"
USER_CY = "user-cy"
WS_1 = "11111111-1111-4111-8111-111111111111"
WS_2 = "22222222-2222-4222-8222-222222222222"
WS_3 = "33333333-3333-4333-8333-333333333333"
WS_4 = "44444444-4444-4444-8444-444444444444"
WS_5 = "55555555-5555-4555-8555-555555555555"

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _install_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_rows: list[dict[str, Any]] | None = None,
    ws_rows: list[dict[str, Any]] | None = None,
    coverage_rows: list[dict[str, Any]] | None = None,
    sql_log: list[tuple[str, Any]] | None = None,
    fail_on_execute: Exception | None = None,
) -> None:
    """Install a `create_autospec` DB connection yielding deterministic query rows."""
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.__aenter__.return_value = cursor
    cursor.__aexit__.return_value = None
    last_query: dict[str, str] = {"sql": ""}

    async def _execute(sql: str, params: Any = None) -> None:
        if fail_on_execute is not None:
            raise fail_on_execute
        last_query["sql"] = sql
        if sql_log is not None:
            sql_log.append((sql, params))

    async def _fetchall() -> list[dict[str, Any]]:
        sql = last_query["sql"]
        if "FROM dalton_coverage_units" in sql:
            return list(unit_rows or [])
        if "FROM workspaces" in sql:
            return list(ws_rows or [])
        if "FROM dalton_coverage" in sql:
            return list(coverage_rows or [])
        return []

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        yield conn

    conn.cursor.return_value = cursor
    conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _get_conn(_conn: Any = None):
        yield conn

    monkeypatch.setattr(dedup_mod, "get_db_connection", _get_conn)


def _install_read_access(
    monkeypatch: pytest.MonkeyPatch,
    readable_ws_ids: set[str] | None = None,
) -> None:
    allowed = set(readable_ws_ids or set())

    async def _can_read(
        ws_id: str | None,
        actor_user_id: str,
        *,
        workspace: Any = None,
        conn: Any = None,
    ) -> bool:
        return str(ws_id or "") in allowed

    monkeypatch.setattr(
        dedup_mod,
        "_can_read_workspace",
        create_autospec(_REAL_CAN_READ, side_effect=_can_read),
    )


def _unit_row(
    *,
    ws_id: str = WS_1,
    ws_name: str = "ANT",
    ticker: str | None = "ANT",
    ticker_key: str | None = None,
    display_name: str = "Anthropic",
    slug: str = "ant",
    kind: str = "company",
    owner_id: str = USER_RICHARD,
    owner_name: str = "Richard",
    lead_id: str | None = None,
    lead_name: str | None = None,
    config: dict[str, Any] | None = None,
    last_activity_at: datetime | None = NOW,
    aliases: Any = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "unit_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "kind": kind,
        "owner_user_id": owner_id,
        "lead_analyst_user_id": lead_id or owner_id,
        "slug": slug,
        "display_name": display_name,
        "workspace_id": ws_id,
        "ticker": ticker,
        "ticker_key": ticker_key if ticker_key is not None else ticker,
        "workspace_name": ws_name,
        "workspace_owner_id": owner_id,
        "workspace_status": "running",
        "workspace_config": config,
        "workspace_last_activity_at": last_activity_at,
        "workspace_updated_at": last_activity_at,
        "workspace_created_at": last_activity_at,
        "owner_name": owner_name,
        "owner_email": f"{owner_name.lower()}@example.com",
        "lead_name": lead_name or owner_name,
        "lead_email": f"{(lead_name or owner_name).lower()}@example.com",
    }
    if aliases is not None:
        row["aliases"] = aliases
    return row


def _ws_row(
    *,
    ws_id: str = WS_2,
    name: str = "Anthropic",
    owner_id: str = USER_CY,
    owner_name: str = "CY",
    status: str = "running",
    config: dict[str, Any] | None = None,
    last_activity_at: datetime | None = NOW,
) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "user_id": owner_id,
        "name": name,
        "status": status,
        "config": config,
        "last_activity_at": last_activity_at,
        "updated_at": last_activity_at,
        "created_at": last_activity_at,
        "owner_name": owner_name,
        "owner_email": f"{owner_name.lower()}@example.com",
    }


def _enable_feature(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setenv(
        "DALTON_COVERAGE_UNITS_ENABLED", "true" if enabled else "false"
    )


def test_normalize_company_name_strips_whitespace_case_and_punctuation() -> None:
    assert normalize_company_name("  Alphabet (Google)  ") == "alphabetgoogle"
    assert normalize_company_name("Meta Platforms, Inc.") == "metaplatformsinc"
    assert normalize_company_name("Open-AI") == "openai"
    assert normalize_company_name("腾讯控股。") == "腾讯控股"


@pytest.mark.asyncio
async def test_unit_alias_match_for_anthropic_and_ant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, True)
    sql_log: list[tuple[str, Any]] = []
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="ANT",
                slug="ant",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                lead_id=USER_RICHARD,
                lead_name="Richard",
            )
        ],
        coverage_rows=[
            {
                "ticker": "ANT",
                "name": "Anthropic",
                "aliases": {"search_names": ["Anthropic", "Claude"]},
            }
        ],
        sql_log=sql_log,
    )
    _install_read_access(monkeypatch, {WS_1})

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_1
    assert match.workspace_name == "ANT"
    assert match.owner_name == "Richard"
    assert match.lead_analyst_name == "Richard"
    assert match.ticker == "ANT"
    assert match.can_read is True
    assert match.message == (
        "「Anthropic」已有工作区「ANT」（所有者 Richard，主分析师 Richard），"
        "请直接在那里继续。确实要另建，请确认后重试。"
    )
    assert match.matches_payload() == [
        {
            "workspace_name": "ANT",
            "owner_name": "Richard",
            "lead_analyst_name": "Richard",
            "ticker": "ANT",
            "can_read": True,
            "workspace_id": WS_1,
        }
    ]
    assert all("Anthropic" not in sql for sql, _ in sql_log)


@pytest.mark.asyncio
async def test_colleague_same_name_workspace_readable_vs_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, True)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
            )
        ],
    )

    _install_read_access(monkeypatch, {WS_2})
    readable_match = await find_existing_company_workspace(
        USER_CALLER, "  anthropic  "
    )
    assert readable_match is not None
    assert readable_match.can_read is True
    assert readable_match.workspace_id == WS_2
    assert readable_match.matches_payload()[0]["workspace_id"] == WS_2
    assert (
        readable_match.message
        == "「anthropic」已有工作区「Anthropic」（所有者 CY），请直接在那里继续。确实要另建，请确认后重试。"
    )

    _install_read_access(monkeypatch, set())
    unreadable_match = await find_existing_company_workspace(
        USER_CALLER, "Anthropic"
    )
    assert unreadable_match is not None
    assert unreadable_match.can_read is False
    assert unreadable_match.workspace_id is None
    assert unreadable_match.lead_analyst_name is None
    payload = unreadable_match.matches_payload()[0]
    assert "workspace_id" not in payload
    assert payload["workspace_name"] == "Anthropic"
    assert payload["owner_name"] == "CY"
    assert payload["can_read"] is False
    assert (
        unreadable_match.message
        == "「Anthropic」已有工作区「Anthropic」（所有者 CY），你目前没有访问权限，可以请 CY 开放共享。确实要另建，请确认后重试。"
    )


@pytest.mark.asyncio
async def test_excludes_historical_duplicate_home_deleted_and_sector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, True)
    assert SOURCE_HISTORICAL_DUPLICATE == "historical_duplicate"
    home_id = get_home_workspace_id(USER_RICHARD)
    stand_in_id = get_home_stand_in_id(USER_RICHARD)

    _install_db(
        monkeypatch,
        unit_rows=[
            # Sector unit must never participate, even when name/slug matches.
            _unit_row(
                ws_id=WS_1,
                ws_name="Anthropic",
                ticker=None,
                display_name="Anthropic",
                slug="anthropic",
                kind="sector",
                owner_id=USER_RICHARD,
                owner_name="Richard",
            ),
            # Historical duplicate bound to a company unit is excluded.
            _unit_row(
                ws_id=WS_2,
                ws_name="Anthropic Copy",
                ticker="ANT",
                display_name="Anthropic",
                slug="ant",
                kind="company",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                config={"dalton_historical_duplicate_of": WS_3},
            ),
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                config={"dalton_historical_duplicate_of": WS_3},
            ),
            _ws_row(
                ws_id=home_id,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_name="Richard",
            ),
            _ws_row(
                ws_id=stand_in_id,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                config={HOME_STAND_IN_KEY: True},
            ),
            _ws_row(
                ws_id=WS_3,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                status="deleted",
            ),
            _ws_row(
                ws_id=WS_4,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                status="unbound",
            ),
            # Workspace bound to the sector unit above must also be excluded from rule (b).
            _ws_row(
                ws_id=WS_1,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_name="Richard",
            ),
            # Caller's own exact name_key is handled by WorkspaceNameTaken, not dedup.
            _ws_row(
                ws_id=WS_5,
                name="anthropic",
                owner_id=USER_CALLER,
                owner_name="Caller",
            ),
        ],
    )
    _install_read_access(monkeypatch, {WS_1, WS_2, WS_3, WS_4, WS_5})

    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_ticker_rule_and_sorting_cap_at_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, True)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="Tencent Unit",
                ticker="0700.HK",
                display_name="Tencent",
                slug="tencent",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                last_activity_at=NOW - timedelta(days=10),
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="700.HK",
                owner_id=USER_CY,
                owner_name="CY",
                last_activity_at=NOW - timedelta(days=1),
            ),
            _ws_row(
                ws_id=WS_3,
                name="700",
                owner_id="user-3",
                owner_name="Alice",
                last_activity_at=NOW - timedelta(days=2),
            ),
            _ws_row(
                ws_id=WS_4,
                name="0700.HK",
                owner_id="user-4",
                owner_name="Bob",
                last_activity_at=NOW - timedelta(days=5),
            ),
        ],
    )
    _install_read_access(monkeypatch, {WS_1, WS_2, WS_3, WS_4})

    match = await find_existing_company_workspace(USER_CALLER, "0700.HK")
    assert match is not None
    assert len(match.matches) == 3
    # Coverage unit ranks first despite older activity; remaining sorted by recent activity.
    assert [m.workspace_name for m in match.matches] == [
        "Tencent Unit",
        "700.HK",
        "700",
    ]


@pytest.mark.asyncio
async def test_feature_flag_off_returns_none_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, False)

    def _fail_db(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB must not be queried when coverage_units is disabled")

    monkeypatch.setattr(dedup_mod, "get_db_connection", _fail_db)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_db_error_returns_none_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_feature(monkeypatch, True)
    _install_db(monkeypatch, fail_on_execute=RuntimeError("db connection lost"))

    with caplog.at_level(logging.WARNING):
        res = await find_existing_company_workspace(USER_CALLER, "Anthropic")

    assert res is None
    assert "Company workspace dedup check failed" in caplog.text


@pytest.mark.asyncio
async def test_real_can_read_workspace_used_by_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_feature(monkeypatch, True)
    _install_db(
        monkeypatch,
        ws_rows=[_ws_row(ws_id=WS_2, name="Anthropic", owner_id=USER_CY, owner_name="CY")],
    )

    async def _req(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(_REAL_REQ_ACCESS, side_effect=_req),
    )

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.can_read is False
    assert match.workspace_id is None


@pytest.mark.asyncio
async def test_rest_create_workspace_returns_409_and_allows_override(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.server.app.workspaces import router

    app = create_test_app(router)
    dup = DuplicateMatch(
        company="Anthropic",
        message=(
            "「Anthropic」已有工作区「ANT」（所有者 Richard，主分析师 Richard），"
            "请直接在那里继续。确实要另建，请确认后重试。"
        ),
        matches=(
            DuplicateWorkspaceCandidate(
                workspace_name="ANT",
                owner_name="Richard",
                can_read=True,
                workspace_id=WS_1,
                lead_analyst_name="Richard",
                ticker="ANT",
            ),
        ),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "Anthropic",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=dup)

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.app.workspaces.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
        patch(
            "src.server.services.platform_client.platform_gating_active",
            return_value=False,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_409 = await client.post(
                "/api/v1/workspaces", json={"name": "Anthropic"}
            )
            assert resp_409.status_code == 409
            assert resp_409.json()["detail"] == {
                "code": "company_workspace_exists",
                "message": dup.message,
                "matches": [
                    {
                        "workspace_name": "ANT",
                        "owner_name": "Richard",
                        "lead_analyst_name": "Richard",
                        "ticker": "ANT",
                        "can_read": True,
                        "workspace_id": WS_1,
                    }
                ],
            }
            mgr.create_workspace.assert_not_awaited()

            with caplog.at_level(logging.INFO):
                resp_201 = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Anthropic", "allow_duplicate_company": True},
                )
            assert resp_201.status_code == 201
            assert resp_201.json()["workspace_id"] == WS_5
            dedup_mock.assert_awaited_once()
            assert "allow_duplicate_company=True" in caplog.text


@pytest.mark.asyncio
async def test_rest_create_workspace_proceeds_when_dedup_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.server.app.workspaces import router

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "Anthropic",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(
        _REAL_FIND_DEDUP, side_effect=RuntimeError("transient error")
    )

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.app.workspaces.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
        patch(
            "src.server.services.platform_client.platform_gating_active",
            return_value=False,
        ),
        caplog.at_level(logging.WARNING),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workspaces", json={"name": "Anthropic"})

    assert resp.status_code == 201
    assert "Company workspace dedup check failed" in caplog.text


@pytest.mark.asyncio
async def test_manage_workspaces_create_returns_dedup_error_and_allows_override() -> None:
    dup = DuplicateMatch(
        company="Anthropic",
        message=(
            "「Anthropic」已有工作区「Anthropic」（所有者 CY），"
            "你目前没有访问权限，可以请 CY 开放共享。确实要另建，请确认后重试。"
        ),
        matches=(
            DuplicateWorkspaceCandidate(
                workspace_name="Anthropic",
                owner_name="CY",
                can_read=False,
                workspace_id=None,
                lead_analyst_name=None,
                ticker=None,
            ),
        ),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {"workspace_id": WS_5, "name": "Anthropic"}
    mgr.resolve_binding.return_value = None
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=dup)

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.tools.secretary.tools.hitl_confirm",
            create_autospec(hitl_confirm, return_value=(True, {})),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
    ):
        res_blocked = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {"action": "create", "name": "Anthropic", "state": {}},
                "id": "call-1",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
        payload = json.loads(res_blocked.update["messages"][0].content)
        assert payload == {
            "success": False,
            "error": dup.message,
            "code": "company_workspace_exists",
            "message": dup.message,
            "matches": [
                {
                    "workspace_name": "Anthropic",
                    "owner_name": "CY",
                    "lead_analyst_name": None,
                    "ticker": None,
                    "can_read": False,
                }
            ],
        }
        mgr.create_workspace.assert_not_awaited()

        res_allowed = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {
                    "action": "create",
                    "name": "Anthropic",
                    "allow_duplicate_company": True,
                    "state": {},
                },
                "id": "call-2",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
        ok_payload = json.loads(res_allowed.update["messages"][0].content)
        assert ok_payload["success"] is True
        assert ok_payload["workspace_id"] == WS_5
        dedup_mock.assert_awaited_once()
        mgr.create_workspace.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspaces_create_proceeds_when_dedup_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {"workspace_id": WS_5, "name": "Anthropic"}
    mgr.resolve_binding.return_value = None
    dedup_mock = create_autospec(
        _REAL_FIND_DEDUP, side_effect=RuntimeError("db unreachable")
    )

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
        caplog.at_level(logging.WARNING),
    ):
        res = await workspaces_create(
            USER_CALLER, "Anthropic", None, "call-3", preapproved=True
        )

    payload = json.loads(res.update["messages"][0].content)
    assert payload["success"] is True
    assert "Company workspace dedup check failed" in caplog.text
