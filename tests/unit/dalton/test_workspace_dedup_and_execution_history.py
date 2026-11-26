"""Reproducing & regression tests for Bundle 7:
1. E2: Web workspace create concurrent company dedup under lock_user_writes.
2. BD4: Locked recheck reads company knowledge doc tickers from dalton_knowledge_versions.
3. O1/Q1: Execution history resolves linked thread & colleague workspace visibility in Python per page.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

from httpx import ASGITransport, AsyncClient
import psycopg
import pytest

from src.dalton import sharing as sharing_mod
from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage.dedup import (
    DedupLlmResult,
    DedupMatchItem,
    recheck_company_workspace_before_create,
)
from src.server.database import automation_executions as exec_db
from src.server.database import workspace as ws_db
from src.server.database.workspace_names import workspace_name_key
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app

USER_A = "test-user-123"
USER_B = "user-colleague-b"
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
):
    """In-memory transactional workspace DB with a real asyncio.Lock for lock_user_writes."""
    workspaces: list[dict[str, Any]] = [dict(w) for w in (initial_workspaces or [])]
    docs_by_ws: dict[str, dict[str, Any]] = dict(knowledge_docs or {})
    units_by_ws: dict[str, dict[str, Any]] = dict(coverage_units or {})
    user_locks: dict[str, asyncio.Lock] = {}
    lock_events: list[str] = []

    def _get_lock(uid: str) -> asyncio.Lock:
        return user_locks.setdefault(uid, asyncio.Lock())

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
            if "FROM dalton_coverage_units u" in text:
                self._rows = []
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
                # Enforce unique (user_id, name_key) like idx_workspaces_user_name_key
                for existing in workspaces:
                    if (
                        existing["user_id"] == uid
                        and existing["status"] not in ("deleted", "unbound")
                        and workspace_name_key(existing["name"]) == nkey
                    ):
                        err = psycopg.errors.UniqueViolation("duplicate name_key")
                        raise err
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
                    "config": {},
                    "is_pinned": False,
                    "sort_order": 0,
                    "resource_tier": "standard",
                    "is_always_on": False,
                    "files_restore_incomplete": False,
                    "owner_name": "Alice",
                    "owner_email": "alice@team.com",
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
                if params and isinstance(params, tuple) and len(params) == 1 and isinstance(params[0], list):
                    wanted = set(params[0])
                    pool = [w for w in workspaces if w["workspace_id"] in wanted]
                elif isinstance(params, dict) and "ws_ids" in params:
                    wanted = set(params["ws_ids"])
                    pool = [w for w in workspaces if w["workspace_id"] in wanted]
                elif params and isinstance(params, tuple) and len(params) == 1 and isinstance(params[0], str):
                    uid_filter = params[0]
                    pool = [w for w in workspaces if w["user_id"] == uid_filter]
                else:
                    pool = list(workspaces)
                out: list[dict[str, Any]] = []
                for w in pool:
                    if w.get("status") in ("deleted", "unbound"):
                        continue
                    r = dict(w)
                    r.setdefault("owner_name", "Alice" if w["user_id"] == USER_A else "Bob")
                    r.setdefault(
                        "owner_email",
                        "alice@team.com" if w["user_id"] == USER_A else "bob@team.com",
                    )
                    r["is_home_stand_in"] = False
                    r["is_historical_duplicate"] = False
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
                        "name": w["name"],
                        "is_historical_duplicate": False,
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
        "get_db_connection": _get_db_connection,
        "folder_allocation": _folder_allocation,
        "lock_user_writes": _lock_user_writes,
    }


def _patch_web_create_env(monkeypatch: pytest.MonkeyPatch, db_state: dict[str, Any]):
    comp = _computer_row()
    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(_self, _uid: str, **_kw):
        return dict(comp)

    async def _resolve_binding(_self, _wid: str, **_kw):
        return None

    async def _get_computer(_cid: str, conn=None):
        return dict(comp)

    async def _noop_release(*_a, **_kw):
        return None

    async def _noop_selection(*_a, **_kw):
        return None

    async def _noop_cli_approval(*_a, **_kw):
        return None

    async def _require_ws_access(ws_id, uid, **_kw):
        return {"workspace_id": ws_id, "user_id": uid}

    monkeypatch.setattr(WorkspaceManager, "ensure_primary_computer", _ensure_primary)
    monkeypatch.setattr(WorkspaceManager, "resolve_binding", _resolve_binding)
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.cli_approval.workspace_create",
        _noop_cli_approval,
    )
    monkeypatch.setattr(ws_db, "get_db_connection", db_state["get_db_connection"])
    monkeypatch.setattr(ws_db, "folder_allocation", db_state["folder_allocation"])
    monkeypatch.setattr(ws_db, "lock_user_writes", db_state["lock_user_writes"])
    monkeypatch.setattr(ws_db, "get_computer", _get_computer)
    monkeypatch.setattr(ws_db, "release_former_folder", _noop_release)
    monkeypatch.setattr(ws_db, "start_new_workspace_selection", _noop_selection)
    monkeypatch.setattr(dedup_mod, "get_db_connection", db_state["get_db_connection"])
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "require_workspace_access", _require_ws_access)
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


# =============================================================================
# 1. E2: Web concurrent workspace create dedup under lock_user_writes
# =============================================================================


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_same_company_ticker_variants_creates_only_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Two concurrent web creates for the same company ('700.HK' and '0700.HK')
    both pass initial lock-free dedup; the locked recheck serializes them under
    lock_user_writes so only one workspace is created and the second receives
    409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)

    barrier_count = 0
    both_entered = asyncio.Event()
    orig_collect = dedup_mod._collect_candidates

    async def _synced_collect(*args, **kwargs):
        nonlocal barrier_count
        res = await orig_collect(*args, **kwargs)
        barrier_count += 1
        if barrier_count <= 2:
            if barrier_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    async def _fake_complete(**kwargs):
        user_prompt = kwargs["user_prompt"]
        # Match Tencent / 700.HK / 0700.HK if present in candidate list.
        matches = []
        for line in user_prompt.splitlines():
            if line.startswith("c") and ("700.HK" in line or "0700.HK" in line):
                cid = line.split("|", 1)[0].strip()
                matches.append(DedupMatchItem(id=cid, confidence="high", reason="same ticker"))
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=matches))

    monkeypatch.setattr(dedup_mod, "_collect_candidates", _synced_collect)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _fake_complete)

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK"}),
            client.post("/api/v1/workspaces", json={"name": "0700.HK"}),
        )

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    assert statuses == [201, 409], f"Expected [201, 409], got {statuses}: {resp_a.json()} / {resp_b.json()}"
    assert len(db_state["workspaces"]) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert len(detail["matches"]) == 1
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_same_company_different_alias_names_creates_only_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Two concurrent web creates for the same company using different non-ticker
    names ('腾讯控股' and 'Tencent') both pass initial dedup; the second detects
    candidate-set drift under lock_user_writes, re-runs dedup against the newly
    committed workspace, and returns 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)

    barrier_count = 0
    both_entered = asyncio.Event()
    orig_collect = dedup_mod._collect_candidates

    async def _synced_collect(*args, **kwargs):
        nonlocal barrier_count
        res = await orig_collect(*args, **kwargs)
        barrier_count += 1
        if barrier_count <= 2:
            if barrier_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    async def _fake_complete(**kwargs):
        user_prompt = kwargs["user_prompt"]
        matches = []
        for line in user_prompt.splitlines():
            if line.startswith("c") and ("腾讯控股" in line or "Tencent" in line):
                cid = line.split("|", 1)[0].strip()
                matches.append(DedupMatchItem(id=cid, confidence="high", reason="same company"))
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=matches))

    monkeypatch.setattr(dedup_mod, "_collect_candidates", _synced_collect)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _fake_complete)

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "腾讯控股"}),
            client.post("/api/v1/workspaces", json={"name": "Tencent"}),
        )

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    assert statuses == [201, 409], f"Expected [201, 409], got {statuses}: {resp_a.json()} / {resp_b.json()}"
    assert len(db_state["workspaces"]) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    detail = conflict_resp.json()["detail"]
    assert detail["code"] == "company_workspace_exists"
    assert detail["matches"][0]["workspace_id"] == db_state["workspaces"][0]["workspace_id"]


@pytest.mark.asyncio
async def test_e2_web_concurrent_create_different_companies_both_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2 control: Two concurrent web creates for unrelated companies ('腾讯控股' and
    '阿里巴巴') both succeed after the second retries against the refreshed snapshot."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)

    barrier_count = 0
    both_entered = asyncio.Event()
    orig_collect = dedup_mod._collect_candidates

    async def _synced_collect(*args, **kwargs):
        nonlocal barrier_count
        res = await orig_collect(*args, **kwargs)
        barrier_count += 1
        if barrier_count <= 2:
            if barrier_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    async def _fake_complete(**_kwargs):
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "_collect_candidates", _synced_collect)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _fake_complete)

    app = create_test_app(ws_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "腾讯控股"}),
            client.post("/api/v1/workspaces", json={"name": "阿里巴巴"}),
        )

    assert [resp_a.status_code, resp_b.status_code] == [201, 201]
    assert sorted(w["name"] for w in db_state["workspaces"]) == ["腾讯控股", "阿里巴巴"]


# =============================================================================
# 2. BD4: Locked recheck reads company knowledge doc tickers
# =============================================================================


@pytest.mark.asyncio
async def test_bd4_locked_recheck_matches_colleague_company_knowledge_doc_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Locked recheck reads company knowledge doc ticker from
    dalton_knowledge_versions so creating '700.HK' when a colleague workspace
    named '腾讯深度研究' has doc_ticker='0700.HK' (no coverage unit) returns False."""
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
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

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
    """BD4: Locked recheck reads doc_ticker for caller's own workspaces (even with
    check_colleague_duplicate=False) and detects doc_ticker drift in candidates."""
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
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

    async with db_state["get_db_connection"]() as conn:
        async with conn.cursor() as cur:
            # Own workspace doc_ticker='0700.HK' matches creating '00700' (even with colleague check off)
            ok_own = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                USER_A,
                "00700",
                check_colleague_duplicate=False,
                conn=conn,
            )
            assert ok_own is False

            # Doc ticker drift: Phase 1 observed doc_ticker=None, now it is '0700.HK'
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
    """BD4: Phase 1 lock-free recheck also includes doc_ticker in checked_candidates
    and deterministic ticker matching when LLM misses the workspace."""
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

    async def _llm_misses(**_kwargs):
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_misses)

    snap = await recheck_company_workspace_before_create(USER_A, "700.HK", channel="cos")
    assert snap.status == "matched"
    assert WS_DOC_ONLY in snap.matched_workspace_ids
    assert any(
        len(c) >= 4 and c[0] == WS_DOC_ONLY and c[3] == "00700"
        for c in snap.checked_candidates
    )


