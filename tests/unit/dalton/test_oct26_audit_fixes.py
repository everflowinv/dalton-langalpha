"""Unit tests for the 10/26 audit robustness fixes (items 1–8)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import langgraph.graph.ui as lg_ui
import pandas as pd
import pytest
import yfinance as yf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.agents.middleware.types import ToolCallRequest
from langgraph._internal._constants import CONFIG_KEY_SEND
from langgraph.constants import CONF
from psycopg import AsyncConnection, AsyncCursor
from yfinance.scrapers.quote import FastInfo

from ptc_agent.agent.middleware.background_subagent.middleware import (
    BackgroundSubagentMiddleware,
)
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.background_subagent.task_actions import (
    _handle_resume,
)
from ptc_agent.agent.middleware.background_subagent.tools import (
    _compose_missing_reply,
    _settled_text,
)
from ptc_agent.agent.middleware.model_resilience import (
    ModelResilienceMiddleware,
    _AttemptRecord,
)
from ptc_agent.agent.subagents.compiler import SubagentCompiler
from ptc_agent.agent.subagents.definition import SubagentDefinition
from ptc_agent.agent.subagents.merge import ResolvedSubagents, plugin_definition
from ptc_agent.config.agent import SubagentsConfig
from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox import mcp_setup
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import ExecResult, SandboxRuntime
from src.config.models import (
    NewsPollConfig,
    NewsPollFeedConfig,
)
from src.dalton.app import dalton_router
from src.dalton.autonomy.tracking import get_tracking_status
from src.dalton.extended_hours import format_quote_change_parts
from src.dalton.workflows.journal import worker_lost_failure
from src.data_client.base import NewsDataSource
from src.data_client.fmp.fmp_client import FmpQuotaExhausted
from src.data_client.news_data_provider import NewsDataProvider
from src.data_client.yfinance import data_source as yf_ds_mod
from src.server.app import workspace_sandbox
from src.server.app.workspace_sandbox import PackageInstallRequest
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.models.chat import TaskReport
from src.server.services import news_refresh_service as news_refresh_mod
from src.server.services.cache.news_cache_service import NewsCacheService
from src.server.services.history.projector import MODEL_FALLBACK_FIELDS
from src.server.services.llm.subagents import demote_clientless
from src.server.services.news_refresh_service import NewsRefreshService
from src.server.services.report_back.subagent import task_report_message
from src.server.utils.api import get_current_user_id


# ---------------------------------------------------------------------------
# Items 1 & 2: Workflow resume contract & resumable-only condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_failure_messages_mandate_run_workflow_resume_only_when_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resumable workflow failures tell the main agent to call RunWorkflow(resume=...) and forbid manual Task subagent re-dispatch; non-resumable failures do not prompt resume."""
    import src.server.database.pool as db_pool_mod

    fake_cur = create_autospec(AsyncCursor, instance=True)
    fake_cur.fetchone.return_value = ("workflow",)
    fake_cur.__aenter__.return_value = fake_cur
    fake_cur.__aexit__.return_value = None

    fake_conn = create_autospec(AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cur

    @asynccontextmanager
    async def _fake_get_db_conn():
        yield fake_conn

    monkeypatch.setattr(
        db_pool_mod,
        "get_db_connection",
        create_autospec(db_pool_mod.get_db_connection, side_effect=_fake_get_db_conn),
    )

    lost_payload = await worker_lost_failure("64f22fbb", "s_rL_A")
    lost_msg = lost_payload["error"]
    assert "resume='s_rL_A'" in lost_msg
    assert "MUST call RunWorkflow" in lost_msg
    assert "MUST NOT manually re-dispatch Task subagents" in lost_msg

    report_obj = TaskReport(
        kind="task",
        task_id="s_rL_A",
        display_id="Task-s_rL_A",
        description="initial_coverage",
        failed=True,
    )
    report_text = task_report_message(
        report_obj,
        "workflow",
        failure_error=lost_msg,
    )
    assert (
        "RunWorkflow(workflow=<same name>, params=<same as before>, resume='s_rL_A')"
        in report_text
    )
    assert "do NOT manually re-dispatch Task subagents" in report_text

    registry = create_autospec(BackgroundTaskRegistry, instance=True)
    registry.resolve_result_text.return_value = None
    registry.resolve_archived_result_text.return_value = None

    resumable_task = BackgroundTask(
        task_id="s_rL_A",
        tool_call_id="tc-1",
        description="initial_coverage",
        prompt="run workflow",
        subagent_type="workflow",
        terminal_status="error",
        error=lost_msg,
        result={"success": False, "error": lost_msg},
    )
    settled = await _settled_text(registry, resumable_task)
    # B5/D6: the resume re-passes the SAME source the run was launched with —
    # a name for a named run, the script verbatim for an inline one (an
    # inline script has no workflow name; `workflow=<name>` answered Unknown
    # workflow). The named form keeps its instruction; the inline form is
    # asserted in test_bridge…/D6 tests with a real thread id.
    assert (
        "RunWorkflow(workflow=<same name>, params=<same as before>, resume='s_rL_A')"
        in settled
    )
    assert "RunWorkflow(script=<the same script verbatim>" in settled
    assert "do NOT manually re-dispatch Task subagents" in settled

    missing = await _compose_missing_reply(
        registry,
        "s_rL_A",
        {
            "task_run_id": "run-1",
            "status": "error",
            "subagent_type": "workflow",
            "failure": {"error": lost_msg},
        },
    )
    assert (
        "RunWorkflow(workflow=<same name>, params=<same as before>, resume='s_rL_A')"
        in missing
    )
    assert "do NOT manually re-dispatch Task subagents" in missing

    # Non-resumable workflow failure must NOT prompt to call RunWorkflow(resume=...)
    non_resumable_err = "WorkflowScriptError: SyntaxError at line 12"
    non_resumable_task = BackgroundTask(
        task_id="x_bad1",
        tool_call_id="tc-2",
        description="initial_coverage",
        prompt="run workflow",
        subagent_type="workflow",
        terminal_status="error",
        error=non_resumable_err,
        result={"success": False, "error": non_resumable_err},
    )
    settled_non_resumable = await _settled_text(registry, non_resumable_task)
    assert "resume='x_bad1'" not in settled_non_resumable

    missing_non_resumable = await _compose_missing_reply(
        registry,
        "x_bad1",
        {
            "task_run_id": "run-2",
            "status": "error",
            "subagent_type": "workflow",
            "failure": {"error": non_resumable_err},
        },
    )
    assert "resume='x_bad1'" not in missing_non_resumable


@pytest.mark.asyncio
async def test_task_resume_on_workflow_redirects_to_run_workflow_resume() -> None:
    wf_task = BackgroundTask(
        task_id="s_rL_A",
        tool_call_id="tc-1",
        description="initial_coverage",
        prompt="run",
        subagent_type="workflow",
        terminal_status="error",
    )
    mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
    mw._resolve_or_error.return_value = wf_task
    req = create_autospec(ToolCallRequest, instance=True)

    async def _noop_handler(_request: ToolCallRequest) -> Any:
        return None

    handler = create_autospec(_noop_handler)

    msg = await _handle_resume(
        mw,
        req,
        handler,
        target_task_id="s_rL_A",
        subagent_type=None,
        description="resume workflow",
        prompt="continue",
        args={},
        parent_thread_id="64f22fbb",
        tool_call_id="tc-2",
        current_run_id="run-2",
    )
    assert (
        "RunWorkflow(workflow=<same name>, params=<same as before>, resume='s_rL_A')"
        in str(msg.content)
    )
    assert "RunWorkflow(script=<the same script verbatim>" in str(msg.content)
    assert "do NOT manually re-dispatch individual Task subagents" in str(msg.content)


# ---------------------------------------------------------------------------
# Item 3: yfinance KeyError('currentTradingPeriod') falls back to history(period="5d", auto_adjust=False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yfinance_keyerror_current_trading_period_falls_back_to_history_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fast_info raises KeyError('currentTradingPeriod') on a weekend/ex-dividend day, _fetch_single_snapshot falls back to history(period='5d', auto_adjust=False) and uses unadjusted Close."""
    broken_fi = create_autospec(FastInfo, instance=True)
    broken_fi.get.side_effect = KeyError("currentTradingPeriod")

    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = broken_fi
    idx = pd.to_datetime(
        ["2026-10-22 00:00:00-04:00", "2026-10-23 00:00:00-04:00"]
    )
    # Simulate an ex-dividend day where Close (unadjusted, 582.5) differs from Adj Close (580.0)
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [570.0, 575.0],
            "High": [580.0, 585.0],
            "Low": [568.0, 572.0],
            "Close": [575.0, 582.5],
            "Adj Close": [572.5, 580.0],
            "Dividends": [0.0, 2.5],
            "Volume": [10_000_000, 12_000_000],
        },
        index=idx,
    )

    # Saturday 2026-10-24 15:00 UTC (11:00 ET): 2026-10-23 session is already closed
    now_sat = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        yf_ds_mod,
        "_now_utc",
        create_autospec(yf_ds_mod._now_utc, return_value=now_sat),
    )
    monkeypatch.setattr(
        yf_ds_mod,
        "yahoo_ticker",
        create_autospec(yf_ds_mod.yahoo_ticker, return_value=ticker),
    )

    snap = yf_ds_mod._fetch_single_snapshot("META")
    assert snap is not None
    assert snap["symbol"] == "META"
    assert snap["price"] == 582.5
    assert snap["previous_close"] == 575.0
    assert snap["change"] == 7.5
    assert snap["market_status"] == "closed"
    assert snap["regular_trading_date"] == "2026-10-23"
    assert snap["as_of"] <= int(now_sat.timestamp() * 1000)
    ticker.history.assert_called_once_with(period="5d", auto_adjust=False)

    main_seg, _parts = format_quote_change_parts(
        snap,
        None,
        now_sat,
        None,
    )
    assert isinstance(main_seg, str)
    assert "last close 10-23" in main_seg
    assert "$582.50" in main_seg


