"""Unit tests for Personal Calendar & Mail Batch 1 (r1):
migration d064, data layer, 2A purger, purge fence, remote revoke state machine,
archive/memory skip, restore reset SQL, and 4-layer callback query redaction.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import create_autospec, patch
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import psycopg
import pytest

from ptc_agent.agent.middleware.compaction.utils import (
    COMPACTION_SUMMARY_KEY,
    SIDECAR_KEY,
    SUMMARIZED_KEY,
    SummarySpan,
    build_summary_message,
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
    purge_lock_key,
    purge_thread_lock_key,
    refresh_lock_key,
    revoke_lock_key,
    touch_user_lock_key,
)
from src.dalton.migrations.versions import d064_user_integrations as d064_mod
from src.dalton.services import integration_data_purge as purge_mod
from src.dalton.services import integration_revoke as revoke_mod
from src.dalton.services import purge_fence
from src.dalton.services.integration_data_purge import (
    PURGED_PLACEHOLDER,
    PURGED_TOOL_ARGS,
    PURGED_TOOL_ARGS_JSON,
    collect_redis_keys_for_suffix,
    is_redis_configured_for_purge,
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
    build_guarded_search_rows,
    check_thread_not_purging_fast,
    check_writeback_fence_tx,
    clip_purged_ranges_on_truncate_tx,
    clip_purged_turn_ranges,
    copy_personal_integration_state_for_fork_tx,
    fork_max_inclusive_turn,
    is_personal_integration_thread,
    is_turn_in_purged_ranges,
    merge_turn_range,
    normalize_turn_ranges,
    read_expected_purge_generation,
    read_thread_purge_state,
    save_stored_transcript_fenced,
    should_skip_memory_extraction_for_thread,
    verify_writeback_fence_tx,
    write_many_fenced,
)
from src.observability.private_query import (
    AccessLogQueryFilter,
    drop_span_query,
    is_private,
)
from src.server.database import session_lock
from src.server.services.forks.model import BeforeTurn, ForkRefused, ThroughTurn

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _enable_dalton_env_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly enable Dalton in unit tests exercising Dalton purge/fence paths."""
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("REDIS_CACHE_ENABLED", "false")


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
        self.closed = False

    def cursor(self, **_kw):
        return self._cursor

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return self._cursor

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# 1. Migration d064 structure & reversibility
# ---------------------------------------------------------------------------


