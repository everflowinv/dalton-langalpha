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
    import src.config.settings as app_settings
    import src.dalton.settings as dalton_settings_mod

    monkeypatch.setenv(
        "DALTON_COVERAGE_UNITS_ENABLED", "true" if coverage_enabled else "false"
    )
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(dalton_settings_mod, "HOST_MODE", "oss")
    monkeypatch.delenv("DALTON_ASSIST_FALLBACK_MODEL", raising=False)
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
    import src.config.settings as app_settings
    from src.dalton import auth as dalton_auth
    import src.dalton.settings as dalton_settings_mod
    from src.server.database import user as user_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(dalton_settings_mod, "HOST_MODE", "oss")
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
    assert "only when colleague dispatch is enabled for this deployment and the workspace's access is `rw`" in prompt
    assert "company_workspace_exists" in prompt
    assert "仅在部署已启用同事派发且该工作区权限为 `rw` 时才可派发" in prompt
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
    import src.dalton.settings as settings_mod

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
    # plus personal dalton_assist_model; dedup ignores user preferences and
    # still uses the deployment default from dalton.yaml.
    async def _prefs_shadow_primary(uid: str) -> dict[str, Any]:
        return {
            "model_preference": {
                "dalton_assist_model": "moonshot-kimi-k2.5",
                "dalton_assist_fallback_models": ["gpt-6.2-sol-oauth"],
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
    assert calls[0]["request_model"] == "gemini-4-hydrogen-low-oauth"

    # Case B: deployment dalton.yaml has neither assist_model nor fallback -> fail open without calling LLM.
    base_cfg = settings_mod.get_dalton_settings()
    empty_models_cfg = base_cfg.model_copy(
        update={
            "models": base_cfg.models.model_copy(
                update={"assist_model": "", "assist_fallback_model": []}
            )
        }
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=empty_models_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=[]),
    )
    calls.clear()
    with caplog.at_level(logging.WARNING):
        assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    assert len(calls) == 0
    assert "RuntimeError" in caplog.text

    # Case C: forbidden contributor fallback model is rejected by RolePolicyError -> fail open.
    monkeypatch.undo()
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
    calls.clear()
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "dup"}]),
        captured_calls=calls,
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

    # Configure deployment assist model as cash model `deepseek-flash`,
    # exercising budget admission + journal usage recording under real `dalton_complete`.
    import src.dalton.settings as settings_mod

    base_cfg = settings_mod.get_dalton_settings()
    cash_cfg = base_cfg.model_copy(
        update={
            "models": base_cfg.models.model_copy(
                update={"assist_model": "deepseek-flash", "assist_fallback_model": []}
            )
        }
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=cash_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=[]),
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


# ---------------------------------------------------------------------------
# Round 6 tests: server-side credentials only, external isolation, no error leak,
# send-time 9s cap + 15s pref read, unit rebind/unbind refresh, refresh-then-cap,
# boundary-free UUID redaction, CoS allow_duplicate_company post-approval logging.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_caller_ignores_byok_base_url_and_uses_server_credentials_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.credentials as cred_mod
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.server.database.api_keys as api_keys_db
    import src.server.database.oauth_tokens as oauth_tokens_db
    import src.server.database.user as user_db
    import src.server.services.llm.service as llm_svc_mod
    import src.server.services.oauth_health as oauth_health_mod
    from src.server.services.oauth_protocol import TokenSet

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

    # Minimal AgentConfig & no-op journal/budget stubs for real `dalton_complete` + `LLMService.complete`.
    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="gemini-4-hydrogen-low-oauth", background="deepseek-flash")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )

    budget_calls: list[str] = []
    journal_claims: list[dict[str, Any]] = []
    journal_dones: list[dict[str, Any]] = []

    async def _budget_ok(uid: str, **_kw: Any) -> tuple[bool, None, None]:
        budget_calls.append(uid)
        return True, None, None

    async def _inspect(_inv_id: str, **_kw: Any) -> tuple[str, None]:
        return "proceed", None

    async def _claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        journal_claims.append(dict(kwargs))
        return "claimed", {"attempt": 1, **kwargs}

    async def _done(inv_id: str, **kwargs: Any) -> dict[str, Any]:
        journal_dones.append({"invocation_id": inv_id, **kwargs})
        return {"invocation_id": inv_id, **kwargs}

    async def _failed(inv_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"invocation_id": inv_id, **kwargs}

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
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, side_effect=_failed),
    )

    # Case A: Team mode ON, credential sharing OFF, caller saved a custom BYOK key & base_url
    # for `deepseek`. Primary OAuth model is skipped/rejected (no team holder), fallback
    # `deepseek-flash` MUST use platform env key + official endpoint, NEVER caller's base_url!
    settings_mod.get_dalton_config.cache_clear()
    orig_load_cfg = settings_mod._load_base_dalton_config
    base_dalton_cfg = orig_load_cfg()
    off_cfg = base_dalton_cfg.model_copy(
        update={
            "enabled": True,
            "credentials": settings_mod.DaltonCredentialsConfig(sharing="off"),
        }
    )
    monkeypatch.setattr(
        settings_mod,
        "_load_base_dalton_config",
        create_autospec(orig_load_cfg, return_value=off_cfg),
    )
    settings_mod.get_dalton_config.cache_clear()

    async def _empty_prefs(_uid: str) -> dict[str, Any]:
        return {"model_preference": {}}

    prefs_stub = create_autospec(
        model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_empty_prefs
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", prefs_stub)
    monkeypatch.setattr(user_db, "get_user_preferences", prefs_stub)

    byok_spy = create_autospec(
        api_keys_db.get_byok_config_for_provider,
        return_value={"api_key": "sk-caller-evil", "base_url": "https://evil-exfil.example/v1"},
    )
    monkeypatch.setattr(api_keys_db, "get_byok_config_for_provider", byok_spy)
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_configs_for_providers",
        create_autospec(
            api_keys_db.get_byok_configs_for_providers,
            return_value={"deepseek": {"api_key": "sk-caller-evil", "base_url": "https://evil-exfil.example/v1"}},
        ),
    )

    seen_clients: list[Any] = []

    async def _fake_api_call(llm: Any, **_kw: Any) -> tuple[DedupLlmResult, dict[str, Any]]:
        seen_clients.append(llm)
        return (
            DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="same")]),
            {"input_tokens": 50, "output_tokens": 10},
        )

    monkeypatch.setattr(
        llm_svc_mod,
        "make_api_call",
        create_autospec(llm_svc_mod.make_api_call, side_effect=_fake_api_call),
    )

    # First sub-case: no DEEPSEEK_API_KEY env -> no server-side credential available -> fails open!
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    assert len(seen_clients) == 0
    byok_spy.assert_not_called()

    # Second sub-case: DEEPSEEK_API_KEY set in env -> uses platform key & official endpoint,
    # NEVER caller's BYOK key or evil base_url!
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-platform-deepseek-env")
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_2
    assert len(seen_clients) == 1
    built_llm = seen_clients[0]
    api_base = str(
        getattr(built_llm, "anthropic_api_url", None)
        or getattr(built_llm, "api_base", None)
        or getattr(built_llm, "base_url", None)
        or ""
    )
    assert "evil-exfil.example" not in api_base
    api_key_val = getattr(built_llm, "anthropic_api_key", None) or getattr(
        built_llm, "api_key", None
    )
    secret_str = api_key_val.get_secret_value() if hasattr(api_key_val, "get_secret_value") else str(api_key_val)
    assert secret_str == "sk-platform-deepseek-env"
    byok_spy.assert_not_called()
    assert budget_calls == [USER_CALLER, USER_CALLER]
    assert journal_claims[-1]["user_id"] == USER_CALLER

    # Case B: Team mode ON, credential sharing ON with DALTON_PM_USER_IDS=USER_RICHARD.
    # Primary OAuth model uses HOLDER's (USER_RICHARD) OAuth token while budget/journal stay on USER_CALLER.
    monkeypatch.setattr(settings_mod, "_load_base_dalton_config", orig_load_cfg)
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_RICHARD)
    settings_mod.get_dalton_config.cache_clear()
    assert cred_mod.get_team_credential_holder_id() == USER_RICHARD

    oauth_lookup_users: list[str] = []

    async def _valid_tok(uid: str, provider: str) -> TokenSet | None:
        oauth_lookup_users.append(uid)
        if uid == USER_RICHARD and provider == "antigravity-oauth":
            return TokenSet(
                access_token="holder-agy-token",
                refresh_token="holder-agy-refresh",
                account_id="acct-holder",
                email=EMAIL_RICHARD,
                plan_type="pro",
                expires_at=NOW + timedelta(hours=2),
            )
        return None

    monkeypatch.setattr(
        oauth_health_mod,
        "valid_token",
        create_autospec(oauth_health_mod.valid_token, side_effect=_valid_tok),
    )
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_tokens",
        create_autospec(oauth_tokens_db.get_oauth_tokens, return_value=None),
    )

    seen_clients.clear()
    journal_claims.clear()
    journal_dones.clear()

    # Load user context stubs so resolve_llm_config runs cleanly for USER_RICHARD.
    import src.server.services.user_skills as user_skills_mod

    monkeypatch.setattr(
        user_skills_mod,
        "load_user_skill_bundle",
        create_autospec(
            user_skills_mod.load_user_skill_bundle,
            return_value=user_skills_mod.EMPTY_USER_SKILL_BUNDLE,
        ),
    )
    import src.server.services.features as features_mod

    monkeypatch.setattr(
        features_mod,
        "effective_flags_for_user",
        create_autospec(features_mod.effective_flags_for_user, return_value={}),
    )

    match_team = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match_team is not None
    assert oauth_lookup_users
    assert set(oauth_lookup_users) == {USER_RICHARD}
    assert len(seen_clients) == 1
    assert journal_claims[0]["user_id"] == USER_CALLER
    assert journal_dones[0]["usage"]["credential_user_id"] == USER_RICHARD
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_external_caller_uses_team_shared_credentials_and_own_candidates_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.credentials as cred_mod
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.server.database.api_keys as api_keys_db
    import src.server.database.oauth_tokens as oauth_tokens_db
    import src.server.database.user as user_db
    import src.server.services.features as features_mod
    import src.server.services.llm.service as llm_svc_mod
    import src.server.services.oauth_health as oauth_health_mod
    from src.server.services.oauth_protocol import TokenSet
    import src.server.services.user_skills as user_skills_mod

    _enable_team_mode(monkeypatch)
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_RICHARD)
    settings_mod.get_dalton_config.cache_clear()
    assert cred_mod.is_team_credential_sharing_enabled() is True

    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_4,
                name="MyNVDA",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
            ),
            _ws_row(
                ws_id=WS_2,
                name="TeamSecretNvidia",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_4, WS_2})

    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="gemini-4-hydrogen-low-oauth", background="deepseek-flash")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )

    async def _empty_prefs(_uid: str) -> dict[str, Any]:
        return {"model_preference": {}}

    prefs_stub = create_autospec(
        model_prefs_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_empty_prefs
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", prefs_stub)
    monkeypatch.setattr(user_db, "get_user_preferences", prefs_stub)
    monkeypatch.setattr(
        user_skills_mod,
        "load_user_skill_bundle",
        create_autospec(
            user_skills_mod.load_user_skill_bundle,
            return_value=user_skills_mod.EMPTY_USER_SKILL_BUNDLE,
        ),
    )
    monkeypatch.setattr(
        features_mod,
        "effective_flags_for_user",
        create_autospec(features_mod.effective_flags_for_user, return_value={}),
    )

    journal_claims: list[dict[str, Any]] = []
    journal_dones: list[dict[str, Any]] = []

    async def _claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        journal_claims.append(dict(kwargs))
        return "claimed", {"attempt": 1, **kwargs}

    async def _done(inv_id: str, **kwargs: Any) -> dict[str, Any]:
        journal_dones.append({"invocation_id": inv_id, **kwargs})
        return {"invocation_id": inv_id, **kwargs}

    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
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
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value={}),
    )

    # Team holder (USER_RICHARD) has OAuth tokens in DB; external user (USER_EXTERNAL) has none.
    seen_oauth_queries: list[tuple[str, str, bool]] = []

    async def _valid_tok(uid: str, provider: str) -> TokenSet | None:
        resolved_uid = cred_mod.credential_user_id(uid)
        seen_oauth_queries.append((uid, resolved_uid, cred_mod.is_team_credential_sharing_enabled()))
        if resolved_uid == USER_RICHARD and provider == "antigravity-oauth":
            return TokenSet(
                access_token="holder-token",
                refresh_token="holder-refresh",
                account_id="acct-holder",
                email=EMAIL_RICHARD,
                plan_type="pro",
                expires_at=NOW + timedelta(hours=2),
            )
        return None

    monkeypatch.setattr(
        oauth_health_mod,
        "valid_token",
        create_autospec(oauth_health_mod.valid_token, side_effect=_valid_tok),
    )
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_tokens",
        create_autospec(oauth_tokens_db.get_oauth_tokens, return_value=None),
    )
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_config_for_provider",
        create_autospec(api_keys_db.get_byok_config_for_provider, return_value=None),
    )
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_configs_for_providers",
        create_autospec(api_keys_db.get_byok_configs_for_providers, return_value={}),
    )

    seen_prompts: list[str] = []

    async def _fake_api_call(_llm: Any, **kw: Any) -> tuple[DedupLlmResult, dict[str, Any]]:
        seen_prompts.append(str(kw.get("user_prompt") or ""))
        return (
            DedupLlmResult(
                matches=[DedupMatchItem(id="c1", confidence="high", reason="same")],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA Corporation",
            ),
            {"input_tokens": 50, "output_tokens": 10},
        )

    monkeypatch.setattr(
        llm_svc_mod,
        "make_api_call",
        create_autospec(llm_svc_mod.make_api_call, side_effect=_fake_api_call),
    )

    res = await find_existing_company_workspace(USER_EXTERNAL, "NVIDIA")
    assert res is not None
    assert res.workspace_id == WS_4
    assert res.suggestion is None
    assert len(seen_prompts) == 1
    assert "MyNVDA" in seen_prompts[0]
    assert "TeamSecretNvidia" not in seen_prompts[0]
    assert seen_oauth_queries
    assert all(
        uid == USER_EXTERNAL and resolved == USER_RICHARD and sharing is True
        for uid, resolved, sharing in seen_oauth_queries
    )
    assert journal_claims[0]["user_id"] == USER_EXTERNAL
    assert journal_dones[0]["usage"]["credential_user_id"] == USER_RICHARD
    assert cred_mod.is_team_credential_sharing_enabled() is True
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_dedup_llm_error_text_never_written_to_provider_status_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.dalton.database.provider_events as pe_db
    import src.dalton.llm.complete as complete_mod
    import src.dalton.services.provider_status as ps_mod
    from src.server.services.llm.service import LLMService

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="SecretHiddenCo",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )

    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(
            complete_mod.claim_journal_invocation,
            return_value=("claimed", {"attempt": 1}),
        ),
    )

    failed_records: list[dict[str, Any]] = []

    async def _record_fail(inv_id: str, **kwargs: Any) -> dict[str, Any]:
        failed_records.append({"invocation_id": inv_id, **kwargs})
        return {"invocation_id": inv_id, **kwargs}

    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, side_effect=_record_fail),
    )

    recorded_events: list[dict[str, Any]] = []

    async def _rec_event(provider: str, kind: str, **kwargs: Any) -> int:
        recorded_events.append({"provider": provider, "kind": kind, **kwargs})
        return 1

    record_event_spy = create_autospec(complete_mod.record_provider_event, side_effect=_rec_event)
    monkeypatch.setattr(complete_mod, "record_provider_event", record_event_spy)
    monkeypatch.setattr(pe_db, "record_provider_event", record_event_spy)

    async def _list_events(prov: str | None = None, **_kw: Any) -> list[dict[str, Any]]:
        return [ev for ev in recorded_events if prov is None or ev.get("provider") == prov]

    monkeypatch.setattr(
        ps_mod,
        "list_recent_provider_events",
        create_autospec(pe_db.list_recent_provider_events, side_effect=_list_events),
    )
    monkeypatch.setattr(
        ps_mod,
        "count_provider_errors_24h",
        create_autospec(pe_db.count_provider_errors_24h, return_value={}),
    )
    monkeypatch.setattr(
        ps_mod,
        "get_last_refresh_event",
        create_autospec(pe_db.get_last_refresh_event, return_value=None),
    )
    monkeypatch.setattr(
        ps_mod,
        "_fetch_usage_stats_24h",
        create_autospec(ps_mod._fetch_usage_stats_24h, return_value={}),
    )
    monkeypatch.setattr(
        ps_mod,
        "explain_model_availability",
        create_autospec(ps_mod.explain_model_availability, return_value={}),
    )
    import src.server.database.api_keys as api_keys_db
    import src.server.database.oauth_tokens as oauth_tokens_db

    monkeypatch.setattr(
        api_keys_db,
        "get_user_api_keys",
        create_autospec(api_keys_db.get_user_api_keys, return_value={"byok_enabled": False, "keys": {}, "base_urls": {}}),
    )
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_connection",
        create_autospec(oauth_tokens_db.get_oauth_connection, return_value=None),
    )

    llm_service = create_autospec(LLMService, instance=True)
    llm_service.complete.side_effect = ValueError("Parse error referencing SecretHiddenCo / SHC")

    async def _run_real_complete(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        kwargs["llm_service"] = llm_service
        return await _REAL_DALTON_COMPLETE(*args, **kwargs)

    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(_REAL_DALTON_COMPLETE, side_effect=_run_real_complete),
    )

    # Use cash fallback model only so `LLMService.complete` is called with ValueError.
    import src.dalton.llm.model_prefs as model_prefs_mod

    async def _prefs_cash(_uid: str) -> dict[str, Any]:
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

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res is None
    record_event_spy.assert_not_awaited()
    assert recorded_events == []
    assert failed_records
    for rec in failed_records:
        assert "SecretHiddenCo" not in str(rec.get("error_text") or "")
        assert "SHC" not in str(rec.get("error_text") or "")
        assert rec.get("error_text") == "ValueError"
    assert "SecretHiddenCo" not in caplog.text
    assert "SHC" not in caplog.text

    status_payload = await ps_mod.get_providers_status(user_id=USER_CALLER)
    for prow in status_payload["providers"]:
        assert prow["recent_errors"] == []


