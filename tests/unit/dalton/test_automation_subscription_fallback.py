"""Unit tests for automation subscription fallback, notice surfacing, and pre-START signal release."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from ptc_agent.config.agent import AgentConfig, CredentialSource, LLMConfig
from src.dalton import auth as dalton_auth
from src.dalton.services import automation_subscription_fallback as fb_mod
from src.dalton.services import automation_thread_model as atm_mod
from src.llms.llm import LLM as LLMFactory
from src.server.app import setup as app_setup
from src.server.database import automation_executions as exec_db
from src.server.database import conversation as qr_db
from src.server.database import oauth_tokens as oauth_db
from src.server.database import workspace as ws_db
from src.server.database.runs import lifecycle as tl_db
from src.server.dependencies import usage_limits
from src.server.services.platform_client import quota_service_unavailable
from src.server.services.shared_workspaces import turn as _turn_mod
from src.server.services.shared_workspaces.turn import TurnIdentity
from src.server.handlers.chat import ptc_run, request_prep
from src.server.models.chat import ChatMessage, ChatRequest, ThreadOrigin
from src.server.services import automation_settlement
from src.server.services.automation_excerpt import RunAnswer
from src.server.services.llm import config as llm_config_mod
from src.server.services.llm import thread_model, user_models
from src.server.services.report_back.handoff import carry
from src.server.services.runs import admission as admission_mod
from src.server.services.turn_runtime import TurnRoute
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app, pin_chat_limit

_USER = "usr-sub-fb-1"
_WS = "ws-sub-fb-1"
_THREAD = "11111111-1111-4111-8111-111111111111"
_EXEC = "22222222-2222-4222-8222-222222222222"
_AUTO = "33333333-3333-4333-8333-333333333333"
_RUN = "44444444-4444-4444-8444-444444444444"
_THREADS_MOD = "src.server.app.threads.messaging"


def _make_agent_config(
    model_name: str,
    *,
    cred: CredentialSource = CredentialSource.PLATFORM,
    has_client: bool = True,
) -> AgentConfig:
    cfg = create_autospec(AgentConfig, instance=True)
    llm_cfg = create_autospec(LLMConfig, instance=True)
    llm_cfg.name = model_name
    cfg.llm = llm_cfg
    cfg.credential_source = cred
    client = object() if has_client else None
    cfg.llm_client = client
    if has_client:
        cfg.get_llm_client.return_value = client
    else:
        cfg.get_llm_client.side_effect = ValueError("missing credentials")
    cfg.user_skill_dir = None
    cfg.disabled_skills = ()
    cfg.llm_definition = None
    cfg.claude_code_admitted = None
    cfg.claude_code = False
    sub_cfg = create_autospec(AgentConfig, instance=True)
    sub_cfg.enabled = False
    cfg.subagents = sub_cfg
    return cfg


class _FakeModelCatalog:
    def __init__(self, providers_by_model: dict[str, str]) -> None:
        self._providers = providers_by_model
        self.llm_config = {
            name: {"provider": provider} for name, provider in providers_by_model.items()
        }

    def get_model_config(self, name: str) -> dict[str, str] | None:
        provider = self._providers.get(name)
        return {"provider": provider} if provider else None


def _reauth_exc(
    provider: str = "claude-oauth", model: str | None = "claude-opus-4-6-oauth"
) -> HTTPException:
    from src.server.services.llm.availability import raise_oauth_reauth_required
    from src.server.services.oauth_providers import SUBSCRIPTIONS

    sub = SUBSCRIPTIONS[provider]
    try:
        raise_oauth_reauth_required(sub, model, "invalid_grant")
    except HTTPException as exc:
        return exc
    raise AssertionError("raise_oauth_reauth_required must raise")


def _plan_no_api_exc(provider: str = "grok-oauth") -> HTTPException:
    from src.server.services.llm.availability import raise_oauth_plan_no_api_access
    from src.server.services.oauth_providers import SUBSCRIPTIONS

    try:
        raise_oauth_plan_no_api_access(SUBSCRIPTIONS[provider])
    except HTTPException as exc:
        return exc
    raise AssertionError("raise_oauth_plan_no_api_access must raise")


def _refresh_unavailable_exc(provider: str = "claude-oauth") -> HTTPException:
    from src.server.services.llm.availability import raise_oauth_refresh_unavailable
    from src.server.services.oauth_providers import SUBSCRIPTIONS

    try:
        raise_oauth_refresh_unavailable(SUBSCRIPTIONS[provider])
    except HTTPException as exc:
        return exc
    raise AssertionError("raise_oauth_refresh_unavailable must raise")


@contextmanager
def _catalog_and_classify(providers_by_model: dict[str, str]):
    catalog = _FakeModelCatalog(providers_by_model)

    async def _classify(_uid: str, model_name: str, _pref_cache=None):
        if model_name in providers_by_model:
            return user_models.ModelSource.SYSTEM, {
                "provider": providers_by_model[model_name]
            }
        return user_models.ModelSource.UNKNOWN, {}

    with (
        patch.object(
            LLMFactory,
            "get_model_config",
            create_autospec(LLMFactory.get_model_config, return_value=catalog),
        ),
        patch.object(
            user_models,
            "classify_model",
            create_autospec(user_models.classify_model, side_effect=_classify),
        ),
    ):
        yield


class _CapturedBeforeStart(Exception):
    pass


async def _drive_ptc_pre_start(
    *,
    request: ChatRequest,
    run_metadata: dict | None,
    resolve_fn,
    begin_run_side_effect=None,
):
    """Run ``ptc_run.astream_ptc_workflow`` with ``create_autospec`` doubles."""
    base_cfg = _make_agent_config("base-model")
    wm = create_autospec(WorkspaceManager, instance=True)
    wm.has_ready_session.return_value = True

    begin_run_mock = create_autospec(
        ptc_run.begin_run,
        side_effect=begin_run_side_effect or _CapturedBeforeStart(),
    )

    patches = [
        patch.object(app_setup, "agent_config", base_cfg),
        patch.object(
            ptc_run.WorkspaceManager,
            "get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=wm),
        ),
        patch.object(
            ptc_run,
            "wait_or_steer",
            create_autospec(ptc_run.wait_or_steer, return_value=(True, None)),
        ),
        patch.object(
            ptc_run,
            "dedup_retransmit_or_raise",
            create_autospec(ptc_run.dedup_retransmit_or_raise, return_value=None),
        ),
        patch.object(
            ptc_run,
            "read_disk_notice",
            create_autospec(ptc_run.read_disk_notice, return_value=(None, False)),
        ),
        patch.object(
            _turn_mod,
            "get_user_profile_for_prompt",
            create_autospec(_turn_mod.get_user_profile_for_prompt, return_value=None),
        ),
        patch.object(
            ptc_run.HomeTurnStart,
            "read",
            create_autospec(
                ptc_run.HomeTurnStart.read,
                return_value=ptc_run.HomeTurnStart(workspace_id=_WS, away=None, back=None),
            ),
        ),
        patch.object(
            carry,
            "prior_attempt",
            create_autospec(
                carry.prior_attempt,
                return_value=carry.PriorAttempt(latest=None, retried=None),
            ),
        ),
        patch.object(
            carry,
            "carried_pair",
            create_autospec(carry.carried_pair, return_value={}),
        ),
        patch.object(
            carry,
            "carried_delivery",
            create_autospec(carry.carried_delivery, return_value=None),
        ),
        patch.object(
            carry,
            "carried_firing",
            create_autospec(carry.carried_firing, return_value={}),
        ),
        patch.object(
            ptc_run,
            "_resolve_origin_meta",
            create_autospec(ptc_run._resolve_origin_meta, return_value={}),
        ),
        patch.object(ptc_run, "begin_run", begin_run_mock),
        patch.object(
            request_prep.qr_db,
            "get_thread_by_id",
            create_autospec(
                qr_db.get_thread_by_id,
                return_value={
                    "conversation_thread_id": _THREAD,
                    "workspace_id": _WS,
                    "current_status": "completed",
                },
            ),
        ),
        patch.object(
            request_prep.qr_db,
            "ensure_thread_exists",
            create_autospec(qr_db.ensure_thread_exists, return_value=False),
        ),
        patch.object(
            request_prep.qr_db,
            "update_thread_status",
            create_autospec(qr_db.update_thread_status, return_value=True),
        ),
        patch.object(
            request_prep.tl_db,
            "get_latest_attempt",
            create_autospec(tl_db.get_latest_attempt, return_value=None),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=resolve_fn),
        ),
        patch.object(
            usage_limits,
            "release_burst_slot",
            create_autospec(usage_limits.release_burst_slot, return_value=None),
        ),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        gen = ptc_run.astream_ptc_workflow(
            request=request,
            thread_id=_THREAD,
            run_id=_RUN,
            user_input="run check",
            user_id=_USER,
            route=TurnRoute(_WS, "analyst"),
            identity=TurnIdentity(_USER, _USER, _USER),
            is_byok=False,
            from_server=True,
            steerable=False,
            run_metadata=run_metadata,
        )
        events: list[str] = []
        raised: BaseException | None = None
        try:
            async for ev in gen:
                events.append(ev)
        except BaseException as exc:
            raised = exc
        finally:
            await gen.aclose()
    return begin_run_mock, events, raised


@pytest.mark.asyncio
async def test_automation_first_turn_falls_back_on_oauth_reauth_and_settles_with_notice():
    """Automation first turn skips same-provider fallback, uses first healthy fallback, and settles with notice."""
    resolved_models: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_models.append(model_name)
        if model_name in ("claude-opus-4-6-oauth", "claude-sonnet-4-6-oauth"):
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": [
            "claude-sonnet-4-6-oauth",
            "missing-model",
            "gemini-3-flash",
            "gpt-6.1",
        ],
    }

    request = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="run check")],
        llm_model="claude-opus-4-6-oauth",
        origin=ThreadOrigin(type="automation", id=_AUTO, name="Daily Check"),
    )

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "claude-sonnet-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
                "gpt-6.1": "openai",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(
                fb_mod.model_resolves,
                side_effect=lambda _p, name: name != "missing-model",
            ),
        ),
    ):
        begin_mock, _events, raised = await _drive_ptc_pre_start(
            request=request,
            run_metadata={"automation_execution_id": _EXEC, "automation_id": _AUTO},
            resolve_fn=_resolve,
        )

    assert isinstance(raised, _CapturedBeforeStart)
    assert resolved_models == ["claude-opus-4-6-oauth", "gemini-3-flash"]

    begin_kwargs = begin_mock.await_args.kwargs
    assert begin_kwargs["query_metadata"]["llm_model"] == "gemini-3-flash"
    extra_meta = begin_kwargs["extra_run_metadata"]
    assert extra_meta["llm_model"] == "gemini-3-flash"
    expected_notice = (
        "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    )
    assert extra_meta["subscription_fallback"]["notice"] == expected_notice

    captured_settle: dict = {}

    async def _capture_settle(execution_id, **kwargs):
        captured_settle.update(kwargs)
        return {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": _RUN,
            "settled_from": "running",
        }

    delivered_final_texts: list[str] = []

    async def _capture_notify(**kwargs):
        delivered_final_texts.append(kwargs.get("final_text"))
        return True

    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.dalton.notify.dispatch import on_automation_settled

    run_row = {
        "conversation_response_id": _RUN,
        "status": "completed",
        "metadata": extra_meta,
    }
    with (
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_capture_settle),
        ),
        patch.object(
            automation_settlement,
            "read_run_answer",
            create_autospec(
                automation_settlement.read_run_answer,
                return_value=RunAnswer(text="Market summary looks steady.", sent=None),
            ),
        ),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(
                automation_settlement.publish_automation_wait, return_value=None
            ),
        ),
        patch(
            "src.dalton.notify.dispatch.on_automation_settled",
            create_autospec(on_automation_settled, side_effect=_capture_notify),
        ),
    ):
        outcome = await automation_settlement.settle_by_run(
            {"automation_id": _AUTO, "user_id": _USER, "trigger_type": "cron"},
            _EXEC,
            _RUN,
            run_row,
            thread_id=_THREAD,
            workspace_id=_WS,
        )

    assert outcome is automation_settlement.Outcome.COMPLETED
    assert captured_settle["result_excerpt"].startswith(expected_notice)

    assert "Market summary looks steady." in captured_settle["result_excerpt"]
    assert len(delivered_final_texts) == 1
    assert delivered_final_texts[0].startswith(expected_notice)
    assert "Market summary looks steady." in delivered_final_texts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _reauth_exc("claude-oauth", "claude-opus-4-6-oauth"),
        lambda: _plan_no_api_exc("claude-oauth"),
        lambda: _refresh_unavailable_exc("claude-oauth"),
    ],
    ids=[
        "oauth_reauth_required",
        "oauth_plan_no_api_access",
        "oauth_refresh_unavailable",
    ],
)
async def test_automation_first_turn_fails_when_no_usable_fallback(
    exc_factory,
):
    """When all fallback_models fail or match failed provider, original error surfaces."""

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise exc_factory()
        raise HTTPException(
            status_code=400,
            detail={"message": "No key", "type": "byok_key_required"},
        )

    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["claude-sonnet-4-6-oauth", "custom-no-key"],
    }
    request = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="run check")],
        llm_model="claude-opus-4-6-oauth",
        origin=ThreadOrigin(type="automation", id=_AUTO, name="Daily Check"),
    )

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "claude-sonnet-4-6-oauth": "claude-oauth",
                "custom-no-key": "openai",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
    ):
        begin_mock, events, raised = await _drive_ptc_pre_start(
            request=request,
            run_metadata={"automation_execution_id": _EXEC, "automation_id": _AUTO},
            resolve_fn=_resolve,
        )

    begin_mock.assert_not_awaited()
    assert isinstance(raised, HTTPException)
    assert raised.detail["type"] == exc_factory().detail["type"]
    assert any("event: error" in ev for ev in events)


@pytest.mark.asyncio
async def test_interactive_chat_oauth_reauth_still_raises_and_does_not_fallback():
    """Interactive turns never silently fall back on oauth_reauth_required."""
    resolved_models: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_models.append(model_name)
        raise _reauth_exc("claude-oauth", str(model_name))

    interactive_req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="hello")],
        llm_model="claude-opus-4-6-oauth",
        platform="web",
    )
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"fallback_models": ["gemini-3-flash"]},
            ),
        ),
    ):
        begin_mock, _events, raised = await _drive_ptc_pre_start(
            request=interactive_req,
            run_metadata=None,
            resolve_fn=_resolve,
        )

    begin_mock.assert_not_awaited()
    assert isinstance(raised, HTTPException)
    assert raised.detail["type"] == "oauth_reauth_required"
    assert resolved_models == ["claude-opus-4-6-oauth"]


@pytest.mark.asyncio
async def test_interactive_messaging_route_with_flagged_subscription_still_raises_400():
    """Interactive user turn on messaging route still raises 400 oauth_reauth_required even with fallback_models."""
    from src.server.app.threads import router

    app = create_test_app(router)
    pin_chat_limit(
        app,
        usage_limits.ChatAuthResult(
            user_id=_USER,
            is_byok=False,
            has_oauth=False,
            access_tier=-1,
            burst_slot_id="s1",
        ),
    )

    with (
        patch("src.config.settings.HOST_MODE", "platform"),
        patch.object(
            admission_mod.RunScope,
            "release_slot",
            create_autospec(admission_mod.RunScope.release_slot, return_value=None),
        ),
        patch.object(
            oauth_db,
            "flagged_oauth_connection",
            create_autospec(
                oauth_db.flagged_oauth_connection,
                return_value={
                    "provider": "claude-oauth",
                    "last_error_code": "invalid_grant",
                },
            ),
        ),
        patch.object(
            user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"fallback_models": ["gemini-3-flash"]},
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "workspace_id": _WS,
                    "platform": "web",
                },
            )

    assert resp.status_code == 400
    assert resp.json()["detail"]["type"] == "oauth_reauth_required"
    assert resp.json()["detail"]["provider"] == "claude-oauth"


@contextmanager
def _notification_route_stubs(
    *,
    access_tier: int,
    has_oauth: bool,
    flagged: dict | None,
    prefs: dict,
    resolve_side_effect,
):
    from src.server.app.threads import messaging as msg_mod
    from src.server.app.threads import router

    app = create_test_app(router)
    pin_chat_limit(
        app,
        usage_limits.ChatAuthResult(
            user_id=_USER,
            is_byok=False,
            has_oauth=has_oauth,
            access_tier=access_tier,
            burst_slot_id="s1",
        ),
    )
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
        "workspace_id": _WS,
    }
    thread_meta = {
        "user_id": _USER,
        "owner_id": _USER,
        "thread_user_id": _USER,
        "workspace_owner_id": _USER,
        "workspace_status": "running",
        "metadata": {},
        "is_shared": False,
        "msg_type": "ptc",
        "workspace_id": _WS,
        "llm_model": None,
        "reasoning_effort": None,
        "fast_mode": False,
    }
    wm = create_autospec(WorkspaceManager, instance=True)
    wm.has_ready_session.return_value = True
    astream_calls: list[dict] = []

    def _fake_astream(*_args, **kwargs):
        astream_calls.append(kwargs)

        async def _g():
            if kwargs.get("dispatched"):
                yield request_prep.DISPATCH_STARTED_MARKER

        return _g()

    patches = [
        patch("src.config.settings.HOST_MODE", "platform"),
        patch.object(
            dalton_auth,
            "is_dalton_supabase_auth_enabled",
            create_autospec(dalton_auth.is_dalton_supabase_auth_enabled, return_value=False),
        ),
        patch.object(
            msg_mod,
            "_get_service_token",
            create_autospec(msg_mod._get_service_token, return_value="svc-token"),
        ),
        patch.object(
            msg_mod,
            "get_thread_auth_meta",
            create_autospec(qr_db.get_thread_auth_meta, return_value=thread_meta),
        ),
        patch.object(
            msg_mod,
            "get_thread_by_id",
            create_autospec(
                qr_db.get_thread_by_id,
                return_value={"conversation_thread_id": _THREAD, "workspace_id": _WS},
            ),
        ),
        patch.object(
            ws_db,
            "get_workspace",
            create_autospec(
                ws_db.get_workspace,
                return_value={
                    "workspace_id": _WS,
                    "user_id": _USER,
                    "status": "running",
                },
            ),
        ),
        patch.object(app_setup, "agent_config", _make_agent_config("base-model")),
        patch.object(
            oauth_db,
            "flagged_oauth_connection",
            create_autospec(oauth_db.flagged_oauth_connection, return_value=flagged),
        ),
        patch.object(
            atm_mod.exec_db,
            "get_running_executions_for_thread",
            create_autospec(
                exec_db.get_running_executions_for_thread, return_value=[exec_row]
            ),
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(
            atm_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            atm_mod,
            "model_resolves",
            create_autospec(thread_model.model_resolves, return_value=True),
        ),
        patch.object(
            thread_model.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            thread_model,
            "model_resolves",
            create_autospec(thread_model.model_resolves, return_value=True),
        ),
        patch.object(
            llm_config_mod,
            "resolve_llm_config",
            create_autospec(
                llm_config_mod.resolve_llm_config, side_effect=resolve_side_effect
            ),
        ),
        patch.object(
            usage_limits,
            "enforce_credit_limit",
            create_autospec(usage_limits.enforce_credit_limit, return_value=None),
        ),
        patch.object(
            WorkspaceManager,
            "get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=wm),
        ),
        patch(
            "src.server.handlers.chat.astream_ptc_workflow",
            create_autospec(ptc_run.astream_ptc_workflow, side_effect=_fake_astream),
        ),
        patch.object(
            msg_mod,
            "observe_chat_stream",
            create_autospec(
                msg_mod.observe_chat_stream, side_effect=lambda gen, **_: gen
            ),
        ),
        patch.object(
            msg_mod,
            "_assert_stream_transport_ready",
            create_autospec(msg_mod._assert_stream_transport_ready, return_value=None),
        ),
        patch.object(
            admission_mod.RunScope,
            "release_slot",
            create_autospec(admission_mod.RunScope.release_slot, return_value=None),
        ),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield SimpleNamespace(app=app, astream_calls=astream_calls)


@pytest.mark.asyncio
async def test_automation_notification_turn_falls_back_through_flagged_gate():
    """Automation notification turn defers messaging flagged-subscription gate and falls back to fallback_models."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["claude-sonnet-4-6-oauth", "gemini-3-flash"],
    }
    resolve_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolve_calls.append(model_name)
        if model_name in ("claude-opus-4-6-oauth", "claude-sonnet-4-6-oauth"):
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "claude-sonnet-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        _notification_route_stubs(
            access_tier=-1,
            has_oauth=False,
            flagged={"provider": "claude-oauth", "last_error_code": "invalid_grant"},
            prefs=prefs,
            resolve_side_effect=_resolve,
        ) as st,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "automation_delivery": {"id": _EXEC, "targets": []},
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )

    assert resp.status_code == 200
    assert resolve_calls == ["gemini-3-flash"]
    run_meta = st.astream_calls[0]["run_metadata"]
    assert run_meta["subscription_fallback"]["notice"] == (
        "Claude 订阅的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    )


@pytest.mark.asyncio
async def test_automation_notification_turn_falls_back_on_direct_subscription_refusal():
    """Automation notification turn falls back to fallback_models when inherited model hits oauth_reauth_required/oauth_refresh_unavailable."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["claude-sonnet-4-6-oauth", "gemini-3-flash"],
    }
    resolve_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolve_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _refresh_unavailable_exc("claude-oauth")
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "claude-sonnet-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        _notification_route_stubs(
            access_tier=0,
            has_oauth=True,
            flagged=None,
            prefs=prefs,
            resolve_side_effect=_resolve,
        ) as st,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "automation_delivery": {"id": _EXEC, "targets": []},
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )

    assert resp.status_code == 200
    assert resolve_calls == ["claude-opus-4-6-oauth", "gemini-3-flash"]
    run_meta = st.astream_calls[0]["run_metadata"]
    assert run_meta["subscription_fallback"]["notice"] == (
        "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    )


@pytest.mark.asyncio
async def test_automation_notification_turn_flagged_gate_without_fallback_fails_400():
    """Automation notification turn still returns 400 oauth_reauth_required when no fallback_models work."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["claude-sonnet-4-6-oauth"],
    }

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        raise _reauth_exc("claude-oauth", str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "claude-sonnet-4-6-oauth": "claude-oauth",
            }
        ),
        _notification_route_stubs(
            access_tier=-1,
            has_oauth=False,
            flagged={"provider": "claude-oauth", "last_error_code": "invalid_grant"},
            prefs=prefs,
            resolve_side_effect=_resolve,
        ) as st,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "automation_delivery": {"id": _EXEC, "targets": []},
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )

    assert resp.status_code == 400
    assert resp.json()["detail"]["type"] == "oauth_reauth_required"
    assert st.astream_calls == []