def test_d064_migration_revisions_and_sql_objects():
    assert d064_mod.revision == "d064"
    assert d064_mod.down_revision == "d063"

    upgrade_sqls: list[str] = []
    downgrade_sqls: list[str] = []

    with patch.object(d064_mod.op, "execute", side_effect=upgrade_sqls.append):
        d064_mod.upgrade()
    with patch.object(d064_mod.op, "execute", side_effect=downgrade_sqls.append):
        d064_mod.downgrade()

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
    assert revoke_lock_key("123456789012", "sub-1") != revoke_lock_key("999999999999", "sub-1")
    assert revoke_lock_key("123456789012", "sub-1") == revoke_lock_key("123456789012", "sub-1")
    assert purge_lock_key("p1") != purge_lock_key("p2")
    assert purge_lock_key("p1") == purge_lock_key("p1")
    assert touch_user_lock_key("u1") != touch_user_lock_key("u2")
    assert touch_user_lock_key("u1") == touch_user_lock_key("u1")
    assert purge_thread_lock_key("t1") != purge_thread_lock_key("t2")
    assert purge_thread_lock_key("t1") == purge_thread_lock_key("t1")


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
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-secret"}],
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
    assert "reasoning_details" not in out_ai.additional_kwargs
    assert out_ai.additional_kwargs["tool_calls"][0]["function"]["arguments"] == PURGED_TOOL_ARGS_JSON

    # Dict-shaped message fallback also deletes reasoning_details and replaces reasoning_content
    dict_ai = {
        "id": "m2-dict",
        "content": "secret reply",
        "additional_kwargs": {
            "reasoning_content": "secret chain",
            "reasoning_details": [{"type": "reasoning.summary", "summary": "secret"}],
        },
    }
    out_dict, ch_dict = redact_message(
        dict_ai, in_suffix=True, affected_summary_ids=set(), min_turn=1, max_turn=3
    )
    assert ch_dict
    assert out_dict["additional_kwargs"]["reasoning_content"] == PURGED_PLACEHOLDER
    assert "reasoning_details" not in out_dict["additional_kwargs"]

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
    revoker.revoke.return_value = RevokeOutcome.SUCCEEDED

    pid = str(uuid.uuid4())
    row = _sample_ledger(purge_id=pid)
    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _fake_get_conn(_conn=None):
        yield fake_conn

    updates: list[dict] = []

    async def _capture_update(purge_id, **kw):
        updates.append({"purge_id": purge_id, **kw})
        return _sample_ledger(
            purge_id=purge_id,
            remote_status=kw["remote_revoke_status"],
        )

    async def _capture_finalize(purge_id, **kw):
        updates.append({"purge_id": purge_id, **kw})
        return _sample_ledger(
            purge_id=purge_id,
            remote_status=kw["remote_revoke_status"],
        )

    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", autospec=True, return_value=row),
        patch.object(db, "has_running_local_purge_for_user_provider", autospec=True, return_value=False),
        patch.object(revoke_mod, "_has_newer_active_google_connection", autospec=True, return_value=False),
        patch.object(db, "get_active_google_oauth_state_expiry", autospec=True, return_value=None),
        patch.object(db, "claim_revoke_attempt_tx", autospec=True, return_value=1),
        patch.object(db, "finalize_revoke_attempt_tx", autospec=True, side_effect=_capture_finalize),
        patch.object(db, "update_purge_ledger_remote_status", autospec=True, side_effect=_capture_update),
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
    revoker_retry.revoke.return_value = RevokeOutcome.RETRYABLE
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", autospec=True, return_value=_sample_ledger(purge_id=pid, retry_count=2)),
        patch.object(db, "has_running_local_purge_for_user_provider", autospec=True, return_value=False),
        patch.object(revoke_mod, "_has_newer_active_google_connection", autospec=True, return_value=False),
        patch.object(db, "get_active_google_oauth_state_expiry", autospec=True, return_value=None),
        patch.object(db, "claim_revoke_attempt_tx", autospec=True, return_value=2),
        patch.object(db, "finalize_revoke_attempt_tx", autospec=True, side_effect=_capture_finalize),
        patch.object(db, "update_purge_ledger_remote_status", autospec=True, side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker_retry)
    assert status == RemoteRevokeStatus.FAILED_RETRYABLE
    assert updates[-1]["increment_retry"] is True
    assert updates[-1]["clear_revoke_token"] is False
    assert updates[-1]["next_retry_at"] is not None
    assert compute_revoke_backoff_seconds(2) == 120

    # Permanent failure -> clear token & mark superseded
    revoker_perm = create_autospec(RemoteRevoker, instance=True)
    revoker_perm.revoke.return_value = RevokeOutcome.PERMANENT_FAILURE
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", autospec=True, return_value=row),
        patch.object(db, "has_running_local_purge_for_user_provider", autospec=True, return_value=False),
        patch.object(revoke_mod, "_has_newer_active_google_connection", autospec=True, return_value=False),
        patch.object(db, "get_active_google_oauth_state_expiry", autospec=True, return_value=None),
        patch.object(db, "claim_revoke_attempt_tx", autospec=True, return_value=3),
        patch.object(db, "finalize_revoke_attempt_tx", autospec=True, side_effect=_capture_finalize),
        patch.object(db, "update_purge_ledger_remote_status", autospec=True, side_effect=_capture_update),
    ):
        status = await process_revoke(pid, revoker_perm)
    assert status == RemoteRevokeStatus.SUPERSEDED
    assert updates[-1]["clear_revoke_token"] is True
    assert updates[-1]["mark_remote_completed"] is True

    # Newer active authorization already exists -> mark superseded WITHOUT calling revoker
    revoker_never = create_autospec(RemoteRevoker, instance=True)
    revoker_never.revoke.return_value = RevokeOutcome.SUCCEEDED
    updates.clear()
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "get_purge_ledger", autospec=True, return_value=row),
        patch.object(db, "has_running_local_purge_for_user_provider", autospec=True, return_value=False),
        patch.object(revoke_mod, "_has_newer_active_google_connection", autospec=True, return_value=True),
        patch.object(db, "update_purge_ledger_remote_status", autospec=True, side_effect=_capture_update),
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
        return _sample_ledger(purge_id=purge_id, remote_status=kw["remote_revoke_status"])

    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "list_active_revokes_for_google_project_sub", autospec=True, return_value=[row1, row2]),
        patch.object(db, "update_purge_ledger_remote_status", autospec=True, side_effect=_capture_update),
    ):
        count = await supersede_pending_revokes("123456789012", "sub-abc")
    assert count == 2
    assert all(u["remote_revoke_status"] == RemoteRevokeStatus.SUPERSEDED and u["clear_revoke_token"] for u in updates)

    # revoke_before_new_authorization: retryable failure preserves ciphertext & stays FAILED_RETRYABLE
    revoker = create_autospec(RemoteRevoker, instance=True)
    revoker.revoke.side_effect = [RevokeOutcome.SUCCEEDED, RevokeOutcome.RETRYABLE]
    updates.clear()
    rows_by_id = {row1.purge_id: row1, row2.purge_id: row2}
    with (
        patch.object(revoke_mod, "get_db_connection", _fake_get_conn),
        patch.object(db, "list_active_revokes_for_google_project_sub", autospec=True, return_value=[row1, row2]),
        patch.object(db, "get_purge_ledger", autospec=True, side_effect=lambda pid, **_k: rows_by_id[pid]),
        patch.object(db, "has_running_local_purge_for_user_provider", autospec=True, return_value=False),
        patch.object(revoke_mod, "_has_newer_active_google_connection", autospec=True, return_value=False),
        patch.object(db, "get_active_google_oauth_state_expiry", autospec=True, return_value=None),
        patch.object(db, "claim_revoke_attempt_tx", autospec=True, return_value=1),
        patch.object(db, "finalize_revoke_attempt_tx", autospec=True, side_effect=_capture_update),
    ):
        res = await revoke_before_new_authorization("123456789012", "sub-abc", revoker)
    assert res[row1.purge_id] == RemoteRevokeStatus.SUCCEEDED
    assert res[row2.purge_id] == RemoteRevokeStatus.FAILED_RETRYABLE
    row2_upd = [u for u in updates if u["purge_id"] == row2.purge_id][-1]
    assert row2_upd["clear_revoke_token"] is False
    assert row2_upd["increment_retry"] is True

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
    from src.server.database import thread_transcripts as tt_db

    tid = str(uuid.uuid4())
    fake_conn = _FakeConn()

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield fake_conn

    upload_mock = create_autospec(
        purge_fence._upload_and_register_blobs_tracked,
        return_value=[("a" * 64, datetime.now(UTC))],
    )
    release_mock = create_autospec(purge_fence._release_uploaded_blobs, return_value=None)
    save_mock = create_autospec(tt_db.save_stored, return_value=False)

    # 1. Fence mismatch -> aborts before uploading any blob
    with (
        patch.object(purge_fence, "get_db_connection", _get_conn),
        patch.object(purge_fence, "verify_writeback_fence_tx", autospec=True, return_value=False),
        patch.object(purge_fence, "_upload_and_register_blobs_tracked", upload_mock),
        patch.object(purge_fence, "_release_uploaded_blobs", release_mock),
        patch.object(tt_db, "save_stored", save_mock),
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
        patch.object(purge_fence, "verify_writeback_fence_tx", autospec=True, return_value=True),
        patch.object(purge_fence, "_upload_and_register_blobs_tracked", upload_mock),
        patch.object(purge_fence, "_release_uploaded_blobs", release_mock),
        patch.object(tt_db, "save_stored", save_mock),
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
    from src.server.services.history import committed as committed_mod
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
        patch.object(purge_fence, "read_thread_purge_state", autospec=True, return_value=(False, 2, [[1, 2]])),
        patch.object(search_db, "turns_to_index", autospec=True, return_value=(sources, now)),
        patch.object(committed_mod, "read_committed_texts", autospec=True, return_value=committed),
        patch.object(search_db, "write_turns", autospec=True, side_effect=_fake_write),
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
    assist_mock = create_autospec(ext_mod.assist_json)
    with (
        patch.object(purge_fence, "is_personal_integration_thread", autospec=True, return_value=True),
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

    with patch.object(purge_fence, "is_personal_integration_thread", autospec=True, return_value=True):
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
    fake_conn = _FakeConn(
        _FakeCursor(
            fetchone_seq=[
                {"local_purge_status": "running", "local_purge_attempt_gen": 1},
                {"local_purge_status": "running", "local_purge_attempt_gen": 1},
            ],
            fetchall_seq=[[]],
        )
    )

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield fake_conn

    @asynccontextmanager
    async def _lock_ok(_key):
        yield True

    with (
        patch.object(session_lock, "try_own_session_lock", autospec=True, side_effect=_lock_ok),
        patch.object(db, "get_purge_ledger", autospec=True, return_value=ledger),
        patch.object(db, "claim_local_purge_attempt", autospec=True, return_value=1),
        patch.object(db, "renew_local_purge_attempt", autospec=True, return_value=True),
        patch.object(db, "release_local_purge_attempt", autospec=True, return_value=None),
        patch.object(purge_mod, "get_db_connection", _get_conn),
        patch.object(db, "delete_approvals_for_disconnect", autospec=True, return_value=0),
        patch.object(purge_mod, "get_checkpointer", autospec=True, return_value=None),
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
        fake_conn2 = _FakeConn(
            _FakeCursor(
                fetchone_seq=[
                    {"local_purge_status": "running", "local_purge_attempt_gen": 1},
                    {"local_purge_status": "running", "local_purge_attempt_gen": 1},
                ],
                fetchall_seq=[[]],
            )
        )

        @asynccontextmanager
        async def _get_conn2(_conn=None):
            yield fake_conn2

        with (
            patch.object(session_lock, "try_own_session_lock", autospec=True, side_effect=_lock_ok),
            patch.object(db, "get_purge_ledger", autospec=True, return_value=ledger),
            patch.object(db, "claim_local_purge_attempt", autospec=True, return_value=1),
            patch.object(db, "renew_local_purge_attempt", autospec=True, return_value=True),
            patch.object(db, "release_local_purge_attempt", autospec=True, return_value=None),
            patch.object(purge_mod, "get_db_connection", _get_conn2),
            patch.object(db, "delete_approvals_for_disconnect", autospec=True, return_value=0),
            patch.object(purge_mod, "get_checkpointer", autospec=True, return_value=None),
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
    import re

    # Layer 1 & 2: Caddyfiles (single-layer request>uri regexp redacts OAuth code/state + ticket)
    standalone_caddy = (REPO_ROOT / "deploy" / "standalone" / "Caddyfile").read_text(encoding="utf-8")
    dalton_caddy = (REPO_ROOT / "deploy" / "dalton" / "Caddyfile").read_text(encoding="utf-8")
    sample_uri = "/api/v1/dalton/integrations/google/callback?code=4/0AsecretCode123&state=opaqueState999&ticket=tktSecret777"
    for caddy_name, text in (("standalone", standalone_caddy), ("dalton", dalton_caddy)):
        uri_match = re.search(
            r"request>uri regexp `([^`]+)` `\$\{1\}\$\{2\}REDACTED`", text
        )
        assert uri_match is not None, f"{caddy_name} Caddyfile missing single-layer request>uri regexp"
        uri_re = re.compile(uri_match.group(1))
        redacted_uri = uri_re.sub(
            lambda m: f"{m.group(1) or ''}{m.group(2) or ''}REDACTED", sample_uri
        )
        assert "4/0AsecretCode123" not in redacted_uri, f"{caddy_name} Caddyfile missing code redaction"
        assert "opaqueState999" not in redacted_uri, f"{caddy_name} Caddyfile missing state redaction"
        assert "tktSecret777" not in redacted_uri, f"{caddy_name} Caddyfile missing ticket redaction"
        assert "code=REDACTED" in redacted_uri
        assert "state=REDACTED" in redacted_uri
        assert "ticket=REDACTED" in redacted_uri
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
    step3_pos = restore_sh.index('info "3/7 ')
    check_pos = restore_sh.index("PERSONAL_INTEGRATIONS_RESET_SQL=")
    mutation_pos = restore_sh.index("MUTATION_STARTED=1")
    migrate_pos = restore_sh.index("compose_cmd run --rm migrate")
    exec_pos = restore_sh.index('< "$PERSONAL_INTEGRATIONS_RESET_SQL"')
    up_pos = restore_sh.index("compose_cmd up -d\n")
    assert step3_pos < check_pos < mutation_pos < migrate_pos < exec_pos < up_pos
    missing_branch = restore_sh[check_pos : check_pos + 420]
    assert '! -f "$PERSONAL_INTEGRATIONS_RESET_SQL"' in missing_branch
    assert '! -r "$PERSONAL_INTEGRATIONS_RESET_SQL"' in missing_branch
    assert '! -s "$PERSONAL_INTEGRATIONS_RESET_SQL"' in missing_branch
    assert "err " in missing_branch and "exit 1" in missing_branch

    pkg_sh = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    req_start = pkg_sh.index("ALLOWLIST_REQUIRED=(")
    req_end = pkg_sh.index(")", req_start)
    assert '"scripts/dalton/sql/personal_integrations_restore_reset.sql"' in pkg_sh[req_start:req_end]

    standalone_doc = (REPO_ROOT / "docs" / "dalton" / "dev-guide" / "STANDALONE-DEPLOY.md").read_text(encoding="utf-8")
    assert "psql -v ON_ERROR_STOP=1 -f scripts/dalton/sql/personal_integrations_restore_reset.sql" in standalone_doc

    ops_doc = (REPO_ROOT / "docs" / "dalton" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "scripts/dalton/sql/personal_integrations_restore_reset.sql" in ops_doc


# ---------------------------------------------------------------------------
# 9. r2 regressions: cancel & drain, shared UI & cumulative summaries, mutex, incomplete layers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_and_drain_calls_real_cancel_apis_and_checks_task_namespaces():
    from src.server.database.runs import lifecycle as run_lifecycle
    from src.server.database.runs import subagent_runs
    from src.server.services import writer_guard

    tid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    trid = str(uuid.uuid4())

    req_run_cancel = create_autospec(
        run_lifecycle.request_run_cancel,
        return_value={"state": "requested", "run": None, "returned_steers": [], "held_steers": 0},
    )
    req_task_cancel = create_autospec(
        subagent_runs.request_task_run_cancel,
        return_value={"state": "requested", "run": None},
    )
    nudge = create_autospec(purge_mod.publish_cancel_nudge, return_value=None)
    held_ns = create_autospec(writer_guard.held_task_namespaces, return_value=set())

    cur = _FakeCursor(
        fetchall_seq=[
            [{"run_id": rid}],
            [{"task_id": "task-1", "task_run_id": trid, "status": "in_progress"}],
        ],
        fetchone_seq=[(True,)],
    )
    fake_conn = _FakeConn(cur)

    @asynccontextmanager
    async def _get_conn(_conn=None):
        yield fake_conn

    with (
        patch.object(purge_mod, "get_db_connection", _get_conn),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=fake_conn),
        ),
        patch.object(run_lifecycle, "request_run_cancel", req_run_cancel),
        patch.object(subagent_runs, "request_task_run_cancel", req_task_cancel),
        patch.object(purge_mod, "publish_cancel_nudge", nudge),
        patch.object(writer_guard, "held_task_namespaces", held_ns),
    ):
        await purge_mod._cancel_and_drain_thread(tid, min_turn=1, max_turn=2, timeout_s=1.0)

    req_run_cancel.assert_awaited_once_with(rid, thread_id=tid, by="suspension")
    req_task_cancel.assert_awaited_once_with(trid, thread_id=tid)
    held_ns.assert_awaited_once_with(tid, ["task-1"])
    assert nudge.await_count == 2
    assert fake_conn.closed is True

    # When held_task_namespaces remains non-empty, drain times out and raises TimeoutError
    held_busy = create_autospec(writer_guard.held_task_namespaces, return_value={"task-1"})
    cur2 = _FakeCursor(
        fetchall_seq=[
            [{"run_id": rid}],
            [{"task_id": "task-1", "task_run_id": trid, "status": "in_progress"}],
        ],
        fetchone_seq=[(True,), (True,), (True,)],
    )
    fake_conn2 = _FakeConn(cur2)

    @asynccontextmanager
    async def _get_conn2(_conn=None):
        yield fake_conn2

    with (
        patch.object(purge_mod, "get_db_connection", _get_conn2),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=fake_conn2),
        ),
        patch.object(run_lifecycle, "request_run_cancel", create_autospec(run_lifecycle.request_run_cancel, return_value={"state": "requested"})),
        patch.object(subagent_runs, "request_task_run_cancel", create_autospec(subagent_runs.request_task_run_cancel, return_value={"state": "requested"})),
        patch.object(purge_mod, "publish_cancel_nudge", create_autospec(purge_mod.publish_cancel_nudge, return_value=None)),
        patch.object(writer_guard, "held_task_namespaces", held_busy),
    ):
        with pytest.raises(TimeoutError):
            await purge_mod._cancel_and_drain_thread(tid, min_turn=1, max_turn=2, timeout_s=0.05)

    # r3: No live runs or tasks, yet thread_key is held -> waits until timeout; returns once released
    cur_idle_busy = _FakeCursor(fetchall_seq=[[], []], fetchone_seq=[(False,), (False,), (False,)])
    fake_idle_busy = _FakeConn(cur_idle_busy)

    @asynccontextmanager
    async def _get_idle_busy(_conn=None):
        yield fake_idle_busy

    with (
        patch.object(purge_mod, "get_db_connection", _get_idle_busy),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=fake_idle_busy),
        ),
        patch.object(writer_guard, "held_task_namespaces", create_autospec(writer_guard.held_task_namespaces, return_value=set())),
    ):
        with pytest.raises(TimeoutError):
            await purge_mod._cancel_and_drain_thread(tid, min_turn=1, max_turn=2, timeout_s=0.05)

    cur_idle_free = _FakeCursor(fetchall_seq=[[], []], fetchone_seq=[(True,)])
    fake_idle_free = _FakeConn(cur_idle_free)

    @asynccontextmanager
    async def _get_idle_free(_conn=None):
        yield fake_idle_free

    held_empty = create_autospec(writer_guard.held_task_namespaces, return_value=set())
    with (
        patch.object(purge_mod, "get_db_connection", _get_idle_free),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=fake_idle_free),
        ),
        patch.object(writer_guard, "held_task_namespaces", held_empty),
    ):
        await purge_mod._cancel_and_drain_thread(tid, min_turn=1, max_turn=2, timeout_s=1.0)
    held_empty.assert_awaited_once_with(tid, [])

    # r3: Completed subagent (no longer active) whose namespace lock is still held -> waits & times out without re-cancelling
    req_task_never = create_autospec(subagent_runs.request_task_run_cancel)
    held_still_locked = create_autospec(writer_guard.held_task_namespaces, return_value={"task-finished"})
    cur_done_task = _FakeCursor(
        fetchall_seq=[[], [{"task_id": "task-finished", "task_run_id": trid, "status": "completed"}]],
        fetchone_seq=[(True,), (True,), (True,)],
    )
    fake_done_task = _FakeConn(cur_done_task)

    @asynccontextmanager
    async def _get_done_task(_conn=None):
        yield fake_done_task

    with (
        patch.object(purge_mod, "get_db_connection", _get_done_task),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=fake_done_task),
        ),
        patch.object(subagent_runs, "request_task_run_cancel", req_task_never),
        patch.object(writer_guard, "held_task_namespaces", held_still_locked),
    ):
        with pytest.raises(TimeoutError):
            await purge_mod._cancel_and_drain_thread(tid, min_turn=1, max_turn=2, timeout_s=0.05)
    req_task_never.assert_not_awaited()
    held_still_locked.assert_awaited_with(tid, ["task-finished"])


