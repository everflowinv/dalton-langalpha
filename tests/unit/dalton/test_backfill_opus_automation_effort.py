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
U_SHADOW = "user-shadow"
A_OPUS_OAUTH = "11111111-1111-4111-8111-111111111111"
A_OPUS_API = "22222222-2222-4222-8222-222222222222"
A_DEFAULT_MODEL = "33333333-3333-4333-8333-333333333333"
A_AG_OPUS = "44444444-4444-4444-8444-444444444444"
A_CODE_OPUS = "55555555-5555-4555-8555-555555555555"
A_SHADOW_OPUS = "66666666-6666-4666-8666-666666666666"
A_DELETED = "77777777-7777-4777-8777-777777777777"
A_PRIOR_CLEARED = "88888888-8888-4888-8888-888888888888"
A_ROLLED_BACK = "99999999-9999-4999-8999-999999999999"
WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
                    elif key == "FROM user_preferences WHERE user_id = %s" and params:
                        target_uid = params[0]
                        buf["rows"] = [dict(r) for r in rows if r.get("user_id") == target_uid]
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
async def test_dry_run_and_apply_strictly_limit_to_opus_oauth_and_api_and_exclude_custom_shadow(
    tmp_path: Path, monkeypatch
) -> None:
    """Only claude-opus-5-5-oauth and claude-opus-5-5 qualify (not -code, not Antigravity);
    same-named custom_models entries are excluded identically in dry-run and locked apply."""
    eligible = ops.eligible_opus_catalog_models()
    assert eligible == {"claude-opus-5-5", "claude-opus-5-5-oauth"}
    assert "claude-opus-5-5-code" not in eligible
    assert "claude-opus-5-5-high-oauth" not in eligible

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
            "audit_history": [],
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
            "audit_history": [],
        },
        {
            "automation_id": A_CODE_OPUS,
            "user_id": U1,
            "workspace_id": WS,
            "name": "Claude Code Opus",
            "llm_model": "claude-opus-5-5-code",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
            "audit_history": [],
        },
        {
            "automation_id": A_SHADOW_OPUS,
            "user_id": U_SHADOW,
            "workspace_id": WS,
            "name": "Shadowed Custom Opus",
            "llm_model": "claude-opus-5-5",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
            "audit_history": [],
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
            "audit_history": [],
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
            "audit_history": [],
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
            "audit_history": [],
        },
    ]
    pref_rows = [
        {"user_id": U1, "model_preference": {}, "other_preference": {}},
        {"user_id": U2, "model_preference": {}, "other_preference": {}},
        {
            "user_id": U_SHADOW,
            "model_preference": {
                "custom_models": [{"name": "claude-opus-5-5", "provider": "anthropic"}]
            },
            "other_preference": {},
        },
    ]

    conn = _make_conn(
        select_rows={
            "llm_model = ANY": all_rows,
            "FROM user_preferences WHERE user_id = ANY": pref_rows,
        },
        updates=[],
    )
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
    skipped_by_id = {s["automation_id"]: s["reason"] for s in plan["skipped"]}
    assert skipped_by_id == {A_SHADOW_OPUS: "shadowed_by_custom_model:'claude-opus-5-5'"}

    # Locked apply ALSO refuses if a user added a same-named custom model after dry-run
    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, return_value=None),
    )
    updates_shadow: list[tuple[str, Any]] = []
    conn_shadow_apply = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_OPUS_API,
                    "user_id": U2,
                    "name": "Fixed Opus API",
                    "llm_model": "claude-opus-5-5",
                    "reasoning_effort": None,
                    "config_version": 4,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [],
                }
            ],
            "FROM user_preferences WHERE user_id = %s": [
                {
                    "user_id": U2,
                    "model_preference": {
                        "custom_models": [{"name": "claude-opus-5-5", "provider": "anthropic"}]
                    },
                    "other_preference": {},
                }
            ],
        },
        updates=updates_shadow,
    )
    with pytest.raises(ops.OpsRefusalError, match="shadowed_by_custom_model"):
        await ops.run_apply(
            plan_data={
                "expected_count": 1,
                "candidates": [
                    {
                        "automation_id": A_OPUS_API,
                        "name": "Fixed Opus API",
                        "user_id": U2,
                        "llm_model": "claude-opus-5-5",
                        "config_version": 4,
                    }
                ],
            },
            batch_id="batch-shadow-drift",
            archive_dir=tmp_path / "records",
            conn=conn_shadow_apply,
        )
    assert updates_shadow == []


