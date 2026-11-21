"""Unit tests for scripts/dalton/ops/merge_duplicate_company_workspace.py.

Uses ``unittest.mock.create_autospec`` exclusively for all database doubles;
no real Postgres connection is made.
"""

from __future__ import annotations

import copy
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from scripts.dalton.ops import merge_duplicate_company_workspace as mod

DUP_WS = "0c323f71-4b40-4772-9c26-a7625f11292b"
CANON_WS = "7526b801-244b-4b63-816f-7931476722d5"
CY_USER = "a051e463-11a3-4587-8b54-0cb76ae10124"
RICHARD_USER = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
UNIT_ID = "fdac06cb-6ef3-4191-bdc9-ab6f8917acb4"


def _make_workspace(
    ws_id: str,
    *,
    user_id: str,
    name: str,
    status: str = "stopped",
    config: dict[str, Any] | None = None,
    null_config: bool = False,
) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "user_id": user_id,
        "name": name,
        "description": None,
        "sandbox_id": "sbx-1",
        "computer_id": "comp-1",
        "dir_name": name,
        "status": status,
        "config": None if null_config else (config if config is not None else {}),
        "is_pinned": False,
        "sort_order": 0,
        "files_restore_incomplete_at": None,
        "files_restore_offered_at": None,
        "files_epoch": "11111111-1111-1111-1111-111111111111",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-11-20T00:00:00+00:00",
    }


def _make_unit(
    ws_id: str = CANON_WS,
    *,
    kind: str = "company",
    ticker: str | None = "ANT",
    ticker_key: str | None = None,
    slug: str = "ant",
) -> dict[str, Any]:
    return {
        "unit_id": UNIT_ID,
        "kind": kind,
        "owner_user_id": RICHARD_USER,
        "lead_analyst_user_id": RICHARD_USER,
        "slug": slug,
        "display_name": slug.upper(),
        "workspace_id": ws_id,
        "ticker": ticker,
        "ticker_key": ticker_key if ticker_key is not None else ticker,
        "status": "active",
    }


def _make_thread(
    tid: str = "11111111-2222-3333-4444-555555555555",
    *,
    ws_id: str = DUP_WS,
    thread_index: int = 0,
    title: str = "CY thread",
    is_shared: bool = False,
    share_token: str | None = None,
    share_permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "thread_index": thread_index,
        "title": title,
        "user_id": CY_USER,
        "created_by": CY_USER,
        "current_status": "completed",
        "is_shared": is_shared,
        "share_token": share_token,
        "share_permissions": share_permissions if share_permissions is not None else {},
        "shared_at": None,
        "updated_at": "2026-11-15T00:00:00+00:00",
    }


class _TxnRecorder:
    def __init__(self) -> None:
        self.entered = 0
        self.committed = 0
        self.rolled_back = 0


def _make_fake_conn(recorder: _TxnRecorder | None = None) -> psycopg.AsyncConnection[Any]:
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    txn_rec = recorder or _TxnRecorder()

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    @asynccontextmanager
    async def _txn_cm():
        txn_rec.entered += 1
        try:
            yield None
        except BaseException:
            txn_rec.rolled_back += 1
            raise
        else:
            txn_rec.committed += 1

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _txn_cm
    return conn


def _make_autospec_db(
    *,
    dup_ws: dict[str, Any] | None = None,
    canon_ws: dict[str, Any] | None = None,
    dup_unit: dict[str, Any] | None = None,
    canon_unit: dict[str, Any] | None = None,
    dup_activity: dict[str, Any] | None = None,
    canon_activity: dict[str, Any] | None = None,
    threads: list[dict[str, Any]] | None = None,
    open_wakeups: list[dict[str, Any]] | None = None,
    running_commands: list[dict[str, Any]] | None = None,
    unreported_commands: list[dict[str, Any]] | None = None,
    unfinished_outbox_notifications: list[dict[str, Any]] | None = None,
    queued_file_comments: list[dict[str, Any]] | None = None,
    dup_automations: list[dict[str, Any]] | None = None,
    canon_automations: list[dict[str, Any]] | None = None,
    unsettled_automation_executions: list[dict[str, Any]] | None = None,
    retained_research_counts: dict[str, int] | None = None,
    db_now: str = "2026-11-21T08:00:00+00:00",
    canon_max_run_seq: int = 0,
    dup_max_run_seq: int = 0,
    dup_latest_kv: dict[str, dict[str, Any]] | None = None,
    canon_latest_kv: dict[str, dict[str, Any]] | None = None,
    canon_all_kv: list[dict[str, Any]] | None = None,
    dup_sharing: dict[str, Any] | None = None,
    canon_sharing: dict[str, Any] | None = None,
    post_commit_threads: list[dict[str, Any]] | None = None,
    txn_recorder: _TxnRecorder | None = None,
) -> mod.MergeDb:
    db = create_autospec(mod.MergeDb, instance=True)
    idle = {
        "busy": False,
        "has_live_turns_or_subagents": False,
        "live_responses": 0,
        "live_subagents": 0,
        "live_commands": 0,
    }

    @asynccontextmanager
    async def _connect(*, dsn: str | None = None, read_only: bool = False):
        yield _make_fake_conn(txn_recorder)

    async def _fetch_workspace(cur: Any, ws_id: str) -> dict[str, Any] | None:
        if ws_id == DUP_WS:
            return dup_ws
        if ws_id == CANON_WS:
            return canon_ws
        return None

    async def _fetch_unit(cur: Any, ws_id: str) -> dict[str, Any] | None:
        if ws_id == DUP_WS:
            return dup_unit
        if ws_id == CANON_WS:
            return canon_unit
        return None

    async def _fetch_activity(cur: Any, ws_id: str) -> dict[str, Any]:
        if ws_id == DUP_WS:
            return dup_activity or idle
        return canon_activity or idle

    async def _fetch_latest_kv(cur: Any, ws_id: str) -> dict[str, dict[str, Any]]:
        return (dup_latest_kv or {}) if ws_id == DUP_WS else (canon_latest_kv or {})

    async def _fetch_doc_keys(cur: Any, ws_id: str) -> list[str]:
        if ws_id == DUP_WS:
            return sorted((dup_latest_kv or {}).keys())
        return sorted((canon_latest_kv or {}).keys())

    async def _fetch_automations(
        cur: Any, ws_id: str, thread_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return (dup_automations or []) if ws_id == DUP_WS else (canon_automations or [])

    async def _fetch_sharing(cur: Any, ws_id: str) -> dict[str, Any] | None:
        return dup_sharing if ws_id == DUP_WS else canon_sharing

    db.connect.side_effect = _connect
    db.fetch_workspace.side_effect = _fetch_workspace
    db.fetch_active_coverage_unit.side_effect = _fetch_unit
    db.fetch_live_activity.side_effect = _fetch_activity
    db.fetch_threads_for_workspace.return_value = threads or []
    db.fetch_threads_by_ids.return_value = (
        post_commit_threads if post_commit_threads is not None else (threads or [])
    )
    db.fetch_next_thread_index.return_value = 3
    db.fetch_open_wakeups_for_workspace.return_value = open_wakeups or []
    db.fetch_running_command_runs_for_workspace.return_value = running_commands or []
    db.fetch_unreported_command_runs_for_workspace.return_value = unreported_commands or []
    db.fetch_unfinished_outbox_notifications_for_workspace.return_value = (
        unfinished_outbox_notifications or []
    )
    db.fetch_queued_file_comments_for_workspace.return_value = queued_file_comments or []
    db.fetch_db_watermark.return_value = {
        "db_now": db_now,
        "canon_max_run_seq": canon_max_run_seq,
        "dup_max_run_seq": dup_max_run_seq,
    }
    db.fetch_automations_for_workspace.side_effect = _fetch_automations
    db.fetch_unsettled_automation_executions.return_value = (
        unsettled_automation_executions or []
    )
    db.fetch_dup_retained_research_counts.return_value = retained_research_counts or {
        "dalton_verdicts": 0,
        "dalton_signals": 0,
        "dalton_publications": 0,
        "dalton_publish_requests": 0,
        "workspace_memory": 0,
    }
    db.fetch_knowledge_doc_keys.side_effect = _fetch_doc_keys
    db.fetch_latest_knowledge_versions.side_effect = _fetch_latest_kv
    db.fetch_all_knowledge_versions.return_value = canon_all_kv or []
    db.fetch_sharing_row.side_effect = _fetch_sharing

    db.move_threads.return_value = len(threads or [])
    db.insert_merged_knowledge_version.return_value = {
        "knowledge_version_id": "kv-new",
        "doc_key": "company",
        "version_no": 1,
        "source": "server",
        "ticker": "ANT",
    }
    db.upsert_canon_sharing.return_value = 1
    db.mark_dup_historical_duplicate.return_value = 1
    return db


def _default_knowledge() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    dup_kv = {
        "agenda": {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000012",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "ticker": "ANTH",
            "doc_key": "agenda",
            "version_no": 12,
            "source": "tool",
            "content": {"items": [{"id": "a1"}]},
            "content_sha256": "sha-ag",
            "ops": None,
            "reason": "update",
            "evidence_refs": [],
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "conversation_thread_id": None,
            "run_id": None,
            "tool_call_id": None,
            "files_written_at": None,
            "created_at": "2026-11-18T00:00:00+00:00",
        },
        "company": {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000002",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "ticker": "ANTH",
            "doc_key": "company",
            "version_no": 2,
            "source": "tool",
            "content": {"ticker": "ANTH", "name": "Anthropic"},
            "content_sha256": "sha-co",
            "ops": None,
            "reason": "update company",
            "evidence_refs": [],
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "conversation_thread_id": None,
            "run_id": None,
            "tool_call_id": None,
            "files_written_at": None,
            "created_at": "2026-11-18T00:00:00+00:00",
        },
        "debate": {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000001",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "ticker": "ANTH",
            "doc_key": "debate",
            "version_no": 1,
            "source": "tool",
            "content": {"axes": []},
            "content_sha256": "sha-db",
            "ops": None,
            "reason": "init",
            "evidence_refs": [],
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "conversation_thread_id": None,
            "run_id": None,
            "tool_call_id": None,
            "files_written_at": None,
            "created_at": "2026-11-18T00:00:00+00:00",
        },
        "drivers": {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000022",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "ticker": "ANTH",
            "doc_key": "drivers",
            "version_no": 2,
            "source": "tool",
            "content": {"drivers": []},
            "content_sha256": "sha-dr",
            "ops": None,
            "reason": "init",
            "evidence_refs": [],
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "conversation_thread_id": None,
            "run_id": None,
            "tool_call_id": None,
            "files_written_at": None,
            "created_at": "2026-11-18T00:00:00+00:00",
        },
    }
    canon_kv = {
        "company": {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000003",
            "user_id": RICHARD_USER,
            "workspace_id": CANON_WS,
            "ticker": "ANT",
            "doc_key": "company",
            "version_no": 3,
            "source": "tool",
            "content": {"ticker": "ANT", "name": "Anthropic"},
            "content_sha256": "sha-canon-co",
            "ops": None,
            "reason": "canon company",
            "evidence_refs": [],
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "conversation_thread_id": None,
            "run_id": None,
            "tool_call_id": None,
            "files_written_at": None,
            "created_at": "2026-11-10T00:00:00+00:00",
        }
    }
    return dup_kv, canon_kv


# ---------------------------------------------------------------------------
# CLI & Preflight failure tests
# ---------------------------------------------------------------------------


def test_cli_removed_files_prefix_and_stdout_archive() -> None:
    parser = mod.build_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert "--files-prefix" not in option_strings
    assert "--dup" in option_strings
    assert "--canon" in option_strings
    assert "--apply" in option_strings
    assert "--revert" in option_strings
    assert "--archive-out" in option_strings


@pytest.mark.asyncio
async def test_preflight_rejects_same_workspace_bad_uuid_or_stdout_archive() -> None:
    db = _make_autospec_db()
    with pytest.raises(mod.MergeRefusedError, match="different workspaces"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=DUP_WS, apply=False, db=db)
    with pytest.raises(mod.MergeRefusedError, match="invalid --dup"):
        await mod.run_merge(dup_ws="not-a-uuid", canon_ws=CANON_WS, apply=False, db=db)
    with pytest.raises(mod.MergeRefusedError, match="stdout"):
        await mod.run_merge(
            dup_ws=DUP_WS,
            canon_ws=CANON_WS,
            apply=True,
            archive_out="-",
            db=db,
        )


@pytest.mark.asyncio
async def test_preflight_rejects_missing_or_deleted_or_home_workspace() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")

    db_missing = _make_autospec_db(dup_ws=None, canon_ws=canon_ws, canon_unit=_make_unit())
    with pytest.raises(mod.MergeRefusedError, match="does not exist"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_missing)

    deleted_canon = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT", status="deleted")
    db_del = _make_autospec_db(dup_ws=dup_ws, canon_ws=deleted_canon, canon_unit=_make_unit())
    with pytest.raises(mod.MergeRefusedError, match="is deleted"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_del)

    home_dup = _make_workspace(DUP_WS, user_id=CY_USER, name="Home", status="unbound")
    db_home = _make_autospec_db(dup_ws=home_dup, canon_ws=canon_ws, canon_unit=_make_unit())
    with pytest.raises(mod.MergeRefusedError, match="Home/unbound"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_home)


@pytest.mark.asyncio
async def test_preflight_rejects_coverage_unit_and_duplicate_marker_violations() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")

    db_no_unit = _make_autospec_db(dup_ws=dup_ws, canon_ws=canon_ws, canon_unit=None)
    with pytest.raises(mod.MergeRefusedError, match="not bound to an active coverage unit"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_no_unit)

    db_dup_unit = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_unit=_make_unit(ws_id=DUP_WS, ticker="ANTH"),
    )
    with pytest.raises(mod.MergeRefusedError, match="dup workspace .* bound to active"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_dup_unit)

    marked_dup = _make_workspace(
        DUP_WS,
        user_id=CY_USER,
        name="Anthropic",
        config={"dalton_historical_duplicate_of": CANON_WS},
    )
    db_marked = _make_autospec_db(
        dup_ws=marked_dup, canon_ws=canon_ws, canon_unit=_make_unit()
    )
    with pytest.raises(mod.MergeRefusedError, match="dalton_historical_duplicate_of"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_marked)