@pytest.mark.asyncio
async def test_shared_ui_version_and_cumulative_messages_preserve_pre_touch_summaries():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = JsonPlusSerializer()
    tid = str(uuid.uuid4())
    r0 = str(uuid.uuid4())
    r1 = str(uuid.uuid4())

    sum_pre = HumanMessage(
        content="Pre-touch summary of turn 0 research",
        id="sum-pre",
        additional_kwargs={
            "lc_source": "summarization",
            SUMMARIZED_KEY: {"span": [0, 0], "gap": None, "skills": []},
        },
    )
    sum_cross = HumanMessage(
        content="Cross-boundary summary leaking turn 1 calendar",
        id="sum-cross",
        additional_kwargs={
            "lc_source": "summarization",
            SUMMARIZED_KEY: {"span": [0, 1], "gap": None, "skills": []},
        },
    )
    m0 = HumanMessage(content="NVDA Q0", id="m0")
    m1 = HumanMessage(content="Private calendar Q1", id="m1")

    v0_t, v0_b = serde.dumps_typed(_DeltaSnapshot([m0, sum_pre, sum_cross]))
    v1_t, v1_b = serde.dumps_typed(_DeltaSnapshot([m0, sum_pre, sum_cross, m1]))
    u0_t, u0_b = serde.dumps_typed(
        [
            {
                "type": "ui",
                "id": "ui-pre",
                "name": "context_window",
                "props": {"action": "summarize", "summary_text": "Pre-touch UI summary"},
                "metadata": {
                    "summary_message_id": "sum-pre",
                    "replaced_summary": {
                        "id": "sum-pre",
                        "action": "summarize",
                        "summary_text": "Pre-touch replaced summary",
                    },
                },
            },
            {
                "type": "ui",
                "id": "ui-cross",
                "name": "context_window",
                "props": {"action": "summarize", "summary_text": "Cross UI summary"},
                "metadata": {
                    "summary_message_id": "sum-cross",
                    "replaced_summary": {
                        "id": "sum-cross",
                        "action": "summarize",
                        "summary_text": "Cross replaced summary",
                    },
                },
            },
        ]
    )

    all_cps = [
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-0",
            "parent_checkpoint_id": None,
            "checkpoint": {"channel_versions": {"messages": "v0", "ui": "u0"}},
            "metadata": {"turn_index": 0, "run_id": r0},
        },
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
            "parent_checkpoint_id": "cp-0",
            "checkpoint": {"channel_versions": {"messages": "v1", "ui": "u0"}},  # shared ui version u0!
            "metadata": {"turn_index": 1, "run_id": r1},
        },
    ]
    blobs = [
        {"checkpoint_ns": "", "channel": "messages", "version": "v0", "type": v0_t, "blob": v0_b},
        {"checkpoint_ns": "", "channel": "messages", "version": "v1", "type": v1_t, "blob": v1_b},
        {"checkpoint_ns": "", "channel": "ui", "version": "u0", "type": u0_t, "blob": u0_b},
    ]

    cur = _FakeCursor(fetchall_seq=[all_cps, [], blobs])
    cp_conn = _FakeConn(cur)

    updated = await purge_mod._purge_thread_checkpoints(
        cp_conn,
        serde,
        thread_id=tid,
        min_turn=1,
        max_turn=1,
        run_turn_map={r0: 0, r1: 1},
        suffix_run_ids={r1},
        suffix_task_ids=set(),
    )
    assert updated == 3

    updated_blobs: dict[tuple[str, str], object] = {}
    for sql, params in cur.executed:
        if "UPDATE checkpoint_blobs" in sql:
            new_type, new_blob, _t, _ns, ch, ver = params  # type: ignore[misc]
            updated_blobs[(ch, ver)] = serde.loads_typed((new_type, new_blob))

    msgs_v0 = {m.id: m for m in updated_blobs[("messages", "v0")].value}
    msgs_v1 = {m.id: m for m in updated_blobs[("messages", "v1")].value}
    assert msgs_v0["sum-pre"].content == "Pre-touch summary of turn 0 research"
    assert msgs_v1["sum-pre"].content == "Pre-touch summary of turn 0 research"
    assert msgs_v0["sum-cross"].content == PURGED_PLACEHOLDER
    assert msgs_v1["sum-cross"].content == PURGED_PLACEHOLDER
    assert msgs_v1["m1"].content == PURGED_PLACEHOLDER

    ui_u0 = {r["id"]: r for r in updated_blobs[("ui", "u0")]}
    assert ui_u0["ui-pre"]["metadata"]["replaced_summary"]["summary_text"] == "Pre-touch replaced summary"
    assert ui_u0["ui-pre"]["props"]["summary_text"] == "Pre-touch UI summary"
    assert ui_u0["ui-cross"]["metadata"]["replaced_summary"]["summary_text"] == PURGED_PLACEHOLDER
    assert ui_u0["ui-cross"]["props"]["summary_text"] == PURGED_PLACEHOLDER


