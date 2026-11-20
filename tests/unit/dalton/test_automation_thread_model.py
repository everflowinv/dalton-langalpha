"""Unit tests for automation execution model inheritance on notification turns."""

from __future__ import annotations

import logging
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import psycopg
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from ptc_agent.config.agent import CredentialSource
from scripts.dalton.ops import automation_thread_models
from src.dalton.services import automation_thread_model
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.conversation import threads_write
from src.server.database.runs import command_runs as command_runs_db
from src.server.handlers.chat import request_prep
from src.server.models.chat import ChatMessage, ChatRequest, ThreadOrigin
from src.server.services.llm import thread_model
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app, pin_chat_limit

_THREADS_MOD = "src.server.app.threads.messaging"
_ATM_MOD = "src.dalton.services.automation_thread_model"
_USER = "usr-auto-1"
_WS = "ws-auto-1"
_THREAD = "11111111-1111-4111-8111-111111111111"
_EXEC_1 = "22222222-2222-4222-8222-222222222222"
_EXEC_2 = "55555555-5555-4555-8555-555555555555"
_AUTO_ID = "33333333-3333-4333-8333-333333333333"
_RUN_0 = "44444444-4444-4444-8444-444444444440"
_RUN_1 = "44444444-4444-4444-8444-444444444441"
_RUN_2 = "44444444-4444-4444-8444-444444444442"