@pytest.mark.asyncio
async def test_yfinance_keyerror_during_intraday_drops_unfinished_today_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fast_info raises KeyError during regular trading hours (before 16:00 ET), today's unfinished bar is dropped and the prior completed session's close is returned with a non-future timestamp."""
    broken_fi = create_autospec(FastInfo, instance=True)
    broken_fi.get.side_effect = KeyError("currentTradingPeriod")

    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = broken_fi
    idx = pd.to_datetime(
        [
            "2026-10-21 00:00:00-04:00",
            "2026-10-22 00:00:00-04:00",
            "2026-10-23 00:00:00-04:00",
        ]
    )
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [565.0, 570.0, 575.0],
            "High": [572.0, 580.0, 585.0],
            "Low": [564.0, 568.0, 572.0],
            "Close": [570.0, 575.0, 582.5],
            "Volume": [9_000_000, 10_000_000, 4_000_000],
        },
        index=idx,
    )

    # Friday 2026-10-23 14:30 ET (18:30 UTC) — before 16:00 ET close
    now_intraday = datetime(2026, 10, 23, 18, 30, tzinfo=timezone.utc)
    now_ms = int(now_intraday.timestamp() * 1000)
    monkeypatch.setattr(
        yf_ds_mod,
        "_now_utc",
        create_autospec(yf_ds_mod._now_utc, return_value=now_intraday),
    )
    monkeypatch.setattr(
        yf_ds_mod,
        "yahoo_ticker",
        create_autospec(yf_ds_mod.yahoo_ticker, return_value=ticker),
    )

    snap = yf_ds_mod._fetch_single_snapshot("META")
    assert snap is not None
    assert snap["regular_trading_date"] == "2026-10-22"
    assert snap["price"] == 575.0
    assert snap["previous_close"] == 570.0
    assert snap["change"] == 5.0
    assert snap["as_of"] is not None and snap["as_of"] <= now_ms
    assert snap["regular_as_of"] is not None and snap["regular_as_of"] <= now_ms
    ticker.history.assert_called_once_with(period="5d", auto_adjust=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sym", "dates", "now"),
    [
        # US early close (13:00 ET on 2026-11-27), read at 14:30 ET.
        (
            "META",
            ["2026-11-24", "2026-11-25", "2026-11-27"],
            datetime(2026, 11, 27, 19, 30, tzinfo=timezone.utc),
        ),
        # Hong Kong closes 16:00 HKT (08:00 UTC), read at 10:00 ET the same day.
        (
            "0700.HK",
            ["2026-10-21", "2026-10-22", "2026-10-23"],
            datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc),
        ),
    ],
)
async def test_yfinance_keyerror_keeps_today_bar_once_its_session_closed(
    monkeypatch: pytest.MonkeyPatch, sym: str, dates: list[str], now: datetime
) -> None:
    """A bar whose exchange session has closed is kept even before 16:00 ET."""
    broken_fi = create_autospec(FastInfo, instance=True)
    broken_fi.get.side_effect = KeyError("currentTradingPeriod")
    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = broken_fi
    ticker.history.return_value = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 104.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 103.0],
            "Volume": [1_000, 1_000, 1_000],
        },
        index=pd.to_datetime(dates),
    )
    monkeypatch.setattr(
        yf_ds_mod, "_now_utc", create_autospec(yf_ds_mod._now_utc, return_value=now)
    )
    monkeypatch.setattr(
        yf_ds_mod,
        "yahoo_ticker",
        create_autospec(yf_ds_mod.yahoo_ticker, return_value=ticker),
    )

    snap = yf_ds_mod._fetch_single_snapshot(sym)
    assert snap is not None
    assert snap["regular_trading_date"] == dates[-1]
    assert snap["price"] == 103.0
    assert snap["previous_close"] == 101.5
    assert snap["as_of"] <= int(now.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Item 4: Background news polling excludes FMP & skips feed when only FMP is configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_refresh_service_excludes_fmp_and_skips_when_only_fmp_configured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NewsRefreshService._resolve_source(None) strips FMP from the provider chain; when only FMP is configured, _refresh_feed logs an info message and skips without calling FMP."""
    import src.data_client as data_client_mod
    import src.data_client.registry as registry_mod

    fmp_src = create_autospec(NewsDataSource, instance=True)
    yf_src = create_autospec(NewsDataSource, instance=True)
    full_chain = NewsDataProvider([("fmp", fmp_src), ("yfinance", yf_src)])

    get_provider_mock = create_autospec(
        data_client_mod.get_news_data_provider, return_value=full_chain
    )
    monkeypatch.setattr(
        data_client_mod,
        "get_news_data_provider",
        get_provider_mock,
    )

    svc = NewsRefreshService()
    resolved = await svc._resolve_source(None)
    assert isinstance(resolved, NewsDataProvider)
    assert resolved.source_names == ["yfinance"]
    # User-initiated singleton still has both providers
    assert full_chain.source_names == ["fmp", "yfinance"]

    # When only FMP is in the provider chain, _resolve_source(None) returns None
    # and _refresh_feed skips the feed with an INFO log (never falling back to FMP).
    fmp_only_chain = NewsDataProvider([("fmp", fmp_src)])
    get_provider_mock.return_value = fmp_only_chain
    fake_cache = create_autospec(NewsCacheService, instance=True)
    fake_cache.acquire_lock.return_value = True
    svc._cache = fake_cache

    assert await svc._resolve_source(None) is None
    with caplog.at_level(logging.INFO):
        await svc._refresh_feed(NewsPollFeedConfig(provider=None, limit=50))
    fmp_src.get_news.assert_not_called()
    assert any(
        "skipped (no non-FMP news provider available)" in r.getMessage()
        for r in caplog.records
    )

    # Also verify start() drops any feed explicitly targeting provider="fmp"
    poll_cfg = NewsPollConfig(
        enabled=True,
        interval_seconds=60,
        max_items=50,
        feeds=[NewsPollFeedConfig(provider="fmp", limit=50)],
    )
    monkeypatch.setattr(
        news_refresh_mod,
        "get_news_poll_config",
        create_autospec(news_refresh_mod.get_news_poll_config, return_value=poll_cfg),
    )
    monkeypatch.setattr(
        registry_mod,
        "news_source_available",
        create_autospec(registry_mod.news_source_available, return_value=True),
    )
    await svc.start()
    assert svc._feeds == []