@pytest.mark.asyncio
async def test_primary_9s_deadline_checked_at_send_time_and_pref_read_within_15s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.llm.complete as complete_mod
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
    _install_read_access(monkeypatch, web_readable={WS_2})

    # Case A: model resolution hangs past total 15s (patched to 0.05s) ->
    # dedup times out around 0.05s without invoking dalton_complete.
    monkeypatch.setattr(dedup_mod, "DEDUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(dedup_mod, "DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS", 0.03)
    real_resolve_assist = dedup_mod.resolve_assist_model

    async def _slow_resolve_assist(**_kw: Any) -> str:
        await asyncio.sleep(0.30)
        return "gemini-4-hydrogen-low-oauth"

    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(real_resolve_assist, side_effect=_slow_resolve_assist),
    )
    llm_spy = _install_llm(monkeypatch, _ok_result([]))
    t0 = asyncio.get_running_loop().time()
    assert await find_existing_company_workspace(USER_CALLER, "Anthropic") is None
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.20
    llm_spy.assert_not_awaited()

    # Case B: claim_journal_invocation sleeps past primary's 9s cap (patched to 0.04s)
    # while total 15s deadline (patched to 0.6s) still has room.
    # Primary MUST NOT send to llm_service.complete at send time (raises DeadlineBeforeSendError),
    # while fallback model sends and succeeds!
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(real_resolve_assist, return_value="gemini-4-hydrogen-low-oauth"),
    )
    monkeypatch.setattr(dedup_mod, "DEDUP_PRIMARY_MODEL_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(dedup_mod, "DEDUP_TIMEOUT_SECONDS", 0.60)
    monkeypatch.setattr(dedup_mod, "get_team_credential_holder_id", lambda: USER_RICHARD)

    async def _slow_primary_claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        if kwargs.get("model_key") == "gemini-4-hydrogen-low-oauth":
            await asyncio.sleep(0.08)
        return "claimed", {"attempt": 1}

    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, side_effect=_slow_primary_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value={}),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, return_value={}),
    )

    sent_models: list[str] = []
    llm_service = create_autospec(LLMService, instance=True)

    async def _llm_complete(**kwargs: Any) -> tuple[DedupLlmResult, dict[str, Any]]:
        sent_models.append(str(kwargs.get("request_model") or ""))
        return (
            DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="fb")]),
            {"input_tokens": 10, "output_tokens": 5},
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

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    await asyncio.sleep(0.10)
    assert match is not None
    assert match.workspace_id == WS_2
    assert sent_models == ["deepseek-flash"]


@pytest.mark.asyncio
async def test_refresh_unit_unbound_rebound_and_deduped_by_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    unit_id_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    unit_id_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"

    # Case A: initially bound unit U1 -> WS_1 is UNBOUND during LLM wait ->
    # returns as unbound_unit=True with no workspace_id.
    bound_u1 = {
        **_unit_row(
            ws_id=WS_1,
            ws_name="ANT",
            ticker="ANT",
            display_name="Anthropic, PBC",
            owner_id=USER_RICHARD,
            owner_name="Richard",
            owner_email=EMAIL_RICHARD,
        ),
        "unit_id": unit_id_1,
        "status": "active",
    }
    unbound_u1 = {
        **bound_u1,
        "workspace_id": None,
        "workspace_name": None,
        "workspace_owner_id": None,
        "workspace_status": None,
        "owner_name": None,
        "owner_email": None,
    }
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[bound_u1], [unbound_u1]],
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
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
    )
    m_unbound = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert m_unbound is not None
    assert m_unbound.workspace_id is None
    assert m_unbound.matched_workspace_ids == ()
    assert m_unbound.message == "「Anthropic」已登记为团队覆盖公司（主分析师 Richard），尚未绑定工作区"
    assert m_unbound.matches_payload() == [
        {
            "unbound_unit": True,
            "display_name": "Anthropic, PBC",
            "ticker": "ANT",
            "lead_analyst_name": "Richard",
            "can_read": True,
        }
    ]

    # Case B: initially bound unit U1 -> WS_1 is REBOUND to WS_2 (owned by CY) during wait ->
    # returns WS_2 with CY as owner_name (both readable and unreadable paths).
    rebound_u1 = {**bound_u1, "workspace_id": WS_2}
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[bound_u1], [rebound_u1]],
        ws_rows_sequence=[
            [
                _ws_row(
                    ws_id=WS_1,
                    name="ANT",
                    owner_id=USER_RICHARD,
                    owner_name="Richard",
                    owner_email=EMAIL_RICHARD,
                )
            ],
            [
                _ws_row(
                    ws_id=WS_2,
                    name="Anthropic-CY",
                    owner_id=USER_CY,
                    owner_name="CY",
                    owner_email=EMAIL_CY,
                )
            ],
        ],
    )
    _install_read_access(monkeypatch, web_readable=set())
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
    )
    m_rebound = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert m_rebound is not None
    assert m_rebound.workspace_name == "Anthropic-CY"
    assert m_rebound.owner_name == "CY"
    assert m_rebound.workspace_id is None
    assert m_rebound.matched_workspace_ids == (WS_2,)

    # Case C: unbound unit U2 newly bound to WS_2 during wait while WS_2 was ALSO matched ->
    # deduplicated by workspace_id with WS_2's owner_name ("CY"), not lead_analyst_name ("Richard").
    unbound_u2 = {
        "unit_id": unit_id_2,
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
    bound_u2_to_ws2 = {**unbound_u2, "workspace_id": WS_2}
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[unbound_u2], [bound_u2_to_ws2]],
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="Anthropic-CY",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            [
                {"id": "c1", "confidence": "high", "reason": "unit"},
                {"id": "c2", "confidence": "high", "reason": "ws"},
            ]
        ),
    )
    m_dedup = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert m_dedup is not None
    assert len(m_dedup.matches) == 1
    assert m_dedup.matches[0].workspace_id == WS_2
    assert m_dedup.matches[0].owner_name == "CY"
    assert m_dedup.matches[0].lead_analyst_name == "Richard"


