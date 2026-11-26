"""Reproducing & regression unit tests for Bundle 7 (r2):
1. Bound Home / stand-in / historical duplicate exclusion parity between Phase 1 and Phase 2;
   drift without conflicting workspace ID never fakes company_workspace_exists.
2. Alias concurrency with same ticker: company identity (coverage unit / config ticker)
   committed in the same transaction as workspace row so locked recheck catches it
   deterministically even when LLM returns empty matches.
3. Separation rule: upstream workspace.py has zero Dalton table SQL; upstream workspaces.py
   keeps only a thin DALTON-HOOK delegation.
4. Standalone Core (local auth) own-only locked recheck & explicit locked recheck on Phase 1
   snapshot collection failure.
5. Execution history link visibility matches real web read rules (threads_read.py + sharing.py):
   preserves initiator's readable thread link when colleague workspace is soft-deleted, strips
   inaccessible links, and strips colleague-initiated thread in own workspace for external callers.
6. Locked team recheck scopes SQL to internal members (no unbounded full-table scan).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

from httpx import ASGITransport, AsyncClient
import psycopg
import pytest

from src.dalton import access_tiers as access_tiers_mod
from src.dalton import sharing as sharing_mod
from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage import locked_recheck as locked_recheck_mod
from src.dalton.coverage import units as units_mod
from src.dalton.coverage.dedup import (
    DedupLlmResult,
    DedupMatchItem,
    recheck_company_workspace_before_create,
)
from src.dalton.database import coverage_units as cu_db
from src.server.database import automation_executions as exec_db
from src.server.database import workspace as ws_db
from src.server.database.home_workspace import (
    HOME_STAND_IN_KEY,
    get_home_stand_in_id,
    get_home_workspace_id,
)
from src.server.database.workspace_names import workspace_name_key
from src.server.dependencies import cli_approval as cli_approval_mod
from src.server.services.computer_errors import DuplicateWorkspaceConflictError
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app

USER_A = "test-user-123"
USER_B = "user-colleague-b"

_REAL_GET_INSTANCE = WorkspaceManager.get_instance
_REAL_IS_COV_ENABLED = dedup_mod.is_coverage_units_enabled
_REAL_IS_SUPA_AUTH = dedup_mod.is_dalton_supabase_auth_enabled
_REAL_IS_LOCAL_AUTH = dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled
_REAL_BUILD_DET_MATCH = dedup_mod.build_deterministic_duplicate_match
_REAL_IS_WS_COV_REG_ACTIVE = units_mod.is_workspace_coverage_registration_active
_REAL_IS_INTERNAL_OR_ADMIN = access_tiers_mod.is_internal_or_admin
COMP_A = "11111111-1111-4111-8111-111111111111"
WS_COLLEAGUE = "22222222-2222-4222-8222-222222222222"
WS_OWN_A = "33333333-3333-4333-8333-333333333333"
WS_DOC_ONLY = "44444444-4444-4444-8444-444444444444"
THREAD_COLLEAGUE = "55555555-5555-4555-8555-555555555555"
THREAD_OWN = "66666666-6666-4666-8666-666666666666"
AUTO_COLLEAGUE = "77777777-7777-4777-8777-777777777777"
AUTO_OWN = "88888888-8888-4888-8888-888888888888"
EXEC_COLLEAGUE = "99999999-9999-4999-8999-999999999999"
EXEC_OWN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


def _computer_row(computer_id: str = COMP_A, user_id: str = USER_A) -> dict[str, Any]:
    return {
        "computer_id": computer_id,
        "user_id": user_id,
        "name": "Cloud Primary",
        "kind": "docker",
        "is_primary": True,
        "status": "stopped",
        "provider_ref": "sandbox-ref-1",
        "resource_tier": "standard",
        "is_always_on": False,
        "platform_secret_version": 1,
        "root_dir": "/home/workspace",
        "config": {},
    }


def _make_in_memory_workspace_db(
    *,
    initial_workspaces: list[dict[str, Any]] | None = None,
    knowledge_docs: dict[str, dict[str, Any]] | None = None,
    coverage_units: dict[str, dict[str, Any]] | None = None,
    local_internal_users: list[str] | None = None,
):
    """In-memory transactional workspace DB with a real asyncio.Lock for lock_user_writes."""
    workspaces: list[dict[str, Any]] = [dict(w) for w in (initial_workspaces or [])]
    docs_by_ws: dict[str, dict[str, Any]] = dict(knowledge_docs or {})
    units_by_ws: dict[str, dict[str, Any]] = dict(coverage_units or {})
    user_locks: dict[str, asyncio.Lock] = {}
    lock_events: list[str] = []
    executed_sql: list[tuple[str, Any]] = []
    local_roles = list(local_internal_users or [])

    def _get_lock(uid: str) -> asyncio.Lock:
        return user_locks.setdefault(uid, asyncio.Lock())

    def _config_dict(w: dict[str, Any]) -> dict[str, Any]:
        cfg = w.get("config")
        return dict(cfg) if isinstance(cfg, dict) else {}

    def _is_stand_in(w: dict[str, Any]) -> bool:
        return bool(_config_dict(w).get(HOME_STAND_IN_KEY)) or bool(w.get("is_home_stand_in"))

    def _is_hist_dup(w: dict[str, Any]) -> bool:
        marker = _config_dict(w).get("dalton_historical_duplicate_of")
        return bool(isinstance(marker, str) and marker.strip()) or bool(
            w.get("is_historical_duplicate")
        )

    class _MemCursor:
        def __init__(self, conn: "_MemConn") -> None:
            self._conn = conn
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            executed_sql.append((text, params))

            if "FROM dalton_user_roles" in text:
                self._rows = [{"user_id": u} for u in local_roles]
                return

            if "UPDATE workspaces SET config = COALESCE(config, '{}'::jsonb) ||" in text:
                raw_json, wid = params
                payload = raw_json.obj if hasattr(raw_json, "obj") else dict(raw_json)
                for w in workspaces:
                    if str(w["workspace_id"]) == str(wid):
                        cfg = _config_dict(w)
                        cfg.update(payload)
                        w["config"] = cfg
                self._rows = []
                return

            if "FROM dalton_coverage_units u" in text:
                out_units: list[dict[str, Any]] = []
                for wid, unit in units_by_ws.items():
                    if str(unit.get("status") or "active") != "active":
                        continue
                    ws_match = next(
                        (w for w in workspaces if str(w["workspace_id"]) == str(wid)), None
                    )
                    if ws_match is None:
                        continue
                    out_units.append(
                        {
                            "unit_id": unit.get("unit_id", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                            "kind": unit.get("kind", "company"),
                            "status": "active",
                            "owner_user_id": unit.get("owner_user_id", ws_match["user_id"]),
                            "lead_analyst_user_id": unit.get(
                                "lead_analyst_user_id", ws_match["user_id"]
                            ),
                            "display_name": unit.get("display_name", ws_match["name"]),
                            "workspace_id": wid,
                            "ticker": unit.get("ticker"),
                            "workspace_name": ws_match["name"],
                            "workspace_owner_id": ws_match["user_id"],
                            "workspace_status": ws_match.get("status", "stopped"),
                            "is_home_stand_in": _is_stand_in(ws_match),
                            "is_historical_duplicate": _is_hist_dup(ws_match),
                            "owner_name": ws_match.get("owner_name", "Alice"),
                            "owner_email": ws_match.get("owner_email", "alice@team.com"),
                            "unit_owner_email": ws_match.get("owner_email", "alice@team.com"),
                            "lead_name": ws_match.get("owner_name", "Alice"),
                            "lead_email": ws_match.get("owner_email", "alice@team.com"),
                        }
                    )
                self._rows = out_units
                return

            if "FROM dalton_knowledge_versions" in text and "DISTINCT ON" in text:
                ws_ids = set(params[0] if params else [])
                self._rows = [
                    {"workspace_id": wid, "content": dict(doc)}
                    for wid, doc in docs_by_ws.items()
                    if wid in ws_ids
                ]
                return

            if "INSERT INTO workspaces" in text:
                assert isinstance(params, dict)
                wid = str(params["workspace_id"])
                uid = str(params["user_id"])
                name = str(params["name"])
                nkey = str(params["name_key"])
                raw_cfg = params.get("config")
                cfg_dict = (
                    dict(raw_cfg.obj)
                    if hasattr(raw_cfg, "obj") and isinstance(raw_cfg.obj, dict)
                    else (dict(raw_cfg) if isinstance(raw_cfg, dict) else {})
                )
                for existing in workspaces:
                    if (
                        existing["user_id"] == uid
                        and existing["status"] not in ("deleted", "unbound")
                        and not _is_stand_in(existing)
                        and str(existing["workspace_id"]).lower()
                        != get_home_workspace_id(uid).lower()
                        and workspace_name_key(existing["name"]) == nkey
                    ):
                        raise psycopg.errors.UniqueViolation("duplicate name_key")
                row = {
                    "workspace_id": wid,
                    "user_id": uid,
                    "name": name,
                    "description": params.get("description"),
                    "sandbox_id": "sandbox-ref-1",
                    "computer_id": str(params["computer_id"]),
                    "dir_name": str(params["dir_name"]),
                    "previous_dir_names": [],
                    "previous_root_dirs": [],
                    "status": "stopped",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "last_activity_at": NOW,
                    "stopped_at": NOW,
                    "config": cfg_dict,
                    "is_pinned": False,
                    "sort_order": 0,
                    "resource_tier": "standard",
                    "is_always_on": False,
                    "files_restore_incomplete": False,
                    "owner_name": "Alice" if uid == USER_A else "Bob",
                    "owner_email": "alice@team.com" if uid == USER_A else "bob@team.com",
                }
                workspaces.append(row)
                self._rows = [dict(row)]
                return

            if "SELECT dir_name FROM workspaces" in text:
                self._rows = [
                    {"dir_name": w.get("dir_name")}
                    for w in workspaces
                    if w.get("dir_name")
                ]
                return

            if "FROM workspaces w" in text:
                if (
                    params
                    and isinstance(params, tuple)
                    and len(params) == 1
                    and isinstance(params[0], list)
                ):
                    wanted = set(params[0])
                    pool = [w for w in workspaces if w["workspace_id"] in wanted]
                elif isinstance(params, dict) and "ws_ids" in params:
                    wanted = set(params["ws_ids"])
                    pool = [w for w in workspaces if w["workspace_id"] in wanted]
                elif (
                    params
                    and isinstance(params, tuple)
                    and len(params) == 1
                    and isinstance(params[0], str)
                ):
                    uid_filter = params[0]
                    pool = [w for w in workspaces if w["user_id"] == uid_filter]
                else:
                    pool = list(workspaces)

                # Apply SQL WHERE predicates faithfully so tests exercise real filter parity
                out: list[dict[str, Any]] = []
                for w in pool:
                    if w.get("status") in ("deleted", "unbound"):
                        continue
                    if "home_stand_in') IS DISTINCT FROM 'true'::jsonb" in text and _is_stand_in(w):
                        continue
                    if "dalton_historical_duplicate_of', '')), '') IS NULL" in text and _is_hist_dup(w):
                        continue
                    r = dict(w)
                    r.setdefault("owner_name", "Alice" if w["user_id"] == USER_A else "Bob")
                    r.setdefault(
                        "owner_email",
                        "alice@team.com" if w["user_id"] == USER_A else "bob@team.com",
                    )
                    r["is_home_stand_in"] = _is_stand_in(w)
                    r["is_historical_duplicate"] = _is_hist_dup(w)
                    r["config_ticker"] = _config_dict(w).get(
                        locked_recheck_mod.WORKSPACE_COMPANY_TICKER_CONFIG_KEY
                    )
                    if "AS unit_ticker" in text:
                        unit = units_by_ws.get(w["workspace_id"])
                        r["unit_ticker"] = unit.get("ticker") if unit else None
                    if "AS doc_ticker" in text:
                        doc = docs_by_ws.get(w["workspace_id"])
                        r["doc_ticker"] = doc.get("ticker") if doc else None
                    out.append(r)
                self._rows = out
                return

            if "FROM workspaces" in text and isinstance(params, dict) and "user_id" in params:
                uid = str(params["user_id"])
                out = []
                for w in workspaces:
                    if w["user_id"] != uid or w.get("status") in ("deleted", "unbound"):
                        continue
                    r = {
                        "workspace_id": w["workspace_id"],
                        "user_id": w["user_id"],
                        "name": w["name"],
                        "status": w.get("status", "stopped"),
                        "config": _config_dict(w),
                        "is_home_stand_in": _is_stand_in(w),
                        "is_historical_duplicate": _is_hist_dup(w),
                        "config_ticker": _config_dict(w).get(
                            locked_recheck_mod.WORKSPACE_COMPANY_TICKER_CONFIG_KEY
                        ),
                    }
                    if "AS doc_ticker" in text:
                        doc = docs_by_ws.get(w["workspace_id"])
                        r["doc_ticker"] = doc.get("ticker") if doc else None
                    if "AS unit_ticker" in text:
                        unit = units_by_ws.get(w["workspace_id"])
                        r["unit_ticker"] = unit.get("ticker") if unit else None
                    out.append(r)
                self._rows = out
                return

            if "FROM workspaces" in text and "WHERE user_id = %s AND name_key = %s" in text:
                uid, nkey = params
                self._rows = [
                    {"workspace_id": w["workspace_id"], "name": w["name"]}
                    for w in workspaces
                    if w["user_id"] == uid
                    and w.get("status") not in ("deleted", "unbound")
                    and workspace_name_key(w["name"]) == nkey
                ]
                return

            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _MemConn:
        def __init__(self) -> None:
            self.held_locks: list[asyncio.Lock] = []

        def cursor(self, **_kwargs):
            return _MemCursor(self)

        @asynccontextmanager
        async def transaction(self):
            yield

        def release_locks(self) -> None:
            while self.held_locks:
                lk = self.held_locks.pop()
                lk.release()
                lock_events.append("release")

    @asynccontextmanager
    async def _get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        c = _MemConn()
        try:
            yield c
        finally:
            c.release_locks()

    @asynccontextmanager
    async def _folder_allocation(_cid: str, conn=None):
        async with _get_db_connection(conn) as owned:
            yield owned

    async def _lock_user_writes(cur: Any, uid: str) -> None:
        lk = _get_lock(uid)
        await lk.acquire()
        cur._conn.held_locks.append(lk)
        lock_events.append(f"acquire:{uid}")

    return {
        "workspaces": workspaces,
        "docs_by_ws": docs_by_ws,
        "units_by_ws": units_by_ws,
        "lock_events": lock_events,
        "executed_sql": executed_sql,
        "get_db_connection": _get_db_connection,
        "folder_allocation": _folder_allocation,
        "lock_user_writes": _lock_user_writes,
    }


def _patch_web_create_env(monkeypatch: pytest.MonkeyPatch, db_state: dict[str, Any]):
    comp = _computer_row()
    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(
        _self, _uid: str, *, default_name=None, resource_tier=None
    ):
        del default_name, resource_tier
        return dict(comp)

    async def _resolve_binding(_self, _wid: str, *, workspace=None):
        del workspace
        return None

    async def _get_computer(_cid: str, *, conn=None):
        del conn
        return dict(comp)

    async def _noop_release(cur, *, computer_id: str, workspace_id: str, folder: str):
        del cur, computer_id, workspace_id, folder
        return None

    async def _noop_selection(cur, user_id: str, workspace_id: str, *, like_workspace_id=None):
        del cur, user_id, workspace_id, like_workspace_id
        return None

    async def _noop_cli_approval(http_request, principal, request):
        del http_request, principal, request
        return None

    async def _require_ws_access(
        workspace_id,
        user_id,
        channel="web",
        need="read",
        *,
        workspace=None,
        conn=None,
    ):
        del channel, need, workspace, conn
        return {"workspace_id": workspace_id, "user_id": user_id}

    monkeypatch.setattr(
        WorkspaceManager,
        "ensure_primary_computer",
        create_autospec(WorkspaceManager.ensure_primary_computer, side_effect=_ensure_primary),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "resolve_binding",
        create_autospec(WorkspaceManager.resolve_binding, side_effect=_resolve_binding),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.cli_approval.workspace_create",
        create_autospec(
            cli_approval_mod.workspace_create, side_effect=_noop_cli_approval
        ),
    )
    monkeypatch.setattr(ws_db, "get_db_connection", db_state["get_db_connection"])
    monkeypatch.setattr(ws_db, "folder_allocation", db_state["folder_allocation"])
    monkeypatch.setattr(
        ws_db,
        "lock_user_writes",
        create_autospec(ws_db.lock_user_writes, side_effect=db_state["lock_user_writes"]),
    )
    monkeypatch.setattr(
        ws_db,
        "get_computer",
        create_autospec(ws_db.get_computer, side_effect=_get_computer),
    )
    monkeypatch.setattr(
        ws_db,
        "release_former_folder",
        create_autospec(ws_db.release_former_folder, side_effect=_noop_release),
    )
    monkeypatch.setattr(
        ws_db,
        "start_new_workspace_selection",
        create_autospec(ws_db.start_new_workspace_selection, side_effect=_noop_selection),
    )
    monkeypatch.setattr(dedup_mod, "get_db_connection", db_state["get_db_connection"])
    monkeypatch.setattr(
        dedup_mod,
        "is_coverage_units_enabled",
        create_autospec(dedup_mod.is_coverage_units_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dedup_mod.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(
            dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled, return_value=False
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(sharing_mod.is_dalton_local_auth_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            sharing_mod.get_dalton_allowed_emails,
            return_value=frozenset({"alice@team.com", "bob@team.com"}),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(sharing_mod.is_invite_pm, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(sharing_mod.is_company_system_user, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_require_ws_access),
    )
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
    monkeypatch.setattr(
        dedup_mod,
        "model_requires_user_oauth",
        create_autospec(dedup_mod.model_requires_user_oauth, return_value=False),
    )


def _wrap_collect_with_barrier(
    orig_collect: Any,
    barrier_limit: int = 2,
) -> Any:
    barrier_count = 0
    both_entered = asyncio.Event()

    async def _synced_collect(
        uid: str,
        raw_name: str,
        *,
        caller_tier: str,
        include_colleagues: bool | None = None,
        conn: Any = None,
        stats: dict[str, int] | None = None,
    ):
        nonlocal barrier_count
        res = await orig_collect(
            uid,
            raw_name,
            caller_tier=caller_tier,
            include_colleagues=include_colleagues,
            conn=conn,
            stats=stats,
        )
        barrier_count += 1
        if barrier_count <= barrier_limit:
            if barrier_count == barrier_limit:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    return create_autospec(orig_collect, side_effect=_synced_collect)


def _make_llm_mock(match_tokens: tuple[str, ...] = ()) -> Any:
    async def _fake_complete(
        *,
        user_prompt: str,
        **_unused: Any,
    ):
        matches = []
        if match_tokens:
            for line in user_prompt.splitlines():
                if line.startswith("c") and any(tok in line for tok in match_tokens):
                    cid = line.split("|", 1)[0].strip()
                    matches.append(
                        DedupMatchItem(id=cid, confidence="high", reason="same company")
                    )
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=matches))

    return create_autospec(dedup_mod.dalton_complete, side_effect=_fake_complete)


# =============================================================================
# 1. Item 1: Bound Home / stand-in exclusion parity & drift without ID
# =============================================================================


@pytest.mark.asyncio
async def test_e2_web_create_succeeds_first_try_when_team_and_caller_have_bound_homes_and_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: Bound Home rows (for caller and teammates) and Home stand-ins
    are excluded identically in Phase 1 and Phase 2; ordinary web create succeeds
    on attempt 1 with zero false drift and CoS locked recheck also passes."""
    from src.server.app.workspaces import router as ws_router

    caller_home_id = get_home_workspace_id(USER_A)
    caller_stand_in_id = get_home_stand_in_id(USER_A)
    colleague_home_id = get_home_workspace_id(USER_B)

    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": caller_home_id,
                "user_id": USER_A,
                "name": "Home",
                "status": "stopped",
                "owner_name": "Alice",
                "owner_email": "alice@team.com",
                "config": {},
            },
            {
                "workspace_id": caller_stand_in_id,
                "user_id": USER_A,
                "name": "Home",
                "status": "stopped",
                "owner_name": "Alice",
                "owner_email": "alice@team.com",
                "config": {HOME_STAND_IN_KEY: True},
            },
            {
                "workspace_id": colleague_home_id,
                "user_id": USER_B,
                "name": "Home",
                "status": "running",
                "owner_name": "Bob",
                "owner_email": "bob@team.com",
                "config": {},
            },
        ]
    )
    _patch_web_create_env(monkeypatch, db_state)

    orig_collect = dedup_mod._collect_candidates
    collect_spy = create_autospec(orig_collect, side_effect=orig_collect)
    monkeypatch.setattr(dedup_mod, "_collect_candidates", collect_spy)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workspaces", json={"name": "Nvidia"})

    assert resp.status_code == 201, f"Expected 201 with bound Homes present, got {resp.status_code}: {resp.text}"
    assert resp.json()["name"] == "Nvidia"
    # Succeeded on first attempt with zero false drift retries.
    assert collect_spy.await_count == 1

    # Chief of Staff Phase 2 locked recheck with verified snapshot also ignores bound Homes.
    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok_team = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "Apple",
                verified_company_candidates=[(resp.json()["workspace_id"], "Nvidia", None, None)],
                check_colleague_duplicate=True,
                conn=conn,
            )
            ok_own = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "Apple",
                verified_company_candidates=[(resp.json()["workspace_id"], "Nvidia", None, None)],
                check_colleague_duplicate=False,
                conn=conn,
            )
    assert ok_team is True
    assert ok_own is True