# =============================================================================
# 3. O1 / Q1: Execution history resolves linked thread accessibility in Python
# =============================================================================


def _build_sqlite_execution_history_db() -> sqlite3.Connection:
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
            (WS_COLLEAGUE, USER_B, COMP_A, "running", "{}"),
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
            # Thread initiated by USER_A inside colleague USER_B's workspace
            (THREAD_COLLEAGUE, WS_COLLEAGUE, USER_A),
            # Normal thread in USER_A's own workspace
            (THREAD_OWN, WS_OWN_A, USER_A),
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
    """O1 / Q1: When colleague dispatch is disabled and colleague workspace is
    hidden, the execution history query does NOT link via t.user_id or touch
    Dalton tables in SQL; Python per-page resolution strips both workspace_id
    and conversation_thread_id while keeping the execution row itself."""
    sq = _build_sqlite_execution_history_db()
    get_conn, executed_sql = _sqlite_async_wrapper(sq)

    try:
        # Sharing auth is on, colleague dispatch is OFF, colleague workspace is hidden.
        monkeypatch.setattr(sharing_mod, "_sharing_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "_multiuser_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "is_colleague_dispatch_enabled", lambda: False)
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
        # Upstream SQL query must not reference t.user_id or any dalton_* table.
        first_sql = executed_sql[0]
        assert "t.user_id" not in first_sql
        assert "dalton_" not in first_sql.lower()

        by_id = {r["automation_execution_id"]: r for r in rows}
        # Own execution keeps both workspace_id and conversation_thread_id.
        assert by_id[EXEC_OWN]["workspace_id"] == WS_OWN_A
        assert by_id[EXEC_OWN]["conversation_thread_id"] == THREAD_OWN
        # Inaccessible colleague execution stays in history but strips both workspace_id and thread_id.
        assert EXEC_COLLEAGUE in by_id
        assert by_id[EXEC_COLLEAGUE]["workspace_id"] is None
        assert by_id[EXEC_COLLEAGUE]["conversation_thread_id"] is None
    finally:
        sq.close()