@pytest.mark.asyncio
async def test_refresh_before_truncating_to_3_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    total = DEDUP_BATCH_SIZE + 2  # 2 batches
    initial_ws = [
        _ws_row(
            ws_id=f"00000000-0000-4000-8000-{i:012d}",
            name=f"Cand-{i}",
            owner_id=USER_RICHARD,
            owner_name="Richard",
            owner_email=EMAIL_RICHARD,
            last_activity_at=NOW - timedelta(minutes=i),
        )
        for i in range(1, total + 1)
    ]
    # Top 3 hits (Cand-1, Cand-2, Cand-3) are deleted during wait; Cand-1002 (batch 2) survives!
    refreshed_ws = [
        {**row, "status": "deleted"} if idx < 3 else row
        for idx, row in enumerate(initial_ws)
    ]
    _install_db(monkeypatch, ws_rows_sequence=[initial_ws, refreshed_ws])
    _install_read_access(
        monkeypatch,
        web_readable={r["workspace_id"] for r in initial_ws},
    )

    def _batch_llm(**kwargs: Any) -> DaltonCompleteResult:
        prompt = kwargs["user_prompt"]
        if "c1 |" in prompt:
            return _ok_result(
                [
                    {"id": "c1", "confidence": "high", "reason": "b1-1"},
                    {"id": "c2", "confidence": "high", "reason": "b1-2"},
                    {"id": "c3", "confidence": "high", "reason": "b1-3"},
                ]
            )
        return _ok_result(
            [
                {"id": f"c{DEDUP_BATCH_SIZE + 2}", "confidence": "medium", "reason": "b2-survivor"},
            ]
        )

    _install_llm(monkeypatch, _batch_llm)
    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert [m.workspace_name for m in match.matches] == [f"Cand-{total}"]


@pytest.mark.asyncio
async def test_uuid_redaction_ignores_adjacent_hex_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    u1 = "12345678-1234-4123-8123-123456789abc"
    u2 = "abcdefab-cdef-4abc-8abc-abcdefabcdef"
    u3 = "99999999-aaaa-4bbb-8ccc-dddddddddddd"

    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name=f"case{u1}ef",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            )
        ],
        doc_rows=[
            {
                "workspace_id": WS_1,
                "doc_key": "company",
                "content": {"ticker": "", "name": f"0x{u2}ff", "name_zh": ""},
            }
        ],
    )
    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([]),
        captured_calls=calls,
    )

    assert await find_existing_company_workspace(USER_CALLER, f"deadbeef{u3}00") is None
    assert len(calls) == 1
    prompt = calls[0]["user_prompt"]
    assert u1 not in prompt
    assert u2 not in prompt
    assert u3 not in prompt
    assert "待新建工作区名称：deadbeef[id]00" in prompt
    assert "c1 | case[id]ef | - | 0x[id]ff" in prompt


@pytest.mark.asyncio
async def test_cos_allow_duplicate_company_logs_only_after_approval(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.tools.secretary.tools as sec_tools

    monkeypatch.setattr(
        sec_tools,
        "duplicate_workspace",
        create_autospec(sec_tools.duplicate_workspace, return_value=(None, True)),
    )

    dedup_spy = create_autospec(
        _REAL_FIND_DEDUP,
        return_value=DuplicateMatch(
            company="Anthropic",
            message="dup",
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
            matched_workspace_ids=(WS_1,),
        ),
    )

    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_2,
        "name": "Anthropic",
        "dir_name": "Anthropic",
    }

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_spy),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
        patch.object(
            sec_tools,
            "_folder_ready_result",
            create_autospec(sec_tools._folder_ready_result, return_value={}),
        ),
    ):
        # 1. Approval declined -> dedup NOT called and skip log NOT emitted.
        monkeypatch.setattr(
            sec_tools,
            "hitl_confirm",
            create_autospec(
                hitl_confirm,
                return_value=(False, {"decisions": [{"type": "reject", "message": "no"}]}),
            ),
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            declined_cmd = await workspaces_create(
                USER_CALLER,
                "Anthropic",
                None,
                "call-decline",
                preapproved=False,
                allow_duplicate_company=True,
            )
        assert "Skipping company workspace dedup check" not in caplog.text
        dedup_spy.assert_not_awaited()
        mgr.create_workspace.assert_not_awaited()
        assert "declined" in json.dumps(declined_cmd.update["messages"][0].content).lower() or "cancelled" in json.dumps(declined_cmd.update["messages"][0].content).lower() or "cancel" in json.dumps(declined_cmd.update["messages"][0].content).lower() or "reject" in json.dumps(declined_cmd.update["messages"][0].content).lower() or declined_cmd.update["messages"][0].content

        # 2. Approval granted -> dedup called ONCE after hitl_confirm, skip log emitted ONCE.
        monkeypatch.setattr(
            sec_tools,
            "hitl_confirm",
            create_autospec(hitl_confirm, return_value=(True, "ok")),
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            ok_cmd = await workspaces_create(
                USER_CALLER,
                "Anthropic",
                None,
                "call-approve",
                preapproved=False,
                allow_duplicate_company=True,
            )
        dedup_spy.assert_awaited_once()
        mgr.create_workspace.assert_awaited_once()
        assert caplog.text.count("Skipping company workspace dedup check") == 1
        assert WS_1 in caplog.text
        assert json.loads(ok_cmd.update["messages"][0].content)["success"] is True

        # 3. allow_duplicate_company=False -> dedup still runs BEFORE hitl_confirm and rejects without card.
        dedup_spy.reset_mock()
        hitl_spy = create_autospec(hitl_confirm, return_value=(True, "ok"))
        monkeypatch.setattr(sec_tools, "hitl_confirm", hitl_spy)
        rej_cmd = await workspaces_create(
            USER_CALLER,
            "Anthropic",
            None,
            "call-reject-before-card",
            preapproved=False,
            allow_duplicate_company=False,
        )
        dedup_spy.assert_awaited_once()
        hitl_spy.assert_not_called()
        assert json.loads(rej_cmd.update["messages"][0].content)["code"] == "company_workspace_exists"


# ---------------------------------------------------------------------------
# Round 7 tests: deployment-only model resolution, external platform fallback
# prohibition, GLM Coding Plan deployment key, probe is_external, debug log
# parse-error redaction, empty credential holder on deployment key calls.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_assist_model_preferences_do_not_affect_dedup_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod

    _enable_team_mode(monkeypatch)
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_RICHARD)
    settings_mod.get_dalton_config.cache_clear()
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

    # Both caller and team credential holder configure personal dalton_assist_model
    # and dalton_assist_fallback_models pointing to another provider (moonshot / gpt).
    user_prefs_spy = create_autospec(
        model_prefs_mod._DEFAULT_GET_USER_PREFERENCES,
        return_value={
            "model_preference": {
                "dalton_assist_model": "moonshot-kimi-k2.5",
                "dalton_assist_fallback_models": ["gpt-6.2-sol-oauth"],
            }
        },
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", user_prefs_spy)

    seen_models: list[str] = []

    def _fail_primary_then_match(**kwargs: Any) -> DaltonCompleteResult:
        model = str(kwargs["request_model"])
        seen_models.append(model)
        if model == "gemini-4-hydrogen-low-oauth":
            return DaltonCompleteResult(ok=False, status="failed", error_kind="pre_send")
        return _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}])

    _install_llm(monkeypatch, _fail_primary_then_match)

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_2
    assert seen_models == ["gemini-4-hydrogen-low-oauth", "deepseek-flash"]
    user_prefs_spy.assert_not_awaited()
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_external_caller_follows_normal_chat_platform_fallback_when_sharing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.server.database.api_keys as api_keys_db
    import src.server.database.oauth_tokens as oauth_tokens_db
    import src.server.database.user as user_db
    import src.server.services.features as features_mod
    import src.server.services.llm.service as llm_svc_mod
    import src.server.services.user_skills as user_skills_mod

    _enable_team_mode(monkeypatch)
    orig_load_cfg = settings_mod._load_base_dalton_config
    base_cfg = orig_load_cfg()
    off_cfg = base_cfg.model_copy(
        update={
            "enabled": True,
            "credentials": settings_mod.DaltonCredentialsConfig(sharing="off"),
            "models": base_cfg.models.model_copy(
                update={
                    "assist_model": "gemini-4-hydrogen-low-oauth",
                    "assist_fallback_model": ["deepseek-flash"],
                }
            ),
        }
    )
    monkeypatch.setattr(
        settings_mod,
        "_load_base_dalton_config",
        create_autospec(orig_load_cfg, return_value=off_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=off_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=["deepseek-flash"]),
    )
    settings_mod.get_dalton_config.cache_clear()

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
    _install_read_access(monkeypatch, web_readable={WS_4})

    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="gemini-4-hydrogen-low-oauth", background="deepseek-flash")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )
    prefs_stub = create_autospec(
        model_prefs_mod._DEFAULT_GET_USER_PREFERENCES,
        return_value={"model_preference": {}},
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", prefs_stub)
    monkeypatch.setattr(user_db, "get_user_preferences", prefs_stub)
    monkeypatch.setattr(
        user_skills_mod,
        "load_user_skill_bundle",
        create_autospec(
            user_skills_mod.load_user_skill_bundle,
            return_value=user_skills_mod.EMPTY_USER_SKILL_BUNDLE,
        ),
    )
    monkeypatch.setattr(
        features_mod,
        "effective_flags_for_user",
        create_autospec(features_mod.effective_flags_for_user, return_value={}),
    )
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, return_value=("claimed", {"attempt": 1})),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, return_value={}),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value={}),
    )
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_tokens",
        create_autospec(oauth_tokens_db.get_oauth_tokens, return_value=None),
    )
    import src.server.services.oauth_health as oauth_health_mod

    monkeypatch.setattr(oauth_health_mod, "get_oauth_tokens", oauth_tokens_db.get_oauth_tokens)
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_config_for_provider",
        create_autospec(api_keys_db.get_byok_config_for_provider, return_value=None),
    )
    monkeypatch.setattr(
        api_keys_db,
        "get_byok_configs_for_providers",
        create_autospec(api_keys_db.get_byok_configs_for_providers, return_value={}),
    )

    api_call_spy = create_autospec(
        llm_svc_mod.make_api_call,
        return_value=(
            DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="same")]),
            {"input_tokens": 10, "output_tokens": 5},
        ),
    )
    monkeypatch.setattr(llm_svc_mod, "make_api_call", api_call_spy)

    # 1. Sharing OFF + no own creds + no platform env key -> fails open.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert await find_existing_company_workspace(USER_EXTERNAL, "NVIDIA") is None
    api_call_spy.assert_not_awaited()

    # 2. Sharing OFF + DEEPSEEK_API_KEY set -> falls back to platform key, same as normal chat.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-platform-deepseek-secret")
    match = await find_existing_company_workspace(USER_EXTERNAL, "NVIDIA")
    assert match is not None
    assert match.workspace_id == WS_4
    api_call_spy.assert_awaited_once()
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_glm_coding_plan_works_with_deployment_key_when_sharing_off_and_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.server.services.llm.service as llm_svc_mod

    _enable_team_mode(monkeypatch)
    assert complete_mod.model_requires_user_oauth("glm-5.3-flash-plan") is False
    assert complete_mod.model_requires_user_oauth("gemini-4-hydrogen-low-oauth") is True

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

    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="glm-5.3-flash-plan", background="glm-5.3-flash-plan")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, return_value=("claimed", {"attempt": 1})),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, return_value={}),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value={}),
    )

    seen_clients: list[Any] = []

    async def _fake_api_call(llm: Any, **_kw: Any) -> tuple[DedupLlmResult, dict[str, Any]]:
        seen_clients.append(llm)
        return (
            DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="same")]),
            {"input_tokens": 30, "output_tokens": 10},
        )

    monkeypatch.setattr(
        llm_svc_mod,
        "make_api_call",
        create_autospec(llm_svc_mod.make_api_call, side_effect=_fake_api_call),
    )

    # Configure deployment assist model as glm-5.3-flash-plan (coding_plan subscription).
    orig_load_cfg = settings_mod._load_base_dalton_config
    base_cfg = orig_load_cfg()
    off_coding_cfg = base_cfg.model_copy(
        update={
            "enabled": True,
            "credentials": settings_mod.DaltonCredentialsConfig(sharing="off"),
            "models": base_cfg.models.model_copy(
                update={"assist_model": "glm-5.3-flash-plan", "assist_fallback_model": []}
            ),
        }
    )
    monkeypatch.setattr(
        settings_mod,
        "_load_base_dalton_config",
        create_autospec(orig_load_cfg, return_value=off_coding_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=off_coding_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=[]),
    )
    settings_mod.get_dalton_config.cache_clear()
    monkeypatch.setenv("ZAI_CN_CODING_API_KEY", "sk-zai-coding-deploy-key")

    # 1. With sharing=off and valid ZAI_CN_CODING_API_KEY, glm-5.3-flash-plan succeeds!
    match_off = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match_off is not None
    assert match_off.workspace_id == WS_2
    assert len(seen_clients) == 1
    api_key_obj = getattr(seen_clients[0], "anthropic_api_key", None) or getattr(
        seen_clients[0], "api_key", None
    )
    secret_val = (
        api_key_obj.get_secret_value()
        if hasattr(api_key_obj, "get_secret_value")
        else str(api_key_obj)
    )
    assert secret_val == "sk-zai-coding-deploy-key"

    # 2. With sharing=team, glm-5.3-flash-plan also works via deployment key.
    team_coding_cfg = off_coding_cfg.model_copy(
        update={"credentials": settings_mod.DaltonCredentialsConfig(sharing="team")}
    )
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_RICHARD)
    monkeypatch.setattr(
        settings_mod,
        "_load_base_dalton_config",
        create_autospec(orig_load_cfg, return_value=team_coding_cfg),
    )
    settings_mod.get_dalton_config.cache_clear()
    seen_clients.clear()

    match_team = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match_team is not None
    assert match_team.workspace_id == WS_2
    assert len(seen_clients) == 1
    settings_mod.get_dalton_config.cache_clear()


