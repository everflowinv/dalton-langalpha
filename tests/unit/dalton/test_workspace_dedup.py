"""LLM-based company workspace deduplication before workspace creation (round 4).

Covers:
1. LLM short-ID mapping back to workspaces and discarding out-of-list / invalid-confidence IDs;
2. Fallback to `models.assist_fallback_model` when `models.assist_model` fails;
3. Fail-open on timeout, budget exhaustion, parse failure, and unexpected exceptions
   with redacted warning logs (exception class + workspace_count only);
4. External caller isolation: candidate list contains ONLY caller's own live,
   non-Home, non-historical-duplicate workspaces (no coverage units, no internal data);
5. Internal caller candidate list includes caller's and colleagues' workspaces of all
   kinds (company, sector, topic), enriched with active company coverage units and
   latest company docs, while excluding external / unknown owners, Home, deleted,
   and historical duplicates;
6. Caller's own exact `workspace_name_key` (including historical duplicates) exits
   early without calling `dalton_complete`;
7. Channel-aware read access (`web` vs `cos`) and redaction of `workspace_id`,
   `lead_analyst_name`, `ticker` when unreadable;
8. DB connection released before both LLM call and permission checks, and SQL
   projects only booleans from `w.config`;
9. Parallel batching when candidates > 1000 (`DEDUP_BATCH_SIZE`), merging & sorting
   by confidence (`high` > `medium`), cap at 3, partial batch failure tolerance,
   all-batch failure fail-open, and shared 15s timeout;
10. Chief of Staff prompt non-contradiction, REST 409 + override, CoS tool gate.
"""

from __future__ import annotations

import asyncio
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
    DEDUP_BATCH_SIZE,
    DEDUP_SYSTEM_PROMPT,
    DedupLlmResult,
    DedupMatchItem,
    DuplicateMatch,
    DuplicateWorkspaceCandidate,
    find_existing_company_workspace,
)
from src.dalton.coverage.identity import SOURCE_HISTORICAL_DUPLICATE
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
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
_REAL_DALTON_COMPLETE = dalton_complete

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
    doc_rows: list[dict[str, Any]] | None = None,
    ws_rows_sequence: list[list[dict[str, Any]]] | None = None,
    unit_rows_sequence: list[list[dict[str, Any]]] | None = None,
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
    query_counts = {"ws": 0, "unit": 0}

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
            idx = query_counts["unit"]
            query_counts["unit"] += 1
            if unit_rows_sequence is not None:
                return list(unit_rows_sequence[min(idx, len(unit_rows_sequence) - 1)])
            return list(unit_rows or [])
        if "FROM workspaces" in sql:
            idx = query_counts["ws"]
            query_counts["ws"] += 1
            if ws_rows_sequence is not None:
                return list(ws_rows_sequence[min(idx, len(ws_rows_sequence) - 1)])
            return list(ws_rows or [])
        if "FROM dalton_knowledge_versions" in sql:
            return list(doc_rows or [])
        return []

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction.side_effect = _tx

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


def _install_llm(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: Any,
    *,
    conn_state: dict[str, bool] | None = None,
    captured_calls: list[dict[str, Any]] | None = None,
) -> Any:
    """Install a `create_autospec(dalton_complete)` stub recording all invocations."""

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        if conn_state is not None and conn_state.get("active"):
            raise AssertionError("Outer DB connection was still held during dalton_complete")
        if captured_calls is not None:
            captured_calls.append({"args": args, **kwargs})
        if callable(side_effect):
            res = side_effect(*args, **kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res
        if isinstance(side_effect, Exception):
            raise side_effect
        return side_effect

    mock = create_autospec(_REAL_DALTON_COMPLETE, side_effect=_wrapper)
    monkeypatch.setattr(dedup_mod, "dalton_complete", mock)
    return mock


def _ok_result(matches: list[dict[str, Any]] | DedupLlmResult) -> DaltonCompleteResult:
    data = (
        matches
        if isinstance(matches, DedupLlmResult)
        else DedupLlmResult(
            matches=[DedupMatchItem(**m) for m in matches]
        )
    )
    return DaltonCompleteResult(ok=True, status="done", data=data, model="gemini-4-hydrogen-low-oauth")


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


def test_dedup_system_prompt_has_no_specific_companies_and_covers_criteria() -> None:
    assert "同一家公司" in DEDUP_SYSTEM_PROMPT
    assert "同一行业" in DEDUP_SYSTEM_PROMPT
    assert "上下游" in DEDUP_SYSTEM_PROMPT
    assert "母子公司" in DEDUP_SYSTEM_PROMPT
    assert "Macro" in DEDUP_SYSTEM_PROMPT
    assert "AI" in DEDUP_SYSTEM_PROMPT
    assert "不确定" in DEDUP_SYSTEM_PROMPT
    for forbidden in ("Anthropic", "Nvidia", "NVDA", "Tencent", "腾讯", "富途", "Futu", "Apple", "Meta"):
        assert forbidden not in DEDUP_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_llm_id_maps_to_workspace_and_discards_unknown_or_low_confidence(
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
                last_activity_at=NOW,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="ANT",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW,
            ),
            _ws_row(
                ws_id=WS_2,
                name="Futu",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(days=1),
            ),
        ],
        doc_rows=[
            {
                "workspace_id": WS_2,
                "doc_key": "company",
                "content": {
                    "ticker": "FUTU",
                    "name": "Futu Holdings Limited",
                    "name_zh": "富途控股",
                },
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2})

    calls: list[dict[str, Any]] = []

    def _llm(**kwargs: Any) -> DaltonCompleteResult:
        # Return raw dict payload with one valid match (c2), one hallucinated ID (c999),
        # and one unsupported confidence ("low").
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "matches": [
                    {"id": "c999", "confidence": "high", "reason": "不存在"},
                    {"id": "c1", "confidence": "low", "reason": "不确定"},
                    {"id": "c2", "confidence": "high", "reason": "同一家公司"},
                ]
            },
        )

    _install_llm(monkeypatch, _llm, captured_calls=calls)

    match = await find_existing_company_workspace(USER_CALLER, "富途")
    assert match is not None
    assert len(match.matches) == 1
    assert match.workspace_id == WS_2
    assert match.workspace_name == "Futu"
    assert match.owner_name == "CY"
    assert match.ticker == "FUTU"
    assert len(calls) == 1

    call = calls[0]
    assert call["user_id"] == USER_CALLER
    assert call["system_prompt"] == DEDUP_SYSTEM_PROMPT
    assert call["response_schema"] is DedupLlmResult
    assert call["request_model"] == "gemini-4-hydrogen-low-oauth"
    user_prompt = call["user_prompt"]
    assert "c1 | ANT | ANT | Anthropic, PBC" in user_prompt
    assert "c2 | Futu | FUTU | Futu Holdings Limited / 富途控股" in user_prompt
    # Never send UUIDs or owner identities to the model.
    assert WS_1 not in user_prompt
    assert WS_2 not in user_prompt
    assert "Richard" not in user_prompt
    assert EMAIL_CY not in user_prompt

    # Only out-of-list ID returned -> treated as no match.
    _install_llm(
        monkeypatch,
        lambda **_kw: DaltonCompleteResult(
            ok=True,
            status="done",
            data={"matches": [{"id": "c404", "confidence": "high", "reason": "幻觉"}]},
        ),
    )
    assert await find_existing_company_workspace(USER_CALLER, "富途") is None