@pytest.mark.asyncio
async def test_dry_run_and_apply_exclude_previously_backfilled_then_user_cleared_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-running backfill must never overwrite tasks previously backfilled and cleared by users,
    while tasks rolled back by ops remain eligible."""
    rows = [
        {
            "automation_id": A_PRIOR_CLEARED,
            "user_id": U1,
            "name": "User Cleared After Backfill",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 3,
            "deleted_at": None,
            "notify_storage_mode": "task_row",
            "audit_history": [
                {"version": 2, "action": ops.BACKFILL_ACTION, "batch_id": "batch-old-1"},
                {"version": 3, "action": "update"},
            ],
        },
        {
            "automation_id": A_ROLLED_BACK,
            "user_id": U1,
            "name": "Ops Rolled Back Task",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 3,
            "deleted_at": None,
            "notify_storage_mode": "task_row",
            "audit_history": [
                {"version": 2, "action": ops.BACKFILL_ACTION, "batch_id": "batch-old-1"},
                {"version": 3, "action": ops.ROLLBACK_ACTION, "rollback_of_batch_id": "batch-old-1"},
            ],
        },
    ]

    conn_dry = _make_conn(
        select_rows={
            "llm_model = ANY": rows,
            "FROM user_preferences WHERE user_id = ANY": [],
        },
        updates=[],
    )
    plan = await ops.run_dry_run(conn=conn_dry)
    assert [c["automation_id"] for c in plan["candidates"]] == [A_ROLLED_BACK]
    assert {s["automation_id"]: s["reason"] for s in plan["skipped"]} == {
        A_PRIOR_CLEARED: "previously_backfilled"
    }

    # Even if a stale plan includes A_PRIOR_CLEARED, locked apply refuses it
    monkeypatch.setattr(
        ops,
        "lock_user_preferences",
        create_autospec(ops.lock_user_preferences, return_value=None),
    )
    updates: list[tuple[str, Any]] = []
    conn_apply = _make_conn(
        select_rows={
            "FOR UPDATE": [rows[0]],
            "FROM user_preferences WHERE user_id = %s": [],
        },
        updates=updates,
    )
    with pytest.raises(ops.OpsRefusalError, match="previously_backfilled"):
        await ops.run_apply(
            plan_data={
                "expected_count": 1,
                "candidates": [
                    {
                        "automation_id": A_PRIOR_CLEARED,
                        "name": "User Cleared After Backfill",
                        "user_id": U1,
                        "llm_model": "claude-opus-5-5-oauth",
                        "config_version": 3,
                    }
                ],
            },
            batch_id="batch-rerun",
            archive_dir=tmp_path / "records",
            conn=conn_apply,
        )
    assert updates == []


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
            ],
            "FROM user_preferences WHERE user_id = %s": [],
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

    # Case 2: rowcount assertion failure -> refuses, archive stays pending
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
            ],
            "FROM user_preferences WHERE user_id = %s": [],
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
            ],
            "FROM user_preferences WHERE user_id = %s": [],
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
async def test_rollback_accepts_pending_archive_after_commit_crash_and_skips_uncommitted_or_user_edited(
    tmp_path: Path, monkeypatch
) -> None:
    """A pending archive (process exited after DB commit before flipping archive status) can
    still be rolled back by verifying batch_id/action/version/medium under FOR UPDATE lock;
    aborted rows where the user set medium themselves (no matching batch audit) are skipped."""
    archive_dir = tmp_path / "operator-records"
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

    # Case 1: DB committed, process exited while archive stayed 'pending':
    #   - A_OPUS_OAUTH has matching batch_id audit at v3 & medium -> REVERTED
    #   - A_OPUS_API was user-authored medium at v3 (no matching batch_id) -> SKIPPED
    #   - A_CODE_OPUS was later edited by user to v4 -> SKIPPED
    pending_batch = "batch-crash-after-commit"
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
                },
                {
                    "automation_id": A_OPUS_API,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                },
                {
                    "automation_id": A_CODE_OPUS,
                    "user_id": U1,
                    "before_reasoning_effort": None,
                    "before_config_version": 2,
                    "after_config_version": 3,
                },
            ],
        },
    )

    updates: list[tuple[str, Any]] = []
    conn = _make_conn(
        select_rows={
            "FOR UPDATE": [
                {
                    "automation_id": A_OPUS_OAUTH,
                    "user_id": U1,
                    "reasoning_effort": "medium",
                    "config_version": 3,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {
                            "version": 3,
                            "action": ops.BACKFILL_ACTION,
                            "batch_id": pending_batch,
                        }
                    ],
                },
                {
                    # Same version 3 and effort='medium', but written by user (no matching batch_id!)
                    "automation_id": A_OPUS_API,
                    "user_id": U1,
                    "reasoning_effort": "medium",
                    "config_version": 3,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {"version": 3, "action": "update"},
                    ],
                },
                {
                    "automation_id": A_CODE_OPUS,
                    "user_id": U1,
                    "reasoning_effort": "high",
                    "config_version": 4,
                    "deleted_at": None,
                    "notify_storage_mode": "task_row",
                    "audit_history": [
                        {
                            "version": 3,
                            "action": ops.BACKFILL_ACTION,
                            "batch_id": pending_batch,
                        },
                        {"version": 4, "action": "update"},
                    ],
                },
            ],
            "FROM user_preferences WHERE user_id = %s": [],
        },
        updates=updates,
    )

    res = await ops.run_rollback(
        batch_or_path=pending_batch,
        archive_dir=archive_dir,
        conn=conn,
    )
    assert res["archive_status"] == "pending"
    assert res["reverted"] == [A_OPUS_OAUTH]
    skipped_by_id = {item["automation_id"]: item["reason"] for item in res["skipped"]}
    assert skipped_by_id[A_OPUS_API] == "missing_matching_batch_audit_entry"
    assert skipped_by_id[A_CODE_OPUS].startswith("version_changed:")
    assert len(updates) == 1
    assert updates[0][1][0] is None  # reverted reasoning_effort back to NULL


@pytest.mark.asyncio
async def test_company_system_user_and_missing_pref_user_never_fail_online_policy_gating(
    tmp_path: Path, monkeypatch
) -> None:
    """Simplified backfill never calls online require_policy/own_access, so company system
    accounts (system:dalton-company) and suspended/no-pref users process cleanly without error."""
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID

    archive_dir = tmp_path / "operator-records"
    rows = [
        {
            "automation_id": A_OPUS_OAUTH,
            "user_id": COMPANY_SYSTEM_USER_ID,
            "name": "Company Opus Task",
            "llm_model": "claude-opus-5-5-oauth",
            "reasoning_effort": None,
            "config_version": 1,
            "deleted_at": None,
            "notify_storage_mode": "task_row",
            "audit_history": [],
        },
        {
            "automation_id": A_OPUS_API,
            "user_id": "suspended-member-99",
            "name": "Suspended Member Opus Task",
            "llm_model": "claude-opus-5-5",
            "reasoning_effort": None,
            "config_version": 2,
            "deleted_at": None,
            "notify_storage_mode": "legacy",
            "audit_history": [],
        },
    ]

    dry_conn = _make_conn(
        select_rows={
            "llm_model = ANY": rows,
            "FROM user_preferences WHERE user_id = ANY": [],
        },
        updates=[],
    )
    plan = await ops.run_dry_run(conn=dry_conn)
    assert plan["expected_count"] == 2
    assert {c["user_id"] for c in plan["candidates"]} == {
        COMPANY_SYSTEM_USER_ID,
        "suspended-member-99",
    }

    locked_uids: list[str] = []

    async def _lock_pref(uid: str, *, conn: Any = None):
        locked_uids.append(uid)

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
            side_effect=lambda row, *_a, **_kw: (
                {
                    "config_version": int(row["config_version"]) + 1,
                    "active_version": int(row["config_version"]) + 1,
                    "updated_by": ops.OPS_ACTOR_USER_ID,
                    "audit_history": [
                        {"version": int(row["config_version"]) + 1, "action": ops.BACKFILL_ACTION}
                    ],
                },
                False,
            ),
        ),
    )

    updates: list[tuple[str, Any]] = []
    apply_conn = _make_conn(
        select_rows={
            "FOR UPDATE": rows,
            "FROM user_preferences WHERE user_id = %s": [],
        },
        updates=updates,
    )
    res = await ops.run_apply(
        plan_data=plan,
        batch_id="batch-company-and-suspended",
        archive_dir=archive_dir,
        conn=apply_conn,
    )
    assert res["updated_count"] == 2
    assert locked_uids == ["suspended-member-99"]
