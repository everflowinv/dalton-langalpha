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

    monkeypatch.setattr(eds, "get_db_connection", _conn_cm)
    monkeypatch.setattr(eds, "is_dalton_enabled", lambda: True)
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

    # Upstream unconfirmed delivery_result within 3-minute settle window -> retrying.
    now = _utc(13, 15)
    retrying_upstream = eds.summarize_execution_delivery(
        delivery_result=[
            {
                "method": "slack",
                "address": "slack:T1/C1",
                "name": "#desk",
                "success": False,
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

    # LiveFS file rendering keeps status="completed" and exposes delivery_status + reason
    rendered = await auto_file.rendered_files("user-richard")
    assert "market-pm.json" in rendered
    assert '"status": "completed"' in rendered["market-pm.json"][0]
    assert '"delivery_status": "failed"' in rendered["market-pm.json"][0]
    assert '"delivery_failure_reason": "卡片超过 30 KB 上限"' in rendered["market-pm.json"][0]


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