@pytest.mark.asyncio
async def test_llm_uses_assist_fallback_model_when_primary_fails(
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
    _install_read_access(monkeypatch, web_readable={WS_1})

    seen_models: list[str] = []

    def _llm(**kwargs: Any) -> DaltonCompleteResult:
        model = kwargs["request_model"]
        seen_models.append(model)
        if model == "gemini-4-hydrogen-low-oauth":
            return DaltonCompleteResult(
                ok=False, status="failed", error_kind="pre_send", model=model
            )
        return _ok_result([{"id": "c1", "confidence": "medium", "reason": "同一公司"}])

    _install_llm(monkeypatch, _llm)

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_1
    assert seen_models == ["gemini-4-hydrogen-low-oauth", "deepseek-flash"]


@pytest.mark.asyncio
async def test_team_mode_off_and_company_tier_skip_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_db(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB must not be queried")

    monkeypatch.setattr(dedup_mod, "get_db_connection", _fail_db)
    llm_mock = _install_llm(monkeypatch, _ok_result([]))

    _enable_team_mode(monkeypatch, supabase_auth=False)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None

    _enable_team_mode(monkeypatch, coverage_enabled=False, supabase_auth=True)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None

    _enable_team_mode(
        monkeypatch, coverage_enabled=True, supabase_auth=True, caller_tier="company"
    )
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    llm_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_caller_candidates_only_contain_own_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    sql_log: list[tuple[str, Any]] = []
    _install_db(
        monkeypatch,
        # Even if mock DB returns internal units or internal workspaces unconditionally,
        # external caller path must not query units and must filter strictly to uid.
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name="InternalANT",
                ticker="ANT",
                display_name="Anthropic, PBC",
                owner_id=USER_RICHARD,
                owner_email=EMAIL_RICHARD,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="InternalANT",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            _ws_row(
                ws_id=WS_2,
                name="ColleagueNVDA",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
            _ws_row(
                ws_id=WS_3,
                name="MyAnthropic",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
            ),
            _ws_row(
                ws_id=WS_4,
                name="ExtOldCopy",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
                is_historical_duplicate=True,
                config={"dalton_historical_duplicate_of": WS_3},
            ),
            _ws_row(
                ws_id=get_home_workspace_id(USER_EXTERNAL),
                name="Anthropic",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
            ),
        ],
        doc_rows=[
            {
                "workspace_id": WS_3,
                "doc_key": "company",
                "content": {"ticker": "ANT", "name": "Anthropic, PBC"},
            },
            {
                "workspace_id": WS_1,
                "doc_key": "company",
                "content": {"ticker": "SECRET", "name": "Internal Secret"},
            },
        ],
        sql_log=sql_log,
    )
    # Use real require_workspace_access to verify external caller's own workspace
    # returns can_read=True with workspace_id (identical structure to internal).
    monkeypatch.setattr(sharing_mod, "require_workspace_access", _REAL_REQ_ACCESS)
    monkeypatch.setattr(
        sharing_mod,
        "require_team_reader",
        create_autospec(sharing_mod.require_team_reader, return_value=None),
    )

    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "本人已有"}]),
        captured_calls=calls,
    )

    match = await find_existing_company_workspace(USER_EXTERNAL, "Anthropic")
    assert match is not None
    assert match.can_read is True
    assert match.workspace_id == WS_3
    assert match.workspace_name == "MyAnthropic"
    assert match.ticker == "ANT"
    assert match.matches_payload() == [
        {
            "workspace_name": "MyAnthropic",
            "owner_name": "ExtUser",
            "lead_analyst_name": None,
            "ticker": "ANT",
            "can_read": True,
            "workspace_id": WS_3,
        }
    ]

    # Must NOT query dalton_coverage_units; initial workspaces query filters by owner_user_id,
    # and post-LLM live-state check queries only the matched workspace_id.
    assert not any("dalton_coverage_units" in sql for sql, _ in sql_log)
    ws_queries = [(sql, params) for sql, params in sql_log if "FROM workspaces" in sql]
    assert len(ws_queries) == 2
    assert "w.user_id = %s" in ws_queries[0][0]
    assert ws_queries[0][1] == (USER_EXTERNAL,)
    assert "w.workspace_id = ANY(%s::uuid[])" in ws_queries[1][0]
    assert ws_queries[1][1] == ([WS_3],)

    # Prompt sent to LLM must contain ONLY external user's own workspace, zero internal data.
    assert len(calls) == 1
    prompt = calls[0]["user_prompt"]
    assert "c1 | MyAnthropic | ANT | Anthropic, PBC" in prompt
    assert "InternalANT" not in prompt
    assert "ColleagueNVDA" not in prompt
    assert "ExtOldCopy" not in prompt
    assert "Internal Secret" not in prompt