@contextmanager
def _send_route_stubs(
    *,
    stored_thread_model: str | None = None,
    running_executions: list[dict] | None = None,
    source_exec_by_id: dict[str, dict] | None = None,
    source_exec_by_command: dict[str, dict] | None = None,
    source_exec_by_task: dict[str, dict] | None = None,
    source_exec_by_run: dict[str, dict] | None = None,
    first_turn_models: dict[str, str | None] | None = None,
    automation_cfg_model: str | None = None,
    model_available: bool = True,
    resolve_side_effect=None,
    exec_query_error: Exception | None = None,
    latest_retry_run: dict | None = None,
    latest_retry_query: dict | None = None,
    service_token: str = "svc-token",
    host_mode: str = "platform",
    dalton_auth_enabled: bool = False,
):
    """Stub ``POST /threads/{id}/messages`` and ``/retry`` with ``create_autospec`` doubles."""
    from src.server.app import setup as setup_module
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult

    app = create_test_app(router)
    pin_chat_limit(app, ChatAuthResult(user_id=_USER, access_tier=0))

    meta = {
        "user_id": _USER,
        "owner_id": _USER,
        "thread_user_id": _USER,
        "workspace_owner_id": _USER,
        "workspace_status": "running",
        "metadata": {},
        "is_shared": False,
        "msg_type": "ptc",
        "workspace_id": _WS,
        "llm_model": stored_thread_model,
        "reasoning_effort": None,
        "fast_mode": False,
    }
    resolved_cfg = SimpleNamespace(
        credential_source=CredentialSource.PLATFORM,
        llm_client=None,
        llm=SimpleNamespace(name="resolved"),
    )
    resolve_mock = AsyncMock(
        side_effect=resolve_side_effect,
        return_value=resolved_cfg,
    )
    wm_singleton = create_autospec(WorkspaceManager, instance=True)
    wm_singleton.has_ready_session.return_value = True

    astream_calls: list[dict] = []

    def _fake_astream(*_args, **kwargs):
        astream_calls.append(kwargs)

        async def _g():
            if kwargs.get("dispatched"):
                yield request_prep.DISPATCH_STARTED_MARKER

        return _g()

    if exec_query_error is not None:
        get_running_mock = create_autospec(
            exec_db.get_running_executions_for_thread,
            side_effect=exec_query_error,
        )
    else:
        get_running_mock = create_autospec(
            exec_db.get_running_executions_for_thread,
            return_value=running_executions or [],
        )

    exec_by_id = source_exec_by_id or {}
    exec_by_cmd = source_exec_by_command or {}
    exec_by_task = source_exec_by_task or {}
    exec_by_run = source_exec_by_run or {}
    turn_models_map = first_turn_models or {}

    async def _first_turn_lookup(execution, **_kwargs):
        return turn_models_map.get(str(execution["automation_execution_id"]))

    get_by_id_mock = create_autospec(
        automation_thread_model._get_execution_by_id,
        side_effect=lambda eid: exec_by_id.get(str(eid)),
    )
    get_by_run_mock = create_autospec(
        automation_thread_model._get_execution_for_run_id,
        side_effect=lambda rid: exec_by_run.get(str(rid)),
    )
    get_by_cmd_mock = create_autospec(
        automation_thread_model._get_execution_for_command,
        side_effect=lambda _tid, cid: exec_by_cmd.get(str(cid)),
    )
    get_by_task_mock = create_autospec(
        automation_thread_model._get_execution_for_task,
        side_effect=lambda _tid, *, task_run_id=None, task_id=None: exec_by_task.get(
            str(task_run_id or task_id or "")
        ),
    )
    first_turn_mock = create_autospec(
        automation_thread_model._get_execution_first_turn_llm_model,
        side_effect=_first_turn_lookup,
    )
    auto_row = (
        {"automation_id": _AUTO_ID, "user_id": _USER, "llm_model": automation_cfg_model}
        if automation_cfg_model is not None
        else None
    )
    get_auto_mock = create_autospec(
        auto_db.get_automation_including_deleted,
        return_value=auto_row,
    )
    remember_mock = create_autospec(
        threads_write.remember_thread_llm_model,
        return_value=True,
    )
    update_fields_mock = create_autospec(
        threads_write.update_thread_fields,
        return_value=None,
    )

    model_check = (
        (lambda _prefs, _name: True)
        if model_available
        else (lambda _prefs, name: name != "gpt-6.1-sol-oauth")
    )

    patches = [
        patch(
            f"{_THREADS_MOD}.get_thread_auth_meta",
            create_autospec(request_prep.qr_db.get_thread_auth_meta, return_value=meta),
        ),
        patch(
            f"{_THREADS_MOD}.get_thread_by_id",
            create_autospec(
                request_prep.qr_db.get_thread_by_id,
                return_value={"conversation_thread_id": _THREAD, "workspace_id": _WS},
            ),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            AsyncMock(
                return_value={"workspace_id": _WS, "user_id": _USER, "status": "running"}
            ),
        ),
        patch.object(setup_module, "agent_config", MagicMock()),
        patch("src.server.services.llm.config.resolve_llm_config", new=resolve_mock),
        patch(
            "src.server.services.llm.thread_model.user_models.get_model_preference",
            AsyncMock(return_value={}),
        ),
        patch(
            "src.server.services.llm.thread_model.model_resolves",
            create_autospec(thread_model.model_resolves, side_effect=model_check),
        ),
        patch(
            f"{_ATM_MOD}.user_models.get_model_preference",
            AsyncMock(return_value={}),
        ),
        patch(
            f"{_ATM_MOD}.model_resolves",
            create_autospec(thread_model.model_resolves, side_effect=model_check),
        ),
        patch(f"{_ATM_MOD}.exec_db.get_running_executions_for_thread", get_running_mock),
        patch(f"{_ATM_MOD}._get_execution_by_id", get_by_id_mock),
        patch(f"{_ATM_MOD}._get_execution_for_run_id", get_by_run_mock),
        patch(f"{_ATM_MOD}._get_execution_for_command", get_by_cmd_mock),
        patch(f"{_ATM_MOD}._get_execution_for_task", get_by_task_mock),
        patch(f"{_ATM_MOD}._get_execution_first_turn_llm_model", first_turn_mock),
        patch(f"{_ATM_MOD}.auto_db.get_automation_including_deleted", get_auto_mock),
        patch(
            "src.server.database.runs.command_runs.get_thread_command",
            create_autospec(
                command_runs_db.get_thread_command,
                return_value={"workspace_id": _WS},
            ),
        ),
        patch.object(threads_write, "remember_thread_llm_model", remember_mock),
        patch.object(threads_write, "update_thread_fields", update_fields_mock),
        patch("src.server.dependencies.usage_limits.enforce_credit_limit", AsyncMock()),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            return_value=wm_singleton,
        ),
        patch("src.server.handlers.chat.astream_ptc_workflow", side_effect=_fake_astream),
        patch(f"{_THREADS_MOD}.observe_chat_stream", side_effect=lambda gen, **_: gen),
        patch("src.server.dependencies.usage_limits.release_burst_slot", AsyncMock()),
        patch(f"{_THREADS_MOD}._assert_stream_transport_ready", AsyncMock()),
        patch(f"{_THREADS_MOD}._get_service_token", return_value=service_token),
        patch("src.config.settings.HOST_MODE", host_mode),
        patch(
            "src.dalton.auth.is_dalton_supabase_auth_enabled",
            return_value=dalton_auth_enabled,
        ),
        patch(
            "src.server.database.runs.lifecycle.get_latest_attempt",
            AsyncMock(return_value=latest_retry_run),
        ),
        patch(
            "src.server.handlers.checkpoint_handler.get_retry_checkpoint",
            AsyncMock(return_value="cp-1"),
        ),
        patch(
            "src.server.services.runs.pending_input.retried_wake_fields",
            AsyncMock(return_value={}),
        ),
        patch(
            "src.server.database.conversation.queries.get_query_at_turn",
            AsyncMock(return_value=latest_retry_query),
        ),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield SimpleNamespace(
            app=app,
            resolve=resolve_mock,
            astream_calls=astream_calls,
            get_running=get_running_mock,
            first_turn=first_turn_mock,
            get_auto=get_auto_mock,
            remember=remember_mock,
            update_fields=update_fields_mock,
        )