@pytest.mark.asyncio
async def test_interactive_cos_messaging_route_still_raises_400_on_flagged_subscription():
    """Chief of Staff interactive turn still raises 400 oauth_reauth_required without fallback."""
    from src.server.app.threads import router

    app = create_test_app(router)
    pin_chat_limit(
        app,
        usage_limits.ChatAuthResult(
            user_id=_USER,
            is_byok=False,
            has_oauth=False,
            access_tier=-1,
            burst_slot_id="s1",
        ),
    )

    with (
        patch("src.config.settings.HOST_MODE", "platform"),
        patch.object(
            admission_mod.RunScope,
            "release_slot",
            create_autospec(admission_mod.RunScope.release_slot, return_value=None),
        ),
        patch.object(
            oauth_db,
            "flagged_oauth_connection",
            create_autospec(
                oauth_db.flagged_oauth_connection,
                return_value={
                    "provider": "claude-oauth",
                    "last_error_code": "invalid_grant",
                },
            ),
        ),
        patch.object(
            user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"fallback_models": ["gemini-3-flash"]},
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "delegate check"}],
                    "origin": {"type": "agent", "agent": "chief_of_staff", "id": "cos-1"},
                    "platform": "web",
                },
            )

    assert resp.status_code == 400
    assert resp.json()["detail"]["type"] == "oauth_reauth_required"