@pytest.mark.asyncio
async def test_probe_script_passes_is_external_for_external_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.dalton.ops.probe_workspace_dedup as probe_mod
    import src.server.database.pool as pool_mod

    _enable_team_mode(monkeypatch)
    fake_pool = create_autospec(pool_mod.get_or_create_pool(), instance=True)
    fake_pool.open.return_value = None
    fake_pool.close.return_value = None
    monkeypatch.setattr(pool_mod, "get_or_create_pool", lambda: fake_pool)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_4,
                name="ExtNVDA",
                owner_id=USER_EXTERNAL,
                owner_name="ExtUser",
                owner_email=EMAIL_EXTERNAL,
            ),
            _ws_row(
                ws_id=WS_2,
                name="TeamNVDA",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
        ],
    )
    monkeypatch.setattr(pool_mod, "get_db_connection", dedup_mod.get_db_connection)
    _install_read_access(monkeypatch, web_readable={WS_4, WS_2})

    calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([]),
        captured_calls=calls,
    )
    judge_spy = create_autospec(
        dedup_mod._judge_candidates_with_llm,
        wraps=dedup_mod._judge_candidates_with_llm,
    )
    monkeypatch.setattr(dedup_mod, "_judge_candidates_with_llm", judge_spy)

    # External member -> is_external=True (own candidates only), standard team/normal-chat creds (no disable flags).
    ext_report = await probe_mod.probe_names(["NVDA"], user_id=USER_EXTERNAL)
    assert ext_report["caller_tier"] == "external"
    assert judge_spy.call_args.kwargs["is_external"] is True
    assert len(calls) == 1
    assert calls[0]["is_byok"] is True
    assert "disable_team_credentials" not in calls[0]
    assert "disable_platform_fallback" not in calls[0]

    # Internal member -> is_external=False.
    calls.clear()
    judge_spy.reset_mock()
    int_report = await probe_mod.probe_names(["NVDA"], user_id=USER_CALLER)
    assert int_report["caller_tier"] == "internal"
    assert judge_spy.call_args.kwargs["is_external"] is False
    assert calls[0]["is_byok"] is False
    assert "disable_team_credentials" not in calls[0]
    assert "disable_platform_fallback" not in calls[0]


@pytest.mark.asyncio
async def test_dedup_parse_retry_exhaustion_debug_log_omits_candidate_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.llms.api_call as api_call_mod
    import src.server.services.llm.service as llm_svc_mod

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="SecretCandidateCorp",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )

    base_cfg = settings_mod.get_dalton_settings()
    one_model_cfg = base_cfg.model_copy(
        update={
            "models": base_cfg.models.model_copy(
                update={"assist_model": "deepseek-flash", "assist_fallback_model": []}
            )
        }
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=one_model_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=[]),
    )

    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="deepseek-flash", background="deepseek-flash")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, return_value=("claimed", {"attempt": 1})),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value={}),
    )

    structured_runnable = create_autospec(Runnable, instance=True)
    structured_runnable.ainvoke.side_effect = RuntimeError("force manual parse fallback")

    fake_llm = create_autospec(BaseChatModel, instance=True)
    fake_llm.with_structured_output.return_value = structured_runnable
    # Returns JSON missing required `confidence`, so Pydantic ValidationError embeds `SecretCandidateCorp`.
    fake_llm.ainvoke.return_value = AIMessage(
        content='{"matches": [{"id": "c1", "reason": "SecretCandidateCorp"}]}'
    )

    monkeypatch.setattr(
        llm_svc_mod,
        "create_llm",
        create_autospec(llm_svc_mod.create_llm, return_value=fake_llm),
    )
    monkeypatch.setattr(api_call_mod.asyncio, "sleep", create_autospec(asyncio.sleep, return_value=None))

    # 1. During dedup with DEBUG logging enabled, SecretCandidateCorp MUST NOT appear in logs.
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        res = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res is None
    assert "SecretCandidateCorp" not in caplog.text
    assert "Failed to parse response after 5 attempts. Last error: ValidationError" in caplog.text

    # 2. Outside dedup, normal callers keep standard api_call debug logging unchanged.
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError, match="SecretCandidateCorp"):
            await api_call_mod.make_api_call(
                fake_llm,
                system_prompt="sys",
                user_prompt="usr",
                response_schema=DedupLlmResult,
                max_parsing_retries=1,
            )
    assert "SecretCandidateCorp" in caplog.text


@pytest.mark.asyncio
async def test_deployment_key_journal_leaves_credential_holder_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg
    import ptc_agent.config as ptc_cfg_mod
    from ptc_agent.config.agent import LLMConfig
    import src.dalton.database.llm_journal as journal_db
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.model_prefs as model_prefs_mod
    import src.dalton.settings as settings_mod
    import src.server.services.llm.service as llm_svc_mod

    _enable_team_mode(monkeypatch)
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER_RICHARD)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-platform-deepseek")
    settings_mod.get_dalton_config.cache_clear()

    base_cfg = settings_mod.get_dalton_settings()
    deepseek_cfg = base_cfg.model_copy(
        update={
            "models": base_cfg.models.model_copy(
                update={"assist_model": "deepseek-flash", "assist_fallback_model": []}
            )
        }
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_dalton_settings",
        create_autospec(model_prefs_mod.get_dalton_settings, return_value=deepseek_cfg),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_assist_fallback_models",
        create_autospec(model_prefs_mod.get_assist_fallback_models, return_value=[]),
    )

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

    # Spy on real `claim_journal_invocation` and `complete_journal_done` backed by an autospec cursor.
    executed_journal: list[tuple[str, Any]] = []
    j_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    j_cur.__aenter__.return_value = j_cur
    j_cur.__aexit__.return_value = None

    async def _j_execute(sql: str, params: Any = None) -> None:
        executed_journal.append((sql, params))

    j_cur.execute.side_effect = _j_execute
    j_cur.fetchone.side_effect = [
        {"invocation_id": "lj1:test", "user_id": USER_CALLER, "state": "pending", "attempt": 1},
        {"invocation_id": "lj1:test", "user_id": USER_CALLER, "state": "done", "usage": {}},
    ]
    j_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    j_conn.cursor.return_value = j_cur

    @asynccontextmanager
    async def _j_tx():
        yield

    j_conn.transaction.side_effect = _j_tx

    @asynccontextmanager
    async def _get_j_conn(_c: Any = None):
        yield j_conn

    monkeypatch.setattr(journal_db, "get_db_connection", _get_j_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )

    fake_cfg = create_autospec(ptc_cfg_mod.AgentConfig, instance=True)
    fake_cfg.llm = LLMConfig(name="deepseek-flash", background="deepseek-flash")
    monkeypatch.setattr(
        ptc_cfg_mod,
        "load_from_files",
        create_autospec(ptc_cfg_mod.load_from_files, return_value=fake_cfg),
    )
    monkeypatch.setattr(
        llm_svc_mod,
        "make_api_call",
        create_autospec(
            llm_svc_mod.make_api_call,
            return_value=(
                DedupLlmResult(matches=[DedupMatchItem(id="c1", confidence="high", reason="same")]),
                {"input_tokens": 25, "output_tokens": 8},
            ),
        ),
    )

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_2
    assert len(executed_journal) == 2

    claim_sql, claim_params = executed_journal[0]
    done_sql, done_params = executed_journal[1]
    assert "usage" not in claim_sql
    assert USER_RICHARD not in repr(claim_params)
    assert "COALESCE(usage" not in done_sql
    assert "usage = %s::jsonb" in done_sql
    done_usage = json.loads(done_params[1])
    assert "credential_user_id" not in done_usage
    assert done_usage["input_tokens"] == 25
    settings_mod.get_dalton_config.cache_clear()


# ---------------------------------------------------------------------------
# Round 8 tests: A1 (_refresh_live_hits doc_ticker isolation), A2 (primary
# pre-send deadline fallback), B2 (model prefill suggestion), B3/B4/B5 (auto
# coverage unit registration on web & CoS, conflict non-rollback, external gate).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_live_hits_drops_old_doc_ticker_on_unbind_and_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_team_mode(monkeypatch)
    unit_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    bound_private = _unit_row(
        ws_id=WS_1,
        ws_name="PrivateProj",
        ticker=None,
        display_name="ByteDance",
        owner_id=USER_CY,
        owner_name="CY",
        owner_email=EMAIL_CY,
    )
    bound_private["unit_id"] = unit_id

    # Case 1: unit without unit_ticker bound to private WS_1 (doc_ticker="PRIV1")
    # gets unbound during LLM wait -> unbound match must NOT leak "PRIV1".
    unbound_now = {**bound_private, "workspace_id": None, "ticker": None}
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[bound_private], [unbound_now]],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="PrivateProj",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
        doc_rows=[
            {
                "workspace_id": WS_1,
                "doc_key": "company",
                "content": {"ticker": "PRIV1", "name": "ByteDance", "name_zh": "字节跳动"},
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable=set())
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
    )
    m_unbound = await find_existing_company_workspace(USER_CALLER, "ByteDance")
    assert m_unbound is not None
    assert m_unbound.matches_payload() == [
        {
            "unbound_unit": True,
            "display_name": "ByteDance",
            "ticker": None,
            "lead_analyst_name": "CY",
            "can_read": True,
        }
    ]

    # Case 2: unit without unit_ticker bound to private WS_1 (doc_ticker="PRIV1")
    # gets rebound to readable WS_2 during LLM wait -> must NOT attach WS_1's "PRIV1".
    rebound_to_ws2 = {**bound_private, "workspace_id": WS_2, "ticker": None}
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[bound_private], [rebound_to_ws2]],
        ws_rows_sequence=[
            [
                _ws_row(
                    ws_id=WS_1,
                    name="PrivateProj",
                    owner_id=USER_CY,
                    owner_name="CY",
                    owner_email=EMAIL_CY,
                )
            ],
            [
                _ws_row(
                    ws_id=WS_2,
                    name="ByteDance-Shared",
                    owner_id=USER_RICHARD,
                    owner_name="Richard",
                    owner_email=EMAIL_RICHARD,
                )
            ],
        ],
        doc_rows=[
            {
                "workspace_id": WS_1,
                "doc_key": "company",
                "content": {"ticker": "PRIV1", "name": "ByteDance", "name_zh": "字节跳动"},
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
    )
    m_rebound = await find_existing_company_workspace(USER_CALLER, "ByteDance")
    assert m_rebound is not None
    assert m_rebound.workspace_id == WS_2
    assert m_rebound.ticker is None

    # Case 3: binding remains WS_1 (readable) -> doc_ticker "DOC1" IS preserved.
    _install_db(
        monkeypatch,
        unit_rows_sequence=[[bound_private], [bound_private]],
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="PrivateProj",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
        doc_rows=[
            {
                "workspace_id": WS_1,
                "doc_key": "company",
                "content": {"ticker": "DOC1", "name": "ByteDance", "name_zh": "字节跳动"},
            }
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1})
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}]),
    )
    m_same = await find_existing_company_workspace(USER_CALLER, "ByteDance")
    assert m_same is not None
    assert m_same.workspace_id == WS_1
    assert m_same.ticker == "DOC1"