def _exec_row(exec_id: str = _EXEC_1, status: str = "running") -> dict:
    return {
        "automation_execution_id": exec_id,
        "automation_id": _AUTO_ID,
        "status": status,
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
        "workspace_id": _WS,
    }


@pytest.mark.asyncio
async def test_unservable_inherited_model_falls_back_to_default_and_succeeds(caplog):
    """When inherited model raises an _UNSERVABLE_REFUSALS 400, re-resolve with named=None."""
    caplog.set_level(logging.WARNING, logger=automation_thread_model.logger.name)
    calls: list[str | None] = []

    async def _resolve_side_effect(_cfg, _uid, model_name, *_args, **_kwargs):
        calls.append(model_name)
        if model_name == "gpt-6.1-sol-oauth":
            raise HTTPException(
                status_code=400,
                detail={"message": "Reconnect OAuth", "type": "oauth_reauth_required"},
            )
        return SimpleNamespace(
            credential_source=CredentialSource.PLATFORM,
            llm_client=None,
            llm=SimpleNamespace(name=model_name or "account-default"),
        )

    with _send_route_stubs(
        stored_thread_model="held-fallback-model",
        running_executions=[_exec_row(_EXEC_1)],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
        resolve_side_effect=_resolve_side_effect,
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert calls == ["gpt-6.1-sol-oauth", "held-fallback-model"]
    assert st.astream_calls[0]["named_model"] is None
    st.remember.assert_not_awaited()
    assert any(
        "oauth_reauth_required" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_running_execution_without_pending_background_work_still_inherits():
    """Deployment-interrupted running execution (no background tasks/commands) still inherits."""
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[_exec_row(_EXEC_1, status="running")],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [
                        {"role": "user", "content": "<system>Routine deployment paused turn.</system>"}
                    ],
                    "workspace_id": _WS,
                    "query_type": "system",
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] == "gpt-6.1-sol-oauth"
    assert st.astream_calls[0]["named_model"] is None

    # Once execution settled, a later wakeup does not inherit.
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
    ) as st2:
        async with AsyncClient(
            transport=ASGITransport(app=st2.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<wakeup>tick</wakeup>"}],
                    "workspace_id": _WS,
                    "query_type": "wakeup",
                    "wakeup": {"v": 1, "notices": []},
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert st2.resolve.await_args.args[2] is None


@pytest.mark.asyncio
async def test_explicit_source_execution_beats_latest_running_execution():
    """Requests carrying report_source, automation_delivery, or retry_of_run_id use that execution."""
    exec1 = _exec_row(_EXEC_1)
    exec2 = _exec_row(_EXEC_2)
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[exec1, exec2],
        source_exec_by_id={_EXEC_1: exec1},
        source_exec_by_command={"cmd-1": exec1},
        source_exec_by_task={_RUN_0: exec1},
        source_exec_by_run={_RUN_1: exec1},
        first_turn_models={
            _EXEC_1: "model-from-source-exec",
            _EXEC_2: "model-from-newer-exec",
        },
        latest_retry_run={
            "conversation_response_id": _RUN_1,
            "turn_index": 1,
            "status": "error",
            "metadata": {},
        },
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            headers = {
                "X-Service-Token": "svc-token",
                "X-User-Id": _USER,
                "X-Dispatch": "background",
            }
            # 1) command_report -> exec1
            r1 = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>cmd</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "report_source": {
                        "kind": "command_report",
                        "command_id": "cmd-1",
                        "tool": "Bash",
                        "status": "completed",
                    },
                },
                headers=headers,
            )
            assert r1.status_code == 200

            # 2) task report_source -> exec1
            r2 = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>task</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "report_source": {
                        "kind": "task",
                        "task_id": "t-1",
                        "display_id": "Task-t-1",
                        "task_run_id": _RUN_0,
                    },
                },
                headers=headers,
            )
            assert r2.status_code == 200

            # 3) handoff automation_delivery -> exec1 (id with .2 suffix)
            r3 = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>handoff</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "automation_delivery": {"id": f"{_EXEC_1}.2", "targets": []},
                },
                headers=headers,
            )
            assert r3.status_code == 200

            # 4) server retry of run belonging to exec1 -> exec1
            r4 = await client.post(
                f"/api/v1/threads/{_THREAD}/retry",
                json={"workspace_id": _WS},
                headers=headers,
            )
            assert r4.status_code == 200

            # 5) wakeup with no source -> falls back to latest running execution (exec2)
            r5 = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<wakeup>tick</wakeup>"}],
                    "workspace_id": _WS,
                    "query_type": "wakeup",
                    "wakeup": {"v": 1, "notices": []},
                },
                headers=headers,
            )
            assert r5.status_code == 200

    assert [call.args[2] for call in st.resolve.await_args_list] == [
        "model-from-source-exec",
        "model-from-source-exec",
        "model-from-source-exec",
        "model-from-source-exec",
        "model-from-newer-exec",
    ]
    assert all(call["named_model"] is None for call in st.astream_calls)


