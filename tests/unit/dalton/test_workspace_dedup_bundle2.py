"""Unit tests for Bundle 2: BD5, BD6, and F6 + BO3."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage.dedup import (
    DedupLlmResult,
    WEB_DEDUP_OVERALL_TIMEOUT_SECONDS,
    find_existing_company_workspace,
    recheck_company_workspace_before_create,
)
from src.ptc_agent.agent.prompts import get_loader
from src.server.database import computer as computer_db
from src.server.database import workspace as ws_db
from src.tools.secretary.approvals import build_create_workspace_snapshot

USER_ID = "user-bundle2-1"
COLLEAGUE_ID = "user-colleague-2"


@pytest.mark.asyncio
async def test_bd6_proposal_phase_runs_deterministic_ticker_match_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD6: proposal-phase dedup runs deterministic ticker-key matching before calling the LLM."""
    ws_colleague = "bbbbbbbb-bbbb-4bbb-8bbb-000000000700"
    ws_row = {
        "workspace_id": ws_colleague,
        "user_id": COLLEAGUE_ID,
        "name": "00700",
        "status": "stopped",
        "owner_name": "Bob",
        "owner_email": "bob@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                self._rows = []
            elif "FROM workspaces w" in text:
                self._rows = [dict(ws_row)]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kwargs: Any) -> _Cur:
            return _Cur()

    @asynccontextmanager
    async def _use_conn(c: Any = None):
        yield c or _Conn()

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod.sharing_mod,
        "require_workspace_access",
        create_autospec(dedup_mod.sharing_mod.require_workspace_access, return_value=None),
    )
    llm_spy = create_autospec(dedup_mod._REAL_DALTON_COMPLETE)
    monkeypatch.setattr(dedup_mod, "dalton_complete", llm_spy)

    dup = await find_existing_company_workspace(USER_ID, "700.HK", channel="cos")
    assert dup is not None
    assert ws_colleague in dup.matched_workspace_ids
    assert dup.matches[0].workspace_name == "00700"
    llm_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_bd5_recheck_recollects_candidates_when_coverage_unit_archived_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD5: when a bound coverage unit is archived during lock-free recheck, recheck
    re-collects fresh candidates so checked_candidates carries unit_ticker=None and
    does not fail Phase 2 or falsely report duplicate."""
    unit_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000000505"
    ws_notes = "bbbbbbbb-bbbb-4bbb-8bbb-000000000505"
    unit_state = {"status": "active"}

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_id,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": USER_ID,
            "lead_analyst_user_id": USER_ID,
            "display_name": "腾讯控股",
            "workspace_id": ws_notes,
            "ticker": "00700",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": "宏观随笔",
            "workspace_owner_id": USER_ID,
            "workspace_status": "stopped",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_owner_email": "alice@team.com",
            "lead_name": "Alice",
            "lead_email": "alice@team.com",
        }

    live_ws_row = {
        "workspace_id": ws_notes,
        "user_id": USER_ID,
        "name": "宏观随笔",
        "status": "stopped",
        "owner_name": "Alice",
        "owner_email": "alice@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    collect_calls = {"n": 0}

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    self._rows = [row] if unit_id in set(params[0]) else []
                else:
                    collect_calls["n"] += 1
                    self._rows = [row] if row["status"] == "active" else []
            elif "FROM workspaces w" in text:
                self._rows = [dict(live_ws_row)]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kwargs: Any) -> _Cur:
            return _Cur()

    @asynccontextmanager
    async def _use_conn(c: Any = None):
        yield c or _Conn()

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    # LLM returns empty on attempt 1 while unit archives -> deterministic fallback triggers refresh ->
    # unit_changed causes candidate re-collection so checked_candidates carries unit_ticker=None!
    async def _llm_empty_and_archive(**_kwargs: Any) -> Any:
        unit_state["status"] = "archived"
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(dedup_mod._REAL_DALTON_COMPLETE, side_effect=_llm_empty_and_archive),
    )

    snap = await recheck_company_workspace_before_create(USER_ID, "700.HK", channel="cos")
    assert snap.status == "clear"
    assert snap.matched_workspace_ids == ()
    assert collect_calls["n"] == 2
    assert snap.checked_candidates == ((ws_notes, "宏观随笔", None),)


@pytest.mark.asyncio
async def test_bd5_card_and_locked_recheck_consistent_for_own_ticker_and_unit_ticker_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD5: confirmation card lists own ticker-named ('00700') and own company-unit-bound
    ('腾讯' -> unit_ticker='00700') workspace consistently with locked recheck."""
    ws_unit_id = "cccccccc-cccc-4ccc-8ccc-000000000506"
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(
            computer_db.get_primary_computer,
            return_value={
                "computer_id": "11111111-1111-4111-8111-111111111111",
                "user_id": USER_ID,
                "name": "Cloud",
                "kind": "docker",
                "status": "running",
                "config": {},
                "provider_ref": "ctr-1",
                "is_primary": True,
            },
        ),
    )
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(
            ws_db.get_live_workspace_names,
            return_value=[{"workspace_id": ws_unit_id, "name": "腾讯", "unit_ticker": "00700"}],
        ),
    )

    snap = await build_create_workspace_snapshot(
        USER_ID,
        {
            "id": "call-bd5-unit-ticker",
            "name": "manage_workspaces",
            "args": {"action": "create", "name": "700.HK"},
        },
    )
    assert snap is not None
    assert snap["duplicate_workspace_id"] == ws_unit_id
    assert snap["duplicate_workspace_name"] == "腾讯"

    class _LockedCur:
        async def execute(self, _sql: str, _params: Any = None) -> None:
            return None

        async def fetchall(self) -> list[dict[str, Any]]:
            return [{"workspace_id": ws_unit_id, "name": "腾讯", "unit_ticker": "00700"}]

    # Locked recheck accepts when card disclosed ws_unit_id and rejects when undisclosed.
    assert (
        await ws_db.check_duplicate_snapshot_unchanged(
            _LockedCur(),
            USER_ID,
            "700.HK",
            expected_duplicate_workspace_id=ws_unit_id,
            expected_duplicate_workspace_name="腾讯",
        )
        is True
    )
    assert (
        await ws_db.check_duplicate_snapshot_unchanged(
            _LockedCur(),
            USER_ID,
            "700.HK",
            expected_duplicate_workspace_id=None,
        )
        is False
    )