@pytest.mark.asyncio
async def test_primary_pre_send_deadline_tries_fallback_when_total_budget_remains(
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

    monkeypatch.setattr(
        dedup_mod,
        "_resolve_dedup_models",
        create_autospec(
            dedup_mod._resolve_dedup_models,
            return_value=["primary-oauth", "fallback-flash"],
        ),
    )

    seen_models: list[str] = []

    def _deadline_primary_then_ok(**kwargs: Any) -> DaltonCompleteResult:
        model = str(kwargs["request_model"])
        seen_models.append(model)
        if model == "primary-oauth":
            return DaltonCompleteResult(
                ok=False,
                status="failed",
                error_kind="deadline",
                model=model,
            )
        return _ok_result([{"id": "c1", "confidence": "high", "reason": "same"}])

    _install_llm(monkeypatch, _deadline_primary_then_ok)

    match = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert match is not None
    assert match.workspace_id == WS_2
    assert seen_models == ["primary-oauth", "fallback-flash"]

    # When total deadline is already exhausted before/at primary deadline return, fallback is NOT tried.
    seen_models.clear()
    clock = {"now": 100.0}
    monkeypatch.setattr(
        dedup_mod.time,
        "monotonic",
        create_autospec(dedup_mod.time.monotonic, side_effect=lambda: clock["now"]),
    )

    def _deadline_primary_and_expire(**kwargs: Any) -> DaltonCompleteResult:
        model = str(kwargs["request_model"])
        seen_models.append(model)
        clock["now"] = 116.0
        return DaltonCompleteResult(
            ok=False,
            status="failed",
            error_kind="deadline",
            model=model,
        )

    _install_llm(monkeypatch, _deadline_primary_and_expire)
    res_exhausted = await find_existing_company_workspace(USER_CALLER, "Anthropic")
    assert res_exhausted is None
    assert seen_models == ["primary-oauth"]


@pytest.mark.asyncio
async def test_dedup_llm_suggestion_prefill_and_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="NVDA Research",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})

    # 1. High confidence company suggestion -> included in DuplicateMatch and REST 409 detail.
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[DedupMatchItem(id="c1", confidence="high", reason="same")],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="nvda",
                suggested_display_name="NVIDIA Corporation",
            )
        ),
    )
    dup = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup is not None
    assert dup.suggestion_payload() == {
        "coverage_kind": "company",
        "ticker": "NVDA",
        "display_name": "NVIDIA Corporation",
    }

    app = create_test_app(router)
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=dup)
    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.platform_client.platform_gating_active",
            return_value=False,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workspaces", json={"name": "Nvidia"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["suggestion"] == {
        "coverage_kind": "company",
        "ticker": "NVDA",
        "display_name": "NVIDIA Corporation",
    }

    # 2. Medium/low confidence -> suggestion omitted.
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[DedupMatchItem(id="c1", confidence="high", reason="same")],
                suggested_kind="company",
                suggested_confidence="medium",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA",
            )
        ),
    )
    dup_low = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup_low is not None
    assert dup_low.suggestion_payload() is None

    # 3. Sector suggestion -> strips any ticker.
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[DedupMatchItem(id="c1", confidence="medium", reason="same")],
                suggested_kind="sector",
                suggested_confidence="high",
                suggested_ticker="SOXX",
                suggested_display_name="Semiconductors",
            )
        ),
    )
    dup_sector = await find_existing_company_workspace(USER_CALLER, "Semiconductors")
    assert dup_sector is not None
    assert dup_sector.suggestion_payload() == {
        "coverage_kind": "sector",
        "display_name": "Semiconductors",
    }


@pytest.mark.asyncio
async def test_rest_create_workspace_auto_registers_unit_and_skips_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    import src.dalton.database.coverage_units as cu_db
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )

    inserted_calls: list[dict[str, Any]] = []

    async def _fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted_calls.append(kwargs)
        return {
            "unit_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "kind": kwargs["kind"],
            "owner_user_id": kwargs["owner_user_id"],
            "lead_analyst_user_id": kwargs["lead_analyst_user_id"],
            "slug": kwargs["slug"],
            "display_name": kwargs["display_name"],
            "ticker": kwargs["ticker"],
            "ticker_key": kwargs["ticker_key"],
            "workspace_id": kwargs.get("workspace_id"),
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }

    class _TxCm:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *args: Any) -> None:
            return None

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.transaction.return_value = _TxCm()

    @asynccontextmanager
    async def _fake_get_conn(_c: Any = None):
        yield fake_conn

    monkeypatch.setattr(cu_db, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(
        cu_db,
        "insert_coverage_unit",
        create_autospec(cu_db.insert_coverage_unit, side_effect=_fake_insert),
    )
    monkeypatch.setattr(
        cu_db,
        "_can_read_workspace",
        create_autospec(cu_db._can_read_workspace, return_value=True),
    )
    monkeypatch.setattr(
        cu_svc,
        "_acquire_sorted_personal_coverage_locks",
        create_autospec(cu_svc._acquire_sorted_personal_coverage_locks, return_value=[]),
    )
    monkeypatch.setattr(
        cu_svc,
        "_sync_lead_personal_coverage",
        create_autospec(cu_svc._sync_lead_personal_coverage, return_value=None),
    )

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "Nvidia",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)

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
            # 1. Company + ticker -> unit created, creator as lead analyst, bound to WS_5.
            resp_company = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "Nvidia",
                    "coverage_kind": "company",
                    "ticker": "nvda",
                    "display_name": "NVIDIA Corporation",
                },
            )
            assert resp_company.status_code == 201
            body = resp_company.json()
            assert body["coverage_unit_warning"] is None
            assert body["coverage_unit"]["kind"] == "company"
            assert body["coverage_unit"]["ticker"] == "NVDA"
            assert body["coverage_unit"]["workspace_id"] == WS_5
            assert body["coverage_unit"]["lead_analyst_user_id"] == "test-user-123"
            assert len(inserted_calls) == 1
            assert inserted_calls[0]["slug"] == "nvda"

            # 2. coverage_kind="none" -> does NOT register any coverage unit.
            inserted_calls.clear()
            resp_none = await client.post(
                "/api/v1/workspaces",
                json={"name": "Random Notes", "coverage_kind": "none"},
            )
            assert resp_none.status_code == 201
            body_none = resp_none.json()
            assert body_none["coverage_unit"] is None
            assert body_none["coverage_unit_warning"] is None
            assert inserted_calls == []


@pytest.mark.asyncio
async def test_cos_manage_workspaces_create_auto_registers_unit_and_prompt_mentions_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    import src.dalton.database.coverage_units as cu_db
    import src.tools.secretary.tools as sec_tools

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )

    prompt = get_loader().get_system_prompt(
        current_time="2026-11-21 12:00 ET",
        subagent_summary="",
        role="chief_of_staff",
    )
    assert "自动登记覆盖单元" in prompt
    assert "coverage_kind" in prompt

    class _TxCm:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *args: Any) -> None:
            return None

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.transaction.return_value = _TxCm()

    @asynccontextmanager
    async def _fake_get_conn(_c: Any = None):
        yield fake_conn

    async def _fake_insert(**kwargs: Any) -> dict[str, Any]:
        return {
            "unit_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "kind": kwargs["kind"],
            "owner_user_id": kwargs["owner_user_id"],
            "lead_analyst_user_id": kwargs["lead_analyst_user_id"],
            "slug": kwargs["slug"],
            "display_name": kwargs["display_name"],
            "ticker": kwargs["ticker"],
            "workspace_id": kwargs.get("workspace_id"),
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }

    monkeypatch.setattr(cu_db, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(
        cu_db,
        "insert_coverage_unit",
        create_autospec(cu_db.insert_coverage_unit, side_effect=_fake_insert),
    )
    monkeypatch.setattr(
        cu_db,
        "_can_read_workspace",
        create_autospec(cu_db._can_read_workspace, return_value=True),
    )
    monkeypatch.setattr(
        cu_svc,
        "_acquire_sorted_personal_coverage_locks",
        create_autospec(cu_svc._acquire_sorted_personal_coverage_locks, return_value=[]),
    )
    monkeypatch.setattr(
        cu_svc,
        "_sync_lead_personal_coverage",
        create_autospec(cu_svc._sync_lead_personal_coverage, return_value=None),
    )
    monkeypatch.setattr(
        sec_tools,
        "duplicate_workspace",
        create_autospec(sec_tools.duplicate_workspace, return_value=(None, True)),
    )
    monkeypatch.setattr(
        sec_tools,
        "hitl_confirm",
        create_autospec(hitl_confirm, return_value=(True, "ok")),
    )

    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {"workspace_id": WS_5, "name": "AI Infrastructure"}
    mgr.resolve_binding.return_value = None
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
    ):
        res = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {
                    "action": "create",
                    "name": "AI Infrastructure",
                    "coverage_kind": "sector",
                    "display_name": "AI Infrastructure",
                    "state": {},
                },
                "id": "call-cos-sector",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
    payload = json.loads(res.update["messages"][0].content)
    assert payload["success"] is True
    assert payload["workspace_id"] == WS_5
    assert payload["coverage_unit"]["kind"] == "sector"
    assert payload["coverage_unit"]["slug"] == "ai-infrastructure"
    assert payload["coverage_unit"]["workspace_id"] == WS_5
    assert payload["coverage_unit"]["lead_analyst_user_id"] == USER_CALLER


@pytest.mark.asyncio
async def test_auto_register_ticker_conflict_keeps_workspace_and_returns_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    from src.dalton.database.coverage_units import CoverageUnitConflictError
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    monkeypatch.setattr(
        cu_svc,
        "register_coverage_unit",
        create_autospec(
            cu_svc.register_coverage_unit,
            side_effect=CoverageUnitConflictError(
                "coverage unit ticker already registered for an active company",
                conflict={"unit_id": "existing-u", "lead_analyst_user_id": USER_RICHARD},
            ),
        ),
    )

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "NVDA-2",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)

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
            resp = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "NVDA-2",
                    "allow_duplicate_company": True,
                    "coverage_kind": "company",
                    "ticker": "NVDA",
                },
            )
    assert resp.status_code == 201
    body = resp.json()
    assert body["workspace_id"] == WS_5
    assert body["coverage_unit"] is None
    assert "NVDA" in (body["coverage_unit_warning"] or "")
    assert "已被" in (body["coverage_unit_warning"] or "")


