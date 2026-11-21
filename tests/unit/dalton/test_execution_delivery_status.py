"""Unit tests for execution delivery status aggregation and summaries.

Covers all five delivery states (delivered, all failed, partial failed, retrying,
no delivery) across:
- ``summarize_execution_delivery`` & ``enrich_executions_with_delivery_summary``
- Chief of Staff ``<activity>`` (``src/tools/secretary/activity.py``)
- System jobs summary (``src/dalton/autonomy/system_jobs.py``)
- ``ThreadStatus`` automation listing & detail (``src/tools/thread_status/``)
- LiveFS automation state rendering (``src/server/services/automations/file.py``)
- Automations REST ``_runs_page`` (``src/server/app/automations.py``)
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest

from src.dalton.autonomy import system_jobs
from src.dalton.services import execution_delivery_status as eds
from src.server.app import automations as automations_router
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.services.automations import file as auto_file
from src.tools.secretary import activity
from src.tools.thread_status import listing as ts_listing
from src.tools.thread_status import tool as ts_tool

_ORIG_GET_DB_CONNECTION = eds.get_db_connection
_ORIG_IS_DALTON_ENABLED = eds.is_dalton_enabled


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 20, hour, minute, tzinfo=UTC)


def _mock_db_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    notify_rows: list[dict[str, Any]],
    channel_rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Patch ``eds.get_db_connection`` using ``create_autospec`` doubles."""
    executed_sql: list[str] = []
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    last_table = {"name": ""}

    async def _execute(sql: Any, params: Any = None) -> None:
        _ = params
        text = " ".join(str(sql).split())
        executed_sql.append(text)
        if "FROM dalton_channel_deliveries" in text:
            last_table["name"] = "channel"
        else:
            last_table["name"] = "notify"

    async def _fetchall() -> list[dict[str, Any]]:
        if last_table["name"] == "channel":
            return list(channel_rows or [])
        return list(notify_rows)

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @contextlib.asynccontextmanager
    async def _cur_cm(*_args: Any, **_kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cur_cm

    @contextlib.asynccontextmanager
    async def _conn_cm(_conn: Any = None):
        yield conn

    monkeypatch.setattr(
        eds,
        "get_db_connection",
        create_autospec(_ORIG_GET_DB_CONNECTION, side_effect=_conn_cm),
    )
    monkeypatch.setattr(
        eds,
        "is_dalton_enabled",
        create_autospec(_ORIG_IS_DALTON_ENABLED, return_value=True),
    )
    return executed_sql


# ---------------------------------------------------------------------------
# 1. Pure 5-state classification tests
# ---------------------------------------------------------------------------


def test_summarize_delivery_succeeded() -> None:
    summary = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-ok",
                "channel": "feishu",
                "target": "feishu:user:ou_richard",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report"},
            }
        ]
    )
    assert summary is not None
    assert summary["status"] == "delivered"
    assert summary["total_targets"] == 1
    assert summary["sent_count"] == 1
    assert summary["failed_count"] == 0
    assert summary["retrying_count"] == 0
    assert summary["failure_code"] is None
    assert summary["failure_reason"] is None


def test_summarize_delivery_all_failed_card_limit_30kb() -> None:
    """Real incident 782f546a/b8042763: 35,221-byte card > 30,720-byte gateway limit."""
    summary = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": "b8042763-0000-4000-8000-000000000001",
                "execution_id": "782f546a-0000-4000-8000-000000000001",
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "not_allowed",
                "channel_error_code": "not_allowed",
                "channel_error_message": "Feishu card payload 35221 bytes exceeds 30720 byte card limit",
                "payload_json": {"source": "automation_report"},
            },
            # Failure alert row must be excluded from the report's own aggregate.
            {
                "delivery_id": "b8042763-0000-4000-8000-000000000002",
                "execution_id": "782f546a-0000-4000-8000-000000000001",
                "channel": "discord",
                "target": "discord:user:123456789",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report_failure_alert"},
            },
        ]
    )
    assert summary is not None
    assert summary["status"] == "failed"
    assert summary["total_targets"] == 1
    assert summary["sent_count"] == 0
    assert summary["failed_count"] == 1
    assert summary["failure_code"] == "card_limit_exceeded"
    assert summary["failure_reason"] == "卡片超过 30 KB 上限"
    assert (
        eds.humanize_delivery_error(
            summary["failure_code"], summary["failure_reason"], lang="en"
        )
        == "Card exceeds 30 KB limit"
    )


def test_summarize_delivery_partial_failed() -> None:
    summary = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-partial",
                "channel": "feishu",
                "target": "feishu:user:ou_ok",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-partial",
                "channel": "feishu",
                "target": "feishu:chat:oc_big",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "card_limit_exceeded",
                "payload_json": {"source": "automation_report"},
            },
        ]
    )
    assert summary is not None
    assert summary["status"] == "partial_failed"
    assert summary["total_targets"] == 2
    assert summary["sent_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["retrying_count"] == 0
    assert summary["failure_code"] == "card_limit_exceeded"
    assert summary["failure_reason"] == "卡片超过 30 KB 上限"


def test_summarize_delivery_retrying() -> None:
    # Retryable failure with attempt_count < 3 stays retrying.
    retrying_failed = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-retry-1",
                "channel": "feishu",
                "target": "feishu:user:ou_richard",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "payload_json": {"source": "automation_report"},
            }
        ]
    )
    assert retrying_failed is not None
    assert retrying_failed["status"] == "retrying"
    assert retrying_failed["retrying_count"] == 1
    assert retrying_failed["failure_reason"] is None

    # Pending row (attempt 0 or uncertain) stays retrying even if another target succeeded.
    retrying_pending = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-retry-2",
                "channel": "feishu",
                "target": "feishu:user:ou_ok",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-retry-2",
                "channel": "discord",
                "target": "discord:user:999",
                "status": "pending",
                "attempt_count": 1,
                "error_code": "delivery_uncertain",
                "payload_json": {"source": "automation_completion"},
            },
        ]
    )
    assert retrying_pending is not None
    assert retrying_pending["status"] == "retrying"
    assert retrying_pending["sent_count"] == 1
    assert retrying_pending["retrying_count"] == 1

    # Upstream delivery_result with persisted retrying=True within settle window -> retrying.
    now = _utc(13, 15)
    retrying_upstream = eds.summarize_execution_delivery(
        delivery_result=[
            {
                "method": "slack",
                "address": "slack:T1/C1",
                "name": "#desk",
                "success": False,
                "retrying": True,
                "error": "Delivery couldn't be confirmed. Gateway timeout.",
            }
        ],
        completed_at=now - timedelta(seconds=30),
        now=now,
    )
    assert retrying_upstream is not None
    assert retrying_upstream["status"] == "retrying"