@pytest.mark.asyncio
async def test_first_turn_lookup_ignores_updated_conversation_response_id():
    """First-turn resolution uses metadata/provenance even after take_over_execution_run."""
    t0 = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    responses = [
        {
            "conversation_response_id": _RUN_0,
            "conversation_thread_id": _THREAD,
            "turn_index": 0,
            "attempt_no": 1,
            "metadata": {"automation_execution_id": _EXEC_1, "run_origin": "automation"},
        },
        {
            "conversation_response_id": _RUN_2,
            "conversation_thread_id": _THREAD,
            "turn_index": 1,
            "attempt_no": 1,
            "metadata": {"automation_execution_id": _EXEC_1, "run_origin": "report"},
        },
    ]
    queries = [
        {
            "conversation_thread_id": _THREAD,
            "turn_index": 0,
            "type": "initial",
            "metadata": {"llm_model": "gpt-6.1-sol-oauth", "origin": {"type": "automation"}},
            "created_at": t0,
        },
        {
            "conversation_thread_id": _THREAD,
            "turn_index": 1,
            "type": "system",
            "metadata": {"llm_model": "claude-opus-5-5-oauth"},
            "created_at": t1,
        },
    ]

    @asynccontextmanager
    async def _fake_conn():
        class _Cur:
            def __init__(self) -> None:
                self._row = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql: str, params=None) -> None:
                self._row = None
                norm = " ".join(sql.split())
                if "r.metadata->>'automation_execution_id' = %s" in norm:
                    tid, eid = params
                    matches = [
                        (r, q)
                        for r in responses
                        for q in queries
                        if r["conversation_thread_id"] == tid
                        and q["conversation_thread_id"] == tid
                        and q["turn_index"] == r["turn_index"]
                        and r["metadata"].get("automation_execution_id") == eid
                        and q["type"] not in ("system", "wakeup", "market_target")
                    ]
                    matches.sort(key=lambda pair: (pair[0]["turn_index"], pair[0]["attempt_no"]))
                    if matches:
                        self._row = {"llm_model": matches[0][1]["metadata"].get("llm_model")}
                elif "q.metadata->'origin'->>'type' = 'automation'" in norm:
                    tid, started = params
                    matches = [
                        q
                        for q in queries
                        if q["conversation_thread_id"] == tid
                        and q["created_at"] >= started
                        and (q["metadata"].get("origin") or {}).get("type") == "automation"
                    ]
                    matches.sort(key=lambda q: (q["turn_index"], q["created_at"]))
                    if matches:
                        self._row = {"llm_model": matches[0]["metadata"].get("llm_model")}

            async def fetchone(self):
                return self._row

        class _Conn:
            def cursor(self, *args, **kwargs):
                return _Cur()

        yield _Conn()

    # Execution's conversation_response_id was already overwritten to _RUN_2 (turn 1),
    # yet first-turn lookup resolves turn 0's gpt-6.1-sol-oauth.
    execution_taken_over = {
        "automation_execution_id": _EXEC_1,
        "conversation_thread_id": _THREAD,
        "conversation_response_id": _RUN_2,
        "started_at": t0,
    }
    with patch.object(automation_thread_model, "get_db_connection", side_effect=_fake_conn):
        model = await automation_thread_model._get_execution_first_turn_llm_model(
            execution_taken_over, fallback_thread_id=_THREAD
        )
    assert model == "gpt-6.1-sol-oauth"

    # Also verify provenance fallback when execution_id wasn't stamped in response metadata.
    responses[0]["metadata"] = {"run_origin": "automation"}
    with patch.object(automation_thread_model, "get_db_connection", side_effect=_fake_conn):
        model_via_provenance = await automation_thread_model._get_execution_first_turn_llm_model(
            execution_taken_over, fallback_thread_id=_THREAD
        )
    assert model_via_provenance == "gpt-6.1-sol-oauth"