@pytest.mark.asyncio
async def test_purge_mutex_incomplete_layers_all_services_guard_and_resume_loop(monkeypatch):
    from src.dalton.services import user_integrations_lifecycle as lifecycle_mod
    from src.server.services import writer_guard
    from src.utils.cache import redis_cache as redis_cache_mod

    # begin_disconnect rejects service=None and points to begin_disconnect_all_services
    with pytest.raises(ValueError, match="begin_disconnect_all_services"):
        await lifecycle_mod.begin_disconnect("u1", "google", None)  # type: ignore[arg-type]

    # finish_disconnect_locked & run_local_purge return early without calling body when try_own_session_lock yields False
    @asynccontextmanager
    async def _lock_busy(_key):
        yield False

    lock_busy_mock = create_autospec(session_lock.try_own_session_lock, side_effect=_lock_busy)
    body_mock = create_autospec(purge_mod._run_local_purge_body)
    with (
        patch.object(session_lock, "try_own_session_lock", lock_busy_mock),
        patch.object(purge_mod, "_run_local_purge_body", body_mock),
    ):
        assert await lifecycle_mod.finish_disconnect_locked(str(uuid.uuid4())) is None
        rep_skip = await purge_mod.run_local_purge(str(uuid.uuid4()))
        assert rep_skip.skipped is True
    body_mock.assert_not_awaited()
    assert lock_busy_mock.call_count == 2

    # Redis configured disabled -> returns 0; configured enabled + cache.enabled=False -> raises; delete error -> raises
    monkeypatch.setenv("REDIS_CACHE_ENABLED", "false")
    assert is_redis_configured_for_purge() is False
    assert await purge_mod._delete_redis_keys(["k1"]) == 0

    monkeypatch.setenv("REDIS_CACHE_ENABLED", "true")
    with (
        patch("src.config.settings.is_redis_cache_enabled", autospec=True, return_value=True),
        patch.object(redis_cache_mod, "get_cache_client", autospec=True, return_value=SimpleNamespace(enabled=False, client=None)),
    ):
        assert is_redis_configured_for_purge() is True
        with pytest.raises(RuntimeError, match="unavailable"):
            await purge_mod._delete_redis_keys(["k1"])

    class _FailingRedis:
        async def delete(self, *keys):
            raise ConnectionError("redis down")

    with (
        patch("src.config.settings.is_redis_cache_enabled", autospec=True, return_value=True),
        patch.object(redis_cache_mod, "get_cache_client", autospec=True, return_value=SimpleNamespace(enabled=True, client=_FailingRedis())),
    ):
        with pytest.raises(ConnectionError):
            await purge_mod._delete_redis_keys(["k1"])
    monkeypatch.setenv("REDIS_CACHE_ENABLED", "false")

    # Missing checkpointer on a ledger with affected threads -> raises RuntimeError (never marks completed)
    import dataclasses
    from src.dalton.conversations import render as render_mod

    tid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    running_ledger = dataclasses.replace(
        _sample_ledger(purge_id=pid),
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={tid: {"min_turn": 0, "max_turn": 0}},
    )
    cp_missing_conn = _FakeConn(_FakeCursor(fetchone_seq=[(True,)], fetchall_seq=[[], [], []]))

    @asynccontextmanager
    async def _cp_missing_get_conn(_conn=None):
        yield cp_missing_conn

    @asynccontextmanager
    async def _lock_ok(_key):
        yield True

    with (
        patch.object(session_lock, "try_own_session_lock", autospec=True, side_effect=_lock_ok),
        patch.object(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=running_ledger)),
        patch.object(db, "claim_local_purge_attempt", create_autospec(db.claim_local_purge_attempt, return_value=1)),
        patch.object(db, "renew_local_purge_attempt", create_autospec(db.renew_local_purge_attempt, return_value=True)),
        patch.object(db, "release_local_purge_attempt", create_autospec(db.release_local_purge_attempt, return_value=None)),
        patch.object(purge_mod, "get_db_connection", _cp_missing_get_conn),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=cp_missing_conn),
        ),
        patch.object(writer_guard, "held_task_namespaces", create_autospec(writer_guard.held_task_namespaces, return_value=set())),
        patch.object(purge_mod, "get_checkpointer", autospec=True, return_value=None),
    ):
        with pytest.raises(RuntimeError, match="Checkpointer"):
            await purge_mod.run_local_purge(pid)

    # Conversation digest plan collects locations inside tx WITHOUT calling sandbox renderer;
    # redraw runs strictly outside advisory locks and propagates failures instead of swallowing.
    from src.dalton.integrations.lock_guard import advisory_lock_http_guard, track_advisory_lock

    card_conn = _FakeConn(
        _FakeCursor(
            fetchone_seq=[{"tbl": "dalton_conversation_cards"}],
            fetchall_seq=[
                [{"card_id": "c1", "date_et": "2026-11-26", "rendered_locations": [{"workspace_id": "w1", "path": "a.md"}], "tickers": []}],
            ],
        )
    )
    render_spy = create_autospec(
        render_mod.render_and_write_conversation_digests,
        side_effect=RuntimeError("render boom"),
    )
    read_conn = _FakeConn(_FakeCursor(fetchall_seq=[[]]))

    @asynccontextmanager
    async def _read_remaining_conn(_conn=None):
        yield read_conn

    with (
        patch.object(render_mod, "render_and_write_conversation_digests", render_spy),
        patch.object(purge_mod, "get_db_connection", _read_remaining_conn),
    ):
        plan = await purge_mod._collect_conversation_card_purge_plan(card_conn, tid)
        assert plan is not None
        assert plan.card_ids == ["c1"]
        render_spy.assert_not_awaited()
        with advisory_lock_http_guard(True):
            with track_advisory_lock("purge_thread", 123):
                with pytest.raises(RuntimeError, match="advisory lock"):
                    await purge_mod._execute_conversation_card_digest_redraw(plan, user_id="u1")
        with pytest.raises(RuntimeError, match="render boom"):
            await purge_mod._execute_conversation_card_digest_redraw(plan, user_id="u1")

    # Background resume loop scans immediately on start and exits cleanly on cancel
    scan_ran = asyncio.Event()

    async def _fake_once(*, revoker=None):
        scan_ran.set()
        return []

    with patch.object(purge_mod, "run_personal_purge_resume_once", autospec=True, side_effect=_fake_once):
        task = asyncio.create_task(purge_mod.run_personal_purge_resume_loop(poll_interval_s=60.0))
        await asyncio.wait_for(scan_ran.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# 10. r3 additions: real _summarization_event, partial failures, Dalton=0 gate, restore.sh pre-mutation fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_compaction_summarization_event_redaction_and_pre_touch_preservation():
    """Real compaction stores summary_message inside ``_summarization_event`` (plus context_summary & ui).
    Overlapping summaries redact all 3 copies and drop SIDECAR_KEY while preserving event metadata;
    pre-touch summaries remain untouched.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    serde = JsonPlusSerializer()
    tid = str(uuid.uuid4())
    r0 = str(uuid.uuid4())
    r1 = str(uuid.uuid4())

    sum_pre = build_summary_message(
        "Pre-touch research summary of turn 0",
        original_message_count=4,
        span=SummarySpan(0, 0),
        native={"type": "compaction", "content": "pre-native-keep"},
        origin="anthropic",
    )
    sum_cross = build_summary_message(
        "Cross-boundary summary leaking turn 1 personal calendar dinner",
        original_message_count=8,
        span=SummarySpan(0, 1),
        native={"type": "compaction", "content": "secret-native-block"},
        origin="anthropic",
    )
    assert SIDECAR_KEY in sum_pre.additional_kwargs
    assert SIDECAR_KEY in sum_cross.additional_kwargs

    ev_pre = {
        "cutoff_index": 2,
        "summary_message": sum_pre,
        "file_path": "/workspace/history-pre.md",
        "anchor_message_id": "m-pre-anchor",
    }
    ev_cross = {
        "cutoff_index": 5,
        "summary_message": sum_cross,
        "file_path": "/workspace/history-cross.md",
        "anchor_message_id": "m-cross-anchor",
    }

    ai_pre = AIMessage(
        content="Turn 0 public answer",
        id="ai-0",
        response_metadata={
            COMPACTION_SUMMARY_KEY: {
                "summary_message_id": sum_pre.id,
                "summary_text": "Pre-touch research summary of turn 0",
                "summary_length": 36,
            }
        },
    )
    ai_cross = AIMessage(
        content="Turn 0 second answer carrying cross summary stamp",
        id="ai-0b",
        response_metadata={
            COMPACTION_SUMMARY_KEY: {
                "summary_message_id": sum_cross.id,
                "summary_text": "Cross-boundary summary leaking turn 1 personal calendar dinner",
                "summary_length": 62,
            }
        },
    )

    m0_t, m0_b = serde.dumps_typed(_DeltaSnapshot([HumanMessage(content="NVDA Q0", id="m0"), ai_pre, ai_cross]))
    e0_t, e0_b = serde.dumps_typed(ev_pre)
    e1_t, e1_b = serde.dumps_typed(ev_cross)
    u0_t, u0_b = serde.dumps_typed(
        [
            {
                "type": "ui",
                "id": "ui-pre",
                "name": "context_window",
                "props": {"action": "summarize"},
                "metadata": {
                    "summary_message_id": sum_pre.id,
                    "replaced_summary": {
                        "id": sum_pre.id,
                        "action": "summarize",
                        "summary_text": "Pre-touch replaced summary",
                        SIDECAR_KEY: {"content": "keep-pre"},
                    },
                },
            },
            {
                "type": "ui",
                "id": "ui-cross",
                "name": "context_window",
                "props": {"action": "summarize"},
                "metadata": {
                    "summary_message_id": sum_cross.id,
                    "replaced_summary": {
                        "id": sum_cross.id,
                        "action": "summarize",
                        "summary_text": "Cross replaced summary leaking dinner",
                        SIDECAR_KEY: {"content": "drop-cross"},
                    },
                },
            },
        ]
    )

    all_cps = [
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-0",
            "parent_checkpoint_id": None,
            "checkpoint": {"channel_versions": {"messages": "v0", "ui": "u0", "_summarization_event": "e0"}},
            "metadata": {"turn_index": 0, "run_id": r0},
        },
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
            "parent_checkpoint_id": "cp-0",
            "checkpoint": {"channel_versions": {"messages": "v0", "ui": "u0", "_summarization_event": "e1"}},
            "metadata": {"turn_index": 1, "run_id": r1},
        },
    ]
    writes = [
        {"checkpoint_ns": "", "checkpoint_id": "cp-0", "task_id": "t0", "idx": 0, "channel": "_summarization_event", "type": e0_t, "blob": e0_b},
        {"checkpoint_ns": "", "checkpoint_id": "cp-1", "task_id": "t1", "idx": 0, "channel": "_summarization_event", "type": e1_t, "blob": e1_b},
    ]
    blobs = [
        {"checkpoint_ns": "", "channel": "messages", "version": "v0", "type": m0_t, "blob": m0_b},
        {"checkpoint_ns": "", "channel": "ui", "version": "u0", "type": u0_t, "blob": u0_b},
        {"checkpoint_ns": "", "channel": "_summarization_event", "version": "e0", "type": e0_t, "blob": e0_b},
        {"checkpoint_ns": "", "channel": "_summarization_event", "version": "e1", "type": e1_t, "blob": e1_b},
    ]

    cur = _FakeCursor(fetchall_seq=[all_cps, writes, blobs])
    cp_conn = _FakeConn(cur)

    updated = await purge_mod._purge_thread_checkpoints(
        cp_conn,
        serde,
        thread_id=tid,
        min_turn=1,
        max_turn=1,
        run_turn_map={r0: 0, r1: 1},
        suffix_run_ids={r1},
        suffix_task_ids=set(),
    )
    assert updated == 4  # write e1 + blobs v0, u0, e1 (pre-touch e0 untouched)

    updated_blobs: dict[tuple[str, str], object] = {}
    updated_writes: dict[tuple[str, str], object] = {}
    for sql, params in cur.executed:
        if "UPDATE checkpoint_blobs" in sql:
            new_type, new_blob, _t, _ns, ch, ver = params  # type: ignore[misc]
            updated_blobs[(ch, ver)] = serde.loads_typed((new_type, new_blob))
        elif "UPDATE checkpoint_writes" in sql:
            new_type, new_blob, _t, _ns, cid, _tk, _idx = params  # type: ignore[misc]
            updated_writes[(cid, "_summarization_event")] = serde.loads_typed((new_type, new_blob))

    # Pre-touch _summarization_event e0 was NOT rewritten at all
    assert ("_summarization_event", "e0") not in updated_blobs
    assert ("cp-0", "_summarization_event") not in updated_writes

    # Overlapping _summarization_event e1 rewritten in both blob and write, metadata preserved, SIDECAR_KEY removed
    for rewritten_ev in (updated_blobs[("_summarization_event", "e1")], updated_writes[("cp-1", "_summarization_event")]):
        assert isinstance(rewritten_ev, dict)
        assert rewritten_ev["cutoff_index"] == 5
        assert rewritten_ev["file_path"] == "/workspace/history-cross.md"
        assert rewritten_ev["anchor_message_id"] == "m-cross-anchor"
        sm = rewritten_ev["summary_message"]
        assert sm.content == PURGED_PLACEHOLDER
        assert SIDECAR_KEY not in sm.additional_kwargs

    # Copy 2: AIMessage.response_metadata.context_summary
    msgs_v0 = {m.id: m for m in updated_blobs[("messages", "v0")].value}
    assert msgs_v0["ai-0"].response_metadata[COMPACTION_SUMMARY_KEY]["summary_text"] == "Pre-touch research summary of turn 0"
    assert msgs_v0["ai-0b"].response_metadata[COMPACTION_SUMMARY_KEY]["summary_text"] == PURGED_PLACEHOLDER

    # Copy 3: ui.replaced_summary
    ui_u0 = {r["id"]: r for r in updated_blobs[("ui", "u0")]}
    assert ui_u0["ui-pre"]["metadata"]["replaced_summary"]["summary_text"] == "Pre-touch replaced summary"
    assert SIDECAR_KEY in ui_u0["ui-pre"]["metadata"]["replaced_summary"]
    assert ui_u0["ui-cross"]["metadata"]["replaced_summary"]["summary_text"] == PURGED_PLACEHOLDER
    assert SIDECAR_KEY not in ui_u0["ui-cross"]["metadata"]["replaced_summary"]


@pytest.mark.asyncio
async def test_partial_failures_decode_and_same_db_missing_checkpoints():
    """Corrupt checkpoint blob/write and missing public.checkpoints in same-DB fallback must raise RuntimeError."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from src.server.services import writer_guard

    serde = JsonPlusSerializer()
    tid = str(uuid.uuid4())
    r0 = str(uuid.uuid4())
    all_cps = [
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-corrupt",
            "parent_checkpoint_id": None,
            "checkpoint": {"channel_versions": {"messages": "v-bad"}},
            "metadata": {"turn_index": 0, "run_id": r0},
        }
    ]
    secret_payload = b"SENSITIVE_RAW_BLOB_DO_NOT_LEAK"
    bad_blobs = [
        {"checkpoint_ns": "", "channel": "messages", "version": "v-bad", "type": "json", "blob": secret_payload}
    ]
    cur = _FakeCursor(fetchall_seq=[all_cps, [], bad_blobs])
    cp_conn = _FakeConn(cur)

    with pytest.raises(RuntimeError) as exc_blob:
        await purge_mod._purge_thread_checkpoints(
            cp_conn,
            serde,
            thread_id=tid,
            min_turn=0,
            max_turn=0,
            run_turn_map={r0: 0},
            suffix_run_ids={r0},
            suffix_task_ids=set(),
        )
    msg = str(exc_blob.value)
    assert tid in msg and "cp-corrupt" in msg and "messages" in msg
    assert "SENSITIVE_RAW_BLOB_DO_NOT_LEAK" not in msg

    # Same-DB fallback with to_regclass('public.checkpoints') returning None raises RuntimeError
    import dataclasses

    pid = str(uuid.uuid4())
    running_ledger = dataclasses.replace(
        _sample_ledger(purge_id=pid),
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={tid: {"min_turn": 0, "max_turn": 0}},
    )
    # fetchall sequence: touches=[], live_runs=[], suffix_tasks=[], all_responses=[]
    # fetchone sequence: thread_key lock=(True,),
    #   Phase 0 guard (ledger, thread), workspace_id={"workspace_id": None},
    #   Phase 1 guard (ledger, thread), to_regclass={"tbl": None}
    no_cp_conn = _FakeConn(
        _FakeCursor(
            fetchone_seq=[
                (True,),
                {"local_purge_status": "running", "local_purge_attempt_gen": 1},
                {"personal_purge_in_progress": True},
                {"workspace_id": None},
                {"local_purge_status": "running", "local_purge_attempt_gen": 1},
                {"personal_purge_in_progress": True},
                {"tbl": None},
            ],
            fetchall_seq=[[], [], [], []],
        )
    )

    @asynccontextmanager
    async def _get_no_cp_conn(_conn=None):
        yield no_cp_conn

    @asynccontextmanager
    async def _lock_ok(_key):
        yield True

    @asynccontextmanager
    async def _thread_lock_ok(_tid, *, timeout_s=120.0, poll_interval_s=0.05):
        yield

    with (
        patch.object(session_lock, "try_own_session_lock", autospec=True, side_effect=_lock_ok),
        patch.object(purge_mod, "acquire_purge_thread_lock", autospec=True, side_effect=_thread_lock_ok),
        patch.object(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=running_ledger)),
        patch.object(db, "claim_local_purge_attempt", create_autospec(db.claim_local_purge_attempt, return_value=1)),
        patch.object(db, "renew_local_purge_attempt", create_autospec(db.renew_local_purge_attempt, return_value=True)),
        patch.object(db, "release_local_purge_attempt", create_autospec(db.release_local_purge_attempt, return_value=None)),
        patch.object(purge_mod, "get_db_connection", _get_no_cp_conn),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=no_cp_conn),
        ),
        patch.object(writer_guard, "held_task_namespaces", create_autospec(writer_guard.held_task_namespaces, return_value=set())),
        patch.object(purge_mod, "get_checkpointer", autospec=True, return_value=SimpleNamespace(conn=None, serde=serde)),
    ):
        with pytest.raises(RuntimeError, match="public.checkpoints"):
            await purge_mod.run_local_purge(pid)