@pytest.mark.asyncio
async def test_settlement_reads_fallback_notice_from_earlier_turn_in_execution():
    """When turn 1 fell back and turn 2 settles the execution, settlement still finds the notice."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor

    expected_notice = (
        "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    )
    mock_cur = create_autospec(AsyncCursor, instance=True)
    mock_cur.fetchall.return_value = [
        {"metadata": {"subscription_fallback": {"notice": expected_notice}}}
    ]

    @asynccontextmanager
    async def _cursor_ctx(*_a, **_kw):
        yield mock_cur

    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.cursor.side_effect = _cursor_ctx

    @asynccontextmanager
    async def _conn_ctx():
        yield mock_conn

    captured_settle: dict = {}

    async def _capture_settle(_execution_id, **kwargs):
        captured_settle.update(kwargs)
        return {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": _RUN,
            "settled_from": "running",
        }

    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.dalton.notify.dispatch import on_automation_settled

    settling_continuation_run = {
        "conversation_response_id": _RUN,
        "status": "completed",
        "metadata": {"automation_execution_id": _EXEC},
    }
    with (
        patch.object(
            fb_mod,
            "get_db_connection",
            create_autospec(fb_mod.get_db_connection, side_effect=_conn_ctx),
        ),
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_capture_settle),
        ),
        patch.object(
            automation_settlement,
            "read_run_answer",
            create_autospec(
                automation_settlement.read_run_answer,
                return_value=RunAnswer(text="Continuation done.", sent=None),
            ),
        ),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(
                automation_settlement.publish_automation_wait, return_value=None
            ),
        ),
        patch(
            "src.dalton.notify.dispatch.on_automation_settled",
            create_autospec(on_automation_settled, return_value=True),
        ),
    ):
        outcome = await automation_settlement.settle_by_run(
            {"automation_id": _AUTO, "user_id": _USER, "trigger_type": "cron"},
            _EXEC,
            _RUN,
            settling_continuation_run,
            thread_id=_THREAD,
            workspace_id=_WS,
            exec_row={
                "automation_execution_id": _EXEC,
                "delivery": {"subscription_fallback": True},
            },
        )

    assert outcome is automation_settlement.Outcome.COMPLETED
    assert captured_settle["result_excerpt"].startswith(expected_notice)


def _install_signal_release_db_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started_exec_ids: set[str],
    initial_signals: dict[str, dict],
) -> object:
    """Install stateful psycopg AsyncConnection/AsyncCursor autospecs for signal release."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor
    from src.dalton.database import watch as watch_db

    mock_cur = create_autospec(AsyncCursor, instance=True)
    last_rows: list[dict] = []
    last_one: dict | None = None

    async def _exec(sql: str, params=None) -> None:
        nonlocal last_one
        norm = " ".join(sql.split())
        last_rows.clear()
        last_one = None
        if "FROM conversation_responses" in norm and "automation_execution_id" in norm:
            (*_, exec_arg) = params
            if str(exec_arg) in started_exec_ids:
                last_one = {"started": 1}
        elif norm.startswith("UPDATE dalton_signals SET woken_execution_id = NULL"):
            assert "resolved_at IS NULL" in norm
            assert "expired_at IS NULL" in norm
            (q_exec,) = params
            for row in initial_signals.values():
                if (
                    row["woken_execution_id"] == q_exec
                    and row["resolved_at"] is None
                    and row["expired_at"] is None
                ):
                    row["woken_execution_id"] = None
                    row["woken_at"] = None
                    row["payload"] = {**(row.get("payload") or {}), "status": "pending"}
                    last_rows.append({"signal_id": row["signal_id"]})

    mock_cur.execute.side_effect = _exec
    mock_cur.fetchone.side_effect = lambda: last_one
    mock_cur.fetchall.side_effect = lambda: list(last_rows)

    @asynccontextmanager
    async def _cursor_ctx(*_args, **_kwargs):
        yield mock_cur

    @asynccontextmanager
    async def _tx_ctx():
        yield None

    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.cursor.side_effect = _cursor_ctx
    mock_conn.transaction.side_effect = _tx_ctx

    @asynccontextmanager
    async def _conn_ctx():
        yield mock_conn

    monkeypatch.setattr(
        watch_db,
        "get_db_connection",
        create_autospec(watch_db.get_db_connection, side_effect=_conn_ctx),
    )
    monkeypatch.setattr(
        fb_mod,
        "get_db_connection",
        create_autospec(fb_mod.get_db_connection, side_effect=_conn_ctx),
    )
    return mock_conn


@pytest.mark.asyncio
async def test_pre_start_failed_settlement_releases_claimed_signals_and_keeps_resolved(
    monkeypatch: pytest.MonkeyPatch,
):
    """Pre-START FAILED settles release unresolved schedule claims while preserving resolved ones."""
    from datetime import datetime, timezone
    from src.dalton.database import watch as watch_db
    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.server.services.webhook_client import WebhookClient

    ts = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    exec_uuid = watch_db._signal_exec_uuid(_EXEC)
    signals = {
        "schedule_open": {
            "signal_id": "11111111-1111-1111-1111-111111111111",
            "woken_execution_id": exec_uuid,
            "woken_at": ts,
            "resolved_at": None,
            "expired_at": None,
            "payload": {"kind": "weekly_delta", "status": "woken"},
        },
        "already_resolved": {
            "signal_id": "22222222-2222-2222-2222-222222222222",
            "woken_execution_id": exec_uuid,
            "woken_at": ts,
            "resolved_at": ts,
            "expired_at": None,
            "payload": {"kind": "weekly_delta", "status": "resolved"},
        },
    }
    tx_conn = _install_signal_release_db_double(
        monkeypatch, started_exec_ids=set(), initial_signals=signals
    )

    async def _fake_settle_execution(_execution_id: str, *, in_transaction=None, **_kw):
        settled = {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": None,
            "settled_from": "running",
            "delivery": None,
            "previous_failure_reason": None,
            "previous_delivery_result": None,
        }
        if in_transaction is not None:
            await in_transaction(tx_conn, settled)
        return settled

    webhook_inst = create_autospec(WebhookClient, instance=True)
    webhook_inst.fire_event.return_value = []
    with (
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_fake_settle_execution),
        ),
        patch.object(fb_mod, "get_execution_fallback_notice", create_autospec(fb_mod.get_execution_fallback_notice, return_value=None)),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "WebhookClient",
            create_autospec(WebhookClient, return_value=webhook_inst),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(automation_settlement.publish_automation_wait, return_value=None),
        ),
    ):
        settled = await automation_settlement.settle(
            {
                "automation_id": _AUTO,
                "user_id": _USER,
                "name": "dalton-nvda-delta",
                "trigger_type": "cron",
                "metadata": {"dalton_managed": True},
            },
            _EXEC,
            automation_settlement.Outcome.FAILED,
            thread_id=_THREAD,
            workspace_id=_WS,
            error="oauth_reauth_required",
            release_pre_start_signals=True,
        )
        await automation_settlement.drain_tails()

    assert settled is True
    assert signals["schedule_open"]["woken_execution_id"] is None
    assert signals["schedule_open"]["woken_at"] is None
    assert signals["schedule_open"]["payload"]["status"] == "pending"
    assert signals["already_resolved"]["woken_execution_id"] == exec_uuid
    assert signals["already_resolved"]["woken_at"] == ts