def test_fmp_quota_exhausted_message_is_accurate() -> None:
    reset = datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)
    err = FmpQuotaExhausted(reset)
    msg = str(err)
    assert "only FMP serves this data" not in msg
    assert "resets at 2026-10-24 15:00 US/Eastern" in msg
    assert "yfinance" in msg


# ---------------------------------------------------------------------------
# Item 4 (Round 1): ModelResilienceMiddleware logs fallback reason with scrubbed error
# ---------------------------------------------------------------------------


def test_model_fallback_emits_warning_log_with_reason_and_scrubs_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_fallback logs a WARNING with from_model, to_model, status_code, error_type, and sanitized error, and includes error_type in UI props."""
    import ptc_agent.agent.middleware.model_resilience as mr_mod

    warning_spy = create_autospec(mr_mod.logger.warning)
    monkeypatch.setattr(mr_mod.logger, "warning", warning_spy)

    captured_ui: list[dict[str, Any]] = []
    monkeypatch.setattr(
        lg_ui,
        "get_stream_writer",
        create_autospec(lg_ui.get_stream_writer, return_value=captured_ui.append),
    )

    def _noop_send(_writes: Any) -> None:
        return None

    monkeypatch.setattr(
        lg_ui,
        "get_config",
        create_autospec(
            lg_ui.get_config,
            return_value={CONF: {CONFIG_KEY_SEND: create_autospec(_noop_send)}},
        ),
    )

    mw = ModelResilienceMiddleware(primary_name="claude-opus-4-7")
    exc = RuntimeError("Rate limit for api_key=sk-secretkey1234567890 on upstream")
    record = _AttemptRecord(
        model="claude-opus-4-7",
        exc=exc,
        status_code=429,
        attempts=2,
    )
    req = SimpleNamespace(state={"messages": []})
    mw._emit_fallback(record, "muse-wonder-1.0-oauth", None, req, from_is_primary=True)

    warning_spy.assert_called_once()
    call_kwargs = warning_spy.call_args.kwargs
    assert call_kwargs["from_model"] == "claude-opus-4-7"
    assert call_kwargs["to_model"] == "muse-wonder-1.0-oauth"
    assert call_kwargs["status_code"] == 429
    assert call_kwargs["error_type"] == "RuntimeError"
    assert "sk-secretkey1234567890" not in call_kwargs["error"]
    assert "[REDACTED]" in call_kwargs["error"]

    assert len(captured_ui) == 1
    props = captured_ui[0]["props"]
    assert props["error_type"] == "RuntimeError"
    assert "sk-secretkey1234567890" not in props["error"]
    assert "error_type" in MODEL_FALLBACK_FIELDS


# ---------------------------------------------------------------------------
# Item 5 & 6 (Round 2): Sandbox uv pip install --system, skip local workspace dirs & skip on probe failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_pip_install_uses_system_and_skips_local_workspace_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_install_dependencies, _install_package, and install_sandbox_packages use --system, local workspace folders/modules are skipped, and probe failure skips auto-install."""
    sandbox = create_autospec(PTCSandbox, instance=True)
    runtime = create_autospec(SandboxRuntime, instance=True)
    sandbox.runtime = runtime
    sandbox.layout = SimpleNamespace(root="/home/workspace")
    sandbox.workspace.return_value = WorkspaceLayout(
        "/home/workspace", "ai_strategy_lp"
    )

    commands: list[str] = []

    async def _runtime_call(fn: Any, cmd: str, **_kwargs: Any) -> ExecResult:
        commands.append(cmd)
        if "printf" in cmd:
            return ExecResult(exit_code=0, stdout="local_helper\n", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    sandbox._runtime_call.side_effect = _runtime_call

    # 1. missing_import reads only the RAISED_FILE record's own name, takes its
    # base package, and nothing a run printed can name one: a forged
    # "ModuleNotFoundError" line (or any text with shell metacharacters) fails
    # the dotted-path match, so no dependency-confusion install can fire.
    assert mcp_setup.missing_import("scipy.stats") == "scipy"
    assert mcp_setup.missing_import("ai_strategy_lp") == "ai_strategy_lp"
    assert mcp_setup.missing_import(None) is None
    assert (
        mcp_setup.missing_import("ModuleNotFoundError: No module named 'evil'")
        is None
    )

    # 2. The retry loop's Dalton filter drops the workspace dir_name
    # ('ai_strategy_lp') by known name and 'local_helper' by the on-disk probe.
    detected = ["ai_strategy_lp", "local_helper", "scipy"]
    filtered = await mcp_setup._filter_local_packages(sandbox, detected)
    assert filtered == ["scipy"]

    # 2b. When the local directory probe fails (nonzero exit code or exception),
    # _filter_local_packages logs a warning and returns [] so candidates are NOT installed.
    warn_spy = create_autospec(mcp_setup.logger.warning)
    monkeypatch.setattr(mcp_setup.logger, "warning", warn_spy)

    async def _probe_nonzero(fn: Any, cmd: str, **_kwargs: Any) -> ExecResult:
        return ExecResult(exit_code=1, stdout="", stderr="probe error")

    sandbox._runtime_call.side_effect = _probe_nonzero
    assert await mcp_setup._filter_local_packages(sandbox, ["scipy"]) == []
    assert warn_spy.call_count == 1

    sandbox._runtime_call.side_effect = RuntimeError("sandbox disconnected")
    assert await mcp_setup._filter_local_packages(sandbox, ["scipy"]) == []
    assert warn_spy.call_count == 2

    sandbox._runtime_call.side_effect = _runtime_call

    # 3. _install_package and _install_dependencies pass --system
    commands.clear()
    assert await mcp_setup._install_package(sandbox, "scipy") is True
    assert commands == ["uv pip install --system -q scipy"]

    commands.clear()
    await mcp_setup._install_dependencies(sandbox)
    assert any("uv pip install --system -q --override" in c for c in commands)

    # 4. workspace_sandbox.install_sandbox_packages passes --system
    sandbox.execute_bash_command.return_value = {
        "success": True,
        "stdout": "installed",
        "stderr": "",
    }
    monkeypatch.setattr(
        workspace_sandbox,
        "_get_sandbox",
        create_autospec(
            workspace_sandbox._get_sandbox, return_value=(None, sandbox)
        ),
    )
    resp = await workspace_sandbox.install_sandbox_packages(
        workspace_id="ws-1",
        x_user_id="user-1",
        body=PackageInstallRequest(packages=["openpyxl"]),
    )
    assert resp.success is True
    sandbox.execute_bash_command.assert_awaited_once_with(
        "uv pip install --system openpyxl", timeout=120
    )

    # 5. execution.start_code_job (P0-11 hook site): a code run that ended on a
    # missing import installs it and runs again — unless the name is a local
    # workspace folder, or the local probe could not say, in which case nothing
    # is installed and the failed outcome is returned as it stands.
    from ptc_agent.core.sandbox import execution as sandbox_execution
    from ptc_agent.core.sandbox.jobs import Ended, JobHandle, JobRef

    def _code_run(raised: str | None, *, exit_code: int) -> Ended:
        """One ended code run as the retry loop reads it: the module its own
        unhandled exception failed to import sits in ``raised``, apart from the
        output."""
        ref = JobRef(
            command_id="bg-000000000001",
            state_dir="/root/.system/jobs/bg-000000000001",
            log_path=(
                "/home/workspace/ai_strategy_lp/.agents/threads/t1/"
                "commands/bg-000000000001.log"
            ),
            trace_path="/root/.system/trace/bg-000000000001.jsonl",
            log_relpath=".agents/threads/t1/commands/bg-000000000001.log",
        )
        handle = JobHandle(ref=ref, started_at=datetime.now(timezone.utc), kind="code")
        return Ended(handle=handle, exit_code=exit_code, output="", raised=raised)

    # The install goes through the double's _install_package into the real
    # mcp_setup one, so the --system wording in `commands` is what ran, not a
    # mock's record of the call.
    async def _real_install(package: str) -> bool:
        return await mcp_setup._install_package(sandbox, package)

    sandbox._install_package.side_effect = _real_install
    run_code = create_autospec(sandbox_execution._run_code)
    monkeypatch.setattr(sandbox_execution, "_run_code", run_code)

    # 5a. Local folder 'ai_strategy_lp': filtered out -> no uv pip install, no retry
    commands.clear()
    run_code.reset_mock()
    run_code.side_effect = [_code_run("ai_strategy_lp", exit_code=1)]
    out_local = await sandbox_execution.start_code_job(
        sandbox, "import ai_strategy_lp", wait_s=5.0
    )
    assert isinstance(out_local, Ended) and out_local.exit_code == 1
    assert run_code.await_count == 1
    sandbox._install_package.assert_not_awaited()
    assert not any("uv pip install" in c for c in commands)

    # 5b. Genuine package 'scipy': installed with --system and rerun once
    commands.clear()
    run_code.reset_mock()
    sandbox._install_package.reset_mock()
    run_code.side_effect = [
        _code_run("scipy", exit_code=1),
        _code_run(None, exit_code=0),
    ]
    out_scipy = await sandbox_execution.start_code_job(
        sandbox, "import scipy.stats", wait_s=5.0
    )
    assert isinstance(out_scipy, Ended) and out_scipy.exit_code == 0
    assert run_code.await_count == 2
    sandbox._install_package.assert_awaited_once_with("scipy")
    assert "uv pip install --system -q scipy" in commands

    # 5c. The local probe failing: no install, no retry, the outcome stands
    commands.clear()
    run_code.reset_mock()
    sandbox._install_package.reset_mock()
    sandbox._runtime_call.side_effect = _probe_nonzero
    run_code.side_effect = [_code_run("scipy", exit_code=1)]
    out_probe = await sandbox_execution.start_code_job(
        sandbox, "import scipy", wait_s=5.0
    )
    assert isinstance(out_probe, Ended) and out_probe.exit_code == 1
    assert run_code.await_count == 1
    sandbox._install_package.assert_not_awaited()
    assert not any("uv pip install" in c for c in commands)

    sandbox._runtime_call.side_effect = _runtime_call


# ---------------------------------------------------------------------------
# Item 6 (Round 1): demote_clientless only inspects current role's subagents on primary slot
# ---------------------------------------------------------------------------


def test_demote_clientless_skips_other_roles_and_non_primary_slots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home (chief_of_staff) and non-primary slots do not warn on or demote analyst-only subagents like dalton-reviewer."""
    reviewer = SubagentDefinition(
        name="dalton-reviewer",
        description="reviewer",
        model="grok-4.7-oauth",
        roles=frozenset({"analyst"}),
    )
    general = SubagentDefinition(
        name="general-purpose",
        description="general",
        model=None,
    )
    roster = ResolvedSubagents(
        definitions={"dalton-reviewer": reviewer, "general-purpose": general},
        enabled=("dalton-reviewer", "general-purpose"),
    )
    config = SimpleNamespace(
        subagents=SubagentsConfig(resolved=roster),
        subsidiary_llm_clients={},
        role_prompt_guidance={"subagent:dalton-reviewer": "guidance:grok"},
    )

    # 1. chief_of_staff role -> does not touch analyst-only dalton-reviewer
    with caplog.at_level(logging.WARNING):
        demote_clientless(config, role="chief_of_staff", slot="primary")
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        config.subagents.resolved.definitions["dalton-reviewer"].model
        == "grok-4.7-oauth"
    )

    # 2. non-primary slot -> does not touch any subagents even for analyst role
    with caplog.at_level(logging.WARNING):
        demote_clientless(config, role="analyst", slot="background")
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        config.subagents.resolved.definitions["dalton-reviewer"].model
        == "grok-4.7-oauth"
    )

    # 3. analyst role on primary slot -> demotes clientless dalton-reviewer with a warning
    with caplog.at_level(logging.WARNING):
        demote_clientless(config, role="analyst", slot="primary")
    assert config.subagents.resolved.definitions["dalton-reviewer"].model is None
    assert any("dalton-reviewer" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Item 7 (Round 1): Tracking GET returns 200 {"status": "not_applicable"} for topic workspaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracking_get_returns_200_not_applicable_for_topic_workspace_while_post_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topic workspaces without a company ticker return 200 status='not_applicable' on GET and 400 on POST."""
    topic_ws_id = "00000000-0000-0000-0000-000000000099"

    mock_get_ws = create_autospec(
        ws_db.get_workspace,
        return_value={
            "workspace_id": topic_ws_id,
            "user_id": "user-1",
            "name": "人形机器人专题",
            "status": "running",
            "metadata": {},
        },
    )
    monkeypatch.setattr(ws_db, "get_workspace", mock_get_ws)
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[]),
    )

    st = await get_tracking_status("user-1", topic_ws_id)
    assert st["ok"] is True
    assert st["status"] == "not_applicable"
    assert st["enabled"] is False
    assert st["tasks"] == []

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = create_autospec(
        get_current_user_id, return_value="user-1"
    )
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    r_get = client.get(f"/api/v1/dalton/tracking/{topic_ws_id}")
    assert r_get.status_code == 200
    body = r_get.json()
    assert body["status"] == "not_applicable"
    assert body["can_edit"] is False

    import src.dalton.autonomy.tracking as tracking_mod

    @asynccontextmanager
    async def _fake_lock(_uid: str):
        yield None

    monkeypatch.setattr(
        tracking_mod,
        "user_automations_lock",
        create_autospec(tracking_mod.user_automations_lock, side_effect=_fake_lock),
    )

    r_post = client.post(
        f"/api/v1/dalton/tracking/{topic_ws_id}", json={"enabled": True}
    )
    assert r_post.status_code == 400