@pytest.mark.asyncio
async def test_coverage_fields_ignored_when_disabled_or_external_or_company_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R10: coverage_kind/ticker/display_name are ignored (no registration, no warning) when disabled, external, or company tier; 'other' never 422s."""
    from starlette.requests import Request as StarletteRequest
    import src.dalton.access_tiers as access_tiers_mod
    from src.dalton.access_tiers import COMPANY_SYSTEM_USER_ID
    import src.dalton.coverage.units as cu_svc
    import src.tools.secretary.tools as sec_tools
    from src.server.app.workspaces import create_workspace as create_workspace_endpoint, router
    from src.server.dependencies.principal import Principal
    from src.server.models.workspace import WorkspaceCreate

    real_is_internal_or_admin = access_tiers_mod.is_internal_or_admin

    reg_spy = create_autospec(cu_svc.register_coverage_unit)
    monkeypatch.setattr(cu_svc, "register_coverage_unit", reg_spy)
    auto_reg_spy = create_autospec(
        cu_svc.auto_register_workspace_coverage_unit,
        wraps=cu_svc.auto_register_workspace_coverage_unit,
    )
    monkeypatch.setattr(cu_svc, "auto_register_workspace_coverage_unit", auto_reg_spy)

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "Nvidia",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    mgr.resolve_binding.return_value = None
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)
    monkeypatch.setattr(
        sec_tools,
        "duplicate_workspace",
        create_autospec(sec_tools.duplicate_workspace, return_value=(None, True)),
    )
    monkeypatch.setattr(
        sec_tools,
        "hitl_confirm",
        create_autospec(hitl_confirm, return_value=(True, "ok")),
    )

    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
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
            # 1. External member -> backend ignores coverage fields (no registration, no warning).
            _enable_team_mode(monkeypatch, caller_tier="external")
            is_internal_spy = create_autospec(
                access_tiers_mod.is_internal_or_admin, return_value=False
            )
            monkeypatch.setattr(
                access_tiers_mod,
                "is_internal_or_admin",
                is_internal_spy,
            )
            resp_ext = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "Nvidia",
                    "coverage_kind": "company",
                    "ticker": "NVDA",
                    "display_name": "NVIDIA Corporation",
                },
            )
            assert resp_ext.status_code == 201
            body_ext = resp_ext.json()
            assert body_ext["workspace_id"] == WS_5
            assert body_ext["coverage_unit"] is None
            assert body_ext["coverage_unit_warning"] is None
            assert body_ext["coverage_unit_warning_detail"] is None

            cos_ext = await chief_of_staff_workspaces.ainvoke(
                {
                    "name": "manage_workspaces",
                    "args": {
                        "action": "create",
                        "name": "Nvidia",
                        "coverage_kind": "company",
                        "ticker": "NVDA",
                        "state": {},
                    },
                    "id": "call-ext-cos",
                    "type": "tool_call",
                },
                config={"configurable": {"user_id": USER_EXTERNAL}},
            )
            cos_ext_payload = json.loads(cos_ext.update["messages"][0].content)
            assert cos_ext_payload["success"] is True
            assert "coverage_unit" not in cos_ext_payload
            assert "coverage_unit_warning" not in cos_ext_payload
            assert "coverage_unit_warning_detail" not in cos_ext_payload

            # 2. Company tier -> use real is_internal_or_admin + COMPANY_SYSTEM_USER_ID (no stub).
            # HTTP rejects COMPANY_SYSTEM_USER_ID via reject_company_system_auth (401), so internal
            # create_workspace handler & Chief of Staff tool paths pass COMPANY_SYSTEM_USER_ID directly.
            _enable_team_mode(monkeypatch, caller_tier="company")
            company_internal_spy = create_autospec(
                real_is_internal_or_admin, wraps=real_is_internal_or_admin
            )
            monkeypatch.setattr(
                access_tiers_mod, "is_internal_or_admin", company_internal_spy
            )
            dummy_req = StarletteRequest(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/workspaces",
                    "headers": [],
                }
            )
            ct_ws_resp = await create_workspace_endpoint(
                WorkspaceCreate(
                    name="Nvidia",
                    coverage_kind="sector",
                    display_name="Semiconductors",
                ),
                Principal(user_id=COMPANY_SYSTEM_USER_ID, auth="service"),
                dummy_req,
            )
            assert ct_ws_resp.coverage_unit is None
            assert ct_ws_resp.coverage_unit_warning is None
            assert ct_ws_resp.coverage_unit_warning_detail is None

            cos_ct = await chief_of_staff_workspaces.ainvoke(
                {
                    "name": "manage_workspaces",
                    "args": {
                        "action": "create",
                        "name": "Nvidia",
                        "coverage_kind": "sector",
                        "display_name": "Semiconductors",
                        "state": {},
                    },
                    "id": "call-ct-cos",
                    "type": "tool_call",
                },
                config={"configurable": {"user_id": COMPANY_SYSTEM_USER_ID}},
            )
            cos_ct_payload = json.loads(cos_ct.update["messages"][0].content)
            assert cos_ct_payload["success"] is True
            assert "coverage_unit" not in cos_ct_payload
            assert "coverage_unit_warning" not in cos_ct_payload
            assert [call.args[0] for call in company_internal_spy.await_args_list] == [
                COMPANY_SYSTEM_USER_ID,
                COMPANY_SYSTEM_USER_ID,
            ]

            # 3. coverage_units disabled -> even internal member's coverage fields are ignored.
            _enable_team_mode(monkeypatch, coverage_enabled=False, caller_tier="internal")
            monkeypatch.setattr(
                access_tiers_mod,
                "is_internal_or_admin",
                create_autospec(real_is_internal_or_admin, return_value=True),
            )
            resp_disabled = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "Nvidia",
                    "coverage_kind": "company",
                    "ticker": "NVDA",
                },
            )
            assert resp_disabled.status_code == 201
            body_dis = resp_disabled.json()
            assert body_dis["coverage_unit"] is None
            assert body_dis["coverage_unit_warning"] is None
            assert body_dis["coverage_unit_warning_detail"] is None

            cos_dis = await chief_of_staff_workspaces.ainvoke(
                {
                    "name": "manage_workspaces",
                    "args": {
                        "action": "create",
                        "name": "Nvidia",
                        "coverage_kind": "company",
                        "ticker": "NVDA",
                        "state": {},
                    },
                    "id": "call-dis-cos",
                    "type": "tool_call",
                },
                config={"configurable": {"user_id": USER_CALLER}},
            )
            cos_dis_payload = json.loads(cos_dis.update["messages"][0].content)
            assert cos_dis_payload["success"] is True
            assert "coverage_unit" not in cos_dis_payload
            assert "coverage_unit_warning" not in cos_dis_payload

            # 4. coverage_kind="other" (enabled internal) never returns 422.
            _enable_team_mode(monkeypatch, coverage_enabled=True, caller_tier="internal")
            resp_other = await client.post(
                "/api/v1/workspaces",
                json={"name": "Misc Research", "coverage_kind": "other"},
            )
            assert resp_other.status_code == 201
            assert resp_other.json()["coverage_unit"] is None
            assert resp_other.json()["coverage_unit_warning"] is None

    auto_reg_spy.assert_not_awaited()
    reg_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Round 9 tests (requirements 1-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_suggestion_withheld_when_any_hit_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #1: If any matched workspace/unit is unreadable, suggestion MUST be None."""
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="NVDA Readable",
                owner_id=USER_RICHARD,
                owner_name="Richard",
                owner_email=EMAIL_RICHARD,
            ),
            _ws_row(
                ws_id=WS_2,
                name="NVDA Private",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            ),
        ],
    )
    # Only WS_1 is readable; WS_2 is unreadable.
    _install_read_access(monkeypatch, web_readable={WS_1})
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[
                    DedupMatchItem(id="c1", confidence="high", reason="same"),
                    DedupMatchItem(id="c2", confidence="high", reason="same"),
                ],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA Corporation",
            )
        ),
    )

    dup = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup is not None
    assert len(dup.matches) == 2
    by_name = {m.workspace_name: m for m in dup.matches}
    assert by_name["NVDA Readable"].can_read is True
    assert by_name["NVDA Private"].can_read is False
    assert dup.suggestion is None
    assert dup.suggestion_payload() is None

    app = create_test_app(router)
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=dup)
    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.platform_client.platform_gating_active",
            return_value=False,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workspaces", json={"name": "Nvidia"})
    assert resp.status_code == 409
    assert "suggestion" not in resp.json()["detail"]

    # When all hits are readable, suggestion IS returned.
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2})
    dup_readable = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup_readable is not None
    assert dup_readable.suggestion_payload() == {
        "coverage_kind": "company",
        "ticker": "NVDA",
        "display_name": "NVIDIA Corporation",
    }

    # Even if >3 hits occur and the 4th (truncated) hit is unreadable, suggestion is withheld.
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(ws_id=WS_1, name="NVDA 1", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW),
            _ws_row(ws_id=WS_2, name="NVDA 2", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(hours=1)),
            _ws_row(ws_id=WS_3, name="NVDA 3", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(hours=2)),
            _ws_row(ws_id=WS_4, name="NVDA 4 Private", owner_id=USER_CY, owner_name="CY", owner_email=EMAIL_CY, last_activity_at=NOW - timedelta(hours=3)),
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3})
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[
                    DedupMatchItem(id="c1", confidence="high", reason="same"),
                    DedupMatchItem(id="c2", confidence="high", reason="same"),
                    DedupMatchItem(id="c3", confidence="high", reason="same"),
                    DedupMatchItem(id="c4", confidence="high", reason="same"),
                ],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA Corporation",
            )
        ),
    )
    # Bypass per-batch cap in _parse_llm_payload by testing with >3 live hits across 2 batches:
    # simulate 4 hits via _judge_candidates_with_llm returning 4 entries.
    with patch.object(
        dedup_mod,
        "_judge_candidates_with_llm",
        create_autospec(
            dedup_mod._judge_candidates_with_llm,
            side_effect=lambda **kw: (
                kw["suggestion_out"].append(
                    dedup_mod.DedupCoverageSuggestion(
                        coverage_kind="company",
                        ticker="NVDA",
                        display_name="NVIDIA Corporation",
                    )
                ),
                [(e, "high", "same") for e in kw["entries"]],
            )[1],
        ),
    ):
        dup_four = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup_four is not None
    assert len(dup_four.matches) == 3
    assert dup_four.suggestion is None


