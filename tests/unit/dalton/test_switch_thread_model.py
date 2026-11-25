"""Tests for `switch_thread_model` Dalton tool and shared `thread_model.switch_thread_model` service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage

from ptc_agent.agent.agent import PTCAgent
from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from src.dalton.database import model_switch as model_switch_db
from src.dalton.tools import build_dalton_tools
from src.dalton.tools.thread_model_tools import create_thread_model_tools
from src.llms.llm import ModelConfig
from src.server.database.conversation import threads_read, threads_write
from src.server.handlers import handoff_brief, thread_maintenance, thread_model_handoff
from src.server.services.llm import thread_model as thread_model_svc
from src.server.services.llm import user_models
from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app
from tests.unit.server.access_stub import as_owner

TOOL_MOD = "src.dalton.tools.thread_model_tools"
SVC_MOD = "src.server.services.llm.thread_model"
CRUD_MOD = "src.server.app.threads.crud"
HANDOFF_MOD = "src.server.handlers.thread_model_handoff"
MODEL_SWITCH_DB_MOD = "src.dalton.database.model_switch"

USER_ID = "usr-switch-001"
THREAD_ID = "11111111-1111-1111-1111-111111111111"
WS_ID = "ws-switch-001"


def _make_catalog(entries: dict[str, dict[str, Any]]) -> ModelConfig:
    mc = create_autospec(ModelConfig, instance=True)
    mc.llm_config = entries
    mc.get_model_config.side_effect = lambda name: entries.get(name)
    mc.get_provider_info.side_effect = lambda prov: (
        {"sdk": "claude-code"} if prov == "claude-code" else {}
    )
    mc.listed_models.return_value = [
        (name, info) for name, info in entries.items() if info.get("visible", False)
    ]
    return mc


def _user_config(
    *,
    origin: str | None = "user",
    checkpoint_ns: str = "",
    wake_turn: bool = False,
    user_id: str | None = USER_ID,
    thread_id: str | None = THREAD_ID,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {}
    if user_id is not None:
        configurable["user_id"] = user_id
    if thread_id is not None:
        configurable["thread_id"] = thread_id
    if checkpoint_ns:
        configurable["checkpoint_ns"] = checkpoint_ns
    if wake_turn:
        configurable["wake_turn"] = True
    metadata: dict[str, Any] = {}
    if origin is not None:
        metadata["origin"] = origin
    return {"configurable": configurable, "metadata": metadata}


def _thread_row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    row: dict[str, Any] = {
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS_ID,
        "current_status": "completed",
        "msg_type": "ptc",
        "thread_index": 0,
        "title": "Thread",
        "llm_model": "gemini-4-neon-high-oauth",
        "subagents_allowed": True,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _tool():
    return create_thread_model_tools()[0]


# ---------------------------------------------------------------------------
# 1. Validation failures do NOT write to DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_model_refused_without_db_write():
    catalog = _make_catalog(
        {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True, "provider": "gemini"},
            "gemini-4-neon-high-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
        patch.object(threads_write, "update_thread_fields", new=update_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "nonexistent-model"}, config=_user_config()
        )

    assert "未找到模型" in result
    assert "gemini-4-neon" in result
    switch_spy.assert_not_awaited()
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_display_name_refused_with_candidates():
    catalog = _make_catalog(
        {
            "claude-opus-5-5": {"display_name": "Claude Opus 5.5", "visible": True},
            "claude-opus-5-5-oauth": {"display_name": "Claude Opus 5.5", "visible": True},
        }
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "claude opus 5.5"}, config=_user_config()
        )

    assert "匹配到多个模型" in result
    assert "claude-opus-5-5" in result
    assert "claude-opus-5-5-oauth" in result
    switch_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_unselectable_model_refused_without_db_write():
    catalog = _make_catalog(
        {
            "gemini-4-neon-high-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    require_mock = create_autospec(
        thread_model_svc.require_selectable_model,
        side_effect=HTTPException(
            status_code=400,
            detail={"type": "model_unavailable", "message": "Model 'gemini-4-neon-high-oauth' is not available."},
        ),
    )
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth"}, config=_user_config()
        )

    assert "切换模型失败" in result
    require_mock.assert_awaited_once_with(USER_ID, "gemini-4-neon-high-oauth")
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_unselectable_model_raises_without_db_write():
    catalog = _make_catalog({"m-live": {"visible": True}})
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch(f"{SVC_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        pytest.raises(HTTPException) as exc,
    ):
        await thread_model_svc.switch_thread_model(USER_ID, THREAD_ID, "not-in-catalog")

    assert exc.value.status_code == 400
    assert exc.value.detail["type"] == "model_unavailable"
    update_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Non-user turns rejected (matches empty_call_retry._user_sent_turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cfg",
    [
        _user_config(origin="agent"),
        _user_config(origin="automation"),
        _user_config(origin="system", wake_turn=True),
        _user_config(origin="system"),
        _user_config(origin="user", checkpoint_ns="task:subagent-1"),
        _user_config(origin=None),
    ],
    ids=["agent-dispatch", "automation", "wakeup", "report-back", "subagent", "unknown-origin"],
)
async def test_non_user_turn_is_refused(cfg):
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    with patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth"}, config=cfg
        )

    assert "拒绝切换模型" in result
    switch_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_steered_notification_turn_is_allowed():
    catalog = _make_catalog(
        {"gemini-4-neon-high-oauth": {"visible": True}}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(
        thread_model_svc.switch_thread_model,
        return_value=thread_model_svc.ThreadModelSwitchResult(
            thread=_thread_row(),
            workspace_id=WS_ID,
            previous_model="m-old",
            model="gemini-4-neon-high-oauth",
            changed=True,
        ),
    )
    steer_msg = HumanMessage(
        content="[Steering from User]\n换成 gemini-4-neon-high-oauth",
        additional_kwargs={"lc_source": "steering", "steering_delivered": {"count": 1}},
    )

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth", "state": {"messages": [steer_msg]}},
            config=_user_config(origin="system", wake_turn=True),
        )

    assert "已切换到 gemini-4-neon-high-oauth" in result
    switch_spy.assert_awaited_once_with(
        USER_ID,
        THREAD_ID,
        "gemini-4-neon-high-oauth",
        defer_handoff=True,
        model_pref={},
        short_circuit_default=True,
        running_model=None,
    )


# ---------------------------------------------------------------------------
# 3. Same model does NOT trigger handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_same_model_skips_write_and_handoff():
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={"llm_model": "gemini-4-neon-high-oauth", "workspace_id": WS_ID},
    )
    update_spy = create_autospec(threads_write.update_thread_fields)
    brief_spy = create_autospec(handoff_brief.brief_on_switch)
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch)

    with (
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch("src.server.handlers.handoff_brief.brief_on_switch", new=brief_spy),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
    ):
        res = await thread_model_svc.switch_thread_model(
            USER_ID, THREAD_ID, "gemini-4-neon-high-oauth"
        )

    assert res.changed is False
    update_spy.assert_not_awaited()
    brief_spy.assert_not_awaited()
    handoff_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_same_model_returns_already_on_model_message():
    catalog = _make_catalog(
        {"gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True}}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(
        thread_model_svc.switch_thread_model,
        return_value=thread_model_svc.ThreadModelSwitchResult(
            thread=None,
            workspace_id=WS_ID,
            previous_model="gemini-4-neon",
            model="gemini-4-neon",
            changed=False,
        ),
    )

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "Gemini 4 Neon"}, config=_user_config()
        )

    assert "已在使用模型" in result
    assert "Gemini 4 Neon (gemini-4-neon)" in result
    switch_spy.assert_awaited_once_with(
        USER_ID,
        THREAD_ID,
        "gemini-4-neon",
        defer_handoff=True,
        model_pref={},
        short_circuit_default=True,
        running_model=None,
    )


# ---------------------------------------------------------------------------
# 4. Normal switch calls service once; PATCH route uses the same service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_normal_switch_calls_service_once_with_display_name_match():
    catalog = _make_catalog(
        {"gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True}}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(
        thread_model_svc.switch_thread_model,
        return_value=thread_model_svc.ThreadModelSwitchResult(
            thread=_thread_row(llm_model="gemini-4-neon"),
            workspace_id=WS_ID,
            previous_model="claude-sonnet-5-5",
            model="gemini-4-neon",
            changed=True,
            summary_handoff=False,
        ),
    )

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "gemini 4 neon"}, config=_user_config()
        )

    switch_spy.assert_awaited_once_with(
        USER_ID,
        THREAD_ID,
        "gemini-4-neon",
        defer_handoff=True,
        model_pref={},
        short_circuit_default=True,
        running_model=None,
    )
    assert result == (
        "已切换到 Gemini 4 Neon (gemini-4-neon)，从用户的下一条消息起生效；本轮仍由当前模型完成。"
    )


# Removed obsolete test_patch_route_uses_shared_switch_validation_and_handoff (PATCH restored to upstream behavior)


# ---------------------------------------------------------------------------
# 5. Main-agent-only tool mounting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["chief_of_staff", "analyst"])
def test_switch_thread_model_mounted_for_both_main_agent_roles(role):
    names = {t.name for t in build_dalton_tools(role)}
    assert "switch_thread_model" in names


def test_switch_thread_model_only_on_main_agent_not_subagents():
    main_calls: list[dict[str, Any]] = []
    subagent_calls: list[dict[str, Any]] = []

    def capture_create_agent(model: Any, **kw: Any) -> Any:
        main_calls.append(kw)
        graph = create_autospec(PTCAgent, instance=True)
        graph.with_config = create_autospec(lambda *_a, **_kw: graph)
        return graph

    def capture_task_middleware(*args: Any, **kw: Any) -> Any:
        subagent_calls.append(kw)
        return None

    config = AgentConfig(
        llm=LLMConfig(name="claude-fable-5-2"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    from ptc_agent.core.mcp_registry import MCPRegistry
    from ptc_agent.core.paths import SandboxLayout
    from ptc_agent.core.sandbox import PTCSandbox

    fake_llm = GenericFakeChatModel(messages=iter([]))
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.config = config
    sandbox.sandbox_id = "sb-1"
    sandbox.working_dir = "/home/workspace"
    sandbox.layout = SandboxLayout("/home/workspace")
    sandbox.livefs = None
    sandbox.mounts_livefs = True
    sandbox.history = None
    sandbox.workspace.return_value = sandbox.layout.for_workspace(None)

    mcp_registry = create_autospec(MCPRegistry, instance=True)
    mcp_registry.connectors = {}
    mcp_registry.get_all_tools.return_value = {}

    with (
        patch.object(AgentConfig, "get_llm_client", return_value=fake_llm),
        patch(
            "ptc_agent.agent.middleware.compaction.compact.create_llm",
            return_value=fake_llm,
        ),
        patch("ptc_agent.agent.agent.create_agent", side_effect=capture_create_agent),
        patch("ptc_agent.agent.agent.task_middleware", side_effect=capture_task_middleware),
    ):
        PTCAgent(config).create_agent(
            llm=fake_llm,
            thread_id=THREAD_ID,
            sandbox=sandbox,
            mcp_registry=mcp_registry,
            user_id=USER_ID,
            tool_summary="",
        )

    main_tool_names = {t.name for t in main_calls[0]["tools"]}
    subagent_tool_names = {t.name for t in subagent_calls[0]["tools"]}

    assert "switch_thread_model" in main_tool_names
    assert "switch_thread_model" not in subagent_tool_names


# ---------------------------------------------------------------------------
# 6. Round 2 fixes: disabled runtime, sharing, default match, deferred handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_claude_code_runtime_refused_without_db_write():
    catalog = _make_catalog(
        {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True},
            "claude-sonnet-4-6-code": {
                "display_name": "Claude Sonnet 4.6 (Claude Code)",
                "visible": True,
                "provider": "claude-code",
            },
        }
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.rows_available", return_value=False),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
        patch.object(threads_write, "update_thread_fields", new=update_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "claude-sonnet-4-6-code"}, config=_user_config()
        )

    assert "在当前服务中未启用" in result
    assert "gemini-4-neon" in result
    assert "可选模型：claude-sonnet-4-6-code" not in result
    switch_spy.assert_not_awaited()
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_publicly_shared_personal_thread_can_switch_model():
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": True,
            "workspace_id": WS_ID,
            "llm_model": "m-old",
            "metadata": {},
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="m-new", is_shared=True),
    )
    mark_spy = create_autospec(model_switch_db.set_thread_metadata_key, return_value=True)

    with (
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.set_thread_metadata_key", new=mark_spy),
    ):
        res = await thread_model_svc.switch_thread_model(
            USER_ID, THREAD_ID, "m-new", defer_handoff=True
        )

    assert res.changed is True
    assert update_spy.await_count == 1
    assert update_spy.await_args.args == (THREAD_ID,)
    assert update_spy.await_args.kwargs["llm_model"] == "m-new"
    marker = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker["previous_model"] == "m-old"
    assert marker["user_id"] == USER_ID
    assert isinstance(marker["version"], str) and len(marker["version"]) == 32
    mark_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_workspace_thread_refused():
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": "shared-holder-1",
            "workspace_owner_id": "shared-holder-1",
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": "m-old",
            "metadata": {},
        },
    )
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        pytest.raises(HTTPException) as exc,
    ):
        await thread_model_svc.switch_thread_model(
            USER_ID, THREAD_ID, "m-new", defer_handoff=True
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["type"] == "shared_thread_setting"
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_following_default_matching_target_short_circuits_without_write():
    catalog = _make_catalog(
        {"gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True}}
    )
    pref = {"preferred_model": "gemini-4-neon"}
    get_pref = create_autospec(user_models.get_model_preference, return_value=pref)
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": None,
            "metadata": {},
        },
    )
    update_spy = create_autospec(threads_write.update_thread_fields)
    mark_spy = create_autospec(model_switch_db.set_thread_metadata_key)
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.set_thread_metadata_key", new=mark_spy),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "Gemini 4 Neon"}, config=_user_config()
        )

    assert "已在使用模型" in result
    update_spy.assert_not_awaited()
    mark_spy.assert_not_awaited()
    handoff_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_defers_handoff_and_records_pending_metadata():
    catalog = _make_catalog(
        {"gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True}}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": "claude-sonnet-5-5",
            "metadata": {},
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="gemini-4-neon"),
    )
    mark_spy = create_autospec(model_switch_db.set_thread_metadata_key, return_value=True)
    brief_spy = create_autospec(handoff_brief.brief_on_switch)
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.set_thread_metadata_key", new=mark_spy),
        patch("src.server.handlers.handoff_brief.brief_on_switch", new=brief_spy),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "Gemini 4 Neon"}, config=_user_config()
        )

    assert "已切换到 Gemini 4 Neon (gemini-4-neon)" in result
    assert update_spy.await_count == 1
    assert update_spy.await_args.args == (THREAD_ID,)
    assert update_spy.await_args.kwargs["llm_model"] == "gemini-4-neon"
    marker = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker["previous_model"] == "claude-sonnet-5-5"
    assert marker["user_id"] == USER_ID
    assert isinstance(marker["version"], str) and len(marker["version"]) == 32
    mark_spy.assert_not_awaited()
    brief_spy.assert_not_awaited()
    handoff_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pending_model_handoff_executes_and_clears_marker():
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_id": WS_ID,
            "llm_model": "gemini-4-neon",
            "metadata": {
                thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                    "previous_model": "claude-sonnet-5-5",
                    "user_id": USER_ID,
                    "version": "ver-1",
                }
            },
        },
    )
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=True
    )

    async def fake_wait(*_a, **_kw):
        return True

    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[("ready", ("cfg", "route")), ("not_needed", None)],
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    handoff_spy.assert_awaited_once_with(
        THREAD_ID, USER_ID, "claude-sonnet-5-5", source_system_only=False
    )
    clear_spy.assert_awaited_once_with(
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version="ver-1",
    )


@pytest.mark.asyncio
async def test_run_pending_model_handoff_keeps_marker_on_failure(caplog):
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_id": WS_ID,
            "llm_model": "gemini-4-neon",
            "metadata": {
                thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                    "previous_model": "claude-sonnet-5-5",
                    "user_id": USER_ID,
                    "version": "ver-1",
                }
            },
        },
    )
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        return_value=("ready", ("cfg", "route")),
    )
    handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch,
        side_effect=RuntimeError("handoff boom"),
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key)

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "retryable_failure"
    clear_spy.assert_not_awaited()
    assert "deferred model handoff failed" in caplog.text


@pytest.mark.asyncio
async def test_coordinator_teardown_runs_pending_handoff_after_guard_release():
    import asyncio
    from src.server.database.runs.lifecycle import NewestCheckpoint
    from src.server.services.runs.coordinator import (
        FinalizeResult,
        RunCoordinator,
        RunHandle,
        RunOutcome,
    )

    from contextlib import nullcontext

    events: list[str] = []

    class _GuardContract:
        usable = True
        conn = None
        mutex = nullcontext()

        def bind_owner(self, _owner: Any) -> None:
            pass

        async def demote_to_tail(self) -> None:
            events.append("demote")

        async def release(self, *, discard: bool = False) -> None:
            events.append(f"release:{discard}")

    guard = _GuardContract()
    handle = RunHandle(
        run_id="run-defer-1",
        thread_id=THREAD_ID,
        turn_index=0,
        attempt_no=1,
        guard=guard,  # type: ignore[arg-type]
    )
    coord = RunCoordinator()
    coord._schedule_replay_refresh = lambda *_a: None
    coord._schedule_search_index = lambda *_a: None
    coord._nudge_hook_drainer = lambda *_a: None

    async def fake_newest(_tid):
        return NewestCheckpoint("cp-1", "run-defer-1")

    coord._newest_checkpoint = fake_newest

    async def fake_finalize(**_kw):
        events.append("finalize")
        return FinalizeResult(
            applied=True,
            run={
                "conversation_response_id": "run-defer-1",
                "conversation_thread_id": THREAD_ID,
                "status": "completed",
                "metadata": {"msg_type": "ptc", "workspace_id": WS_ID},
            },
        )

    async def fake_tail_drain():
        events.append("tail_drain")

    async def fake_pending(tid, *, user_id=None):
        events.append(f"pending:{tid}")
        return True

    pending_spy = create_autospec(
        thread_model_svc.run_pending_model_handoff, side_effect=fake_pending
    )

    with (
        patch("src.server.database.runs.lifecycle.finalize_run", side_effect=fake_finalize),
        patch("src.server.services.transcripts.schedule_thread_export"),
        patch(f"{SVC_MOD}.run_pending_model_handoff", new=pending_spy),
    ):
        await coord.finalize_run(
            handle,
            RunOutcome(status="completed"),
            tail_drain=fake_tail_drain,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert events == [
        "finalize",
        "demote",
        "tail_drain",
        "release:False",
        f"pending:{THREAD_ID}",
    ]
    pending_spy.assert_awaited_once_with(THREAD_ID)


@pytest.mark.asyncio
async def test_request_prep_reads_pending_model_handoff_from_thread_metadata():
    from src.server.handlers.chat import request_prep

    pending = {"previous_model": "m-old", "user_id": USER_ID}
    get_thread = create_autospec(
        threads_read.get_thread_by_id,
        return_value={
            "conversation_thread_id": THREAD_ID,
            "metadata": {thread_model_svc.PENDING_MODEL_HANDOFF_KEY: pending},
        },
    )

    with (
        patch.object(request_prep.qr_db, "get_thread_by_id", new=get_thread),
        patch.object(request_prep.tl_db, "get_latest_attempt", return_value=None),
        patch.object(request_prep, "cache_clock_attempt", return_value=None),
    ):
        prior = await request_prep._read_prior_thread(THREAD_ID)

    assert prior.pending_model_handoff == pending


# ---------------------------------------------------------------------------
# 7. Round 3 fixes: dispatch marker, versioned clear/PATCH, 3-way handoff,
#    PATCH explicit default write, turn-scoped steering, detached finalize,
#    and begin_run handoff lock wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatched_turn_yields_dispatch_started_marker_first_when_pending_handoff_exists():
    from types import SimpleNamespace

    from ptc_agent.config.agent import AgentConfig, LLMConfig
    from ptc_agent.config.core import (
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )
    from src.server.handlers.chat import ptc_run
    from src.server.handlers.chat.request_prep import (
        DISPATCH_STARTED_MARKER,
        PriorThread,
    )
    from src.server.models.chat import ChatRequest
    from src.server.services.shared_workspaces.turn import TurnIdentity
    from src.server.services.turn_runtime import TurnRoute

    req = ChatRequest(workspace_id=WS_ID, messages=[{"role": "user", "content": "hi"}])
    wm = create_autospec(ptc_run.WorkspaceManager, instance=True)
    wm.has_ready_session.return_value = True
    cfg = AgentConfig(
        llm=LLMConfig(name="gemini-4-neon"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    pending = {"previous_model": "m-old", "user_id": USER_ID, "version": "ver-1"}
    prior = PriorThread(pending_model_handoff=pending)
    run_handle = SimpleNamespace(run_id="r-1", turn_index=1, finalized=False)
    pending_runner = create_autospec(thread_model_svc.run_pending_model_handoff)

    identity = TurnIdentity(USER_ID, USER_ID, USER_ID)

    with (
        patch.object(ptc_run, "setup") as mock_setup,
        patch.object(ptc_run, "ensure_thread", create_autospec(ptc_run.ensure_thread, return_value=prior)),
        patch.object(ptc_run.carry, "prior_attempt", create_autospec(ptc_run.carry.prior_attempt, return_value=ptc_run.carry.PriorAttempt(latest=None, retried=None))),
        patch.object(ptc_run, "wait_or_steer", create_autospec(ptc_run.wait_or_steer, return_value=(True, None))),
        patch.object(ptc_run, "begin_run", create_autospec(ptc_run.begin_run, return_value=run_handle)),
        patch.object(ptc_run, "retried_on_its_surface", create_autospec(ptc_run.retried_on_its_surface, return_value=(req, False))),
        patch.object(ptc_run, "read_disk_notice", create_autospec(ptc_run.read_disk_notice, return_value=(None, False))),
        patch.object(ptc_run, "_resolve_origin_meta", create_autospec(ptc_run._resolve_origin_meta, return_value={})),
        patch.object(ptc_run.HomeTurnStart, "read", create_autospec(ptc_run.HomeTurnStart.read, return_value=ptc_run.HomeTurnStart(WS_ID))),
        patch.object(TurnIdentity, "begin", create_autospec(TurnIdentity.begin, return_value=None)),
        patch.object(TurnIdentity, "profile", create_autospec(TurnIdentity.profile, return_value={})),
        patch.object(ptc_run, "_resolve_fork", return_value=("Q", None)),
        patch.object(ptc_run, "WorkspaceManager") as wm_cls,
        patch(f"{SVC_MOD}.run_pending_model_handoff", new=pending_runner),
    ):
        mock_setup.agent_config = cfg
        wm_cls.get_instance.return_value = wm
        gen = ptc_run.astream_ptc_workflow(
            request=req,
            thread_id=THREAD_ID,
            run_id="r-1",
            user_input="hi",
            user_id=USER_ID,
            route=TurnRoute(WS_ID, "analyst", USER_ID),
            identity=identity,
            is_byok=False,
            config=cfg,
            dispatched=True,
        )
        try:
            first = await anext(gen)
        finally:
            await gen.aclose()

    assert first == DISPATCH_STARTED_MARKER
    pending_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_thread_metadata_key_checks_expected_version_and_stale_task_cannot_delete_newer():
    executed: list[tuple[str, list[Any]]] = []

    class _FakeCursor:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any) -> None:
            executed.append((sql, list(params)))

    class _FakeConn:
        def __init__(self, rowcount: int) -> None:
            self._rowcount = rowcount

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def cursor(self):
            return _FakeCursor(self._rowcount)

    class _FakePool:
        def __init__(self, rowcount: int) -> None:
            self._rowcount = rowcount

        def get_db_connection(self):
            return _FakeConn(self._rowcount)

    with patch.object(model_switch_db, "pool", _FakePool(rowcount=0)):
        cleared = await model_switch_db.clear_thread_metadata_key(
            THREAD_ID,
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
            expected_version="ver-old",
        )

    assert cleared is False
    sql, params = executed[0]
    assert "metadata -> %s ->> 'version' = %s" in sql
    assert params == [
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        "ver-old",
    ]


@pytest.mark.asyncio
async def test_stale_pending_handoff_passes_read_version_to_conditional_clear():
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_id": WS_ID,
            "llm_model": "gemini-4-neon",
            "metadata": {
                thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                    "previous_model": "m-old",
                    "user_id": USER_ID,
                    "version": "ver-1",
                }
            },
        },
    )
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=True
    )

    async def fake_wait(*_a, **_kw):
        return True

    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[("ready", ("cfg", "route")), ("not_needed", None)],
    )
    # Simulate DB row holding ver-2 when ver-1 tries to clear -> conditional UPDATE returns False
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=False)

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    clear_spy.assert_awaited_once_with(
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version="ver-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_result", [True, False])
async def test_patch_preserves_pending_marker_content_and_version(handoff_result):
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    marker = {
        "previous_model": "m-a",
        "user_id": USER_ID,
        "version": "ver-1",
    }
    stored_row = _thread_row(
        llm_model="m-b",
        metadata={thread_model_svc.PENDING_MODEL_HANDOFF_KEY: dict(marker)},
    )

    async def fake_update(tid: str, **kwargs):
        assert tid == THREAD_ID
        assert kwargs == {"llm_model": "m-c"}
        stored_row["llm_model"] = kwargs["llm_model"]
        return dict(stored_row)

    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": "m-b",
            "metadata": {thread_model_svc.PENDING_MODEL_HANDOFF_KEY: dict(marker)},
        },
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key)
    set_spy = create_autospec(model_switch_db.set_thread_metadata_key)
    handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=handoff_result
    )
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )

    with (
        as_owner(CRUD_MOD, user_id=USER_ID),
        patch(f"{CRUD_MOD}.require_selectable_model", new=require_mock),
        patch(f"{CRUD_MOD}.get_thread_auth_meta", new=meta_mock),
        patch(f"{CRUD_MOD}.update_thread_fields", side_effect=fake_update),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.set_thread_metadata_key", new=set_spy),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "m-c"},
            )

    assert resp.status_code == 200
    assert resp.json()["llm_model"] == "m-c"
    assert resp.json()["summary_handoff"] is handoff_result
    handoff_spy.assert_awaited_once_with(THREAD_ID, USER_ID, "m-b")
    clear_spy.assert_not_awaited()
    set_spy.assert_not_awaited()
    assert stored_row["metadata"][thread_model_svc.PENDING_MODEL_HANDOFF_KEY] == marker


@pytest.mark.asyncio
async def test_pending_handoff_uses_marker_previous_model_after_patch_switch():
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    marker = {
        "previous_model": "m-a",
        "user_id": USER_ID,
        "version": "ver-1",
    }
    stored_row = _thread_row(
        llm_model="m-b",
        metadata={thread_model_svc.PENDING_MODEL_HANDOFF_KEY: dict(marker)},
    )

    async def fake_update(tid: str, **kwargs):
        stored_row["llm_model"] = kwargs["llm_model"]
        return dict(stored_row)

    async def fake_get_meta(_tid: str):
        return {
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": stored_row["llm_model"],
            "metadata": dict(stored_row["metadata"]),
        }

    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    patch_handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=False
    )

    with (
        as_owner(CRUD_MOD, user_id=USER_ID),
        patch(f"{CRUD_MOD}.require_selectable_model", new=create_autospec(thread_model_svc.require_selectable_model)),
        patch(f"{CRUD_MOD}.get_thread_auth_meta", side_effect=fake_get_meta),
        patch(f"{CRUD_MOD}.update_thread_fields", side_effect=fake_update),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=patch_handoff_spy),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "m-c"},
            )

    assert resp.status_code == 200
    assert stored_row["llm_model"] == "m-c"

    deferred_handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=True
    )
    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[("ready", ("cfg", "route")), ("not_needed", None)],
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    async def fake_wait(*_a, **_kw):
        return True

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", side_effect=fake_get_meta),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=deferred_handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    deferred_handoff_spy.assert_awaited_once_with(
        THREAD_ID, USER_ID, "m-a", source_system_only=False
    )
    clear_spy.assert_awaited_once_with(
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version="ver-1",
    )


@pytest.mark.asyncio
async def test_pending_handoff_falls_back_to_alt_source_when_primary_has_nothing():
    """Summary written by the turn's own model (automation override): try the alt source (codex final review)."""
    meta = {
        "user_id": USER_ID,
        "workspace_owner_id": USER_ID,
        "is_shared": False,
        "workspace_id": WS_ID,
        "llm_model": "claude-sonnet-5-5",
        "metadata": {
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                "previous_model": "gemini-4-neon",
                "alt_previous_model": "gpt-6.2-sol",
                "user_id": USER_ID,
                "version": "ver-alt",
            }
        },
    }

    async def fake_get_meta(_tid: str):
        return meta

    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch, return_value=True)
    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[
            ("not_needed", None),          # primary (stored Gemini) has nothing
            ("ready", ("cfg", "route")),   # alt (turn's GPT) wrote the summary
            ("not_needed", None),          # settled after hand-off
        ],
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    async def fake_wait(*_a, **_kw):
        return True

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", side_effect=fake_get_meta),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    handoff_spy.assert_awaited_once_with(
        THREAD_ID, USER_ID, "gpt-6.2-sol", source_system_only=False
    )
    clear_spy.assert_awaited_once_with(
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version="ver-alt",
    )