@pytest.mark.asyncio
async def test_internal_caller_list_includes_own_colleagues_sector_and_topic_workspaces(
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
                last_activity_at=NOW,
            ),
            # Sector unit must NOT exclude its workspace from the LLM candidate list,
            # nor attach sector unit metadata as a company unit.
            _unit_row(
                ws_id=WS_3,
                ws_name="AI Sector",
                ticker=None,
                display_name="AI Sector",
                kind="sector",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(days=1),
            ),
            # External owner's unit must be excluded.
            _unit_row(
                ws_id="99999999-9999-4999-8999-999999999999",
                ws_name="ExtUnit",
                ticker="EXT",
                display_name="External Co",
                owner_id=USER_EXTERNAL,
                owner_email=EMAIL_EXTERNAL,
            ),
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="ANT",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW,
            ),
            _ws_row(
                ws_id=WS_2,
                name="MyNVDA",
                owner_id=USER_CALLER,
                owner_name="Caller",
                owner_email=EMAIL_CALLER,
                last_activity_at=NOW - timedelta(days=1),
            ),
            _ws_row(
                ws_id=WS_3,
                name="AI Sector",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(days=2),
            ),
            _ws_row(
                ws_id=WS_4,
                name="Macro",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(days=3),
            ),
            # External and unknown owners must be excluded.
            _ws_row(
                ws_id=WS_5,
                name="ExtTopic",
                owner_id=USER_EXTERNAL,
                owner_name="Ext",
                owner_email=EMAIL_EXTERNAL,
            ),
            _ws_row(
                ws_id="88888888-8888-4888-8888-888888888888",
                name="UnknownTopic",
                owner_id=USER_UNKNOWN,
                owner_name="Unknown",
                owner_email=None,
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    calls: list[dict[str, Any]] = []
    _install_llm(monkeypatch, lambda **_kw: _ok_result([]), captured_calls=calls)

    assert await find_existing_company_workspace(USER_CALLER, "OpenAI") is None
    assert len(calls) == 1
    prompt = calls[0]["user_prompt"]
    # Colleague company unit workspace, caller's own workspace, sector workspace,
    # and ordinary topic workspace are all present in the candidate list.
    assert "ANT | ANT | Anthropic, PBC" in prompt
    assert "MyNVDA" in prompt
    assert "AI Sector" in prompt
    assert "Macro" in prompt
    # External & unknown owners excluded.
    assert "ExtUnit" not in prompt
    assert "ExtTopic" not in prompt
    assert "UnknownTopic" not in prompt


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
    seen_channels: list[str] = []
    _install_read_access(
        monkeypatch,
        web_readable=set(),
        cos_readable={WS_1},
        seen_channels=seen_channels,
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "同一公司"}]),
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
async def test_caller_own_exact_name_exits_early_without_calling_llm(
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
    llm_mock = _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "dup"}]),
    )

    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    llm_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_excludes_historical_duplicate_home_and_deleted_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    home_id = get_home_workspace_id(USER_RICHARD)
    stand_in_id = get_home_stand_in_id(USER_RICHARD)

    _install_db(
        monkeypatch,
        unit_rows=[
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
                name="Anthropic Copy",
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
                name="Anthropic Deleted",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
                status="deleted",
            ),
            _ws_row(
                ws_id=WS_4,
                name="Anthropic Unbound",
                owner_id=USER_CY,
                owner_email=EMAIL_CY,
                status="unbound",
            ),
        ],
    )
    llm_mock = _install_llm(monkeypatch, _ok_result([]))

    assert SOURCE_HISTORICAL_DUPLICATE == "historical_duplicate"
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    llm_mock.assert_not_awaited()


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
    calls: list[dict[str, Any]] = []
    _install_llm(monkeypatch, lambda **_kw: _ok_result([]), captured_calls=calls)

    # Caller's own historical duplicate still holds idx_workspaces_user_name_key -> early exit, no LLM call.
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    assert len(calls) == 0

    # Colleague's historical duplicate remains excluded from candidate list sent to LLM.
    assert await find_existing_company_workspace(USER_CALLER, "OpenAI") is None
    assert len(calls) == 1
    assert "OpenAI" not in calls[0]["user_prompt"].split("已有工作区候选清单", 1)[1]

    ws_sql = [sql for sql, _ in sql_log if "FROM workspaces " in sql][0]
    assert "is_historical_duplicate" in ws_sql
    assert "WHERE" in ws_sql
    where_part = ws_sql.split("WHERE", 1)[1]
    assert "dalton_historical_duplicate_of" not in where_part


@pytest.mark.asyncio
async def test_connection_released_before_llm_and_permission_check_and_sql_projects_booleans(
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
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "同一公司"}]),
        conn_state=conn_state,
    )

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert conn_state["active"] is False

    assert not any("FROM dalton_coverage " in sql for sql, _ in sql_log)
    for sql, _ in sql_log:
        if "FROM workspaces" in sql or "FROM dalton_coverage_units" in sql:
            assert "is_home_stand_in" in sql
            assert "is_historical_duplicate" in sql
            assert "w.config AS" not in sql
            assert "w.config," not in sql