@pytest.mark.asyncio
async def test_workspace_write_only_pat_cannot_auto_register_coverage_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #2: A PAT with only workspace:write is refused by POST /coverage/units and skips auto-register."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    from src.dalton.app import dalton_router
    from src.server.app.workspaces import router as ws_router
    from src.server.auth import pats
    from src.server.dependencies.principal import Principal, get_principal

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    reg_spy = create_autospec(cu_svc.register_coverage_unit)
    monkeypatch.setattr(cu_svc, "register_coverage_unit", reg_spy)

    # 1. Same workspace:write PAT is rejected by POST /api/v1/dalton/coverage/units (real auth).
    import src.dalton.auth as dalton_auth_mod

    monkeypatch.setattr(
        dalton_auth_mod,
        "_ensure_dalton_auth_ready",
        create_autospec(dalton_auth_mod._ensure_dalton_auth_ready, return_value=None),
    )
    monkeypatch.setattr(
        pats,
        "is_pat",
        create_autospec(pats.is_pat, side_effect=lambda tok: tok.startswith("la_pat_")),
    )
    monkeypatch.setattr(
        pats,
        "verify_pat",
        create_autospec(
            pats.verify_pat,
            return_value=pats.PatPrincipal(USER_CALLER, ("workspace:write",)),
        ),
    )
    monkeypatch.setattr(
        "src.server.dependencies.principal.admit_caller",
        create_autospec(
            __import__("src.server.utils.api", fromlist=["admit_caller"]).admit_caller,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        "src.server.dependencies.principal.note_caller",
        create_autospec(
            __import__("src.server.utils.api", fromlist=["note_caller"]).note_caller,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        "src.server.dependencies.principal.refuse_holder_credential",
        create_autospec(
            __import__("src.server.utils.api", fromlist=["refuse_holder_credential"]).refuse_holder_credential,
            return_value=None,
        ),
    )

    real_dalton_app = create_test_app(dalton_router)
    real_dalton_app.dependency_overrides.clear()
    async with AsyncClient(
        transport=ASGITransport(app=real_dalton_app), base_url="http://test"
    ) as client:
        cu_resp = await client.post(
            "/api/v1/dalton/coverage/units",
            headers={"Authorization": "Bearer la_pat_only_ws_write"},
            json={"kind": "company", "slug": "nvda", "ticker": "NVDA", "display_name": "NVIDIA"},
        )
    assert cu_resp.status_code in (401, 403)
    reg_spy.assert_not_awaited()

    # 2. Same workspace:write PAT creates workspace (201) via POST /api/v1/workspaces,
    #    does NOT register coverage unit, and returns structured token_not_allowed warning.
    app = create_test_app(ws_router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_CALLER, auth="pat", scopes=("workspace:write",)
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": USER_CALLER,
        "name": "Nvidia",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)
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
            ws_resp = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "Nvidia",
                    "coverage_kind": "company",
                    "ticker": "NVDA",
                },
            )
    assert ws_resp.status_code == 201
    body = ws_resp.json()
    assert body["workspace_id"] == WS_5
    assert body["coverage_unit"] is None
    assert body["coverage_unit_warning_detail"] == {
        "code": "token_not_allowed",
        "params": {},
    }
    assert "令牌" in (body["coverage_unit_warning"] or "")
    reg_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_ignores_holder_and_external_custom_models_with_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #3: Dedup passes ignore_custom_models=True and resolves built-in provider even if holder shadows model name."""
    from ptc_agent.config.agent import CredentialSource, LLMConfig
    from src.llms.llm import LLM as LLMFactory
    from src.server.services.llm import clients as llm_clients
    from src.server.services.llm import config as llm_config
    from src.server.services.llm import user_models as user_models_mod
    from src.server.services.llm.service import LLMService
    from src.server.services.policy import Policy

    # 1. Verify dedup passes ignore_custom_models=True to dalton_complete.
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_2,
                name="NVDA Research",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_2})
    captured_calls: list[dict[str, Any]] = []
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(DedupLlmResult(matches=[])),
        captured_calls=captured_calls,
    )
    await find_existing_company_workspace(USER_CALLER, "SomeRandomTopic")
    assert len(captured_calls) >= 1
    assert captured_calls[0].get("ignore_custom_models") is True

    # 2. Verify client resolution with holder having a same-named custom_models entry
    #    pointing to a custom provider uses built-in catalog when ignore_custom_models=True,
    #    while default (ignore_custom_models=False) still uses the custom entry.
    mc = LLMFactory.get_model_config()
    builtin_model = next(
        name
        for name in ("gpt-6.1-terra", "gpt-6.2-astra", "claude-sonnet-4-6")
        if mc.get_model_config(name)
    )
    builtin_provider = mc.get_model_config(builtin_model)["provider"]
    holder_pref = {
        "custom_models": [
            {
                "name": builtin_model,
                "model_id": "hijacked-model",
                "provider": "my-custom-gw",
            }
        ],
        "custom_providers": [
            {
                "name": "my-custom-gw",
                "parent_provider": "openai",
                "use_response_api": False,
            }
        ],
    }

    monkeypatch.setattr(
        user_models_mod,
        "get_model_preference",
        create_autospec(user_models_mod.get_model_preference, return_value=holder_pref),
    )
    monkeypatch.setattr(
        llm_clients,
        "require_policy",
        create_autospec(llm_clients.require_policy, return_value=Policy()),
    )
    monkeypatch.setattr(
        llm_config,
        "require_policy",
        create_autospec(llm_config.require_policy, return_value=Policy()),
    )
    monkeypatch.setattr(
        llm_config,
        "load_user_context",
        create_autospec(llm_config.load_user_context, return_value=None),
    )
    monkeypatch.setattr(
        llm_config,
        "gate_capabilities",
        create_autospec(llm_config.gate_capabilities, return_value=None),
    )
    monkeypatch.setattr(
        "src.server.services.platform_client.platform_gating_active",
        lambda: False,
    )

    seen_byok_providers: list[str] = []

    async def _fake_walk(user_id: str, provider: str, mc_arg: Any, **_kw: Any):
        seen_byok_providers.append(provider)
        return ({"api_key": "sk-test"}, provider)

    monkeypatch.setattr(
        llm_clients,
        "walk_byok_candidates",
        create_autospec(llm_clients.walk_byok_candidates, side_effect=_fake_walk),
    )
    monkeypatch.setattr(
        llm_clients,
        "stored_key",
        create_autospec(llm_clients.stored_key, return_value={"api_key": "sk-custom"}),
    )
    monkeypatch.setattr(
        llm_clients,
        "resolve_oauth_llm_client",
        create_autospec(llm_clients.resolve_oauth_llm_client, return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.api_keys.get_byok_configs_for_providers",
        create_autospec(
            __import__("src.server.database.api_keys", fromlist=["get_byok_configs_for_providers"]).get_byok_configs_for_providers,
            return_value={},
        ),
    )

    from langchain_core.language_models.chat_models import BaseChatModel

    fake_client = create_autospec(BaseChatModel, instance=True)
    fake_client.metadata = {}
    fake_client.model_copy.return_value = fake_client
    create_llm_spy = create_autospec(
        __import__("src.llms.llm", fromlist=["create_llm"]).create_llm,
        return_value=fake_client,
    )
    create_custom_spy = create_autospec(
        __import__("src.llms.llm", fromlist=["create_llm_from_custom"]).create_llm_from_custom,
        return_value=fake_client,
    )
    monkeypatch.setattr("src.llms.llm.create_llm", create_llm_spy)
    monkeypatch.setattr("src.llms.llm.create_llm_from_custom", create_custom_spy)

    # Direct resolve_model_client with ignore_custom_models=True -> built-in provider, not custom!
    rc_ignored = await llm_clients.resolve_model_client(
        "holder-uid",
        builtin_model,
        is_byok=True,
        ignore_custom_models=True,
    )
    assert rc_ignored.model_source == user_models_mod.ModelSource.SYSTEM
    assert rc_ignored.credential_source == CredentialSource.BYOK
    assert seen_byok_providers[-1] == builtin_provider
    create_custom_spy.assert_not_called()
    assert create_llm_spy.call_count == 1

    # Through LLMService.complete(..., ignore_custom_models=True) -> also uses built-in catalog!
    from ptc_agent.config import load_from_files

    agent_cfg = (await load_from_files()).model_copy(
        update={"llm": LLMConfig(name=builtin_model, background=builtin_model)}
    )
    svc = LLMService(agent_config=agent_cfg)
    monkeypatch.setattr(
        "src.server.services.llm.service.make_api_call",
        create_autospec(
            __import__("src.llms.api_call", fromlist=["make_api_call"]).make_api_call,
            return_value=("ok", {}),
        ),
    )
    await svc.complete(
        user_id="holder-uid",
        user_prompt="hi",
        request_model=builtin_model,
        is_byok=True,
        ignore_custom_models=True,
    )
    assert seen_byok_providers[-1] == builtin_provider
    create_custom_spy.assert_not_called()

    # Default (ignore_custom_models=False) still respects holder's custom model!
    rc_default = await llm_clients.resolve_model_client(
        "holder-uid",
        builtin_model,
        is_byok=True,
    )
    assert rc_default.model_source == user_models_mod.ModelSource.CUSTOM
    assert create_custom_spy.call_count == 1


@pytest.mark.asyncio
async def test_coverage_suggestion_confirmation_when_no_hits_web_and_cos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #4: No duplicate hits + high-confidence suggestion triggers confirmation when Type is Other."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    import src.tools.secretary.tools as sec_tools
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="Apple Research",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
            )
        ],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA Corporation",
            )
        ),
    )

    dup = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup is not None
    assert dup.matches == ()
    assert not dup.has_matches
    assert dup.suggestion_payload() == {
        "coverage_kind": "company",
        "ticker": "NVDA",
        "display_name": "NVIDIA Corporation",
    }

    # REST: coverage_kind omitted / "none" -> 409 coverage_suggestion without creating workspace.
    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "Nvidia",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    reg_mock = create_autospec(
        cu_svc.register_coverage_unit,
        return_value={
            "unit_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "kind": "company",
            "ticker": "NVDA",
            "workspace_id": WS_5,
            "lead_analyst_user_id": "test-user-123",
        },
    )
    monkeypatch.setattr(cu_svc, "register_coverage_unit", reg_mock)

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
            resp_suggest = await client.post("/api/v1/workspaces", json={"name": "Nvidia"})
            assert resp_suggest.status_code == 409
            assert resp_suggest.json()["detail"]["code"] == "coverage_suggestion"
            assert resp_suggest.json()["detail"]["suggestion"] == {
                "coverage_kind": "company",
                "ticker": "NVDA",
                "display_name": "NVIDIA Corporation",
            }
            mgr.create_workspace.assert_not_awaited()

            # Retry with skip_coverage_suggestion=True -> 201 without registering.
            resp_skip = await client.post(
                "/api/v1/workspaces",
                json={"name": "Nvidia", "skip_coverage_suggestion": True},
            )
            assert resp_skip.status_code == 201
            assert resp_skip.json()["coverage_unit"] is None
            reg_mock.assert_not_awaited()

            # Submitting directly with coverage_kind="company" -> 201 without prompting again.
            resp_direct = await client.post(
                "/api/v1/workspaces",
                json={"name": "Nvidia", "coverage_kind": "company", "ticker": "NVDA"},
            )
            assert resp_direct.status_code == 201
            assert resp_direct.json()["coverage_unit"]["ticker"] == "NVDA"
            reg_mock.assert_awaited_once()

    # CoS prompt mentions coverage_suggestion & skip_coverage_suggestion, and tool returns coverage_suggestion.
    prompt = get_loader().get_system_prompt(
        current_time="2026-11-21 12:00 ET",
        subagent_summary="",
        role="chief_of_staff",
    )
    assert "coverage_suggestion" in prompt
    assert "skip_coverage_suggestion" in prompt

    hitl_spy = create_autospec(hitl_confirm, return_value=(True, "ok"))
    monkeypatch.setattr(sec_tools, "hitl_confirm", hitl_spy)
    monkeypatch.setattr(
        sec_tools,
        "duplicate_workspace",
        create_autospec(sec_tools.duplicate_workspace, return_value=(None, True)),
    )
    mgr.resolve_binding.return_value = None
    reg_mock.reset_mock()
    with (
        patch("src.dalton.coverage.dedup.find_existing_company_workspace", dedup_mock),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=mgr),
        ),
    ):
        cos_suggest = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {"action": "create", "name": "Nvidia", "state": {}},
                "id": "call-cos-suggest",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
        cos_payload = json.loads(cos_suggest.update["messages"][0].content)
        assert cos_payload["success"] is False
        assert cos_payload["code"] == "coverage_suggestion"
        assert cos_payload["suggestion"]["ticker"] == "NVDA"
        hitl_spy.assert_not_called()

        cos_skip = await chief_of_staff_workspaces.ainvoke(
            {
                "name": "manage_workspaces",
                "args": {
                    "action": "create",
                    "name": "Nvidia",
                    "skip_coverage_suggestion": True,
                    "state": {},
                },
                "id": "call-cos-skip",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_CALLER}},
        )
        cos_skip_payload = json.loads(cos_skip.update["messages"][0].content)
        assert cos_skip_payload["success"] is True
        reg_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlong_ticker_or_display_name_creates_workspace_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #5 & #7: Overlong ticker/display_name does not 422; workspace creates (201) with structured warning."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    reg_spy = create_autospec(cu_svc.register_coverage_unit)
    monkeypatch.setattr(cu_svc, "register_coverage_unit", reg_spy)

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "LongCo",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)
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
            resp_ticker = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "LongCo",
                    "coverage_kind": "company",
                    "ticker": "A" * 33,
                },
            )
            assert resp_ticker.status_code == 201
            body_ticker = resp_ticker.json()
            assert body_ticker["coverage_unit"] is None
            assert body_ticker["coverage_unit_warning_detail"] == {
                "code": "ticker_too_long",
                "params": {"max_length": 32},
            }

            resp_name = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "LongCo",
                    "coverage_kind": "sector",
                    "display_name": "B" * 121,
                },
            )
            assert resp_name.status_code == 201
            body_name = resp_name.json()
            assert body_name["coverage_unit"] is None
            assert body_name["coverage_unit_warning_detail"] == {
                "code": "display_name_too_long",
                "params": {"max_length": 120},
            }
    reg_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_script_refreshes_drops_stale_and_caps_at_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #6: probe_workspace_dedup.py refreshes live hits, drops deleted/stale hits, and caps at 3."""
    from scripts.dalton.ops.probe_workspace_dedup import probe_names

    _enable_team_mode(monkeypatch)
    ws_deleted = "66666666-6666-4666-8666-666666666666"
    initial_ws = [
        _ws_row(ws_id=WS_1, name="Co 1", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW),
        _ws_row(ws_id=ws_deleted, name="Co Deleted", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(minutes=1)),
        _ws_row(ws_id=WS_2, name="Co 2", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(minutes=2)),
        _ws_row(ws_id=WS_3, name="Co 3", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(minutes=3)),
        _ws_row(ws_id=WS_4, name="Co 4", owner_id=USER_RICHARD, owner_name="R", owner_email=EMAIL_RICHARD, last_activity_at=NOW - timedelta(minutes=4)),
    ]
    # Second query (live refresh) sees ws_deleted as status='deleted'.
    refreshed_ws = [
        initial_ws[0],
        {**initial_ws[1], "status": "deleted"},
        initial_ws[2],
        initial_ws[3],
        initial_ws[4],
    ]
    _install_db(
        monkeypatch,
        ws_rows_sequence=[initial_ws, refreshed_ws],
    )
    _install_read_access(
        monkeypatch,
        web_readable={WS_1, WS_2, WS_3, WS_4},
    )

    class _DummyPool:
        closed = False

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.server.database.pool.get_or_create_pool",
        lambda: _DummyPool(),
    )
    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection",
        dedup_mod.get_db_connection,
    )

    async def _fake_judge(**kw: Any) -> list[tuple[Any, str, str]]:
        return [(e, "high", "same") for e in kw["entries"]]

    monkeypatch.setattr(
        dedup_mod,
        "_judge_candidates_with_llm",
        create_autospec(dedup_mod._judge_candidates_with_llm, side_effect=_fake_judge),
    )

    report = await probe_names(["Co"], user_id=USER_CALLER, channel="web")
    matches = report["results"][0]["matches"]
    assert len(matches) == 3
    returned_ids = [m["candidate"]["workspace_id"] for m in matches]
    assert ws_deleted not in returned_ids
    assert returned_ids == [WS_1, WS_2, WS_3]