@pytest.mark.asyncio
async def test_pending_handoff_tries_next_candidate_when_first_is_retryable():
    """Stored model left the catalog (retryable) but the turn's GPT wrote the summary (codex review)."""
    meta = {
        "user_id": USER_ID,
        "workspace_owner_id": USER_ID,
        "is_shared": False,
        "workspace_id": WS_ID,
        "llm_model": "claude-sonnet-5-5",
        "metadata": {
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                "previous_model": "gemini-gone",
                "candidate_models": ["gemini-gone", "gpt-6.2-sol"],
                "user_id": USER_ID,
                "version": "ver-c",
            }
        },
    }

    async def fake_get_meta(_tid: str):
        return meta

    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch, return_value=True)
    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[
            ("retryable_failure", None),
            ("ready", ("cfg", "route")),
            ("not_needed", None),
        ],
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    async def fake_wait(*_a, **_kw):
        return True

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", side_effect=fake_get_meta),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    handoff_spy.assert_awaited_once_with(
        THREAD_ID, USER_ID, "gpt-6.2-sol", source_system_only=False
    )


@pytest.mark.asyncio
async def test_new_switch_keeps_old_candidates_and_adds_the_turns_model():
    """An older marker's candidates stay and this turn's model is appended (codex review)."""
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"display_name": "GPT 6.2 Sol", "visible": True},
            "claude-sonnet-5-5": {"display_name": "Claude Sonnet 5.5", "visible": True},
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True},
            "glm-5.3": {"display_name": "GLM 5.3", "visible": True},
        }
    )
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": "claude-sonnet-5-5",
            "metadata": {
                thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                    "previous_model": "gemini-4-neon",
                    "candidate_models": ["gemini-4-neon", "claude-sonnet-5-5"],
                    "user_id": USER_ID,
                    "version": "old",
                }
            },
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="glm-5.3"),
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    config = _user_config()
    config["configurable"]["dalton_turn_model"] = "gpt-6.2-sol"
    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
    ):
        await _tool().ainvoke({"model": "glm-5.3"}, config=config)

    marker = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker["previous_model"] == "gemini-4-neon"
    assert marker["candidate_models"] == ["gemini-4-neon", "claude-sonnet-5-5", "gpt-6.2-sol"]