@pytest.mark.asyncio
async def test_llm_errors_timeout_budget_parse_and_permission_503_proceed_with_redacted_warning(
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
    _install_read_access(monkeypatch, web_readable={WS_2})

    # 1. TimeoutError from dalton_complete
    _install_llm(monkeypatch, TimeoutError("secret-upstream-timeout-host"))
    with caplog.at_level(logging.WARNING):
        res_timeout = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_timeout is None
    assert "TimeoutError" in caplog.text
    assert "workspace_count=1" in caplog.text
    assert "secret-upstream-timeout-host" not in caplog.text
    assert "Anthropic" not in caplog.text

    # 2. Budget exhausted
    caplog.clear()
    _install_llm(
        monkeypatch,
        lambda **_kw: DaltonCompleteResult(
            ok=False, status="budget_exhausted", error_kind="budget_exhausted"
        ),
    )
    with caplog.at_level(logging.WARNING):
        res_budget = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_budget is None
    assert "RuntimeError" in caplog.text
    assert "Anthropic" not in caplog.text

    # 3. Parse failure (non-JSON text and None data)
    caplog.clear()
    _install_llm(
        monkeypatch,
        lambda **_kw: DaltonCompleteResult(ok=True, status="done", data=None, text="not-json"),
    )
    with caplog.at_level(logging.WARNING):
        res_parse = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_parse is None
    assert "JSONDecodeError" in caplog.text
    assert "Anthropic" not in caplog.text

    # 4. Unexpected exception
    caplog.clear()
    _install_llm(monkeypatch, RuntimeError("secret-llm-api-key"))
    with caplog.at_level(logging.WARNING):
        res_err = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_err is None
    assert "RuntimeError" in caplog.text
    assert "secret-llm-api-key" not in caplog.text
    assert "Anthropic" not in caplog.text

    # 5. Permission 503 after LLM returns match
    caplog.clear()
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "match"}]),
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


@pytest.mark.asyncio
async def test_batched_parallel_over_1000_candidates_merges_caps_and_tolerates_partial_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert DEDUP_BATCH_SIZE == 1000
    _enable_team_mode(monkeypatch)

    total = 2050
    ws_rows = [
        _ws_row(
            ws_id=f"00000000-0000-4000-8000-{i:012d}",
            name=f"Company-{i:04d}",
            owner_id=USER_RICHARD,
            owner_name="Richard",
            owner_email=EMAIL_RICHARD,
            last_activity_at=NOW - timedelta(minutes=i),
        )
        for i in range(1, total + 1)
    ]
    _install_db(monkeypatch, ws_rows=ws_rows)
    _install_read_access(
        monkeypatch,
        web_readable={r["workspace_id"] for r in ws_rows},
    )

    # Case A: All 3 batches run in parallel, results merged & sorted (high > medium), capped at 3,
    # and cross-batch hallucinated IDs are discarded.
    in_flight = {"cur": 0, "max": 0}
    seen_batches: list[list[str]] = []

    async def _parallel_llm(**kwargs: Any) -> DaltonCompleteResult:
        in_flight["cur"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["cur"])
        await asyncio.sleep(0.01)
        in_flight["cur"] -= 1

        prompt = kwargs["user_prompt"]
        lines = [ln for ln in prompt.splitlines() if ln.startswith("c")]
        ids = [ln.split(" | ", 1)[0] for ln in lines]
        seen_batches.append(ids)
        if "c1" in ids:
            return _ok_result(
                [
                    {"id": "c1", "confidence": "medium", "reason": "batch0-med-1"},
                    {"id": "c2", "confidence": "medium", "reason": "batch0-med-2"},
                    # ID belonging to batch 1 must be rejected inside batch 0!
                    {"id": "c1005", "confidence": "high", "reason": "wrong-batch"},
                ]
            )
        if "c1001" in ids:
            return _ok_result(
                [{"id": "c1005", "confidence": "high", "reason": "batch1-high"}]
            )
        return _ok_result(
            [
                {"id": "c2001", "confidence": "high", "reason": "batch2-high"},
                {"id": "c2002", "confidence": "medium", "reason": "batch2-med"},
            ]
        )

    _install_llm(monkeypatch, _parallel_llm)
    match = await find_existing_company_workspace(USER_CALLER, "TargetCo")
    assert match is not None
    assert len(seen_batches) == 3
    assert [len(b) for b in seen_batches] == [1000, 1000, 50]
    assert in_flight["max"] == 3
    assert len(match.matches) == 3
    # Sorted: high confidence (c1005, c2001) before medium (c1).
    assert [m.workspace_name for m in match.matches] == [
        "Company-1005",
        "Company-2001",
        "Company-0001",
    ]

    # Case B: One batch fails across primary & fallback -> remaining batches still produce result.
    caplog.clear()

    async def _partial_fail_llm(**kwargs: Any) -> DaltonCompleteResult:
        prompt = kwargs["user_prompt"]
        if "c1001 | " in prompt:
            raise RuntimeError("batch-1-transient")
        if "c1 | " in prompt:
            return _ok_result([{"id": "c1", "confidence": "medium", "reason": "ok-b0"}])
        return _ok_result([{"id": "c2001", "confidence": "high", "reason": "ok-b2"}])

    _install_llm(monkeypatch, _partial_fail_llm)
    with caplog.at_level(logging.WARNING):
        partial_match = await find_existing_company_workspace(USER_CALLER, "TargetCo")
    assert partial_match is not None
    assert [m.workspace_name for m in partial_match.matches] == [
        "Company-2001",
        "Company-0001",
    ]
    assert "partial batch failure" in caplog.text
    assert "batch-1-transient" not in caplog.text

    # Case C: All batches fail -> fail open (return None).
    caplog.clear()
    _install_llm(monkeypatch, RuntimeError("all-batches-down"))
    with caplog.at_level(logging.WARNING):
        all_fail = await find_existing_company_workspace(USER_CALLER, "TargetCo")
    assert all_fail is None
    assert "RuntimeError" in caplog.text
    assert "all-batches-down" not in caplog.text