@pytest.mark.asyncio
async def test_post_start_failed_settlement_keeps_claimed_signals(
    monkeypatch: pytest.MonkeyPatch,
):
    """FAILED executions that already entered START (by run link or conversation_responses) keep signals."""
    from datetime import datetime, timezone
    from src.dalton.database import watch as watch_db
    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.server.services.webhook_client import WebhookClient

    ts = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    exec_uuid = watch_db._signal_exec_uuid(_EXEC)
    signals = {
        "schedule_open": {
            "signal_id": "33333333-3333-3333-3333-333333333333",
            "woken_execution_id": exec_uuid,
            "woken_at": ts,
            "resolved_at": None,
            "expired_at": None,
            "payload": {"kind": "weekly_delta", "status": "woken"},
        }
    }
    tx_conn = _install_signal_release_db_double(
        monkeypatch, started_exec_ids={_EXEC}, initial_signals=signals
    )

    async def _settle_unlinked_but_started(_execution_id: str, *, in_transaction=None, **_kw):
        settled = {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": None,
            "settled_from": "running",
            "delivery": None,
            "previous_failure_reason": None,
            "previous_delivery_result": None,
        }
        if in_transaction is not None:
            await in_transaction(tx_conn, settled)
        return settled

    webhook_inst = create_autospec(WebhookClient, instance=True)
    webhook_inst.fire_event.return_value = []
    dalton_auto = {
        "automation_id": _AUTO,
        "user_id": _USER,
        "name": "dalton-nvda-delta",
        "trigger_type": "cron",
        "metadata": {"dalton_managed": True},
    }
    with (
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_settle_unlinked_but_started),
        ),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "WebhookClient",
            create_autospec(WebhookClient, return_value=webhook_inst),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(automation_settlement.publish_automation_wait, return_value=None),
        ),
    ):
        assert (
            await automation_settlement.settle(
                dalton_auto,
                _EXEC,
                automation_settlement.Outcome.FAILED,
                thread_id=_THREAD,
                workspace_id=_WS,
                error="mid-turn crash",
                release_pre_start_signals=True,
            )
        ) is True
        await automation_settlement.drain_tails()

    assert signals["schedule_open"]["woken_execution_id"] == exec_uuid
    assert signals["schedule_open"]["woken_at"] == ts


@pytest.mark.asyncio
async def test_has_started_response_rejects_unsupported_connection():
    """_has_started_response_in_db raises TypeError rather than guessing 'unstarted' on an invalid conn."""
    with pytest.raises(TypeError, match="Unsupported DB connection"):
        await fb_mod._has_started_response_in_db(_EXEC, conn=object())


@pytest.mark.asyncio
async def test_generic_timeout_and_skipped_settlements_never_release_signals(
    monkeypatch: pytest.MonkeyPatch,
):
    """TIMEOUT / PRECHECK_SKIPPED / INTERRUPTED settlements never clear woken_execution_id."""
    from datetime import datetime, timezone
    from src.dalton.database import watch as watch_db
    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.server.services.webhook_client import WebhookClient

    ts = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    exec_uuid = watch_db._signal_exec_uuid(_EXEC)
    signals = {
        "schedule_open": {
            "signal_id": "44444444-4444-4444-4444-444444444444",
            "woken_execution_id": exec_uuid,
            "woken_at": ts,
            "resolved_at": None,
            "expired_at": None,
            "payload": {"kind": "weekly_delta", "status": "woken"},
        }
    }
    _install_signal_release_db_double(
        monkeypatch, started_exec_ids=set(), initial_signals=signals
    )

    async def _fake_settle_execution(_eid: str, *, in_transaction=None, **_kw):
        settled = {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": None,
            "settled_from": "running",
            "delivery": None,
            "previous_failure_reason": None,
            "previous_delivery_result": None,
        }
        if in_transaction is not None:
            await in_transaction(
                create_autospec(
                    __import__("psycopg").AsyncConnection, instance=True
                ),
                settled,
            )
        return settled

    webhook_inst = create_autospec(WebhookClient, instance=True)
    webhook_inst.fire_event.return_value = []
    dalton_auto = {
        "automation_id": _AUTO,
        "user_id": _USER,
        "name": "dalton-nvda-delta",
        "trigger_type": "cron",
        "metadata": {"dalton_managed": True},
    }
    with (
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_fake_settle_execution),
        ),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "WebhookClient",
            create_autospec(WebhookClient, return_value=webhook_inst),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(automation_settlement.publish_automation_wait, return_value=None),
        ),
    ):
        for outcome in (
            automation_settlement.Outcome.TIMEOUT,
            automation_settlement.Outcome.PRECHECK_SKIPPED,
            automation_settlement.Outcome.INTERRUPTED,
        ):
            assert (
                await automation_settlement.settle(
                    dalton_auto,
                    _EXEC,
                    outcome,
                    thread_id=_THREAD,
                    workspace_id=_WS,
                )
            ) is True
        await automation_settlement.drain_tails()

    assert signals["schedule_open"]["woken_execution_id"] == exec_uuid
    assert signals["schedule_open"]["woken_at"] == ts


@pytest.mark.asyncio
async def test_unlinked_running_thread_notification_denied_subscription_fallback():
    """Standalone wakeup / unlinked system turn on thread with running execution is denied subscription fallback."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gemini-3-flash"],
    }
    resolve_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolve_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        _notification_route_stubs(
            access_tier=-1,
            has_oauth=False,
            flagged={"provider": "claude-oauth", "last_error_code": "invalid_grant"},
            prefs=prefs,
            resolve_side_effect=_resolve,
        ) as st,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp_wakeup = await client.post(
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

    assert resp_wakeup.status_code == 400
    assert resp_wakeup.json()["detail"]["type"] == "oauth_reauth_required"
    assert resolve_calls == []
    assert st.astream_calls == []


@pytest.mark.asyncio
async def test_explicit_model_linked_notification_turn_allows_subscription_fallback():
    """Notification turn carrying explicit llm_model AND explicit execution link uses subscription fallback."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gemini-3-flash"],
    }
    resolve_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolve_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        _notification_route_stubs(
            access_tier=0,
            has_oauth=True,
            flagged=None,
            prefs=prefs,
            resolve_side_effect=_resolve,
        ) as st,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=st.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{_THREAD}/messages",
                json={
                    "messages": [{"role": "user", "content": "<system>done</system>"}],
                    "workspace_id": _WS,
                    "query_type": "system",
                    "llm_model": "claude-opus-4-6-oauth",
                    "automation_delivery": {"id": _EXEC, "targets": []},
                },
                headers={
                    "X-Service-Token": "svc-token",
                    "X-User-Id": _USER,
                    "X-Dispatch": "background",
                },
            )

    assert resp.status_code == 200
    assert resolve_calls == ["claude-opus-4-6-oauth", "gemini-3-flash"]
    run_meta = st.astream_calls[0]["run_metadata"]
    assert run_meta["subscription_fallback"]["execution_id"] == _EXEC
    assert "gemini-3-flash" in run_meta["subscription_fallback"]["notice"]