@pytest.mark.asyncio
async def test_run_pending_model_handoff_distinguishes_not_needed_vs_retryable_failure():
    from src.server.services.turn_runtime import TurnRoute

    opaque_summary = HumanMessage(
        content="digest",
        additional_kwargs={"lc_source": "summarization"},
    )
    meta = {
        "user_id": USER_ID,
        "workspace_id": WS_ID,
        "llm_model": "gemini-4-neon",
        "metadata": {
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                "previous_model": "gpt-old",
                "user_id": USER_ID,
                "version": "ver-1",
            }
        },
    }

    async def fake_resolve(model_name):
        if model_name == "gpt-old":
            raise ConnectionError("upstream temporarily unreachable")
        client = create_autospec(GenericFakeChatModel, instance=True)
        cfg = create_autospec(AgentConfig, instance=True)
        cfg.get_llm_client.return_value = client
        return cfg

    meta_mock = create_autospec(threads_read.get_thread_auth_meta, return_value=meta)
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
        patch("src.server.handlers.thread_maintenance._summary_to_hand_off", create_autospec(thread_maintenance._summary_to_hand_off, return_value=opaque_summary)),
        patch("src.server.handlers.thread_maintenance.thread_route", create_autospec(thread_maintenance.thread_route, return_value=(meta, TurnRoute(WS_ID, "analyst", USER_ID)))),
        patch("src.server.handlers.thread_maintenance._resolver", create_autospec(thread_maintenance._resolver, return_value=fake_resolve)),
        patch("src.llms.provider_compaction.replays", return_value=False),
    ):
        retryable_outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert retryable_outcome == "retryable_failure"
    clear_spy.assert_not_awaited()

    # When no opaque summary remains, outcome is "not_needed" and marker is cleared by version
    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
        patch("src.server.handlers.thread_maintenance._summary_to_hand_off", create_autospec(thread_maintenance._summary_to_hand_off, return_value=None)),
    ):
        not_needed_outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert not_needed_outcome == "not_needed"
    clear_spy.assert_awaited_once_with(
        THREAD_ID,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version="ver-1",
    )