@pytest.mark.asyncio
async def test_preflight_rejects_sector_workspaces() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="AI Sector")

    # 1. canon bound to a sector unit
    db_canon_sector_unit = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(kind="sector", ticker=None, slug="ai-infra"),
    )
    with pytest.raises(mod.MergeRefusedError, match="sector workspace"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_canon_sector_unit)

    # 2. dup bound to a sector unit
    db_dup_sector_unit = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_unit=_make_unit(ws_id=DUP_WS, kind="sector", ticker=None, slug="ai-infra"),
    )
    with pytest.raises(mod.MergeRefusedError, match="sector workspace"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_dup_sector_unit)

    # 3. dup has 'sector' knowledge document
    db_dup_sector_doc = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_latest_kv={"sector": {"doc_key": "sector", "version_no": 1}},
    )
    with pytest.raises(mod.MergeRefusedError, match="sector workspace"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_dup_sector_doc)

    # 4. dup has dalton-sector-* automation
    db_dup_sector_auto = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_automations=[
            {
                "automation_id": "auto-sec-1",
                "name": "dalton-sector-ai-delta",
                "workspace_id": DUP_WS,
                "status": "paused",
                "thread_strategy": "new",
                "conversation_thread_id": None,
                "metadata": {"subject_kind": "sector"},
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="sector workspace"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_dup_sector_auto)


@pytest.mark.asyncio
async def test_preflight_rejects_shared_threads_open_wakeups_and_running_commands(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")

    # 1. Active thread share link
    shared_thread = _make_thread(
        is_shared=True,
        share_token="tok-abc123",
        share_permissions={"allow_files": True, "audience": "public"},
    )
    db_shared = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[shared_thread],
    )
    with pytest.raises(mod.MergeRefusedError, match="active thread share link"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_shared)
    out_shared = capsys.readouterr().out
    assert "tok-abc123" in out_shared
    assert "allow_files" in out_shared

    # Unshared thread (is_shared=False with retained share_token) is allowed
    unshared_thread = _make_thread(
        is_shared=False,
        share_token="tok-revoked",
        share_permissions={"allow_files": False},
    )
    db_unshared = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[unshared_thread],
    )
    dry = await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_unshared)
    assert dry["applied"] is False
    assert "tok-revoked" in capsys.readouterr().out

    # 2. Unfinished wakeups
    db_wakeup = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        open_wakeups=[
            {
                "wakeup_id": "wk-1",
                "short_id": "w1",
                "conversation_thread_id": "11111111-2222-3333-4444-555555555555",
                "kind": "at",
                "status": "armed",
                "label": "check earnings",
                "next_fire_at": "2026-11-22T09:00:00+00:00",
                "last_fired_seq": 0,
                "delivered_seq": 0,
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="unfinished wakeup"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_wakeup)
    assert "wk-1" in capsys.readouterr().out

    # 3. Unfinished / running command_runs (real activity=busy branch prints command IDs before refusing)
    db_running_cmd = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        dup_activity={
            "busy": True,
            "has_live_turns_or_subagents": False,
            "live_responses": 0,
            "live_subagents": 0,
            "live_commands": 1,
        },
        running_commands=[
            {
                "command_id": "bg-000000000001",
                "conversation_thread_id": "11111111-2222-3333-4444-555555555555",
                "workspace_id": DUP_WS,
                "computer_id": "comp-1",
                "tool": "Bash",
                "status": "running",
                "started_at": "2026-11-21T01:00:00+00:00",
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="running command_runs"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_running_cmd)
    out_running = capsys.readouterr().out
    assert "bg-000000000001" in out_running
    db_running_cmd.fetch_running_command_runs_for_workspace.assert_awaited()

    # 3b. When live_responses AND running_commands are both active, command IDs are still printed
    db_both_busy = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        dup_activity={
            "busy": True,
            "has_live_turns_or_subagents": True,
            "live_responses": 1,
            "live_subagents": 0,
            "live_commands": 1,
        },
        running_commands=[
            {
                "command_id": "bg-000000000002",
                "conversation_thread_id": "11111111-2222-3333-4444-555555555555",
                "workspace_id": DUP_WS,
                "computer_id": "comp-1",
                "tool": "Bash",
                "status": "running",
                "started_at": "2026-11-21T01:00:00+00:00",
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="bg-000000000002"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_both_busy)
    assert "bg-000000000002" in capsys.readouterr().out

    # 4. Cancelled-in-flight wakeup (status='cancelled' but still owed in hook_outbox)
    db_cancelled_inflight = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        open_wakeups=[
            {
                "wakeup_id": "wk-cif-1",
                "short_id": "w2",
                "conversation_thread_id": "11111111-2222-3333-4444-555555555555",
                "kind": "cron",
                "status": "cancelled",
                "cancelled_by": "user",
                "label": "in-flight notice",
                "next_fire_at": None,
                "last_fired_seq": 2,
                "delivered_seq": 1,
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="wakeup"):
        await mod.run_merge(
            dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_cancelled_inflight
        )
    out_cif = capsys.readouterr().out
    assert "wk-cif-1" in out_cif
    assert "cancelled_by='user'" in out_cif

    # 5. Finished command_runs whose report-back is still owed
    db_unreported = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        unreported_commands=[
            {
                "command_id": "bg-finished-owed-1",
                "conversation_thread_id": "11111111-2222-3333-4444-555555555555",
                "workspace_id": DUP_WS,
                "tool": "ExecuteCode",
                "status": "completed",
                "stopped_by": None,
                "ended_at": "2026-11-21T02:00:00+00:00",
                "outbox_hook_type": "command_report_back",
                "outbox_status": "pending",
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="unreported finished command_runs"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_unreported)
    assert "bg-finished-owed-1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_preflight_prints_paused_automation_pinned_notice_without_modifying(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("t-1", thread_index=0)
    autos = [
        {
            "automation_id": "auto-1",
            "name": "dalton-anth-watch",
            "workspace_id": DUP_WS,
            "status": "paused",
            "thread_strategy": "continue",
            "conversation_thread_id": "t-1",
            "metadata": {},
        },
        {
            "automation_id": "auto-2",
            "name": "dalton-anth-delta",
            "workspace_id": DUP_WS,
            "status": "paused",
            "thread_strategy": "new",
            "conversation_thread_id": None,
            "metadata": {},
        },
    ]
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        dup_automations=autos,
    )
    res = await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db)
    assert res["applied"] is False
    out = capsys.readouterr().out
    assert "thread_strategy='continue'" in out
    assert "pinned to migrating thread t-1; automation will NOT be modified." in out
    assert mod.FILE_COPY_NOTICE in out


# ---------------------------------------------------------------------------
# Knowledge version calculation & write contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_version_numbering_and_insert_sql_without_trigger_disable() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, canon_kv = _default_knowledge()
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
    )
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    plan = await mod.build_merge_plan(
        db, cur, dup_ws=DUP_WS, canon_ws=CANON_WS, tx_now="2026-11-21T08:00:00+00:00"
    )
    by_doc = {item.doc_key: item for item in plan.knowledge_items}
    assert by_doc["agenda"].target_version_no == 1
    assert by_doc["debate"].target_version_no == 1
    assert by_doc["drivers"].target_version_no == 1
    assert by_doc["company"].target_version_no == 4
    assert by_doc["company"].ticker == "ANT"
    assert f"merge:{DUP_WS}" in by_doc["agenda"].reason

    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    planned_id = by_doc["company"].target_knowledge_version_id
    sql_cur.fetchone.side_effect = [
        {"version_no": 3},
        {
            "knowledge_version_id": planned_id,
            "doc_key": "company",
            "version_no": 4,
            "source": "server",
            "ticker": "ANT",
        },
    ]
    sql_cur.rowcount = 1
    inserted = await real_db.insert_merged_knowledge_version(
        sql_cur,
        canon_user_id=RICHARD_USER,
        canon_ws=CANON_WS,
        dup_ws=DUP_WS,
        item=by_doc["company"],
    )
    assert inserted["knowledge_version_id"] == planned_id
    assert inserted["version_no"] == 4
    executed_sqls = [call.args[0] for call in sql_cur.execute.call_args_list]
    assert not any("DISABLE TRIGGER" in s for s in executed_sqls)
    assert "pg_advisory_xact_lock" in executed_sqls[0]
    insert_sql, insert_params = sql_cur.execute.call_args_list[-1].args
    assert "INSERT INTO dalton_knowledge_versions" in insert_sql
    assert insert_params[0] == planned_id
    assert insert_params[1] == RICHARD_USER
    assert insert_params[2] == CANON_WS
    assert insert_params[3] == "company"
    assert insert_params[4] == 4
    assert insert_params[5] == "server"
    assert f"merge:{DUP_WS}" in insert_params[9]


# ---------------------------------------------------------------------------
# Archive safety, lock ordering, transactional rollback, and revert checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_refuses_overwrite_and_rolls_back_without_db_writes(
    tmp_path: Path,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, canon_kv = _default_knowledge()
    rec = _TxnRecorder()
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
        txn_recorder=rec,
    )
    archive_file = tmp_path / "existing.jsonl"
    archive_file.write_text("do-not-overwrite\n", encoding="utf-8")

    with pytest.raises(mod.MergeRefusedError, match="already exists"):
        await mod.run_merge(
            dup_ws=DUP_WS,
            canon_ws=CANON_WS,
            apply=True,
            archive_out=str(archive_file),
            db=db,
        )

    assert archive_file.read_text(encoding="utf-8") == "do-not-overwrite\n"
    assert rec.entered == 1
    assert rec.rolled_back == 1
    assert rec.committed == 0
    db.move_threads.assert_not_awaited()
    db.insert_merged_knowledge_version.assert_not_awaited()
    db.upsert_canon_sharing.assert_not_awaited()
    db.mark_dup_historical_duplicate.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_locks_in_order_and_archives_exact_written_plan(
    tmp_path: Path, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic", null_config=True)
    dup_kv, canon_kv = _default_knowledge()
    apply_rec = _TxnRecorder()
    threads = [_make_thread("t-1", thread_index=0), _make_thread("t-2", thread_index=1)]

    call_order: list[str] = []
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=threads,
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
        canon_all_kv=[canon_kv["company"]],
        dup_sharing={
            "workspace_id": DUP_WS,
            "cos_access": "rw",
            "web_access": "rw",
            "updated_by": CY_USER,
            "updated_at": "2026-11-01T00:00:00+00:00",
        },
        canon_sharing=None,
        txn_recorder=apply_rec,
    )

    def _rec(name: str, ret: Any = None):
        async def _fn(*args: Any, **kwargs: Any):
            call_order.append(name)
            return ret
        return _fn

    db.set_lock_timeout.side_effect = _rec("lock_timeout")
    db.lock_thread_index.side_effect = _rec("lock_thread_index")
    db.lock_workspaces_for_update.side_effect = _rec("lock_workspaces")
    db.lock_threads_for_update.side_effect = _rec("lock_threads")
    db.lock_sharing_for_update.side_effect = _rec("lock_sharing")
    db.lock_knowledge_doc.side_effect = _rec("lock_kv")

    archive_file = tmp_path / "merge-ws-test" / "before.jsonl"
    result = await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(archive_file),
        db=db,
    )

    assert result["applied"] is True
    assert apply_rec.committed == 1
    assert apply_rec.rolled_back == 0
    assert stat.S_IMODE(archive_file.stat().st_mode) == 0o600
    assert mod.FILE_COPY_NOTICE in capsys.readouterr().out

    # Verify lock order matching app (home_fold.py & src/dalton/sharing.py:261):
    # lock_timeout -> lock_thread_index -> lock_sharing -> lock_workspaces ->
    # lock_threads -> lock_kv
    assert call_order[0] == "lock_timeout"
    idx_ti = call_order.index("lock_thread_index")
    idx_sh = call_order.index("lock_sharing")
    idx_ws = call_order.index("lock_workspaces")
    idx_th = call_order.index("lock_threads")
    idx_kv = call_order.index("lock_kv")
    assert idx_ti < idx_sh < idx_ws < idx_th < idx_kv

    # Verify archive matches exact values passed to DB writes
    records = mod.load_archive_jsonl(archive_file)
    kv_plans = {r["doc_key"]: r for r in records if r["record_type"] == "knowledge_plan"}
    assert set(kv_plans) == {"agenda", "company", "debate", "drivers"}
    assert kv_plans["company"]["target_version_no"] == 4
    assert kv_plans["company"]["new"]["version_no"] == 4
    assert kv_plans["agenda"]["target_version_no"] == 1

    # Check exact knowledge_version_id passed to insert_merged_knowledge_version
    inserted_items = [
        call.kwargs["item"]
        for call in db.insert_merged_knowledge_version.call_args_list
    ]
    for item in inserted_items:
        archived = kv_plans[item.doc_key]
        assert archived["knowledge_version_id"] == item.target_knowledge_version_id
        assert archived["new"]["knowledge_version_id"] == item.target_knowledge_version_id
        assert archived["target_version_no"] == item.target_version_no

    # Check exact thread_index values passed to move_threads
    moved_items = db.move_threads.call_args.args[3]
    archived_threads = {
        r["pk"]["conversation_thread_id"]: r
        for r in records
        if r["record_type"] == "conversation_threads"
    }
    for m in moved_items:
        tid = m.row["conversation_thread_id"]
        assert archived_threads[tid]["new"]["thread_index"] == m.target_thread_index
        assert archived_threads[tid]["new"]["workspace_id"] == CANON_WS

    # Check dup config NULL preserved in old
    dup_rec = next(
        r for r in records if r["record_type"] == "workspaces" and r.get("role") == "dup"
    )
    assert dup_rec["old"]["config"] is None
    assert dup_rec["new"]["config"] == {"dalton_historical_duplicate_of": CANON_WS}


