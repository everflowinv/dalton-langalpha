"""Company workspace deduplication before workspace creation (round 2).

Covers:
1. Deterministic `display_name` candidate derivation (parens, CJK/Latin boundary,
   repeated corporate suffix stripping, >= 2 chars; 6 real prod display names)
   and exclusion of unit `slug`;
2. Strict `normalize_company_name` (keeps `+`, `#`, `&`, parens);
3. Visibility boundaries: team mode (`is_dalton_supabase_auth_enabled()`),
   `external` caller bypass, internal-only candidate units/workspaces/aliases;
4. Channel-aware read access (`web` vs `cos`) and redaction of `ticker`,
   `workspace_id`, `lead_analyst_name` when unreadable;
5. Chief of Staff prompt non-contradiction;
6. Caller's own exact `workspace_name_key` exits dedup early for `WorkspaceNameTaken`;
7. Permission lookup failures (503 / `OwnerTierUnavailable`) and `dalton_coverage`
   alias read failures allow creation and log only exception class + workspace count;
8. DB connection released before permission checks, SQL projects only booleans
   from `config` and filters in SQL;
9. Sector exclusion reused from `company_routing._is_sector_workspace` and rule (b)
   requires `identity.py` company classification;
10. CoS dedup runs before `hitl_confirm` when `allow_duplicate_company=False`;
12. `allow_duplicate_company=True` runs dedup once to audit-log matched workspace IDs.
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
import src.dalton.sharing as sharing_mod
from src.dalton.coverage.dedup import (
    DuplicateMatch,
    DuplicateWorkspaceCandidate,
    derive_display_name_candidates,
    find_existing_company_workspace,
    normalize_company_name,
)
from src.dalton.coverage.identity import SOURCE_HISTORICAL_DUPLICATE
from src.ptc_agent.agent.prompts import get_loader
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
_REAL_REQ_ACCESS = sharing_mod.require_workspace_access
_REAL_GET_USER_TIER = dedup_mod.get_user_tier

USER_CALLER = "user-caller"
USER_RICHARD = "user-richard"
USER_CY = "user-cy"
USER_EXTERNAL = "user-external"
USER_UNKNOWN = "user-unknown"

EMAIL_CALLER = "caller@everflow.ai"
EMAIL_RICHARD = "richard@everflow.ai"
EMAIL_CY = "cy@everflow.ai"
EMAIL_EXTERNAL = "guest@gmail.com"

WS_1 = "11111111-1111-4111-8111-111111111111"
WS_2 = "22222222-2222-4222-8222-222222222222"
WS_3 = "33333333-3333-4333-8333-333333333333"
WS_4 = "44444444-4444-4444-8444-444444444444"
WS_5 = "55555555-5555-4555-8555-555555555555"

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _enable_team_mode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    coverage_enabled: bool = True,
    supabase_auth: bool = True,
    caller_tier: str = "internal",
) -> None:
    monkeypatch.setenv(
        "DALTON_COVERAGE_UNITS_ENABLED", "true" if coverage_enabled else "false"
    )
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase" if supabase_auth else "none")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{EMAIL_CALLER},{EMAIL_RICHARD},{EMAIL_CY},alice@everflow.ai,bob@everflow.ai",
    )

    async def _tier(user_id: str | None) -> str:
        if user_id == USER_EXTERNAL:
            return "external"
        return caller_tier

    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(_REAL_GET_USER_TIER, side_effect=_tier),
    )


def _install_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_rows: list[dict[str, Any]] | None = None,
    ws_rows: list[dict[str, Any]] | None = None,
    coverage_rows: list[dict[str, Any]] | None = None,
    doc_rows: list[dict[str, Any]] | None = None,
    automation_rows: list[dict[str, Any]] | None = None,
    sql_log: list[tuple[str, Any]] | None = None,
    fail_on_table: dict[str, Exception] | None = None,
    fail_on_execute: Exception | None = None,
    conn_state: dict[str, bool] | None = None,
) -> None:
    """Install a `create_autospec` DB connection yielding deterministic query rows."""
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.__aenter__.return_value = cursor
    cursor.__aexit__.return_value = None
    last_query: dict[str, str] = {"sql": ""}
    table_errors = fail_on_table or {}

    async def _execute(sql: str, params: Any = None) -> None:
        if fail_on_execute is not None:
            raise fail_on_execute
        for marker, exc in table_errors.items():
            if marker in sql:
                raise exc
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
        if "FROM dalton_knowledge_versions" in sql:
            return list(doc_rows or [])
        if "FROM automations" in sql:
            return list(automation_rows or [])
        return []

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_conn(_conn: Any = None):
        if conn_state is not None:
            conn_state["active"] = True
        try:
            yield conn
        finally:
            if conn_state is not None:
                conn_state["active"] = False

    monkeypatch.setattr(dedup_mod, "get_db_connection", _get_conn)


def _install_read_access(
    monkeypatch: pytest.MonkeyPatch,
    *,
    web_readable: set[str] | None = None,
    cos_readable: set[str] | None = None,
    error_for_ws: dict[str, Exception] | None = None,
    conn_state: dict[str, bool] | None = None,
    seen_channels: list[str] | None = None,
) -> None:
    web_set = set(web_readable or set())
    cos_set = set(cos_readable or set())
    errs = error_for_ws or {}

    async def _req(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if conn_state is not None and conn_state.get("active"):
            raise AssertionError("Outer DB connection was still held during permission check")
        if seen_channels is not None:
            seen_channels.append(channel)
        wid = str(ws_id or "")
        if wid in errs:
            raise errs[wid]
        allowed = cos_set if channel == "cos" else web_set
        if wid in allowed:
            return dict(workspace or {"workspace_id": wid})
        raise HTTPException(status_code=404, detail="Workspace not found")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(_REAL_REQ_ACCESS, side_effect=_req),
    )


def _unit_row(
    *,
    ws_id: str = WS_1,
    ws_name: str = "ANT",
    ticker: str | None = "ANT",
    ticker_key: str | None = None,
    display_name: str = "Anthropic, PBC",
    kind: str = "company",
    owner_id: str = USER_RICHARD,
    owner_name: str = "Richard",
    owner_email: str | None = EMAIL_RICHARD,
    lead_id: str | None = None,
    lead_name: str | None = None,
    is_home_stand_in: bool = False,
    is_historical_duplicate: bool = False,
    config: dict[str, Any] | None = None,
    last_activity_at: datetime | None = NOW,
) -> dict[str, Any]:
    return {
        "unit_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "kind": kind,
        "owner_user_id": owner_id,
        "lead_analyst_user_id": lead_id or owner_id,
        "display_name": display_name,
        "workspace_id": ws_id,
        "ticker": ticker,
        "ticker_key": ticker_key if ticker_key is not None else ticker,
        "workspace_name": ws_name,
        "workspace_owner_id": owner_id,
        "workspace_status": "running",
        "is_home_stand_in": is_home_stand_in,
        "is_historical_duplicate": is_historical_duplicate,
        "workspace_config": config,
        "workspace_last_activity_at": last_activity_at,
        "workspace_updated_at": last_activity_at,
        "workspace_created_at": last_activity_at,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "unit_owner_email": owner_email,
        "lead_name": lead_name or owner_name,
        "lead_email": owner_email,
    }


def _ws_row(
    *,
    ws_id: str = WS_2,
    name: str = "Anthropic",
    owner_id: str = USER_CY,
    owner_name: str = "CY",
    owner_email: str | None = EMAIL_CY,
    status: str = "running",
    is_home_stand_in: bool = False,
    is_historical_duplicate: bool = False,
    config: dict[str, Any] | None = None,
    last_activity_at: datetime | None = NOW,
) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "user_id": owner_id,
        "name": name,
        "status": status,
        "is_home_stand_in": is_home_stand_in,
        "is_historical_duplicate": is_historical_duplicate,
        "config": config,
        "last_activity_at": last_activity_at,
        "updated_at": last_activity_at,
        "created_at": last_activity_at,
        "owner_name": owner_name,
        "owner_email": owner_email,
    }


def test_normalize_company_name_preserves_symbols_and_strips_specified_chars() -> None:
    assert normalize_company_name("  Alphabet (Google)  ") == "alphabet(google)"
    assert normalize_company_name("Meta Platforms, Inc.") == "metaplatformsinc"
    assert normalize_company_name("Open-AI") == "openai"
    assert normalize_company_name("腾讯控股。") == "腾讯控股"
    assert normalize_company_name("Foo_Bar's ’Baz·中国，") == "foobarsbaz中国"
    assert normalize_company_name("C++") == "c++"
    assert normalize_company_name("C#") == "c#"
    assert normalize_company_name("AT&T") == "at&t"


def test_derive_display_name_candidates_rules_and_edge_cases() -> None:
    assert "Anthropic" in derive_display_name_candidates("Anthropic, PBC")
    assert derive_display_name_candidates("Futu Holdings（富途）") == (
        "Futu Holdings（富途）",
        "Futu Holdings",
        "Futu",
        "富途",
    )
    assert derive_display_name_candidates("Interactive Brokers Group, Inc.") == (
        "Interactive Brokers Group, Inc.",
        "Interactive Brokers Group",
        "Interactive Brokers",
    )
    assert derive_display_name_candidates("邦吉 Bunge Global") == (
        "邦吉 Bunge Global",
        "邦吉",
        "Bunge Global",
    )
    assert derive_display_name_candidates("海大集团") == ("海大集团", "海大")
    assert derive_display_name_candidates("宁德时代") == ("宁德时代",)
    # < 2 char stripped candidates are dropped; mid-word suffixes like MetaCo are untouched.
    assert derive_display_name_candidates("A Inc.") == ("A Inc.",)
    assert derive_display_name_candidates("MetaCo") == ("MetaCo",)


@pytest.mark.asyncio
async def test_real_prod_display_names_matching_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    units = [
        _unit_row(ws_id=WS_1, ws_name="ANT", ticker="ANT", display_name="Anthropic, PBC"),
        _unit_row(ws_id=WS_2, ws_name="FUTU", ticker="FUTU", display_name="Futu Holdings（富途）"),
        _unit_row(
            ws_id=WS_3,
            ws_name="IBKR",
            ticker="IBKR",
            display_name="Interactive Brokers Group, Inc.",
        ),
        _unit_row(ws_id=WS_4, ws_name="BG", ticker="BG", display_name="邦吉 Bunge Global"),
        _unit_row(ws_id="66666666-6666-4666-8666-666666666666", ws_name="002311.SZ", ticker="002311.SZ", display_name="海大集团"),
        _unit_row(ws_id="77777777-7777-4777-8777-777777777777", ws_name="300750.SZ", ticker="300750.SZ", display_name="宁德时代"),
    ]
    _install_db(monkeypatch, unit_rows=units)
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    assert (await find_existing_company_workspace(USER_CALLER, "Anthropic")).workspace_name == "ANT"
    assert (await find_existing_company_workspace(USER_CALLER, "富途")).workspace_name == "FUTU"
    assert (await find_existing_company_workspace(USER_CALLER, "Futu Holdings")).workspace_name == "FUTU"
    assert (await find_existing_company_workspace(USER_CALLER, "Futu")).workspace_name == "FUTU"
    assert (await find_existing_company_workspace(USER_CALLER, "Interactive Brokers")).workspace_name == "IBKR"
    assert await find_existing_company_workspace(USER_CALLER, "Bunge") is None
    assert (await find_existing_company_workspace(USER_CALLER, "邦吉")).workspace_name == "BG"
    assert (await find_existing_company_workspace(USER_CALLER, "海大集团")).workspace_name == "002311.SZ"
    assert (await find_existing_company_workspace(USER_CALLER, "海大")).workspace_name == "002311.SZ"
    assert (await find_existing_company_workspace(USER_CALLER, "宁德时代")).workspace_name == "300750.SZ"


@pytest.mark.asyncio
async def test_slug_ignored_and_c_plus_plus_does_not_hit_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            {
                **_unit_row(
                    ws_id=WS_1,
                    ws_name="NVDA",
                    ticker="NVDA",
                    display_name="NVIDIA Corporation",
                ),
                "slug": "ai-infrastructure",
            },
            _unit_row(
                ws_id=WS_2,
                ws_name="C",
                ticker="C",
                display_name="Citigroup, Inc.",
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2})

    assert await find_existing_company_workspace(USER_CALLER, "AI Infrastructure") is None
    assert await find_existing_company_workspace(USER_CALLER, "C++") is None
    assert await find_existing_company_workspace(USER_CALLER, "C#") is None


@pytest.mark.asyncio
async def test_team_mode_off_and_external_caller_skip_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_db(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB must not be queried")

    monkeypatch.setattr(dedup_mod, "get_db_connection", _fail_db)

    _enable_team_mode(monkeypatch, supabase_auth=False)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None

    _enable_team_mode(monkeypatch, coverage_enabled=False, supabase_auth=True)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None

    _enable_team_mode(monkeypatch, coverage_enabled=True, supabase_auth=True)
    assert await find_existing_company_workspace(USER_EXTERNAL, "Anthropic") is None


@pytest.mark.asyncio
async def test_excludes_external_and_unknown_owners_and_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="ANT",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            ),
            # External owner's unit must not participate.
            _unit_row(
                ws_id=WS_3,
                ws_name="NVDA",
                ticker="NVDA",
                display_name="NVIDIA",
                owner_id=USER_EXTERNAL,
                owner_email=EMAIL_EXTERNAL,
            ),
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_EXTERNAL,
                owner_name="Ext",
                owner_email=EMAIL_EXTERNAL,
            ),
            _ws_row(
                ws_id=WS_4,
                name="Anthropic",
                owner_id=USER_UNKNOWN,
                owner_name="Unknown",
                owner_email=None,
            ),
        ],
        coverage_rows=[
            # External user's personal alias must be ignored.
            {
                "ticker": "ANT",
                "name": "ExternalSecretAlias",
                "aliases": {"search_names": ["ExternalSecretAlias"]},
                "user_id": USER_EXTERNAL,
                "owner_email": EMAIL_EXTERNAL,
            },
            # Internal user's personal alias matches.
            {
                "ticker": "ANT",
                "name": "Anthropic AI",
                "aliases": {"search_names": ["Claude"]},
                "user_id": USER_RICHARD,
                "owner_email": EMAIL_RICHARD,
            },
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1})

    assert await find_existing_company_workspace(USER_CALLER, "ExternalSecretAlias") is None
    assert await find_existing_company_workspace(USER_CALLER, "NVIDIA") is None
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None

    claude_match = await find_existing_company_workspace(USER_CALLER, "Claude")
    assert claude_match is not None
    assert claude_match.workspace_id == WS_1


@pytest.mark.asyncio
async def test_channel_aware_access_and_unreadable_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="Anthropic, PBC",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
    )
    seen_channels: list[str] = []
    _install_read_access(
        monkeypatch,
        web_readable=set(),
        cos_readable={WS_1},
        seen_channels=seen_channels,
    )

    web_match = await find_existing_company_workspace(
        USER_CALLER, "Anthropic", channel="web"
    )
    assert web_match is not None
    assert web_match.can_read is False
    assert web_match.workspace_id is None
    assert web_match.ticker is None
    assert web_match.lead_analyst_name is None
    assert web_match.matched_workspace_ids == (WS_1,)
    assert web_match.matches_payload() == [
        {
            "workspace_name": "ANT",
            "owner_name": "Richard",
            "can_read": False,
        }
    ]
    assert "你目前没有访问权限" in web_match.message

    cos_match = await find_existing_company_workspace(
        USER_CALLER, "Anthropic", channel="cos"
    )
    assert cos_match is not None
    assert cos_match.can_read is True
    assert cos_match.workspace_id == WS_1
    assert cos_match.ticker == "ANT"
    assert cos_match.lead_analyst_name == "Richard"
    assert seen_channels == ["web", "cos"]


@pytest.mark.asyncio
async def test_caller_own_exact_name_exits_early_for_workspace_name_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="Anthropic, PBC",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_5,
                name="anthropic",
                owner_id=USER_CALLER,
                owner_name="Caller",
                owner_email=EMAIL_CALLER,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_5})

    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_excludes_historical_duplicate_home_deleted_and_sector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    assert SOURCE_HISTORICAL_DUPLICATE == "historical_duplicate"
    home_id = get_home_workspace_id(USER_RICHARD)
    stand_in_id = get_home_stand_in_id(USER_RICHARD)

    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="Anthropic",
                ticker=None,
                display_name="Anthropic",
                kind="sector",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            ),
            _unit_row(
                ws_id=WS_2,
                ws_name="Anthropic Copy",
                ticker="ANT",
                display_name="Anthropic",
                kind="company",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
                is_historical_duplicate=True,
            ),
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
                is_historical_duplicate=True,
            ),
            _ws_row(
                ws_id=home_id,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            ),
            _ws_row(
                ws_id=stand_in_id,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
                is_home_stand_in=True,
                config={HOME_STAND_IN_KEY: True},
            ),
            _ws_row(
                ws_id=WS_3,
                name="Anthropic",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
                status="deleted",
            ),
            _ws_row(
                ws_id=WS_4,
                name="Anthropic",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
                status="unbound",
            ),
            _ws_row(
                ws_id=WS_1,
                name="Anthropic",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_sector_doc_sector_automation_and_non_company_topic_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            # Sector doc workspace (no sector unit) -> excluded.
            _ws_row(
                ws_id=WS_1,
                name="NVDA",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            # Sector automation workspace -> excluded.
            _ws_row(
                ws_id=WS_2,
                name="TSLA",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
            # Ordinary topic workspace (unclassified by identity.py) -> excluded from rule (b).
            _ws_row(
                ws_id=WS_3,
                name="Macro Research",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
            # Company workspace classified by company doc -> matches rule (b).
            _ws_row(
                ws_id=WS_4,
                name="Meta Platforms",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
        ],
        doc_rows=[
            {"workspace_id": WS_1, "doc_key": "sector", "content": {}},
            {"workspace_id": WS_4, "doc_key": "company", "content": {"ticker": "META"}},
        ],
        automation_rows=[
            {
                "user_id": USER_CY,
                "workspace_id": WS_2,
                "name": "dalton-sector-ev-weekly",
                "metadata": {},
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    assert await find_existing_company_workspace(USER_CALLER, "NVDA") is None
    assert await find_existing_company_workspace(USER_CALLER, "TSLA") is None
    assert await find_existing_company_workspace(USER_CALLER, "Macro Research") is None

    meta_match = await find_existing_company_workspace(USER_CALLER, "Meta Platforms")
    assert meta_match is not None
    assert meta_match.workspace_id == WS_4
    assert meta_match.ticker == "META"


@pytest.mark.asyncio
async def test_ticker_rule_and_sorting_cap_at_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="Tencent Unit",
                ticker="0700.HK",
                display_name="Tencent",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW - timedelta(days=10),
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="700.HK",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(days=1),
            ),
            _ws_row(
                ws_id=WS_3,
                name="700",
                owner_id="user-3",
                owner_name="Alice",
                owner_email="alice@everflow.ai",
                last_activity_at=NOW - timedelta(days=2),
            ),
            _ws_row(
                ws_id=WS_4,
                name="0700.HK",
                owner_id="user-4",
                owner_name="Bob",
                owner_email="bob@everflow.ai",
                last_activity_at=NOW - timedelta(days=5),
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    match = await find_existing_company_workspace(USER_CALLER, "0700.HK")
    assert match is not None
    assert len(match.matches) == 3
    assert [m.workspace_name for m in match.matches] == [
        "Tencent Unit",
        "700.HK",
        "700",
    ]


@pytest.mark.asyncio
async def test_connection_released_before_permission_check_and_sql_projects_booleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    sql_log: list[tuple[str, Any]] = []
    conn_state = {"active": False}
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
            )
        ],
        doc_rows=[
            {"workspace_id": WS_2, "doc_key": "company", "content": {"ticker": "ANT"}}
        ],
        sql_log=sql_log,
        conn_state=conn_state,
    )
    _install_read_access(
        monkeypatch, web_readable={WS_2}, conn_state=conn_state
    )

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert conn_state["active"] is False

    # SQL projects only the two booleans rather than fetching full w.config JSONB,
    # and skips dalton_coverage entirely when there are no active company units.
    assert not any("FROM dalton_coverage " in sql for sql, _ in sql_log)
    for sql, _ in sql_log:
        if "FROM workspaces" in sql or "FROM dalton_coverage_units" in sql:
            assert "is_home_stand_in" in sql
            assert "is_historical_duplicate" in sql
            assert "w.config AS" not in sql
            assert "w.config," not in sql


@pytest.mark.asyncio
async def test_permission_503_and_alias_failure_proceed_with_redacted_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
            )
        ],
        doc_rows=[
            {"workspace_id": WS_2, "doc_key": "company", "content": {"ticker": "ANT"}}
        ],
    )
    _install_read_access(
        monkeypatch,
        error_for_ws={
            WS_2: HTTPException(
                status_code=503, detail="secret-db-host-user-data"
            )
        },
    )

    with caplog.at_level(logging.WARNING):
        res_perm = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_perm is None
    assert "HTTPException" in caplog.text
    assert "workspace_count=1" in caplog.text
    assert "secret-db-host-user-data" not in caplog.text
    assert "Anthropic" not in caplog.text

    caplog.clear()
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="ANT",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(ws_id=WS_1, name="ANT", owner_id=USER_RICHARD, owner_email=EMAIL_RICHARD)
        ],
        fail_on_table={
            "FROM dalton_coverage ": RuntimeError("sensitive-alias-payload")
        },
    )
    with caplog.at_level(logging.WARNING):
        res_alias = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_alias is None
    assert "RuntimeError" in caplog.text
    assert "workspace_count=1" in caplog.text
    assert "sensitive-alias-payload" not in caplog.text
    assert "Anthropic" not in caplog.text


def test_chief_of_staff_prompt_non_contradictory() -> None:
    prompt = get_loader().get_system_prompt(
        current_time="2026-11-20 12:00 ET",
        subagent_summary="",
        role="chief_of_staff",
    )
    assert "delegate_to_analyst` into one is refused" in prompt
    assert "company_workspace_exists" in prompt
    assert "不往同事工作区派发任务" in prompt
    assert "除非派发工具允许" not in prompt
    assert "allow_duplicate_company" in prompt


@pytest.mark.asyncio
async def test_rest_create_workspace_returns_409_and_allows_override_with_match_log(
    caplog: pytest.LogCaptureFixture,
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
                can_read=False,
            ),
        ),
        matched_workspace_ids=(WS_1,),
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
                        "can_read": False,
                    }
                ],
            }
            assert dedup_mock.await_args.kwargs["channel"] == "web"
            mgr.create_workspace.assert_not_awaited()

            with caplog.at_level(logging.INFO):
                resp_201 = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Anthropic", "allow_duplicate_company": True},
                )
            assert resp_201.status_code == 201
            assert resp_201.json()["workspace_id"] == WS_5
            assert dedup_mock.await_count == 2
            assert "allow_duplicate_company=True" in caplog.text
            assert WS_1 in caplog.text


@pytest.mark.asyncio
async def test_rest_create_workspace_proceeds_when_dedup_raises(
    caplog: pytest.LogCaptureFixture,
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
    assert "transient error" not in caplog.text


@pytest.mark.asyncio
async def test_manage_workspaces_create_runs_dedup_before_hitl_and_logs_override(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
            ),
        ),
        matched_workspace_ids=(WS_2,),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {"workspace_id": WS_5, "name": "Anthropic"}
    mgr.resolve_binding.return_value = None
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=dup)
    hitl_mock = create_autospec(hitl_confirm, return_value=(True, {}))

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch("src.tools.secretary.tools.hitl_confirm", hitl_mock),
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
                    "can_read": False,
                }
            ],
        }
        assert dedup_mock.await_args.kwargs["channel"] == "cos"
        hitl_mock.assert_not_called()
        mgr.create_workspace.assert_not_awaited()

        with caplog.at_level(logging.INFO):
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
        assert dedup_mock.await_count == 2
        assert "allow_duplicate_company=True" in caplog.text
        assert WS_2 in caplog.text
        hitl_mock.assert_called_once()
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
    assert "db unreachable" not in caplog.text


@pytest.mark.asyncio
async def test_ticker_shaped_ordinary_workspace_not_company_vs_bound_unit_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="NVDA",
                ticker="NVDA",
                display_name="NVIDIA Corporation",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            # Bound company unit without ticker: still classified via SOURCE_REGISTERED.
            _unit_row(
                ws_id=WS_3,
                ws_name="AAPL",
                ticker=None,
                ticker_key=None,
                display_name="Apple Computer",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
        ],
        ws_rows=[
            # Ordinary topic workspace whose name looks like a ticker ("Macro").
            _ws_row(
                ws_id=WS_2,
                name="Macro",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
            _ws_row(
                ws_id=WS_1,
                name="NVDA",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            _ws_row(
                ws_id=WS_3,
                name="AAPL",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3})

    assert await find_existing_company_workspace(USER_CALLER, "Macro") is None

    nvda_match = await find_existing_company_workspace(USER_CALLER, "NVDA")
    assert nvda_match is not None
    assert nvda_match.workspace_id == WS_1

    aapl_match = await find_existing_company_workspace(USER_CALLER, "AAPL")
    assert aapl_match is not None
    assert aapl_match.workspace_id == WS_3


@pytest.mark.asyncio
async def test_ticker_comparison_uses_ticker_key_not_name_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="AX-L",
                ticker="AX-L",
                ticker_key="AX.L",
                display_name="Axon London Ltd.",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="AX-L",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1})

    assert await find_existing_company_workspace(USER_CALLER, "AXL") is None

    match_hyphen = await find_existing_company_workspace(USER_CALLER, "AX-L")
    assert match_hyphen is not None
    assert match_hyphen.workspace_id == WS_1

    match_dot = await find_existing_company_workspace(USER_CALLER, "AX.L")
    assert match_dot is not None
    assert match_dot.workspace_id == WS_1


@pytest.mark.asyncio
async def test_workspace_name_ticker_comparison_uses_ticker_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="AX-L",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
        doc_rows=[
            {"workspace_id": WS_2, "doc_key": "company", "content": {"ticker": "AX-L"}},
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})

    assert await find_existing_company_workspace(USER_CALLER, "AXL") is None
    assert (await find_existing_company_workspace(USER_CALLER, "AX-L")).workspace_id == WS_2
    assert (await find_existing_company_workspace(USER_CALLER, "AX.L")).workspace_id == WS_2


@pytest.mark.asyncio
async def test_identity_lookup_failure_or_fallback_default_allows_creation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.dalton import auth as dalton_auth
    from src.server.database import user as user_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{EMAIL_CALLER},{EMAIL_RICHARD},{EMAIL_CY}",
    )

    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="Anthropic, PBC",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="ANT",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
    )

    _real_get_user = user_db.get_user

    # Case 1: initial caller identity lookup fails -> proceeds and logs only exception class.
    async def _fail_first(uid: str) -> dict[str, Any]:
        raise TimeoutError("secret-dsn-caller-host")

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(_real_get_user, side_effect=_fail_first),
    )
    with caplog.at_level(logging.WARNING):
        res_first = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_first is None
    assert "TimeoutError" in caplog.text
    assert "secret-dsn-caller-host" not in caplog.text
    assert "Anthropic" not in caplog.text

    # Case 2: initial identity check succeeds, then get_user inside
    # require_workspace_access -> get_user_tier times out and returns fallback
    # 'external'; dedup treats it as lookup error (503) and proceeds.
    caplog.clear()
    call_count = {"n": 0}

    async def _fail_second(uid: str) -> dict[str, Any]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"user_id": uid, "email": EMAIL_CALLER}
        raise TimeoutError("secret-pool-timeout-second")

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(_real_get_user, side_effect=_fail_second),
    )
    token = dalton_auth._verified_token_identity.set((USER_CALLER, EMAIL_CALLER))
    try:
        with caplog.at_level(logging.WARNING):
            res_second = await find_existing_company_workspace(
                USER_CALLER, "Anthropic"
            )
    finally:
        dalton_auth._verified_token_identity.reset(token)

    assert res_second is None
    assert call_count["n"] >= 2
    assert "HTTPException" in caplog.text
    assert "workspace_count=1" in caplog.text
    assert "secret-pool-timeout-second" not in caplog.text
    assert "Anthropic" not in caplog.text

    # Case 3: real external member (get_user returns non-allowlisted email) skips dedup without warning.
    caplog.clear()
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            _real_get_user,
            return_value={"user_id": USER_EXTERNAL, "email": EMAIL_EXTERNAL},
        ),
    )
    with caplog.at_level(logging.WARNING):
        res_ext = await find_existing_company_workspace(USER_EXTERNAL, "Anthropic")
    assert res_ext is None
    assert "Company workspace dedup check failed" not in caplog.text


@pytest.mark.asyncio
async def test_caller_own_historical_duplicate_exits_early_while_candidates_exclude_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    sql_log: list[tuple[str, Any]] = []
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="ANT",
                ticker="ANT",
                display_name="Anthropic, PBC",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CALLER,
                owner_name="Caller",
                owner_email=EMAIL_CALLER,
                is_historical_duplicate=True,
                config={"dalton_historical_duplicate_of": WS_1},
            ),
            _ws_row(
                ws_id=WS_3,
                name="OpenAI",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                is_historical_duplicate=True,
                config={"dalton_historical_duplicate_of": WS_4},
            ),
        ],
        sql_log=sql_log,
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3})

    # Caller's own historical duplicate still holds idx_workspaces_user_name_key -> early exit.
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    # Colleague's historical duplicate remains excluded from candidate matching.
    assert await find_existing_company_workspace(USER_CALLER, "OpenAI") is None

    ws_sql = [sql for sql, _ in sql_log if "FROM workspaces " in sql][0]
    assert "is_historical_duplicate" in ws_sql
    assert "WHERE" in ws_sql
    where_part = ws_sql.split("WHERE", 1)[1]
    assert "dalton_historical_duplicate_of" not in where_part


def test_suffix_stripping_stays_linear_on_long_whitespace_runs() -> None:
    """A user-entered display_name must not stall the event loop (codex final review)."""
    import time

    from src.dalton.coverage.dedup import derive_display_name_candidates

    started = time.monotonic()
    derive_display_name_candidates("A" + " " * 199 + "x")
    assert derive_display_name_candidates("A" + " " * 5000 + "x") == ()
    assert time.monotonic() - started < 0.5
    assert derive_display_name_candidates("Costco") == ("Costco",)
    assert derive_display_name_candidates("Coca-Cola Co") == ("Coca-Cola Co", "Coca-Cola")


@pytest.mark.asyncio
async def test_ticker_named_workspace_with_tracking_automation_counts_as_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NVDA` with only `dalton-nvda-watch` (no unit, no company doc) still blocks (codex final review)."""
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="NVDA",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            _ws_row(
                ws_id=WS_2,
                name="Macro",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
        ],
        automation_rows=[
            {
                "user_id": USER_RICHARD,
                "workspace_id": WS_1,
                "name": "dalton-nvda-watch",
                "metadata": {},
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2})

    match = await find_existing_company_workspace(USER_CALLER, "NVDA")
    assert match is not None
    assert match.workspace_id == WS_1
    assert await find_existing_company_workspace(USER_CALLER, "Macro") is None