@pytest.mark.asyncio
async def test_oss_no_token_user_retry_does_not_inherit():
    """In OSS mode without INTERNAL_SERVICE_TOKEN, a user /retry must not inherit."""
    # Even if the failed turn was a system notification turn and HOST_MODE=oss has no token,
    # a user clicking Retry (no X-Service-Token, no X-Dispatch) must not inherit.
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[_exec_row(_EXEC_1)],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
        service_token="",
        host_mode="oss",
        dalton_auth_enabled=False,
        latest_retry_run={
            "conversation_response_id": _RUN_1,
            "turn_index": 1,
            "status": "error",
            "metadata": {
                "pending_input": {"checkpoint_id": None, "replay": False, "resend": False, "context": False}
            },
        },
        latest_retry_query={
            "turn_index": 1,
            "content": "<system>done</system>",
            "type": "system",
            "metadata": {},
        },
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/retry",
                json={"workspace_id": _WS},
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] is None
    st.get_running.assert_not_awaited()

    # Direct check: from_service=True alone without is_server_retry=True cannot trigger retry branch.
    req = ChatRequest(workspace_id=_WS, messages=[], query_type="system")
    assert (
        await automation_thread_model.resolve_automation_notification_model(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=_RUN_1,
        )
        is None
    )


@pytest.mark.asyncio
async def test_notification_turn_falls_back_to_automation_config_when_first_turn_missing(
    caplog,
):
    """When first-turn metadata lacks llm_model, use the automation's configured llm_model."""
    caplog.set_level(logging.INFO, logger=automation_thread_model.logger.name)
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[_exec_row(_EXEC_1)],
        first_turn_models={_EXEC_1: None},
        automation_cfg_model="gpt-6.1-sol-oauth",
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] == "gpt-6.1-sol-oauth"
    assert st.astream_calls[0]["named_model"] is None
    st.remember.assert_not_awaited()
    assert any(
        "model=gpt-6.1-sol-oauth" in rec.message and "source=automation_config" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_request_with_explicit_llm_model_is_unchanged():
    """When the request already specifies llm_model, automation lookup is skipped."""
    with _send_route_stubs(
        stored_thread_model="held-thread-model",
        running_executions=[_exec_row(_EXEC_1)],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "llm_model": "explicit-model",
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] == "explicit-model"
    st.get_running.assert_not_awaited()
    st.first_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_turn_is_unaffected_by_running_automation_execution():
    """User-initiated turns (non-notification) must not inherit the automation model."""
    with _send_route_stubs(
        stored_thread_model=None,
        running_executions=[_exec_row(_EXEC_1)],
        first_turn_models={_EXEC_1: "gpt-6.1-sol-oauth"},
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "workspace_id": _WS,
                    "platform": "web",
                },
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] is None
    st.get_running.assert_not_awaited()
    st.remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_exception_logs_warning_and_falls_back_cleanly(caplog):
    """A DB error during execution lookup logs one warning and does not fail the turn."""
    caplog.set_level(logging.WARNING, logger=automation_thread_model.logger.name)
    with _send_route_stubs(
        stored_thread_model="held-thread-model",
        exec_query_error=RuntimeError("db timeout"),
    ) as st:
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )
            assert resp.status_code == 200

    assert st.resolve.await_args.args[2] == "held-thread-model"
    assert st.astream_calls[0]["named_model"] is None
    st.remember.assert_not_awaited()
    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and rec.name == automation_thread_model.logger.name
    ]
    assert len(warnings) == 1