# ---------------------------------------------------------------------------
# Item 8 (Round 1) & Item 5 (Round 2): Subagent prompt includes workspace root across all 3 paths
# ---------------------------------------------------------------------------


def test_subagent_prompt_includes_workspace_absolute_root_across_all_three_paths() -> None:
    """All three prompt paths (default base, custom_prompt, custom_prompt_template) inject the workspace absolute root and skills path prefix."""
    project = ProjectContext(workspace_id="ws-dash", dir_name="DASH")
    compiler = SubagentCompiler(project=project)

    # Path 1: Default base template (including read-only plugin subagent with workspace_paths=False)
    reviewer_defn = plugin_definition(
        {
            "name": "dalton-reviewer",
            "fields": {
                "description": "Independent reviewer",
                "tools": ["web"],
            },
            "instructions": "Review the draft.",
            "roles": ["analyst"],
        }
    )
    assert reviewer_defn.sections.get("workspace_paths") is False

    # Path 2: custom_prompt
    custom_prompt_defn = SubagentDefinition(
        name="custom-reviewer",
        description="Custom prompt subagent",
        custom_prompt="You are a custom reviewer.",
        tools=["web_search"],
    )

    # Path 3: custom_prompt_template
    custom_template_defn = SubagentDefinition(
        name="template-reviewer",
        description="Custom template subagent",
        custom_prompt_template="subagents/roles/general.md.j2",
        tools=["web_search"],
    )

    for defn in (reviewer_defn, custom_prompt_defn, custom_template_defn):
        prompt = compiler.compile(defn)["system_prompt"]
        assert "`/home/workspace/DASH`" in prompt
        assert "`/home/workspace/DASH/.agents/skills/<skill_name>/SKILL.md`" in prompt
        assert "never invent host paths such as `/Users/...`" in prompt