def test_summarize_delivery_none_configured() -> None:
    assert eds.summarize_execution_delivery() is None
    assert eds.summarize_execution_delivery(delivery_result=[]) is None
    assert (
        eds.summarize_execution_delivery(
            notify_rows=[
                {
                    "delivery_id": str(uuid4()),
                    "execution_id": "exec-skip",
                    "channel": "feishu",
                    "target": "feishu:user:ou_richard",
                    "status": "skipped",
                    "attempt_count": 0,
                    "error_code": "toggle_off",
                    "payload_json": {"source": "automation_completion"},
                }
            ]
        )
        is None
    )


# ---------------------------------------------------------------------------
# 2. Batch DB enrichment with create_autospec psycopg doubles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_executions_batch_covers_five_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_delivered = str(uuid4())
    exec_all_failed = "782f546a-1111-4222-8333-444455556666"
    exec_partial = str(uuid4())
    exec_retrying = str(uuid4())
    exec_none = str(uuid4())
    deliv_failed_id = "b8042763-1111-4222-8333-444455556666"

    sql_log = _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_delivered,
                "channel": "feishu",
                "target": "feishu:user:ou_ok",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": deliv_failed_id,
                "execution_id": exec_all_failed,
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "not_allowed",
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_partial,
                "channel": "feishu",
                "target": "feishu:user:ou_ok",
                "status": "sent",
                "attempt_count": 1,
                "error_code": None,
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_partial,
                "channel": "discord",
                "target": "discord:user:99999",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "target_failed",
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_retrying,
                "channel": "feishu",
                "target": "feishu:user:ou_retry",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "payload_json": {"source": "automation_report"},
            },
        ],
        channel_rows=[
            {
                "delivery_key": f"data_task:{exec_all_failed}:item:0",
                "run_id": exec_all_failed,
                "platform": "feishu",
                "target_address": "feishu:chat:oc_market_pm",
                "status": "failed",
                "error_code": "not_allowed",
                "error_message": "Card size 35221 bytes exceeds 30720 byte card limit",
            }
        ],
    )

    executions = [
        {"automation_execution_id": exec_delivered, "status": "completed", "delivery_result": None},
        {"automation_execution_id": exec_all_failed, "status": "completed", "delivery_result": None},
        {"automation_execution_id": exec_partial, "status": "completed", "delivery_result": None},
        {"automation_execution_id": exec_retrying, "status": "completed", "delivery_result": None},
        {"automation_execution_id": exec_none, "status": "completed", "delivery_result": None},
    ]

    await eds.enrich_executions_with_delivery_summary(executions)

    assert len(sql_log) == 2
    assert "FROM dalton_notify_deliveries" in sql_log[0]
    assert "FROM dalton_channel_deliveries" in sql_log[1]

    # Execution status itself is never mutated.
    assert all(row["status"] == "completed" for row in executions)

    assert executions[0]["delivery_summary"]["status"] == "delivered"
    assert executions[1]["delivery_summary"]["status"] == "failed"
    assert executions[1]["delivery_summary"]["failure_code"] == "card_limit_exceeded"
    assert executions[1]["delivery_summary"]["failure_reason"] == "卡片超过 30 KB 上限"
    assert executions[2]["delivery_summary"]["status"] == "partial_failed"
    assert executions[2]["delivery_summary"]["sent_count"] == 1
    assert executions[2]["delivery_summary"]["failed_count"] == 1
    assert executions[3]["delivery_summary"]["status"] == "retrying"
    assert executions[4]["delivery_summary"] is None


# ---------------------------------------------------------------------------
# 3. Chief of Staff <activity>, system_jobs, ThreadStatus, file.py, REST
# ---------------------------------------------------------------------------


def test_cos_activity_renders_all_five_delivery_states() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    runs = [
        {
            "automation_name": "Delivered Task",
            "status": "completed",
            "excerpt": "All clear",
            "completed_at": _utc(5, 0),
            "delivery_summary": {"status": "delivered", "total_targets": 1, "sent_count": 1, "failed_count": 0, "retrying_count": 0},
        },
        {
            "automation_name": "市场晚报",
            "status": "completed",
            "excerpt": "晚报已生成",
            "completed_at": _utc(5, 13),
            "delivery_summary": {
                "status": "failed",
                "total_targets": 1,
                "sent_count": 0,
                "failed_count": 1,
                "retrying_count": 0,
                "failure_code": "card_limit_exceeded",
                "failure_reason": "卡片超过 30 KB 上限",
            },
        },
        {
            "automation_name": "Partial Task",
            "status": "completed",
            "excerpt": "Summary ready",
            "completed_at": _utc(5, 20),
            "delivery_summary": {
                "status": "partial_failed",
                "total_targets": 2,
                "sent_count": 1,
                "failed_count": 1,
                "retrying_count": 0,
                "failure_code": "target_failed",
                "failure_reason": "目标通道投递失败",
            },
        },
        {
            "automation_name": "Retrying Task",
            "status": "completed",
            "excerpt": "Pending push",
            "completed_at": _utc(5, 25),
            "delivery_summary": {
                "status": "retrying",
                "total_targets": 1,
                "sent_count": 0,
                "failed_count": 0,
                "retrying_count": 1,
            },
        },
        {
            "automation_name": "Plain Task",
            "status": "completed",
            "excerpt": "No delivery configured",
            "completed_at": _utc(5, 30),
            "delivery_summary": None,
        },
    ]

    rendered = activity.render_activity(
        workspaces=[],
        workspace_total=0,
        threads=[],
        runs=runs,
        holdings=[],
        home_id="home-1",
        zone=zone,
        today=date(2026, 11, 20),
    )
    lines = rendered.splitlines()
    assert "- 13:00 Delivered Task, finished: All clear" in lines
    assert "- 13:13 市场晚报, finished · delivery failed (Card exceeds 30 KB limit): 晚报已生成" in lines
    assert "- 13:20 Partial Task, finished · partial delivery failed (1/2: Target channel delivery failed): Summary ready" in lines
    assert "- 13:25 Retrying Task, finished · delivering: Pending push" in lines
    assert "- 13:30 Plain Task, finished: No delivery configured" in lines