def test_ops_script_restricts_to_in_execution_notification_turns_and_metadata_first_turn():
    """Ops script filters out post-completion and user turns and avoids conversation_response_id."""
    fake_cur = MagicMock()
    fake_cur.__enter__ = MagicMock(return_value=fake_cur)
    fake_cur.__exit__ = MagicMock(return_value=False)
    fake_cur.fetchone.return_value = {"skipped_count": 2}
    fake_cur.fetchall.return_value = [
        {
            "thread_id": _THREAD,
            "execution_id": _EXEC_1,
            "automation_id": _AUTO_ID,
            "automation_name": "AlphaEngine 机构会议增量整理入库",
            "automation_deleted": True,
            "automation_deleted_at": "2026-11-18T02:00:00Z",
            "execution_status": "completed",
            "execution_completed_at": "2026-11-18T01:20:00Z",
            "thread_strategy": "new",
            "automation_llm_model": "gpt-6.1-sol-oauth",
            "thread_llm_model": None,
            "first_turn_index": 0,
            "first_model": "gpt-6.1-sol-oauth",
            "first_turn_at": "2026-11-18T01:00:00Z",
            "later_turn_index": 1,
            "later_query_type": "system",
            "later_model": "claude-opus-5-5-oauth",
            "later_turn_at": "2026-11-18T01:10:00Z",
        }
    ]
    fake_conn = create_autospec(psycopg.Connection, instance=True)
    fake_conn.cursor.return_value = fake_cur

    result = automation_thread_models.fetch_model_audit(days=7, conn=fake_conn)
    assert result.skipped_missing_first_model == 2
    assert len(result.mismatches) == 1

    assert fake_cur.execute.call_args_list[0].args == (
        automation_thread_models.READ_ONLY_SQL,
    )
    skip_sql, skip_params = fake_cur.execute.call_args_list[1].args
    assert "e.conversation_response_id" not in skip_sql
    assert "ft.first_model IS NULL" in skip_sql
    assert skip_params == {"days": 7}

    mismatch_sql, mismatch_params = fake_cur.execute.call_args_list[2].args
    assert "e.conversation_response_id" not in mismatch_sql
    assert "r.metadata->>'automation_execution_id' = e.automation_execution_id::text" in mismatch_sql
    assert "q.metadata->'origin'->>'type' = 'automation'" in mismatch_sql
    assert "q.type IN ('system', 'wakeup', 'market_target')" in mismatch_sql
    assert "wt.later_turn_at <= eb.execution_completed_at" in mismatch_sql
    assert "a.deleted_at IS NOT NULL" in mismatch_sql
    assert mismatch_params == {"days": 7}

    report = automation_thread_models.format_report(result, days=7)
    assert "Skipped executions with missing first-turn llm_model: 2" in report
    assert "[SOFT-DELETED]" in report
    assert "gpt-6.1-sol-oauth" in report
    assert "claude-opus-5-5-oauth" in report