@pytest.mark.asyncio
async def test_patch_explicitly_persists_account_default_when_thread_llm_model_is_null():
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    pref = {"preferred_model": "gemini-4-neon"}
    row = _thread_row(llm_model="gemini-4-neon")
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": None,
            "metadata": {},
        },
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value=pref)
    update_spy = create_autospec(threads_write.update_thread_fields, return_value=row)
    handoff_spy = create_autospec(thread_maintenance.hand_off_on_switch, return_value=False)
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )

    with (
        as_owner(CRUD_MOD, user_id=USER_ID),
        patch(f"{CRUD_MOD}.require_selectable_model", new=require_mock),
        patch(f"{CRUD_MOD}.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{CRUD_MOD}.update_thread_fields", new=update_spy),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "gemini-4-neon"},
            )

    assert resp.status_code == 200
    assert resp.json()["llm_model"] == "gemini-4-neon"
    update_spy.assert_awaited_once_with(THREAD_ID, llm_model="gemini-4-neon")


@pytest.mark.asyncio
async def test_automation_turn_refuses_when_previous_turn_had_user_steer_without_ai_reply():
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    prev_steer = HumanMessage(
        content="[Steering from User]\n换成 gemini-4-neon-high-oauth",
        additional_kwargs={
            "lc_source": "steering",
            "steering_delivered": {"count": 1},
            "held_jobs": {"run_id": "run-prev", "job_ids": ["j-1"]},
        },
    )
    messages = [
        HumanMessage(content="上一轮输入", id="msg-prev"),
        prev_steer,
        HumanMessage(content="Automation schedule trigger", id="msg-auto"),
    ]
    cfg = _user_config(origin="automation")
    cfg["configurable"]["run_id"] = "run-auto"

    with patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth", "state": {"messages": messages}},
            config=cfg,
        )

    assert "拒绝切换模型" in result
    switch_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_automation_turn_allows_when_steers_ahead_belongs_to_current_run():
    catalog = _make_catalog(
        {"gemini-4-neon-high-oauth": {"visible": True}}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(
        thread_model_svc.switch_thread_model,
        return_value=thread_model_svc.ThreadModelSwitchResult(
            thread=_thread_row(),
            workspace_id=WS_ID,
            previous_model="m-old",
            model="gemini-4-neon-high-oauth",
            changed=True,
        ),
    )
    ahead_steer = HumanMessage(
        content="[Steering from User]\n换成 gemini-4-neon-high-oauth",
        additional_kwargs={
            "lc_source": "steering",
            "steering_delivered": {"count": 1},
            "held_jobs": {"run_id": "run-auto", "job_ids": ["j-2"]},
        },
    )
    messages = [
        HumanMessage(content="上一轮输入", id="msg-prev"),
        ahead_steer,
        HumanMessage(content="Automation schedule trigger", id="msg-auto"),
    ]
    cfg = _user_config(origin="automation")
    cfg["configurable"]["run_id"] = "run-auto"

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth", "state": {"messages": messages}},
            config=cfg,
        )

    assert "已切换到 gemini-4-neon-high-oauth" in result
    switch_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_coordinator_detached_finalize_triggers_pending_handoff_only_when_applied():
    import asyncio
    from src.server.services.runs.coordinator import (
        FinalizeResult,
        RunCoordinator,
        RunOutcome,
    )

    coord = RunCoordinator()
    coord._schedule_replay_refresh = lambda *_a: None
    coord._schedule_search_index = lambda *_a: None
    coord._nudge_hook_drainer = lambda *_a: None
    from src.server.database.runs import lifecycle as tl_db
    from src.server.services.report_back import steer as rb_steer
    from src.server.services.runs.executor import LocalRunExecutor

    executor = create_autospec(LocalRunExecutor, instance=True)
    pending_spy = create_autospec(
        thread_model_svc.run_pending_model_handoff, return_value="completed"
    )
    confirm_spy = create_autospec(rb_steer.confirm_checkpointed_holds, return_value=[])
    finalize_spy = create_autospec(
        tl_db.finalize_run,
        return_value=FinalizeResult(
            applied=True,
            run={
                "conversation_response_id": "run-det-1",
                "conversation_thread_id": THREAD_ID,
                "status": "error",
                "metadata": {"msg_type": "ptc", "workspace_id": WS_ID},
            },
        ),
    )

    with (
        patch(
            "src.server.services.report_back.steer.confirm_checkpointed_holds",
            new=confirm_spy,
        ),
        patch(
            "src.server.database.runs.lifecycle.finalize_run",
            new=finalize_spy,
        ),
        patch("src.server.services.transcripts.schedule_thread_export"),
        patch.object(LocalRunExecutor, "get_instance", return_value=executor),
        patch(f"{SVC_MOD}.run_pending_model_handoff", new=pending_spy),
    ):
        await coord.finalize_detached_run(
            THREAD_ID, "run-det-1", RunOutcome(status="error")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    pending_spy.assert_awaited_once_with(THREAD_ID)

    pending_spy.reset_mock()
    finalize_spy.return_value = FinalizeResult(
        applied=False,
        run={
            "conversation_response_id": "run-det-2",
            "conversation_thread_id": THREAD_ID,
            "status": "cancelled",
            "metadata": {"msg_type": "ptc", "workspace_id": WS_ID},
        },
    )
    with (
        patch(
            "src.server.services.report_back.steer.confirm_checkpointed_holds",
            new=confirm_spy,
        ),
        patch(
            "src.server.database.runs.lifecycle.finalize_run",
            new=finalize_spy,
        ),
        patch("src.server.services.transcripts.schedule_thread_export"),
        patch.object(LocalRunExecutor, "get_instance", return_value=executor),
        patch(f"{SVC_MOD}.run_pending_model_handoff", new=pending_spy),
    ):
        await coord.finalize_detached_run(
            THREAD_ID, "run-det-2", RunOutcome(status="error")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    pending_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. Round 4 fixes: HITL interrupt keeps marker, NULL previous_model resolves
#    actual default, retry preserves persisted origin, disabled providers
#    filtered, and wait timeout aligns with brief budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_hand_off_returns_retryable_failure_and_keeps_marker_when_interrupted():
    from types import SimpleNamespace

    from langgraph.types import Interrupt

    opaque_summary = HumanMessage(
        content="digest",
        additional_kwargs={"lc_source": "summarization"},
    )
    saved_tuple = SimpleNamespace(
        checkpoint={
            "channel_values": {
                "_summarization_event": {"summary_message": opaque_summary},
            }
        },
        pending_writes=[
            ("task-1", "__interrupt__", Interrupt(value={"question": "Proceed?"}, id="int-1")),
        ],
    )

    class _FakeCheckpointer:
        async def aget_tuple(self, _cfg):
            return saved_tuple

    meta = {
        "user_id": USER_ID,
        "workspace_id": WS_ID,
        "llm_model": "gemini-4-neon",
        "metadata": {
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                "previous_model": "gpt-old",
                "user_id": USER_ID,
                "version": "ver-1",
            }
        },
    }
    meta_mock = create_autospec(threads_read.get_thread_auth_meta, return_value=meta)
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key)

    with (
        patch("src.server.handlers.thread_maintenance.get_checkpointer", return_value=_FakeCheckpointer()),
        patch("src.server.handlers.thread_maintenance.turn_base_checkpoint_id", create_autospec(lambda *_a: None, return_value=None)),
        patch("src.server.utils.checkpoint_helpers.get_checkpointer", return_value=_FakeCheckpointer()),
        patch("src.server.utils.checkpoint_helpers.turn_base_checkpoint_id", create_autospec(lambda *_a: None, return_value=None)),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        status, payload = await thread_model_handoff.inspect_hand_off(
            THREAD_ID, USER_ID, "gpt-old"
        )
        assert status == "retryable_failure"
        assert payload is None

        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "retryable_failure"
    clear_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_records_resolved_default_model_when_thread_llm_model_is_null():
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"display_name": "GPT 6.2 Sol", "visible": True},
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True},
        }
    )
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": None,
            "metadata": {},
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="gemini-4-neon"),
    )

    # Case 1: user preference defines preferred_model
    pref = {"preferred_model": "gpt-6.2-sol"}
    get_pref = create_autospec(user_models.get_model_preference, return_value=pref)
    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
    ):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon"}, config=_user_config()
        )

    assert "已切换到 Gemini 4 Neon (gemini-4-neon)" in result
    marker1 = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker1["previous_model"] == "gpt-6.2-sol"

    # Case 2: user preference is empty -> falls back to system_default_models()
    update_spy.reset_mock()
    get_empty_pref = create_autospec(user_models.get_model_preference, return_value={})
    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_empty_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch(
            f"{SVC_MOD}.system_default_models",
            return_value={"default_model": "gpt-6.2-sol"},
        ),
    ):
        result2 = await _tool().ainvoke(
            {"model": "gemini-4-neon"}, config=_user_config()
        )

    assert "已切换到 Gemini 4 Neon (gemini-4-neon)" in result2
    marker2 = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker2["previous_model"] == "gpt-6.2-sol"