@pytest.mark.asyncio
async def test_dalton_disabled_skips_all_purge_fence_sql(monkeypatch: pytest.MonkeyPatch):
    """When DALTON_ENABLED=0, all upstream-facing purge_fence helpers execute zero Dalton SQL."""
    from src.server.database import thread_transcripts as tt_db
    from src.server.database.search import index as search_db

    monkeypatch.setenv("DALTON_ENABLED", "0")
    tid = str(uuid.uuid4())
    dst_tid = str(uuid.uuid4())

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    conn.cursor.return_value.__aenter__.return_value = cur

    class _NoSqlConn(_FakeConn):
        async def execute(self, sql: str, params=None):
            raise AssertionError(f"Unexpected SQL when DALTON_ENABLED=0: {sql}")

    class _NoSqlCursor(_FakeCursor):
        async def execute(self, sql: str, params=None):
            raise AssertionError(f"Unexpected SQL when DALTON_ENABLED=0: {sql}")

    no_sql_conn = _NoSqlConn(_NoSqlCursor())

    @asynccontextmanager
    async def _never_db(_conn=None):
        yield no_sql_conn

    with patch.object(purge_fence, "get_db_connection", _never_db):
        for target_conn in (conn, no_sql_conn):
            await check_thread_not_purging_fast(tid)
            await assert_thread_not_purging_tx(target_conn, tid)
            await assert_fork_source_not_purging_tx(target_conn, tid, cut_turn_index=0, expected_purge_gen=0)
            assert await clip_purged_ranges_on_truncate_tx(target_conn, tid, 1) == []
            await copy_personal_integration_state_for_fork_tx(
                target_conn,
                source_thread_id=tid,
                target_thread_id=dst_tid,
                cut=ThroughTurn(0),
                kept_turn_indexes=[0],
                run_map={},
            )
            assert await read_thread_purge_state(tid, conn=target_conn) is None
            assert await read_expected_purge_generation(tid, conn=target_conn) is None
            assert await verify_writeback_fence_tx(target_conn, tid, expected_purge_gen=0) is True
            assert await check_writeback_fence_tx(target_conn, tid, expected_purge_gen=0) is True
            assert await is_personal_integration_thread(tid, conn=target_conn) is False
            assert await should_skip_memory_extraction_for_thread(tid, conn=target_conn) is False

    conn.execute.assert_not_called()
    cur.execute.assert_not_called()
    assert no_sql_conn.executed == []
    assert no_sql_conn._cursor.executed == []

    # build_guarded_search_rows ignores purged ranges and returns expected_purge_gen=None
    now = datetime.now(UTC)
    src = search_db.TurnSource(0, str(uuid.uuid4()), "Q0", "user", now)
    rows, gen = build_guarded_search_rows(
        [src],
        SimpleNamespace(texts={src.response_id: "A0"}, unread=set()),
        (False, 5, [[0, 0]]),
        lambda s: s or "",
    )
    assert gen is None
    assert rows[0].user_text == "Q0"
    assert rows[0].answer_text == "A0"

    # save_stored_transcript_fenced skips fence SQL and calls save_stored directly
    save_mock = create_autospec(tt_db.save_stored, return_value=True)
    with patch.object(tt_db, "save_stored", save_mock):
        assert await save_stored_transcript_fenced(
            tid, "", "u1", SimpleNamespace(), {}, expected_purge_gen=0
        ) is True
    save_mock.assert_awaited_once()
    assert "conn" not in save_mock.call_args.kwargs

    # write_many_fenced runs plain executemany without calling verify_writeback_fence_tx
    verify_mock = create_autospec(purge_fence.verify_writeback_fence_tx)
    plain_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    plain_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    plain_conn.cursor.return_value.__aenter__.return_value = plain_cur

    @asynccontextmanager
    async def _plain_db(_conn=None):
        yield plain_conn

    from src.server.database import pool as pool_mod

    with (
        patch.object(pool_mod, "get_db_connection", _plain_db),
        patch.object(purge_fence, "verify_writeback_fence_tx", verify_mock),
    ):
        await write_many_fenced("INSERT 1", [(1,)], "slice", thread_id=tid, expected_purge_gen=0)
    verify_mock.assert_not_awaited()
    plain_cur.executemany.assert_awaited_once()