@pytest.mark.asyncio
async def test_apply_rolls_back_on_rowcount_mismatch(tmp_path: Path, capsys: Any) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    rec = _TxnRecorder()
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        txn_recorder=rec,
    )
    db.move_threads.return_value = 0
    archive_file = tmp_path / "before.jsonl"

    with pytest.raises(RuntimeError, match="rowcount mismatch on conversation_threads"):
        await mod.run_merge(
            dup_ws=DUP_WS,
            canon_ws=CANON_WS,
            apply=True,
            archive_out=str(archive_file),
            db=db,
        )

    assert rec.entered == 1
    assert rec.rolled_back == 1
    assert rec.committed == 0
    assert "APPLIED:" not in capsys.readouterr().out


def _build_applied_archive(
    tmp_path: Path,
    *,
    dup_null_config: bool = False,
    canon_sharing_existed: bool = False,
    canon_max_run_seq: int = 10,
    dup_max_run_seq: int = 5,
) -> tuple[Path, list[dict[str, Any]]]:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(
        DUP_WS, user_id=CY_USER, name="Anthropic", null_config=dup_null_config
    )
    dup_kv, canon_kv = _default_knowledge()
    tx_now = "2026-11-21T08:00:00+00:00"
    t1 = _make_thread("11111111-1111-1111-1111-111111111111", thread_index=0)
    canon_sharing = (
        {
            "workspace_id": CANON_WS,
            "cos_access": "ro",
            "web_access": "hidden",
            "updated_by": RICHARD_USER,
            "updated_at": "2026-10-01T00:00:00+00:00",
        }
        if canon_sharing_existed
        else None
    )
    plan = mod.MergePlan(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        tx_now=tx_now,
        dup_workspace=dup_ws,
        canon_workspace=canon_ws,
        canon_unit=_make_unit(),
        canon_max_run_seq=canon_max_run_seq,
        dup_max_run_seq=dup_max_run_seq,
        thread_moves=[mod.ThreadMoveItem(row=t1, target_workspace_id=CANON_WS, target_thread_index=3)],
        canon_next_thread_index=3,
        knowledge_items=[
            mod.KnowledgeMergeItem(
                doc_key="company",
                dup_latest=dup_kv["company"],
                canon_latest=canon_kv["company"],
                target_knowledge_version_id="99999999-9999-9999-9999-999999999999",
                target_version_no=4,
                reason=f"merge:{DUP_WS}: merged company v2",
                content_sha256="sha-co",
                ticker="ANT",
            )
        ],
        canon_existing_knowledge=[canon_kv["company"]],
        dup_sharing=None,
        canon_sharing=canon_sharing,
        dup_new_config={"dalton_historical_duplicate_of": CANON_WS},
    )
    records = mod.build_archive_records(plan, timestamp=tx_now)
    archive_file = tmp_path / "before.jsonl"
    mod.write_archive_jsonl(records, archive_file)
    return archive_file, records