# ---------------------------------------------------------------------------
# B7 Round 2 Item 1: Concurrent delete vs publish mutual exclusion on workspace row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_delete_workspace_rechecks_published_reports_under_row_lock_and_refuses() -> None:
    """If a report is published before db_delete_workspace acquires FOR UPDATE on the workspace row, the in-transaction check sees it and aborts deletion without marking status='deleted'."""
    executed_sqls: list[str] = []

    async def _execute(sql: str, params: Any = None) -> None:
        executed_sqls.append(sql)

    fetch_rows = iter(
        [
            {"user_id": "user-1"},
            None,  # lock_user_egress_state (advisory xact lock does not fetch)
            {"workspace_id": "ws-1", "computer_id": "comp-1"},
            {"busy": False},
            {"moving": False},
            {"has_pub": True},
        ]
    )

    async def _fetchone() -> dict[str, Any] | None:
        last_sql = executed_sqls[-1] if executed_sqls else ""
        if "SELECT user_id FROM workspaces" in last_sql:
            return {"user_id": "user-1"}
        if "FOR UPDATE" in last_sql:
            return {"workspace_id": "ws-1", "computer_id": "comp-1"}
        if "AS busy" in last_sql:
            return {"busy": False}
        if "AS moving" in last_sql:
            return {"moving": False}
        if "FROM dalton_publications" in last_sql:
            return {"has_pub": True}
        return next(fetch_rows, None)

    fake_cur = create_autospec(AsyncCursor, instance=True)
    fake_cur.execute.side_effect = _execute
    fake_cur.fetchone.side_effect = _fetchone
    fake_cur.__aenter__.return_value = fake_cur
    fake_cur.__aexit__.return_value = None

    @asynccontextmanager
    async def _fake_tx():
        yield None

    fake_conn = create_autospec(AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cur
    fake_conn.transaction.side_effect = _fake_tx

    with pytest.raises(
        ws_db.WorkspaceHasPublishedReportsError,
        match=ws_db.PUBLISHED_REPORTS_DELETE_REFUSAL,
    ):
        await ws_db.delete_workspace(
            "ws-1", conn=fake_conn, bound_to="comp-1", keep_folder=False
        )

    for_update_idx = next(
        i for i, q in enumerate(executed_sqls) if "FROM workspaces" in q and "FOR UPDATE" in q
    )
    pub_check_idx = next(
        i for i, q in enumerate(executed_sqls) if "FROM dalton_publications" in q
    )
    assert for_update_idx < pub_check_idx
    assert not any("UPDATE workspaces" in q for q in executed_sqls)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["deleted", "archiving"])