def test_system_jobs_summarize_last_execution_five_states() -> None:
    base = {"status": "completed", "completed_at": "2026-11-20T13:13:00Z", "excerpt": "市场晚报摘要"}

    _, delivered = system_jobs._summarize_last_execution(
        {**base, "delivery_summary": {"status": "delivered", "total_targets": 1, "sent_count": 1, "failed_count": 0, "retrying_count": 0}}
    )
    assert delivered == "市场晚报摘要"

    _, failed = system_jobs._summarize_last_execution(
        {
            **base,
            "delivery_summary": {
                "status": "failed",
                "total_targets": 1,
                "sent_count": 0,
                "failed_count": 1,
                "retrying_count": 0,
                "failure_code": "card_limit_exceeded",
                "failure_reason": "卡片超过 30 KB 上限",
            },
        }
    )
    assert failed == "已完成 · 投递失败（卡片超过 30 KB 上限）"

    _, partial = system_jobs._summarize_last_execution(
        {
            **base,
            "delivery_summary": {
                "status": "partial_failed",
                "total_targets": 2,
                "sent_count": 1,
                "failed_count": 1,
                "retrying_count": 0,
                "failure_code": "target_failed",
                "failure_reason": "目标通道投递失败",
            },
        }
    )
    assert partial == "已完成 · 部分投递失败（1/2：目标通道投递失败）"

    _, retrying = system_jobs._summarize_last_execution(
        {**base, "delivery_summary": {"status": "retrying", "total_targets": 1, "sent_count": 0, "failed_count": 0, "retrying_count": 1}}
    )
    assert retrying == "已完成 · 投递中"

    _, no_delivery = system_jobs._summarize_last_execution({**base, "delivery_summary": None})
    assert no_delivery == "市场晚报摘要"


@pytest.mark.asyncio
async def test_thread_status_and_file_state_surface_delivery_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _utc(13, 30)
    exec_id = "782f546a-1111-4222-8333-444455556666"
    auto_id = str(uuid4())
    thread_id = str(uuid4())

    _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_id,
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "card_limit_exceeded",
                "payload_json": {"source": "automation_report"},
            }
        ],
    )

    row = {
        "automation_id": auto_id,
        "user_id": "user-richard",
        "name": "市场晚报",
        "file_name": "market-pm.json",
        "status": "active",
        "trigger_type": "cron",
        "cron_expression": "0 13 * * 1-5",
        "timezone": "UTC",
        "instruction": "Generate market close digest",
        "workspace_id": None,
        "thread_strategy": "continue",
        "conversation_thread_id": thread_id,
        "max_failures": 3,
        "delivery_config": {"methods": []},
        "next_run_at": _utc(18, 0),
        "last_execution": {
            "automation_execution_id": exec_id,
            "automation_id": auto_id,
            "status": "completed",
            "completed_at": _utc(13, 13),
            "started_at": _utc(13, 10),
            "scheduled_at": _utc(13, 10),
            "delivery_result": None,
        },
    }

    list_all_mock = create_autospec(auto_db.list_all_automations, return_value=[row])
    monkeypatch.setattr(auto_db, "list_all_automations", list_all_mock)

    # ThreadStatus _Automations.items() enriches last_execution
    reader = ts_tool._Reader(thread_id=thread_id, user_id="user-richard", user_tz="UTC", registry=None)
    reader.now = now
    items = await ts_tool._Automations(reader).items()
    assert len(items) == 1
    assert "last run finished · delivery failed (Card exceeds 30 KB limit)" in items[0].text
    detail = ts_listing.automation_detail(row, now, "/automations")
    assert "Last run: finished · delivery failed (Card exceeds 30 KB limit)" in detail

    # LiveFS file rendering keeps status="completed" and exposes delivery_status + English reason
    rendered = await auto_file.rendered_files("user-richard")
    assert "market-pm.json" in rendered
    assert '"status": "completed"' in rendered["market-pm.json"][0]
    assert '"delivery_status": "failed"' in rendered["market-pm.json"][0]
    assert '"delivery_failure_reason": "Card exceeds 30 KB limit"' in rendered["market-pm.json"][0]


@pytest.mark.asyncio
async def test_runs_page_enriches_delivery_summary_without_changing_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_id = "782f546a-1111-4222-8333-444455556666"
    auto_id = str(uuid4())

    _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_id,
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "card_limit_exceeded",
                "payload_json": {"source": "automation_report"},
            }
        ],
    )

    fake_rows = [
        {
            "automation_execution_id": exec_id,
            "automation_id": auto_id,
            "automation_name": "市场晚报",
            "trigger_type": "cron",
            "workspace_id": None,
            "status": "completed",
            "conversation_thread_id": None,
            "scheduled_at": _utc(13, 10),
            "started_at": _utc(13, 10),
            "completed_at": _utc(13, 13),
            "error_message": None,
            "skip_reason": None,
            "failure_reason": None,
            "wait_reason": None,
            "server_id": "srv-1",
            "delivery_result": None,
            "created_at": _utc(13, 10),
            "dismissed_at": None,
            "excerpt": "市场晚报已生成",
        }
    ]
    list_exec_mock = create_autospec(exec_db.list_executions, return_value=(fake_rows, False))
    monkeypatch.setattr(exec_db, "list_executions", list_exec_mock)

    page = await automations_router._runs_page("user-richard")
    assert len(page.executions) == 1
    run = page.executions[0]
    assert run.status == "completed"
    assert run.delivery_summary is not None
    assert run.delivery_summary.status == "failed"
    assert run.delivery_summary.failure_code == "card_limit_exceeded"
    assert run.delivery_summary.failure_reason == "卡片超过 30 KB 上限"


# ---------------------------------------------------------------------------
# 4. Round-2 review regression tests (high & medium findings)
# ---------------------------------------------------------------------------