def _make_revert_db(
    records: list[dict[str, Any]],
    *,
    curr_thread_override: dict[str, Any] | None = None,
    curr_sharing_override: dict[str, Any] | None = None,
    curr_dup_ws_override: dict[str, Any] | None = None,
    canon_activity: dict[str, Any] | None = None,
    canon_responses_after: list[dict[str, Any]] | None = None,
    dup_post_threads: list[dict[str, Any]] | None = None,
    sharing_rowcount: int = 1,
    txn_recorder: _TxnRecorder | None = None,
) -> mod.MergeDb:
    revert_db = create_autospec(mod.MergeDb, instance=True)

    @asynccontextmanager
    async def _revert_connect(*, dsn: str | None = None, read_only: bool = False):
        yield _make_fake_conn(txn_recorder)

    revert_db.connect.side_effect = _revert_connect

    thread_rec = next(r for r in records if r["record_type"] == "conversation_threads")
    curr_thread = curr_thread_override or {
        "conversation_thread_id": thread_rec["pk"]["conversation_thread_id"],
        "workspace_id": thread_rec["new"]["workspace_id"],
        "thread_index": thread_rec["new"]["thread_index"],
        "updated_at": thread_rec["new"]["updated_at"],
    }
    sharing_rec = next(
        r for r in records if r["record_type"] == "dalton_workspace_sharing" and r["role"] == "canon"
    )
    curr_sharing = (
        curr_sharing_override
        if curr_sharing_override is not None
        else copy.deepcopy(sharing_rec["new"])
    )
    dup_rec = next(r for r in records if r["record_type"] == "workspaces" and r["role"] == "dup")
    curr_dup = (
        curr_dup_ws_override
        if curr_dup_ws_override is not None
        else {
            "workspace_id": DUP_WS,
            "config": copy.deepcopy(dup_rec["new"]["config"]),
            "updated_at": dup_rec["new"]["updated_at"],
        }
    )

    revert_db.fetch_threads_by_ids.return_value = [curr_thread]
    revert_db.fetch_sharing_row.return_value = curr_sharing
    revert_db.fetch_workspace.return_value = curr_dup
    revert_db.fetch_live_activity.return_value = canon_activity or {
        "busy": False,
        "has_live_turns_or_subagents": False,
        "live_responses": 0,
        "live_subagents": 0,
        "live_commands": 0,
    }
    revert_db.fetch_canon_responses_after.return_value = canon_responses_after or []
    revert_db.fetch_threads_for_workspace.return_value = dup_post_threads or []
    revert_db.verify_and_delete_merged_knowledge_versions.return_value = 1
    revert_db.restore_threads.return_value = 1
    revert_db.restore_sharing.return_value = sharing_rowcount
    revert_db.restore_dup_workspace_row.return_value = 1
    return revert_db


@pytest.mark.asyncio
async def test_revert_stops_if_row_modified_after_merge(tmp_path: Path) -> None:
    archive_file, records = _build_applied_archive(tmp_path)

    # 1. Thread modified after merge (e.g., new turn bumped updated_at)
    thread_rec = next(r for r in records if r["record_type"] == "conversation_threads")
    rec1 = _TxnRecorder()
    db1 = _make_revert_db(
        records,
        curr_thread_override={
            "conversation_thread_id": thread_rec["pk"]["conversation_thread_id"],
            "workspace_id": CANON_WS,
            "thread_index": 3,
            "updated_at": "2026-11-21T10:00:00+00:00",
        },
        txn_recorder=rec1,
    )
    with pytest.raises(mod.MergeRefusedError, match="updated_at changed after merge"):
        await mod.run_revert(archive_path=archive_file, db=db1)
    assert rec1.rolled_back == 1
    db1.restore_threads.assert_not_awaited()
    db1.verify_and_delete_merged_knowledge_versions.assert_not_awaited()

    # 2. Sharing modified after merge
    sharing_rec = next(
        r for r in records if r["record_type"] == "dalton_workspace_sharing" and r["role"] == "canon"
    )
    modified_sharing = {**sharing_rec["new"], "web_access": "ro"}
    rec2 = _TxnRecorder()
    db2 = _make_revert_db(records, curr_sharing_override=modified_sharing, txn_recorder=rec2)
    with pytest.raises(mod.MergeRefusedError, match="web_access changed after merge"):
        await mod.run_revert(archive_path=archive_file, db=db2)
    assert rec2.rolled_back == 1
    db2.restore_sharing.assert_not_awaited()

    # 3. dup workspace config modified after merge
    rec3 = _TxnRecorder()
    db3 = _make_revert_db(
        records,
        curr_dup_ws_override={
            "workspace_id": DUP_WS,
            "config": {"dalton_historical_duplicate_of": CANON_WS, "extra": 1},
            "updated_at": "2026-11-21T08:00:00+00:00",
        },
        txn_recorder=rec3,
    )
    with pytest.raises(mod.MergeRefusedError, match="config changed after merge"):
        await mod.run_revert(archive_path=archive_file, db=db3)
    assert rec3.rolled_back == 1
    db3.restore_dup_workspace_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_revert_refuses_non_latest_knowledge_version_and_skips_trigger_disable() -> None:
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    # Current latest for 'company' is v5 (different UUID & version), not archived v4
    cur.fetchone.return_value = {
        "knowledge_version_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "version_no": 5,
        "source": "tool",
        "reason": "user edited after merge",
    }
    db = mod.MergeDb()
    targets = [
        {
            "doc_key": "company",
            "knowledge_version_id": "99999999-9999-9999-9999-999999999999",
            "target_version_no": 4,
        }
    ]
    with pytest.raises(mod.MergeRefusedError, match="no longer the latest version"):
        await db.verify_and_delete_merged_knowledge_versions(
            cur, CANON_WS, DUP_WS, targets
        )
    executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
    assert "pg_advisory_xact_lock" in executed_sqls[0]
    assert not any("DISABLE TRIGGER" in s for s in executed_sqls)
    assert not any("DELETE FROM dalton_knowledge_versions" in s for s in executed_sqls)


@pytest.mark.asyncio
async def test_revert_deletes_exact_knowledge_version_id_with_lock_before_trigger_disable() -> None:
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    kv_id = "99999999-9999-9999-9999-999999999999"
    cur.fetchone.return_value = {
        "knowledge_version_id": kv_id,
        "version_no": 4,
        "source": "server",
        "reason": f"merge:{DUP_WS}: merged company v2",
    }
    cur.rowcount = 1
    db = mod.MergeDb()
    targets = [
        {
            "doc_key": "company",
            "knowledge_version_id": kv_id,
            "target_version_no": 4,
        }
    ]
    deleted = await db.verify_and_delete_merged_knowledge_versions(
        cur, CANON_WS, DUP_WS, targets
    )
    assert deleted == 1
    executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
    idx_lock = next(i for i, s in enumerate(executed_sqls) if "pg_advisory_xact_lock" in s)
    idx_disable = next(i for i, s in enumerate(executed_sqls) if "DISABLE TRIGGER" in s)
    idx_delete = next(i for i, s in enumerate(executed_sqls) if "DELETE FROM dalton_knowledge_versions" in s)
    idx_enable = next(i for i, s in enumerate(executed_sqls) if "ENABLE TRIGGER" in s)
    assert idx_lock < idx_disable < idx_delete < idx_enable
    del_params = cur.execute.call_args_list[idx_delete].args[1]
    assert del_params[0] == kv_id
    assert del_params[1] == CANON_WS
    assert del_params[2] == "company"
    assert del_params[3] == 4


@pytest.mark.asyncio
async def test_revert_asserts_sharing_delete_rowcount_and_restores_sql_null_config(
    tmp_path: Path,
) -> None:
    # When canon sharing did NOT exist before merge, revert must delete and assert rowcount == 1
    archive_file, records = _build_applied_archive(
        tmp_path, dup_null_config=True, canon_sharing_existed=False
    )

    rec_fail = _TxnRecorder()
    db_fail = _make_revert_db(records, sharing_rowcount=0, txn_recorder=rec_fail)
    with pytest.raises(RuntimeError, match="rowcount mismatch on restore dalton_workspace_sharing"):
        await mod.run_revert(archive_path=archive_file, db=db_fail)
    assert rec_fail.rolled_back == 1

    rec_ok = _TxnRecorder()
    db_ok = _make_revert_db(records, sharing_rowcount=1, txn_recorder=rec_ok)
    out = await mod.run_revert(archive_path=archive_file, db=db_ok)
    assert out["reverted"] is True
    assert rec_ok.committed == 1
    _, _, existed, old_sharing = db_ok.restore_sharing.call_args.args
    assert existed is False
    assert old_sharing is None

    # Verify MergeDb.restore_sharing executes DELETE when existed=False
    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur.rowcount = 1
    deleted_sharing = await real_db.restore_sharing(sql_cur, CANON_WS, False, None)
    assert deleted_sharing == 1
    assert "DELETE FROM dalton_workspace_sharing" in sql_cur.execute.call_args.args[0]

    # Verify MergeDb.restore_dup_workspace_row passes None (SQL NULL) when old config was None
    sql_cur2 = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur2.rowcount = 1
    restored = await real_db.restore_dup_workspace_row(
        sql_cur2,
        DUP_WS,
        {"config": None, "updated_at": "2026-11-20T00:00:00+00:00"},
    )
    assert restored == 1
    _, params = sql_cur2.execute.call_args.args
    assert params[0] is None
    assert params[1] == "2026-11-20T00:00:00+00:00"
    assert params[2] == DUP_WS


@pytest.mark.asyncio
async def test_preflight_rejects_live_activity_on_dup_or_canon() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")

    db_busy_canon = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        canon_activity={"busy": True, "live_responses": 1, "live_subagents": 0, "live_commands": 0},
    )
    with pytest.raises(mod.MergeRefusedError, match="live work in progress"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_busy_canon)