async def test_insert_publication_and_run_publish_reject_deleted_or_archiving_workspace(
    monkeypatch: pytest.MonkeyPatch,
    bad_status: str,
) -> None:
    """insert_publication locks the workspace row FOR SHARE and refuses when status is deleted or archiving; run_publish transitions the request to rejected and returns an explicit error."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import publications as db_publications
    from src.dalton.publish import files as publish_files
    from src.dalton.publish import service as publish_svc

    executed_sqls: list[str] = []

    async def _execute(sql: str, params: Any = None) -> None:
        executed_sqls.append(sql)

    async def _fetchone() -> dict[str, Any] | None:
        last_sql = executed_sqls[-1] if executed_sqls else ""
        if "FROM workspaces" in last_sql and "FOR SHARE" in last_sql:
            return {"workspace_id": "ws-1", "status": bad_status}
        return None

    fake_cur = create_autospec(AsyncCursor, instance=True)
    fake_cur.execute.side_effect = _execute
    fake_cur.fetchone.side_effect = _fetchone
    fake_cur.__aenter__.return_value = fake_cur
    fake_cur.__aexit__.return_value = None

    @asynccontextmanager
    async def _fake_tx():
        yield None

    fake_conn = create_autospec(AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cur
    fake_conn.transaction.side_effect = _fake_tx

    @asynccontextmanager
    async def _fake_get_db_conn():
        yield fake_conn

    monkeypatch.setattr(
        db_publications,
        "get_db_connection",
        create_autospec(db_publications.get_db_connection, side_effect=_fake_get_db_conn),
    )

    with pytest.raises(
        db_publications.WorkspaceUnavailableForPublishError,
        match=bad_status,
    ):
        await db_publications.insert_publication(
            publish_request_id="req-1",
            user_id="user-1",
            workspace_id="ws-1",
            ticker="NVDA",
            kind="event_note",
            title="NVDA Note",
            published_date="2026-10-27",
            files=[{"path": "published/2026-10-27-nvda.md"}],
            file_blobs=[],
            bundle_sha256="a" * 64,
            sources=[],
            audit_summary={},
            reviewer={},
            approver_user_id="user-1",
            approved_at=datetime.now(timezone.utc),
        )

    assert any("FROM workspaces" in q and "FOR SHARE" in q for q in executed_sqls)
    assert not any("INSERT INTO dalton_publications" in q for q in executed_sqls)

    # Also verify run_publish surfaces the rejection cleanly and marks the request rejected
    class _BackendProto:
        async def aread_text(self, path: str) -> str | None: ...
        async def awrite_text(self, path: str, content: str) -> None: ...

    fake_backend = create_autospec(_BackendProto, instance=True)
    draft_md = "# NVDA Note\n\nQualitative summary."
    fake_backend.aread_text.side_effect = (
        lambda path: draft_md if path == "drafts/nvda.md" else None
    )

    bundle_sha = publish_files.compute_bundle_sha256(draft_md.encode("utf-8"), [])
    monkeypatch.setattr(
        publish_svc,
        "verify_workspace_access",
        create_autospec(publish_svc.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        db_publications,
        "resolve_thread_author_model",
        create_autospec(db_publications.resolve_thread_author_model, return_value="claude-opus-5-5-oauth"),
    )
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
    )
    monkeypatch.setattr(
        db_publications,
        "get_request",
        create_autospec(
            db_publications.get_request,
            return_value={
                "publish_request_id": "req-1",
                "workspace_id": "ws-1",
                "user_id": "user-1",
                "status": "publishing",
                "bundle_sha256": bundle_sha,
                "audit": {"figures": 0, "matched": 0, "verified": 0, "contradicted": 0, "unverified": 0, "uncertain": 0, "exempt": 0, "matches": []},
                "review": {"model": "gemini-3.1-pro", "family": "google", "verdict": "pass"},
            },
        ),
    )
    mock_transition = create_autospec(db_publications.transition, return_value=None)
    monkeypatch.setattr(db_publications, "transition", mock_transition)

    res = await publish_svc.run_publish(
        user_id="user-1",
        workspace_id="ws-1",
        thread_id="th-1",
        tool_call_id="tc-1",
        draft_path="drafts/nvda.md",
        title="NVDA Note",
        kind="event_note",
        slug="nvda-note",
        request_id="req-1",
        backend=fake_backend,
    )
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert bad_status in res["message"]
    mock_transition.assert_awaited_once()
    assert mock_transition.await_args.args[:3] == ("req-1", ("publishing",), "rejected")


# ---------------------------------------------------------------------------
# Sync2 B12 (#637): Payer, Dalton budget admission before fallback, empty
# fallback_models list, Grok OAuth display name unification, and task_costs
# isolation from Dalton budget ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b12_zero_budget_blocks_fallback_on_main_and_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When budget is 0 / exhausted, a failing turn on main or subagent does not initiate fallback."""
    from decimal import Decimal
    from fastapi import HTTPException
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from ptc_agent.agent.middleware.credit_gate import current_credit_gate
    import src.dalton.budget.gate as budget_gate_mod

    # 1. Pre-turn admission: enforce_local_admission rejects when available == 0
    # regardless of byok=True / payer tag.
    fake_conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _fake_tx():
        yield None

    @asynccontextmanager
    async def _fake_get_db():
        yield fake_conn

    fake_conn.transaction.side_effect = _fake_tx
    monkeypatch.setattr(budget_gate_mod, "get_db_connection", _fake_get_db)
    monkeypatch.setattr(
        budget_gate_mod,
        "reconcile_and_summarize_day",
        create_autospec(
            budget_gate_mod.reconcile_and_summarize_day,
            return_value={
                "configured": True,
                "retry_after_seconds": 3600,
                "_decimals": {
                    "cap_usd": Decimal("50"),
                    "spent_usd": Decimal("50"),
                    "settled_usd": Decimal("50"),
                    "unreserved_usage_usd": Decimal("0"),
                    "open_reserved_usd": Decimal("0"),
                    "expired_conservative_usd": Decimal("0"),
                },
            },
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        await budget_gate_mod.enforce_local_admission("user-1", byok=True)
    assert exc_info.value.status_code == 429

    # 2. Mid-turn resilience with real CreditGateState (main gate + spawned
    # subagent child gate sharing the same CreditLease):
    # (a) when budget is sufficient, main and subagent make model calls normally
    #     (and fall back normally on primary failure);
    # (b) when the shared lease is exhausted, neither main agent nor subagent
    #     selects or initiates any fallback candidate.
    main_gate = budget_gate_mod.build_local_run_gate(
        user_id="user-1",
        run_id="run-b12-1",
        token_callback=None,
        is_byok=True,
    )
    assert main_gate is not None
    sub_gate = main_gate.spawn_child(
        run_ref="task-b12-1",
        tracker=None,
        tool_tracker=None,
    )
    assert sub_gate.lease is main_gate.lease

    primary_client = SimpleNamespace(
        __langalpha_meta__={"manifest_model": "claude-opus-4-7", "billing_type": "oauth"}
    )
    subagent_client = SimpleNamespace(
        __langalpha_meta__={"manifest_model": "grok-4.7-oauth", "billing_type": "oauth"}
    )
    fallback_client = SimpleNamespace(
        __langalpha_meta__={"manifest_model": "deepseek-flash", "billing_type": "platform"}
    )

    mw = ModelResilienceMiddleware(
        primary_name="claude-opus-4-7",
        primary_client=primary_client,
        fallbacks=[("deepseek-flash", fallback_client)],
        max_retries=0,
        users_fallbacks=True,
    )
    emit_fallback_spy = create_autospec(mw._emit_fallback)
    monkeypatch.setattr(mw, "_emit_fallback", emit_fallback_spy)

    ok_response = create_autospec(ModelResponse, instance=True)
    ok_response.result = []
    called_models: list[Any] = []

    async def _ok_handler(req: ModelRequest) -> ModelResponse:
        called_models.append(req.model)
        return ok_response

    async def _failing_handler(req: ModelRequest) -> ModelResponse:
        called_models.append(req.model)
        raise RuntimeError("primary provider 503 failure")

    def _make_req(client: Any) -> ModelRequest:
        req = create_autospec(ModelRequest, instance=True)
        req.model = client
        req.messages = []
        req.model_settings = {}
        req.state = {"messages": []}
        req.override.side_effect = lambda **kw: SimpleNamespace(
            model=kw.get("model", client),
            messages=kw.get("messages", []),
            model_settings={},
            state={"messages": []},
        )
        return req

    # Scenario A: Sufficient budget (grant active, denial is None) -> normal model calls work
    # on both main agent and subagent without AttributeError.
    main_gate.lease.ceiling_credits = 500.0
    main_gate.lease.denial = None

    token = current_credit_gate.set(main_gate)
    try:
        called_models.clear()
        res_main = await mw.awrap_model_call(_make_req(primary_client), _ok_handler)
        assert res_main is ok_response
        assert called_models == [primary_client]
    finally:
        current_credit_gate.reset(token)

    token = current_credit_gate.set(sub_gate)
    try:
        called_models.clear()
        res_sub = await mw.awrap_model_call(_make_req(subagent_client), _ok_handler)
        assert res_sub is ok_response
        assert called_models == [subagent_client]
    finally:
        current_credit_gate.reset(token)

    # Scenario B: Exhausted budget (denial recorded and spend >= ceiling_credits) ->
    # failing turn on both main agent and subagent does NOT initiate fallback.
    main_gate.lease.ceiling_credits = 0.0
    main_gate.lease.denial = {"message": "Daily budget exhausted"}

    token = current_credit_gate.set(main_gate)
    try:
        called_models.clear()
        emit_fallback_spy.reset_mock()
        with pytest.raises(RuntimeError, match="primary provider 503 failure"):
            await mw.awrap_model_call(_make_req(primary_client), _failing_handler)
        assert called_models == [primary_client]
        emit_fallback_spy.assert_not_called()
    finally:
        current_credit_gate.reset(token)

    token = current_credit_gate.set(sub_gate)
    try:
        called_models.clear()
        emit_fallback_spy.reset_mock()
        with pytest.raises(RuntimeError, match="primary provider 503 failure"):
            await mw.awrap_model_call(_make_req(subagent_client), _failing_handler)
        assert called_models == [subagent_client]
        emit_fallback_spy.assert_not_called()
    finally:
        current_credit_gate.reset(token)


@pytest.mark.asyncio
async def test_b12_saved_empty_fallback_list_never_falls_back_to_system_defaults() -> None:
    """When a user saves an empty fallback list [], it stays [] and does not fall back to system defaults."""
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from ptc_agent.agent.middleware.model_resilience import build_fallback_pairs
    from src.server.services.llm.availability import drop_dead_models
    from src.server.services.llm.clients import _resolve_fallback_clients
    from src.server.services.llm.config import select_model

    # 1. drop_dead_models preserves an explicit empty fallback_models list []
    # and keeps [] when every saved fallback model was removed from the catalog.
    pruned_empty, dropped_empty = drop_dead_models({"fallback_models": []}, ())
    assert pruned_empty == {"fallback_models": []}
    assert dropped_empty == []

    pruned_dead, dropped_dead = drop_dead_models(
        {"fallback_models": ["nonexistent-dead-model-xyz"]}, ()
    )
    assert pruned_dead == {"fallback_models": []}
    assert dropped_dead == [("fallback_models", "nonexistent-dead-model-xyz")]

    # 2. select_model overrides system default fallback list when user_fallback == []
    cfg = SimpleNamespace(
        llm=SimpleNamespace(
            name="deepseek-flash",
            flash=None,
            compaction=None,
            fetch=None,
            fallback=["deepseek-flash", "glm-5.2"],
        ),
        subagents=SimpleNamespace(),
    )
    selection = select_model(
        cfg,
        {"fallback_models": []},
        "primary",
        "claude-opus-4-7",
    )
    assert selection.effective_model == "claude-opus-4-7"
    assert selection.users_fallbacks is True
    assert cfg.llm.fallback == []

    # 3. _resolve_fallback_clients and build_fallback_pairs keep [] without loading defaults
    await _resolve_fallback_clients(
        cfg,
        "u-1",
        {"fallback_models": []},
        {},
        is_byok=False,
        cache_key=None,
        users_fallbacks=selection.users_fallbacks,
    )
    assert cfg.fallback_llm_clients == []
    assert cfg.fallback_llm_names == []
    assert build_fallback_pairs(cfg) == []

    # 4. Middleware does not fall back when fallbacks == []
    primary_client = SimpleNamespace(
        __langalpha_meta__={"manifest_model": "claude-opus-4-7", "billing_type": "platform"}
    )
    mw = ModelResilienceMiddleware(
        primary_name="claude-opus-4-7",
        primary_client=primary_client,
        fallbacks=build_fallback_pairs(cfg),
        max_retries=0,
        users_fallbacks=selection.users_fallbacks,
    )
    req = create_autospec(ModelRequest, instance=True)
    req.model = primary_client
    req.messages = []
    req.model_settings = {}
    req.state = {"messages": []}
    calls: list[Any] = []

    async def _fail(r: ModelRequest) -> ModelResponse:
        calls.append(r.model)
        raise RuntimeError("primary failed with empty fallbacks")

    with pytest.raises(RuntimeError, match="primary failed with empty fallbacks"):
        await mw.awrap_model_call(req, _fail)
    assert calls == [primary_client]


def test_b12_grok_oauth_display_name_unified_and_pinned_routing_by_key() -> None:
    """grok-4.7-oauth and grok-4.3-oauth drop (OAuth) suffix in display_name while staying pinned to xai-oauth."""
    from src.dalton.llm.overlay import PINNED_OVERLAY_MODEL_KEYS, load_dalton_meta
    from src.llms.llm import LLM

    mc = LLM.get_model_config()
    all_meta = load_dalton_meta()
    for key, expected_display in (
        ("grok-4.7-oauth", "Grok 4.7"),
        ("grok-4.3-oauth", "Grok 4.3"),
        ("grok-4.7-build-fast-oauth", "Grok 4.7 Build Fast"),
    ):
        if key in ("grok-4.7-oauth", "grok-4.3-oauth"):
            assert key in PINNED_OVERLAY_MODEL_KEYS
        cfg = mc.get_model_config(key)
        assert cfg is not None
        assert cfg["provider"] == "xai-oauth"
        assert cfg["display_name"] == expected_display
        assert "(OAuth)" not in cfg["display_name"]
        meta = all_meta.get(key)
        assert meta is not None
        assert meta.get("billing") == "subscription"


def test_b12_task_costs_isolated_from_dalton_budget_ledger() -> None:
    """Upstream task_cost metadata is display-only and never alters Dalton's cash or subscription-notional accounting."""
    from decimal import Decimal
    from src.dalton.llm.complete import compute_call_costs
    from src.llms.task_costs import task_cost

    # Upstream task_cost exists for DeepSeek/Kimi display
    tc = task_cost("deepseek-v3.2", ("low", "medium", "high"))
    assert tc is None or isinstance(tc, dict)

    # Dalton compute_call_costs computes cash and subscription notional strictly from token usage & pricing
    cash_usd, sub_notional_usd = compute_call_costs(
        model_key="deepseek-flash",
        billing_type="platform",
        token_usage={"input_tokens": 1000, "output_tokens": 500},
    )
    assert cash_usd >= Decimal("0")
    assert sub_notional_usd == Decimal("0")