@pytest.mark.asyncio
async def test_shared_timeout_cancels_hanging_batches_and_keeps_completed_or_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    ws_rows = [
        _ws_row(
            ws_id=f"00000000-0000-4000-8000-{i:012d}",
            name=f"Company-{i:04d}",
            owner_id=USER_RICHARD,
            owner_name="Richard",
            owner_email=EMAIL_RICHARD,
            last_activity_at=NOW - timedelta(minutes=i),
        )
        for i in range(1, 1005)
    ]
    _install_db(monkeypatch, ws_rows=ws_rows)
    _install_read_access(
        monkeypatch, web_readable={r["workspace_id"] for r in ws_rows}
    )
    monkeypatch.setattr(dedup_mod, "DEDUP_TIMEOUT_SECONDS", 0.08)

    # Batch 0 finishes quickly, Batch 1 hangs past shared deadline -> Batch 0 result is kept.
    async def _one_fast_one_hang(**kwargs: Any) -> DaltonCompleteResult:
        if "c1001 | " in kwargs["user_prompt"]:
            await asyncio.sleep(5.0)
        return _ok_result([{"id": "c1", "confidence": "high", "reason": "fast"}])

    _install_llm(monkeypatch, _one_fast_one_hang)
    partial = await find_existing_company_workspace(USER_CALLER, "TargetCo")
    assert partial is not None
    assert [m.workspace_name for m in partial.matches] == ["Company-0001"]

    # Both batches hang past shared deadline -> fail open (None).
    async def _all_hang(**_kwargs: Any) -> DaltonCompleteResult:
        await asyncio.sleep(5.0)
        return _ok_result([])

    _install_llm(monkeypatch, _all_hang)
    assert await find_existing_company_workspace(USER_CALLER, "TargetCo") is None


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
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "match"}]),
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

    # Case 3: real external member (get_user returns non-allowlisted email) checks only own workspaces (0 here) without warning.
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
async def test_custom_shadowed_model_and_forbidden_role_model_never_selected(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.dalton.llm.model_prefs as model_prefs_mod

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})

    # Case A: caller defines a custom_model with the same name as assist_model
    # plus a custom_provider; model resolution via assist.py/model_prefs.py must
    # drop the shadowed model and use the unshadowed system fallback instead.
    async def _prefs_shadow_primary(uid: str) -> dict[str, Any]:
        if uid == USER_CALLER:
            return {
                "model_preference": {
                    "custom_providers": [
                        {"name": "attacker-proxy", "parent_provider": "openai"}
                    ],
                    "custom_models": [
                        {
                            "name": "gemini-4-hydrogen-low-oauth",
                            "model_id": "evil-intercept",
                            "provider": "attacker-proxy",
                        }
                    ],
                }
            }
        return {"model_preference": {}}

    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_prefs_shadow_primary),
    )

    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "dup"}]),
        captured_calls=calls,
    )

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert len(calls) == 1
    assert calls[0]["request_model"] == "deepseek-flash"

    # Case B: caller shadows BOTH assist_model and assist_fallback_model -> fail open without calling LLM.
    async def _prefs_shadow_both(uid: str) -> dict[str, Any]:
        if uid == USER_CALLER:
            return {
                "model_preference": {
                    "custom_models": [
                        {"name": "gemini-4-hydrogen-low-oauth", "model_id": "m1", "provider": "openai"},
                        {"name": "deepseek-flash", "model_id": "m2", "provider": "openai"},
                    ]
                }
            }
        return {"model_preference": {}}

    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_prefs_shadow_both),
    )
    calls.clear()
    with caplog.at_level(logging.WARNING):
        assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    assert len(calls) == 0
    assert "RuntimeError" in caplog.text

    # Case C: forbidden contributor fallback model is rejected by RolePolicyError -> fail open.
    async def _prefs_empty(_uid: str) -> dict[str, Any]:
        return {"model_preference": {}}

    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_prefs_empty),
    )
    monkeypatch.setenv("DALTON_ASSIST_FALLBACK_MODEL", "gemini-3-contributor-oauth")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    assert len(calls) == 0
    assert "RolePolicyError" in caplog.text