@pytest.mark.asyncio
async def test_mergedb_lock_timeout_and_narrowed_sql_contracts() -> None:
    real_db = mod.MergeDb()
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.rowcount = 1
    cur.fetchall.return_value = []

    await real_db.set_lock_timeout(cur)
    assert cur.execute.call_args.args[0] == "SET LOCAL lock_timeout = '5s'"

    t1 = _make_thread("t-1", thread_index=0)
    move = mod.ThreadMoveItem(row=t1, target_workspace_id=CANON_WS, target_thread_index=5)
    moved = await real_db.move_threads(cur, DUP_WS, CANON_WS, [move])
    assert moved == 1
    move_sql, move_params = cur.execute.call_args.args
    assert "UPDATE conversation_threads" in move_sql
    assert move_params["thread_ids"] == ["t-1"]
    assert move_params["indices"] == [5]

    await real_db.fetch_open_wakeups_for_workspace(cur, DUP_WS)
    wk_sql = cur.execute.call_args.args[0]
    assert "FROM wakeups w" in wk_sql
    assert "w.status = 'armed'" in wk_sql

    restored_sh = await real_db.restore_sharing(
        cur,
        CANON_WS,
        True,
        {
            "cos_access": "ro",
            "web_access": "hidden",
            "updated_by": RICHARD_USER,
            "updated_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert restored_sh == 1
    assert "UPDATE dalton_workspace_sharing" in cur.execute.call_args.args[0]


# ---------------------------------------------------------------------------
# Round 3 review fixes & invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_refuses_when_merged_knowledge_files_written_at_is_set() -> None:
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    kv_id = "99999999-9999-9999-9999-999999999999"
    cur.fetchone.return_value = {
        "knowledge_version_id": kv_id,
        "version_no": 4,
        "source": "server",
        "reason": f"merge:{DUP_WS}: merged company v2",
        "files_written_at": "2026-11-21T08:05:00+00:00",
    }
    db = mod.MergeDb()
    targets = [
        {
            "doc_key": "company",
            "knowledge_version_id": kv_id,
            "target_version_no": 4,
        }
    ]
    with pytest.raises(mod.MergeRefusedError, match="already synced to workspace files"):
        await db.verify_and_delete_merged_knowledge_versions(
            cur, CANON_WS, DUP_WS, targets
        )
    executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
    assert not any("DISABLE TRIGGER" in s for s in executed_sqls)
    assert not any("DELETE FROM dalton_knowledge_versions" in s for s in executed_sqls)


@pytest.mark.asyncio
async def test_revert_refuses_when_canon_has_responses_after_merge(
    tmp_path: Path,
) -> None:
    archive_file, records = _build_applied_archive(tmp_path)
    rec = _TxnRecorder()
    db = _make_revert_db(
        records,
        canon_responses_after=[
            {
                "conversation_response_id": "resp-after-1",
                "conversation_thread_id": "22222222-2222-2222-2222-222222222222",
                "turn_index": 5,
                "status": "completed",
                "created_at": "2026-11-21T08:02:00+00:00",
            }
        ],
        txn_recorder=rec,
    )
    with pytest.raises(mod.MergeRefusedError, match="conversation_responses"):
        await mod.run_revert(archive_path=archive_file, db=db)
    assert rec.rolled_back == 1
    assert rec.committed == 0
    db.verify_and_delete_merged_knowledge_versions.assert_not_awaited()
    db.restore_threads.assert_not_awaited()


@pytest.mark.asyncio
async def test_wakeup_sql_includes_cancelled_in_flight_and_unreported_commands_filter() -> None:
    real_db = mod.MergeDb()
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.fetchall.return_value = []

    await real_db.fetch_open_wakeups_for_workspace(cur, DUP_WS)
    wk_sql = cur.execute.call_args.args[0]
    assert "hook_outbox o" in wk_sql
    assert "WAKEUP_OWED_SQL" not in wk_sql  # interpolated into SQL
    assert "wk.cancelled_by NOT IN" in wk_sql

    # fetch_unreported_command_runs_for_workspace keeps owed row & filters stop_already_covered
    cur2 = create_autospec(psycopg.AsyncCursor, instance=True)
    cur2.fetchall.return_value = [
        {
            "command_id": "cmd-owed",
            "conversation_thread_id": "t-1",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "computer_id": "comp-1",
            "run_id": "run-1",
            "tool": "Bash",
            "description": "wait",
            "status": "completed",
            "failure_reason": None,
            "stopped_by": None,
            "started_at": "2026-11-21T01:00:00+00:00",
            "ended_at": "2026-11-21T01:05:00+00:00",
            "result_delivered_at": None,
            "updated_at": "2026-11-21T01:05:00+00:00",
            "outbox_id": "ob-1",
            "outbox_hook_type": "command_report_back",
            "outbox_status": "pending",
            "parent_status": "completed",
            "parent_metadata": {},
            "parent_cancel_requested_at": None,
        },
        {
            "command_id": "cmd-stop-covered",
            "conversation_thread_id": "t-1",
            "user_id": CY_USER,
            "workspace_id": DUP_WS,
            "computer_id": "comp-1",
            "run_id": "run-2",
            "tool": "Bash",
            "description": "covered",
            "status": "completed",
            "failure_reason": None,
            "stopped_by": None,
            "started_at": "2026-11-21T01:00:00+00:00",
            "ended_at": "2026-11-21T01:04:00+00:00",
            "result_delivered_at": None,
            "updated_at": "2026-11-21T01:04:00+00:00",
            "outbox_id": "ob-2",
            "outbox_hook_type": "command_report_back",
            "outbox_status": "pending",
            "parent_status": "cancelled",
            "parent_metadata": {"cancelled_by_user": True},
            "parent_cancel_requested_at": "2026-11-21T01:05:00+00:00",
        },
    ]
    owed = await real_db.fetch_unreported_command_runs_for_workspace(cur2, DUP_WS)
    assert [r["command_id"] for r in owed] == ["cmd-owed"]


@pytest.mark.asyncio
async def test_archive_uses_ensure_ascii_and_splits_on_newline_for_u2028_title(
    tmp_path: Path,
) -> None:
    title_with_ls = "Anthropic\u2028Research Note"
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-1111-1111-1111-111111111111", title=title_with_ls)
    plan = mod.MergePlan(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        tx_now="2026-11-21T08:00:00+00:00",
        dup_workspace=dup_ws,
        canon_workspace=canon_ws,
        canon_unit=_make_unit(),
        thread_moves=[
            mod.ThreadMoveItem(row=t1, target_workspace_id=CANON_WS, target_thread_index=3)
        ],
        canon_next_thread_index=3,
    )
    records = mod.build_archive_records(plan, timestamp="20261121T080000Z")
    archive_file = tmp_path / "u2028.jsonl"
    mod.write_archive_jsonl(records, archive_file)

    raw_text = archive_file.read_text(encoding="utf-8")
    assert "\u2028" not in raw_text
    assert "\\u2028" in raw_text

    loaded = mod.load_archive_jsonl(archive_file)
    thread_rec = next(r for r in loaded if r["record_type"] == "conversation_threads")
    assert thread_rec["row"]["title"] == title_with_ls

    # Even if an archive file contains a literal unescaped U+2028 inside a JSON string,
    # load_archive_jsonl splits strictly on '\n' (not splitlines()).
    raw_u2028_file = tmp_path / "raw_u2028.jsonl"
    raw_u2028_file.write_text(
        '{"record_type": "meta", "title": "a\u2028b"}\n', encoding="utf-8"
    )
    raw_loaded = mod.load_archive_jsonl(raw_u2028_file)
    assert raw_loaded == [{"record_type": "meta", "title": "a\u2028b"}]


@pytest.mark.asyncio
async def test_revert_refuses_when_archive_missing_knowledge_plan_count_mismatch(
    tmp_path: Path,
) -> None:
    archive_file, records = _build_applied_archive(tmp_path)
    # Remove the single knowledge_plan line while leaving meta.counts untouched
    truncated = [r for r in records if r["record_type"] != "knowledge_plan"]
    bad_archive = tmp_path / "missing_kp.jsonl"
    mod.write_archive_jsonl(truncated, bad_archive)

    db = _make_revert_db(records)
    with pytest.raises(mod.MergeRefusedError, match="record count mismatch for dalton_knowledge_versions"):
        await mod.run_revert(archive_path=bad_archive, db=db)
    db.connect.assert_not_called()


@pytest.mark.asyncio
async def test_revert_locks_sharing_before_workspaces(tmp_path: Path) -> None:
    archive_file, records = _build_applied_archive(tmp_path)
    call_order: list[str] = []
    rec = _TxnRecorder()
    db = _make_revert_db(records, txn_recorder=rec)

    def _rec(name: str):
        async def _fn(*args: Any, **kwargs: Any):
            call_order.append(name)
            return None
        return _fn

    db.set_lock_timeout.side_effect = _rec("lock_timeout")
    db.lock_thread_index.side_effect = _rec("lock_thread_index")
    db.lock_sharing_for_update.side_effect = _rec("lock_sharing")
    db.lock_workspaces_for_update.side_effect = _rec("lock_workspaces")
    db.lock_threads_by_ids_for_update.side_effect = _rec("lock_threads")
    db.lock_knowledge_doc.side_effect = _rec("lock_kv")

    out = await mod.run_revert(archive_path=archive_file, db=db)
    assert out["reverted"] is True
    assert call_order[0] == "lock_timeout"
    assert (
        call_order.index("lock_thread_index")
        < call_order.index("lock_sharing")
        < call_order.index("lock_workspaces")
        < call_order.index("lock_threads")
        < call_order.index("lock_kv")
    )


@pytest.mark.asyncio
async def test_preflight_rejects_non_paused_automation_and_unsettled_executions(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")

    # 1. Non-paused automation on dup
    db_active_auto = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_automations=[
            {
                "automation_id": "auto-active-1",
                "name": "dalton-anth-watch",
                "workspace_id": DUP_WS,
                "status": "active",
                "thread_strategy": "new",
                "conversation_thread_id": None,
                "metadata": {},
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="non-paused automation"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_active_auto)
    assert "auto-active-1" in capsys.readouterr().out

    # 2. Paused automation with pending/running execution record
    db_pending_exec = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        dup_automations=[
            {
                "automation_id": "auto-paused-1",
                "name": "dalton-anth-watch",
                "workspace_id": DUP_WS,
                "status": "paused",
                "thread_strategy": "new",
                "conversation_thread_id": None,
                "metadata": {},
            }
        ],
        unsettled_automation_executions=[
            {
                "automation_execution_id": "exec-pend-1",
                "automation_id": "auto-paused-1",
                "automation_name": "dalton-anth-watch",
                "status": "pending",
                "conversation_thread_id": None,
                "scheduled_at": "2026-11-21T08:00:00+00:00",
                "started_at": None,
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="pending/waiting/running automation execution"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_pending_exec)
    assert "exec-pend-1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_prints_retained_research_counts_on_dup(capsys: Any) -> None:
    from ptc_agent.agent.backends.langgraph_store import workspace_memory_namespace

    assert mod._workspace_memory_prefix("cy.user", DUP_WS) == ".".join(
        workspace_memory_namespace("cy.user", DUP_WS)
    )

    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        retained_research_counts={
            "dalton_verdicts": 4,
            "dalton_signals": 7,
            "dalton_publications": 2,
            "dalton_publish_requests": 3,
            "workspace_memory": 5,
        },
    )
    res = await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db)
    assert res["applied"] is False
    out = capsys.readouterr().out
    assert "dalton_verdicts: 4 (留在副本)" in out
    assert "dalton_signals: 7 (留在副本)" in out
    assert "dalton_publications: 2 (留在副本)" in out
    assert "dalton_publish_requests: 3 (留在副本)" in out
    assert "workspace_memory: 5 (留在副本)" in out
    assert "旧回复的相对文件链接" in out
    assert "执行历史按计算机筛选" in out
    assert "请在无人使用时运行" in out


@pytest.mark.asyncio
async def test_revert_does_not_enable_trigger_in_finally_when_delete_fails() -> None:
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    kv_id = "99999999-9999-9999-9999-999999999999"
    cur.fetchone.return_value = {
        "knowledge_version_id": kv_id,
        "version_no": 4,
        "source": "server",
        "reason": f"merge:{DUP_WS}: merged company v2",
        "files_written_at": None,
    }
    cur.rowcount = 0
    db = mod.MergeDb()
    targets = [
        {
            "doc_key": "company",
            "knowledge_version_id": kv_id,
            "target_version_no": 4,
        }
    ]
    with pytest.raises(RuntimeError, match="delete dalton_knowledge_versions rowcount=0"):
        await db.verify_and_delete_merged_knowledge_versions(
            cur, CANON_WS, DUP_WS, targets
        )
    executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
    assert any("DISABLE TRIGGER" in s for s in executed_sqls)
    assert not any("ENABLE TRIGGER" in s for s in executed_sqls)


@pytest.mark.asyncio
async def test_lock_timeout_wrapped_into_merge_refused_error(
    tmp_path: Path,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
    )
    db.lock_workspaces_for_update.side_effect = psycopg.errors.LockNotAvailable("lock timeout")

    with pytest.raises(mod.MergeRefusedError, match="lock_timeout"):
        await mod.run_merge(
            dup_ws=DUP_WS,
            canon_ws=CANON_WS,
            apply=True,
            archive_out=str(tmp_path / "never_written.jsonl"),
            db=db,
        )
    assert not (tmp_path / "never_written.jsonl").exists()


def test_cli_exits_nonzero_on_lock_timeout(
    monkeypatch: Any, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
    )
    db.lock_workspaces_for_update.side_effect = psycopg.errors.LockNotAvailable("lock timeout")
    monkeypatch.setattr(mod, "MergeDb", lambda: db)
    rc = mod.main(["--dup", DUP_WS, "--canon", CANON_WS, "--apply"])
    assert rc == 2
    assert "lock_timeout" in capsys.readouterr().err


def test_cli_rejects_revert_combined_with_archive_out() -> None:
    with pytest.raises(SystemExit) as exc_info:
        mod.main(["--revert", "before.jsonl", "--archive-out", "out.jsonl"])
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_knowledge_content_ticker_rewritten_to_canon_unit_when_different() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, canon_kv = _default_knowledge()
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="ANT"),
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
    )
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    plan = await mod.build_merge_plan(
        db, cur, dup_ws=DUP_WS, canon_ws=CANON_WS, tx_now="2026-11-21T08:00:00+00:00"
    )
    by_doc = {item.doc_key: item for item in plan.knowledge_items}

    # 'company' had content['ticker'] == 'ANTH', rewritten to 'ANT'
    company_item = by_doc["company"]
    assert company_item.original_content_ticker == "ANTH"
    assert company_item.content["ticker"] == "ANT"
    _, expected_sha = mod.canonical_json({"ticker": "ANT", "name": "Anthropic"})
    assert company_item.content_sha256 == expected_sha
    # Source dup_kv row must remain untouched
    assert dup_kv["company"]["content"]["ticker"] == "ANTH"
    # 'agenda' has no ticker in content, left unchanged
    assert "ticker" not in by_doc["agenda"].content

    # insert_merged_knowledge_version writes the rewritten content
    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur.fetchone.side_effect = [
        {"version_no": 3},
        {
            "knowledge_version_id": company_item.target_knowledge_version_id,
            "doc_key": "company",
            "version_no": 4,
            "source": "server",
            "ticker": "ANT",
        },
    ]
    sql_cur.rowcount = 1
    await real_db.insert_merged_knowledge_version(
        sql_cur,
        canon_user_id=RICHARD_USER,
        canon_ws=CANON_WS,
        dup_ws=DUP_WS,
        item=company_item,
    )
    _, insert_params = sql_cur.execute.call_args_list[-1].args
    assert insert_params[6].obj["ticker"] == "ANT"
    assert insert_params[7] == expected_sha


@pytest.mark.asyncio
async def test_apply_post_commit_share_recheck_alarms_and_prints_follow_up_notes(
    tmp_path: Path, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-1111-1111-1111-111111111111", is_shared=False)

    # 1. Clean post-commit check
    db_clean = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        post_commit_threads=[{**t1, "workspace_id": CANON_WS, "is_shared": False}],
    )
    res_clean = await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(tmp_path / "clean.jsonl"),
        db=db_clean,
    )
    assert res_clean["post_commit_shared_threads"] == []
    captured_clean = capsys.readouterr()
    assert "CRITICAL ALARM" not in captured_clean.out
    assert "CRITICAL ALARM" not in captured_clean.err
    assert "旧回复的相对文件链接" in captured_clean.out
    assert "执行历史按计算机筛选" in captured_clean.out

    # 2. Concurrent share enabled during merge -> read-only recheck alarms loudly
    shared_after = {
        **t1,
        "workspace_id": CANON_WS,
        "is_shared": True,
        "share_token": "tok-concurrent",
        "share_permissions": {"allow_files": True, "audience": "public"},
        "shared_at": "2026-11-21T08:00:01+00:00",
    }
    connect_ro_flags: list[bool] = []
    db_raced = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        post_commit_threads=[shared_after],
    )
    orig_connect = db_raced.connect.side_effect

    @asynccontextmanager
    async def _recording_connect(*, dsn: str | None = None, read_only: bool = False):
        connect_ro_flags.append(read_only)
        async with orig_connect(dsn=dsn, read_only=read_only) as conn:
            yield conn

    db_raced.connect.side_effect = _recording_connect
    res_raced = await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(tmp_path / "raced.jsonl"),
        db=db_raced,
    )
    assert res_raced["applied"] is True
    assert res_raced["post_commit_shared_threads"] == [t1["conversation_thread_id"]]
    assert connect_ro_flags == [False, True]
    captured_raced = capsys.readouterr()
    assert "CRITICAL ALARM: CONCURRENT THREAD SHARING DETECTED" in captured_raced.out
    assert "CRITICAL ALARM: CONCURRENT THREAD SHARING DETECTED" in captured_raced.err
    assert "tok-concurrent" in captured_raced.err
    assert "SET is_shared = FALSE" in captured_raced.err