def test_expired_notify_row_past_24h_retry_window_is_terminal_failed() -> None:
    """Codex High #1 / Carbon High #2: rows outside 24h retry scan cutoff are terminal failed."""
    now = _utc(15, 0)
    expired_at = now - timedelta(hours=25)
    recent_at = now - timedelta(hours=2)

    expired_failed = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-expired-failed",
                "channel": "feishu",
                "target": "feishu:user:ou_richard",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "created_at": expired_at,
                "updated_at": expired_at,
                "payload_json": {"source": "automation_completion"},
            }
        ],
        now=now,
    )
    assert expired_failed is not None
    assert expired_failed["status"] == "failed"
    assert expired_failed["retrying_count"] == 0
    assert expired_failed["failed_count"] == 1
    assert expired_failed["failure_code"] == "target_failed"

    expired_uncertain = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-expired-pending",
                "channel": "feishu",
                "target": "feishu:user:ou_richard",
                "status": "pending",
                "attempt_count": 1,
                "error_code": "delivery_uncertain",
                "created_at": expired_at,
                "updated_at": expired_at,
                "payload_json": {"source": "automation_report"},
            }
        ],
        now=now,
    )
    assert expired_uncertain is not None
    assert expired_uncertain["status"] == "failed"
    assert expired_uncertain["failure_code"] == "delivery_uncertain"
    assert expired_uncertain["failure_reason"] == "投递状态未确认"
    assert (
        eds.humanize_delivery_error(
            expired_uncertain["failure_code"],
            expired_uncertain["failure_reason"],
            lang="en",
        )
        == "Delivery could not be confirmed"
    )

    still_recent = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-recent",
                "channel": "feishu",
                "target": "feishu:user:ou_richard",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "created_at": recent_at,
                "updated_at": recent_at,
                "payload_json": {"source": "automation_completion"},
            }
        ],
        now=now,
    )
    assert still_recent is not None
    assert still_recent["status"] == "retrying"


@pytest.mark.asyncio
async def test_same_channel_multi_target_does_not_leak_channel_error_across_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex High #2 / Carbon Med #3: errors never bleed between two Feishu targets."""
    now = _utc(15, 0)
    exec_id = str(uuid4())
    _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_id,
                "channel": "feishu",
                "target": "feishu:user:ou_retry_user",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "connecterror",
                "created_at": now - timedelta(minutes=2),
                "updated_at": now - timedelta(minutes=2),
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_id,
                "channel": "feishu",
                "target": "feishu:chat:oc_large_group",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "not_allowed",
                "created_at": now - timedelta(minutes=2),
                "updated_at": now - timedelta(minutes=2),
                "payload_json": {"source": "automation_report"},
            },
        ],
        channel_rows=[
            {
                "delivery_key": f"data_task:{exec_id}:feishu:oc_large_group:item:0",
                "run_id": exec_id,
                "platform": "feishu",
                "target_address": "feishu:oc_large_group",
                "status": "failed",
                "error_code": "not_allowed",
                "error_message": "Feishu card payload 35221 bytes exceeds 30720 byte card limit",
            }
        ],
    )

    executions = [
        {"automation_execution_id": exec_id, "status": "completed", "delivery_result": None}
    ]
    await eds.enrich_executions_with_delivery_summary(executions, now=now)
    summary = executions[0]["delivery_summary"]
    assert summary is not None
    assert summary["status"] == "retrying"
    assert summary["retrying_count"] == 1
    assert summary["failed_count"] == 1

    by_target = {t["target"]: t for t in summary["targets"]}
    assert by_target["feishu:user:ou_retry_user"]["status"] == "retrying"
    assert by_target["feishu:user:ou_retry_user"]["error_code"] is None
    assert by_target["feishu:chat:oc_large_group"]["status"] == "failed"
    assert by_target["feishu:chat:oc_large_group"]["error_code"] == "card_limit_exceeded"
    assert by_target["feishu:chat:oc_large_group"]["error_message"] == "卡片超过 30 KB 上限"


@pytest.mark.asyncio
async def test_gateway_401_404_finish_persists_retry_false_and_is_immediately_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #3: 401/404 finish returns retry=False, persisted so it never shows retrying."""
    from src.server.services import automation_delivery, automation_settlement
    from src.tools.messaging import tools as messaging

    recorded_batches: list[list[dict[str, Any]]] = []

    async def _capture_record(execution_id: str, delivery_result: list[dict[str, Any]]) -> None:
        _ = execution_id
        recorded_batches.append(delivery_result)

    monkeypatch.setattr(
        automation_settlement,
        "_record_delivery",
        create_autospec(automation_settlement._record_delivery, side_effect=_capture_record),
    )

    automation = {
        "automation_id": str(uuid4()),
        "user_id": "user-1",
        "delivery_config": {"methods": ["slack:T1/C1"]},
    }
    delivery = automation_delivery.Delivery(
        id="exec-404",
        targets=[
            automation_delivery.Target(
                entry="slack:T1/C1", address="slack:T1/C1", name="#desk", ok=True
            )
        ],
    )

    orig_gateway_request = messaging.gateway_request
    for reply in (
        messaging.GatewayError("Refused credentials.", delivered_unknown=False, status=401),
        messaging.GatewayAnswer(404, {"detail": "Not Found"}, ""),
    ):
        recorded_batches.clear()
        if isinstance(reply, Exception):
            ask_mock = create_autospec(orig_gateway_request, side_effect=reply)
        else:
            ask_mock = create_autospec(orig_gateway_request, return_value=reply)
        monkeypatch.setattr(messaging, "gateway_request", ask_mock)

        await automation_settlement._finish_here(
            automation, "exec-404", "thread-1", delivery, "completed", "Done."
        )
        assert len(recorded_batches) == 1
        persisted = recorded_batches[0]
        assert persisted[0]["success"] is False
        assert persisted[0]["retrying"] is False
        assert persisted[0]["error"].startswith("Delivery couldn't be confirmed.")

        now = _utc(15, 0)
        summary = eds.summarize_execution_delivery(
            delivery_result=persisted,
            completed_at=now - timedelta(seconds=10),
            now=now,
        )
        assert summary is not None
        assert summary["status"] == "failed"
        assert summary["retrying_count"] == 0
        assert summary["failed_count"] == 1


@pytest.mark.asyncio
async def test_script_notify_status_fallback_without_target_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #6: script history reads notify_status when no target rows exist, preserving skipped."""
    _mock_db_connection(monkeypatch, notify_rows=[])

    runs = [
        {"run_id": str(uuid4()), "status": "succeeded", "notify_status": "pending_dispatch"},
        {"run_id": str(uuid4()), "status": "succeeded", "notify_status": "failed"},
        {"run_id": str(uuid4()), "status": "succeeded", "notify_status": "skipped"},
        {"run_id": str(uuid4()), "status": "succeeded", "notify_status": "preview_only"},
    ]
    await eds.enrich_executions_with_delivery_summary(runs)

    assert runs[0]["delivery_summary"] is not None
    assert runs[0]["delivery_summary"]["status"] == "retrying"
    assert eds.format_execution_delivery_note(runs[0], lang="zh") == "已完成 · 投递中"

    assert runs[1]["delivery_summary"] is not None
    assert runs[1]["delivery_summary"]["status"] == "failed"
    assert runs[1]["delivery_summary"]["failure_code"] == "delivery_failed"
    assert eds.format_execution_delivery_note(runs[1], lang="zh") == "已完成 · 投递失败（目标通道投递失败）"

    assert runs[2]["delivery_summary"] is None
    assert eds.format_execution_delivery_note(runs[2], lang="zh") is None
    assert runs[3]["delivery_summary"] is None