@pytest.mark.asyncio
async def test_candidate_with_none_llm_client_is_skipped_and_rejects_when_all_none():
    """Candidates resolving with llm_client=None are skipped; raises original error when none have clients."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gpt-no-key", "gemini-3-flash"],
    }
    attempted: list[str] = []

    async def _resolve_skip_none(_cfg, _uid, model_name, *_args, **_kwargs):
        attempted.append(str(model_name))
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        if model_name == "gpt-no-key":
            return _make_agent_config("gpt-no-key", cred=CredentialSource.NONE, has_client=False)
        return _make_agent_config("gemini-3-flash")

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-no-key": "openai",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_skip_none),
        ),
    ):
        cfg, record = await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )

    assert attempted == ["claude-opus-4-6-oauth", "gpt-no-key", "gemini-3-flash"]
    assert cfg.llm.name == "gemini-3-flash"
    assert record is not None and record["fallback_model"] == "gemini-3-flash"

    async def _resolve_all_none(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name), cred=CredentialSource.NONE, has_client=False)

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-no-key": "openai",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_all_none),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )
    assert exc_info.value.detail["type"] == "oauth_reauth_required"


@pytest.mark.asyncio
async def test_notification_fallback_exhaustion_raises_original_error_never_default_and_propagates_quota_outage():
    """Explicit-linked notification turn with no usable fallback never falls back to held/default; quota 503 propagates."""
    prefs_empty = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": [],
    }
    calls_empty: list[str | None] = []

    async def _resolve_empty(_cfg, _uid, model_name, *_args, **_kwargs):
        calls_empty.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _plan_no_api_exc("claude-oauth")
        return _make_agent_config(str(model_name or "account-default"))

    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    with (
        _catalog_and_classify({"claude-opus-4-6-oauth": "claude-oauth"}),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(
            atm_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs_empty),
        ),
        patch.object(
            atm_mod,
            "model_resolves",
            create_autospec(thread_model.model_resolves, return_value=True),
        ),
        pytest.raises(HTTPException) as exc_empty,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="held-thread-model",
            from_service=True,
            resolve=lambda named: _resolve_empty(None, _USER, named),
        )

    assert exc_empty.value.status_code == 400
    assert exc_empty.value.detail["type"] == "oauth_plan_no_api_access"
    # Never fell back to held-thread-model or None.
    assert calls_empty == ["claude-opus-4-6-oauth"]

    # Quota 503 raised inside candidate resolution must propagate out.
    prefs_quota = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gemini-3-flash"],
    }

    async def _resolve_quota(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        raise quota_service_unavailable()

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(
            atm_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs_quota),
        ),
        patch.object(
            atm_mod,
            "model_resolves",
            create_autospec(thread_model.model_resolves, return_value=True),
        ),
        pytest.raises(HTTPException) as exc_quota,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="held-thread-model",
            from_service=True,
            resolve=lambda named: _resolve_quota(None, _USER, named),
        )

    expected_quota_exc = quota_service_unavailable()
    assert exc_quota.value.status_code == expected_quota_exc.status_code
    assert exc_quota.value.detail["type"] == expected_quota_exc.detail["type"]


@pytest.mark.asyncio
async def test_execution_fallback_notice_aggregates_all_turns_and_stopped_settlement_writes_excerpt():
    """Aggregate deduplicated notices across all turns in order, write STOPPED excerpt, and bound <= 320 chars."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor
    from src.dalton.database.automation_recycle import automation_is_soft_deleted

    notice_1 = "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    notice_2 = "gpt-5-oauth 的登录已失效，本次改用 deepseek-v3；请到设置里重新连接"

    mock_cur = create_autospec(AsyncCursor, instance=True)
    executed_sql: list[str] = []

    async def _exec(sql: str, _params=None) -> None:
        executed_sql.append(" ".join(sql.split()))

    mock_cur.execute.side_effect = _exec
    mock_cur.fetchall.return_value = [
        {"metadata": {"subscription_fallback": {"notice": notice_1}}},
        {"metadata": {"subscription_fallback": {"notice": notice_1}}},
        {"metadata": {"subscription_fallback": {"notice": notice_2}}},
    ]

    @asynccontextmanager
    async def _cursor_ctx(*_a, **_kw):
        yield mock_cur

    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.cursor.side_effect = _cursor_ctx

    @asynccontextmanager
    async def _conn_ctx():
        yield mock_conn

    captured_settle: dict = {}

    async def _capture_settle(_eid: str, **kwargs):
        captured_settle.update(kwargs)
        return {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": _RUN,
            "settled_from": "running",
        }

    stopped_run = {
        "conversation_response_id": _RUN,
        "status": "cancelled",
        "cancel_requested_at": None,
        "metadata": {
            "cancelled_by_user": True,
            "subscription_fallback": {"notice": notice_2, "execution_id": _EXEC},
        },
    }
    with (
        patch.object(
            fb_mod,
            "get_db_connection",
            create_autospec(fb_mod.get_db_connection, side_effect=_conn_ctx),
        ),
        patch.object(
            automation_settlement.exec_db,
            "settle_execution",
            create_autospec(exec_db.settle_execution, side_effect=_capture_settle),
        ),
        patch(
            "src.dalton.database.automation_recycle.automation_is_soft_deleted",
            create_autospec(automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            automation_settlement,
            "publish_automation_wait",
            create_autospec(automation_settlement.publish_automation_wait, return_value=None),
        ),
    ):
        outcome = await automation_settlement.settle_by_run(
            {"automation_id": _AUTO, "user_id": _USER, "trigger_type": "cron"},
            _EXEC,
            _RUN,
            stopped_run,
            thread_id=_THREAD,
            workspace_id=_WS,
        )

    assert outcome is automation_settlement.Outcome.STOPPED
    assert "automation_delivery" in executed_sql[0]
    assert "automation_executions" in executed_sql[0]
    assert "conversation_thread_id" in executed_sql[0]
    assert captured_settle["result_excerpt"] == f"{notice_1}\n{notice_2}"

    # Verify strict <= 800 char bound (aligned with automation_excerpt._EXCERPT_RAW_CHARS) even with long unspaced CJK text.
    long_cjk = "中文无空格连续分析文本" * 100
    bounded = fb_mod.prepend_fallback_notice_to_excerpt(notice_1, long_cjk)
    assert len(bounded) <= 800
    assert bounded.startswith(notice_1)
    assert bounded.endswith("…")


@pytest.mark.asyncio
async def test_lazy_candidate_client_build_succeeds_when_valid_and_retries_on_build_or_unexpected_error():
    """Lazy llm_client=None candidates build via get_llm_client(); unexpected/build failures skip to next."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gpt-unexpected", "gemini-lazy"],
    }
    built_client = object()
    lazy_cfg = _make_agent_config("gemini-lazy", cred=CredentialSource.PLATFORM, has_client=False)
    lazy_cfg.get_llm_client.side_effect = None
    lazy_cfg.get_llm_client.return_value = built_client

    async def _resolve_lazy(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        if model_name == "gpt-unexpected":
            raise RuntimeError("unexpected catalog lookup bug")
        return lazy_cfg

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-unexpected": "openai",
                "gemini-lazy": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_lazy),
        ),
    ):
        cfg, record = await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )

    lazy_cfg.get_llm_client.assert_called_once_with()
    assert cfg is lazy_cfg
    assert record is not None and record["fallback_model"] == "gemini-lazy"

    # Reject path: get_llm_client returns None or quota 503 during build.
    none_cfg = _make_agent_config("gemini-lazy", cred=CredentialSource.NONE, has_client=False)
    none_cfg.get_llm_client.side_effect = None
    none_cfg.get_llm_client.return_value = None

    async def _resolve_returns_none_builder(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return none_cfg

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-unexpected": "openai",
                "gemini-lazy": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(
                llm_config_mod.resolve_llm_config, side_effect=_resolve_returns_none_builder
            ),
        ),
        pytest.raises(HTTPException) as exc_none,
    ):
        await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )
    assert exc_none.value.detail["type"] == "oauth_reauth_required"

    quota_cfg = _make_agent_config("gemini-lazy", cred=CredentialSource.NONE, has_client=False)
    quota_cfg.get_llm_client.side_effect = quota_service_unavailable()

    async def _resolve_quota_on_build(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return quota_cfg

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-unexpected": "openai",
                "gemini-lazy": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_quota_on_build),
        ),
        pytest.raises(HTTPException) as exc_quota,
    ):
        await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )
    assert exc_quota.value.status_code == 503


@pytest.mark.asyncio
async def test_explicit_linked_notification_with_no_inherited_model_uses_fall_back_false():
    """Explicit-linked notification with inherited_model=None passes fall_back=False: falls back or raises original, never account default."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    resolved_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name or "account-default"))

    records: list[dict] = []
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(atm_mod._get_execution_first_turn_llm_model, return_value=None),
        ),
        patch.object(
            atm_mod.auto_db,
            "get_automation_including_deleted",
            create_autospec(atm_mod.auto_db.get_automation_including_deleted, return_value=None),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value="claude-opus-4-6-oauth"),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"preferred_model": "claude-opus-4-6-oauth", "fallback_models": ["gemini-3-flash"]},
            ),
        ),
        patch.object(fb_mod, "model_resolves", create_autospec(fb_mod.model_resolves, return_value=True)),
        patch.object(fb_mod, "resolve_llm_config", create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve)),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            from_service=True,
            is_server_retry=False,
            requested_model=None,
            held_model="claude-opus-4-6-oauth",
            resolve=lambda model_name: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, model_name, False
            ),
            flagged_subscription=None,
            retry_of_run_id=None,
            on_fallback=records.append,
        )

    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1 and records[0]["fallback_model"] == "gemini-3-flash"
    assert None not in resolved_calls

    # Reject case: empty fallback list raises original oauth_reauth_required instead of account default.
    resolved_calls.clear()
    with (
        _catalog_and_classify({"claude-opus-4-6-oauth": "claude-oauth"}),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(atm_mod._get_execution_first_turn_llm_model, return_value=None),
        ),
        patch.object(
            atm_mod.auto_db,
            "get_automation_including_deleted",
            create_autospec(atm_mod.auto_db.get_automation_including_deleted, return_value=None),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value="claude-opus-4-6-oauth"),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"preferred_model": "claude-opus-4-6-oauth", "fallback_models": []},
            ),
        ),
        patch.object(fb_mod, "model_resolves", create_autospec(fb_mod.model_resolves, return_value=True)),
        patch.object(fb_mod, "resolve_llm_config", create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            from_service=True,
            is_server_retry=False,
            requested_model=None,
            held_model="claude-opus-4-6-oauth",
            resolve=lambda model_name: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, model_name, False
            ),
            flagged_subscription=None,
            retry_of_run_id=None,
        )
    assert exc_info.value.detail["type"] == "oauth_reauth_required"
    assert None not in resolved_calls


@pytest.mark.asyncio
async def test_restart_resume_carries_execution_link_for_model_resolution():
    """restart.resume_verdict + resume_turn carry explicit automation_delivery id into model resolution."""
    from datetime import datetime, timedelta, timezone
    from src.server.contracts.status import RESTART_INTERRUPTED_META
    from src.server.database.runs import restart_resume as rr_db
    from src.server.services.report_back import notify_turn, restart

    job = {
        "hook_outbox_id": "job-rr-1",
        "attempts": 1,
        "conversation_thread_id": _THREAD,
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=30),
        "payload": {"run_id": _RUN},
    }
    state = {
        "user_id": _USER,
        "workspace_id": _WS,
        "no_turn": False,
        "latest_run_id": _RUN,
        "status": "cancelled",
        "metadata": {
            RESTART_INTERRUPTED_META: True,
            "automation_execution_id": _EXEC,
            "automation_id": _AUTO,
        },
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=2),
    }

    with (
        patch.object(
            rr_db,
            "read_resume_state",
            create_autospec(rr_db.read_resume_state, return_value=state),
        ),
        patch.object(
            rr_db,
            "list_restart_stopped_tasks",
            create_autospec(rr_db.list_restart_stopped_tasks, return_value=[]),
        ),
        patch.object(
            rr_db,
            "has_unsettled_tasks",
            create_autospec(rr_db.has_unsettled_tasks, return_value=False),
        ),
        patch.object(
            notify_turn,
            "_card_waits",
            create_autospec(notify_turn._card_waits, return_value=False),
        ),
    ):
        owed = await restart.resume_verdict(job)

    assert isinstance(owed, restart.OwedResume)
    assert owed.automation_execution_id == _EXEC
    route, body = restart.resume_turn(owed, "00000000-0000-4000-8000-000000000099")
    assert route == "messages"
    assert body["automation_delivery"] == {"id": _EXEC}

    # Verify ChatRequest preserves automation_delivery and resolves deterministically even if execution already settled.
    req = ChatRequest(**body)
    settled_exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "interrupted",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    records: list[dict] = []
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=settled_exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={"preferred_model": "claude-opus-4-6-oauth", "fallback_models": ["gemini-3-flash"]},
            ),
        ),
        patch.object(fb_mod, "model_resolves", create_autospec(fb_mod.model_resolves, return_value=True)),
        patch.object(fb_mod, "resolve_llm_config", create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve)),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            from_service=True,
            is_server_retry=False,
            requested_model=None,
            held_model=None,
            resolve=lambda model_name: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, model_name, False
            ),
            flagged_subscription=None,
            retry_of_run_id=None,
            on_fallback=records.append,
        )
    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1 and records[0]["execution_id"] == _EXEC


@pytest.mark.asyncio
async def test_settle_unresumed_writes_aggregated_fallback_notice_excerpt():
    """settle_unresumed forwards aggregated fallback notice into INTERRUPTED settlement excerpt."""
    notice = "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    running_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "user_id": _USER,
        "conversation_thread_id": _THREAD,
        "workspace_id": _WS,
    }
    latest_run = {
        "conversation_response_id": _RUN,
        "metadata": {"automation_execution_id": _EXEC},
    }
    automation = {"automation_id": _AUTO, "user_id": _USER, "workspace_id": _WS}

    from src.server.database.runs import lifecycle as tl_db

    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    with (
        patch.object(
            automation_settlement.exec_db,
            "get_running_execution_for_run",
            create_autospec(exec_db.get_running_execution_for_run, return_value=running_row),
        ),
        patch.object(
            tl_db,
            "get_latest_attempt",
            create_autospec(tl_db.get_latest_attempt, return_value=latest_run),
        ),
        patch.object(
            automation_settlement.auto_db,
            "get_automation",
            create_autospec(automation_settlement.auto_db.get_automation, return_value=automation),
        ),
        patch.object(
            automation_settlement.exec_db,
            "get_settling_run",
            create_autospec(exec_db.get_settling_run, return_value=latest_run),
        ),
        patch.object(
            fb_mod,
            "get_execution_fallback_notice",
            create_autospec(fb_mod.get_execution_fallback_notice, return_value=notice),
        ),
        patch.object(automation_settlement, "settle", settle_mock),
    ):
        await automation_settlement.settle_unresumed(_RUN)

    settle_mock.assert_awaited_once()
    assert settle_mock.await_args.args[2] is automation_settlement.Outcome.INTERRUPTED
    assert settle_mock.await_args.kwargs["excerpt"] == notice