# ---------------------------------------------------------------------------
# Round 4 review fixes & invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_rejects_unfinished_outbox_task_report_back_and_restart_resume(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-2222-3333-4444-555555555555")

    # 1. Deferred/pending task_report_back for a completed subagent on dup
    db_task_rb = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        unfinished_outbox_notifications=[
            {
                "hook_outbox_id": "ob-task-1",
                "run_id": "run-parent-1",
                "conversation_thread_id": t1["conversation_thread_id"],
                "hook_type": "task_report_back",
                "status": "pending",
                "attempts": 1,
                "ordering_key": t1["conversation_thread_id"],
                "idempotency_key": "run-parent-1:task:task-1:tr-1:report_back",
                "payload": {"task_id": "task-1", "task_run_id": "tr-1"},
                "next_retry_at": "infinity",
                "is_deferred": True,
                "created_at": "2026-11-21T07:50:00+00:00",
                "completed_at": None,
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="unfinished hook_outbox notification"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_task_rb)
    out_task = capsys.readouterr().out
    assert "hook_type=task_report_back" in out_task
    assert "ob-task-1" in out_task
    assert "task_run_id=tr-1" in out_task
    assert "(deferred)" in out_task

    # 2. Claimed/pending restart_resume on dup
    db_restart = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        unfinished_outbox_notifications=[
            {
                "hook_outbox_id": "ob-restart-1",
                "run_id": "run-stopped-1",
                "conversation_thread_id": t1["conversation_thread_id"],
                "hook_type": "restart_resume",
                "status": "claimed",
                "attempts": 1,
                "ordering_key": f"restart_resume:{t1['conversation_thread_id']}",
                "idempotency_key": "run-stopped-1:restart_resume",
                "payload": {"run_id": "run-stopped-1"},
                "next_retry_at": None,
                "is_deferred": False,
                "created_at": "2026-11-21T07:55:00+00:00",
                "completed_at": None,
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="unfinished hook_outbox notification"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_restart)
    out_restart = capsys.readouterr().out
    assert "hook_type=restart_resume" in out_restart
    assert "ob-restart-1" in out_restart
    assert "run_id=run-stopped-1" in out_restart


@pytest.mark.asyncio
async def test_outbox_preflight_sql_reuses_retry_window_and_allows_expired_dead_rows() -> None:
    real_db = mod.MergeDb()
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.fetchall.return_value = []

    rows = await real_db.fetch_unfinished_outbox_notifications_for_workspace(cur, DUP_WS)
    assert rows == []
    outbox_sql = cur.execute.call_args.args[0]
    assert "status IN ('pending', 'claimed', 'deferred')" in outbox_sql
    assert "o.status = 'dead'" in outbox_sql
    assert "86400.0::float" in outbox_sql
    assert "< 2" in outbox_sql
    assert "o.hook_type = 'watch_clear'" in outbox_sql
    assert "o.attempts < 150" in outbox_sql
    assert "604800" not in outbox_sql
    for payload_key in (
        "thread_id",
        "ptc_thread_id",
        "conversation_thread_id",
        "origin_home_thread_id",
        "run_id",
        "dispatched_run_id",
        "steered_run_id",
        "task_run_id",
        "command_id",
    ):
        assert payload_key in outbox_sql

    # Wakeup & unreported command queries also enforce the dead retry window
    cur_wk = create_autospec(psycopg.AsyncCursor, instance=True)
    cur_wk.fetchall.return_value = []
    await real_db.fetch_open_wakeups_for_workspace(cur_wk, DUP_WS)
    wk_sql = cur_wk.execute.call_args.args[0]
    assert "86400.0::float" in wk_sql
    assert "< 2" in wk_sql

    cur_cmd = create_autospec(psycopg.AsyncCursor, instance=True)
    cur_cmd.fetchall.return_value = []
    await real_db.fetch_unreported_command_runs_for_workspace(cur_cmd, DUP_WS)
    cmd_sql = cur_cmd.execute.call_args.args[0]
    assert "86400.0::float" in cmd_sql
    assert "< 2" in cmd_sql

    # When an expired dead row is filtered out (empty outbox/wakeups/commands), dry-run passes
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db_clean = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        unfinished_outbox_notifications=[],
    )
    dry = await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_clean)
    assert dry["applied"] is False


@pytest.mark.asyncio
async def test_apply_records_db_watermark_without_python_clock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class _ForbidPythonNow(mod.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            raise AssertionError("Python datetime.now() must not be called during --apply")

    monkeypatch.setattr(mod, "datetime", _ForbidPythonNow)

    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        db_now="2026-11-21T09:15:30+00:00",
        canon_max_run_seq=42,
        dup_max_run_seq=99,
    )
    archive_file = tmp_path / "watermark.jsonl"
    result = await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(archive_file),
        db=db,
    )
    assert result["applied"] is True
    records = mod.load_archive_jsonl(archive_file)
    meta = next(r for r in records if r["record_type"] == "meta")
    assert meta["tx_now"] == "2026-11-21T09:15:30+00:00"
    assert meta["created_at"] == "2026-11-21T09:15:30+00:00"
    assert meta["canon_max_run_seq"] == 42
    assert meta["dup_max_run_seq"] == 99

    # Verify MergeDb.fetch_db_watermark SQL selects NOW() and MAX(r.run_seq)
    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur.fetchone.return_value = {
        "db_now": "2026-11-21T09:15:30+00:00",
        "canon_max_run_seq": 42,
        "dup_max_run_seq": 99,
    }
    wm = await real_db.fetch_db_watermark(sql_cur, CANON_WS, DUP_WS)
    assert wm == {
        "db_now": "2026-11-21T09:15:30+00:00",
        "canon_max_run_seq": 42,
        "dup_max_run_seq": 99,
    }
    wm_sql = sql_cur.execute.call_args.args[0]
    assert "NOW() AS db_now" in wm_sql
    assert "MAX(r.run_seq)" in wm_sql