@pytest.mark.asyncio
async def test_automation_file_fetch_enriches_delivery_summary_in_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #7 / Carbon Med #4 & #6: AutomationFile.fetch enriches single reads & strips synced suffix."""
    exec_id = "782f546a-1111-4222-8333-444455556666"
    auto_id = str(uuid4())
    _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_id,
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "not_allowed",
                "payload_json": {"source": "automation_report"},
            }
        ],
        channel_rows=[
            {
                "delivery_key": f"data_task:{exec_id}:feishu:oc_market_pm:item:0",
                "run_id": exec_id,
                "platform": "feishu",
                "target_address": "feishu:oc_market_pm",
                "status": "failed",
                "error_code": "not_allowed",
                "error_message": "Feishu card payload 35221 bytes exceeds 30720 byte card limit",
            }
        ],
    )

    row = {
        "automation_id": auto_id,
        "user_id": "user-richard",
        "name": "市场晚报",
        "file_name": "market-pm.json",
        "status": "active",
        "trigger_type": "cron",
        "cron_expression": "0 13 * * 1-5",
        "timezone": "UTC",
        "instruction": "Generate market close digest",
        "workspace_id": None,
        "thread_strategy": "continue",
        "conversation_thread_id": None,
        "max_failures": 3,
        "delivery_config": {"methods": []},
        "next_run_at": _utc(18, 0),
        "last_execution": {
            "automation_execution_id": exec_id,
            "automation_id": auto_id,
            "status": "completed",
            "completed_at": _utc(13, 13),
            "started_at": _utc(13, 10),
            "scheduled_at": _utc(13, 10),
            "excerpt": "市场晚报已生成\n\n投递状态：已生成但未送达（not_allowed）",
            "delivery_result": [
                {
                    "method": "feishu",
                    "address": "feishu:chat:oc_market_pm",
                    "success": False,
                    "error": "已生成但未送达（not_allowed）",
                }
            ],
        },
    }

    monkeypatch.setattr(
        auto_db,
        "get_automation_file",
        create_autospec(auto_db.get_automation_file, return_value=row),
    )

    f = auto_file.AutomationFile("market-pm.json")
    fetched = await f.fetch("user-richard")
    assert fetched is not None
    rendered = f.render(fetched)
    assert rendered is not None
    content, _version = rendered
    assert '"status": "completed"' in content
    assert '"delivery_status": "failed"' in content
    assert '"delivery_failure_reason": "Card exceeds 30 KB limit"' in content
    assert '"excerpt": "市场晚报已生成"' in content
    assert "已生成但未送达" not in content


