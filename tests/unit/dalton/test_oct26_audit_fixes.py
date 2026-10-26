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
    settled = await _settled_text(registry, resumable_task, listed=False)
    assert "RunWorkflow(workflow=<same workflow>, resume='s_rL_A')" in settled
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
    assert "RunWorkflow(workflow=<same workflow>, resume='s_rL_A')" in missing
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
    settled_non_resumable = await _settled_text(
        registry, non_resumable_task, listed=False
    )
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
        'RunWorkflow(workflow=<same workflow>, resume="s_rL_A", params=...)'
        in str(msg.content)
    )
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
    mw._emit_fallback(record, "muse-wonder-1.0-oauth", req, from_is_primary=True)

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

    # 1. _detect_missing_imports immediately drops the workspace dir_name ('ai_strategy_lp')
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/home/workspace/ai_strategy_lp/run.py", line 1, in <module>\n'
        "    import ai_strategy_lp\n"
        "ModuleNotFoundError: No module named 'ai_strategy_lp'\n"
        "ModuleNotFoundError: No module named 'local_helper'\n"
        "ModuleNotFoundError: No module named 'scipy'\n"
    )
    detected = mcp_setup._detect_missing_imports(sandbox, tb)
    assert "ai_strategy_lp" not in detected
    assert set(detected) == {"local_helper", "scipy"}

    # 2. _filter_local_packages drops 'local_helper' discovered on disk in the workspace
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