@pytest.mark.asyncio
async def test_primary_9s_cap_fallback_remaining_and_protected_usage_survives_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})

    assert dedup_mod.DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS == 9.0
    assert dedup_mod.DEDUP_TIMEOUT_SECONDS == 15.0

    # Case A: default 9s primary cap & fallback remaining time (< 15s total).
    seen_timeouts: list[tuple[str, float]] = []

    async def _timeout_probe(**kwargs: Any) -> DaltonCompleteResult:
        model = kwargs["request_model"]
        seen_timeouts.append((model, kwargs["timeout_s"]))
        if model == "gemini-4-hydrogen-low-oauth":
            return DaltonCompleteResult(ok=False, status="indeterminate", error_kind="timeout", model=model)
        return _ok_result([{"id": "c1", "confidence": "high", "reason": "dup"}])

    _install_llm(monkeypatch, _timeout_probe)
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert seen_timeouts[0][0] == "gemini-4-hydrogen-low-oauth"
    assert seen_timeouts[0][1] == pytest.approx(9.0, abs=0.2)
    assert seen_timeouts[1][0] == "deepseek-flash"
    assert 9.0 < seen_timeouts[1][1] <= 15.0

    # Case B: primary hangs past its 9s cap (patched to 0.04s) while writing usage;
    # dedup stops waiting and falls back, while primary's protected task is NOT cancelled
    # and finishes writing usage.
    monkeypatch.setattr(dedup_mod, "DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(dedup_mod, "DEDUP_TIMEOUT_SECONDS", 0.5)
    events: dict[str, bool] = {"primary_cancelled": False, "primary_usage_written": False}

    async def _primary_slow_fallback_fast(**kwargs: Any) -> DaltonCompleteResult:
        if kwargs["request_model"] == "gemini-4-hydrogen-low-oauth":
            try:
                await asyncio.sleep(0.12)
                events["primary_usage_written"] = True
            except asyncio.CancelledError:
                events["primary_cancelled"] = True
                raise
            return _ok_result([])
        return _ok_result([{"id": "c1", "confidence": "high", "reason": "fallback"}])

    _install_llm(monkeypatch, _primary_slow_fallback_fast)
    fb_match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert fb_match is not None
    assert fb_match.workspace_id == WS_2
    await asyncio.sleep(0.18)
    assert events["primary_cancelled"] is False
    assert events["primary_usage_written"] is True

    # Case C: both primary and fallback exceed total 15s deadline (patched to 0.06s);
    # dedup returns None (fail-open), while both protected tasks finish writing usage uncancelled.
    monkeypatch.setattr(dedup_mod, "DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(dedup_mod, "DEDUP_TIMEOUT_SECONDS", 0.06)
    both_events: dict[str, int] = {"cancelled": 0, "usage_written": 0}

    async def _both_hang_during_usage_write(**_kwargs: Any) -> DaltonCompleteResult:
        try:
            await asyncio.sleep(0.12)
            both_events["usage_written"] += 1
        except asyncio.CancelledError:
            both_events["cancelled"] += 1
            raise
        return _ok_result([])

    _install_llm(monkeypatch, _both_hang_during_usage_write)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    await asyncio.sleep(0.18)
    assert both_events["cancelled"] == 0
    assert both_events["usage_written"] >= 1


@pytest.mark.asyncio
async def test_concurrent_same_name_dedup_two_users_isolated_with_budget_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    from src.server.services.llm.service import LLMService

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2, WS_1})

    # Shadow primary so both users resolve to the cash model `deepseek-flash`,
    # exercising budget admission + journal usage recording under real `dalton_complete`.
    async def _prefs_cash(uid: str) -> dict[str, Any]:
        return {
            "model_preference": {
                "custom_models": [
                    {"name": "gemini-4-hydrogen-low-oauth", "model_id": "x", "provider": "openai"}
                ]
            }
        }

    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_prefs_cash),
    )

    journal_rows: dict[str, dict[str, Any]] = {}
    admitted_users: list[str] = []
    recorded_done: list[dict[str, Any]] = []

    async def _budget_ok(user_id: str, **_kw: Any) -> tuple[bool, None, None]:
        admitted_users.append(user_id)
        return True, None, None

    async def _inspect(inv_id: str, **_kw: Any) -> tuple[str, dict[str, Any] | None]:
        row = journal_rows.get(inv_id)
        if row is not None:
            raise AssertionError(f"Duplicate invocation_id across concurrent dedup calls: {inv_id}")
        return "proceed", None

    async def _claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        inv_id = kwargs["invocation_id"]
        if inv_id in journal_rows:
            raise AssertionError(f"invocation_id collided on claim: {inv_id}")
        journal_rows[inv_id] = {
            "invocation_id": inv_id,
            "user_id": kwargs["user_id"],
            "scope_ref": kwargs["scope_ref"],
            "state": "pending",
            "attempt": 1,
        }
        return "claimed", journal_rows[inv_id]

    async def _done(inv_id: str, **kwargs: Any) -> dict[str, Any]:
        journal_rows[inv_id]["state"] = "done"
        journal_rows[inv_id]["usage"] = kwargs.get("usage")
        recorded_done.append({"invocation_id": inv_id, "user_id": journal_rows[inv_id]["user_id"], **kwargs})
        return journal_rows[inv_id]

    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, side_effect=_budget_ok),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, side_effect=_done),
    )

    llm_service = create_autospec(LLMService, instance=True)

    async def _llm_complete(**_kw: Any) -> tuple[DedupLlmResult, dict[str, Any]]:
        await asyncio.sleep(0.02)
        return (
            DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="same")]),
            {"input_tokens": 100, "output_tokens": 20},
        )

    llm_service.complete.side_effect = _llm_complete

    async def _patched_complete(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        kwargs["llm_service"] = llm_service
        return await _REAL_DALTON_COMPLETE(*args, **kwargs)

    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(_REAL_DALTON_COMPLETE, side_effect=_patched_complete),
    )

    res_a, res_b = await asyncio.gather(
        find_existing_company_workspace(USER_CALLER, "Anthropic"),
        find_existing_company_workspace(USER_RICHARD, "Anthropic"),
    )
    assert res_a is not None and res_b is not None
    assert res_a.workspace_id == WS_2 and res_b.workspace_id == WS_2
    assert sorted(admitted_users) == sorted([USER_CALLER, USER_RICHARD])
    assert len(journal_rows) == 2
    assert {r["user_id"] for r in journal_rows.values()} == {USER_CALLER, USER_RICHARD}
    assert all(uid in row["scope_ref"] for row in journal_rows.values() for uid in [row["user_id"]])
    assert len(recorded_done) == 2
    assert llm_service.complete.await_count == 2