@pytest.mark.asyncio
async def test_tool_records_the_turns_running_model_over_a_changed_account_default():
    """Default changed mid-turn: the marker names the model the turn ran on (codex r4 mid 1)."""
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"display_name": "GPT 6.2 Sol", "visible": True},
            "claude-sonnet-5-5": {"display_name": "Claude Sonnet 5.5", "visible": True},
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True},
        }
    )
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": None,
            "metadata": {},
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="gemini-4-neon"),
    )
    pref = {"preferred_model": "claude-sonnet-5-5"}
    get_pref = create_autospec(user_models.get_model_preference, return_value=pref)
    config = _user_config()
    config["configurable"]["dalton_turn_model"] = "gpt-6.2-sol"
    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
    ):
        result = await _tool().ainvoke({"model": "gemini-4-neon"}, config=config)

    assert "已切换到 Gemini 4 Neon (gemini-4-neon)" in result
    marker = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker["previous_model"] == "gpt-6.2-sol"


@pytest.mark.asyncio
async def test_stored_thread_model_beats_a_one_off_running_model_as_handoff_source():
    """Stored GPT + a turn temporarily on Gemini: the marker still names GPT (codex final review)."""
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"display_name": "GPT 6.2 Sol", "visible": True},
            "claude-sonnet-5-5": {"display_name": "Claude Sonnet 5.5", "visible": True},
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True},
        }
    )
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_owner_id": USER_ID,
            "is_shared": False,
            "workspace_id": WS_ID,
            "llm_model": "gpt-6.2-sol",
            "metadata": {},
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="claude-sonnet-5-5"),
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    config = _user_config()
    config["configurable"]["dalton_turn_model"] = "gemini-4-neon"
    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
    ):
        await _tool().ainvoke({"model": "claude-sonnet-5-5"}, config=config)

    marker = update_spy.await_args.kwargs["metadata_merge"][
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY
    ]
    assert marker["previous_model"] == "gpt-6.2-sol"
    assert marker["candidate_models"] == ["gpt-6.2-sol", "gemini-4-neon"]