@pytest.mark.asyncio
async def test_revert_refuses_canon_live_run_newer_run_seq_and_dup_new_threads(
    tmp_path: Path,
) -> None:
    archive_file, records = _build_applied_archive(
        tmp_path, canon_max_run_seq=10, dup_max_run_seq=5
    )

    # Gate 1: canon has live run in progress
    rec1 = _TxnRecorder()
    db1 = _make_revert_db(
        records,
        canon_activity={
            "busy": True,
            "has_live_turns_or_subagents": True,
            "live_responses": 1,
            "live_subagents": 0,
            "live_commands": 0,
        },
        txn_recorder=rec1,
    )
    with pytest.raises(mod.MergeRefusedError, match="has live runs in progress"):
        await mod.run_revert(archive_path=archive_file, db=db1)
    assert rec1.rolled_back == 1
    db1.verify_and_delete_merged_knowledge_versions.assert_not_awaited()
    db1.restore_threads.assert_not_awaited()

    # Gate 2: canon has newer response by run_seq (even if created_at <= tx_now)
    rec2 = _TxnRecorder()
    db2 = _make_revert_db(
        records,
        canon_responses_after=[
            {
                "conversation_response_id": "resp-seq-11",
                "conversation_thread_id": "22222222-2222-2222-2222-222222222222",
                "turn_index": 2,
                "run_seq": 11,
                "status": "completed",
                "created_at": "2026-11-21T07:59:59+00:00",
            }
        ],
        txn_recorder=rec2,
    )
    with pytest.raises(mod.MergeRefusedError, match="run_seq>10"):
        await mod.run_revert(archive_path=archive_file, db=db2)
    assert rec2.rolled_back == 1
    kwargs = db2.fetch_canon_responses_after.call_args.kwargs
    assert kwargs["canon_max_run_seq"] == 10
    assert kwargs["dup_max_run_seq"] == 5

    # Verify SQL in MergeDb.fetch_canon_responses_after uses run_seq
    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    sql_cur.fetchall.return_value = []
    await real_db.fetch_canon_responses_after(
        sql_cur,
        CANON_WS,
        since_ts="2026-11-21T08:00:00+00:00",
        canon_max_run_seq=10,
        dup_max_run_seq=5,
        migrated_thread_ids=["11111111-1111-1111-1111-111111111111"],
    )
    resp_sql = sql_cur.execute.call_args.args[0]
    assert "r.run_seq > %(canon_max_run_seq)s" in resp_sql
    assert "r.run_seq > %(dup_max_run_seq)s" in resp_sql

    # Gate 3: dup has newly created thread after merge
    rec3 = _TxnRecorder()
    db3 = _make_revert_db(
        records,
        dup_post_threads=[_make_thread("99999999-0000-0000-0000-000000000001", thread_index=0)],
        txn_recorder=rec3,
    )
    with pytest.raises(mod.MergeRefusedError, match="created after merge"):
        await mod.run_revert(archive_path=archive_file, db=db3)
    assert rec3.rolled_back == 1
    db3.verify_and_delete_merged_knowledge_versions.assert_not_awaited()
    db3.restore_threads.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_fsyncs_archive_file_and_parent_directories_before_db_writes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
    )
    events: list[tuple[str, str]] = []
    real_fsync = mod.os.fsync
    real_fsync_dir = mod._fsync_dir

    def _spy_fsync(fd: int) -> None:
        events.append(("file_or_dir_fsync", str(fd)))
        real_fsync(fd)

    def _spy_fsync_dir(dir_path: Path) -> None:
        events.append(("dir_fsync", str(dir_path)))
        real_fsync_dir(dir_path)

    async def _spy_move_threads(*args: Any, **kwargs: Any) -> int:
        events.append(("db_move_threads", ""))
        return 1

    monkeypatch.setattr(mod.os, "fsync", _spy_fsync)
    monkeypatch.setattr(mod, "_fsync_dir", _spy_fsync_dir)
    db.move_threads.side_effect = _spy_move_threads

    parent_existing = tmp_path / "backups"
    parent_existing.mkdir()
    new_dir = parent_existing / "merge-ws-new"
    archive_file = new_dir / "before.jsonl"

    result = await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(archive_file),
        db=db,
    )
    assert result["applied"] is True
    dir_events = [e for e in events if e[0] == "dir_fsync"]
    assert dir_events == [
        ("dir_fsync", str(new_dir)),
        ("dir_fsync", str(parent_existing)),
    ]
    idx_file_fsync = next(i for i, e in enumerate(events) if e[0] == "file_or_dir_fsync")
    idx_dir_new = events.index(("dir_fsync", str(new_dir)))
    idx_dir_parent = events.index(("dir_fsync", str(parent_existing)))
    idx_db_write = events.index(("db_move_threads", ""))
    assert idx_file_fsync < idx_dir_new < idx_dir_parent < idx_db_write


def test_apply_post_commit_share_recheck_failure_prints_applied_and_exits_3(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-1111-1111-1111-111111111111")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
    )
    db.fetch_threads_by_ids.side_effect = psycopg.OperationalError("read-only check timed out")
    monkeypatch.setattr(mod, "MergeDb", lambda: db)
    archive_file = tmp_path / "recheck_fail.jsonl"

    rc = mod.main(
        [
            "--dup",
            DUP_WS,
            "--canon",
            CANON_WS,
            "--apply",
            "--archive-out",
            str(archive_file),
        ]
    )
    assert rc == mod.EXIT_POST_COMMIT_RECHECK_FAILED
    assert rc == 3
    captured = capsys.readouterr()
    assert "APPLIED:" in captured.out
    assert "conversation_threads: 1" in captured.out
    assert f"Archive path: {archive_file}" in captured.out
    assert "分享状态未能复查" in captured.out
    assert "分享状态未能复查" in captured.err
    assert "SELECT conversation_thread_id" in captured.out
    assert "SELECT conversation_thread_id" in captured.err
    help_text = mod.build_parser().format_help()
    assert "3" in help_text
    assert "分享状态未能复查" in help_text


def test_deadlock_detected_wraps_error_cleans_archive_and_exits_nonzero(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    import asyncio as _asyncio

    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    rec_apply = _TxnRecorder()
    db_apply = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        txn_recorder=rec_apply,
    )
    # Fail at upsert_canon_sharing AFTER archive file was written
    db_apply.upsert_canon_sharing.side_effect = psycopg.errors.DeadlockDetected(
        "deadlock detected"
    )
    archive_file = tmp_path / "deadlock_aborted.jsonl"

    with monkeypatch.context() as mp:
        mp.setattr(mod, "MergeDb", lambda: db_apply)
        rc = mod.main(
            [
                "--dup",
                DUP_WS,
                "--canon",
                CANON_WS,
                "--apply",
                "--archive-out",
                str(archive_file),
            ]
        )
    assert rc == 2
    assert rec_apply.rolled_back == 1
    assert rec_apply.committed == 0
    assert not archive_file.exists()
    err = capsys.readouterr().err
    assert "deadlock detected" in err
    assert "rolled back" in err

    # Also verify --revert wraps DeadlockDetected cleanly
    valid_archive, records = _build_applied_archive(tmp_path)
    rec_rev = _TxnRecorder()
    db_rev = _make_revert_db(records, txn_recorder=rec_rev)
    db_rev.restore_sharing.side_effect = psycopg.errors.DeadlockDetected("deadlock detected")
    with pytest.raises(mod.MergeRefusedError, match="deadlock detected during --revert"):
        _asyncio.run(mod.run_revert(archive_path=valid_archive, db=db_rev))
    assert rec_rev.rolled_back == 1


@pytest.mark.asyncio
async def test_dry_run_lists_queued_file_comments_without_refusing(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-2222-3333-4444-555555555555")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        queued_file_comments=[
            {
                "comment_id": "cmt-queued-1",
                "artifact_id": "art-dup-1",
                "number": 4,
                "path": "notes/anthropic_memo.md",
                "title": "Anthropic Memo",
                "status": "open",
                "delivery": "queued",
                "user_id": CY_USER,
                "thread_id": t1["conversation_thread_id"],
                "artifact_workspace_id": DUP_WS,
                "thread_title": "CY thread",
            }
        ],
    )
    res = await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db)
    assert res["applied"] is False
    out = capsys.readouterr().out
    assert "[artifact_comments(queued)]" in out
    assert "cmt-queued-1" in out
    assert "notes/anthropic_memo.md" in out
    assert "#4" in out


@pytest.mark.asyncio
async def test_knowledge_content_ticker_uses_ticker_key_for_brk_b_and_skips_invalid(
    capsys: Any,
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, _ = _default_knowledge()

    # 1. BRK-B fails ^[A-Z0-9.]{1,12}$ so rewrite falls back to ticker_key ('BRK.B')
    db_brk = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="BRK-B", ticker_key="BRK.B"),
        dup_latest_kv={"company": copy.deepcopy(dup_kv["company"])},
    )
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    plan_brk = await mod.build_merge_plan(
        db_brk, cur, dup_ws=DUP_WS, canon_ws=CANON_WS
    )
    item_brk = plan_brk.knowledge_items[0]
    assert item_brk.original_content_ticker == "ANTH"
    assert item_brk.content["ticker"] == "BRK.B"
    assert mod._matches_company_ticker_schema(item_brk.content["ticker"]) is True
    assert item_brk.ticker == "BRK-B"
    assert item_brk.ticker_rewrite_note is None

    # 2. When neither ticker nor ticker_key matches schema, do NOT rewrite and print explanation
    db_bad = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="INVALID-TICKER-TOO-LONG", ticker_key="BAD!KEY"),
        dup_latest_kv={"company": copy.deepcopy(dup_kv["company"])},
    )
    plan_bad = await mod.build_merge_plan(
        db_bad, cur, dup_ws=DUP_WS, canon_ws=CANON_WS
    )
    item_bad = plan_bad.knowledge_items[0]
    assert item_bad.content["ticker"] == "ANTH"
    assert item_bad.original_content_ticker is None
    assert item_bad.ticker_rewrite_note is not None
    assert "NOT rewritten" in item_bad.ticker_rewrite_note
    out_bad = capsys.readouterr().out
    assert "NOT rewritten" in out_bad