@pytest.mark.asyncio
async def test_auto_register_warning_returns_structured_code_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #7: Auto-registration failure populates structured coverage_unit_warning_detail {code, params}."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    from src.dalton.database.coverage_units import CoverageUnitConflictError
    from src.server.app.workspaces import router

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    monkeypatch.setattr(
        cu_svc,
        "register_coverage_unit",
        create_autospec(
            cu_svc.register_coverage_unit,
            side_effect=CoverageUnitConflictError(
                "coverage unit ticker already registered for an active company",
                conflict={"unit_id": "existing-u", "lead_analyst_user_id": USER_RICHARD},
            ),
        ),
    )

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": "test-user-123",
        "name": "NVDA-2",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=None)
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
            resp = await client.post(
                "/api/v1/workspaces",
                json={
                    "name": "NVDA-2",
                    "allow_duplicate_company": True,
                    "coverage_kind": "company",
                    "ticker": "nvda",
                },
            )
    assert resp.status_code == 201
    body = resp.json()
    assert body["coverage_unit_warning_detail"] == {
        "code": "ticker_conflict",
        "params": {"ticker": "NVDA"},
    }


@pytest.mark.asyncio
async def test_scoped_tokens_skip_coverage_suggestion_409_and_keep_token_not_allowed_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11 #2: Scoped PAT and computer tokens skip 409 coverage_suggestion and still warn on explicit coverage fields."""
    import src.dalton.access_tiers as access_tiers_mod
    import src.dalton.coverage.units as cu_svc
    from src.server.app.workspaces import router
    from src.server.dependencies import cli_approval
    from src.server.dependencies.principal import Principal, get_principal

    _enable_team_mode(monkeypatch)
    monkeypatch.setattr(
        cli_approval,
        "workspace_create",
        create_autospec(cli_approval.workspace_create, return_value=None),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    reg_spy = create_autospec(cu_svc.register_coverage_unit)
    monkeypatch.setattr(cu_svc, "register_coverage_unit", reg_spy)

    app = create_test_app(router)
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_5,
        "user_id": USER_CALLER,
        "name": "Nvidia",
        "description": None,
        "sandbox_id": None,
        "computer_id": None,
        "status": "creating",
        "created_at": NOW,
        "updated_at": NOW,
        "config": {},
    }
    suggestion_only_dup = dedup_mod.DuplicateMatch(
        company="Nvidia",
        message="",
        matches=(),
        matched_workspace_ids=(),
        suggestion=dedup_mod.DedupCoverageSuggestion(
            coverage_kind="company",
            ticker="NVDA",
            display_name="NVIDIA Corporation",
        ),
    )
    dedup_mock = create_autospec(_REAL_FIND_DEDUP, return_value=suggestion_only_dup)

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
            for limited_principal in (
                Principal(user_id=USER_CALLER, auth="pat", scopes=("workspace:write",)),
                Principal(
                    user_id=USER_CALLER,
                    auth="computer",
                    scopes=("workspace:write",),
                    computer_id="comp-1",
                ),
            ):
                app.dependency_overrides[get_principal] = lambda p=limited_principal: p
                resp = await client.post("/api/v1/workspaces", json={"name": "Nvidia"})
                assert resp.status_code == 201
                body = resp.json()
                assert body["workspace_id"] == WS_5
                assert body["coverage_unit"] is None
                assert body["coverage_unit_warning"] is None

                resp_explicit = await client.post(
                    "/api/v1/workspaces",
                    json={
                        "name": "Nvidia",
                        "coverage_kind": "company",
                        "ticker": "NVDA",
                    },
                )
                assert resp_explicit.status_code == 201
                body_explicit = resp_explicit.json()
                assert body_explicit["coverage_unit"] is None
                assert body_explicit["coverage_unit_warning_detail"] == {
                    "code": "token_not_allowed",
                    "params": {},
                }

            # First-party session still receives 409 coverage_suggestion.
            app.dependency_overrides[get_principal] = lambda: Principal(
                user_id=USER_CALLER, auth="session"
            )
            resp_session = await client.post(
                "/api/v1/workspaces", json={"name": "Nvidia"}
            )
            assert resp_session.status_code == 409
            assert resp_session.json()["detail"]["code"] == "coverage_suggestion"
    reg_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_checks_all_batch_hits_for_read_access_before_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11 #3: All hits within a single batch (>=4) are checked for read access before truncating to 3."""
    _enable_team_mode(monkeypatch)
    _install_db(
        monkeypatch,
        ws_rows=[
            _ws_row(
                ws_id=WS_1,
                name="NVDA 1",
                owner_id=USER_RICHARD,
                owner_name="R",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW,
            ),
            _ws_row(
                ws_id=WS_2,
                name="NVDA 2",
                owner_id=USER_RICHARD,
                owner_name="R",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW - timedelta(hours=1),
            ),
            _ws_row(
                ws_id=WS_3,
                name="NVDA 3",
                owner_id=USER_RICHARD,
                owner_name="R",
                owner_email=EMAIL_RICHARD,
                last_activity_at=NOW - timedelta(hours=2),
            ),
            _ws_row(
                ws_id=WS_4,
                name="NVDA 4 Private",
                owner_id=USER_CY,
                owner_name="CY",
                owner_email=EMAIL_CY,
                last_activity_at=NOW - timedelta(hours=3),
            ),
        ],
    )
    _install_llm(
        monkeypatch,
        lambda **_kw: _ok_result(
            DedupLlmResult(
                matches=[
                    DedupMatchItem(id="c1", confidence="high", reason="same 1"),
                    DedupMatchItem(id="c2", confidence="high", reason="same 2"),
                    DedupMatchItem(id="c3", confidence="high", reason="same 3"),
                    DedupMatchItem(id="c4", confidence="high", reason="same 4"),
                ],
                suggested_kind="company",
                suggested_confidence="high",
                suggested_ticker="NVDA",
                suggested_display_name="NVIDIA Corporation",
            )
        ),
    )

    # Real _parse_llm_payload + _judge_candidates_with_llm (no stub) with 4th hit unreadable -> suggestion withheld.
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3})
    dup_fourth_unreadable = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup_fourth_unreadable is not None
    assert [m.workspace_id for m in dup_fourth_unreadable.matches] == [WS_1, WS_2, WS_3]
    assert dup_fourth_unreadable.suggestion is None

    # Same 4 hits, all readable -> still capped at 3 matches and suggestion is returned.
    _install_read_access(monkeypatch, web_readable={WS_1, WS_2, WS_3, WS_4})
    dup_all_readable = await find_existing_company_workspace(USER_CALLER, "Nvidia")
    assert dup_all_readable is not None
    assert [m.workspace_id for m in dup_all_readable.matches] == [WS_1, WS_2, WS_3]
    assert dup_all_readable.suggestion == dedup_mod.DedupCoverageSuggestion(
        coverage_kind="company",
        ticker="NVDA",
        display_name="NVIDIA Corporation",
    )


@pytest.mark.asyncio
async def test_dedup_journal_stores_usage_cost_status_without_reason_or_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11 #4: Dedup journal writes usage, cost, and status='done' with response=None (no reason/suggested_*), leaving other callers unchanged."""
    from decimal import Decimal
    import src.dalton.llm.complete as complete_mod
    from src.server.services.llm.service import LLMService

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
            )
        ],
    )
    _install_read_access(monkeypatch, web_readable={WS_1})
    monkeypatch.setattr(
        dedup_mod,
        "_resolve_dedup_models",
        create_autospec(
            dedup_mod._resolve_dedup_models,
            return_value=["gemini-4-hydrogen-low-oauth"],
        ),
    )
    monkeypatch.setattr(dedup_mod, "get_team_credential_holder_id", lambda: USER_RICHARD)
    monkeypatch.setattr(dedup_mod, "dalton_complete", complete_mod.dalton_complete)

    done_calls: list[dict[str, Any]] = []

    async def _fake_inspect(
        invocation_id: str,
        *,
        request_hash: str,
        stale_after_s: int = 900,
        resend_indeterminate: bool = False,
        resend_max_attempts: int | None = None,
        conn: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        return "proceed", None

    async def _fake_claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "claimed", {
            "invocation_id": kwargs["invocation_id"],
            "state": "pending",
            "attempt": 1,
        }

    async def _fake_done(
        invocation_id: str,
        *,
        attempt: int,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None = None,
        cash_usd: Decimal = Decimal("0"),
        notional_usd: Decimal = Decimal("0"),
        provider: str | None = None,
        billing_type: str | None = None,
        stamp_credential_holder: bool = True,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        done_calls.append(
            {
                "invocation_id": invocation_id,
                "attempt": attempt,
                "response": response,
                "usage": usage,
                "cash_usd": cash_usd,
                "notional_usd": notional_usd,
                "provider": provider,
                "billing_type": billing_type,
            }
        )
        return {"invocation_id": invocation_id, "state": "done", "response": response}

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, side_effect=_fake_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, side_effect=_fake_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, side_effect=_fake_done),
    )
    monkeypatch.setattr(
        complete_mod,
        "compute_call_costs",
        create_autospec(
            complete_mod.compute_call_costs,
            return_value=(Decimal("0"), Decimal("0.0012")),
        ),
    )

    llm_mock = create_autospec(LLMService, instance=True)
    llm_mock.complete.return_value = (
        DedupLlmResult(
            matches=[
                DedupMatchItem(
                    id="c1",
                    confidence="high",
                    reason="SECRET_REASON_MUST_NOT_BE_JOURNALED",
                )
            ],
            suggested_kind="company",
            suggested_confidence="high",
            suggested_ticker="NVDA",
            suggested_display_name="SECRET_SUGGESTED_NAME",
        ),
        {"input_tokens": 111, "output_tokens": 22, "total_tokens": 133},
    )
    with patch("src.server.services.llm.service.LLMService", return_value=llm_mock):
        dup = await find_existing_company_workspace(USER_CALLER, "Nvidia")

    assert dup is not None
    assert len(dup.matches) == 1
    assert dup.suggestion is not None
    assert len(done_calls) == 1
    assert done_calls[0]["response"] is None
    assert done_calls[0]["usage"]["input_tokens"] == 111
    assert done_calls[0]["usage"]["output_tokens"] == 22
    assert done_calls[0]["notional_usd"] == Decimal("0.0012")
    assert done_calls[0]["cash_usd"] == Decimal("0")

    # Other callers with persist_raw=False (and default persist_response=True) keep existing redaction behavior.
    done_calls.clear()
    llm_mock.complete.return_value = (
        json.dumps(
            {
                "category": "research",
                "topic_label": "NVDA",
                "cards": [
                    {
                        "type": "fact",
                        "claim": "NVDA grew",
                        "quote": "RAW_QUOTE_STRIPPED",
                    }
                ],
            }
        ),
        {"input_tokens": 50, "output_tokens": 10},
    )
    res_other = await complete_mod.dalton_complete(
        llm_service=llm_mock,
        user_id=USER_CALLER,
        system_prompt="sys",
        user_prompt="usr",
        scope_ref="other:caller:1",
        purpose="assist.conversation.extract_cards",
        request_model="gemini-4-hydrogen-low-oauth",
        persist_raw=False,
    )
    assert res_other.ok is True
    assert len(done_calls) == 1
    assert isinstance(done_calls[0]["response"], dict)
    assert done_calls[0]["response"]["data"]["cards"][0]["claim"] == "NVDA grew"
    assert "quote" not in done_calls[0]["response"]["data"]["cards"][0]