@pytest.mark.asyncio
async def test_automation_origin_non_notification_turn_is_rejected():
    """Automation-origin non-notification ChatRequest never enters notification inheritance."""
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="run")],
        llm_model=None,
        origin=ThreadOrigin(type="automation", id=_AUTO_ID),
    )
    assert (
        await automation_thread_model.resolve_automation_notification_model(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            from_service=True,
        )
        is None
    )


def _subagent_runs_columns() -> set[str]:
    """Columns of ``subagent_runs`` as the migrations define them."""
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[3] / "migrations" / "versions"
    cols: set[str] = set()
    for path in sorted(versions.glob("*.py")):
        text = path.read_text()
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS subagent_runs \((.*?)\n\s*\)", text, re.DOTALL
        )
        if m:
            for line in m.group(1).splitlines():
                word = line.strip().split(" ", 1)[0]
                if re.fullmatch(r"[a-z_]+", word) and word.upper() not in {
                    "PRIMARY",
                    "UNIQUE",
                    "CONSTRAINT",
                    "CHECK",
                    "FOREIGN",
                }:
                    cols.add(word)
        if "subagent_runs" in text:
            # Some migrations loop over several tables (``ALTER TABLE {table}``),
            # so take every added column in a file that names the table.
            cols.update(re.findall(r"ADD COLUMN IF NOT EXISTS ([a-z_]+)", text))
    return cols


def test_subagent_runs_query_uses_existing_columns():
    """Regression (2026-11-19): ``sr.created_at`` does not exist, so every
    report-back turn on an automation thread lost the automation's model."""
    import inspect
    import re

    cols = _subagent_runs_columns()
    assert {
        "task_run_id",
        "thread_id",
        "task_id",
        "parent_run_id",
        "started_at",
        "usage_settled_at",
        "user_id",
        "in_flight_credits",
    } <= cols
    assert "created_at" not in cols
    used = set(
        re.findall(r"\bsr\.([a-z_]+)", inspect.getsource(automation_thread_model))
    )
    assert used, "expected the subagent_runs query to alias the table as sr"
    assert used <= cols, f"unknown subagent_runs columns: {sorted(used - cols)}"