@pytest.mark.asyncio
async def test_workspace_deleted_unbound_or_historical_during_llm_wait_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})

    # Case A: WS_2 deleted, WS_3 unbound, WS_4 marked historical duplicate during LLM wait;
    # only WS_1 remains live.
    _install_db(
        monkeypatch,
        ws_rows_sequence=[
            [
                _ws_row(ws_id=WS_1, name="Anthropic", owner_id=USER_RICHARD, owner_name="Richard", owner_email=EMAIL_RICHARD),
                _ws_row(ws_id=WS_2, name="ANT-2", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY),
                _ws_row(ws_id=WS_3, name="ANT-3", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY),
                _ws_row(ws_id=WS_4, name="ANT-4", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY),
            ],
            [
                _ws_row(ws_id=WS_1, name="Anthropic", owner_id=USER_RICHARD, owner_name="Richard", owner_email=EMAIL_RICHARD, status="running"),
                _ws_row(ws_id=WS_2, name="ANT-2", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY, status="deleted"),
                _ws_row(ws_id=WS_3, name="ANT-3", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY, status="unbound"),
                _ws_row(ws_id=WS_4, name="ANT-4", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY, is_historical_duplicate=True),
            ],
        ],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            [
                {"id": "c2", "confidence": "high", "reason": "deleted"},
                {"id": "c3", "confidence": "high", "reason": "unbound"},
                {"id": "c4", "confidence": "high", "reason": "historical"},
                {"id": "c1", "confidence": "medium", "reason": "live"},
            ]
        ),
    )
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert [m.workspace_id for m in match.matches] == [WS_1]

    # Case B: only matched workspace disappears completely from DB while waiting -> returns None.
    _install_db(
        monkeypatch,
        ws_rows_sequence=[
            [_ws_row(ws_id=WS_2, name="Anthropic", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY)],
            [],
        ],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "deleted"}]),
    )
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_missing_matches_triggers_fallback_confidence_case_and_per_batch_cap_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    # Schema requires `matches` (empty dict fails validation).
    with pytest.raises(ValidationError):
        DedupLlmResult.model_validate({})

    # `confidence` is `str`, so `"low"` / `"HIGH"` do not fail Pydantic validation.
    parsed_schema = DedupLlmResult.model_validate(
        {
            "matches": [
                {"id": "c1", "confidence": "HIGH", "reason": "upper"},
                {"id": "c2", "confidence": "low", "reason": "dropped later"},
            ]
        }
    )
    assert len(parsed_schema.matches) == 2

    _enable_team_mode(monkeypatch)
    ws_rows = [
        _ws_row(
            ws_id=f"00000000-0000-4000-8000-{i:012d}",
            name=f"AntCo-{i}",
            owner_id=USER_RICHARD,
            owner_name="Richard",
            owner_email=EMAIL_RICHARD,
            last_activity_at=NOW - timedelta(minutes=i),
        )
        for i in range(1, 7)
    ]
    _install_db(monkeypatch, ws_rows=ws_rows)
    _install_read_access(monkeypatch, web_readable={r["workspace_id"] for r in ws_rows})

    seen_models: list[str] = []

    def _llm_missing_then_5(**kwargs: Any) -> DaltonCompleteResult:
        model = kwargs["request_model"]
        seen_models.append(model)
        if model == "gemini-4-hydrogen-low-oauth":
            # Primary omits `matches` -> treated as parse failure -> switches to fallback!
            return DaltonCompleteResult(ok=True, status="done", data={}, text="{}")
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "matches": [
                    {"id": "c1", "confidence": "low", "reason": "ignored-low"},
                    {"id": "c2", "confidence": "Medium", "reason": "med-2"},
                    {"id": "c3", "confidence": "medium", "reason": "med-3"},
                    {"id": "c4", "confidence": "HIGH", "reason": "high-4"},
                    {"id": "c5", "confidence": "High", "reason": "high-5"},
                    {"id": "c6", "confidence": "unknown", "reason": "ignored"},
                ]
            },
        )

    _install_llm(monkeypatch, _llm_missing_then_5)
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert seen_models == ["gemini-4-hydrogen-low-oauth", "deepseek-flash"]
    # High confidence (c4, c5) before medium (c2), capped at 3 per batch; low/unknown dropped.
    assert [m.workspace_name for m in match.matches] == ["AntCo-4", "AntCo-5", "AntCo-2"]