def test_restore_script_fails_before_any_mutation_when_reset_sql_missing(tmp_path: Path):
    """When work_dir lacks personal_integrations_restore_reset.sql, restore.sh exits non-zero
    in Step 3 before running pg_restore, MinIO restore, or CREATE DATABASE.
    """
    proj = f"r3-miss-sql-{uuid.uuid4().hex[:10]}"
    work_dir = tmp_path / "work"
    bak_dir = tmp_path / "backup"
    bin_dir = tmp_path / "bin"
    work_dir.mkdir()
    bak_dir.mkdir()
    bin_dir.mkdir()

    (work_dir / "docker-compose.yml").write_text("services:\n  backend:\n    env_file: .env\n", encoding="utf-8")
    (work_dir / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
    (work_dir / "agent_config.yaml").write_text('storage:\n  provider: "none"\n', encoding="utf-8")
    (work_dir / ".env").write_text(
        f"COMPOSE_PROJECT_NAME={proj}\n"
        "DB_HOST=postgres\nDB_USER=postgres\nDB_PASSWORD=pgtest\nDB_NAME=postgres\n"
        "DALTON_TLS_MODE=loopback\nDALTON_EDGE_PORT=8088\n",
        encoding="utf-8",
    )
    (work_dir / ".env").chmod(0o600)

    (bak_dir / "postgres.dump").write_bytes(b"PGDMP-fake")
    (bak_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "compose_project": proj, "db_name": "postgres"}),
        encoding="utf-8",
    )

    calls_log = tmp_path / "docker-calls.log"
    (bin_dir / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "flock").chmod(0o755)
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls_log}"\n'
        'if [[ "$*" == *" config --format json"* ]]; then echo \'{"services":{"backend":{"environment":{}}}}\'; exit 0; fi\n'
        'if [[ "$*" == *" ps --status running --services"* ]]; then echo "postgres"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    proj_lock = Path(f"/tmp/dalton-{proj}.restore.lock")
    try:
        res = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/restore.sh"),
                "--work-dir",
                str(work_dir),
                "--backup-dir",
                str(bak_dir),
                "--skip-minio",
                "--skip-caddy",
                "--skip-edge-build",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res.returncode != 0
        assert "personal_integrations_restore_reset.sql" in res.stderr
        logged = calls_log.read_text(encoding="utf-8") if calls_log.exists() else ""
        assert "pg_restore" not in logged
        assert "MINIO_RESTORE" not in logged and "minio" not in logged.lower()
        assert "CREATE DATABASE" not in logged
    finally:
        if proj_lock.is_dir():
            proj_lock.rmdir()
        elif proj_lock.exists():
            proj_lock.unlink()


# ---------------------------------------------------------------------------
# 11. r4 narrow fixes: decode error chain hygiene, Redis startup recovery, 1-slot pool drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_decode_failure_strips_exception_chain_and_never_logs_payload(
    caplog: pytest.LogCaptureFixture,
):
    """Decoding a legacy JSON checkpoint write or blob that fails inside JsonPlusSerializer
    must still fail closed (ledger stays running) while stripping __cause__/__context__ so
    wait-to-purge payload text never reaches logs or tracebacks.
    """
    import dataclasses
    import traceback

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from src.dalton.services import user_integrations_lifecycle as lifecycle_mod

    serde = JsonPlusSerializer()
    secret_write = "SECRET_GMAIL_98765"
    secret_blob = "SECRET_CAL_54321"

    legacy_write_payload = json.dumps(
        {
            "lc": 1,
            "type": "constructor",
            "id": ["langchain_core", "messages", "human", "HumanMessage"],
            "kwargs": {"content": {"secret_email": secret_write}},
        }
    ).encode("utf-8")
    legacy_blob_payload = json.dumps(
        {
            "lc": 1,
            "type": "constructor",
            "id": ["langchain_core", "messages", "human", "HumanMessage"],
            "kwargs": {"content": {"secret_event": secret_blob}},
        }
    ).encode("utf-8")

    # Sanity check: raw JsonPlusSerializer error genuinely embeds the secret payload
    with pytest.raises(Exception) as raw_write_exc:
        serde.loads_typed(("json", legacy_write_payload))
    assert secret_write in str(raw_write_exc.value)
    with pytest.raises(Exception) as raw_blob_exc:
        serde.loads_typed(("json", legacy_blob_payload))
    assert secret_blob in str(raw_blob_exc.value)

    tid = str(uuid.uuid4())
    r0 = str(uuid.uuid4())
    all_cps = [
        {
            "checkpoint_ns": "",
            "checkpoint_id": "cp-legacy-0",
            "parent_checkpoint_id": None,
            "checkpoint": {"channel_versions": {"messages": "v-legacy-0"}},
            "metadata": {"turn_index": 0, "run_id": r0},
        }
    ]

    # (1) Write decode failure: location metadata preserved, exception chain stripped
    write_cur = _FakeCursor(
        fetchall_seq=[
            all_cps,
            [
                {
                    "checkpoint_ns": "",
                    "checkpoint_id": "cp-legacy-0",
                    "task_id": "t0",
                    "idx": 0,
                    "channel": "messages",
                    "type": "json",
                    "blob": legacy_write_payload,
                }
            ],
            [],
        ]
    )
    with pytest.raises(RuntimeError) as write_err:
        await purge_mod._purge_thread_checkpoints(
            _FakeConn(write_cur),
            serde,
            thread_id=tid,
            min_turn=0,
            max_turn=0,
            run_turn_map={r0: 0},
            suffix_run_ids={r0},
            suffix_task_ids=set(),
        )
    assert write_err.value.__cause__ is None
    assert write_err.value.__context__ is None
    write_tb = "".join(
        traceback.format_exception(
            type(write_err.value), write_err.value, write_err.value.__traceback__
        )
    )
    assert tid in str(write_err.value)
    assert "cp-legacy-0" in str(write_err.value)
    assert "messages" in str(write_err.value)
    assert secret_write not in write_tb

    # (2) Blob decode failure through run_personal_purge_resume_once with exc_info=True logging
    pid = str(uuid.uuid4())
    running_ledger = dataclasses.replace(
        _sample_ledger(purge_id=pid),
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={tid: {"min_turn": 0, "max_turn": 0}},
    )
    blob_cur = _FakeCursor(
        fetchall_seq=[
            all_cps,
            [],
            [
                {
                    "checkpoint_ns": "",
                    "checkpoint_id": "cp-legacy-0",
                    "channel": "messages",
                    "version": "v-legacy-0",
                    "type": "json",
                    "blob": legacy_blob_payload,
                }
            ],
        ]
    )

    async def _failing_finish(purge_id: str, *, revoker=None):
        assert purge_id == pid
        await purge_mod._purge_thread_checkpoints(
            _FakeConn(blob_cur),
            serde,
            thread_id=tid,
            min_turn=0,
            max_turn=0,
            run_turn_map={r0: 0},
            suffix_run_ids={r0},
            suffix_task_ids=set(),
        )

    caplog.clear()
    with (
        caplog.at_level(logging.WARNING, logger=purge_mod.logger.name),
        patch.object(
            db,
            "list_running_local_purges",
            create_autospec(db.list_running_local_purges, return_value=[running_ledger]),
        ),
        patch.object(
            lifecycle_mod,
            "finish_disconnect_locked",
            create_autospec(lifecycle_mod.finish_disconnect_locked, side_effect=_failing_finish),
        ),
    ):
        resumed = await purge_mod.run_personal_purge_resume_once()

    assert resumed == []
    assert any(pid in rec.getMessage() for rec in caplog.records)
    formatted_logs = "\n".join(
        logging.Formatter().format(rec) for rec in caplog.records
    )
    assert "cp-legacy-0" in formatted_logs
    assert tid in formatted_logs
    assert secret_blob not in formatted_logs
    assert secret_blob not in caplog.text


@pytest.mark.asyncio
async def test_redis_startup_failure_recovers_on_same_worker_and_deletes_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    """When Redis is temporarily down at startup (leaving real RedisCacheClient.enabled=False),
    subsequent purge retries on the same worker must fail while unavailable and recover
    to delete keys once Redis becomes reachable again.
    """
    from src.utils.cache import redis_cache as redis_cache_mod

    class _ToggleRedis:
        def __init__(self) -> None:
            self.available = False
            self.deleted_calls: list[tuple[str, ...]] = []

        async def ping(self) -> bool:
            if not self.available:
                raise ConnectionError("redis temporarily down")
            return True

        async def delete(self, *keys: str) -> int:
            if not self.available:
                raise ConnectionError("redis temporarily down")
            self.deleted_calls.append(tuple(keys))
            return len(keys)

        async def aclose(self) -> None:
            return None

    class _DummyPool:
        async def disconnect(self) -> None:
            return None

    fake_redis = _ToggleRedis()
    monkeypatch.setenv("REDIS_CACHE_ENABLED", "true")

    with (
        patch("src.config.settings.is_redis_cache_enabled", autospec=True, return_value=True),
        patch.object(
            redis_cache_mod, "is_redis_cache_enabled", autospec=True, return_value=True
        ),
        patch.object(
            redis_cache_mod.BlockingConnectionPool,
            "from_url",
            autospec=True,
            return_value=_DummyPool(),
        ),
        patch.object(
            redis_cache_mod.redis,
            "Redis",
            autospec=True,
            return_value=fake_redis,
        ),
    ):
        real_cache = redis_cache_mod.RedisCacheClient(url="redis://localhost:6379/0")
        with pytest.raises(ConnectionError):
            await real_cache.connect()
        assert real_cache.enabled is False  # matches setup.py startup degradation

        with patch.object(
            redis_cache_mod, "get_cache_client", autospec=True, return_value=real_cache
        ):
            # Retry 1 while Redis is still down: fails closed, 0 delete calls
            with pytest.raises(RuntimeError, match="unavailable"):
                await purge_mod._delete_redis_keys(["stream:t1:r1", "meta:t1:r1"])
            assert fake_redis.deleted_calls == []
            assert real_cache.enabled is False

            # Redis comes back on the same worker
            fake_redis.available = True
            deleted = await purge_mod._delete_redis_keys(["stream:t1:r1", "meta:t1:r1"])
            assert deleted == 2
            assert fake_redis.deleted_calls == [("stream:t1:r1", "meta:t1:r1")]
            assert real_cache.enabled is True

        # Also cover startup failure before client creation (client=None, enabled=False)
        real_cache_no_client = redis_cache_mod.RedisCacheClient(url="redis://localhost:6379/0")
        real_cache_no_client.enabled = False
        real_cache_no_client.client = None
        fake_redis.available = False
        fake_redis.deleted_calls.clear()

        with patch.object(
            redis_cache_mod,
            "get_cache_client",
            autospec=True,
            return_value=real_cache_no_client,
        ):
            with pytest.raises(RuntimeError, match="unavailable"):
                await purge_mod._delete_redis_keys(["stream:t2:r2"])
            assert real_cache_no_client.enabled is False
            assert fake_redis.deleted_calls == []

            fake_redis.available = True
            deleted2 = await purge_mod._delete_redis_keys(["stream:t2:r2"])
            assert deleted2 == 1
            assert fake_redis.deleted_calls == [("stream:t2:r2",)]
            assert real_cache_no_client.enabled is True