@pytest.mark.asyncio
async def test_system_jobs_data_task_and_cross_owner_sector_batch_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med #7 / Carbon Med #5: data tasks & cross-owner sector units get batch delivery enrichment."""
    from src.dalton import sharing

    # 1. Data task run summary surfaces delivery failure / pending_dispatch instead of plain "成功"
    failed_data_run = {
        "run_id": str(uuid4()),
        "status": "succeeded",
        "completed_at": _utc(13, 13),
        "notify_status": "failed",
    }
    _mock_db_connection(monkeypatch, notify_rows=[])
    await eds.enrich_executions_with_delivery_summary([failed_data_run])
    _, label_failed, raw_st = system_jobs._summarize_data_task_run(failed_data_run)
    assert raw_st == "succeeded"
    assert label_failed == "已完成 · 投递失败（目标通道投递失败）"

    pending_data_run = {
        "run_id": str(uuid4()),
        "status": "succeeded",
        "completed_at": _utc(13, 14),
        "notify_status": "pending_dispatch",
    }
    await eds.enrich_executions_with_delivery_summary([pending_data_run])
    _, label_pending, _ = system_jobs._summarize_data_task_run(pending_data_run)
    assert label_pending == "已完成 · 投递中"

    # 2. list_system_jobs batch-enriches cross-owner sector units (single batch call for foreign owners)
    exec_s1 = str(uuid4())
    exec_s2 = str(uuid4())
    ws_s1 = str(uuid4())
    ws_s2 = str(uuid4())

    enrich_calls: list[int] = []
    real_enrich = eds.enrich_automations_with_delivery_summary

    async def _counting_enrich(automations: Any, **kwargs: Any) -> None:
        enrich_calls.append(len(automations))
        await real_enrich(automations, **kwargs)

    _mock_db_connection(
        monkeypatch,
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_s1,
                "channel": "feishu",
                "target": "feishu:chat:oc_sector_1",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "card_limit_exceeded",
                "payload_json": {"source": "automation_report"},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": exec_s2,
                "channel": "feishu",
                "target": "feishu:chat:oc_sector_2",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "created_at": datetime.now(UTC) - timedelta(minutes=5),
                "payload_json": {"source": "automation_report"},
            },
        ],
    )
    monkeypatch.setattr(
        eds,
        "enrich_automations_with_delivery_summary",
        create_autospec(eds.enrich_automations_with_delivery_summary, side_effect=_counting_enrich),
    )

    foreign_rows_by_owner = {
        "analyst-a": [
            {
                "automation_id": str(uuid4()),
                "name": "dalton-sector-semi-watch",
                "workspace_id": ws_s1,
                "status": "active",
                "last_execution": {
                    "automation_execution_id": exec_s1,
                    "status": "completed",
                    "completed_at": _utc(13, 13),
                    "excerpt": "半导体周报已生成",
                    "delivery_result": None,
                },
            }
        ],
        "analyst-b": [
            {
                "automation_id": str(uuid4()),
                "name": "dalton-sector-ai-watch",
                "workspace_id": ws_s2,
                "status": "active",
                "last_execution": {
                    "automation_execution_id": exec_s2,
                    "status": "completed",
                    "completed_at": _utc(13, 14),
                    "excerpt": "AI 行业周报已生成",
                    "delivery_result": None,
                },
            }
        ],
    }

    async def _list_all(user_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(foreign_rows_by_owner.get(user_id, []))

    monkeypatch.setattr(
        system_jobs,
        "list_all_user_automations",
        create_autospec(system_jobs.list_all_user_automations, side_effect=_list_all),
    )
    monkeypatch.setattr(
        system_jobs,
        "is_pm_user",
        create_autospec(system_jobs.is_pm_user, return_value=True),
    )
    monkeypatch.setattr(
        system_jobs,
        "get_user_tier",
        create_autospec(system_jobs.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        system_jobs,
        "_discover_company_workspaces",
        create_autospec(system_jobs._discover_company_workspaces, return_value=[]),
    )
    monkeypatch.setattr(
        system_jobs,
        "_discover_sector_units",
        create_autospec(
            system_jobs._discover_sector_units,
            return_value=[
                {"slug": "semi", "name": "半导体", "workspace_id": ws_s1, "lead_analyst_user_id": "analyst-a"},
                {"slug": "ai", "name": "AI", "workspace_id": ws_s2, "lead_analyst_user_id": "analyst-b"},
            ],
        ),
    )
    monkeypatch.setattr(
        sharing,
        "require_workspace_access",
        create_autospec(sharing.require_workspace_access, return_value={"user_id": "analyst-a"}),
    )
    monkeypatch.setattr(
        system_jobs,
        "get_tracking_status",
        create_autospec(
            system_jobs.get_tracking_status,
            return_value={"subject_kind": "sector", "status": "enabled", "tasks": []},
        ),
    )
    monkeypatch.setattr(
        system_jobs,
        "_fetch_ops_jobs",
        create_autospec(system_jobs._fetch_ops_jobs, return_value=[]),
    )
    monkeypatch.setattr(
        system_jobs,
        "_fetch_data_system_jobs",
        create_autospec(system_jobs._fetch_data_system_jobs, return_value=[]),
    )

    result = await system_jobs.list_system_jobs("pm-user")
    sector_by_key = {item["key"]: item for item in result["groups"]["sector"]}
    assert sector_by_key["sector:semi"]["last_result"] == "已完成 · 投递失败（卡片超过 30 KB 上限）"
    assert sector_by_key["sector:ai"]["last_result"] == "已完成 · 投递中"
    # 1 call for caller's own rows + 1 single batch call for BOTH foreign owners (no N+1)
    assert len(enrich_calls) == 2
    assert enrich_calls[1] == 2


def test_activity_strips_synced_undelivered_excerpt_suffix() -> None:
    """Carbon Med #6: <activity> strips fallback synced suffix so model doesn't see contradictory not_allowed."""
    zone = ZoneInfo("Asia/Shanghai")
    rendered = activity.render_activity(
        workspaces=[],
        workspace_total=0,
        threads=[],
        runs=[
            {
                "automation_name": "市场晚报",
                "status": "completed",
                "excerpt": "晚报已生成\n\n投递状态：已生成但未送达（not_allowed）",
                "completed_at": _utc(5, 13),
                "delivery_summary": {
                    "status": "failed",
                    "total_targets": 1,
                    "sent_count": 0,
                    "failed_count": 1,
                    "retrying_count": 0,
                    "failure_code": "card_limit_exceeded",
                    "failure_reason": "卡片超过 30 KB 上限",
                },
            }
        ],
        holdings=[],
        home_id="home-1",
        zone=zone,
        today=date(2026, 11, 20),
    )
    assert "- 13:13 市场晚报, finished · delivery failed (Card exceeds 30 KB limit): 晚报已生成" in rendered
    assert "not_allowed" not in rendered
    assert "已生成但未送达" not in rendered


# ---------------------------------------------------------------------------
# 5. Round-3 review regression tests (high & medium findings)
# ---------------------------------------------------------------------------


def test_reconcile_only_uncertain_pending_shows_unconfirmed_and_updates_on_receipt() -> None:
    """R3 High #1: gateway/report uncertain pending rows (reconcile-only) never show retrying."""
    now = _utc(15, 0)
    recent_at = now - timedelta(minutes=2)

    # Trigger: automation_report 3rd attempt timed out -> pending + delivery_uncertain.
    # Sender stopped resending and only checks receipts.
    uncertain_third = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-report-timeout-3",
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "pending",
                "attempt_count": 3,
                "error_code": "delivery_uncertain",
                "created_at": recent_at,
                "updated_at": recent_at,
                "payload_json": {"source": "automation_report"},
            }
        ],
        now=now,
    )
    assert uncertain_third is not None
    assert uncertain_third["status"] == "failed"
    assert uncertain_third["retrying_count"] == 0
    assert uncertain_third["failed_count"] == 1
    assert uncertain_third["failure_code"] == "delivery_uncertain"
    assert uncertain_third["failure_reason"] == "投递状态未确认"
    assert (
        eds.format_execution_delivery_note(
            {"status": "completed", "delivery_summary": uncertain_third}, lang="zh"
        )
        == "已完成 · 投递失败（投递状态未确认）"
    )
    assert (
        eds.format_execution_delivery_note(
            {"status": "completed", "delivery_summary": uncertain_third}, lang="en"
        )
        == "finished · delivery failed (Delivery could not be confirmed)"
    )

    # Even attempt 1 on gateway/report/data_task with delivery_uncertain is reconcile-only (never resent).
    for row in (
        {
            "delivery_id": str(uuid4()),
            "execution_id": "exec-report-timeout-1",
            "channel": "feishu",
            "target": "feishu:chat:oc_market_pm",
            "status": "pending",
            "attempt_count": 1,
            "error_code": "delivery_uncertain",
            "created_at": recent_at,
            "updated_at": recent_at,
            "payload_json": {"source": "automation_report"},
        },
        {
            "delivery_id": str(uuid4()),
            "execution_id": "exec-gw-timeout-1",
            "channel": "feishu",
            "target": "feishu:user:ou_richard",
            "status": "pending",
            "attempt_count": 1,
            "error_code": "in_flight",
            "created_at": recent_at,
            "updated_at": recent_at,
            "payload_json": {"source": "automation_completion", "transport": "gateway"},
        },
        {
            "delivery_id": str(uuid4()),
            "execution_id": "exec-script-timeout-1",
            "automation_id": "data:daily-brief",
            "channel": "feishu",
            "target": "feishu:chat:oc_ops",
            "status": "pending",
            "attempt_count": 1,
            "error_code": "delivery_uncertain",
            "created_at": recent_at,
            "updated_at": recent_at,
            "payload_json": {"route_key": "data:daily-brief"},
        },
    ):
        summary = eds.summarize_execution_delivery(notify_rows=[row], now=now)
        assert summary is not None
        assert summary["status"] == "failed"
        assert summary["retrying_count"] == 0
        assert summary["failure_reason"] == "投递状态未确认"

    # Queued report (attempt 0, no error) is still eligible to send -> retrying.
    queued_report = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-report-queued",
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "pending",
                "attempt_count": 0,
                "error_code": None,
                "created_at": recent_at,
                "updated_at": recent_at,
                "payload_json": {"source": "automation_report"},
            }
        ],
        now=now,
    )
    assert queued_report is not None
    assert queued_report["status"] == "retrying"

    # Once reconciliation receives receipt and updates row to sent -> delivered.
    after_receipt = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "exec-report-timeout-3",
                "channel": "feishu",
                "target": "feishu:chat:oc_market_pm",
                "status": "sent",
                "attempt_count": 3,
                "error_code": None,
                "created_at": recent_at,
                "updated_at": now,
                "payload_json": {"source": "automation_report"},
            }
        ],
        now=now,
    )
    assert after_receipt is not None
    assert after_receipt["status"] == "delivered"