@pytest.mark.asyncio
async def test_retry_of_automation_turn_is_refused_by_tool():
    from types import SimpleNamespace

    from ptc_agent.config.agent import AgentConfig, LLMConfig
    from ptc_agent.config.core import (
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )
    from src.server.handlers.chat import ptc_run
    from src.server.handlers.chat.request_prep import PriorThread
    from src.server.models.chat import ChatRequest
    from src.server.services.shared_workspaces.turn import TurnIdentity
    from src.server.services.turn_runtime import TurnRoute

    retried_run = {
        "conversation_response_id": "run-auto-1",
        "conversation_thread_id": THREAD_ID,
        "turn_index": 3,
        "status": "error",
        "metadata": {
            "run_origin": "automation",
            "automation_id": "auto-1",
            "automation_execution_id": "exec-1",
        },
    }
    assert (
        await thread_model_svc.resolve_retried_turn_origin(THREAD_ID, retried_run)
        == "automation"
    )

    # Verify ptc_run forwards retried_origin into graph_config configurable when retrying an automation run
    req = ChatRequest(
        workspace_id=WS_ID,
        messages=[],
        checkpoint_id="cp-1",
        retry_of_run_id="run-auto-1",
    )
    wm = create_autospec(ptc_run.WorkspaceManager, instance=True)
    wm.has_ready_session.return_value = True
    cfg = AgentConfig(
        llm=LLMConfig(name="gemini-4-neon"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    prior_thread = PriorThread()
    run_handle = SimpleNamespace(
        run_id="run-retry-1",
        turn_index=3,
        finalized=False,
        checkpointer=None,
        guard=None,
    )
    identity = TurnIdentity(USER_ID, USER_ID, USER_ID)

    class _StopAfterConfig(BaseException):
        pass

    captured_configs: list[dict[str, Any]] = []
    real_build = ptc_run.build_graph_config

    def _capture_build(*args, **kwargs):
        built = real_build(*args, **kwargs)
        captured_configs.append(built)
        raise _StopAfterConfig()

    session_mock = SimpleNamespace(sandbox=None)
    wm.get_session_for_workspace.return_value = session_mock
    inputs_mock = SimpleNamespace(
        project=None,
        direct_mcp=None,
        build_kwargs=lambda: {},
    )
    async def _fake_get_registry(_tid: str):
        return SimpleNamespace(current_run_id=None)

    async def _fake_inputs_read(*_a, **_kw):
        return inputs_mock

    registry_store_inst = SimpleNamespace(
        get_or_create_registry=_fake_get_registry
    )

    from contextlib import ExitStack

    mock_setup = SimpleNamespace(agent_config=cfg)
    wm_cls = create_autospec(ptc_run.WorkspaceManager)
    wm_cls.get_instance.return_value = wm
    reg_cls = create_autospec(ptc_run.BackgroundRegistryStore)
    reg_cls.get_instance.return_value = registry_store_inst

    async def _fake_acq(*_a, **_kw):
        yield session_mock

    acq_mock = create_autospec(ptc_run.acquire_session, side_effect=_fake_acq)
    patches = (
        patch.object(ptc_run, "setup", mock_setup),
        patch.object(ptc_run, "ensure_thread", create_autospec(ptc_run.ensure_thread, return_value=prior_thread)),
        patch.object(ptc_run.carry, "prior_attempt", create_autospec(ptc_run.carry.prior_attempt, return_value=ptc_run.carry.PriorAttempt(latest=retried_run, retried=retried_run))),
        patch.object(ptc_run, "wait_or_steer", create_autospec(ptc_run.wait_or_steer, return_value=(True, None))),
        patch.object(ptc_run, "begin_run", create_autospec(ptc_run.begin_run, return_value=run_handle)),
        patch.object(ptc_run, "retried_on_its_surface", create_autospec(ptc_run.retried_on_its_surface, return_value=(req, False))),
        patch.object(ptc_run, "read_disk_notice", create_autospec(ptc_run.read_disk_notice, return_value=(None, False))),
        patch.object(ptc_run, "_resolve_origin_meta", create_autospec(ptc_run._resolve_origin_meta, return_value={})),
        patch.object(ptc_run.HomeTurnStart, "read", create_autospec(ptc_run.HomeTurnStart.read, return_value=ptc_run.HomeTurnStart(WS_ID))),
        patch.object(TurnIdentity, "begin", create_autospec(TurnIdentity.begin, return_value=None)),
        patch.object(TurnIdentity, "profile", create_autospec(TurnIdentity.profile, return_value={})),
        patch.object(ptc_run, "_resolve_fork", return_value=(None, None)),
        patch.object(ptc_run, "keep_named_model", create_autospec(ptc_run.keep_named_model, return_value=None)),
        patch.object(ptc_run, "init_tracking", create_autospec(ptc_run.init_tracking, return_value=(None, None))),
        patch.object(ptc_run, "charge_owed", create_autospec(ptc_run.charge_owed, return_value=None)),
        patch.object(ptc_run, "build_run_credit_gate", return_value=None),
        patch.object(ptc_run, "apply_fetch_override", return_value=None),
        patch.object(ptc_run, "update_workspace_activity", create_autospec(ptc_run.update_workspace_activity, return_value=None)),
        patch.object(ptc_run, "build_ptc_graph_with_session", create_autospec(ptc_run.build_ptc_graph_with_session, return_value=object())),
        patch.object(ptc_run, "turn_start_checkpoint_id", create_autospec(ptc_run.turn_start_checkpoint_id, return_value=None)),
        patch.object(ptc_run, "build_graph_config", side_effect=_capture_build),
        patch.object(ptc_run, "WorkspaceManager", wm_cls),
        patch.object(ptc_run, "BackgroundRegistryStore", reg_cls),
        patch("src.server.services.user_skills.skills_delivery_signature", return_value="sig"),
        patch("src.server.services.thread_agent.AgentInputs.read", side_effect=_fake_inputs_read),
        patch("src.server.services.egress.direct_tools.order_ledger_for_turn", return_value=None),
        patch("src.server.services.wakeups.cancel_notice.ThreadCancelNotices"),
        patch.object(ptc_run, "acquire_session", acq_mock),
    )

    with ExitStack() as stack:
        stack.enter_context(patches[0])
        for p in patches[1:4]:
            stack.enter_context(p)
        begin_spy = stack.enter_context(patches[4])
        for p in patches[5:]:
            stack.enter_context(p)

        gen = ptc_run.astream_ptc_workflow(
            request=req,
            thread_id=THREAD_ID,
            run_id="run-retry-1",
            user_input="",
            user_id=USER_ID,
            route=TurnRoute(WS_ID, "analyst", USER_ID),
            identity=identity,
            is_byok=False,
            config=cfg,
            from_server=False,
        )
        with pytest.raises(_StopAfterConfig):
            async for _ in gen:
                pass

    assert begin_spy.await_args.kwargs["extra_run_metadata"]["retried_origin"] == "automation"
    retry_graph_cfg = captured_configs[0]
    assert retry_graph_cfg["metadata"]["origin"] == "user"
    assert retry_graph_cfg["configurable"]["retried_origin"] == "automation"

    # Tool refuses when invoked under that retried automation config
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    with patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy):
        result = await _tool().ainvoke(
            {"model": "gemini-4-neon-high-oauth", "state": {"messages": []}},
            config=retry_graph_cfg,
        )

    assert "拒绝切换模型" in result
    switch_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_provider_excluded_from_candidates_and_refused_without_db_write():
    catalog = _make_catalog(
        {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "visible": True, "provider": "gemini"},
            "muse-spark-1.3-oauth": {
                "display_name": "Muse Spark 1.3",
                "visible": True,
                "provider": "muse-oauth",
            },
        }
    )
    catalog.get_provider_info.side_effect = lambda prov: (
        {"parent_provider": "muse"} if prov == "muse-oauth" else {}
    )
    get_pref = create_autospec(user_models.get_model_preference, return_value={})
    switch_spy = create_autospec(thread_model_svc.switch_thread_model)
    update_spy = create_autospec(threads_write.update_thread_fields)

    with (
        patch(f"{TOOL_MOD}.LLMFactory.get_model_config", return_value=catalog),
        patch(f"{TOOL_MOD}.user_models.get_model_preference", new=get_pref),
        patch(f"{TOOL_MOD}.switch_thread_model", new=switch_spy),
        patch.object(threads_write, "update_thread_fields", new=update_spy),
        patch("src.dalton.llm.providers.get_disabled_providers", return_value={"muse"}),
    ):
        refused = await _tool().ainvoke(
            {"model": "muse-spark-1.3-oauth"}, config=_user_config()
        )
        unknown = await _tool().ainvoke(
            {"model": "nonexistent-model"}, config=_user_config()
        )

    assert "在当前服务中未启用" in refused
    assert "gemini-4-neon" in refused
    assert "muse-spark-1.3-oauth" not in refused.split("可选模型：", 1)[1]
    assert "gemini-4-neon" in unknown
    assert "muse-spark-1.3-oauth" not in unknown
    switch_spy.assert_not_awaited()
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pending_model_handoff_wait_timeout_matches_brief_budget():
    observed_timeouts: list[float] = []
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_id": WS_ID,
            "llm_model": "gemini-4-neon",
            "metadata": {
                thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                    "previous_model": "claude-sonnet-5-5",
                    "user_id": USER_ID,
                    "version": "ver-1",
                }
            },
        },
    )
    runs_cc = create_autospec(
        thread_model_svc.next_turn_runs_claude_code, return_value=False
    )
    handoff_spy = create_autospec(
        thread_maintenance.hand_off_on_switch, return_value=True
    )

    async def fake_wait(_self, _tid: str, *, timeout: float):
        observed_timeouts.append(timeout)
        return True

    inspect_spy = create_autospec(
        thread_model_handoff.inspect_hand_off,
        side_effect=[("ready", ("cfg", "route")), ("not_needed", None)],
    )
    clear_spy = create_autospec(model_switch_db.clear_thread_metadata_key, return_value=True)

    with (
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.next_turn_runs_claude_code", new=runs_cc),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=handoff_spy),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.wait_until_idle",
            new=fake_wait,
        ),
        patch(f"{HANDOFF_MOD}.inspect_hand_off", new=inspect_spy),
        patch(f"{MODEL_SWITCH_DB_MOD}.clear_thread_metadata_key", new=clear_spy),
    ):
        outcome = await thread_model_handoff.run_pending_model_handoff(THREAD_ID)

    assert outcome == "completed"
    assert observed_timeouts == [handoff_brief._budget()]