@pytest.mark.asyncio
async def test_unbound_company_unit_candidates_return_shape_rest_cos_and_archived_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    unbound_unit = {
        "unit_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_RICHARD,
        "lead_analyst_user_id": USER_RICHARD,
        "display_name": "Anthropic, PBC",
        "workspace_id": None,
        "ticker": "ANT",
        "unit_updated_at": NOW,
        "unit_created_at": NOW,
        "workspace_name": None,
        "workspace_owner_id": None,
        "workspace_status": None,
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "owner_name": None,
        "owner_email": None,
        "unit_owner_email": EMAIL_RICHARD,
        "lead_name": "Richard",
        "lead_email": EMAIL_RICHARD,
    }

    # 1. External caller NEVER receives unbound units.
    _install_db(monkeypatch, unit_rows=[unbound_unit], ws_rows=[])
    llm_mock = _install_llm(monkeypatch, _ok_result([]))
    assert await find_existing_company_workspace(USER_EXTERNAL, "Anthropic") is None
    llm_mock.assert_not_awaited()

    # 2. Internal caller sees unbound unit formatted with `-` in workspace name column,
    # and hit returns display_name, ticker, lead_analyst_name without workspace_id.
    _install_db(monkeypatch, unit_rows=[unbound_unit], ws_rows=[])
    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "registered unit"}]),
        captured_calls=calls,
    )
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert len(calls) == 1
    assert "c1 | - | ANT | Anthropic, PBC" in calls[0]["user_prompt"]
    assert match.workspace_id is None
    assert match.matched_workspace_ids == ()
    assert match.message == "「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区"
    assert match.matches_payload() == [
        {
            "unbound_unit": True,
            "display_name": "Anthropic, PBC",
            "ticker": "ANT",
            "lead_analyst_name": "Richard",
            "can_read": True,
        }
    ]

    # 3. REST 409 & CoS manage_workspaces propagate unbound unit payload cleanly.
    from src.server.app.workspaces import router

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=match)
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/workspaces", json={"name": "Anthropic"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == {
            "code": "company_workspace_exists",
            "message": "「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区",
            "matches": match.matches_payload(),
        }

        cos_res = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {"action": "create", "name": "Anthropic", "state": {}},
                "id": "call-unbound",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
        cos_payload = json.loads(cos_res.update["messages"][0].content)
        assert cos_payload == {
            "success": False,
            "error": match.message,
            "code": "company_workspace_exists",
            "message": match.message,
            "matches": match.matches_payload(),
        }

    # 4. Unbound unit archived while waiting for LLM -> dropped -> returns None.
    _install_db(
        monkeypatch,
        unit_rows_sequence=[
            [unbound_unit],
            [{**unbound_unit, "status": "archived"}],
        ],
        ws_rows=[],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "stale"}]),
    )
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None


@pytest.mark.asyncio
async def test_uuid_redaction_fixed_4_columns_cleanup_external_cred_error_and_probe_script(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert not hasattr(DuplicateWorkspaceCandidate, "matched_by")

    _enable_team_mode(monkeypatch)
    uuid_a = "12345678-1234-4123-8123-123456789abc"
    uuid_b = "abcdefab-cdef-4abc-8abc-abcdefabcdef"
    uuid_c = "99999999-aaaa-4bbb-8ccc-dddddddddddd"

    sql_log: list[tuple[str, Any]] = []
    _install_db(
        monkeypatch,
        unit_rows=[
            _unit_row(
                ws_id=WS_1,
                ws_name=f"deal_{uuid_a}",
                ticker="ANT",
                display_name=f"Anthropic {uuid_b}",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW,
            )
        ],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name=f"deal_{uuid_a}",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW,
            ),
            _ws_row(
                ws_id=WS_2,
                name="Macro",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(minutes=5),
            ),
            _ws_row(
                ws_id=WS_3,
                name="Anthropic",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(minutes=10),
            ),
        ],
        doc_rows=[
            {
                "workspace_id": WS_3,
                "doc_key": "company",
                "content": {"ticker": "", "name": "Anthropic, PBC", "name_zh": ""},
            }
        ],
        sql_log=sql_log,
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3})

    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
        captured_calls=calls,
    )

    match = await find_existing_company_workspace(USER_CALLER, f"New_{uuid_c}")
    assert match is not None
    # Returned workspace_name keeps real name, while prompt sent to LLM replaces all UUIDs with [id].
    assert match.workspace_name == f"deal_{uuid_a}"
    prompt = calls[0]["user_prompt"]
    assert uuid_a not in prompt
    assert uuid_b not in prompt
    assert uuid_c not in prompt
    assert "待新建工作区名称：New_[id]" in prompt
    assert "c1 | deal_[id] | ANT | Anthropic [id]" in prompt
    assert "c2 | Macro | - | -" in prompt
    assert "c3 | Anthropic | - | Anthropic, PBC" in prompt
    assert "固定 4 列" in prompt

    # SQL no longer selects unused ticker_key column.
    unit_sqls = [sql for sql, _ in sql_log if "FROM dalton_coverage_units" in sql]
    assert unit_sqls
    assert all("ticker_key" not in sql for sql in unit_sqls)

    # External member with missing credentials -> fails open with redacted warning.
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_4,
                name="MyNVDA",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
            )
        ],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: DaltonCompleteResult(
            ok=False, status="failed", error_kind="pre_send", error="oauth_required"
        ),
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await find_existing_company_workspace(USER_EXTERNAL, "NVIDIA") is None
    assert "Company workspace dedup check failed" in caplog.text
    assert "RuntimeError" in caplog.text

    # Probe script --show-candidates captures candidates from the second query when the
    # first query exits early due to caller's own same-name workspace.
    import scripts.dalton.ops.probe_workspace_dedup as probe_mod
    import src.server.database.pool as pool_mod

    fake_pool = create_autospec(pool_mod.get_or_create_pool(), instance=True)
    fake_pool.open.return_value = None
    fake_pool.close.return_value = None
    monkeypatch.setattr(pool_mod, "get_or_create_pool", lambda: fake_pool)

    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="OwnCo",
                owner_id=USER_CALLER,
                owner_name="Caller",
                owner_email=EMAIL_CALLER,
                last_activity_at=NOW,
            ),
            _ws_row(
                ws_id=WS_2,
                name="TargetCo",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(minutes=1),
            ),
        ],
    )
    monkeypatch.setattr(pool_mod, "get_db_connection", dedup_mod.get_db_connection)
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2})
    _install_llm(monkeypatch, lambda **_kw: _ok_result([]))

    report = await probe_mod.probe_names(
        ["OwnCo", "OtherCo"], user_id=USER_CALLER, show_candidates=True
    )
    assert report["results"][0]["early_exit_same_name"] is True
    assert len(report["candidates"]) == 2
    assert any("TargetCo | - | -" in ln for ln in report["candidates"])
    assert "模型凭据刷新时应用会更新凭据记录" in (probe_mod.__doc__ or "")