@pytest.mark.asyncio
async def test_settlement_clears_retrying_flag_when_stopping_retries_without_replacing_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 High #2: stopping retries on 503 exhaustion or 401/404 after answered clears retrying without overwriting record."""
    import asyncio as _asyncio

    from src.server.services import (
        automation_delivery,
        automation_settlement,
        hook_outbox,
    )

    outbox_db = automation_settlement.outbox_db

    # In-memory simulation of automation_executions.delivery_result and clear_delivery_retrying
    stored_records: dict[str, list[dict[str, Any]]] = {}
    record_calls: list[tuple[str, list[dict[str, Any]]]] = []
    clear_calls: list[str] = []

    async def _fake_record(execution_id: str, delivery_result: list[dict[str, Any]], *, conn: Any = None) -> None:
        _ = conn
        copied = [dict(item) for item in delivery_result]
        stored_records[execution_id] = copied
        record_calls.append((execution_id, copied))

    async def _fake_clear(execution_id: str, *, conn: Any = None) -> None:
        _ = conn
        clear_calls.append(execution_id)
        if execution_id in stored_records:
            stored_records[execution_id] = [
                {**item, "retrying": False} if item.get("retrying") is True else item
                for item in stored_records[execution_id]
            ]

    async def _fake_merge(job_id: str, patch: dict[str, Any], *, remove: Any = None, conn: Any = None) -> bool:
        _ = job_id, patch, remove, conn
        return True

    @contextlib.asynccontextmanager
    async def _fake_conn(_conn: Any = None):
        conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @contextlib.asynccontextmanager
        async def _tx():
            yield

        conn.transaction.side_effect = _tx
        yield conn

    monkeypatch.setattr(
        exec_db,
        "record_delivery",
        create_autospec(exec_db.record_delivery, side_effect=_fake_record),
    )
    orig_clear_delivery_retrying = exec_db.clear_delivery_retrying
    monkeypatch.setattr(
        exec_db,
        "clear_delivery_retrying",
        create_autospec(orig_clear_delivery_retrying, side_effect=_fake_clear),
    )
    monkeypatch.setattr(
        outbox_db,
        "merge_job_payload",
        create_autospec(outbox_db.merge_job_payload, side_effect=_fake_merge),
    )
    monkeypatch.setattr(
        automation_settlement,
        "get_db_connection",
        create_autospec(automation_settlement.get_db_connection, side_effect=_fake_conn),
    )
    monkeypatch.setattr(
        auto_db,
        "get_automation",
        create_autospec(
            auto_db.get_automation,
            return_value={
                "automation_id": "auto-1",
                "user_id": "user-1",
                "delivery_config": {"methods": ["slack:T1/C1"]},
            },
        ),
    )

    delivery = automation_delivery.Delivery(
        id="exec-r3",
        targets=[
            automation_delivery.Target(
                entry="slack:T1/C1", address="slack:T1/C1", name="#desk", ok=True
            )
        ],
    )
    stamped_delivery = automation_delivery.stamp(delivery)

    finish_503 = automation_delivery.Finish(
        result=[
            {
                "method": "slack:T1/C1",
                "address": "slack:T1/C1",
                "name": "#desk",
                "success": False,
                "error": "Delivery couldn't be confirmed. The messaging service returned 503.",
            }
        ],
        retry=True,
        answered=False,
    )
    finish_posting = automation_delivery.Finish(
        result=[
            {
                "method": "slack:T1/C1",
                "address": "slack:T1/C1",
                "name": "#desk",
                "success": False,
                "error": "Delivery couldn't be confirmed. Still posting to #desk.",
            }
        ],
        retry=True,
        answered=True,
    )
    finish_404 = automation_delivery.Finish(
        result=[
            {
                "method": "slack:T1/C1",
                "address": "slack:T1/C1",
                "name": "#desk",
                "success": False,
                "error": "Delivery couldn't be confirmed. The messaging service has no record of this run.",
            }
        ],
        retry=False,
        answered=False,
    )

    # Case 1a: Outbox _finish_held_run final attempt still returns 503 -> keeps first record, clears retrying
    orig_finish_run = automation_delivery.finish_run
    monkeypatch.setattr(
        automation_delivery,
        "finish_run",
        create_autospec(orig_finish_run, side_effect=[finish_503, finish_503]),
    )
    job_1 = {
        "hook_outbox_id": "job-1",
        "conversation_thread_id": "thread-1",
        "attempts": 1,
        "payload": {
            "execution_id": "exec-503-outbox",
            "automation_id": "auto-1",
            "user_id": "user-1",
            "status": "completed",
            "final_text": "Done",
            "delivery": stamped_delivery,
            "recorded": False,
            "answered": False,
        },
    }
    with pytest.raises(automation_settlement._FinishPending):
        await automation_settlement._finish_held_run(job_1)
    assert stored_records["exec-503-outbox"][0]["retrying"] is True

    job_last = {
        **job_1,
        "attempts": hook_outbox.MAX_ATTEMPTS,
        "payload": {**job_1["payload"], "recorded": True, "answered": False},
    }
    await automation_settlement._finish_held_run(job_last)
    assert len([c for c in record_calls if c[0] == "exec-503-outbox"]) == 1
    assert "exec-503-outbox" in clear_calls
    assert stored_records["exec-503-outbox"][0]["retrying"] is False
    now = _utc(15, 0)
    summary_503 = eds.summarize_execution_delivery(
        delivery_result=stored_records["exec-503-outbox"],
        completed_at=now - timedelta(seconds=10),
        now=now,
    )
    assert summary_503 is not None
    assert summary_503["status"] == "failed"

    # Case 1b: Outbox _finish_held_run received "still posting" (answered=True), then 404 (retry=False)
    monkeypatch.setattr(
        automation_delivery,
        "finish_run",
        create_autospec(orig_finish_run, side_effect=[finish_posting, finish_404]),
    )
    job_ans = {
        "hook_outbox_id": "job-2",
        "conversation_thread_id": "thread-1",
        "attempts": 1,
        "payload": {
            "execution_id": "exec-ans-404-outbox",
            "automation_id": "auto-1",
            "user_id": "user-1",
            "status": "completed",
            "final_text": "Done",
            "delivery": stamped_delivery,
            "recorded": False,
            "answered": False,
        },
    }
    with pytest.raises(automation_settlement._FinishPending):
        await automation_settlement._finish_held_run(job_ans)
    assert stored_records["exec-ans-404-outbox"][0]["retrying"] is True

    job_ans_next = {
        **job_ans,
        "attempts": 2,
        "payload": {**job_ans["payload"], "recorded": True, "answered": True},
    }
    await automation_settlement._finish_held_run(job_ans_next)
    # Original "still posting" record is kept (NOT overwritten by 404), and retrying is cleared to False.
    assert "Still posting to #desk." in stored_records["exec-ans-404-outbox"][0]["error"]
    assert stored_records["exec-ans-404-outbox"][0]["retrying"] is False
    summary_ans = eds.summarize_execution_delivery(
        delivery_result=stored_records["exec-ans-404-outbox"],
        completed_at=now - timedelta(seconds=10),
        now=now,
    )
    assert summary_ans is not None
    assert summary_ans["status"] == "failed"

    # Case 2: In-process _finish_again for both 503 exhaustion and 401/404 after answered=True
    monkeypatch.setattr(automation_delivery, "FINISH_RETRY_DELAYS", (0, 0))

    async def _noop_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        _asyncio, "sleep", create_autospec(_asyncio.sleep, side_effect=_noop_sleep)
    )

    auto_stub = {
        "automation_id": "auto-1",
        "user_id": "user-1",
        "delivery_config": {"methods": ["slack:T1/C1"]},
    }
    stored_records["exec-503-inline"] = automation_settlement._stamp_delivery_retry(
        finish_503.result, retrying=True
    )
    ask_503 = create_autospec(
        orig_finish_run, side_effect=[finish_503, finish_503]
    )
    await automation_settlement._finish_again(
        lambda: ask_503(
            auto_stub,
            delivery.id,
            "completed",
            targets=delivery.targets,
            final_text="Done",
            thread_id="thread-1",
        ),
        "exec-503-inline",
        answered=False,
    )
    assert stored_records["exec-503-inline"][0]["retrying"] is False

    stored_records["exec-ans-401-inline"] = automation_settlement._stamp_delivery_retry(
        finish_posting.result, retrying=True
    )
    ask_401 = create_autospec(orig_finish_run, side_effect=[finish_404])
    await automation_settlement._finish_again(
        lambda: ask_401(
            auto_stub,
            delivery.id,
            "completed",
            targets=delivery.targets,
            final_text="Done",
            thread_id="thread-1",
        ),
        "exec-ans-401-inline",
        answered=True,
    )
    assert "Still posting to #desk." in stored_records["exec-ans-401-inline"][0]["error"]
    assert stored_records["exec-ans-401-inline"][0]["retrying"] is False

    # Case 3: Verify exec_db.clear_delivery_retrying executes the atomic JSONB retrying->false update.
    executed: list[tuple[str, Any]] = []
    cur = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _cap_exec(sql: Any, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), params))

    cur.execute.side_effect = _cap_exec
    db_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @contextlib.asynccontextmanager
    async def _cur_cm(*_a: Any, **_kw: Any):
        yield cur

    db_conn.cursor.side_effect = _cur_cm
    await orig_clear_delivery_retrying("exec-sql-check", conn=db_conn)
    assert len(executed) == 1
    assert "UPDATE automation_executions" in executed[0][0]
    assert "jsonb_set(elem, '{retrying}', 'false'::jsonb, true)" in executed[0][0]
    assert executed[0][1] == ("exec-sql-check",)


