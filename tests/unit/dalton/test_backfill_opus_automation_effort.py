"""Tests for scripts/dalton/ops/backfill_opus_automation_effort.py (§3.4 simplified fixed-Opus backfill)."""

from __future__ import annotations

import json
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor

from scripts.dalton.ops import backfill_opus_automation_effort as ops

U1 = "user-a"
U2 = "user-b"
A_OPUS_OAUTH = "11111111-1111-4111-8111-111111111111"
A_OPUS_API = "22222222-2222-4222-8222-222222222222"
A_DEFAULT_MODEL = "33333333-3333-4333-8333-333333333333"
A_AG_OPUS = "44444444-4444-4444-8444-444444444444"
A_CUSTOM_OPUS = "55555555-5555-4555-8555-555555555555"
A_DELETED = "66666666-6666-4666-8666-666666666666"
WS = "77777777-7777-4777-8777-777777777777"


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
                    if key == "llm_model = ANY" and params:
                        allowed = set(params[0])
                        buf["rows"] = [
                            dict(r)
                            for r in rows
                            if r.get("deleted_at") is None
                            and r.get("reasoning_effort") is None
                            and r.get("llm_model") in allowed
                        ]
                    else:
                        buf["rows"] = [dict(r) for r in rows]
                    return
            buf["rows"] = []

        async def _fetchall() -> list[dict[str, Any]]:
            return list(buf["rows"])

        async def _fetchone() -> dict[str, Any] | None:
            return buf["rows"][0] if buf["rows"] else None

        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.fetchone.side_effect = _fetchone
        cur.rowcount = 0
        yield cur

    conn.cursor.side_effect = _cursor
    return conn


@pytest.mark.asyncio
async def test_dry_run_selects_only_fixed_builtin_opus_and_excludes_default_antigravity_custom_deleted(
    tmp_path: Path,
) -> None:
    # Uses the REAL model catalog from LLM.get_model_config()
    eligible = ops.eligible_opus_catalog_models()
    assert "claude-opus-5-5" in eligible
    assert "claude-opus-5-5-oauth" in eligible
    assert "claude-opus-5-5-high-oauth" not in eligible
    assert "claude-opus-5-5-medium-oauth" not in eligible
    assert "claude-opus-5-5-low-oauth" not in eligible

    plan_path = tmp_path / "plan.json"
    all_rows = [
        {
            "automation_id": A_OPUS_OAUTH,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Fixed Opus OAuth",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 2,
            "deleted_at": None,
        },
        {
            "automation_id": A_OPUS_API,
            "user_id": U2,
            "workspace_id": WS,
            "name": "Fixed Opus API",
            "llm_model": "claude-opus-5-5",
            "reasoning_effort": None,
            "config_version": 4,
            "deleted_at": None,
        },
        {
            "automation_id": A_DEFAULT_MODEL,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Follow Default",
            "llm_model": None,
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
        },
        {
            "automation_id": A_AG_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Antigravity Opus High",
            "llm_model": "claude-opus-5-5-high-oauth",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
        },
        {
            "automation_id": A_CUSTOM_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Custom Opus",
            "llm_model": "my-custom-opus",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
        },
        {
            "automation_id": A_DELETED,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Deleted Opus",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": "2026-11-20T00:00:00Z",
        },
    ]
    conn = _make_conn(select_rows={"llm_model = ANY": all_rows}, updates=[])
    plan = await ops.run_dry_run(conn=conn, out_path=plan_path)

    assert plan["expected_count"] == 2
    assert stat.S_IMODE(os.stat(plan_path).st_mode) == 0o600
    assert plan["candidates"] == [
        {
            "automation_id": A_OPUS_OAUTH,
            "name": "Fixed Opus OAuth",
            "user_id": U1,
            "llm_model": "claude-opus-5-5-oauth",
            "config_version": 2,
        },
        {
            "automation_id": A_OPUS_API,
            "name": "Fixed Opus API",
            "user_id": U2,
            "llm_model": "claude-opus-5-5",
            "config_version": 4,
        },
    ]