@pytest.mark.asyncio
async def test_cancel_and_drain_succeeds_under_single_slot_pool_for_settled_subagent():
    """With POSTGRES_CONVERSATION_POOL_MAX=1, _cancel_and_drain_thread holds T(thread) on an
    out-of-pool session so real writer_guard.held_task_namespaces() can still borrow the single
    pooled slot without timing out — while still blocking if a completed task's lock is held.
    """
    from psycopg_pool import PoolTimeout

    from src.server.database import pool as pool_mod
    from src.server.services import writer_guard

    tid = str(uuid.uuid4())
    trid = str(uuid.uuid4())
    task_id = "task-settled-1"

    class _SingleSlotPoolDouble:
        def __init__(self, pg_locks_rows: list[tuple[int, int]]) -> None:
            self._checked_out = False
            self._pg_locks_rows = pg_locks_rows

        @asynccontextmanager
        async def get_db_connection(self, conn=None):
            if conn is not None:
                yield conn
                return
            if self._checked_out:
                raise PoolTimeout("single-slot pool exhausted")
            self._checked_out = True
            try:
                cur = _FakeCursor(
                    fetchall_seq=[
                        [],  # live_runs = []
                        [{"task_id": task_id, "task_run_id": trid, "status": "completed"}],
                    ]
                )

                class _SlotCursor(_FakeCursor):
                    async def fetchall(self_inner):
                        last_sql = self_inner.executed[-1][0] if self_inner.executed else ""
                        if "FROM pg_locks" in last_sql:
                            return list(self._pg_locks_rows)
                        return await super().fetchall()

                yield _FakeConn(_SlotCursor(fetchall_seq=cur._fetchall_seq))
            finally:
                self._checked_out = False

    # Case 1: Settled completed subagent (no lock in pg_locks) drains cleanly on 1-slot pool
    pool_free = _SingleSlotPoolDouble(pg_locks_rows=[])
    session_conn_free = _FakeConn(_FakeCursor(fetchone_seq=[(True,)]))
    with (
        patch.object(purge_mod, "get_db_connection", pool_free.get_db_connection),
        patch.object(pool_mod, "get_db_connection", pool_free.get_db_connection),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=session_conn_free),
        ),
    ):
        await purge_mod._cancel_and_drain_thread(tid, min_turn=0, max_turn=2, timeout_s=0.5)
    assert session_conn_free.closed is True

    # Case 2: Completed subagent STILL holding N(thread, task:...) in pg_locks -> still times out
    unsigned = writer_guard.namespace_key(tid, f"task:{task_id}") & 0xFFFFFFFFFFFFFFFF
    held_pair = (unsigned >> 32, unsigned & 0xFFFFFFFF)
    pool_held = _SingleSlotPoolDouble(pg_locks_rows=[held_pair])
    session_conn_held = _FakeConn(_FakeCursor(fetchone_seq=[(True,), (True,), (True,)]))
    with (
        patch.object(purge_mod, "get_db_connection", pool_held.get_db_connection),
        patch.object(pool_mod, "get_db_connection", pool_held.get_db_connection),
        patch.object(
            purge_mod.db_pool,
            "open_session_connection",
            create_autospec(purge_mod.db_pool.open_session_connection, return_value=session_conn_held),
        ),
        pytest.raises(TimeoutError),
    ):
        await purge_mod._cancel_and_drain_thread(tid, min_turn=0, max_turn=2, timeout_s=0.05)
    assert session_conn_held.closed is True


@pytest.mark.asyncio
async def test_r8_purge_attempt_gen_and_thread_blocked_recheck_aborts_stale_worker():
    """Round 8 Decision 2 & 3: inside each destructive phase tx after acquiring thread lock,
    _purge_single_thread_layers re-verifies local_purge_attempt_gen and personal_purge_in_progress,
    aborting without issuing any destructive DELETE/UPDATE when superseded or unblocked;
    render.py also enforces assert_no_advisory_lock_held before sandbox calls.
    """
    from src.dalton.conversations import render as render_mod
    from src.dalton.integrations.lock_guard import advisory_lock_http_guard, track_advisory_lock

    pid = str(uuid.uuid4())
    tid = str(uuid.uuid4())

    # (a) Phase 0 passes (gen=1, blocked=True), Phase 2 App DB destructive tx sees gen=2 (taken over!) -> aborts with ZERO DELETE/UPDATE
    cursor = _FakeCursor(
        fetchone_seq=[
            {"local_purge_status": "running", "local_purge_attempt_gen": 1},
            {"personal_purge_in_progress": True},
            {"workspace_id": None},
            {"local_purge_status": "running", "local_purge_attempt_gen": 2},  # superseded!
        ],
        fetchall_seq=[[]],
    )
    conn = _FakeConn(cursor)

    @asynccontextmanager
    async def _get_conn(_c=None):
        yield conn

    report = purge_mod.LocalPurgeReport(purge_id=pid)
    with patch.object(purge_mod, "get_db_connection", _get_conn):
        ok = await purge_mod._purge_single_thread_layers(
            purge_id=pid,
            claimed_attempt_gen=1,
            tid=tid,
            min_turn=0,
            max_turn=0,
            user_id="u1",
            cp_pool=None,
            cp_serde=None,
            report=report,
            thread_workspace_ids={},
            all_suffix_run_ids=[],
            blobs_to_release=[],
            redis_keys_to_delete=[],
            card_digest_plans=[],
        )
    assert ok is False
    destructive_sql = [
        sql for sql, _ in cursor.executed
        if any(tok in sql.upper() for tok in ("DELETE FROM", "UPDATE CONVERSATION_", "UPDATE HOOK_OUTBOX", "UPDATE THREAD_SEARCH"))
    ]
    assert destructive_sql == []

    # (b) Phase 2 sees matching gen=1 but personal_purge_in_progress=False (already unblocked!) -> aborts with ZERO DELETE/UPDATE
    cursor2 = _FakeCursor(
        fetchone_seq=[
            {"local_purge_status": "running", "local_purge_attempt_gen": 1},
            {"personal_purge_in_progress": True},
            {"workspace_id": None},
            {"local_purge_status": "running", "local_purge_attempt_gen": 1},
            {"personal_purge_in_progress": False},  # already unblocked!
        ],
        fetchall_seq=[[]],
    )
    conn2 = _FakeConn(cursor2)

    @asynccontextmanager
    async def _get_conn2(_c=None):
        yield conn2

    with patch.object(purge_mod, "get_db_connection", _get_conn2):
        ok2 = await purge_mod._purge_single_thread_layers(
            purge_id=pid,
            claimed_attempt_gen=1,
            tid=tid,
            min_turn=0,
            max_turn=0,
            user_id="u1",
            cp_pool=None,
            cp_serde=None,
            report=report,
            thread_workspace_ids={},
            all_suffix_run_ids=[],
            blobs_to_release=[],
            redis_keys_to_delete=[],
            card_digest_plans=[],
        )
    assert ok2 is False
    destructive_sql2 = [
        sql for sql, _ in cursor2.executed
        if any(tok in sql.upper() for tok in ("DELETE FROM", "UPDATE CONVERSATION_", "UPDATE HOOK_OUTBOX", "UPDATE THREAD_SEARCH"))
    ]
    assert destructive_sql2 == []

    # (c) render.py refuses sandbox write/remove if an advisory lock is held
    class _StubBackend:
        async def awrite_text(self, path: str, text: str) -> bool:
            return True

        async def aexecute(self, cmd: str) -> SimpleNamespace:
            return SimpleNamespace(exit_code=0)

    stub_be = _StubBackend()
    with advisory_lock_http_guard(True), track_advisory_lock("purge_thread", 99):
        with pytest.raises(RuntimeError, match="advisory lock"):
            await render_mod.write_conversation_file_to_backend(
                None, "research/topics/conversations/2026-11-26.md", "# hi", backend=stub_be
            )
        with pytest.raises(RuntimeError, match="advisory lock"):
            await render_mod.remove_conversation_file_from_backend(
                None, "research/topics/conversations/2026-11-26.md", backend=stub_be
            )