def test_company_script_pending_dispatch_failed_over_24h_remains_retrying_until_cap() -> None:
    """R3 Med #3: company script still in pending_dispatch retries failed targets even after 24h."""
    now = _utc(15, 0)
    old_at = now - timedelta(hours=30)

    # Trigger: scheduler down for >24h, target failed on attempt 1, script run still pending_dispatch.
    still_retryable = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "script-run-1",
                "automation_id": "data:daily-brief",
                "channel": "feishu",
                "target": "feishu:chat:oc_ops",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "created_at": old_at,
                "updated_at": old_at,
                "payload_json": {"route_key": "data:daily-brief"},
            }
        ],
        notify_status="pending_dispatch",
        now=now,
    )
    assert still_retryable is not None
    assert still_retryable["status"] == "retrying"
    assert still_retryable["retrying_count"] == 1
    assert still_retryable["failed_count"] == 0

    # Once attempts reach cap (3), script scheduler cannot claim -> terminal failed.
    capped = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "script-run-2",
                "automation_id": "data:daily-brief",
                "channel": "feishu",
                "target": "feishu:chat:oc_ops",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "target_failed",
                "created_at": old_at,
                "updated_at": old_at,
                "payload_json": {"route_key": "data:daily-brief"},
            }
        ],
        notify_status="pending_dispatch",
        now=now,
    )
    assert capped is not None
    assert capped["status"] == "failed"

    # Non-retryable card limit error is terminal failed even while pending_dispatch.
    non_retryable = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "script-run-3",
                "automation_id": "data:daily-brief",
                "channel": "feishu",
                "target": "feishu:chat:oc_ops",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "card_limit_exceeded",
                "created_at": old_at,
                "updated_at": old_at,
                "payload_json": {"route_key": "data:daily-brief"},
            }
        ],
        notify_status="pending_dispatch",
        now=now,
    )
    assert non_retryable is not None
    assert non_retryable["status"] == "failed"

    # Once notify_status is no longer pending_dispatch (>24h), retry_failed_notify_deliveries won't retry either.
    settled_script = eds.summarize_execution_delivery(
        notify_rows=[
            {
                "delivery_id": str(uuid4()),
                "execution_id": "script-run-4",
                "automation_id": "data:daily-brief",
                "channel": "feishu",
                "target": "feishu:chat:oc_ops",
                "status": "failed",
                "attempt_count": 1,
                "error_code": "target_failed",
                "created_at": old_at,
                "updated_at": old_at,
                "payload_json": {"route_key": "data:daily-brief"},
            }
        ],
        notify_status="dispatched",
        now=now,
    )
    assert settled_script is not None
    assert settled_script["status"] == "failed"
