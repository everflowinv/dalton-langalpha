"""Unit tests for Dalton automation notification dispatch, Feishu Card 2.0, user preferences, and bridge /notify."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import notify_preview  # noqa: E402
import researchdata_bridge as rb  # noqa: E402
from src.dalton.database.verdicts import get_verdict_for_execution  # noqa: E402
import src.dalton.llm.model_prefs as model_prefs_mod  # noqa: E402
from src.dalton.llm.model_prefs import (  # noqa: E402
    is_valid_dalton_notify_target,
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_targets,
    validate_dalton_model_preferences,
)
import src.dalton.notify.dispatch as dispatch_mod  # noqa: E402
from src.dalton.notify.dispatch import (  # noqa: E402
    build_feishu_notify_card,
    claim_execution_notify,
    claim_target_delivery,
    classify_automation_for_notify,
    extract_card_sections,
    extract_summary_lines,
    format_notify_message,
    is_automation_notify_enabled,
    map_verdict_to_header_template,
    mark_target_delivery,
    on_automation_settled,
    populate_automation_notify_state,
    post_bridge_notify,
    resolve_automation_notify_state,
    retry_failed_notify_deliveries,
)
from src.dalton.settings import (  # noqa: E402
    FEISHU_SEND_CARD_TIMEOUT_S,
    get_dalton_frontend_url,
    get_feishu_send_card_timeout_s,
    get_notify_http_timeout_s,
)
from src.llms.preferences import TuningError  # noqa: E402
import src.server.database.pool as pool_mod  # noqa: E402
from src.server.models.automation import AutomationResponse  # noqa: E402


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-gateway-token-xyz987")
    return rb.ResearchDataBridge(rb.BridgeConfig())


@pytest.mark.asyncio
async def test_classify_automation_four_categories_and_watch_delta_excluded() -> None:
    c_t0 = classify_automation_for_notify(
        {"name": "dalton-googl-earn-2026q1-t0", "metadata": {"ticker": "GOOGL"}}
    )
    assert c_t0 is not None
    assert c_t0.category == "earnings_t0"
    assert c_t0.label_zh == "财报 T0 快讯"
    assert c_t0.scope == "GOOGL"
    assert c_t0.requires_non_no_change_verdict is False

    c_tp1 = classify_automation_for_notify({"name": "dalton-nvda-earn-2026q1-tp1"})
    assert c_tp1 is not None
    assert c_tp1.category == "earnings_tp1"
    assert c_tp1.label_zh == "财报 T+1 深度"
    assert c_tp1.scope == "NVDA"

    c_move = classify_automation_for_notify({"name": "dalton-tsla-move-down"})
    assert c_move is not None
    assert c_move.category == "price_move"
    assert c_move.label_zh == "重大股价异动"
    assert c_move.scope == "TSLA"
    assert c_move.requires_non_no_change_verdict is True

    c_tm3 = classify_automation_for_notify({"name": "dalton-msft-earn-2026q2-tm3"})
    assert c_tm3 is not None
    assert c_tm3.category == "earnings_tm3"
    assert c_tm3.label_zh == "财报 T-3 前瞻"
    assert c_tm3.scope == "MSFT"

    c_weekly = classify_automation_for_notify({"name": "dalton-director-weekly"})
    assert c_weekly is not None
    assert c_weekly.category == "director_weekly"
    assert c_weekly.label_zh == "每周复盘"
    assert c_weekly.scope == "Home"

    c_cal = classify_automation_for_notify({"name": "dalton-earnings-calendar"})
    assert c_cal is not None
    assert c_cal.category == "earnings_calendar"
    assert c_cal.label_zh == "财报日历周报"
    assert c_cal.scope == "Home"

    for excluded_name in (
        "dalton-googl-watch",
        "dalton-googl-delta",
        "dalton-director-daily",
        "dalton-data-health",
    ):
        assert classify_automation_for_notify({"name": excluded_name}) is None

    post_fn = create_autospec(post_bridge_notify, return_value={"ok": True, "results": []})
    res = await on_automation_settled(
        automation={"name": "dalton-googl-watch", "user_id": "u-1"},
        execution_id="exec-watch-1",
        post_notify_fn=post_fn,
    )
    assert res is False
    post_fn.assert_not_called()


@pytest.mark.asyncio
async def test_price_move_no_change_or_missing_verdict_is_not_pushed_and_verdict_only_current_run() -> None:
    assert (
        await get_verdict_for_execution(
            automation_execution_id=None,
            run_id=None,
            thread_id="thread-fixed-weekly",
        )
        is None
    )

    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(
        resolve_dalton_notify_targets, return_value=["feishu:user:ou_analyst_a"]
    )
    verdict_no_change = create_autospec(
        get_verdict_for_execution, return_value={"word": "NO_CHANGE"}
    )
    verdict_missing = create_autospec(get_verdict_for_execution, return_value=None)
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    mark_fn = create_autospec(mark_target_delivery, return_value=None)
    post_fn = create_autospec(
        post_bridge_notify,
        return_value={
            "ok": True,
            "results": [{"target": "feishu:user:ou_analyst_a", "ok": True}],
        },
    )
    url_fn = create_autospec(get_dalton_frontend_url, return_value="https://dalton.example.com")

    pushed = await on_automation_settled(
        automation={"name": "dalton-googl-move-up", "user_id": "u-1"},
        execution_id="exec-move-nochange",
        thread_id="th-1",
        final_text="今日股价波动属于大盘beta带动。基本面逻辑无变化。维持原有判断。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_no_change,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed is False
    claim_fn.assert_not_called()
    post_fn.assert_not_called()

    pushed_missing = await on_automation_settled(
        automation={"name": "dalton-googl-move-up", "user_id": "u-1"},
        execution_id="exec-move-missing-verdict",
        thread_id="th-1",
        final_text="今日股价波动但无结论。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_missing,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_missing is False
    claim_fn.assert_not_called()
    post_fn.assert_not_called()

    verdict_changed = create_autospec(
        get_verdict_for_execution, return_value={"word": "THESIS_WEAKENED"}
    )
    pushed_ok = await on_automation_settled(
        automation={
            "name": "dalton-googl-move-down",
            "user_id": "u-1",
            "delivery_result": [{"method": "webhook", "success": True}],
        },
        execution_id="exec-move-changed",
        thread_id="th-2",
        final_text="核心大客户削减订单导致股价大跌。短期营收增速面临下修风险。建议密切跟踪后续指引。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_changed,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert pushed_ok is True
    post_fn.assert_called_once()
    mark_fn.assert_called_once_with(
        "exec-move-changed",
        "feishu",
        "feishu:user:ou_analyst_a",
        status="sent",
        error_code=None,
    )


@pytest.mark.asyncio
async def test_unconfigured_notify_targets_does_not_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(resolve_dalton_notify_targets, return_value=[])
    verdict_fn = create_autospec(get_verdict_for_execution, return_value={"word": "THESIS_STRENGTHENED"})
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    post_fn = create_autospec(post_bridge_notify, return_value={"ok": True, "results": []})

    pushed = await on_automation_settled(
        automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-no-target"},
        execution_id="exec-no-target",
        thread_id="th-3",
        final_text="谷歌Q1营收超预期。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        post_notify_fn=post_fn,
    )
    assert pushed is False
    claim_fn.assert_not_called()
    post_fn.assert_not_called()

    mock_get_prefs = create_autospec(
        model_prefs_mod.get_user_preferences,
        return_value={"model_preference": {}, "other_preference": {}},
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", mock_get_prefs)
    assert await resolve_dalton_notify_targets("u-no-target") == []

    bag = {"dalton_notify_targets": ["feishu:user:ou_test123", "discord:user:987654"]}
    validate_dalton_model_preferences(bag)
    assert bag["dalton_notify_targets"] == ["feishu:user:ou_test123", "discord:user:987654"]

    with pytest.raises(TuningError):
        validate_dalton_model_preferences({"dalton_notify_targets": ["discord:user:not_a_number"]})
    assert is_valid_dalton_notify_target("feishu:user:ou_abc_1") is True
    assert is_valid_dalton_notify_target("discord:user:123456") is True
    assert is_valid_dalton_notify_target("feishu:chat:oc_123") is False


@pytest.mark.asyncio
async def test_per_target_bridge_requests_and_second_claim_failure_marks_first_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    - Each target is dispatched via its own single-target bridge request (`[target]`).
    - HTTP timeout is derived from `FEISHU_SEND_CARD_TIMEOUT_S + 5s` (when set to 60, HTTP timeout is 65).
    - When claiming the 2nd target raises an exception, the 1st already-claimed target is marked `failed` (never left in `pending`) and can be retried.
    """
    monkeypatch.delenv("FEISHU_SEND_CARD_TIMEOUT_S", raising=False)
    assert FEISHU_SEND_CARD_TIMEOUT_S == 15.0
    assert get_feishu_send_card_timeout_s() == 15.0
    assert get_notify_http_timeout_s() == 20.0

    captured_http_timeouts: list[Any] = []
    fake_http_resp = create_autospec(httpx.Response, instance=True)
    fake_http_resp.is_success = True
    fake_http_resp.status_code = 200
    fake_http_resp.content = b'{"ok":true,"results":[{"target":"feishu:user:ou_target_1","ok":true}]}'
    fake_http_resp.json.return_value = {
        "ok": True,
        "results": [{"target": "feishu:user:ou_target_1", "ok": True}],
    }
    fake_http_client = create_autospec(httpx.AsyncClient, instance=True)

    async def _client_aenter() -> Any:
        return fake_http_client

    async def _client_aexit(*args: object) -> None:
        _ = args

    fake_http_client.__aenter__.side_effect = _client_aenter
    fake_http_client.__aexit__.side_effect = _client_aexit
    fake_http_client.post.return_value = fake_http_resp

    def _make_async_client(*args: object, **kwargs: object) -> Any:
        _ = args
        captured_http_timeouts.append(kwargs.get("timeout"))
        return fake_http_client

    async_client_spy = create_autospec(
        httpx.AsyncClient, side_effect=_make_async_client
    )

    monkeypatch.setenv("FEISHU_SEND_CARD_TIMEOUT_S", "60")
    with monkeypatch.context() as m_http:
        m_http.setattr(dispatch_mod.httpx, "AsyncClient", async_client_spy)
        await post_bridge_notify(["feishu:user:ou_target_1"], "ping")
    assert captured_http_timeouts == [65.0]
    assert rb.BridgeConfig().feishu_send_card_timeout_s == 60.0
    monkeypatch.delenv("FEISHU_SEND_CARD_TIMEOUT_S", raising=False)

    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(
        resolve_dalton_notify_targets,
        return_value=["feishu:user:ou_target_1", "discord:user:222222"],
    )
    verdict_fn = create_autospec(
        get_verdict_for_execution, return_value={"word": "THESIS_STRENGTHENED"}
    )
    url_fn = create_autospec(get_dalton_frontend_url, return_value="https://dalton.example.com")

    ledger: dict[tuple[str, str, str], str] = {}
    fail_second_claim = True

    async def _fake_claim_target(
        execution_id: str,
        channel: str,
        target: str,
        *,
        user_id: str = "",
        automation_id: str | None = None,
        payload: dict | None = None,
        stale_pending_minutes: int = 10,
    ) -> bool:
        _ = (user_id, automation_id, payload, stale_pending_minutes)
        if fail_second_claim and target == "discord:user:222222":
            raise RuntimeError("db error on second target claim")
        key = (execution_id, channel, target)
        state = ledger.get(key)
        if state in ("pending", "sent"):
            return False
        ledger[key] = "pending"
        return True

    async def _fake_mark_target(
        execution_id: str,
        channel: str,
        target: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: str | None = None,
    ) -> None:
        _ = (error_code, reason)
        key = (execution_id, channel, target)
        if expected_status is not None and ledger.get(key) != expected_status:
            return
        ledger[key] = status

    claim_fn = create_autospec(claim_target_delivery, side_effect=_fake_claim_target)
    mark_fn = create_autospec(mark_target_delivery, side_effect=_fake_mark_target)

    post_calls: list[list[str]] = []

    async def _fake_post_notify(
        targets: list[str],
        text: str,
        card: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = (text, card)
        post_calls.append(list(targets))
        return {
            "ok": True,
            "results": [{"target": t, "ok": True} for t in targets],
        }

    post_fn = create_autospec(post_bridge_notify, side_effect=_fake_post_notify)

    # First run: target 1 is claimed as 'pending', target 2 claim raises -> target 1 MUST become 'failed'
    res_crash = await on_automation_settled(
        automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-1"},
        execution_id="exec-claim-err",
        thread_id="th-claim-err",
        final_text="第一句完整结论。第二句核心数据。第三句后续展望。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert res_crash is False
    assert ledger[("exec-claim-err", "feishu", "feishu:user:ou_target_1")] == "failed"
    assert ("exec-claim-err", "discord", "discord:user:222222") not in ledger
    assert post_calls == []

    # Second run (retry): both claims succeed and each target is sent in its own bridge request
    fail_second_claim = False
    res_retry = await on_automation_settled(
        automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-1"},
        execution_id="exec-claim-err",
        thread_id="th-claim-err",
        final_text="第一句完整结论。第二句核心数据。第三句后续展望。",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        get_verdict_fn=verdict_fn,
        claim_target_fn=claim_fn,
        mark_target_fn=mark_fn,
        post_notify_fn=post_fn,
        frontend_url_fn=url_fn,
    )
    assert res_retry is True
    assert post_calls == [["feishu:user:ou_target_1"], ["discord:user:222222"]]
    assert ledger[("exec-claim-err", "feishu", "feishu:user:ou_target_1")] == "sent"
    assert ledger[("exec-claim-err", "discord", "discord:user:222222")] == "sent"

    # Third run: outer exception (2nd claim fails) AND cancelled while writing 'failed' ->
    # _shielded_mark_failed still marks target 1 'failed' and re-raises CancelledError
    fail_second_claim = True
    mark_started = asyncio.Event()
    never_mark: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    mark_call_idx = {"n": 0}

    async def _cancellable_mark_target(
        execution_id: str,
        channel: str,
        target: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: str | None = None,
    ) -> None:
        mark_call_idx["n"] += 1
        if mark_call_idx["n"] == 1:
            mark_started.set()
            await never_mark
        await _fake_mark_target(
            execution_id,
            channel,
            target,
            status=status,
            error_code=error_code,
            reason=reason,
            expected_status=expected_status,
        )

    cancellable_mark_fn = create_autospec(
        mark_target_delivery, side_effect=_cancellable_mark_target
    )
    settled_task = asyncio.create_task(
        on_automation_settled(
            automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-1"},
            execution_id="exec-claim-err-cancel",
            thread_id="th-claim-err",
            final_text="第一句完整结论。第二句核心数据。第三句后续展望。",
            resolve_enabled_fn=enabled_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            get_verdict_fn=verdict_fn,
            claim_target_fn=claim_fn,
            mark_target_fn=cancellable_mark_fn,
            post_notify_fn=post_fn,
            frontend_url_fn=url_fn,
        )
    )
    await mark_started.wait()
    settled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settled_task
    assert (
        ledger[("exec-claim-err-cancel", "feishu", "feishu:user:ou_target_1")]
        == "failed"
    )


@pytest.mark.asyncio
async def test_retry_failed_notify_deliveries_rechecks_toggles_targets_and_scans_stale_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directly call ``retry_failed_notify_deliveries`` (the real function) and verify:
    1. Master toggle off -> marks row ``skipped`` with ``reason="toggle_off"``, does not send.
    2. Target removed from user config -> marks row ``skipped`` with ``reason="target_removed"``, does not send.
    3. Both ``failed`` and stale ``pending`` (>10m) rows are queried in SQL, single-row DB claim failure
       skips only that row without aborting the batch, and valid rows are retried and marked ``sent``.
    """
    executed_queries: list[str] = []
    executed_params: list[Any] = []
    candidate_rows: list[dict[str, Any]] = []
    fail_claim_for_exec_ids: set[str] = set()
    db_status_by_exec: dict[str, str] = {}
    last_mode = {"val": "", "exec_id": "", "claimed": False}

    fake_cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cursor.rowcount = 1

    async def _cursor_aenter() -> Any:
        return fake_cursor

    async def _cursor_aexit(*args: object) -> None:
        _ = args

    async def _cursor_execute(sql: Any, params: Any = None, **kwargs: object) -> Any:
        _ = kwargs
        sql_str = str(sql)
        executed_queries.append(sql_str)
        executed_params.append(params)
        if "SELECT" in sql_str and "FROM dalton_notify_deliveries" in sql_str:
            last_mode["val"] = "select"
            last_mode["exec_id"] = ""
            last_mode["claimed"] = False
            for r in candidate_rows:
                eid = str(r.get("execution_id") or "")
                if eid and eid not in db_status_by_exec:
                    db_status_by_exec[eid] = str(r.get("status") or "failed")
        elif "UPDATE dalton_notify_deliveries" in sql_str:
            last_mode["val"] = "update"
            if "error_code = 'attempts_exhausted'" in sql_str:
                assert "status = 'pending'" in sql_str
                assert "attempt_count >= %s" in sql_str
                assert (
                    "updated_at <= NOW() - make_interval(mins => %s)"
                    in sql_str
                )
                exec_id_param = str(params[0]) if params else ""
                last_mode["exec_id"] = exec_id_param
                cur_db_status = db_status_by_exec.get(exec_id_param, "pending")
                if cur_db_status == "pending":
                    fake_cursor.rowcount = 1
                    db_status_by_exec[exec_id_param] = "failed"
                else:
                    fake_cursor.rowcount = 0
            elif "RETURNING attempt_count" in sql_str:
                assert "status = 'failed'" in sql_str
                assert "status = 'pending'" in sql_str
                assert (
                    "updated_at <= NOW() - make_interval(mins => %s)"
                    in sql_str
                )
                assert "attempt_count < %s" in sql_str
                exec_id_param = str(params[0]) if params else ""
                last_mode["exec_id"] = exec_id_param
                if exec_id_param in fail_claim_for_exec_ids:
                    raise RuntimeError("simulated single-row DB update error")
                cur_db_status = db_status_by_exec.get(exec_id_param, "failed")
                if cur_db_status in ("failed", "pending"):
                    fake_cursor.rowcount = 1
                    last_mode["claimed"] = True
                    db_status_by_exec[exec_id_param] = "pending"
                else:
                    fake_cursor.rowcount = 0
                    last_mode["claimed"] = False
            elif "SET status = %s" in sql_str:
                assert (
                    "AND status = %s" in sql_str
                    or "AND status IN (" in sql_str
                ), f"UPDATE missing WHERE status guard: {sql_str}"
                new_status = str(params[0])
                exec_id_param = str(params[2])
                last_mode["exec_id"] = exec_id_param
                allowed_statuses = {str(s) for s in params[5:]}
                assert allowed_statuses, (
                    f"UPDATE missing bound status params: {params}"
                )
                cur_db_status = db_status_by_exec.get(exec_id_param, "failed")
                if cur_db_status in allowed_statuses:
                    fake_cursor.rowcount = 1
                    db_status_by_exec[exec_id_param] = new_status
                else:
                    fake_cursor.rowcount = 0

    async def _cursor_fetchall() -> list[dict[str, Any]]:
        if last_mode["val"] == "select":
            return list(candidate_rows)
        return []

    async def _cursor_fetchone() -> dict[str, Any] | None:
        if last_mode["val"] == "update":
            if not last_mode["claimed"]:
                return None
            return {"attempt_count": 2}
        return None

    fake_cursor.__aenter__.side_effect = _cursor_aenter
    fake_cursor.__aexit__.side_effect = _cursor_aexit
    fake_cursor.execute.side_effect = _cursor_execute
    fake_cursor.fetchall.side_effect = _cursor_fetchall
    fake_cursor.fetchone.side_effect = _cursor_fetchone

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cursor

    @asynccontextmanager
    async def _fake_get_db_connection(conn: Any = None):
        _ = conn
        yield fake_conn

    get_db_conn_spy = create_autospec(
        pool_mod.get_db_connection, side_effect=_fake_get_db_connection
    )
    monkeypatch.setattr(dispatch_mod, "get_db_connection", get_db_conn_spy)

    recorded_marks: list[tuple[str, str, str, str, str | None]] = []
    recorded_expected_statuses: list[Any] = []

    async def _fake_mark(
        execution_id: str,
        channel: str,
        target: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: Any = None,
    ) -> bool:
        if reason != "attempts_exhausted":
            updated = await mark_target_delivery(
                execution_id,
                channel,
                target,
                status=status,
                error_code=error_code,
                reason=reason,
                expected_status=expected_status,
            )
            if not updated:
                return False
        recorded_marks.append(
            (execution_id, channel, target, status, reason or error_code)
        )
        recorded_expected_statuses.append(expected_status)
        return True

    mark_fn = create_autospec(mark_target_delivery, side_effect=_fake_mark)
    post_fn = create_autospec(
        post_bridge_notify,
        return_value={
            "ok": True,
            "results": [{"target": "feishu:user:ou_keep", "ok": True}],
        },
    )

    # Case 1: Master switch turned OFF -> row marked skipped(reason='toggle_off'), post_fn not called
    candidate_rows = [
        {
            "execution_id": "exec-toggle-off",
            "automation_id": "auto-1",
            "user_id": "u-off",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]
    enabled_off_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(
        resolve_dalton_notify_targets, return_value=["feishu:user:ou_keep"]
    )

    sent_1 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_off_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_1 == 0
    post_fn.assert_not_called()
    assert recorded_marks == [
        ("exec-toggle-off", "feishu", "feishu:user:ou_keep", "skipped", "toggle_off")
    ]
    # Verify SQL scans both 'failed' and stale 'pending' rows (>10 minutes)
    assert any(
        "d.status = 'failed'" in q and "d.status = 'pending'" in q
        for q in executed_queries
    )

    # Case 2: Target removed from user config -> row marked skipped(reason='target_removed'), post_fn not called
    recorded_marks.clear()
    candidate_rows = [
        {
            "execution_id": "exec-target-removed",
            "automation_id": "auto-1",
            "user_id": "u-removed",
            "channel": "feishu",
            "target": "feishu:user:ou_old_removed",
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]
    enabled_on_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    sent_2 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_on_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_2 == 0
    post_fn.assert_not_called()
    assert recorded_marks == [
        (
            "exec-target-removed",
            "feishu",
            "feishu:user:ou_old_removed",
            "skipped",
            "target_removed",
        )
    ]

    # Case 3: Batch with row 1 failing DB claim and row 2 (stale pending >10m) succeeding -> batch continues!
    recorded_marks.clear()
    fail_claim_for_exec_ids.add("exec-claim-db-err")
    candidate_rows = [
        {
            "execution_id": "exec-claim-db-err",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        },
        {
            "execution_id": "exec-stale-pending-ok",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        },
    ]
    sent_3 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_on_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_3 == 1
    post_fn.assert_called_once()
    assert recorded_marks == [
        ("exec-stale-pending-ok", "feishu", "feishu:user:ou_keep", "sent", None)
    ]

    # Case 4: Stale pending row that already reached attempt_count >= 3 -> marked failed(reason='attempts_exhausted')
    recorded_marks.clear()
    post_fn.reset_mock()
    candidate_rows = [
        {
            "execution_id": "exec-stale-pending-exhausted",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "pending",
            "attempt_count": 3,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]
    sent_4 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_on_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_4 == 0
    post_fn.assert_not_called()
    assert recorded_marks == [
        (
            "exec-stale-pending-exhausted",
            "feishu",
            "feishu:user:ou_keep",
            "failed",
            "attempts_exhausted",
        )
    ]

    # Case 5: Event-controlled cancellation after claiming row -> row is marked 'failed' (never left 'pending')
    recorded_marks.clear()
    recorded_expected_statuses.clear()
    candidate_rows = [
        {
            "execution_id": "exec-cancelled-after-claim",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "failed",
            "attempt_count": 1,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]

    post_started = asyncio.Event()
    never_done: asyncio.Future[dict[str, Any]] = (
        asyncio.get_running_loop().create_future()
    )

    async def _blocking_post_notify(
        targets: list[str],
        text: str,
        card: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = (targets, text, card)
        post_started.set()
        return await never_done

    blocking_post_fn = create_autospec(
        post_bridge_notify, side_effect=_blocking_post_notify
    )
    retry_task = asyncio.create_task(
        retry_failed_notify_deliveries(
            post_notify_fn=blocking_post_fn,
            mark_delivery_fn=mark_fn,
            resolve_enabled_fn=enabled_on_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            limit=5,
            max_duration_s=30.0,
        )
    )
    await post_started.wait()
    retry_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retry_task
    assert recorded_marks == [
        (
            "exec-cancelled-after-claim",
            "feishu",
            "feishu:user:ou_keep",
            "failed",
            "CancelledError",
        )
    ]
    assert recorded_expected_statuses == ["pending"]

    # Case 6: Send raises normal exception, and while writing 'failed' in except Exception,
    # outer cancellation arrives -> _shielded_mark_failed still marks row 'failed' and re-raises CancelledError
    recorded_marks.clear()
    recorded_expected_statuses.clear()
    candidate_rows = [
        {
            "execution_id": "exec-cancel-during-err-mark",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "failed",
            "attempt_count": 1,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        }
    ]
    err_post_fn = create_autospec(
        post_bridge_notify, side_effect=RuntimeError("bridge network boom")
    )
    mark_entered = asyncio.Event()
    never_mark_done: asyncio.Future[None] = (
        asyncio.get_running_loop().create_future()
    )
    mark_attempts = {"count": 0}

    async def _cancel_on_first_mark(
        execution_id: str,
        channel: str,
        target: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: str | None = None,
    ) -> None:
        mark_attempts["count"] += 1
        if mark_attempts["count"] == 1:
            mark_entered.set()
            await never_mark_done
        recorded_marks.append(
            (execution_id, channel, target, status, reason or error_code)
        )
        recorded_expected_statuses.append(expected_status)

    cancellable_mark_fn = create_autospec(
        mark_target_delivery, side_effect=_cancel_on_first_mark
    )
    err_cancel_task = asyncio.create_task(
        retry_failed_notify_deliveries(
            post_notify_fn=err_post_fn,
            mark_delivery_fn=cancellable_mark_fn,
            resolve_enabled_fn=enabled_on_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            limit=5,
            max_duration_s=30.0,
        )
    )
    await mark_entered.wait()
    err_cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await err_cancel_task
    assert recorded_marks == [
        (
            "exec-cancel-during-err-mark",
            "feishu",
            "feishu:user:ou_keep",
            "failed",
            "RuntimeError",
        )
    ]
    assert recorded_expected_statuses == ["pending"]

    # Case 7: Concurrency regression — Worker A already marked the row 'sent', while Worker B's
    # scan snapshot still shows old 'pending' (both attempt_count < 3 and attempt_count >= 3).
    # Worker B must NOT overwrite 'sent' or modify the row.
    recorded_marks.clear()
    recorded_expected_statuses.clear()
    executed_queries.clear()
    executed_params.clear()
    post_fn.reset_mock()
    db_status_by_exec["exec-concurrent-sent-1"] = "sent"
    db_status_by_exec["exec-concurrent-sent-exhausted"] = "sent"
    candidate_rows = [
        {
            "execution_id": "exec-concurrent-sent-1",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "pending",
            "attempt_count": 1,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        },
        {
            "execution_id": "exec-concurrent-sent-exhausted",
            "automation_id": "auto-1",
            "user_id": "u-1",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "pending",
            "attempt_count": 3,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        },
    ]
    sent_7 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_on_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_7 == 0
    post_fn.assert_not_called()
    assert recorded_marks == []
    assert db_status_by_exec["exec-concurrent-sent-1"] == "sent"
    assert db_status_by_exec["exec-concurrent-sent-exhausted"] == "sent"
    exhausted_updates = [
        q for q in executed_queries if "error_code = 'attempts_exhausted'" in q
    ]
    assert len(exhausted_updates) == 1
    assert "status = 'pending'" in exhausted_updates[0]
    assert "attempt_count >= %s" in exhausted_updates[0]
    assert (
        "updated_at <= NOW() - make_interval(mins => %s)"
        in exhausted_updates[0]
    )

    # Case 8: Concurrency regression on skip branch — Worker A already marked the row 'sent',
    # while Worker B holds an old 'pending' snapshot and reads master toggle OFF.
    # Worker B's skipped UPDATE includes 'AND status IN (%s, %s)' with ('failed', 'pending')
    # and does NOT modify the 'sent' row.
    recorded_marks.clear()
    recorded_expected_statuses.clear()
    executed_queries.clear()
    executed_params.clear()
    post_fn.reset_mock()
    db_status_by_exec["exec-concurrent-sent-toggle-off"] = "sent"
    candidate_rows = [
        {
            "execution_id": "exec-concurrent-sent-toggle-off",
            "automation_id": "auto-1",
            "user_id": "u-off",
            "channel": "feishu",
            "target": "feishu:user:ou_keep",
            "status": "pending",
            "attempt_count": 1,
            "payload_json": {
                "text": "GOOGL · 财报 T0 快讯",
                "automation_name": "dalton-googl-earn-2026q1-t0",
            },
            "automation_name": "dalton-googl-earn-2026q1-t0",
            "automation_metadata": {"ticker": "GOOGL"},
        },
    ]
    sent_8 = await retry_failed_notify_deliveries(
        post_notify_fn=post_fn,
        mark_delivery_fn=mark_fn,
        resolve_enabled_fn=enabled_off_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        limit=5,
        max_duration_s=30.0,
    )
    assert sent_8 == 0
    post_fn.assert_not_called()
    assert recorded_marks == []
    assert db_status_by_exec["exec-concurrent-sent-toggle-off"] == "sent"
    skip_updates = [
        (q, p)
        for q, p in zip(executed_queries, executed_params, strict=False)
        if "UPDATE dalton_notify_deliveries" in q and "SET status = %s" in q
    ]
    assert len(skip_updates) == 1
    assert "AND status IN (%s, %s)" in skip_updates[0][0]
    assert tuple(skip_updates[0][1][-2:]) == ("failed", "pending")

    # Verify mark_target_delivery with expected_status='pending' includes 'AND status = %s'
    executed_queries.clear()
    executed_params.clear()
    updated_sent_row = await mark_target_delivery(
        "exec-concurrent-sent-1",
        "feishu",
        "feishu:user:ou_keep",
        status="failed",
        error_code="CancelledError",
        expected_status="pending",
    )
    assert updated_sent_row is False
    assert len(executed_queries) == 1
    assert "AND status = %s" in executed_queries[0]
    assert executed_params[0][-1] == "pending"


def test_format_notify_message_and_frontend_url_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setenv("SERVER_BASE_URL", "http://localhost:8000")
    assert get_dalton_frontend_url() == ""

    monkeypatch.setenv("PUBLIC_APP_URL", "http://localhost:8000/")
    assert get_dalton_frontend_url() == ""

    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com/")
    assert get_dalton_frontend_url() == "https://dalton.example.com"

    classification = classify_automation_for_notify(
        {"name": "dalton-googl-earn-2026q1-t0", "metadata": {"ticker": "GOOGL"}}
    )
    assert classification is not None

    long_reply = (
        "谷歌 Q1 营收同比增长 15.2%，超出市场一致预期。"
        "云业务营业利润率提升至 18.4%，AI 算力需求维持高景气。"
        "搜索广告业务受 AI Overviews 带动实现稳健增长。"
        "管理层上调全年资本开支指引并宣布新一轮回购计划。"
        "这是一段超出字数限制或在结尾处没有标点符号的半句话内容将被完整截断在上一句句号处"
    )
    summary = extract_summary_lines(long_reply, max_chars=120)
    assert 3 <= len(summary) <= 6
    for line in summary:
        assert line.endswith("。")
        assert "半句话内容" not in line

    msg_with_link = format_notify_message(
        classification,
        verdict_word="THESIS_STRENGTHENED",
        final_text=long_reply,
        thread_id="thread-789",
        frontend_base_url="https://dalton.example.com/",
        max_summary_chars=120,
    )
    lines_with_link = msg_with_link.splitlines()
    assert lines_with_link[0] == "GOOGL · 财报 T0 快讯"
    assert "THESIS_STRENGTHENED" in lines_with_link[1]
    assert lines_with_link[-1] == "https://dalton.example.com/chat/t/thread-789"

    msg_without_link = format_notify_message(
        classification,
        verdict_word="THESIS_STRENGTHENED",
        final_text=long_reply,
        thread_id="thread-789",
        frontend_base_url="",
        max_summary_chars=120,
    )
    lines_without_link = msg_without_link.splitlines()
    assert not any("/chat/t/" in ln for ln in lines_without_link)


def test_feishu_card_schema_2_0_real_sentences_truncation_and_behaviors_button() -> None:
    """Verify:
    - Verdict template color mapping.
    - Structured `##` sections are each truncated to <=320 chars and total body <=3000 chars.
    - Unstructured text NEVER fabricates fallback sentences or sources; missing sections are omitted;
      empty text produces only header + button.
    - Button uses Card 2.0 `behaviors: [{"type": "open_url", "default_url": ...}]` without top-level `url`.
    """
    assert map_verdict_to_header_template("THESIS_STRENGTHENED") == "green"
    assert map_verdict_to_header_template("THESIS_WEAKENED") == "red"
    assert map_verdict_to_header_template("THESIS_BROKEN") == "red"
    assert map_verdict_to_header_template("NO_CHANGE") == "blue"
    assert map_verdict_to_header_template("NEW_THESIS") == "purple"
    assert map_verdict_to_header_template(None) == "indigo"
    assert map_verdict_to_header_template("") == "indigo"

    c_earn = classify_automation_for_notify(
        {"name": "dalton-googl-earn-2026q1-t0", "metadata": {"ticker": "GOOGL"}}
    )
    assert c_earn is not None

    long_sec = "这是一句超过限制的详细财务分析结论。" * 30  # > 500 chars
    structured_md = (
        f"## 结论\n{long_sec}\n\n"
        f"## 要点\n- {long_sec}\n\n"
        f"## 下一步验证\n{long_sec}\n\n"
        "## 来源\nAlphabet 2026Q1 10-Q 原始披露。"
    )

    sections = extract_card_sections(structured_md, classification=c_earn)
    assert [s[0] for s in sections] == ["结论", "要点", "下一步验证", "来源"]
    for _, sec_body in sections:
        assert len(sec_body) <= 320
    assert sum(len(s[1]) for s in sections) <= 3000
    assert sections[-1][1] == "Alphabet 2026Q1 10-Q 原始披露。"

    card_with_btn = build_feishu_notify_card(
        c_earn,
        verdict_word="THESIS_STRENGTHENED",
        final_text=structured_md,
        thread_id="th-card-1",
        frontend_base_url="https://dalton.example.com/",
        timestamp_str="2026-10-24 14:30",
    )
    assert card_with_btn["schema"] == "2.0"
    elements = card_with_btn["body"]["elements"]
    btn_el = elements[-1]
    assert "url" not in btn_el
    assert btn_el == {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "在 Dalton 中打开"},
        "type": "primary",
        "behaviors": [
            {
                "type": "open_url",
                "default_url": "https://dalton.example.com/chat/t/th-card-1",
            }
        ],
    }

    # Unstructured text with only 1 sentence: only "结论" appears; no fabricated "下一步验证" or "来源"
    one_sent_sections = extract_card_sections(
        "本季核心广告营收增速超市场一致预期。",
        classification=c_earn,
    )
    assert one_sent_sections == [("结论", "本季核心广告营收增速超市场一致预期。")]
    assert "跟踪" not in str(one_sent_sections)
    assert "Dalton 自动任务" not in str(one_sent_sections)

    # Unstructured text with 2 sentences: only "结论" and "要点" appear; no fabricated "下一步验证" or "来源"
    two_sent_sections = extract_card_sections(
        "本季核心广告营收增速超市场一致预期。云业务营业利润率创历史新高。",
        classification=c_earn,
    )
    assert [s[0] for s in two_sent_sections] == ["结论", "要点"]

    # Empty text with frontend URL -> card has ONLY header and button (no markdown body elements)
    card_only_header_and_btn = build_feishu_notify_card(
        c_earn,
        verdict_word="THESIS_STRENGTHENED",
        final_text="",
        thread_id="th-empty-body",
        frontend_base_url="https://dalton.example.com",
        timestamp_str="2026-10-24 18:00",
    )
    assert card_only_header_and_btn["body"]["elements"] == [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "在 Dalton 中打开"},
            "type": "primary",
            "behaviors": [
                {
                    "type": "open_url",
                    "default_url": "https://dalton.example.com/chat/t/th-empty-body",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_bridge_feishu_card_and_discord_log_hygiene_never_logs_body_or_exception_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    secret_token = bridge.cfg.openclaw_gateway_token
    subprocess_secret = "feishu-app-secret-never-log-me-999"
    sensitive_message_body = "机密研报正文：GOOGL 核心客户未公开谈判细节绝不写进日志。"
    sensitive_exception_text = "openclaw-internal-exception-detail-secret-555"

    c_earn = classify_automation_for_notify({"name": "dalton-googl-earn-2026q1-t0"})
    assert c_earn is not None
    card = build_feishu_notify_card(
        c_earn,
        verdict_word="THESIS_STRENGTHENED",
        final_text=sensitive_message_body,
        thread_id="th-1",
        frontend_base_url="https://dalton.example.com",
    )

    def _fake_subprocess_run(
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float = 15.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        _ = (capture_output, text, timeout, check)
        chat_id_idx = cmd.index("--chat-id")
        open_id = cmd[chat_id_idx + 1]
        if open_id == "ou_fail_card":
            return subprocess.CompletedProcess(
                cmd,
                returncode=1,
                stdout=f"stdout leak {subprocess_secret}",
                stderr=f"stderr leak {subprocess_secret}",
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=f'{{"status": "ok", "secret": "{subprocess_secret}"}}',
            stderr="",
        )

    run_spy = create_autospec(subprocess.run, side_effect=_fake_subprocess_run)
    monkeypatch.setattr(rb.subprocess, "run", run_spy)

    def _fake_openclaw_post(
        *,
        gateway_url: str,
        gateway_token: str,
        payload: dict,
        timeout_s: float = 15.0,
    ) -> dict:
        _ = (gateway_url, gateway_token, timeout_s)
        target_val = payload["args"]["target"]
        if target_val == "user:999999":
            raise RuntimeError(f"gateway crash: {sensitive_exception_text}")
        if target_val == "user:888888":
            return {
                "ok": False,
                "error": f"gateway error payload: {sensitive_exception_text}",
            }
        return {"ok": True, "result": {"via": "direct"}}

    post_spy = create_autospec(rb._post_openclaw_tools_invoke, side_effect=_fake_openclaw_post)
    monkeypatch.setattr(rb, "_post_openclaw_tools_invoke", post_spy)

    with caplog.at_level(logging.INFO):
        code, resp = await bridge.handle_notify(
            {
                "targets": [
                    "feishu:user:ou_ok_card",
                    "feishu:user:ou_fail_card",
                    "discord:user:11223344",
                    "discord:user:888888",
                    "discord:user:999999",
                ],
                "text": sensitive_message_body,
                "card": card,
            }
        )

    assert code == 200
    assert resp["ok"] is False
    assert resp["results"] == [
        {"target": "feishu:user:ou_ok_card", "channel": "feishu", "ok": True},
        {
            "target": "feishu:user:ou_fail_card",
            "channel": "feishu",
            "ok": False,
            "error": "exit_code=1",
        },
        {"target": "discord:user:11223344", "channel": "discord", "ok": True},
        {
            "target": "discord:user:888888",
            "channel": "discord",
            "ok": False,
            "error": "openclaw_invoke_error",
        },
        {
            "target": "discord:user:999999",
            "channel": "discord",
            "ok": False,
            "error": "RuntimeError",
        },
    ]

    # Assert message body, subprocess stdout/stderr, gateway token, and raw exception strings NEVER appear in logs
    assert sensitive_message_body not in caplog.text
    assert sensitive_exception_text not in caplog.text
    assert subprocess_secret not in caplog.text
    assert secret_token not in caplog.text
    assert sensitive_exception_text not in str(resp)


def test_notify_preview_loads_real_result_excerpt_and_fails_without_sample(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify ``notify_preview.py``:
    - Opens and closes the backend pool via ``get_or_create_pool()`` + ``await pool.open()`` / ``await pool.close()``.
    - Queries ``result_excerpt`` (not ``excerpt``).
    - Exits non-zero (1) and prints the reason when the execution is missing and ``--sample`` is not passed.
    - Uses fixed sample only when ``--sample`` is explicitly passed, and always invokes ``feishu_send_card.py --dry-run``.
    """
    recorded_cmd: list[str] = []

    def _fake_run(
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float = 15.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        _ = (capture_output, text, timeout, check)
        recorded_cmd.clear()
        recorded_cmd.extend(cmd)
        idx = cmd.index("--card-json-file")
        raw = Path(cmd[idx + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=raw, stderr="")

    run_spy = create_autospec(subprocess.run, side_effect=_fake_run)
    monkeypatch.setattr(notify_preview.subprocess, "run", run_spy)

    fake_pool = create_autospec(AsyncConnectionPool, instance=True)
    executed_sql: list[str] = []
    db_row: dict[str, Any] | None = {
        "conversation_thread_id": "th-db-real",
        "result_excerpt": "## 结论\n真实数据库行里的研报摘要。\n\n## 来源\nSEC 10-Q。",
    }

    fake_cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _cursor_aenter() -> Any:
        return fake_cursor

    async def _cursor_aexit(*args: object) -> None:
        _ = args

    async def _cursor_execute(sql: Any, params: Any = None, **kwargs: object) -> Any:
        _ = (params, kwargs)
        executed_sql.append(str(sql))

    async def _cursor_fetchone() -> dict[str, Any] | None:
        return db_row

    fake_cursor.__aenter__.side_effect = _cursor_aenter
    fake_cursor.__aexit__.side_effect = _cursor_aexit
    fake_cursor.execute.side_effect = _cursor_execute
    fake_cursor.fetchone.side_effect = _cursor_fetchone

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cursor

    @asynccontextmanager
    async def _fake_get_db_conn(conn: Any = None):
        _ = conn
        yield fake_conn

    get_pool_spy = create_autospec(pool_mod.get_or_create_pool, return_value=fake_pool)
    get_db_conn_spy = create_autospec(
        pool_mod.get_db_connection, side_effect=_fake_get_db_conn
    )
    monkeypatch.setattr(pool_mod, "get_or_create_pool", get_pool_spy)
    monkeypatch.setattr(pool_mod, "get_db_connection", get_db_conn_spy)

    import src.dalton.database.verdicts as verdicts_mod

    verdict_spy = create_autospec(
        verdicts_mod.get_verdict_for_execution,
        return_value={"word": "THESIS_STRENGTHENED"},
    )
    monkeypatch.setattr(verdicts_mod, "get_verdict_for_execution", verdict_spy)

    # 1. Real DB execution record
    rc_ok = notify_preview.main(
        [
            "--automation",
            "dalton-googl-earn-2026q1-t0",
            "--execution",
            "exec-real-db-1",
            "--frontend-url",
            "https://dalton.example.com",
        ]
    )
    assert rc_ok == 0
    fake_pool.open.assert_awaited_once()
    fake_pool.close.assert_awaited_once()
    assert any("result_excerpt" in s for s in executed_sql)
    assert "--dry-run" in recorded_cmd
    out_ok = capsys.readouterr().out
    assert "真实数据库行里的研报摘要。" in out_ok

    # 2. Missing execution record without --sample -> returns non-zero (1) and prints reason to stderr
    db_row = None
    rc_missing = notify_preview.main(
        [
            "--automation",
            "dalton-googl-earn-2026q1-t0",
            "--execution",
            "exec-missing-999",
        ]
    )
    assert rc_missing != 0
    err_missing = capsys.readouterr().err
    assert "exec-missing-999" in err_missing

    # 3. Explicit --sample -> uses sample text without querying DB and invokes --dry-run
    rc_sample = notify_preview.main(
        [
            "--automation",
            "dalton-googl-earn-2026q1-t0",
            "--execution",
            "exec-sample-1",
            "--sample",
        ]
    )
    assert rc_sample == 0
    assert "--dry-run" in recorded_cmd


@pytest.mark.asyncio
async def test_master_switch_default_false_and_error_log_hygiene(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_get_prefs = create_autospec(
        model_prefs_mod.get_user_preferences,
        return_value={"model_preference": {}, "other_preference": {}},
    )
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", mock_get_prefs)

    assert await resolve_dalton_notify_enabled("u-default") is False

    enabled_off_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(
        resolve_dalton_notify_targets, return_value=["feishu:user:ou_analyst_a"]
    )
    claim_fn = create_autospec(claim_target_delivery, return_value=True)
    post_fn = create_autospec(post_bridge_notify, return_value={"ok": True, "results": []})

    pushed = await on_automation_settled(
        automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-default"},
        execution_id="exec-master-off",
        thread_id="th-master-off",
        final_text="谷歌 Q1 营收超预期。",
        resolve_enabled_fn=enabled_off_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        claim_target_fn=claim_fn,
        post_notify_fn=post_fn,
    )
    assert pushed is False
    overrides_fn.assert_not_called()
    targets_fn.assert_not_called()
    claim_fn.assert_not_called()
    post_fn.assert_not_called()

    sensitive_body = "bridge-secret-token-leak-body-12345"
    enabled_on_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    verdict_fn = create_autospec(get_verdict_for_execution, return_value={"word": "THESIS_STRENGTHENED"})
    mark_fn = create_autospec(mark_target_delivery, return_value=None)
    post_err_fn = create_autospec(
        post_bridge_notify,
        side_effect=RuntimeError(f"HTTP 500 body={sensitive_body}"),
    )
    with caplog.at_level(logging.ERROR):
        res_err = await on_automation_settled(
            automation={"name": "dalton-googl-earn-2026q1-t0", "user_id": "u-1"},
            execution_id="exec-err-log",
            thread_id="th-err",
            final_text="谷歌 Q1 营收超预期。",
            resolve_enabled_fn=enabled_on_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            get_verdict_fn=verdict_fn,
            claim_target_fn=claim_fn,
            mark_target_fn=mark_fn,
            post_notify_fn=post_err_fn,
        )
    assert res_err is False
    assert "RuntimeError" in caplog.text
    assert sensitive_body not in caplog.text


@pytest.mark.asyncio
async def test_per_task_overrides_and_shared_notify_state() -> None:
    for pushable_name in (
        "dalton-googl-earn-2026q1-t0",
        "dalton-nvda-earn-2026q1-tp1",
        "dalton-tsla-move-down",
        "dalton-msft-earn-2026q2-tm3",
        "dalton-director-weekly",
        "dalton-earnings-calendar",
    ):
        assert is_automation_notify_enabled({"name": pushable_name}, {}) is True
    for non_pushable_name in ("dalton-googl-watch", "dalton-googl-delta"):
        assert is_automation_notify_enabled({"name": non_pushable_name}, {}) is False

    overrides = {
        "dalton-googl-earn-2026q1-t0": False,
        "category:price_move": False,
        "dalton-tsla-move-up": True,
    }
    assert resolve_automation_notify_state(
        {"name": "DALTON-GOOGL-EARN-2026Q1-T0"}, overrides
    ) == ("earnings_t0", False, "task")
    assert resolve_automation_notify_state(
        {"name": "dalton-tsla-move-down"}, overrides
    ) == ("price_move", False, "category")
    assert resolve_automation_notify_state(
        {"name": "dalton-tsla-move-up"}, overrides
    ) == ("price_move", True, "task")

    now = datetime.now(UTC)
    base_fields = {
        "automation_id": uuid4(),
        "user_id": "u-1",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "America/New_York",
        "instruction": "Run check.",
        "thread_strategy": "new",
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    resp_pushable = AutomationResponse.model_validate(
        {**base_fields, "name": "dalton-googl-earn-2026q1-t0"}
    )
    assert resp_pushable.dalton_notify_category == "earnings_t0"
    assert resp_pushable.dalton_notify_effective is True
    assert resp_pushable.dalton_notify_source == "default"

    overrides_fn = create_autospec(
        resolve_dalton_notify_overrides,
        return_value={"dalton-googl-earn-2026q1-t0": False},
    )
    await populate_automation_notify_state(
        "u-1", [resp_pushable], resolve_overrides_fn=overrides_fn
    )
    assert resp_pushable.dalton_notify_effective is False
    assert resp_pushable.dalton_notify_source == "task"

    resp_non_pushable = AutomationResponse.model_validate(
        {**base_fields, "name": "dalton-googl-watch"}
    )
    assert resp_non_pushable.dalton_notify_category is None
    assert resp_non_pushable.dalton_notify_effective is None
    assert resp_non_pushable.dalton_notify_source is None
    assert callable(claim_execution_notify)