@pytest.mark.asyncio
async def test_report_source_parent_with_only_automation_delivery_id_matches_execution():
    """Explicit command_report / task report_source finds execution when parent response only holds automation_delivery.id."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor
    from src.server.models.chat import CommandReport, TaskReport

    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "started_at": None,
        "created_at": None,
        "user_id": _USER,
    }
    captured_sqls: list[str] = []

    def _make_conn(row_to_return: dict | None):
        cur = create_autospec(AsyncCursor, instance=True)

        async def _exec(sql: str, _params=None):
            captured_sqls.append(" ".join(sql.split()))

        cur.execute.side_effect = _exec
        cur.fetchone.return_value = row_to_return

        @asynccontextmanager
        async def _cur_ctx(*_a, **_kw):
            yield cur

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.side_effect = _cur_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield conn

        return _conn_ctx

    with patch.object(
        atm_mod,
        "get_db_connection",
        create_autospec(atm_mod.get_db_connection, side_effect=_make_conn(exec_row)),
    ):
        cmd_exec = await atm_mod._get_execution_for_command(_THREAD, "cmd-delivery-only")
    assert cmd_exec == exec_row
    assert "automation_delivery'->>'id'" in captured_sqls[-1]
    assert "split_part" in captured_sqls[-1]

    with patch.object(
        atm_mod,
        "get_db_connection",
        create_autospec(atm_mod.get_db_connection, side_effect=_make_conn(exec_row)),
    ):
        task_exec = await atm_mod._get_execution_for_task(
            _THREAD, task_run_id=_RUN, task_id="task-1"
        )
    assert task_exec == exec_row
    assert "automation_delivery'->>'id'" in captured_sqls[-1]
    assert "split_part" in captured_sqls[-1]

    # End-to-end inheritance + subscription fallback via CommandReport and TaskReport.
    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name))

    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gemini-3-flash"],
    }
    for source in (
        CommandReport(
            kind="command_report",
            command_id="cmd-delivery-only",
            tool="Bash",
            status="completed",
        ),
        TaskReport(
            kind="task",
            task_id="task-1",
            display_id="t-1",
            task_run_id=_RUN,
        ),
    ):
        req = ChatRequest(
            workspace_id=_WS,
            messages=[ChatMessage(role="user", content="<system>done</system>")],
            query_type="system",
            report_source=source,
        )
        records: list[dict] = []
        with (
            _catalog_and_classify(
                {
                    "claude-opus-4-6-oauth": "claude-oauth",
                    "gemini-3-flash": "google",
                }
            ),
            patch.object(
                atm_mod,
                "get_db_connection",
                create_autospec(atm_mod.get_db_connection, side_effect=_make_conn(exec_row)),
            ),
            patch.object(
                atm_mod,
                "_get_execution_first_turn_llm_model",
                create_autospec(
                    atm_mod._get_execution_first_turn_llm_model,
                    return_value="claude-opus-4-6-oauth",
                ),
            ),
            patch.object(
                fb_mod.user_models,
                "get_model_preference",
                create_autospec(user_models.get_model_preference, return_value=prefs),
            ),
            patch.object(
                fb_mod,
                "model_resolves",
                create_autospec(fb_mod.model_resolves, return_value=True),
            ),
            patch.object(
                fb_mod,
                "resolve_llm_config",
                create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
            ),
        ):
            cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
                user_id=_USER,
                thread_id=_THREAD,
                request=req,
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
                resolve=lambda m: fb_mod.resolve_llm_config(
                    _make_agent_config("base"), _USER, m, False
                ),
                flagged_subscription=None,
                on_fallback=records.append,
            )
        assert cfg.llm.name == "gemini-3-flash"
        assert len(records) == 1 and records[0]["execution_id"] == _EXEC


@pytest.mark.asyncio
async def test_notice_uses_actual_rejected_model_when_stale_held_model_cleared():
    """When stale held_model is cleared by turn_model() and default fails, fallback notice names the rejected default model."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gemini-3-flash"],
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    resolved_models: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_models.append(model_name)
        if model_name is None:
            # Account default resolves to claude-opus-4-6-oauth, whose subscription is expired.
            raise _reauth_exc("claude-oauth", "claude-opus-4-6-oauth")
        return _make_agent_config(str(model_name))

    records: list[dict] = []
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(atm_mod._get_execution_first_turn_llm_model, return_value=None),
        ),
        patch.object(
            atm_mod.auto_db,
            "get_automation_including_deleted",
            create_autospec(atm_mod.auto_db.get_automation_including_deleted, return_value=None),
        ),
        patch.object(
            user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            thread_model,
            "model_resolves",
            create_autospec(
                thread_model.model_resolves,
                side_effect=lambda _p, name: name != "removed-model-x",
            ),
        ),
        patch.object(
            thread_model.threads_write,
            "clear_thread_llm_model",
            create_autospec(
                thread_model.threads_write.clear_thread_llm_model, return_value=True
            ),
        ) as clear_mock,
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(
                fb_mod.model_resolves,
                side_effect=lambda _p, name: name != "removed-model-x",
            ),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="removed-model-x",
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
            on_fallback=records.append,
        )

    clear_mock.assert_awaited_once_with(_THREAD, "removed-model-x")
    assert resolved_models == [None, "gemini-3-flash"]
    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1
    assert records[0]["original_model"] == "claude-opus-4-6-oauth"
    assert "removed-model-x" not in records[0]["notice"]
    assert records[0]["notice"].startswith("claude-opus-4-6-oauth 的登录已失效")


@pytest.mark.asyncio
async def test_quota_service_unavailable_503_always_propagates_even_with_other_candidates():
    """Production quota_service_unavailable() (detail.type='service_unavailable') never falls through to next candidate."""
    prefs = {
        "preferred_model": "claude-opus-4-6-oauth",
        "fallback_models": ["gpt-5", "gemini-3-flash"],
    }
    attempted: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        attempted.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        if model_name == "gpt-5":
            raise quota_service_unavailable()
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gpt-5": "openai",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "claude-opus-4-6-oauth",
            False,
        )

    expected = quota_service_unavailable()
    assert exc_info.value.status_code == expected.status_code
    assert exc_info.value.detail == expected.detail
    assert attempted == ["claude-opus-4-6-oauth", "gpt-5"]


@pytest.mark.asyncio
async def test_flagged_subscription_notice_names_subscription_never_uninspected_model():
    """Flagged-subscription gate has no resolved model: notice names failed subscription, not requested/held model."""
    from src.server.services.oauth_providers import SUBSCRIPTIONS

    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    prefs = {
        "preferred_model": "gemini-2.5-pro",
        "fallback_models": ["claude-opus-4-6-oauth", "gemini-3-flash"],
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        llm_model="gemini-2.5-pro",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    records: list[dict] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-2.5-pro": "google",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model="gemini-2.5-pro",
            held_model="gpt-5",
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=(SUBSCRIPTIONS["claude-oauth"], "invalid_grant"),
            on_fallback=records.append,
        )

    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1
    assert records[0]["original_model"] == "Claude 订阅"
    assert "gemini-2.5-pro" not in records[0]["notice"]
    assert "gpt-5" not in records[0]["notice"]
    assert records[0]["notice"].startswith("Claude 订阅的登录已失效，本次改用 gemini-3-flash")


@pytest.mark.asyncio
async def test_plan_downgraded_default_notice_names_actual_effective_model():
    """When resolve(None) switches off-plan user pref to deployment default and fails, notice names deployment default."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    prefs = {
        "preferred_model": "off-plan-user-pref",
        "fallback_models": ["gemini-3-flash"],
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    records: list[dict] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        if model_name is None:
            # Simulates resolve_llm_config selecting deployment default after gate_models_by_plan.
            fb_mod.note_resolved_effective_model("deployment-default-oauth")
            raise _plan_no_api_exc("grok-oauth")
        return _make_agent_config(str(model_name))

    with (
        _catalog_and_classify(
            {
                "off-plan-user-pref": "openai",
                "deployment-default-oauth": "grok-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(atm_mod._get_execution_first_turn_llm_model, return_value=None),
        ),
        patch.object(
            atm_mod.auto_db,
            "get_automation_including_deleted",
            create_autospec(atm_mod.auto_db.get_automation_including_deleted, return_value=None),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value=None),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(user_models.get_model_preference, return_value=prefs),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model=None,
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
            on_fallback=records.append,
        )

    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1
    assert records[0]["original_model"] == "deployment-default-oauth"
    assert "off-plan-user-pref" not in records[0]["notice"]


@pytest.mark.asyncio
async def test_confirmed_execution_link_kept_when_model_or_pref_lookup_raises():
    """If reading first-turn model or prefs fails after execution id was confirmed, keep execution link and fall_back=False."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    resolved_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        return _make_agent_config(str(model_name or "account-default"))

    # Case A: first-turn model query raises -> fallback_eligible stays True, falls back via fallback_models.
    records: list[dict] = []
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                side_effect=RuntimeError("db read failed"),
            ),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value="claude-opus-4-6-oauth"),
        ),
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                return_value={
                    "preferred_model": "claude-opus-4-6-oauth",
                    "fallback_models": ["gemini-3-flash"],
                },
            ),
        ),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="claude-opus-4-6-oauth",
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
            on_fallback=records.append,
        )

    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1 and records[0]["execution_id"] == _EXEC
    assert None not in resolved_calls

    # Case B: pref read raises during context resolution, then empty fallback_models raises original error instead of account default.
    resolved_calls.clear()
    pref_mock = create_autospec(
        user_models.get_model_preference,
        side_effect=[RuntimeError("redis cache hiccup"), {"fallback_models": []}],
    )
    with (
        _catalog_and_classify({"claude-opus-4-6-oauth": "claude-oauth"}),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value="claude-opus-4-6-oauth"),
        ),
        patch.object(user_models, "get_model_preference", pref_mock),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="claude-opus-4-6-oauth",
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
        )
    assert exc_info.value.detail["type"] == "oauth_reauth_required"
    assert None not in resolved_calls