@pytest.mark.asyncio
async def test_snapshot_fork_preserves_personal_touches_and_purge_covers_fork_and_subsequent_fork():
    """Snapshot fork (empty main-turn run_map) inherits personal-integration touches and
    private flag across fork and fork-of-fork, and disconnect purge covers & redacts the
    fork's materialized snapshot checkpoint."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    from src.dalton.services import user_integrations_lifecycle as lifecycle_mod
    from src.server.services.forks.model import BorrowedSegment, Borrowing, ForkLineage

    uid = "user-fork-cal"
    t0 = str(uuid.uuid4())
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())
    r1 = str(uuid.uuid4())
    now = datetime.now(UTC)

    # Mini in-memory tables covering conversation_threads & dalton_thread_integration_touches
    threads_tbl: dict[str, dict[str, object]] = {
        t0: {
            "has_personal_integration_data": True,
            "personal_purged_turn_ranges": [],
            "is_shared": False,
            "workspace_shared_at": None,
            "metadata": {},
            "personal_purge_in_progress": False,
            "personal_purge_generation": 0,
        },
        t1: {
            "has_personal_integration_data": False,
            "personal_purged_turn_ranges": [],
            "is_shared": True,
            "workspace_shared_at": now,
            "metadata": {"fork": {"borrow": {"turn_base": 2}}},
            "personal_purge_in_progress": False,
            "personal_purge_generation": 0,
        },
        t2: {
            "has_personal_integration_data": False,
            "personal_purged_turn_ranges": [],
            "is_shared": True,
            "workspace_shared_at": now,
            "metadata": {"fork": {"borrow": {"turn_base": 2}}},
            "personal_purge_in_progress": False,
            "personal_purge_generation": 0,
        },
    }
    touches_tbl: list[dict[str, object]] = [
        {
            "thread_id": t0,
            "response_id": r1,
            "provider": "google",
            "service": "calendar",
            "auth_epoch": 1,
            "turn_index": 1,
            "user_id": uid,
            "provider_account_id": "acct-1",
            "touched_at": now,
            "purged_at": None,
        }
    ]

    class _ForkTouchCursor:
        def __init__(self) -> None:
            self._one: object = None
            self._all: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params=None):
            norm = " ".join(sql.split()).upper()
            self._one = None
            self._all = []
            if "SELECT PG_ADVISORY_XACT_LOCK" in norm:
                return
            if "SELECT HAS_PERSONAL_INTEGRATION_DATA, PERSONAL_PURGED_TURN_RANGES FROM CONVERSATION_THREADS" in norm:
                self._one = dict(threads_tbl[str(params[0])])
                return
            if "FROM DALTON_THREAD_INTEGRATION_TOUCHES" in norm and "GROUP BY THREAD_ID" in norm:
                by_tid: dict[str, list[int]] = {}
                for row in touches_tbl:
                    if (
                        row["user_id"] == params["user_id"]
                        and row["provider"] == params["provider"]
                        and (params["service"] is None or row["service"] == params["service"])
                        and int(row["auth_epoch"]) <= int(params["auth_epoch_upto"])
                        and row["purged_at"] is None
                    ):
                        by_tid.setdefault(str(row["thread_id"]), []).append(int(row["turn_index"]))
                self._all = [
                    {
                        "thread_id": tid,
                        "first_touched_turn_index": min(turns),
                        "max_touched_turn_index": max(turns),
                    }
                    for tid, turns in sorted(by_tid.items())
                ]
                return
            if "FROM DALTON_THREAD_INTEGRATION_TOUCHES" in norm and "TURN_INDEX <=" in norm:
                src_tid, max_turn = str(params[0]), int(params[1])
                self._all = [
                    dict(r)
                    for r in touches_tbl
                    if str(r["thread_id"]) == src_tid and int(r["turn_index"]) <= max_turn
                ]
                return
            if "UPDATE CONVERSATION_THREADS SET HAS_PERSONAL_INTEGRATION_DATA = TRUE" in norm:
                clipped, dst_tid = params[0].obj, str(params[1])
                threads_tbl[dst_tid]["has_personal_integration_data"] = True
                threads_tbl[dst_tid]["personal_purged_turn_ranges"] = clipped
                threads_tbl[dst_tid]["is_shared"] = False
                threads_tbl[dst_tid]["workspace_shared_at"] = None
                return
            if "INSERT INTO DALTON_THREAD_INTEGRATION_TOUCHES" in norm:
                rec = {
                    "thread_id": str(params[0]),
                    "response_id": str(params[1]),
                    "provider": params[2],
                    "service": params[3],
                    "auth_epoch": int(params[4]),
                    "turn_index": int(params[5]),
                    "user_id": params[6],
                    "provider_account_id": params[7],
                    "touched_at": params[8],
                    "purged_at": params[9],
                }
                pk = (rec["thread_id"], rec["response_id"], rec["provider"], rec["service"], rec["auth_epoch"])
                if not any(
                    (r["thread_id"], r["response_id"], r["provider"], r["service"], r["auth_epoch"]) == pk
                    for r in touches_tbl
                ):
                    touches_tbl.append(rec)
                return
            if "SELECT PERSONAL_PURGED_TURN_RANGES, METADATA FROM CONVERSATION_THREADS" in norm:
                self._one = dict(threads_tbl[str(params[0])])
                return
            if "SELECT GREATEST(" in norm:
                self._one = {"max_turn": int(params[2])}
                return
            if "UPDATE CONVERSATION_THREADS SET PERSONAL_PURGE_IN_PROGRESS = TRUE" in norm:
                merged, tid = params[0].obj, str(params[1])
                threads_tbl[tid]["personal_purge_in_progress"] = True
                threads_tbl[tid]["personal_purge_generation"] = int(threads_tbl[tid]["personal_purge_generation"]) + 1
                threads_tbl[tid]["personal_purged_turn_ranges"] = merged
                return
            raise AssertionError(f"Unexpected SQL: {norm}")

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._all)

    class _ForkTouchConn:
        def __init__(self) -> None:
            self._cur = _ForkTouchCursor()

        def cursor(self, **_kwargs):
            return self._cur

    conn = _ForkTouchConn()

    # 1. Snapshot fork T0 -> T1 at ThroughTurn(1) with EMPTY run_map (no main turns in run_map)
    seg_t0 = BorrowedSegment(
        thread_id=t0,
        first_turn=0,
        through_turn=1,
        response_id=r1,
        run_horizon=2,
        runs_through=2,
    )
    lineage_t1 = ForkLineage(
        kind="branch",
        thread_id=t0,
        cut="through_turn",
        turn_index=1,
        checkpoint_id="cp-cut-1",
        copy_scratchpad=False,
        tasks=0,
        title="Fork 1",
        at=now,
        by=uid,
        borrow=Borrowing(turn_base=2, checkpoint_id="cp-cut-1", ancestors=(seg_t0,)),
    )
    await copy_personal_integration_state_for_fork_tx(
        conn,
        source_thread_id=t0,
        target_thread_id=t1,
        cut=ThroughTurn(1),
        kept_turn_indexes=[0, 1],
        run_map={},
        lineage=lineage_t1,
    )
    assert threads_tbl[t1]["has_personal_integration_data"] is True
    assert threads_tbl[t1]["is_shared"] is False
    t1_touches = [r for r in touches_tbl if r["thread_id"] == t1]
    assert len(t1_touches) == 1
    assert t1_touches[0]["response_id"] == r1
    assert t1_touches[0]["turn_index"] == 1

    # 2. Fork-of-fork T1 -> T2 at BeforeTurn(2) (kept_turn_indexes=[] because T1 has no own turns < 2,
    #    but lineage.borrow.ancestors still borrows T0 turns 0..1)
    lineage_t2 = ForkLineage(
        kind="branch",
        thread_id=t1,
        cut="before_turn",
        turn_index=2,
        checkpoint_id="cp-cut-1",
        copy_scratchpad=False,
        tasks=0,
        title="Fork 2",
        at=now,
        by=uid,
        borrow=Borrowing(turn_base=2, checkpoint_id="cp-cut-1", ancestors=(seg_t0,)),
    )
    await copy_personal_integration_state_for_fork_tx(
        conn,
        source_thread_id=t1,
        target_thread_id=t2,
        cut=BeforeTurn(2),
        kept_turn_indexes=[],
        run_map={},
        lineage=lineage_t2,
    )
    assert threads_tbl[t2]["has_personal_integration_data"] is True
    assert threads_tbl[t2]["is_shared"] is False
    t2_touches = [r for r in touches_tbl if r["thread_id"] == t2]
    assert len(t2_touches) == 1
    assert t2_touches[0]["response_id"] == r1

    # 3. Disconnect Phase 1 covers T0, T1, AND T2
    insert_ledger_spy = create_autospec(db.insert_purge_ledger, return_value=None)
    with patch.object(lifecycle_mod.db, "insert_purge_ledger", new=insert_ledger_spy):
        _, purged_map = await lifecycle_mod._lock_and_register_phase1_tx(
            conn,
            user_id=uid,
            provider="google",
            service="calendar",
            oauth_client_id="cli-1",
            provider_account_id="acct-1",
            auth_epoch_upto=1,
            trigger_reason="user_disconnect",
            remote_revoke_status=RemoteRevokeStatus.PENDING,
            encrypted_revoke_token=None,
        )
    assert set(purged_map.keys()) == {t0, t1, t2}
    for tid in (t0, t1, t2):
        assert threads_tbl[tid]["personal_purge_in_progress"] is True
        assert threads_tbl[tid]["personal_purged_turn_ranges"] == [[1, 1]]

    # 4. Checkpoint purge redacts T1's materialized snapshot checkpoint (`source == "fork"`)
    serde = JsonPlusSerializer()
    snap_msgs = _DeltaSnapshot(
        [
            HumanMessage(id="m0", content="hello"),
            ToolMessage(
                id="m1-cal",
                content="Secret board meeting at 3pm",
                tool_call_id="tc-cal-1",
                name="google_calendar_list_events",
            ),
        ]
    )
    b_type, b_bytes = serde.dumps_typed(snap_msgs)
    blob_store = {("", "messages", "v1"): (b_type, b_bytes)}

    class _CpPurgeCursor:
        def __init__(self) -> None:
            self._all: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params=None):
            norm = " ".join(sql.split()).upper()
            self._all = []
            if "FROM CHECKPOINTS" in norm:
                self._all = [
                    {
                        "checkpoint_ns": "",
                        "checkpoint_id": "cp-cut-1",
                        "parent_checkpoint_id": None,
                        "checkpoint": {"channel_versions": {"messages": "v1"}},
                        "metadata": {"source": "fork", "step": 2, "parents": {}},
                    }
                ]
            elif "FROM CHECKPOINT_WRITES" in norm:
                self._all = []
            elif "FROM CHECKPOINT_BLOBS" in norm:
                self._all = [
                    {
                        "checkpoint_ns": ns,
                        "channel": ch,
                        "version": ver,
                        "type": bt,
                        "blob": bb,
                    }
                    for (ns, ch, ver), (bt, bb) in blob_store.items()
                ]
            elif "UPDATE CHECKPOINT_BLOBS" in norm:
                bt, bb, _tid, ns, ch, ver = params
                blob_store[(ns, ch, ver)] = (bt, bb)

        async def fetchall(self):
            return list(self._all)

    class _CpPurgeConn:
        def cursor(self, **_kwargs):
            return _CpPurgeCursor()

    updated = await purge_mod._purge_thread_checkpoints(
        _CpPurgeConn(),
        serde,
        thread_id=t1,
        min_turn=1,
        max_turn=1,
        run_turn_map={},
        suffix_run_ids=set(),
        suffix_task_ids=set(),
        fork_turn_base=2,
    )
    assert updated == 1
    redacted_snap = serde.loads_typed(blob_store[("", "messages", "v1")])
    assert isinstance(redacted_snap, _DeltaSnapshot)
    assert all("Secret board meeting" not in str(m.content) for m in redacted_snap.value)
    assert any(PURGED_PLACEHOLDER in str(m.content) for m in redacted_snap.value)

    # Conversely, if a clean fork (turn_base=2) only touched calendar later on own turn 2
    # (min_turn=2 >= fork_turn_base=2), the pre-touch fork snapshot is preserved.
    clean_snap = _DeltaSnapshot([HumanMessage(id="m0-clean", content="safe pre-touch")])
    cb_type, cb_bytes = serde.dumps_typed(clean_snap)
    blob_store[("", "messages", "v1")] = (cb_type, cb_bytes)
    updated_clean = await purge_mod._purge_thread_checkpoints(
        _CpPurgeConn(),
        serde,
        thread_id=t1,
        min_turn=2,
        max_turn=2,
        run_turn_map={},
        suffix_run_ids=set(),
        suffix_task_ids=set(),
        fork_turn_base=2,
    )
    assert updated_clean == 0
    kept_snap = serde.loads_typed(blob_store[("", "messages", "v1")])
    assert kept_snap.value[0].content == "safe pre-touch"
