"""Tests for Dalton migration d042 (automations.reasoning_effort) and downgrade/stale helpers."""

from __future__ import annotations

import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor

from scripts.dalton.ops import backfill_opus_automation_effort as ops_mod
from src.dalton.migrations.versions import d042_automation_reasoning_effort as d042
from src.llms.reasoning import REASONING_LEVELS


def test_d042_revision_chain_and_upgrade_downgrade_sql(monkeypatch) -> None:
    assert d042.revision == "d042"
    assert d042.down_revision == "d041"

    executed_up: list[str] = []
    op_mock = MagicMock()
    op_mock.execute.side_effect = lambda sql: executed_up.append(str(sql))
    monkeypatch.setattr(d042, "op", op_mock)

    d042.upgrade()
    assert any("SET LOCAL lock_timeout = '5s'" in s for s in executed_up)
    alter_up = "\n".join(executed_up)
    assert "ALTER TABLE automations" in alter_up
    assert "ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)" in alter_up
    assert "ck_automations_reasoning_effort" in alter_up
    for level in REASONING_LEVELS:
        assert f"'{level}'" in alter_up

    executed_down: list[str] = []
    op_mock.execute.side_effect = lambda sql: executed_down.append(str(sql))
    d042.downgrade()
    assert any("SET LOCAL lock_timeout = '5s'" in s for s in executed_down)
    assert any(
        "ALTER TABLE automations DROP COLUMN IF EXISTS reasoning_effort" in s
        for s in executed_down
    )


def _fake_db_conn(rows_by_query: dict[str, Any], updates: list[tuple[str, Any]]):
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        yield conn

    conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _cursor(*_args: Any, **_kwargs: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        state: dict[str, Any] = {"rows": []}

        async def _execute(sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            if norm.startswith("UPDATE "):
                updates.append((norm, params))
                cur.rowcount = 1
                state["rows"] = []
                return
            for key, value in rows_by_query.items():
                if key in norm:
                    state["rows"] = list(value)
                    return
            state["rows"] = []

        async def _fetchall() -> list[Any]:
            return list(state["rows"])

        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.rowcount = 0
        yield cur

    conn.cursor.side_effect = _cursor
    return conn


@pytest.mark.asyncio
async def test_d042_downgrade_export_and_restore_skips_edited_rows(tmp_path: Path) -> None:
    export_path = tmp_path / "export.json"
    a1 = "11111111-1111-4111-8111-111111111111"
    a2 = "22222222-2222-4222-8222-222222222222"

    export_conn = _fake_db_conn(
        {
            "WHERE reasoning_effort IS NOT NULL": [
                {
                    "automation_id": a1,
                    "user_id": "u1",
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": "high",
                    "config_version": 3,
                    "active_version": 3,
                },
                {
                    "automation_id": a2,
                    "user_id": "u1",
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": "medium",
                    "config_version": 2,
                    "active_version": 2,
                },
            ]
        },
        [],
    )
    rc, exported = await ops_mod.run_export_downgrade(out_path=export_path, conn=export_conn)
    assert rc == 0
    assert exported["count"] == 2
    assert stat.S_IMODE(os.stat(export_path).st_mode) == 0o600

    # After re-upgrade: a1 is untouched (v3, same model, NULL effort) -> restored;
    # a2 was edited during downgrade (v4, different model) -> skipped.
    updates: list[tuple[str, Any]] = []
    restore_conn = _fake_db_conn(
        {
            "FOR UPDATE": [
                {
                    "automation_id": a1,
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 3,
                },
                {
                    "automation_id": a2,
                    "llm_model": "gemini-4-neon-high-oauth",
                    "reasoning_effort": None,
                    "config_version": 4,
                },
            ]
        },
        updates,
    )
    rc_restore, restored = await ops_mod.run_restore_downgrade(
        export_path=export_path, conn=restore_conn
    )
    assert rc_restore == 0
    assert restored["restored_ids"] == [a1]
    assert len(restored["skipped"]) == 1
    assert restored["skipped"][0]["automation_id"] == a2
    assert len(updates) == 1
    assert updates[0][1] == ("high", a1, 3)


@pytest.mark.asyncio
async def test_d042_reconcile_stale_clears_effort_after_legacy_model_edit(monkeypatch) -> None:
    a_stale_ag = "11111111-1111-4111-8111-111111111111"
    a_stale_changed = "22222222-2222-4222-8222-222222222222"
    a_fresh = "33333333-3333-4333-8333-333333333333"

    rows = [
        {
            "automation_id": a_stale_ag,
            "user_id": "u1",
            "name": "Switched to Antigravity on old code",
            "llm_model": "gemini-4-neon-high-oauth",
            "reasoning_effort": "high",
            "config_version": 2,
            "active_version": 2,
            "audit_history": [
                {"version": 1, "snapshot": {"llm_model": "claude-opus-5-5-oauth", "reasoning_effort": "high"}},
                {"version": 2, "snapshot": {"llm_model": "gemini-4-neon-high-oauth"}},
            ],
        },
        {
            "automation_id": a_stale_changed,
            "user_id": "u1",
            "name": "Changed model on old code",
            "llm_model": "gpt-6.2-sol-oauth",
            "reasoning_effort": "max",
            "config_version": 2,
            "active_version": 2,
            "audit_history": [
                {"version": 1, "snapshot": {"llm_model": "claude-opus-5-5-oauth", "reasoning_effort": "max"}},
                {"version": 2, "snapshot": {"llm_model": "gpt-6.2-sol-oauth"}},
            ],
        },
        {
            "automation_id": a_fresh,
            "user_id": "u1",
            "name": "Current code edit",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": "high",
            "config_version": 2,
            "active_version": 2,
            "audit_history": [
                {"version": 2, "snapshot": {"llm_model": "claude-opus-5-5-oauth", "reasoning_effort": "high"}},
            ],
        },
    ]

    updates: list[tuple[str, Any]] = []
    conn = _fake_db_conn(
        {
            "WHERE reasoning_effort IS NOT NULL": rows,
            "FROM user_preferences": [],
        },
        updates,
    )
    monkeypatch.setattr(
        ops_mod,
        "lock_user_preferences",
        create_autospec(ops_mod.lock_user_preferences, return_value={"model_preference": {}}),
    )
    monkeypatch.setattr(
        ops_mod,
        "prepare_update_history_columns",
        create_autospec(
            ops_mod.prepare_update_history_columns,
            return_value=(
                {
                    "config_version": 3,
                    "active_version": 3,
                    "updated_by": ops_mod.OPS_ACTOR_USER_ID,
                    "audit_history": [{"version": 3, "action": ops_mod.RECONCILE_STALE_ACTION}],
                },
                False,
            ),
        ),
    )

    rc, res = await ops_mod.run_reconcile_stale(apply=True, conn=conn)
    assert rc == 0
    assert res["stale_ids"] == [a_stale_ag, a_stale_changed]
    assert res["cleared_ids"] == [a_stale_ag, a_stale_changed]
    assert len(updates) == 2