@pytest.mark.asyncio
async def test_settlement_skips_response_query_without_fallback_marker_and_scopes_by_thread():
    """Skip conversation_responses lookup when neither run nor execution has fallback marker; scope SQL by thread_id when queried."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor

    db_get = create_autospec(fb_mod.get_db_connection)
    with patch.object(fb_mod, "get_db_connection", db_get):
        notice_none_exec = await fb_mod.get_execution_fallback_notice(
            _EXEC,
            thread_id=_THREAD,
            run={"metadata": {"automation_execution_id": _EXEC}},
            exec_row={"automation_execution_id": _EXEC, "delivery": None},
        )
        notice_held_only = await fb_mod.get_execution_fallback_notice(
            _EXEC,
            thread_id=_THREAD,
            run={"metadata": {"automation_execution_id": _EXEC}},
            exec_row={
                "automation_execution_id": _EXEC,
                "delivery": {"id": _EXEC, "targets": []},
            },
        )

    assert notice_none_exec is None
    assert notice_held_only is None
    db_get.assert_not_called()

    # Verify mark_execution_subscription_fallback stamps delivery JSONB and marked exec queries scoped by thread_id.
    mock_cur = create_autospec(AsyncCursor, instance=True)
    executed: list[tuple[str, object]] = []

    async def _exec(sql: str, params=None) -> None:
        executed.append((" ".join(sql.split()), params))

    mock_cur.execute.side_effect = _exec
    expected_notice = "claude-opus-4-6-oauth 的登录已失效，本次改用 gemini-3-flash；请到设置里重新连接"
    mock_cur.fetchall.return_value = [
        {"metadata": {"subscription_fallback": {"notice": expected_notice}}}
    ]

    @asynccontextmanager
    async def _cur_ctx(*_a, **_kw):
        yield mock_cur

    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.cursor.side_effect = _cur_ctx

    @asynccontextmanager
    async def _conn_ctx():
        yield mock_conn

    await fb_mod.mark_execution_subscription_fallback(_EXEC, conn=mock_conn)
    assert "jsonb_set" in executed[0][0]
    assert "subscription_fallback" in executed[0][0]
    assert executed[0][1] == (_EXEC,)

    executed.clear()
    with patch.object(
        fb_mod,
        "get_db_connection",
        create_autospec(fb_mod.get_db_connection, side_effect=_conn_ctx),
    ):
        notice = await fb_mod.get_execution_fallback_notice(
            _EXEC,
            thread_id=_THREAD,
            run={"metadata": {"automation_execution_id": _EXEC}},
            exec_row={
                "automation_execution_id": _EXEC,
                "delivery": {"id": _EXEC, "targets": [], "subscription_fallback": True},
            },
        )

    assert notice == expected_notice
    assert "r.conversation_thread_id = %(tid)s::uuid" in executed[0][0]
    assert executed[0][1]["tid"] == _THREAD
    assert executed[0][1]["eid"] == _EXEC


@pytest.mark.asyncio
async def test_confirmed_link_preserves_first_turn_model_when_pref_fails_and_thread_has_no_held_model():
    """With confirmed execution and held_model=None, pref read failure preserves first-turn model and never falls back to resolve(None)."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    resolved_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_calls.append(model_name)
        if model_name == "claude-opus-4-6-oauth":
            raise _reauth_exc("claude-oauth", str(model_name))
        # Account default (model_name=None) would have succeeded if erroneously reached!
        return _make_agent_config(str(model_name or "healthy-account-default"))

    # Case A: pref read raises in _resolve_automation_notification_context, then succeeds in _try_subscription_fallback.
    records: list[dict] = []
    pref_mock_a = create_autospec(
        user_models.get_model_preference,
        side_effect=[
            RuntimeError("pref read temporary error"),
            {"fallback_models": ["gemini-3-flash"]},
        ],
    )
    with (
        _catalog_and_classify(
            {
                "claude-opus-4-6-oauth": "claude-oauth",
                "gemini-3-flash": "google",
            }
        ),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(user_models, "get_model_preference", pref_mock_a),
        patch.object(
            fb_mod,
            "model_resolves",
            create_autospec(fb_mod.model_resolves, return_value=True),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
    ):
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model=None,
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
            on_fallback=records.append,
        )

    assert resolved_calls == ["claude-opus-4-6-oauth", "gemini-3-flash"]
    assert None not in resolved_calls
    assert cfg.llm.name == "gemini-3-flash"
    assert len(records) == 1 and records[0]["original_model"] == "claude-opus-4-6-oauth"

    # Case B: pref read fails in both places with held_model=None -> original oauth_reauth_required propagates, never resolve(None).
    resolved_calls.clear()
    pref_mock_b = create_autospec(
        user_models.get_model_preference,
        side_effect=RuntimeError("pref backend offline"),
    )
    with (
        _catalog_and_classify({"claude-opus-4-6-oauth": "claude-oauth"}),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                return_value="claude-opus-4-6-oauth",
            ),
        ),
        patch.object(user_models, "get_model_preference", pref_mock_b),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model=None,
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
        )

    assert resolved_calls == ["claude-opus-4-6-oauth"]
    assert None not in resolved_calls
    assert exc_info.value.detail["type"] == "oauth_reauth_required"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_pref_load_failure_during_fallback_reraises_original_subscription_error():
    """Pref read failure inside subscription fallback re-raises original oauth_* error on both executor and notification turns."""
    # Executor first turn path: resolve_automation_turn_llm_config
    async def _resolve_first(_cfg, _uid, _m, *_a, **_kw):
        raise _plan_no_api_exc("grok-oauth")

    with (
        patch.object(
            fb_mod.user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                side_effect=RuntimeError("db timeout loading fallback_models"),
            ),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_first),
        ),
        pytest.raises(HTTPException) as exec_exc,
    ):
        await fb_mod.resolve_automation_turn_llm_config(
            _make_agent_config("base"),
            _USER,
            "grok-3-oauth",
            False,
        )
    assert exec_exc.value.detail["type"] == "oauth_plan_no_api_access"
    assert isinstance(exec_exc.value.__cause__, RuntimeError)

    # Notification turn path: _try_subscription_fallback when prefs=None
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    with (
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model, return_value=None
            ),
        ),
        patch.object(
            atm_mod.auto_db,
            "get_automation_including_deleted",
            create_autospec(atm_mod.auto_db.get_automation_including_deleted, return_value=None),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value="claude-opus-4-6-oauth"),
        ),
        patch.object(
            user_models,
            "get_model_preference",
            create_autospec(
                user_models.get_model_preference,
                side_effect=OSError("redis unavailable"),
            ),
        ),
        pytest.raises(HTTPException) as notif_exc,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model="claude-opus-4-6-oauth",
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda _m: _raise_async(_refresh_unavailable_exc("claude-oauth")),
            flagged_subscription=None,
        )
    assert notif_exc.value.detail["type"] == "oauth_refresh_unavailable"
    assert isinstance(notif_exc.value.__cause__, OSError)


async def _raise_async(exc: BaseException):
    raise exc


@pytest.mark.asyncio
async def test_rerun_stamp_delivery_preserves_fallback_marker_for_settlement():
    """Re-running an execution that calls stamp_delivery(stamp or None) preserves subscription_fallback marker for settlement."""
    from contextlib import asynccontextmanager
    from psycopg import AsyncConnection, AsyncCursor

    stored_exec: dict[str, object] = {"delivery": None, "status": "running"}
    sql_log: list[str] = []

    def _make_conn():
        cur = create_autospec(AsyncCursor, instance=True)

        async def _exec(sql: str, params=None) -> None:
            norm = " ".join(sql.split())
            sql_log.append(norm)
            if "jsonb_set" in norm and "subscription_fallback" in norm:
                base = dict(stored_exec["delivery"] or {})
                base["subscription_fallback"] = True
                stored_exec["delivery"] = base
            elif norm.startswith("UPDATE automation_executions SET delivery = CASE"):
                assert "WHEN delivery ? 'subscription_fallback'" in norm
                existing = stored_exec["delivery"]
                has_fb = isinstance(existing, dict) and "subscription_fallback" in existing
                if "|| jsonb_build_object" in norm:
                    new_stamp = params[0].obj
                    merged = dict(new_stamp)
                    if has_fb:
                        merged["subscription_fallback"] = existing["subscription_fallback"]
                    stored_exec["delivery"] = merged
                else:
                    stored_exec["delivery"] = (
                        {"subscription_fallback": existing["subscription_fallback"]}
                        if has_fb
                        else None
                    )

        cur.rowcount = 1
        cur.execute.side_effect = _exec

        @asynccontextmanager
        async def _cur_ctx(*_a, **_kw):
            yield cur

        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.side_effect = _cur_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield conn

        return conn, _conn_ctx

    conn_m, conn_ctx = _make_conn()
    await fb_mod.mark_execution_subscription_fallback(_EXEC, conn=conn_m)
    assert stored_exec["delivery"] == {"subscription_fallback": True}

    new_stamp = {"id": f"{_EXEC}.2", "targets": [{"app": "slack", "to": "C1"}]}
    with patch.object(
        exec_db,
        "get_db_connection",
        create_autospec(exec_db.get_db_connection, side_effect=conn_ctx),
    ):
        assert await exec_db.stamp_delivery(_EXEC, new_stamp) is True
        assert stored_exec["delivery"] == {**new_stamp, "subscription_fallback": True}
        assert await exec_db.stamp_delivery(_EXEC, None) is True
        assert stored_exec["delivery"] == {"subscription_fallback": True}

    # Settling after re-run still recognizes the fallback marker on exec_row.
    assert fb_mod.execution_has_fallback_marker(stored_exec) is True