def test_f6_bo3_chief_of_staff_prompt_includes_delete_switch_model_and_workspace_migration() -> None:
    """F6 + BO3: CoS prompt includes concise Chinese guidance for workspace deletion,
    switch_thread_model, and automation workspace_id migration in both lean and detailed modes,
    without internal codes."""
    loader = get_loader()
    for mode in ("lean", "detailed"):
        rendered = loader.render(
            "components/chief_of_staff.md.j2",
            working_directory="/home/Home",
            computer_root="/home",
            guidance=mode,
        )
        assert 'manage_workspaces(action="delete"' in rendered
        assert "switch_thread_model" in rendered
        assert "workspace_id" in rendered
        assert "迁移到目标工作区" in rendered
        assert "工作区标识" in rendered
        assert "切换当前对话模型工具" in rendered
        for internal_code in ("BO2", "BO3", "BD5", "BD6", "F6", "AD5"):
            assert internal_code not in rendered
    assert WEB_DEDUP_OVERALL_TIMEOUT_SECONDS == 20.0


@pytest.mark.asyncio
async def test_bd6_r2_proposal_and_recheck_use_same_strict_name_ticker_shape_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD6 r2: both proposal (_find_matches) and lock-free recheck use
    strict_name_ticker_shape=True so non-explicit mixed-case names like
    'aapl' vs 'Aapl' or 'Anthropic' vs 'Anthropic' never mismatch between
    proposal card disclosure and post-approval recheck."""
    ws_colleague = "bbbbbbbb-bbbb-4bbb-8bbb-00000000aaaa"
    ws_row = {
        "workspace_id": ws_colleague,
        "user_id": COLLEAGUE_ID,
        "name": "Aapl",
        "status": "stopped",
        "owner_name": "Bob",
        "owner_email": "bob@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM workspaces w" in text:
                self._rows = [dict(ws_row)]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kwargs: Any) -> _Cur:
            return _Cur()

    @asynccontextmanager
    async def _use_conn(c: Any = None):
        yield c or _Conn()

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "_resolve_dedup_models",
        create_autospec(dedup_mod._resolve_dedup_models, return_value=["test-model"]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(
            dedup_mod._REAL_DALTON_COMPLETE,
            return_value=SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]), text=None),
        ),
    )

    proposal_dup = await find_existing_company_workspace(USER_ID, "aapl", channel="cos")
    recheck_snap = await recheck_company_workspace_before_create(USER_ID, "aapl", channel="cos")
    assert proposal_dup is None
    assert recheck_snap.status == "clear"


@pytest.mark.asyncio
async def test_bd5_r2_proposal_llm_excludes_disclosed_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD5 r2: proposal-phase dedup excludes exclude_workspace_id from BOTH
    deterministic matching AND LLM candidate judgment so the user's own
    already-disclosed 00700 workspace never blocks confirmation card emission."""
    ws_own = "bbbbbbbb-bbbb-4bbb-8bbb-000000000701"
    ws_other = "bbbbbbbb-bbbb-4bbb-8bbb-000000000702"
    ws_rows = [
        {
            "workspace_id": ws_own,
            "user_id": USER_ID,
            "name": "00700",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        },
        {
            "workspace_id": ws_other,
            "user_id": COLLEAGUE_ID,
            "name": "宏观笔记",
            "status": "stopped",
            "owner_name": "Bob",
            "owner_email": "bob@team.com",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        },
    ]

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM workspaces w" in text:
                self._rows = [dict(r) for r in ws_rows]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kwargs: Any) -> _Cur:
            return _Cur()

    @asynccontextmanager
    async def _use_conn(c: Any = None):
        yield c or _Conn()

    seen_prompts: list[str] = []

    async def _fake_complete(**kwargs: Any) -> Any:
        seen_prompts.append(str(kwargs.get("user_prompt") or ""))
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]), text=None)

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "_resolve_dedup_models",
        create_autospec(dedup_mod._resolve_dedup_models, return_value=["test-model"]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(dedup_mod._REAL_DALTON_COMPLETE, side_effect=_fake_complete),
    )

    dup = await find_existing_company_workspace(
        USER_ID,
        "700.HK",
        channel="cos",
        exclude_workspace_id=ws_own,
    )
    assert dup is None
    assert len(seen_prompts) == 1
    assert "00700" not in seen_prompts[0]
    assert "宏观笔记" in seen_prompts[0]