@pytest.mark.asyncio
async def test_o1_q1_execution_history_keeps_accessible_colleague_thread_when_web_read_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1 positive control: When colleague workspace allows web read (ro/rw),
    the execution row retains colleague workspace_id and conversation_thread_id."""
    sq = _build_sqlite_execution_history_db()
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(sharing_mod, "_sharing_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "_multiuser_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "is_colleague_dispatch_enabled", lambda: False)
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
async def test_e2_web_concurrent_create_deterministic_fallback_when_retry_llm_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: Even if follow-up LLM returns empty matches after a locked ticker conflict,
    the deterministic fallback still returns 409 company_workspace_exists with the
    existing workspace instead of creating a duplicate."""
    from src.server.app.workspaces import router as ws_router

    db_state = _make_in_memory_workspace_db()
    _patch_web_create_env(monkeypatch, db_state)

    barrier_count = 0
    both_entered = asyncio.Event()
    orig_collect = dedup_mod._collect_candidates

    async def _synced_collect(*args, **kwargs):
        nonlocal barrier_count
        res = await orig_collect(*args, **kwargs)
        barrier_count += 1
        if barrier_count <= 2:
            if barrier_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    async def _llm_always_empty(**_kwargs):
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "_collect_candidates", _synced_collect)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_always_empty)

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


@pytest.mark.asyncio
async def test_bd4_locked_recheck_matches_explicit_target_ticker_against_doc_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4: Creating a workspace with a non-ticker name ('Tencent Holdings') and
    explicit ticker='0700.HK' matches an existing workspace ('腾讯深度研究') whose
    company knowledge doc carries ticker='00700'."""
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
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

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


@pytest.mark.asyncio
async def test_o1_q1_execution_history_strips_colleague_links_for_external_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1: An external caller never receives colleague workspace_id or
    conversation_thread_id in execution history even if colleague dispatch is on."""
    sq = _build_sqlite_execution_history_db()
    get_conn, _ = _sqlite_async_wrapper(sq)

    try:
        monkeypatch.setattr(sharing_mod, "_sharing_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "_multiuser_auth_enabled", lambda: True)
        monkeypatch.setattr(sharing_mod, "is_colleague_dispatch_enabled", lambda: True)
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