# ---------------------------------------------------------------------------
# Round 5 review fixes (final narrow round)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_and_apply_reject_schema_invalid_latest_knowledge(
    tmp_path: Path, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, canon_kv = _default_knowledge()
    dup_kv["company"]["schema_valid"] = False
    dup_kv["company"]["version_no"] = 7
    dup_kv["company"]["source"] = "direct_edit"
    dup_kv["company"]["edited_path"] = "data/company.json"

    # 1. Dry-run preflight rejects and prints doc_key, version_no, and remediation
    db_dry = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
    )
    with pytest.raises(mod.MergeRefusedError) as exc_dry:
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db_dry)
    msg_dry = str(exc_dry.value)
    out_dry = capsys.readouterr().out
    for text in (msg_dry, out_dry):
        assert "company" in text
        assert "v7" in text
        assert "schema_valid=False" in text or "schema_valid=false" in text
        assert "先在副本" in text
        assert "没有跳过某份文档的选项" in text

    # 2. --apply locked plan also rejects before writing archive or mutating DB
    rec = _TxnRecorder()
    db_apply = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
        dup_latest_kv=dup_kv,
        canon_latest_kv=canon_kv,
        txn_recorder=rec,
    )
    archive_file = tmp_path / "invalid_kv_aborted.jsonl"
    with pytest.raises(mod.MergeRefusedError, match="schema-invalid latest knowledge"):
        await mod.run_merge(
            dup_ws=DUP_WS,
            canon_ws=CANON_WS,
            apply=True,
            archive_out=str(archive_file),
            db=db_apply,
        )
    assert rec.entered == 1
    assert rec.rolled_back == 1
    assert rec.committed == 0
    assert not archive_file.exists()
    db_apply.move_threads.assert_not_awaited()
    db_apply.insert_merged_knowledge_version.assert_not_awaited()
    db_apply.upsert_canon_sharing.assert_not_awaited()
    db_apply.mark_dup_historical_duplicate.assert_not_awaited()


@pytest.mark.asyncio
async def test_knowledge_content_ticker_normalizes_ticker_key_and_rewrites_case_or_spelling() -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    dup_kv, canon_kv = _default_knowledge()
    cur = create_autospec(psycopg.AsyncCursor, instance=True)

    # 1. Lowercase 'ant' matches normalized ticker_key 'ANT' -> must still rewrite to 'ANT'
    dup_lower = copy.deepcopy(dup_kv)
    dup_lower["company"]["content"] = {"ticker": "ant", "name": "Anthropic"}
    db_lower = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="ANT", ticker_key="ANT"),
        dup_latest_kv={"company": dup_lower["company"]},
        canon_latest_kv=canon_kv,
    )
    plan_lower = await mod.build_merge_plan(
        db_lower, cur, dup_ws=DUP_WS, canon_ws=CANON_WS
    )
    item_lower = plan_lower.knowledge_items[0]
    assert item_lower.original_content_ticker == "ant"
    assert item_lower.content["ticker"] == "ANT"
    assert mod._matches_company_ticker_schema(item_lower.content["ticker"]) is True
    _, expected_sha = mod.canonical_json({"ticker": "ANT", "name": "Anthropic"})
    assert item_lower.content_sha256 == expected_sha

    # 2. Lowercase 'brk-b' matches normalized ticker_key 'BRK.B' -> rewritten to schema-compliant 'BRK.B'
    dup_brk = copy.deepcopy(dup_kv)
    dup_brk["company"]["content"] = {"ticker": "brk-b", "name": "Berkshire"}
    db_brk = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="BRK-B", ticker_key="BRK.B"),
        dup_latest_kv={"company": dup_brk["company"]},
    )
    plan_brk = await mod.build_merge_plan(db_brk, cur, dup_ws=DUP_WS, canon_ws=CANON_WS)
    item_brk = plan_brk.knowledge_items[0]
    assert item_brk.original_content_ticker == "brk-b"
    assert item_brk.content["ticker"] == "BRK.B"

    # 3. Lowercase '0700.hk' matches normalized ticker_key '00700' -> rewritten to '0700.HK'
    dup_hk = copy.deepcopy(dup_kv)
    dup_hk["company"]["content"] = {"ticker": "0700.hk", "name": "Tencent"}
    db_hk = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="0700.HK", ticker_key="00700"),
        dup_latest_kv={"company": dup_hk["company"]},
    )
    plan_hk = await mod.build_merge_plan(db_hk, cur, dup_ws=DUP_WS, canon_ws=CANON_WS)
    item_hk = plan_hk.knowledge_items[0]
    assert item_hk.original_content_ticker == "0700.hk"
    assert item_hk.content["ticker"] == "0700.HK"

    # 4. Exact schema-compliant match ('ANT' == 'ANT') does NOT rewrite
    dup_exact = copy.deepcopy(dup_kv)
    dup_exact["company"]["content"] = {"ticker": "ANT", "name": "Anthropic"}
    db_exact = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(ticker="ANT", ticker_key="ANT"),
        dup_latest_kv={"company": dup_exact["company"]},
    )
    plan_exact = await mod.build_merge_plan(
        db_exact, cur, dup_ws=DUP_WS, canon_ws=CANON_WS
    )
    item_exact = plan_exact.knowledge_items[0]
    assert item_exact.original_content_ticker is None
    assert item_exact.content["ticker"] == "ANT"


@pytest.mark.asyncio
async def test_follow_up_notes_use_actionable_web_panel_steps_without_agent_copy(
    tmp_path: Path, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[_make_thread()],
    )

    # Dry-run output
    await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db)
    dry_out = capsys.readouterr().out
    # Apply output
    await mod.run_merge(
        dup_ws=DUP_WS,
        canon_ws=CANON_WS,
        apply=True,
        archive_out=str(tmp_path / "notes.jsonl"),
        db=db,
    )
    apply_out = capsys.readouterr().out

    for out in (dry_out, apply_out):
        assert "副本工作区的文件留在原处，不随对话迁移" in out
        assert "不要用网页文件面板上传" in out
        assert "cy-anthropic/ 目录（目录名取自副本工作区名的 slug）" in out
        assert "直接到已共享的副本工作区阅读" in out
        assert "Chief of Staff" not in out
        assert "分析师" not in out


def test_post_commit_prints_applied_before_recheck_and_sets_statement_timeout(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-1111-1111-1111-111111111111")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
    )
    events: list[str] = []
    orig_connect = db.connect.side_effect

    @asynccontextmanager
    async def _tracking_connect(*, dsn: str | None = None, read_only: bool = False):
        if read_only:
            # APPLIED + counts + archive path must already be flushed to stdout
            # BEFORE opening the post-commit recheck connection.
            so_far = capsys.readouterr()
            events.append("recheck_connect")
            assert "APPLIED:" in so_far.out
            assert "conversation_threads: 1" in so_far.out
            assert "Archive path:" in so_far.out
            # Re-emit captured output so main's later assertions / caller see it too.
            print(so_far.out, end="")
        async with orig_connect(dsn=dsn, read_only=read_only) as conn:
            yield conn

    async def _spy_set_stmt_timeout(cur: Any, timeout: str = mod.POST_COMMIT_RECHECK_TIMEOUT) -> None:
        events.append(f"set_statement_timeout:{timeout}")

    async def _timed_out_fetch(cur: Any, thread_ids: list[str]) -> list[dict[str, Any]]:
        events.append("fetch_threads_by_ids")
        raise psycopg.errors.QueryCanceled(
            "canceling statement due to statement timeout"
        )

    db.connect.side_effect = _tracking_connect
    db.set_statement_timeout.side_effect = _spy_set_stmt_timeout
    db.fetch_threads_by_ids.side_effect = _timed_out_fetch
    archive_file = tmp_path / "timeout_recheck.jsonl"

    with monkeypatch.context() as mp:
        mp.setattr(mod, "MergeDb", lambda: db)
        rc = mod.main(
            [
                "--dup",
                DUP_WS,
                "--canon",
                CANON_WS,
                "--apply",
                "--archive-out",
                str(archive_file),
            ]
        )
    assert rc == mod.EXIT_POST_COMMIT_RECHECK_FAILED
    assert events == [
        "recheck_connect",
        "set_statement_timeout:10s",
        "fetch_threads_by_ids",
    ]
    captured = capsys.readouterr()
    assert "APPLIED:" in captured.out
    assert "分享状态未能复查" in captured.out
    assert "分享状态未能复查" in captured.err
    assert "statement timeout" in captured.err

    # Also verify MergeDb.set_statement_timeout executes exact SQL literal
    import asyncio as _asyncio

    real_db = mod.MergeDb()
    sql_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    _asyncio.run(real_db.set_statement_timeout(sql_cur, "10s"))
    assert sql_cur.execute.call_args.args == ("SET statement_timeout = '10s'",)


@pytest.mark.asyncio
async def test_preflight_rejects_expired_retention_watch_clear_dead_row_below_attempt_ceiling(
    capsys: Any,
) -> None:
    real_db = mod.MergeDb()
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.fetchall.return_value = []
    await real_db.fetch_unfinished_outbox_notifications_for_workspace(cur, DUP_WS)
    outbox_sql = cur.execute.call_args.args[0]

    # watch_clear branch must check attempts < 150 with NO 7-day retention cutoff
    watch_clear_section = outbox_sql.split("o.hook_type = 'watch_clear'")[1]
    assert "o.attempts < 150" in watch_clear_section
    assert "604800" not in watch_clear_section
    assert "completed_at" not in watch_clear_section

    canon_ws = _make_workspace(CANON_WS, user_id=RICHARD_USER, name="ANT")
    dup_ws = _make_workspace(DUP_WS, user_id=CY_USER, name="Anthropic")
    t1 = _make_thread("11111111-2222-3333-4444-555555555555")
    db = _make_autospec_db(
        dup_ws=dup_ws,
        canon_ws=canon_ws,
        canon_unit=_make_unit(),
        threads=[t1],
        unfinished_outbox_notifications=[
            {
                "hook_outbox_id": "ob-watch-clear-old",
                "run_id": "run-old-1",
                "conversation_thread_id": t1["conversation_thread_id"],
                "hook_type": "watch_clear",
                "status": "dead",
                "attempts": 5,
                "ordering_key": t1["conversation_thread_id"],
                "idempotency_key": "run-old-1:watch_clear",
                "payload": {"thread_id": t1["conversation_thread_id"]},
                "next_retry_at": None,
                "is_deferred": False,
                "created_at": "2026-11-01T00:00:00+00:00",
                "completed_at": "2026-11-01T00:05:00+00:00",
            }
        ],
    )
    with pytest.raises(mod.MergeRefusedError, match="unfinished hook_outbox notification"):
        await mod.run_merge(dup_ws=DUP_WS, canon_ws=CANON_WS, apply=False, db=db)
    out = capsys.readouterr().out
    assert "hook_type=watch_clear" in out
    assert "ob-watch-clear-old" in out
    assert "status=dead" in out
    assert "attempts=5" in out