@pytest.mark.asyncio
async def test_bd5_r2_recheck_detects_unmatched_candidate_unit_archived_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD5 r2: when an unrelated candidate's coverage unit (not matched by LLM or
    deterministic code) archives during lock-free recheck, recheck detects the unit
    change across the full candidate set, re-collects, and emits fresh checked_candidates."""
    unit_baba = "aaaaaaaa-aaaa-4aaa-8aaa-00000000baba"
    ws_baba = "bbbbbbbb-bbbb-4bbb-8bbb-00000000baba"
    unit_state = {"status": "active"}

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_baba,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": COLLEAGUE_ID,
            "lead_analyst_user_id": COLLEAGUE_ID,
            "display_name": "阿里巴巴",
            "workspace_id": ws_baba,
            "ticker": "BABA",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": "阿里研究",
            "workspace_owner_id": COLLEAGUE_ID,
            "workspace_status": "stopped",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Bob",
            "owner_email": "bob@team.com",
            "unit_owner_email": "bob@team.com",
            "lead_name": "Bob",
            "lead_email": "bob@team.com",
        }

    ws_baba_row = {
        "workspace_id": ws_baba,
        "user_id": COLLEAGUE_ID,
        "name": "阿里研究",
        "status": "stopped",
        "owner_name": "Bob",
        "owner_email": "bob@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    collect_calls = {"n": 0}

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    self._rows = [row] if unit_baba in set(params[0]) else []
                else:
                    collect_calls["n"] += 1
                    self._rows = [row] if row["status"] == "active" else []
            elif "FROM workspaces w" in text:
                self._rows = [dict(ws_baba_row)]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kwargs: Any) -> _Cur:
            return _Cur()

    @asynccontextmanager
    async def _use_conn(c: Any = None):
        yield c or _Conn()

    llm_calls = {"n": 0}

    async def _fake_complete(**_kwargs: Any) -> Any:
        llm_calls["n"] += 1
        if llm_calls["n"] == 1:
            # Unrelated coverage unit BABA gets archived while LLM evaluates NVDA!
            unit_state["status"] = "archived"
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]), text=None)

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "_resolve_dedup_models",
        create_autospec(dedup_mod._resolve_dedup_models, return_value=["test-model"]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        create_autospec(dedup_mod._REAL_DALTON_COMPLETE, side_effect=_fake_complete),
    )

    snap = await recheck_company_workspace_before_create(USER_ID, "NVDA", channel="cos")
    assert snap.status == "clear"
    assert collect_calls["n"] == 2
    assert (ws_baba, "阿里研究", None) in snap.checked_candidates