def test_candidate_list_keeps_the_newest_model_when_full():
    """Full list: the turn's model survives and the oldest goes (codex review)."""
    import asyncio

    old = [f"m{i}" for i in range(thread_model_svc.MAX_HANDOFF_CANDIDATE_MODELS)]
    meta = {
        "user_id": USER_ID,
        "workspace_owner_id": USER_ID,
        "is_shared": False,
        "workspace_id": WS_ID,
        "llm_model": "m-current",
        "metadata": {
            thread_model_svc.PENDING_MODEL_HANDOFF_KEY: {
                "previous_model": old[0],
                "candidate_models": old,
                "user_id": USER_ID,
                "version": "full",
            }
        },
    }
    require_mock = create_autospec(thread_model_svc.require_selectable_model)
    meta_mock = create_autospec(threads_read.get_thread_auth_meta, return_value=meta)
    with (
        patch(f"{SVC_MOD}.require_selectable_model", new=require_mock),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
    ):
        plan = asyncio.run(
            thread_model_svc.prepare_thread_model_switch(
                USER_ID, THREAD_ID, "m-next", model_pref={}, running_model="gpt-6.2-sol"
            )
        )
    assert plan.pending_candidate_models[-1] == "gpt-6.2-sol"
    assert "m0" not in plan.pending_candidate_models
    assert len(plan.pending_candidate_models) == thread_model_svc.MAX_HANDOFF_CANDIDATE_MODELS