@pytest.mark.asyncio
async def test_apply_writes_medium_records_batch_id_archives_pre_image_and_asserts_rowcount(
    tmp_path: Path, monkeypatch
) -> None:
    archive_dir = tmp_path / "operator-records"
    plan_path = tmp_path / "plan.json"
    plan_payload = {
        "expected_count": 1,
        "candidates": [
            {
                "automation_id": A_OPUS_OAUTH,
                "name": "Fixed Opus OAuth",
                "user_id": U1,
                "llm_model": "claude-opus-5-5-oauth",
                "config_version": 2,
            }
        ],
    }
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, return_value=None),
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

    # Case 1: version drift under lock -> refuses and writes zero UPDATEs
    updates_drift: list[tuple[str, Any]] = []
    conn_drift = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_OPUS_OAUTH,
                    "user_id": U1,
                    "name": "Fixed Opus OAuth",
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 5,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [],
                }
            ]
        },
        updates=updates_drift,
    )
    with pytest.raises(ops.OpsRefusalError, match="config_version changed"):
        await ops.run_apply(
            plan_path=plan_path,
            batch_id="batch-drift",
            archive_dir=archive_dir,
            conn=conn_drift,
        )
    assert updates_drift == []

    # Case 2: rowcount assertion failure -> refuses, archive stays pending (not applied)
    conn_zero = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_OPUS_OAUTH,
                    "user_id": U1,
                    "name": "Fixed Opus OAuth",
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 2,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [],
                }
            ]
        },
        updates=[],
        rowcount_override=0,
    )
    with pytest.raises(ops.OpsRefusalError, match="Rowcount assertion failed"):
        await ops.run_apply(
            plan_path=plan_path,
            batch_id="batch-zero",
            archive_dir=archive_dir,
            conn=conn_zero,
        )
    pending_archive = json.loads(
        (archive_dir / "opus-effort-backfill-batch-zero.json").read_text(encoding="utf-8")
    )
    assert pending_archive["status"] == "pending"

    # Case 3: clean apply -> writes 0600 archive, stamps batch_id in audit_history, asserts rowcount
    updates_ok: list[tuple[str, Any]] = []
    conn_ok = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_OPUS_OAUTH,
                    "user_id": U1,
                    "name": "Fixed Opus OAuth",
                    "llm_model": "claude-opus-5-5-oauth",
                    "reasoning_effort": None,
                    "config_version": 2,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [],
                }
            ]
        },
        updates=updates_ok,
    )
    res = await ops.run_apply(
        plan_path=plan_path,
        batch_id="batch-ok-1",
        archive_dir=archive_dir,
        conn=conn_ok,
    )
    assert res["batch_id"] == "batch-ok-1"
    assert res["updated_count"] == 1
    assert len(updates_ok) == 1
    audit_json_param = updates_ok[0][1][4]
    assert audit_json_param.obj[-1]["action"] == ops.BACKFILL_ACTION
    assert audit_json_param.obj[-1]["batch_id"] == "batch-ok-1"

    archive_file = Path(res["archive_path"])
    assert stat.S_IMODE(os.stat(archive_file).st_mode) == 0o600
    saved = json.loads(archive_file.read_text(encoding="utf-8"))
    assert saved["status"] == "applied"
    assert saved["batch_id"] == "batch-ok-1"
    assert saved["rows"][0]["before_reasoning_effort"] is None
    assert saved["rows"][0]["after_config_version"] == 3


@pytest.mark.asyncio
async def test_rollback_refuses_pending_archive_and_verifies_batch_audit_entry_under_lock(
    tmp_path: Path, monkeypatch
) -> None:
    archive_dir = tmp_path / "operator-records"

    # 1. Pending archive (from an aborted apply where user themselves set medium at v3) MUST be refused
    pending_batch = "batch-aborted-pending"
    ops._write_secure_json(
        ops.archive_path_for_batch(pending_batch, archive_dir),
        {
            "batch_id": pending_batch,
            "status": "pending",
            "rows": [
                {
                    "automation_id": A_OPUS_OAUTH,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                }
            ],
        },
    )
    with pytest.raises(ops.OpsRefusalError, match="not 'applied'"):
        await ops.run_rollback(batch_or_path=pending_batch, archive_dir=archive_dir)

    # 2. Applied archive: verifies batch_id & action in locked audit_history;
    #    reverts untouched row from this batch, skips user-edited row and row whose v3 was user-authored!
    applied_batch = "batch-applied-42"
    a_reverted = A_OPUS_OAUTH
    a_user_edited_ver = A_OPUS_API
    a_user_authored_same_ver = A_CUSTOM_OPUS

    ops._write_secure_json(
        ops.archive_path_for_batch(applied_batch, archive_dir),
        {
            "batch_id": applied_batch,
            "status": "applied",
            "rows": [
                {
                    "automation_id": a_reverted,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                },
                {
                    "automation_id": a_user_edited_ver,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                },
                {
                    "automation_id": a_user_authored_same_ver,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                },
            ],
        },
    )

    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, return_value=None),
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
                    "automation_id": a_reverted,
                    "user_id": U1,
                    "reasoning_effort": "medium",
                    "config_version": 3,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {
                            "version": 3,
                            "action": ops.BACKFILL_ACTION,
                            "batch_id": applied_batch,
                        }
                    ],
                },
                {
                    "automation_id": a_user_edited_ver,
                    "user_id": U1,
                    "reasoning_effort": "high",
                    "config_version": 4,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {
                            "version": 3,
                            "action": ops.BACKFILL_ACTION,
                            "batch_id": applied_batch,
                        },
                        {"version": 4, "action": "update"},
                    ],
                },
                {
                    # Same version 3 and effort='medium', but written by user (no matching batch_id!)
                    "automation_id": a_user_authored_same_ver,
                    "user_id": U1,
                    "reasoning_effort": "medium",
                    "config_version": 3,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {"version": 3, "action": "update"},
                    ],
                },
            ]
        },
        updates=updates,
    )

    res = await ops.run_rollback(
        batch_or_path=applied_batch,
        archive_dir=archive_dir,
        conn=conn,
    )
    assert res["reverted"] == [a_reverted]
    skipped_by_id = {item["automation_id"]: item["reason"] for item in res["skipped"]}
    assert a_user_edited_ver in skipped_by_id
    assert skipped_by_id[a_user_authored_same_ver] == "missing_matching_batch_audit_entry"
    assert len(updates) == 1
    assert updates[0][1][0] is None  # reverted reasoning_effort back to NULL