@pytest.mark.asyncio
async def test_confirmed_link_with_unreadable_model_and_no_thread_model_stops_the_turn():
    """Model of a confirmed execution unreadable, nothing else names one: the turn stops
    (503 automation_model_unavailable) instead of running on the account default."""
    exec_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "user_id": _USER,
    }
    req = ChatRequest(
        workspace_id=_WS,
        messages=[ChatMessage(role="user", content="<system>done</system>")],
        query_type="system",
        automation_delivery={"id": _EXEC, "targets": []},
    )
    resolved_calls: list[str | None] = []

    async def _resolve(_cfg, _uid, model_name, *_args, **_kwargs):
        resolved_calls.append(model_name)
        return _make_agent_config(str(model_name or "account-default"))

    with (
        _catalog_and_classify({"gemini-3-flash": "google"}),
        patch.object(
            atm_mod,
            "_get_execution_by_id",
            create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
        ),
        patch.object(
            atm_mod,
            "_get_execution_first_turn_llm_model",
            create_autospec(
                atm_mod._get_execution_first_turn_llm_model,
                side_effect=RuntimeError("db read failed"),
            ),
        ),
        patch.object(
            thread_model,
            "turn_model",
            create_autospec(thread_model.turn_model, return_value=None),
        ),
        patch.object(
            fb_mod,
            "resolve_llm_config",
            create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=_USER,
            thread_id=_THREAD,
            request=req,
            requested_model=None,
            held_model=None,
            from_service=True,
            is_server_retry=False,
            retry_of_run_id=None,
            resolve=lambda m: fb_mod.resolve_llm_config(
                _make_agent_config("base"), _USER, m, False
            ),
            flagged_subscription=None,
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["type"] == "automation_model_unavailable"
    assert resolved_calls == []


_DEPLOYMENT_DEFAULT_FALLBACKS = [
    "gemini-4-neon-high-oauth",
    "claude-opus-5-5-oauth",
    "gpt-6.2-sol-oauth",
    "grok-4.7-oauth",
    "muse-wonder-1.0-oauth",
    "deepseek-flash",
]

_DEPLOYMENT_DEFAULT_PROVIDERS = {
    "gemini-4-neon-high-oauth": "antigravity-oauth",
    "claude-opus-5-5-oauth": "claude-oauth",
    "gpt-6.2-sol-oauth": "codex-oauth",
    "grok-4.7-oauth": "xai-oauth",
    "muse-wonder-1.0-oauth": "muse-oauth",
    "deepseek-flash": "deepseek",
}


def _make_deployment_llm(fallback: list[str] | None) -> LLMConfig:
    llm_cfg = create_autospec(LLMConfig, instance=True)
    llm_cfg.fallback = fallback
    return llm_cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("prefs", [{}, {"fallback_models": None}], ids=["missing_key", "none_value"])
async def test_subscription_fallback_uses_deployment_defaults_in_order_when_unset(prefs):
    """Unset fallback_models (absent or None) walks deployment llm.fallback in order, skipping failed provider."""
    attempted: list[str] = []

    async def _resolve_candidate(model_name: str):
        attempted.append(model_name)
        if model_name != "deepseek-flash":
            raise HTTPException(
                status_code=400,
                detail={"message": f"{model_name} unavailable", "type": "oauth_required"},
            )
        return _make_agent_config(model_name)

    resolve_mock = create_autospec(_resolve_candidate, side_effect=_resolve_candidate)
    resolves_mock = create_autospec(fb_mod.model_resolves, return_value=True)
    deploy_llm_mock = create_autospec(
        fb_mod.deployment_llm,
        return_value=_make_deployment_llm(_DEPLOYMENT_DEFAULT_FALLBACKS),
    )

    with (
        _catalog_and_classify(_DEPLOYMENT_DEFAULT_PROVIDERS),
        patch.object(fb_mod, "deployment_llm", deploy_llm_mock),
    ):
        cfg, record = await fb_mod.resolve_with_subscription_fallback(
            user_id=_USER,
            prefs=prefs,
            original_model="claude-opus-5-5-oauth",
            failed_provider="claude-oauth",
            reason="oauth_reauth_required",
            resolve_candidate=resolve_mock,
            exc_to_raise=_reauth_exc("claude-oauth", "claude-opus-5-5-oauth"),
            model_resolves_fn=resolves_mock,
        )

    assert cfg.llm.name == "deepseek-flash"
    assert record["fallback_model"] == "deepseek-flash"
    # claude-opus-5-5-oauth skipped (matches original_model & failed_provider); remaining 5 tried in order.
    assert attempted == [
        "gemini-4-neon-high-oauth",
        "gpt-6.2-sol-oauth",
        "grok-4.7-oauth",
        "muse-wonder-1.0-oauth",
        "deepseek-flash",
    ]


@pytest.mark.asyncio
async def test_subscription_fallback_skips_same_provider_items_from_deployment_defaults():
    """Deployment default entries on the failed provider are skipped even when model name differs."""
    attempted: list[str] = []

    async def _resolve_candidate(model_name: str):
        attempted.append(model_name)
        return _make_agent_config(model_name)

    resolve_mock = create_autospec(_resolve_candidate, side_effect=_resolve_candidate)
    resolves_mock = create_autospec(fb_mod.model_resolves, return_value=True)
    deploy_llm_mock = create_autospec(
        fb_mod.deployment_llm,
        return_value=_make_deployment_llm(_DEPLOYMENT_DEFAULT_FALLBACKS),
    )

    with (
        _catalog_and_classify(
            {
                **_DEPLOYMENT_DEFAULT_PROVIDERS,
                "gemini-4-carbon-medium-oauth": "antigravity-oauth",
            }
        ),
        patch.object(fb_mod, "deployment_llm", deploy_llm_mock),
    ):
        cfg, record = await fb_mod.resolve_with_subscription_fallback(
            user_id=_USER,
            prefs={},
            original_model="gemini-4-carbon-medium-oauth",
            failed_provider="antigravity-oauth",
            reason="oauth_reauth_required",
            resolve_candidate=resolve_mock,
            exc_to_raise=_reauth_exc("antigravity-oauth", "gemini-4-carbon-medium-oauth"),
            model_resolves_fn=resolves_mock,
        )

    assert cfg.llm.name == "claude-opus-5-5-oauth"
    assert record["fallback_model"] == "claude-opus-5-5-oauth"
    # gemini-4-neon-high-oauth shares failed_provider antigravity-oauth and must be skipped.
    assert attempted == ["claude-opus-5-5-oauth"]


@pytest.mark.asyncio
async def test_subscription_fallback_explicit_empty_list_disables_deployment_defaults():
    """An explicitly saved empty fallback_models=[] means no fallback and must not read deployment defaults."""
    attempted: list[str] = []

    async def _resolve_candidate(model_name: str):
        attempted.append(model_name)
        return _make_agent_config(model_name)

    resolve_mock = create_autospec(_resolve_candidate, side_effect=_resolve_candidate)
    resolves_mock = create_autospec(fb_mod.model_resolves, return_value=True)
    deploy_llm_mock = create_autospec(
        fb_mod.deployment_llm,
        return_value=_make_deployment_llm(_DEPLOYMENT_DEFAULT_FALLBACKS),
    )
    exc = _reauth_exc("claude-oauth", "claude-opus-5-5-oauth")

    with (
        _catalog_and_classify(_DEPLOYMENT_DEFAULT_PROVIDERS),
        patch.object(fb_mod, "deployment_llm", deploy_llm_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await fb_mod.resolve_with_subscription_fallback(
            user_id=_USER,
            prefs={"fallback_models": []},
            original_model="claude-opus-5-5-oauth",
            failed_provider="claude-oauth",
            reason="oauth_reauth_required",
            resolve_candidate=resolve_mock,
            exc_to_raise=exc,
            model_resolves_fn=resolves_mock,
        )

    assert exc_info.value is exc
    assert attempted == []
    deploy_llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_subscription_fallback_uses_only_user_saved_list_without_deployment_defaults():
    """When the user saved their own fallback_models, only those entries are tried (never deployment defaults)."""
    attempted: list[str] = []

    async def _resolve_candidate(model_name: str):
        attempted.append(model_name)
        raise HTTPException(
            status_code=400,
            detail={"message": "unavailable", "type": "oauth_required"},
        )

    resolve_mock = create_autospec(_resolve_candidate, side_effect=_resolve_candidate)
    resolves_mock = create_autospec(fb_mod.model_resolves, return_value=True)
    deploy_llm_mock = create_autospec(
        fb_mod.deployment_llm,
        return_value=_make_deployment_llm(_DEPLOYMENT_DEFAULT_FALLBACKS),
    )
    exc = _reauth_exc("claude-oauth", "claude-opus-5-5-oauth")

    with (
        _catalog_and_classify(_DEPLOYMENT_DEFAULT_PROVIDERS),
        patch.object(fb_mod, "deployment_llm", deploy_llm_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await fb_mod.resolve_with_subscription_fallback(
            user_id=_USER,
            prefs={"fallback_models": ["gpt-6.2-sol-oauth"]},
            original_model="claude-opus-5-5-oauth",
            failed_provider="claude-oauth",
            reason="oauth_reauth_required",
            resolve_candidate=resolve_mock,
            exc_to_raise=exc,
            model_resolves_fn=resolves_mock,
        )

    assert exc_info.value is exc
    assert attempted == ["gpt-6.2-sol-oauth"]
    deploy_llm_mock.assert_not_called()



def test_deployment_default_fallbacks_fixture_matches_dalton_config():
    """The fixture above mirrors agent_config.dalton.yaml, so the order tests pin the real list."""
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load((Path(__file__).resolve().parents[3] / "agent_config.dalton.yaml").read_text())
    assert cfg["llm"]["fallback"] == _DEPLOYMENT_DEFAULT_FALLBACKS


@pytest.mark.asyncio
async def test_subscription_fallback_tries_all_six_deployment_defaults_in_order():
    """A failed provider outside the list leaves all six defaults to try, in configured order."""
    attempted: list[str] = []

    async def _resolve_candidate(model_name: str):
        attempted.append(model_name)
        raise HTTPException(
            status_code=400,
            detail={"message": f"{model_name} unavailable", "type": "oauth_required"},
        )

    resolve_mock = create_autospec(_resolve_candidate, side_effect=_resolve_candidate)
    resolves_mock = create_autospec(fb_mod.model_resolves, return_value=True)
    deploy_llm_mock = create_autospec(
        fb_mod.deployment_llm,
        return_value=_make_deployment_llm(_DEPLOYMENT_DEFAULT_FALLBACKS),
    )
    exc = _reauth_exc("claude-oauth", "claude-sonnet-5-5-oauth")

    with (
        _catalog_and_classify(
            {**_DEPLOYMENT_DEFAULT_PROVIDERS, "glm-5.3-plan": "z-ai-cn-coding"}
        ),
        patch.object(fb_mod, "deployment_llm", deploy_llm_mock),
    ):
        with pytest.raises(HTTPException):
            await fb_mod.resolve_with_subscription_fallback(
                user_id=_USER,
                prefs={},
                original_model="glm-5.3-plan",
                failed_provider="z-ai-cn-coding",
                reason="oauth_reauth_required",
                resolve_candidate=resolve_mock,
                exc_to_raise=exc,
                model_resolves_fn=resolves_mock,
            )

    assert attempted == _DEPLOYMENT_DEFAULT_FALLBACKS
