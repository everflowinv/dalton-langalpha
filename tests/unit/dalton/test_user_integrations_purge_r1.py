"""Unit tests for Personal Calendar & Mail Batch 1 (r1):
migration d06X, data layer, 2A purger, purge fence, remote revoke state machine,
archive/memory skip, restore reset SQL, and 4-layer callback query redaction.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from ptc_agent.agent.middleware.compaction.utils import (
    COMPACTION_SUMMARY_KEY,
    SIDECAR_KEY,
    SUMMARIZED_KEY,
)
from src.dalton.auth.dispatch import SensitiveQueryLogFilter, redact_sensitive_query_params
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    PurgeLedgerRow,
    RemoteRevokeStatus,
    TriggerReason,
    extract_google_project,
    lifecycle_lock_key,
    refresh_lock_key,
    revoke_lock_key,
)
from src.dalton.migrations.versions import d06X_user_integrations as d06x_mod
from src.dalton.services import integration_data_purge as purge_mod
from src.dalton.services import integration_revoke as revoke_mod
from src.dalton.services import purge_fence
from src.dalton.services.integration_data_purge import (
    PURGED_PLACEHOLDER,
    PURGED_TOOL_ARGS,
    PURGED_TOOL_ARGS_JSON,
    collect_redis_keys_for_suffix,
    redact_interrupt_payload,
    redact_message,
    redact_ui_record,
    register_tracker_purge_handler,
)
from src.dalton.services.integration_revoke import (
    RemoteRevoker,
    RevokeOutcome,
    compute_revoke_backoff_seconds,
    process_revoke,
    revoke_before_new_authorization,
    run_integration_revoke_retry_loop,
    supersede_pending_revokes,
)
from src.dalton.services.purge_fence import (
    PURGE_IN_PROGRESS_CODE,
    PersonalDataPurgeInProgressError,
    assert_fork_source_not_purging_tx,
    assert_thread_not_purging_tx,
    check_thread_not_purging_fast,
    clip_purged_turn_ranges,
    fork_max_inclusive_turn,
    is_turn_in_purged_ranges,
    merge_turn_range,
    normalize_turn_ranges,
    save_stored_transcript_fenced,
    should_skip_memory_extraction_for_thread,
    verify_writeback_fence_tx,
)
from src.observability.private_query import (
    AccessLogQueryFilter,
    drop_span_query,
    is_private,
)
from src.server.services.forks.model import BeforeTurn, ForkRefused, ThroughTurn

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Helpers: lightweight fake async DB cursor/connection
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, fetchone_seq=None, fetchall_seq=None, rowcount: int = 1):
        self._fetchone_seq = list(fetchone_seq or [])
        self._fetchall_seq = list(fetchall_seq or [])
        self.executed: list[tuple[str, object]] = []
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._fetchone_seq.pop(0) if self._fetchone_seq else None

    async def fetchall(self):
        return self._fetchall_seq.pop(0) if self._fetchall_seq else []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor | None = None):
        self._cursor = cursor or _FakeCursor()
        self.executed: list[tuple[str, object]] = []

    def cursor(self, **_kw):
        return self._cursor

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return self._cursor


# ---------------------------------------------------------------------------
# 1. Migration d06X structure & reversibility
# ---------------------------------------------------------------------------


def test_d06X_migration_revisions_and_sql_objects():
    assert d06x_mod.revision == "d06X"
    assert d06x_mod.down_revision == "d057"

    upgrade_sqls: list[str] = []
    downgrade_sqls: list[str] = []

    with patch.object(d06x_mod.op, "execute", side_effect=upgrade_sqls.append):
        d06x_mod.upgrade()
    with patch.object(d06x_mod.op, "execute", side_effect=downgrade_sqls.append):
        d06x_mod.downgrade()

    up_joined = "\n".join(upgrade_sqls)
    down_joined = "\n".join(downgrade_sqls)

    for obj in (
        "dalton_integration_auth_epoch_seq",
        "dalton_user_integrations",
        "dalton_user_integration_oauth_states",
        "dalton_user_integration_pending_confirmations",
        "dalton_integration_action_approvals",
        "dalton_thread_integration_touches",
        "dalton_integration_purge_ledger",
        "dalton_integration_webhook_events",
        "has_personal_integration_data",
        "personal_purge_in_progress",
        "personal_purge_generation",
        "personal_purged_turn_ranges",
        "authorized_at",
        "auth_epoch",
        "local_purge_status",
        "remote_revoke_status",
    ):
        assert obj in up_joined, f"missing {obj} in upgrade()"
        if obj not in ("authorized_at", "auth_epoch", "local_purge_status", "remote_revoke_status"):
            assert obj in down_joined, f"missing {obj} in downgrade()"


# ---------------------------------------------------------------------------
# 2. Advisory lock keys & safe repr (no token leakage)
# ---------------------------------------------------------------------------


def test_lock_keys_and_google_project_extraction():
    assert extract_google_project("123456789012-abcxyz.apps.googleusercontent.com") == "123456789012"
    assert extract_google_project("custom-client-id") == "custom-client-id"
    with pytest.raises(ValueError):
        extract_google_project("   ")

    # Google shares refresh lock across services under same (oauth_client_id, sub)
    k_g_cal = refresh_lock_key("u1", "google", "123-c.apps.googleusercontent.com", "sub-1", "calendar")
    k_g_mail = refresh_lock_key("u1", "google", "123-c.apps.googleusercontent.com", "sub-1", "mail")
    assert k_g_cal == k_g_mail

    # Feishu has distinct refresh locks per service
    k_f_cal = refresh_lock_key("u1", "feishu", "cli_app", "ou_1", "calendar")
    k_f_mail = refresh_lock_key("u1", "feishu", "cli_app", "ou_1", "mail")
    assert k_f_cal != k_f_mail
    with pytest.raises(ValueError):
        refresh_lock_key("u1", "feishu", "cli_app", "ou_1", None)

    assert lifecycle_lock_key("u1", "google") != lifecycle_lock_key("u1", "feishu")
    assert revoke_lock_key("u1", "123456789012", "sub-1") != revoke_lock_key("u1", "999999999999", "sub-1")


def test_dataclasses_hide_secret_tokens_in_repr():
    now = datetime.now(UTC)
    row = IntegrationRow(
        user_id="u1",
        provider="google",
        service="calendar",
        oauth_client_id="123-abc.apps.googleusercontent.com",
        provider_account_id="sub-1",
        provider_union_id=None,
        provider_email="a@example.com",
        granted_scopes="openid email",
        auth_epoch=1,
        authorized_at=now,
        token_generation=1,
        status=IntegrationStatus.CONNECTED,
        refresh_inflight_gen=None,
        refresh_started_at=None,
        expires_at=now,
        last_refresh_at=None,
        needs_reauth_at=None,
        last_error_code=None,
        reauth_notified_at=None,
        preferences={},
        has_access_token=True,
        has_refresh_token=True,
        created_at=now,
        updated_at=now,
        access_token="SECRET_ACCESS_TOKEN_XYZ",
        refresh_token="SECRET_REFRESH_TOKEN_XYZ",
    )
    rendered = repr(row)
    assert "SECRET_ACCESS_TOKEN_XYZ" not in rendered
    assert "SECRET_REFRESH_TOKEN_XYZ" not in rendered

    ledger = PurgeLedgerRow(
        purge_id=str(uuid.uuid4()),
        user_id="u1",
        provider="google",
        service="calendar",
        oauth_client_id="123-abc.apps.googleusercontent.com",
        provider_account_id="sub-1",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={},
        tracker_reconcile_needed=False,
        local_completed_at=None,
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        has_revoke_token=True,
        retry_count=0,
        next_retry_at=None,
        last_remote_error=None,
        remote_completed_at=None,
        disconnected_at=now,
        completed_at=None,
        revoke_token="SECRET_REVOKE_TOKEN_XYZ",
    )
    assert "SECRET_REVOKE_TOKEN_XYZ" not in repr(ledger)


# ---------------------------------------------------------------------------
# 3. Closed turn-range arithmetic (truncate & Fork ThroughTurn / BeforeTurn)
# ---------------------------------------------------------------------------


def test_closed_turn_ranges_merge_membership_and_clipping():
    ranges = merge_turn_range([], 2, 5)
    assert ranges == [[2, 5]]
    assert not is_turn_in_purged_ranges(1, ranges)
    assert is_turn_in_purged_ranges(2, ranges)
    assert is_turn_in_purged_ranges(5, ranges)
    assert not is_turn_in_purged_ranges(6, ranges)

    merged = merge_turn_range(ranges, 6, 8)
    assert merged == [[2, 8]]
    merged = merge_turn_range(merged, 10, 12)
    assert merged == [[2, 8], [10, 12]]

    # Truncate from_turn_index=4 -> clip to <= 3
    assert clip_purged_turn_ranges(merged, 3) == [[2, 3]]
    assert clip_purged_turn_ranges(merged, 1) == []
    assert clip_purged_turn_ranges(merged, -1) == []

    # Fork cut boundaries match copied turn_indexes exactly
    assert fork_max_inclusive_turn(ThroughTurn(4), [0, 1, 2, 3, 4]) == 4
    assert clip_purged_turn_ranges([[2, 6]], fork_max_inclusive_turn(ThroughTurn(4), [0, 1, 2, 3, 4])) == [[2, 4]]
    assert fork_max_inclusive_turn(BeforeTurn(4), [0, 1, 2, 3]) == 3
    assert clip_purged_turn_ranges([[2, 6]], fork_max_inclusive_turn(BeforeTurn(4), [0, 1, 2, 3])) == [[2, 3]]
    assert fork_max_inclusive_turn(BeforeTurn(2), [0, 1]) == 1
    assert clip_purged_turn_ranges([[2, 6]], fork_max_inclusive_turn(BeforeTurn(2), [0, 1])) == []
    assert fork_max_inclusive_turn(BeforeTurn(0), []) is None
    assert normalize_turn_ranges("invalid") == []


# ---------------------------------------------------------------------------
# 4. Scheme 2A redaction: messages, tool_calls, 3 compaction summary copies, interrupts, Redis keys
# ---------------------------------------------------------------------------


def test_2a_redaction_preserves_pre_touch_and_purges_suffix_and_tool_calls():
    pre_human = HumanMessage(content="NVDA revenue analysis", id="m0")
    out_pre, changed_pre = redact_message(
        pre_human, in_suffix=False, affected_summary_ids=set(), min_turn=1, max_turn=3
    )
    assert not changed_pre
    assert out_pre.content == "NVDA revenue analysis"

    suf_human = HumanMessage(
        content="Check my secret dentist appointment",
        id="m1",
        additional_kwargs={"steering_delivered": {"content": "Also check mom's birthday"}},
    )
    out_h, changed_h = redact_message(
        suf_human, in_suffix=True, affected_summary_ids=set(), min_turn=1, max_turn=3
    )
    assert changed_h
    assert out_h.content == PURGED_PLACEHOLDER
    assert out_h.additional_kwargs["steering_delivered"]["content"] == PURGED_PLACEHOLDER

    suf_ai = AIMessage(
        content=[
            {"type": "thinking", "thinking": "private chain of thought"},
            {"type": "text", "text": "Your dentist is at 3pm"},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "dalton_calendar",
                "input": {"calendar_id": "secret@gmail.com"},
            },
        ],
        id="m2",
        tool_calls=[
            {
                "id": "call_1",
                "name": "dalton_calendar",
                "type": "tool_call",
                "args": {"calendar_id": "secret@gmail.com"},
            }
        ],
        invalid_tool_calls=[
            {
                "id": "call_bad",
                "name": "dalton_calendar",
                "type": "invalid_tool_call",
                "args": '{"calendar_id": "bad"}',
                "error": "parse error",
            }
        ],
        additional_kwargs={
            "reasoning_content": "secret reasoning",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "dalton_calendar", "arguments": '{"secret": 1}'},
                }
            ],
        },
    )
    out_ai, changed_ai = redact_message(
        suf_ai, in_suffix=True, affected_summary_ids=set(), min_turn=1, max_turn=3
    )
    assert changed_ai
    assert out_ai.content[0]["thinking"] == PURGED_PLACEHOLDER
    assert out_ai.content[1]["text"] == PURGED_PLACEHOLDER
    assert out_ai.content[2] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "dalton_calendar",
        "input": PURGED_TOOL_ARGS,
    }
    assert out_ai.tool_calls == [
        {"id": "call_1", "name": "dalton_calendar", "type": "tool_call", "args": PURGED_TOOL_ARGS}
    ]
    assert out_ai.invalid_tool_calls[0]["id"] == "call_bad"
    assert out_ai.invalid_tool_calls[0]["name"] == "dalton_calendar"
    assert out_ai.invalid_tool_calls[0]["args"] == PURGED_TOOL_ARGS_JSON
    assert out_ai.additional_kwargs["reasoning_content"] == PURGED_PLACEHOLDER
    assert out_ai.additional_kwargs["tool_calls"][0]["function"]["arguments"] == PURGED_TOOL_ARGS_JSON

    suf_tool = ToolMessage(
        content="Event: Private Board Meeting",
        tool_call_id="call_1",
        id="m3",
        artifact={"raw": "secret"},
    )
    out_tool, changed_tool = redact_message(
        suf_tool, in_suffix=True, affected_summary_ids=set(), min_turn=1, max_turn=3
    )
    assert changed_tool
    assert out_tool.content == PURGED_PLACEHOLDER
    assert out_tool.artifact is None


def test_2a_cross_boundary_compaction_summary_three_copies():
    """Compaction summary spanning [0, 2] on a pre-touch checkpoint (min_turn=1)
    must redact all 3 copies: summary message, AIMessage response_metadata.context_summary,
    and UI record metadata.replaced_summary."""
    summary_id = "sum-cross-1"

    # Copy 1: HumanMessage(lc_source='summarization') on pre-touch checkpoint
    sum_msg = HumanMessage(
        content="Context summary: user has dentist at 3pm",
        id=summary_id,
        additional_kwargs={
            "lc_source": "summarization",
            "summarize_complete": {"summary_length": 40, "original_message_count": 6},
            SUMMARIZED_KEY: {"span": [0, 2], "gap": None, "skills": []},
            SIDECAR_KEY: {"native": "secret encrypted compaction block"},
        },
    )
    out_sum, ch1 = redact_message(
        sum_msg,
        in_suffix=False,
        affected_summary_ids={summary_id},
        min_turn=1,
        max_turn=2,
    )
    assert ch1
    assert out_sum.content == PURGED_PLACEHOLDER
    assert SIDECAR_KEY not in out_sum.additional_kwargs
    assert out_sum.additional_kwargs["summarize_complete"]["summary_length"] == len(PURGED_PLACEHOLDER)

    # Copy 2: AIMessage.response_metadata.context_summary.summary_text on pre-touch checkpoint
    ai_with_cs = AIMessage(
        content="Public NVDA commentary",
        id="ai-pre-0",
        response_metadata={
            COMPACTION_SUMMARY_KEY: {
                "summary_message_id": summary_id,
                "summary_text": "Summary mentioning private calendar event",
                "summary_length": 41,
            }
        },
    )
    out_ai, ch2 = redact_message(
        ai_with_cs,
        in_suffix=False,
        affected_summary_ids={summary_id},
        min_turn=1,
        max_turn=2,
    )
    assert ch2
    assert out_ai.content == "Public NVDA commentary"  # pre-touch reply kept
    assert out_ai.response_metadata[COMPACTION_SUMMARY_KEY]["summary_text"] == PURGED_PLACEHOLDER

    # Copy 3: UI record metadata.replaced_summary.summary_text
    ui_rec = {
        "type": "ui",
        "id": "ui-abc",
        "name": "context_window",
        "props": {"action": "summarize"},
        "metadata": {
            "summary_message_id": "sum-next",
            "replaced_summary": {
                "id": summary_id,
                "action": "summarize",
                "summary_text": "Replaced summary with private schedule",
                "summary_length": 38,
            },
        },
    }
    out_ui, ch3 = redact_ui_record(
        ui_rec, in_suffix=False, affected_summary_ids={summary_id}
    )
    assert ch3
    assert out_ui["metadata"]["replaced_summary"]["summary_text"] == PURGED_PLACEHOLDER


def test_2a_interrupt_redaction_and_redis_key_builders():
    payload = {
        "action_requests": [
            {
                "id": "act-1",
                "type": "dalton_calendar_create_event",
                "name": "dalton_calendar",
                "args": {"title": "Secret meeting", "attendees": ["boss@co.com"]},
            }
        ],
        "prompt": "Confirm creating Secret meeting?",
    }
    redacted = redact_interrupt_payload(payload)
    assert redacted["action_requests"][0]["id"] == "act-1"
    assert redacted["action_requests"][0]["type"] == "dalton_calendar_create_event"
    assert redacted["action_requests"][0]["args"] == PURGED_TOOL_ARGS
    assert redacted["prompt"] == PURGED_PLACEHOLDER

    keys = collect_redis_keys_for_suffix("tid-1", ["run-1"], ["task-1"], ["trun-1"])
    assert "workflow:stream:tid-1:run-1" in keys
    assert "workflow:events:meta:tid-1:run-1" in keys
    assert "subagent:stream:tid-1:task-1" in keys
    assert "subagent:events:meta:tid-1:task-1" in keys
    assert "subagent:events:tid-1:task-1" in keys
    assert "subagent:stream:tid-1:trun-1" in keys


# ---------------------------------------------------------------------------
# 5. Remote revocation state machine (using create_autospec(RemoteRevoker))
# ---------------------------------------------------------------------------


def _sample_ledger(
    *,
    purge_id: str | None = None,
    remote_status: RemoteRevokeStatus = RemoteRevokeStatus.PENDING,
    revoke_token: str | None = "rt_secret",
    retry_count: int = 0,
) -> PurgeLedgerRow:
    now = datetime.now(UTC)
    return PurgeLedgerRow(
        purge_id=purge_id or str(uuid.uuid4()),
        user_id="user-1",
        provider="google",
        service="calendar",
        oauth_client_id="123456789012-client.apps.googleusercontent.com",
        provider_account_id="sub-abc",
        auth_epoch_upto=10,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        tracker_reconcile_needed=False,
        local_completed_at=now,
        remote_revoke_status=remote_status,
        has_revoke_token=revoke_token is not None,
        retry_count=retry_count,
        next_retry_at=None,
        last_remote_error=None,
        remote_completed_at=None,
        disconnected_at=now,
        completed_at=None,
        revoke_token=revoke_token,
    )


@pytest.mark.asyncio
async def test_process_revoke_outcomes_and_concurrency_with_autospec():
    revoker = create_autospec(RemoteRevoker, instance=True)
    revoker.revoke = AsyncMock(return_value=RevokeOutcome.SUCCEEDED)

    pid = str(uuid.uuid4())
    row = _sample_ledger(purge_id=pid)
    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _fake_get_conn(_conn=None):
        yield fake_conn

    updates: list[dict] = []

    async def _capture_update(purge_id, **kw):
        updates.append({"purge_id": purge_id, **kw})
        return row

    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", AsyncMock(return_value=row)),
        patch.object(revoke_mod, "_has_newer_active_google_connection", AsyncMock(return_value=False)),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker)

    assert status == RemoteRevokeStatus.SUCCEEDED
    revoker.revoke.assert_awaited_once_with(
        provider="google",
        oauth_client_id=row.oauth_client_id,
        token="rt_secret",
    )
    assert updates[-1]["remote_revoke_status"] == RemoteRevokeStatus.SUCCEEDED
    assert updates[-1]["clear_revoke_token"] is True
    assert updates[-1]["mark_remote_completed"] is True

    # Retryable outcome -> exponential backoff & retry_count increment
    revoker_retry = create_autospec(RemoteRevoker, instance=True)
    revoker_retry.revoke = AsyncMock(return_value=RevokeOutcome.RETRYABLE)
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", AsyncMock(return_value=_sample_ledger(purge_id=pid, retry_count=2))),
        patch.object(revoke_mod, "_has_newer_active_google_connection", AsyncMock(return_value=False)),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker_retry)
    assert status == RemoteRevokeStatus.FAILED_RETRYABLE
    assert updates[-1]["increment_retry"] is True
    assert updates[-1]["clear_revoke_token"] is False
    assert updates[-1]["next_retry_at"] is not None
    assert compute_revoke_backoff_seconds(2) == 120

    # Permanent failure -> clear token & mark superseded
    revoker_perm = create_autospec(RemoteRevoker, instance=True)
    revoker_perm.revoke = AsyncMock(return_value=RevokeOutcome.PERMANENT_FAILURE)
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", AsyncMock(return_value=row)),
        patch.object(revoke_mod, "_has_newer_active_google_connection", AsyncMock(return_value=False)),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker_perm)
    assert status == RemoteRevokeStatus.SUPERSEDED
    assert updates[-1]["clear_revoke_token"] is True
    assert updates[-1]["mark_remote_completed"] is True

    # Newer active authorization already exists -> mark superseded WITHOUT calling revoker
    revoker_never = create_autospec(RemoteRevoker, instance=True)
    revoker_never.revoke = AsyncMock(return_value=RevokeOutcome.SUCCEEDED)
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", AsyncMock(return_value=row)),
        patch.object(revoke_mod, "_has_newer_active_google_connection", AsyncMock(return_value=True)),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker_never)
    assert status == RemoteRevokeStatus.SUPERSEDED
    revoker_never.revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersede_and_revoke_before_new_authorization_and_disabled_loop(caplog):
    row1 = _sample_ledger()
    row2 = _sample_ledger(remote_status=RemoteRevokeStatus.FAILED_RETRYABLE)
    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _fake_get_conn(_conn=None):
        yield fake_conn

    updates: list[dict] = []

    async def _capture_update(purge_id, **kw):
        updates.append({"purge_id": purge_id, **kw})
        return row1

    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "list_active_revokes_for_google_project_sub", AsyncMock(return_value=[row1, row2])),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        count = await supersede_pending_revokes("user-1", "123456789012", "sub-abc")
    assert count == 2
    assert all(u["remote_revoke_status"] == RemoteRevokeStatus.SUPERSEDED and u["clear_revoke_token"] for u in updates)

    # revoke_before_new_authorization: failure immediately marks superseded (never retryable)
    revoker = create_autospec(RemoteRevoker, instance=True)
    revoker.revoke = AsyncMock(side_effect=[RevokeOutcome.SUCCEEDED, RevokeOutcome.RETRYABLE])
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "list_active_revokes_for_google_project_sub", AsyncMock(return_value=[row1, row2])),
        patch.object(db, "update_purge_ledger_remote_status", side_effect=_capture_update),
    ):
        res = await revoke_before_new_authorization("user-1", "123456789012", "sub-abc", revoker)
    assert res[row1.purge_id] == RemoteRevokeStatus.SUCCEEDED
    assert res[row2.purge_id] == RemoteRevokeStatus.SUPERSEDED

    # Without revoker, retry loop logs info and returns immediately
    with caplog.at_level(logging.INFO, logger=revoke_mod.__name__):
        await asyncio.wait_for(run_integration_revoke_retry_loop(revoker=None), timeout=1.0)
    assert any("not started" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Gates, FOR SHARE fence, transcript fenced save, search guard, archive/memory skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gates_return_409_and_retry_after():
    tid = str(uuid.uuid4())
    conn_purging = _FakeConn(_FakeCursor(fetchone_seq=[{"personal_purge_in_progress": True}]))

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield conn_purging

    with patch.object(purge_fence, "get_db_connection", _get_conn):
        with pytest.raises(PersonalDataPurgeInProgressError) as exc_info:
            await check_thread_not_purging_fast(tid)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == PURGE_IN_PROGRESS_CODE
    assert exc_info.value.headers["Retry-After"] == "3"

    # start_run authoritative tx gate
    tx_conn = _FakeConn(_FakeCursor(fetchone_seq=[{"personal_purge_in_progress": True}]))
    with pytest.raises(PersonalDataPurgeInProgressError) as exc_tx:
        await assert_thread_not_purging_tx(tx_conn, tid)
    assert exc_tx.value.status_code == 409
    assert any("FOR UPDATE" in sql for sql, _ in tx_conn._cursor.executed)

    # Fork gate raises ForkRefused(409, "personal_data_purge_in_progress", ...)
    fork_conn = _FakeConn(_FakeCursor(fetchone_seq=[{"personal_purge_in_progress": True}]))
    with pytest.raises(ForkRefused) as exc_fork:
        await assert_fork_source_not_purging_tx(fork_conn, tid, cut_turn_index=2)
    assert exc_fork.value.status_code == 409
    assert exc_fork.value.detail["code"] == PURGE_IN_PROGRESS_CODE


@pytest.mark.asyncio
async def test_writeback_fence_for_share_rejects_stale_and_in_progress():
    tid = str(uuid.uuid4())

    # Stale generation (read before purge started, gen bumped twice from 0 -> 2)
    c_stale = _FakeConn(
        _FakeCursor(fetchone_seq=[{"personal_purge_in_progress": False, "personal_purge_generation": 2}])
    )
    assert await verify_writeback_fence_tx(c_stale, tid, expected_purge_gen=0, label="test") is False
    assert any("FOR SHARE" in sql for sql, _ in c_stale._cursor.executed)

    # Mid-purge read (read at gen=1 while in_progress=True; later unblocked at gen=2)
    c_mid = _FakeConn(
        _FakeCursor(fetchone_seq=[{"personal_purge_in_progress": False, "personal_purge_generation": 2}])
    )
    assert await verify_writeback_fence_tx(c_mid, tid, expected_purge_gen=1, label="test") is False

    # Currently in progress even if gen matches
    c_active = _FakeConn(
        _FakeCursor(fetchone_seq=[{"personal_purge_in_progress": True, "personal_purge_generation": 1}])
    )
    assert await verify_writeback_fence_tx(c_active, tid, expected_purge_gen=1, label="test") is False

    # Matching generation and not in progress -> allowed
    c_ok = _FakeConn(
        _FakeCursor(fetchone_seq=[{"personal_purge_in_progress": False, "personal_purge_generation": 2}])
    )
    assert await verify_writeback_fence_tx(c_ok, tid, expected_purge_gen=2, label="test") is True


@pytest.mark.asyncio
async def test_transcript_fenced_save_releases_blobs_on_abort_and_skips_upload_on_mismatch():
    tid = str(uuid.uuid4())
    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield fake_conn

    upload_mock = AsyncMock(return_value=[("a" * 64, datetime.now(UTC))])
    release_mock = AsyncMock()
    save_mock = AsyncMock(return_value=False)

    # 1. Fence mismatch -> aborts before uploading any blob
    with (
        patch.object(purge_fence, "get_db_connection", _get_conn),
        patch.object(purge_fence, "verify_writeback_fence_tx", AsyncMock(return_value=False)),
        patch.object(purge_fence, "_upload_and_register_blobs_tracked", upload_mock),
        patch.object(purge_fence, "_release_uploaded_blobs", release_mock),
        patch("src.server.database.thread_transcripts.save_stored", save_mock),
    ):
        ok = await save_stored_transcript_fenced(
            tid, "", "u1", SimpleNamespace(), {"a" * 64: b"data"}, expected_purge_gen=0
        )
    assert ok is False
    upload_mock.assert_not_awaited()
    save_mock.assert_not_awaited()

    # 2. Fence passes, blob uploads, save_stored returns False -> uploaded blob is released!
    with (
        patch.object(purge_fence, "get_db_connection", _get_conn),
        patch.object(purge_fence, "verify_writeback_fence_tx", AsyncMock(return_value=True)),
        patch.object(purge_fence, "_upload_and_register_blobs_tracked", upload_mock),
        patch.object(purge_fence, "_release_uploaded_blobs", release_mock),
        patch("src.server.database.thread_transcripts.save_stored", save_mock),
    ):
        ok = await save_stored_transcript_fenced(
            tid, "", "u1", SimpleNamespace(), {"a" * 64: b"data"}, expected_purge_gen=0
        )
    assert ok is False
    upload_mock.assert_awaited_once()
    release_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_index_turns_closed_range_guard_and_expected_gen():
    from src.server.database.search import index as search_db
    from src.server.services.search import index as search_svc

    tid = str(uuid.uuid4())
    now = datetime.now(UTC)
    sources = [
        search_db.TurnSource(0, str(uuid.uuid4()), "public Q0", "user", now),
        search_db.TurnSource(1, str(uuid.uuid4()), "private Q1", "user", now),
        search_db.TurnSource(2, str(uuid.uuid4()), "private Q2", "user", now),
        search_db.TurnSource(3, str(uuid.uuid4()), "new post-purge Q3", "user", now),
    ]
    committed = SimpleNamespace(
        texts={
            sources[0].response_id: "public A0",
            sources[1].response_id: "private A1",
            sources[2].response_id: "private A2",
            sources[3].response_id: "new post-purge A3",
        },
        unread=set(),
    )
    captured: list[dict] = []

    async def _fake_write(thread_id, rows, *, read_at, expected_purge_gen=None):
        captured.append({"rows": list(rows), "expected_purge_gen": expected_purge_gen})
        return len(rows)

    with (
        patch.object(purge_fence, "read_thread_purge_state", AsyncMock(return_value=(False, 2, [[1, 2]]))),
        patch.object(search_db, "turns_to_index", AsyncMock(return_value=(sources, now))),
        patch("src.server.services.history.committed.read_committed_texts", AsyncMock(return_value=committed)),
        patch.object(search_db, "write_turns", side_effect=_fake_write),
    ):
        res = await search_svc.index_turns(tid)

    assert res.written == 4
    assert captured[0]["expected_purge_gen"] == 2
    by_rid = {r.response_id: r for r in captured[0]["rows"]}
    assert by_rid[sources[0].response_id].user_text == "public Q0"
    assert by_rid[sources[0].response_id].answer_text == "public A0"
    assert by_rid[sources[1].response_id].user_text == ""
    assert by_rid[sources[1].response_id].answer_text == ""
    assert by_rid[sources[1].response_id].answer_read is True
    assert by_rid[sources[2].response_id].user_text == ""
    assert by_rid[sources[2].response_id].answer_text == ""
    assert by_rid[sources[3].response_id].user_text == "new post-purge Q3"
    assert by_rid[sources[3].response_id].answer_text == "new post-purge A3"


@pytest.mark.asyncio
async def test_archive_extractor_and_memory_skip_personal_integration_threads():
    from src.dalton.conversations import extractor as ext_mod

    tid = str(uuid.uuid4())
    assist_mock = AsyncMock()
    with (
        patch.object(purge_fence, "is_personal_integration_thread", AsyncMock(return_value=True)),
        patch.object(ext_mod, "assist_json", assist_mock),
    ):
        outcome = await ext_mod.extract_segment_cards(
            [{"agent": "main", "session_id": tid, "session_key": tid, "seq": 1, "text": "my calendar"}],
            extractor_model="test-model",
        )
    assert outcome.ok is True
    assert outcome.category == "private"
    assert outcome.discarded_segment is True
    assert outcome.cards == []
    assist_mock.assert_not_awaited()

    with patch.object(purge_fence, "is_personal_integration_thread", AsyncMock(return_value=True)):
        assert await should_skip_memory_extraction_for_thread(tid) is True


@pytest.mark.asyncio
async def test_tracker_reconcile_needed_without_handler_and_with_handler(monkeypatch):
    owner_id = str(uuid.uuid4())
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", owner_id)
    register_tracker_purge_handler(None)

    pid = str(uuid.uuid4())
    now = datetime.now(UTC)
    ledger = PurgeLedgerRow(
        purge_id=pid,
        user_id=owner_id,
        provider="google",
        service="calendar",
        oauth_client_id="123-c.apps.googleusercontent.com",
        provider_account_id="sub-1",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={},
        tracker_reconcile_needed=False,
        local_completed_at=None,
        remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
        has_revoke_token=False,
        retry_count=0,
        next_retry_at=None,
        last_remote_error=None,
        remote_completed_at=now,
        disconnected_at=now,
        completed_at=None,
    )
    fake_conn = _FakeConn(_FakeCursor(fetchall_seq=[[]]))

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield fake_conn

    with (
        patch.object(db, "get_purge_ledger", AsyncMock(return_value=ledger)),
        patch.object(purge_mod, "get_db_connection", _get_conn),
        patch.object(db, "delete_approvals_for_disconnect", AsyncMock(return_value=0)),
        patch.object(purge_mod, "get_checkpointer", return_value=None),
    ):
        report = await purge_mod.run_local_purge(pid)
    assert report.tracker_reconcile_needed is True

    # With registered handler returning True -> tracker_reconcile_needed is False
    handler_called = []

    async def _custom_handler(row: PurgeLedgerRow) -> bool:
        handler_called.append(row.purge_id)
        return True

    register_tracker_purge_handler(_custom_handler)
    try:
        fake_conn2 = _FakeConn(_FakeCursor(fetchall_seq=[[]]))

        @asynccontextmanager
        async def _get_conn2(_conn=None):
            yield fake_conn2

        with (
            patch.object(db, "get_purge_ledger", AsyncMock(return_value=ledger)),
            patch.object(purge_mod, "get_db_connection", _get_conn2),
            patch.object(db, "delete_approvals_for_disconnect", AsyncMock(return_value=0)),
            patch.object(purge_mod, "get_checkpointer", return_value=None),
        ):
            report2 = await purge_mod.run_local_purge(pid)
        assert report2.tracker_reconcile_needed is False
        assert handler_called == [pid]
    finally:
        register_tracker_purge_handler(None)


# ---------------------------------------------------------------------------
# 7. Four-layer OAuth callback query redaction
# ---------------------------------------------------------------------------


def test_four_layer_callback_query_redaction():
    # Layer 1 & 2: Caddyfiles
    standalone_caddy = (REPO_ROOT / "deploy" / "standalone" / "Caddyfile").read_text(encoding="utf-8")
    dalton_caddy = (REPO_ROOT / "deploy" / "dalton" / "Caddyfile").read_text(encoding="utf-8")
    for caddy_name, text in (("standalone", standalone_caddy), ("dalton", dalton_caddy)):
        assert "replace code REDACTED" in text, f"{caddy_name} Caddyfile missing code redaction"
        assert "replace state REDACTED" in text, f"{caddy_name} Caddyfile missing state redaction"
        assert "replace ticket REDACTED" in text, f"{caddy_name} Caddyfile missing ticket redaction"
        assert "resp_headers>Location delete" in text, f"{caddy_name} Caddyfile missing Location header strip"

    # Layer 3: Uvicorn SensitiveQueryLogFilter (success & error callbacks)
    success_url = "/api/v1/dalton/integrations/google/callback?code=4/0AsecretCode123&state=opaqueState999"
    error_url = "/api/v1/dalton/integrations/feishu/callback?error=access_denied&state=opaqueState888"

    redacted_ok = redact_sensitive_query_params(success_url)
    assert "4/0AsecretCode123" not in redacted_ok
    assert "opaqueState999" not in redacted_ok
    assert "code=REDACTED" in redacted_ok
    assert "state=REDACTED" in redacted_ok

    filt = SensitiveQueryLogFilter()
    for url, secrets in (
        (success_url, ("4/0AsecretCode123", "opaqueState999")),
        (error_url, ("opaqueState888",)),
    ):
        rec = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/1.1" %d',
            args=("127.0.0.1", "GET", url, 302),
            exc_info=None,
        )
        assert filt.filter(rec) is True
        formatted = rec.getMessage()
        for s in secrets:
            assert s not in formatted

    # Layer 4: private_query.is_private, AccessLogQueryFilter, OpenTelemetry drop_span_query
    assert is_private("/api/v1/dalton/integrations/google/callback", "code=abc&state=xyz") is True
    assert is_private("/api/v1/dalton/integrations/feishu/callback", "error=denied&state=xyz") is True
    assert is_private("/api/v1/dalton/integrations/google/authorize", "x=1") is False

    access_filter = AccessLogQueryFilter()
    for url, secrets in (
        (success_url, ("4/0AsecretCode123", "opaqueState999")),
        (error_url, ("opaqueState888",)),
    ):
        rec = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/1.1" %d',
            args=("127.0.0.1", "GET", url, 302),
            exc_info=None,
        )
        access_filter.filter(rec)
        formatted = rec.getMessage()
        for s in secrets:
            assert s not in formatted

    class _FakeSpan:
        def __init__(self, attrs: dict):
            self.attributes = dict(attrs)

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, k: str, v) -> None:
            self.attributes[k] = v

    for path, qs, secrets in (
        ("/api/v1/dalton/integrations/google/callback", b"code=secretCode&state=secretState", ("secretCode", "secretState")),
        ("/api/v1/dalton/integrations/feishu/callback", b"error=access_denied&state=secretState2", ("secretState2",)),
    ):
        span = _FakeSpan(
            {
                "http.url": f"https://dalton.example.com{path}?{qs.decode()}",
                "url.full": f"https://dalton.example.com{path}?{qs.decode()}",
                "http.target": f"{path}?{qs.decode()}",
                "url.query": qs.decode(),
            }
        )
        drop_span_query(span, {"path": path, "query_string": qs})
        for val in span.attributes.values():
            for s in secrets:
                assert s not in str(val)


# ---------------------------------------------------------------------------
# 8. Restore reset SQL & restore.sh wiring
# ---------------------------------------------------------------------------


def test_restore_reset_sql_and_restore_script_wiring():
    sql_path = REPO_ROOT / "scripts" / "dalton" / "sql" / "personal_integrations_restore_reset.sql"
    sql = sql_path.read_text(encoding="utf-8")
    for required in (
        "to_regclass('public.dalton_user_integrations')",
        "status = 'needs_reauth'",
        "access_token = NULL",
        "refresh_token = NULL",
        "to_regclass('public.dalton_integration_purge_ledger')",
        "encrypted_revoke_token = NULL",
        "to_regclass('public.dalton_user_integration_oauth_states')",
        "DELETE FROM dalton_user_integration_oauth_states",
        "to_regclass('public.dalton_user_integration_pending_confirmations')",
        "DELETE FROM dalton_user_integration_pending_confirmations",
    ):
        assert required in sql, f"restore SQL missing {required}"

    restore_sh = (REPO_ROOT / "scripts" / "standalone" / "restore.sh").read_text(encoding="utf-8")
    migrate_pos = restore_sh.index("compose_cmd run --rm migrate")
    reset_pos = restore_sh.index("personal_integrations_restore_reset.sql")
    up_pos = restore_sh.index("compose_cmd up -d\n")
    assert migrate_pos < reset_pos < up_pos

    ops_doc = (REPO_ROOT / "docs" / "dalton" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "scripts/dalton/sql/personal_integrations_restore_reset.sql" in ops_doc
