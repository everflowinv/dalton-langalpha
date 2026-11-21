"""Tests for scripts/dalton/ops/backfill_opus_automation_effort.py (§3.4)."""

from __future__ import annotations

import json
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor

from scripts.dalton.ops import backfill_opus_automation_effort as ops
from src.server.services.llm.user_models import ModelSource

U1 = "user-a"
U2 = "user-b"
A_EXPLICIT = "11111111-1111-4111-8111-111111111111"
A_DEFAULT_OPUS = "22222222-2222-4222-8222-222222222222"
A_AG_OPUS = "33333333-3333-4333-8333-333333333333"
A_CUSTOM_OPUS = "44444444-4444-4444-8444-444444444444"
A_CONFLICT = "55555555-5555-4555-8555-555555555555"
A_PRIOR = "66666666-6666-4666-8666-666666666666"
WS = "77777777-7777-4777-8777-777777777777"

_OPUS_ENTRY = {
    "provider": "claude-oauth",
    "model_id": "claude-opus-5-5",
    "reasoning": {"efforts": ["low", "medium", "high", "max"], "default": "medium"},
}
_AG_OPUS_ENTRY = {
    "provider": "antigravity-oauth",
    "model_id": "claude-opus-5-5-high",
    "reasoning": {"efforts": ["high"], "default": "high"},
}