@pytest.mark.asyncio
async def test_e2_drift_without_conflicting_id_never_fakes_company_workspace_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: Pure snapshot drift (conflicting_workspace_id=None) retries and
    returns 409 workspace_create_conflict on exhaustion, never company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.side_effect = DuplicateWorkspaceConflictError(
        conflicting_workspace_id=None
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(_REAL_GET_INSTANCE, return_value=mgr),
    )
    det_spy = create_autospec(
        _REAL_BUILD_DET_MATCH,
        side_effect=_REAL_BUILD_DET_MATCH,
    )
    monkeypatch.setattr(dedup_mod, "build_deterministic_duplicate_match", det_spy)

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workspaces", json={"name": "Alibaba"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "workspace_create_conflict"
    det_spy.assert_not_awaited()
    assert mgr.create_workspace.await_count == 3


# =============================================================================
# 2. Item 2: Alias concurrency + same-tx company identity commit (LLM empty)
# =============================================================================


@pytest.mark.asyncio
async def test_e2_web_concurrent_alias_same_ticker_commits_identity_in_same_tx_when_llm_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: Two concurrent requests with the same ticker ('0700.HK' and '700.HK')
    and different non-ticker alias names ('腾讯控股' and 'Tencent'), fixed registration
    timing (coverage unit registered inside the same tx as workspace insert), and
    LLM returning empty matches -> only ONE workspace is created and the second
    returns 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)

    monkeypatch.setattr(
        units_mod,
        "is_workspace_coverage_registration_active",
        create_autospec(_REAL_IS_WS_COV_REG_ACTIVE, return_value=True),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(_REAL_IS_INTERNAL_OR_ADMIN, return_value=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_settings",
        lambda: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )

    registration_saw_held_lock: list[bool] = []

    async def _fake_insert_unit(
        *,
        kind: str,
        owner_user_id: str,
        slug: str,
        display_name: str = "",
        ticker: str | None = None,
        ticker_key: str | None = None,
        lead_analyst_user_id: str | None = None,
        workspace_id: str | None = None,
        research_tz: str | None = None,
        unit_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        del research_tz
        # Verify coverage unit insert runs inside the same transaction while lock_user_writes is still held.
        registration_saw_held_lock.append(bool(conn is not None and getattr(conn, "held_locks", [])))
        row = {
            "unit_id": unit_id or "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "kind": kind,
            "owner_user_id": owner_user_id,
            "lead_analyst_user_id": lead_analyst_user_id or owner_user_id,
            "slug": slug,
            "display_name": display_name,
            "ticker": ticker,
            "ticker_key": ticker_key,
            "workspace_id": workspace_id,
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }
        if workspace_id:
            db_state["units_by_ws"][workspace_id] = row
        return row

    monkeypatch.setattr(
        cu_db,
        "insert_coverage_unit",
        create_autospec(cu_db.insert_coverage_unit, side_effect=_fake_insert_unit),
    )
    monkeypatch.setattr(
        cu_db,
        "_can_read_workspace",
        create_autospec(cu_db._can_read_workspace, return_value=True),
    )
    monkeypatch.setattr(
        units_mod,
        "_acquire_sorted_personal_coverage_locks",
        create_autospec(units_mod._acquire_sorted_personal_coverage_locks, return_value=[]),
    )
    monkeypatch.setattr(
        units_mod,
        "_sync_lead_personal_coverage",
        create_autospec(units_mod._sync_lead_personal_coverage, return_value=None),
    )

    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    # LLM returns EMPTY matches every single time.
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post(
                "/api/v1/workspaces",
                json={"name": "腾讯控股", "coverage_kind": "company", "ticker": "0700.HK"},
            ),
            client.post(
                "/api/v1/workspaces",
                json={"name": "Tencent", "coverage_kind": "company", "ticker": "700.HK"},
            ),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    assert len(db_state["workspaces"]) == 1
    assert registration_saw_held_lock == [True]
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_alias_explicit_ticker_without_coverage_kind_when_llm_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: Even when coverage_kind is omitted, explicit ticker is stamped
    onto workspace.config in the same transaction so a concurrent alias with
    the same ticker is caught deterministically with LLM returning empty."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "腾讯控股", "ticker": "0700.HK"}),
            client.post("/api/v1/workspaces", json={"name": "Tencent", "ticker": "00700"}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    assert len(db_state["workspaces"]) == 1
    assert (
        db_state["workspaces"][0]["config"][
            locked_recheck_mod.WORKSPACE_COMPANY_TICKER_CONFIG_KEY
        ]
        == "00700"
    )
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_same_company_ticker_variants_creates_only_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Two concurrent web creates for ticker variants ('700.HK' and '0700.HK')
    serialize under lock_user_writes; only one workspace is created."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock(("700.HK", "0700.HK")))

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK"}),
            client.post("/api/v1/workspaces", json={"name": "0700.HK"}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    assert len(db_state["workspaces"]) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_same_company_different_alias_names_creates_only_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Two concurrent web creates for non-ticker aliases ('腾讯控股' and 'Tencent')
    serialize under lock_user_writes; retry dedup catches the first workspace."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock(("腾讯控股", "Tencent")))

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "腾讯控股"}),
            client.post("/api/v1/workspaces", json={"name": "Tencent"}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    assert len(db_state["workspaces"]) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_different_companies_both_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2 control: Two concurrent web creates for unrelated companies both succeed."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "腾讯控股"}),
            client.post("/api/v1/workspaces", json={"name": "阿里巴巴"}),
        )

    assert [resp_a.status_code, resp_b.status_code] == [201, 201]
    assert sorted(w["name"] for w in db_state["workspaces"]) == ["腾讯控股", "阿里巴巴"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_deterministic_fallback_when_retry_llm_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Even if follow-up LLM returns empty matches after a locked ticker conflict,
    the deterministic fallback returns 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK"}),
            client.post("/api/v1/workspaces", json={"name": "00700"}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    assert len(db_state["workspaces"]) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


# =============================================================================
# 3. Item 3 & Item 6: Separation rule + internal-member SQL scoping
# =============================================================================


def test_separation_upstream_files_have_no_dalton_tables_or_heavy_dedup_logic() -> None:
    """Item 3: Upstream workspace.py must not reference Dalton tables in SQL,
    and upstream workspaces.py delegates dedup/retry/409 to src/dalton."""
    repo_root = Path(__file__).resolve().parents[3]
    ws_db_src = (repo_root / "src/server/database/workspace.py").read_text(encoding="utf-8")
    ws_app_src = (repo_root / "src/server/app/workspaces.py").read_text(encoding="utf-8")

    assert "dalton_knowledge_versions" not in ws_db_src
    assert "dalton_coverage_units" not in ws_db_src
    assert "dalton_user_roles" not in ws_db_src
    assert "src.dalton.coverage.locked_recheck" in ws_db_src

    assert "create_workspace_for_web" in ws_app_src
    assert "workspace_create_conflict" not in ws_app_src
    assert "company_workspace_exists" not in ws_app_src


@pytest.mark.asyncio
async def test_locked_recheck_scopes_team_scan_to_internal_members_in_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: Locked team recheck restricts workspaces scan to internal team
    members in SQL (both Supabase auth and local auth) and orders deterministically."""
    db_state = _make_in_memory_workspace_db(local_internal_users=[USER_A, USER_B])
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            sharing_mod.get_dalton_allowed_emails,
            return_value=frozenset({"alice@team.com", "bob@team.com"}),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(sharing_mod.is_invite_pm, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(sharing_mod.is_company_system_user, return_value=False),
    )

    # 1. Supabase auth mode: scopes by pm_user_ids / allowed_emails in SQL.
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(
            dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled, return_value=False
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(sharing_mod.is_dalton_local_auth_enabled, return_value=False),
    )
    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok = await ws_db.check_duplicate_snapshot_unchanged(
                cur, USER_A, "NVDA", check_colleague_duplicate=True, conn=conn
            )
    assert ok is True
    team_sql_supa = [
        (sql, params) for sql, params in db_state["executed_sql"] if "FROM workspaces w" in sql
    ]
    assert len(team_sql_supa) == 1
    sql_text, sql_params = team_sql_supa[0]
    assert "allowed_emails" in sql_text
    assert "pm_user_ids" in sql_text
    assert "ORDER BY w.created_at ASC, w.workspace_id ASC" in sql_text
    assert sorted(sql_params["allowed_emails"]) == ["alice@team.com", "bob@team.com"]

    # 2. Local auth mode: scopes by internal_user_ids from dalton_user_roles in SQL.
    db_state["executed_sql"].clear()
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=True),
    )
    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok_local = await ws_db.check_duplicate_snapshot_unchanged(
                cur, USER_A, "NVDA", check_colleague_duplicate=True, conn=conn
            )
    assert ok_local is True
    team_sql_local = [
        (sql, params) for sql, params in db_state["executed_sql"] if "FROM workspaces w" in sql
    ]
    assert len(team_sql_local) == 1
    local_sql_text, local_sql_params = team_sql_local[0]
    assert "w.user_id = ANY(%(internal_user_ids)s::text[])" in local_sql_text
    assert sorted(local_sql_params["internal_user_ids"]) == sorted([USER_A, USER_B])


# =============================================================================
# 4. Item 4: Standalone Core (local auth) & snapshot collection failure
# =============================================================================


@pytest.mark.asyncio
async def test_core_local_auth_runs_own_locked_recheck_without_colleague_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: Standalone Core (DALTON_PROFILE=core, local auth, colleague sharing off)
    runs own-only locked recheck for the caller without leaking colleague workspaces."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_COLLEAGUE,
                "user_id": USER_B,
                "name": "700.HK",
                "status": "stopped",
                "owner_name": "Bob",
                "owner_email": "bob@core.local",
                "config": {},
            }
        ],
        local_internal_users=[USER_A, USER_B],
    )
    _patch_web_create_env(monkeypatch, db_state)

    # Configure Standalone Core: local auth ON, supabase auth OFF, COLLEAGUE_SHARING OFF.
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("DALTON_CAP_COLLEAGUE_SHARING", raising=False)
    monkeypatch.setattr(
        dedup_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(_REAL_IS_SUPA_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_with_barrier(dedup_mod._collect_candidates)
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Caller creates '700.HK' and '0700.HK' concurrently: Bob's colleague workspace
        # must NOT block Alice (colleague sharing off), while Alice's own two concurrent
        # requests MUST serialize under own locked recheck -> [201, 409].
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK"}),
            client.post("/api/v1/workspaces", json={"name": "0700.HK"}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    alice_ws = [w for w in db_state["workspaces"] if w["user_id"] == USER_A]
    assert len(alice_ws) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"
    assert conflict_resp.json()["detail"]["matches"][0]["workspace_id"] == alice_ws[0]["workspace_id"]


@pytest.mark.asyncio
async def test_snapshot_collection_failure_still_enforces_locked_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: When Phase 1 candidate collection fails and produces no snapshot,
    web create explicitly runs locked deterministic recheck instead of bypassing."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_OWN_A,
                "user_id": USER_A,
                "name": "0700.HK",
                "status": "stopped",
                "owner_name": "Alice",
                "owner_email": "alice@team.com",
                "config": {},
            }
        ]
    )
    _patch_web_create_env(monkeypatch, db_state)

    # First call (Phase 1) fails candidate collection; post-conflict call returns None.
    calls = {"n": 0}

    async def _failing_collect(
        uid: str,
        raw_name: str,
        *,
        caller_tier: str,
        include_colleagues: bool | None = None,
        conn: Any = None,
        stats: dict[str, int] | None = None,
    ):
        del uid, raw_name, caller_tier, include_colleagues, conn, stats
        calls["n"] += 1
        raise RuntimeError("transient candidate query failure")

    monkeypatch.setattr(
        dedup_mod,
        "_collect_candidates",
        create_autospec(dedup_mod._collect_candidates, side_effect=_failing_collect),
    )

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workspaces", json={"name": "700.HK"})

    # Locked deterministic recheck caught existing '0700.HK' and refused duplicate creation.
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "company_workspace_exists"
    assert resp.json()["detail"]["matches"][0]["workspace_id"] == WS_OWN_A
    assert len(db_state["workspaces"]) == 1


# =============================================================================
# 5. BD4: Locked recheck reads company knowledge doc tickers
# =============================================================================


@pytest.mark.asyncio
async def test_bd4_locked_recheck_matches_colleague_company_knowledge_doc_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Locked recheck reads company knowledge doc ticker from
    dalton_knowledge_versions (in src/dalton/coverage/locked_recheck.py)."""
    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_DOC_ONLY,
                "user_id": USER_B,
                "name": "腾讯深度研究",
                "status": "stopped",
                "owner_name": "Bob",
                "owner_email": "bob@team.com",
            }
        ],
        knowledge_docs={WS_DOC_ONLY: {"ticker": "0700.HK", "name": "Tencent"}},
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(
            dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled, return_value=False
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(sharing_mod.is_dalton_local_auth_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            sharing_mod.get_dalton_allowed_emails,
            return_value=frozenset({"alice@team.com", "bob@team.com"}),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(sharing_mod.is_invite_pm, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(sharing_mod.is_company_system_user, return_value=False),
    )

    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "700.HK",
                check_colleague_duplicate=True,
                conn=conn,
            )
    assert ok is False


@pytest.mark.asyncio
async def test_bd4_locked_recheck_matches_own_company_knowledge_doc_ticker_and_detects_doc_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Locked recheck reads doc_ticker for caller's own workspaces and detects doc drift."""
    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_DOC_ONLY,
                "user_id": USER_A,
                "name": "腾讯深度研究",
                "status": "stopped",
                "owner_name": "Alice",
                "owner_email": "alice@team.com",
            }
        ],
        knowledge_docs={WS_DOC_ONLY: {"ticker": "0700.HK", "name": "Tencent"}},
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(
            dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled, return_value=False
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(sharing_mod.is_dalton_local_auth_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            sharing_mod.get_dalton_allowed_emails,
            return_value=frozenset({"alice@team.com"}),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(sharing_mod.is_invite_pm, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(sharing_mod.is_company_system_user, return_value=False),
    )

    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok_own = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "00700",
                check_colleague_duplicate=False,
                conn=conn,
            )
            assert ok_own is False

            ok_drift = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "阿里巴巴",
                verified_company_candidates=[(WS_DOC_ONLY, "腾讯深度研究", None, None)],
                check_colleague_duplicate=True,
                conn=conn,
            )
            assert ok_drift is False


@pytest.mark.asyncio
async def test_bd4_phase1_deterministic_recheck_matches_doc_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Phase 1 lock-free recheck includes doc_ticker in checked_candidates
    and deterministic ticker matching when LLM misses."""
    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_DOC_ONLY,
                "user_id": USER_B,
                "name": "腾讯深度研究",
                "status": "stopped",
                "owner_name": "Bob",
                "owner_email": "bob@team.com",
            }
        ],
        knowledge_docs={WS_DOC_ONLY: {"ticker": "0700.HK", "name": "Tencent"}},
    )
    _patch_web_create_env(monkeypatch, db_state)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_mock())

    snap = await recheck_company_workspace_before_create(USER_A, "700.HK", channel="cos")
    assert snap.status == "matched"
    assert WS_DOC_ONLY in snap.matched_workspace_ids
    assert any(
        len(c) >= 4 and c[0] == WS_DOC_ONLY and c[3] == "00700"
        for c in snap.checked_candidates
    )


@pytest.mark.asyncio
async def test_bd4_locked_recheck_matches_explicit_target_ticker_against_doc_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Explicit target_ticker matches colleague workspace company knowledge doc ticker."""
    db_state = _make_in_memory_workspace_db(
        initial_workspaces=[
            {
                "workspace_id": WS_DOC_ONLY,
                "user_id": USER_B,
                "name": "腾讯深度研究",
                "status": "stopped",
                "owner_name": "Bob",
                "owner_email": "bob@team.com",
            }
        ],
        knowledge_docs={WS_DOC_ONLY: {"ticker": "00700", "name": "Tencent"}},
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(
            dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled, return_value=False
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(sharing_mod.is_dalton_local_auth_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(
            sharing_mod.get_dalton_allowed_emails,
            return_value=frozenset({"alice@team.com", "bob@team.com"}),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(sharing_mod.is_invite_pm, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(sharing_mod.is_company_system_user, return_value=False),
    )

    conflict: dict[str, Any] = {}
    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            ok = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "Tencent Holdings",
                check_colleague_duplicate=True,
                target_ticker="0700.HK",
                conflict_out=conflict,
                conn=conn,
            )
    assert ok is False
    assert conflict["workspace_id"] == WS_DOC_ONLY


# =============================================================================
# 6. Item 5 (O1 / Q1): Execution history link visibility parity with web read
# =============================================================================


def _build_sqlite_execution_history_db(
    *,
    colleague_ws_status: str = "running",
    own_thread_initiator: str = USER_A,
) -> sqlite3.Connection:
    sq = sqlite3.connect(":memory:")
    sq.row_factory = sqlite3.Row
    sq.executescript(
        """
        CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            computer_id TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            config TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE automations (
            automation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            workspace_id TEXT,
            scope TEXT NOT NULL DEFAULT 'personal',
            deleted_at TEXT
        );
        CREATE TABLE conversation_threads (
            conversation_thread_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT,
            metadata TEXT,
            is_shared INTEGER DEFAULT 0,
            msg_type TEXT,
            llm_model TEXT,
            reasoning_effort TEXT,
            fast_mode INTEGER DEFAULT 0,
            title TEXT,
            created_at TEXT
        );
        CREATE TABLE automation_executions (
            automation_execution_id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            conversation_thread_id TEXT,
            scheduled_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            error_message TEXT,
            skip_reason TEXT,
            failure_reason TEXT,
            server_id TEXT,
            delivery_result TEXT,
            created_at TEXT NOT NULL,
            dismissed_at TEXT,
            wait_reason TEXT,
            result_excerpt TEXT
        );
        """
    )
    sq.executemany(
        "INSERT INTO workspaces (workspace_id, user_id, computer_id, status, config) VALUES (?, ?, ?, ?, ?)",
        [
            (WS_OWN_A, USER_A, COMP_A, "running", "{}"),
            (WS_COLLEAGUE, USER_B, COMP_A, colleague_ws_status, "{}"),
        ],
    )
    sq.executemany(
        "INSERT INTO automations (automation_id, user_id, name, trigger_type, workspace_id, scope, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        [
            (AUTO_COLLEAGUE, USER_A, "Colleague WS Task", "cron", WS_COLLEAGUE, "personal"),
            (AUTO_OWN, USER_A, "Own WS Task", "cron", WS_OWN_A, "personal"),
        ],
    )
    sq.executemany(
        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id) VALUES (?, ?, ?)",
        [
            (THREAD_COLLEAGUE, WS_COLLEAGUE, USER_A),
            (THREAD_OWN, WS_OWN_A, own_thread_initiator),
        ],
    )
    sq.executemany(
        "INSERT INTO automation_executions "
        "(automation_execution_id, automation_id, status, conversation_thread_id, created_at) "
        "VALUES (?, ?, 'completed', ?, ?)",
        [
            (EXEC_COLLEAGUE, AUTO_COLLEAGUE, THREAD_COLLEAGUE, "2026-11-26T02:00:00Z"),
            (EXEC_OWN, AUTO_OWN, THREAD_OWN, "2026-11-26T01:00:00Z"),
        ],
    )
    sq.commit()
    return sq


def _sqlite_async_wrapper(sq: sqlite3.Connection):
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    last_rows: list[dict[str, Any]] = []
    executed_sql: list[str] = []

    async def _execute(sql: str, params=()):
        nonlocal last_rows
        executed_sql.append(sql)
        sqlite_sql = sql.replace("::uuid", "").replace("%s", "?")
        sq_cur = sq.execute(sqlite_sql, tuple(params) if params is not None else ())
        rows = []
        for row in sq_cur.fetchall():
            d = dict(row)
            if isinstance(d.get("workspace_config"), str):
                d["workspace_config"] = json.loads(d["workspace_config"])
            if isinstance(d.get("config"), str):
                d["config"] = json.loads(d["config"])
            rows.append(d)
        last_rows = rows

    async def _fetchall():
        return list(last_rows)

    async def _fetchone():
        return last_rows[0] if last_rows else None

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone

    @asynccontextmanager
    async def _cursor(**_kwargs):
        yield cur

    @asynccontextmanager
    async def _get_db_connection(existing=None):
        yield existing if existing is not None else conn

    conn.cursor.side_effect = _cursor
    return _get_db_connection, executed_sql


@pytest.mark.asyncio
async def test_o1_q1_execution_history_strips_inaccessible_colleague_workspace_and_thread_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1: Colleague dispatch off + hidden colleague workspace strips both
    workspace_id and conversation_thread_id while keeping the execution row."""
    sq = _build_sqlite_execution_history_db()
    get_conn, executed_sql = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "_multiuser_auth_enabled",
            create_autospec(sharing_mod._multiuser_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=False),
        )
        monkeypatch.setattr(
            sharing_mod,
            "require_team_reader",
            create_autospec(sharing_mod.require_team_reader, return_value=USER_A),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_tier",
            create_autospec(sharing_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "classify_owner_tier",
            create_autospec(sharing_mod.classify_owner_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_workspace_sharing",
            create_autospec(
                sharing_mod.get_workspace_sharing,
                return_value={"web_access": "hidden", "cos_access": "hidden"},
            ),
        )

        with (
            patch("src.server.database.automation_executions.get_db_connection", new=get_conn),
            patch("src.server.database.pool.get_db_connection", new=get_conn),
        ):
            rows, has_more = await exec_db.list_executions(USER_A, limit=20)

        assert has_more is False
        first_sql = executed_sql[0]
        assert "t.user_id" not in first_sql
        assert "dalton_" not in first_sql.lower()

        by_id = {r["automation_execution_id"]: r for r in rows}
        assert by_id[EXEC_OWN]["workspace_id"] == WS_OWN_A
        assert by_id[EXEC_OWN]["conversation_thread_id"] == THREAD_OWN
        assert EXEC_COLLEAGUE in by_id
        assert by_id[EXEC_COLLEAGUE]["workspace_id"] is None
        assert by_id[EXEC_COLLEAGUE]["conversation_thread_id"] is None
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_o1_q1_execution_history_keeps_accessible_colleague_thread_when_web_read_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1 positive control: When colleague workspace allows web read (ro),
    execution row retains colleague workspace_id and conversation_thread_id."""
    sq = _build_sqlite_execution_history_db()
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "_multiuser_auth_enabled",
            create_autospec(sharing_mod._multiuser_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=False),
        )
        monkeypatch.setattr(
            sharing_mod,
            "require_team_reader",
            create_autospec(sharing_mod.require_team_reader, return_value=USER_A),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_tier",
            create_autospec(sharing_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "classify_owner_tier",
            create_autospec(sharing_mod.classify_owner_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_workspace_sharing",
            create_autospec(
                sharing_mod.get_workspace_sharing,
                return_value={"web_access": "ro", "cos_access": "hidden"},
            ),
        )

        with (
            patch("src.server.database.automation_executions.get_db_connection", new=get_conn),
            patch("src.server.database.pool.get_db_connection", new=get_conn),
        ):
            rows, _ = await exec_db.list_executions(USER_A, limit=20)

        by_id = {r["automation_execution_id"]: r for r in rows}
        assert by_id[EXEC_COLLEAGUE]["workspace_id"] == WS_COLLEAGUE
        assert by_id[EXEC_COLLEAGUE]["conversation_thread_id"] == THREAD_COLLEAGUE
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_o1_q1_execution_history_keeps_initiator_thread_link_when_colleague_workspace_soft_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: When a colleague workspace is soft-deleted and colleague dispatch
    is enabled, web read rules (threads_read.py + sharing.py) still allow the
    thread initiator to read their own thread; execution history MUST keep
    conversation_thread_id while clearing the deleted workspace_id."""
    sq = _build_sqlite_execution_history_db(colleague_ws_status="deleted")
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "_multiuser_auth_enabled",
            create_autospec(sharing_mod._multiuser_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "require_team_reader",
            create_autospec(sharing_mod.require_team_reader, return_value=USER_A),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_tier",
            create_autospec(sharing_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "classify_owner_tier",
            create_autospec(sharing_mod.classify_owner_tier, return_value="internal"),
        )

        with (
            patch("src.server.database.automation_executions.get_db_connection", new=get_conn),
            patch("src.server.database.pool.get_db_connection", new=get_conn),
        ):
            rows, _ = await exec_db.list_executions(USER_A, limit=20)

        by_id = {r["automation_execution_id"]: r for r in rows}
        # Initiator still has web read access to their own thread even after colleague workspace soft-delete.
        assert by_id[EXEC_COLLEAGUE]["conversation_thread_id"] == THREAD_COLLEAGUE
        # Soft-deleted workspace itself is NOT exposed as workspace_id.
        assert by_id[EXEC_COLLEAGUE]["workspace_id"] is None
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_o1_q1_execution_history_strips_colleague_links_for_external_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1: An external caller never receives colleague workspace_id or
    conversation_thread_id in execution history even if colleague dispatch is on."""
    sq = _build_sqlite_execution_history_db()
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "_multiuser_auth_enabled",
            create_autospec(sharing_mod._multiuser_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "require_team_reader",
            create_autospec(sharing_mod.require_team_reader, return_value=USER_A),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_tier",
            create_autospec(sharing_mod.get_user_tier, return_value="external"),
        )

        with (
            patch("src.server.database.automation_executions.get_db_connection", new=get_conn),
            patch("src.server.database.pool.get_db_connection", new=get_conn),
        ):
            rows, _ = await exec_db.list_executions(USER_A, limit=20)

        by_id = {r["automation_execution_id"]: r for r in rows}
        assert by_id[EXEC_COLLEAGUE]["workspace_id"] is None
        assert by_id[EXEC_COLLEAGUE]["conversation_thread_id"] is None
        assert by_id[EXEC_OWN]["workspace_id"] == WS_OWN_A
        assert by_id[EXEC_OWN]["conversation_thread_id"] == THREAD_OWN
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_o1_q1_execution_history_external_caller_strips_colleague_thread_in_own_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5 (Codex B): Even when a thread lives inside the caller's own live
    workspace, if the thread was initiated by a colleague and the caller is now
    an external member (whom sharing.py rejects with 404), execution history
    strips conversation_thread_id rather than trusting the owner SQL fast path."""
    sq = _build_sqlite_execution_history_db(own_thread_initiator=USER_B)
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "_multiuser_auth_enabled",
            create_autospec(sharing_mod._multiuser_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "require_team_reader",
            create_autospec(sharing_mod.require_team_reader, return_value=USER_A),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_tier",
            create_autospec(sharing_mod.get_user_tier, return_value="external"),
        )

        with (
            patch("src.server.database.automation_executions.get_db_connection", new=get_conn),
            patch("src.server.database.pool.get_db_connection", new=get_conn),
        ):
            rows, _ = await exec_db.list_executions(USER_A, limit=20)

        by_id = {r["automation_execution_id"]: r for r in rows}
        # Colleague-initiated thread inside external caller's own workspace is unreadable (404 on web).
        assert by_id[EXEC_OWN]["conversation_thread_id"] is None
        # Caller's own live task workspace remains accessible.
        assert by_id[EXEC_OWN]["workspace_id"] == WS_OWN_A
    finally:
        sq.close()