def _make_conn(
    *,
    select_rows: dict[str, list[dict[str, Any]]],
    updates: list[tuple[str, Any]],
    rowcount_override: int = 1,
):
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        yield conn

    conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        buf: dict[str, Any] = {"rows": []}

        async def _execute(sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            if norm.startswith("UPDATE "):
                updates.append((norm, params))
                cur.rowcount = rowcount_override
                buf["rows"] = []
                return
            for key, rows in select_rows.items():
                if key in norm:
                    buf["rows"] = list(rows)
                    return
            buf["rows"] = []

        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = lambda: list(buf["rows"])
        cur.rowcount = 0
        yield cur

    conn.cursor.side_effect = _cursor
    return conn


@pytest.mark.asyncio
async def test_dry_run_resolves_real_effective_model_excludes_antigravity_and_custom_and_flags_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    plan_path = tmp_path / "plan.json"

    async def _fake_resolve(
        *, base_config: Any, user_id: str, workspace_id: str | None, row_llm_model: str | None, raw_pref: dict[str, Any]
    ):
        if row_llm_model == "claude-opus-5-5-high-oauth":
            return row_llm_model, ModelSource.SYSTEM, _AG_OPUS_ENTRY, "high"
        if row_llm_model == "custom-opus":
            return row_llm_model, ModelSource.CUSTOM, _OPUS_ENTRY, "medium"
        if user_id == U2:
            return "claude-opus-5-5-oauth", ModelSource.SYSTEM, _OPUS_ENTRY, "high"
        return "claude-opus-5-5-oauth", ModelSource.SYSTEM, _OPUS_ENTRY, "medium"

    monkeypatch.setattr(
        ops,
        "resolve_effective_model_and_effort",
        create_autospec(ops.resolve_effective_model_and_effort, side_effect=_fake_resolve),
    )

    rows = [
        {
            "automation_id": A_EXPLICIT,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Explicit Opus",
            "scope": "personal",
            "status": "active",
            "deleted_at": None,
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 2,
            "active_version": 2,
            "audit_history": [],
        },
        {
            "automation_id": A_DEFAULT_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Default Opus",
            "scope": "personal",
            "status": "paused",
            "deleted_at": None,
            "llm_model": None,
            "reasoning_effort": None,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
        },
        {
            "automation_id": A_AG_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Antigravity Opus",
            "scope": "personal",
            "status": "active",
            "deleted_at": None,
            "llm_model": "claude-opus-5-5-high-oauth",
            "reasoning_effort": None,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
        },
        {
            "automation_id": A_CUSTOM_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Custom Opus",
            "scope": "personal",
            "status": "active",
            "deleted_at": None,
            "llm_model": "custom-opus",
            "reasoning_effort": None,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
        },
        {
            "automation_id": A_CONFLICT,
            "user_id": U2,
            "workspace_id": WS,
            "name": "High Pref Conflict",
            "scope": "personal",
            "status": "active",
            "deleted_at": None,
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
        },
        {
            "automation_id": A_PRIOR,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Cleared After Backfill",
            "scope": "personal",
            "status": "active",
            "deleted_at": None,
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 3,
            "active_version": 3,
            "audit_history": [{"version": 2, "action": ops.BACKFILL_ACTION}],
        },
    ]

    conn = _make_conn(
        select_rows={
            "WHERE reasoning_effort IS NULL": rows,
            "FROM user_preferences": [
                {"user_id": U1, "model_preference": {"preferred_model": "claude-opus-5-5-oauth"}},
                {
                    "user_id": U2,
                    "model_preference": {
                        "preferred_model": "claude-opus-5-5-oauth",
                        "reasoning_effort": "high",
                    },
                },
            ],
        },
        updates=[],
    )

    rc, plan = await ops.run_dry_run(
        out_path=plan_path,
        sentinel_path=tmp_path / "sentinel.json",
        conn=conn,
        base_config=SimpleNamespace(),
    )
    assert rc == 0
    assert stat.S_IMODE(os.stat(plan_path).st_mode) == 0o600
    ids = [e["automation_id"] for e in plan["entries"]]
    assert ids == [A_EXPLICIT, A_DEFAULT_OPUS, A_CONFLICT, A_PRIOR]
    assert A_AG_OPUS not in ids
    assert A_CUSTOM_OPUS not in ids
    by_id = {e["automation_id"]: e for e in plan["entries"]}
    assert by_id[A_CONFLICT]["has_effort_conflict"] is True
    assert by_id[A_CONFLICT]["current_effective_effort"] == "high"
    assert by_id[A_PRIOR]["previously_backfilled"] is True


@pytest.mark.asyncio
async def test_apply_refuses_unapproved_conflict_sentinel_and_prior_backfill_with_exit_2(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.json"
    plan_path = tmp_path / "plan.json"

    # 1. Sentinel already exists -> exit 2
    sentinel.write_text("{}", encoding="utf-8")
    plan_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    rc, res = await ops.run_apply(plan_path=plan_path, sentinel_path=sentinel)
    assert rc == 2 and res["error"] == "sentinel_exists"
    sentinel.unlink()

    # 2. Previously backfilled entry -> exit 2
    plan_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "automation_id": A_PRIOR,
                        "user_id": U1,
                        "llm_model": "claude-opus-5-5-oauth",
                        "effective_model": "claude-opus-5-5-oauth",
                        "config_version": 3,
                        "active_version": 3,
                        "model_preference_digest": "abc",
                        "has_effort_conflict": False,
                        "previously_backfilled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc2, res2 = await ops.run_apply(plan_path=plan_path, sentinel_path=sentinel)
    assert rc2 == 2 and res2["error"] == "previously_backfilled"

    # 3. Unapproved effort conflict -> exit 2
    plan_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "automation_id": A_CONFLICT,
                        "user_id": U2,
                        "llm_model": "claude-opus-5-5-oauth",
                        "effective_model": "claude-opus-5-5-oauth",
                        "config_version": 1,
                        "active_version": 1,
                        "model_preference_digest": "abc",
                        "has_effort_conflict": True,
                        "previously_backfilled": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc3, res3 = await ops.run_apply(plan_path=plan_path, sentinel_path=sentinel)
    assert rc3 == 2 and res3["error"] == "unapproved_effort_conflicts"


@pytest.mark.asyncio
async def test_apply_locked_recheck_asserts_rowcount_writes_archive_and_aborts_on_drift(
    tmp_path: Path, monkeypatch
) -> None:
    pref = {"preferred_model": "claude-opus-5-5-oauth"}
    digest = ops._model_pref_digest(pref)
    plan_path = tmp_path / "plan.json"
    archive_path = tmp_path / "archive.json"
    sentinel_path = tmp_path / "sentinel.json"

    plan_payload = {
        "entries": [
            {
                "automation_id": A_EXPLICIT,
                "user_id": U1,
                "workspace_id": WS,
                "llm_model": "claude-opus-5-5-oauth",
                "effective_model": "claude-opus-5-5-oauth",
                "config_version": 2,
                "active_version": 2,
                "model_preference_digest": digest,
                "current_effective_effort": "medium",
                "has_effort_conflict": False,
                "previously_backfilled": False,
            }
        ]
    }
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    monkeypatch.setattr(
        ops,
        "resolve_effective_model_and_effort",
        create_autospec(
            ops.resolve_effective_model_and_effort,
            return_value=("claude-opus-5-5-oauth", ModelSource.SYSTEM, _OPUS_ENTRY, "medium"),
        ),
    )
    locked_users: list[str] = []

    async def _lock_pref(uid: str, *, conn: Any):
        locked_users.append(uid)
        return {"user_id": uid, "model_preference": pref}

    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, side_effect=_lock_pref),
    )
    monkeypatch.setattr(
        ops,
        "prepare_update_history_columns",
        create_autospec(
            ops.prepare_update_history_columns,
            return_value=(
                {
                    "config_version": 3,
                    "active_version": 3,
                    "updated_by": ops.OPS_ACTOR_USER_ID,
                    "audit_history": [{"version": 3, "action": ops.BACKFILL_ACTION}],
                },
                False,
            ),
        ),
    )

    # Case A: config_version drifted under lock -> exit 2, zero updates, no sentinel
    updates_drift: list[tuple[str, Any]] = []
    conn_drift = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_EXPLICIT,
                    "user_id": U1,
                    "workspace_id": WS,
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 99,  # drifted!
                    "active_version": 99,
                    "audit_history": [],
                }
            ]
        },
        updates=updates_drift,
    )
    rc_drift, res_drift = await ops.run_apply(
        plan_path=plan_path,
        archive_path=archive_path,
        sentinel_path=sentinel_path,
        conn=conn_drift,
        base_config=SimpleNamespace(),
    )
    assert rc_drift == 2
    assert res_drift["error"] == "locked_recheck_failed"
    assert updates_drift == []
    assert not sentinel_path.exists()

    # Case B: rowcount assertion failure (0 rows affected) -> exit 2, no sentinel
    conn_zero = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_EXPLICIT,
                    "user_id": U1,
                    "workspace_id": WS,
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 2,
                    "active_version": 2,
                    "audit_history": [],
                }
            ]
        },
        updates=[],
        rowcount_override=0,
    )
    rc_zero, res_zero = await ops.run_apply(
        plan_path=plan_path,
        archive_path=archive_path,
        sentinel_path=sentinel_path,
        conn=conn_zero,
        base_config=SimpleNamespace(),
    )
    assert rc_zero == 2
    assert "expected 1 updated row, got 0" in res_zero["message"]
    assert not sentinel_path.exists()

    # Case C: clean apply -> writes archive (0600) + sentinel (0600) + updates 1 row
    updates_ok: list[tuple[str, Any]] = []
    conn_ok = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_EXPLICIT,
                    "user_id": U1,
                    "workspace_id": WS,
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 2,
                    "active_version": 2,
                    "audit_history": [],
                }
            ]
        },
        updates=updates_ok,
    )
    rc_ok, res_ok = await ops.run_apply(
        plan_path=plan_path,
        archive_path=archive_path,
        sentinel_path=sentinel_path,
        conn=conn_ok,
        base_config=SimpleNamespace(),
    )
    assert rc_ok == 0
    assert res_ok["applied_count"] == 1
    assert len(updates_ok) == 1
    assert stat.S_IMODE(os.stat(archive_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(sentinel_path).st_mode) == 0o600
    saved_archive = json.loads(archive_path.read_text(encoding="utf-8"))
    assert saved_archive["status"] == "applied"
    assert saved_archive["entries"][0]["post_config_version"] == 3


@pytest.mark.asyncio
async def test_rollback_reverts_unmodified_and_skips_user_edited(
    tmp_path: Path, monkeypatch
) -> None:
    archive_path = tmp_path / "archive.json"
    archive_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "automation_id": A_EXPLICIT,
                        "user_id": U1,
                        "post_config_version": 3,
                    },
                    {
                        "automation_id": A_DEFAULT_OPUS,
                        "user_id": U1,
                        "post_config_version": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, return_value={"model_preference": {}}),
    )
    monkeypatch.setattr(
        ops,
        "prepare_update_history_columns",
        create_autospec(
            ops.prepare_update_history_columns,
            return_value=(
                {
                    "config_version": 4,
                    "active_version": 4,
                    "updated_by": ops.OPS_ACTOR_USER_ID,
                    "audit_history": [{"version": 4, "action": ops.ROLLBACK_ACTION}],
                },
                False,
            ),
        ),
    )

    updates: list[tuple[str, Any]] = []
    conn = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_EXPLICIT,
                    "user_id": U1,
                    "reasoning_effort": "medium",
                    "config_version": 3,
                    "active_version": 3,
                },
                {
                    "automation_id": A_DEFAULT_OPUS,
                    "user_id": U1,
                    "reasoning_effort": "high",  # user modified after backfill!
                    "config_version": 3,
                    "active_version": 3,
                },
            ]
        },
        updates=updates,
    )

    rc, res = await ops.run_rollback(archive_path=archive_path, conn=conn)
    assert rc == 0
    assert res["reverted_ids"] == [A_EXPLICIT]
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["automation_id"] == A_DEFAULT_OPUS
    assert len(updates) == 1
