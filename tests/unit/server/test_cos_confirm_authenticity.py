"""Batch 2 unit tests: backend confirmation authenticity verification across
DALTON_AUTH_MODE in ('local', 'supabase', 'oss'), covering all Principal.auth
literals ('session', 'service', 'oauth', 'pat', 'computer'), FastAPI route-level
mixed fork + hitl_response rejection, route-level first-party enforcement,
first Chief of Staff entry without own-machine promotion, SSE debug log
whitelisting, and unchanged behavior for other interrupts.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.base import BaseCheckpointSaver

from src.server.database.conversation import senders, threads_read
from src.server.database.home_workspace import get_home_workspace_id
from src.server.dependencies.principal import Principal, get_principal
from src.server.handlers import checkpoint_handler
from src.server.handlers.chat.ptc_run import astream_ptc_workflow
from src.server.handlers.checkpoint_handler import settle_resume_request
from src.server.models.chat import ChatRequest, HITLDecision, HITLResponse
from src.server.services.computer_manager import ComputerManager
from src.server.services.runs.sse_producer import RunSSEProducer
from tests.unit.server.app.test_threads_authz import (
    _app_for,
    _empty_async_gen,
    _stub_downstream,
)

_REAL_GET_THREAD_BRANCH_READ = threads_read.get_thread_branch_read
_REAL_TURN_BASE_CHECKPOINT_ID = checkpoint_handler.turn_base_checkpoint_id

OWNER_ID = "user-owner"
OTHER_ID = "user-other"
THREAD_ID = "11111111-1111-1111-1111-111111111111"
INTERRUPT_ID = "intr-ws-1"
TOOL_CALL_ID = "call-ws-1"
COMPUTER_ID = "22222222-2222-2222-2222-222222222222"


def _make_raw_request(
    *,
    headers: dict[str, str] | None = None,
    url_scheme: str = "http",
    url_netloc: str = "127.0.0.1:8088",
) -> Any:
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in hdrs.items()]

    class _Headers(dict):
        def get(self, key: str, default: Any = None) -> Any:
            return super().get(key.lower(), default)

    return SimpleNamespace(
        headers=_Headers(hdrs),
        scope={"headers": raw_headers},
        url=SimpleNamespace(scheme=url_scheme, netloc=url_netloc),
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _checkpoint_tuple(
    *,
    action_type: str = "create_workspace",
    interrupt_id: str = INTERRUPT_ID,
    tool_call_id: str = TOOL_CALL_ID,
    snapshot: dict[str, Any] | None = None,
    include_snapshot: bool = True,
) -> Any:
    if action_type == "create_workspace":
        action_req: dict[str, Any] = {
            "type": "create_workspace",
            "tool_call_id": tool_call_id,
            "workspace_name": "NVDA",
            "workspace_description": "",
            "computer_id": COMPUTER_ID,
            "computer_name": "My Mac",
            "computer_is_own_machine": True,
            "bootstrap_cloud_primary": False,
        }
        default_snap: dict[str, Any] = {
            "action": "create_workspace",
            "tool_call_id": tool_call_id,
            "workspace_name": "NVDA",
            "workspace_description": "",
            "computer_id": COMPUTER_ID,
            "computer_name": "My Mac",
            "computer_is_own_machine": True,
            "bootstrap_cloud_primary": False,
            "duplicate_workspace_id": None,
            "duplicate_workspace_name": None,
            "duplicate_check_unavailable": False,
        }
    elif action_type == "archive_workspace":
        action_req = {
            "type": "archive_workspace",
            "tool_call_id": tool_call_id,
            "workspace_id": "ws-1",
            "computer_id": COMPUTER_ID,
        }
        default_snap = {
            "action": "archive_workspace",
            "tool_call_id": tool_call_id,
            "workspace_id": "ws-1",
            "computer_id": COMPUTER_ID,
        }
    else:
        action_req = {"type": action_type, "question": "Proceed?"}
        default_snap = {}

    intr = SimpleNamespace(id=interrupt_id, value={"action_requests": [action_req]})
    channel_values: dict[str, Any] = {}
    if include_snapshot and default_snap:
        channel_values["approval_snapshots"] = {
            tool_call_id: snapshot if snapshot is not None else default_snap
        }
    return SimpleNamespace(
        config={"configurable": {"checkpoint_id": "cp-newest"}},
        checkpoint={"channel_values": channel_values},
        pending_writes=[("task-1", "__interrupt__", [intr])],
    )


def _setup_checkpointer(monkeypatch: pytest.MonkeyPatch, cp_tuple: Any) -> Any:
    branch_read = SimpleNamespace(checkpoint_id="cp-newest", run_horizon=7)
    monkeypatch.setattr(
        threads_read,
        "get_thread_branch_read",
        create_autospec(_REAL_GET_THREAD_BRANCH_READ, return_value=branch_read),
    )

    cp = create_autospec(BaseCheckpointSaver, instance=True)
    cp.aget_tuple.return_value = cp_tuple
    monkeypatch.setattr(
        "src.server.handlers.checkpoint_handler.get_checkpointer", lambda: cp
    )
    monkeypatch.setattr(
        "src.server.handlers.checkpoint_handler.turn_base_checkpoint_id",
        create_autospec(_REAL_TURN_BASE_CHECKPOINT_ID, return_value=None),
    )
    return cp


def _resume_body(
    *,
    interrupt_id: str = INTERRUPT_ID,
    decision_type: str = "approve",
    platform: str | None = None,
    external_thread_id: str | None = None,
) -> ChatRequest:
    return ChatRequest(
        messages=[],
        hitl_response={
            interrupt_id: HITLResponse(decisions=[HITLDecision(type=decision_type)])
        },
        platform=platform,
        external_thread_id=external_thread_id,
    )


def _set_auth_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
    monkeypatch.setattr(
        "src.dalton.auth.is_dalton_supabase_auth_enabled",
        lambda: mode == "supabase",
    )
    monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: mode)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["local", "supabase"])
@pytest.mark.parametrize("action_type", ["create_workspace", "archive_workspace"])
async def test_local_and_supabase_allow_first_party_session_initiator(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str, action_type: str
) -> None:
    _set_auth_mode(monkeypatch, auth_mode)
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type=action_type))

    out = await settle_resume_request(
        THREAD_ID,
        _resume_body(),
        principal=Principal(user_id=OWNER_ID, auth="session"),
        raw_request=_make_raw_request(headers={"Authorization": "Bearer jwt-session"}),
        thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
    )
    assert out.resume_run_horizon == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["local", "supabase"])
@pytest.mark.parametrize("bad_auth", ["service", "pat", "oauth", "computer"])
async def test_local_and_supabase_reject_non_session_principals_including_computer(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str, bad_auth: str
) -> None:
    """Rejects all non-session Principal.auth literals: service, pat, oauth, computer."""
    _set_auth_mode(monkeypatch, auth_mode)
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    principal = Principal(
        user_id=OWNER_ID,
        auth=bad_auth,  # type: ignore[arg-type]
        computer_id=COMPUTER_ID if bad_auth == "computer" else None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=principal,
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "web_session_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["local", "supabase"])
async def test_local_and_supabase_reject_non_initiator_and_shared_thread_other_member(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str
) -> None:
    _set_auth_mode(monkeypatch, auth_mode)
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    # Personal thread, different user
    with pytest.raises(HTTPException) as exc_info:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=Principal(user_id=OTHER_ID, auth="session"),
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "confirm_initiator_mismatch"

    # Colleague / shared thread where latest_turn_sender is OWNER_ID
    monkeypatch.setattr(
        senders,
        "latest_turn_sender",
        create_autospec(senders.latest_turn_sender, return_value=OWNER_ID),
    )
    with pytest.raises(HTTPException) as exc_info2:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=Principal(user_id=OTHER_ID, auth="session"),
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OTHER_ID, "workspace_owner_id": OWNER_ID},
        )
    assert exc_info2.value.status_code == 403
    assert exc_info2.value.detail["code"] == "confirm_initiator_mismatch"


@pytest.mark.asyncio
async def test_interrupt_id_mismatch_returns_409_and_snapshot_drift_or_missing_consumes_interrupt_with_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_auth_mode(monkeypatch, "local")
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    # Unknown interrupt_id (not pending on checkpoint) -> 409 scope_changed
    with pytest.raises(HTTPException) as exc_info:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(interrupt_id="intr-stale-999"),
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "scope_changed"

    # Snapshot drift on computer_id, workspace_description, duplicate_workspace_id,
    # or duplicate_check_unavailable -> rewrites hitl_response with scope_changed=True
    # (no 409) so LangGraph consumes the interrupt and emits a terminal error result.
    for drifted_field in (
        {"computer_id": "99999999-9999-9999-9999-999999999999"},
        {"workspace_description": "Changed description"},
        {"duplicate_workspace_id": "dup-ws-1", "duplicate_workspace_name": "NVDA"},
        {"duplicate_check_unavailable": True},
    ):
        base_snap = {
            "action": "create_workspace",
            "tool_call_id": TOOL_CALL_ID,
            "workspace_name": "NVDA",
            "workspace_description": "",
            "computer_id": COMPUTER_ID,
            "computer_name": "My Mac",
            "computer_is_own_machine": True,
            "bootstrap_cloud_primary": False,
            "duplicate_workspace_id": None,
            "duplicate_workspace_name": None,
            "duplicate_check_unavailable": False,
            **drifted_field,
        }
        _setup_checkpointer(
            monkeypatch,
            _checkpoint_tuple(action_type="create_workspace", snapshot=base_snap),
        )
        out = await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
        )
        assert out.resume_run_horizon == 7
        assert out.hitl_response is not None
        assert out.hitl_response[INTERRUPT_ID].scope_changed is True

    # Pre-upgrade card missing approval_snapshots entirely -> also consumes interrupt
    # via scope_changed=True instead of raising 409.
    _setup_checkpointer(
        monkeypatch,
        _checkpoint_tuple(action_type="create_workspace", include_snapshot=False),
    )
    out_legacy = await settle_resume_request(
        THREAD_ID,
        _resume_body(),
        principal=Principal(user_id=OWNER_ID, auth="session"),
        raw_request=_make_raw_request(),
        thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
    )
    assert out_legacy.hitl_response is not None
    assert out_legacy.hitl_response[INTERRUPT_ID].scope_changed is True


@pytest.mark.asyncio
async def test_oss_mode_enforces_owner_secret_when_set_and_blocks_tokens_channels_and_cross_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSS passwordless loopback trial mode:
    - Without LANGALPHA_OWNER_SECRET: rejects Authorization, X-Service-Token,
      channel markers (platform/external_thread_id), and Sec-Fetch-Site: cross-site;
      allows remaining loopback session requests matching interrupt_id & snapshot.
    - With LANGALPHA_OWNER_SECRET: enforces x-langalpha-owner HMAC verification.
    """
    _set_auth_mode(monkeypatch, "oss")
    monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))
    session_principal = Principal(user_id=OWNER_ID, auth="session")
    meta = {"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID}

    # 1. Passwordless loopback trial (no owner secret, no token/channel/cross-site) -> ALLOWED
    out = await settle_resume_request(
        THREAD_ID,
        _resume_body(),
        principal=session_principal,
        raw_request=_make_raw_request(headers={}),
        thread_meta=meta,
    )
    assert out.resume_run_horizon == 7

    # 2. Cross-site Fetch Metadata -> 403
    with pytest.raises(HTTPException) as exc_csrf:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=session_principal,
            raw_request=_make_raw_request(headers={"Sec-Fetch-Site": "cross-site"}),
            thread_meta=meta,
        )
    assert exc_csrf.value.status_code == 403
    assert exc_csrf.value.detail["code"] == "web_session_required"

    # 3. Non-empty Authorization, X-Service-Token, or channel markers -> 403
    for bad_headers, bad_body in (
        ({"Authorization": "Bearer some-api-token"}, _resume_body()),
        ({"X-Service-Token": "svc-token"}, _resume_body()),
        ({}, _resume_body(platform="feishu")),
        ({}, _resume_body(external_thread_id="ext-1")),
    ):
        with pytest.raises(HTTPException) as exc_bad:
            await settle_resume_request(
                THREAD_ID,
                bad_body,
                principal=session_principal,
                raw_request=_make_raw_request(headers=bad_headers),
                thread_meta=meta,
            )
        assert exc_bad.value.status_code == 403

    # 4. Desktop app with LANGALPHA_OWNER_SECRET configured
    monkeypatch.setenv("LANGALPHA_OWNER_SECRET", "desktop-owner-secret-xyz")
    with pytest.raises(HTTPException) as exc_no_owner:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=session_principal,
            raw_request=_make_raw_request(headers={}),
            thread_meta=meta,
        )
    assert exc_no_owner.value.status_code == 403

    out_desktop = await settle_resume_request(
        THREAD_ID,
        _resume_body(),
        principal=session_principal,
        raw_request=_make_raw_request(
            headers={"x-langalpha-owner": "desktop-owner-secret-xyz"}
        ),
        thread_meta=meta,
    )
    assert out_desktop.resume_run_horizon == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["ask_user_question", "credit_pause"])
async def test_channel_service_resume_for_other_interrupts_remains_unchanged(
    monkeypatch: pytest.MonkeyPatch, action_type: str
) -> None:
    """Channel gateway resuming ask_user_question or credit_pause via service auth
    must continue to work unchanged."""
    _set_auth_mode(monkeypatch, "supabase")
    _setup_checkpointer(
        monkeypatch,
        _checkpoint_tuple(action_type=action_type, include_snapshot=False),
    )

    out = await settle_resume_request(
        THREAD_ID,
        _resume_body(platform="feishu", external_thread_id="ext-1"),
        principal=Principal(user_id=OWNER_ID, auth="service"),
        raw_request=_make_raw_request(headers={"X-Service-Token": "svc-token"}),
        thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
    )
    assert out.resume_run_horizon == 7


@pytest.mark.asyncio
async def test_route_rejects_service_caller_combining_fork_and_hitl_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route-level regression for Codex High 1: a service caller sending both
    fork_from_turn and hitl_response must be rejected with HTTP 400 before
    _settle_against_branch so confirmation verification cannot be bypassed."""
    _set_auth_mode(monkeypatch, "local")
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    app = _app_for(OWNER_ID)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=OWNER_ID, auth="service"
    )
    workflow = create_autospec(
        astream_ptc_workflow, return_value=_empty_async_gen()
    )
    with (
        _stub_downstream(owner_id=OWNER_ID, ws_owner=OWNER_ID),
        patch("src.server.handlers.chat.astream_ptc_workflow", workflow),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{THREAD_ID}/messages",
                json={
                    "workspace_id": "ws-placeholder",
                    "messages": [],
                    "checkpoint_id": "cp-newest",
                    "fork_from_turn": 1,
                    "hitl_response": {
                        INTERRUPT_ID: {"decisions": [{"type": "approve"}]}
                    },
                },
            )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_request"
    workflow.assert_not_called()


@pytest.mark.asyncio
async def test_route_enforces_web_session_for_create_workspace_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real FastAPI route enforces first-party session for create_workspace HITL
    resumes: service principal gets 403, first-party session initiator gets 200."""
    _set_auth_mode(monkeypatch, "local")
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    # 1. Service caller -> 403 web_session_required
    app_svc = _app_for(OWNER_ID)
    app_svc.dependency_overrides[get_principal] = lambda: Principal(
        user_id=OWNER_ID, auth="service"
    )
    workflow_svc = create_autospec(
        astream_ptc_workflow, return_value=_empty_async_gen()
    )
    with (
        _stub_downstream(owner_id=OWNER_ID, ws_owner=OWNER_ID),
        patch("src.server.handlers.chat.astream_ptc_workflow", workflow_svc),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_svc), base_url="http://test"
        ) as client:
            resp_svc = await client.post(
                f"/api/v1/threads/{THREAD_ID}/messages",
                json={
                    "workspace_id": "ws-placeholder",
                    "messages": [],
                    "hitl_response": {
                        INTERRUPT_ID: {"decisions": [{"type": "approve"}]}
                    },
                },
            )
    assert resp_svc.status_code == 403
    assert resp_svc.json()["detail"]["code"] == "web_session_required"
    workflow_svc.assert_not_called()

    # 2. First-party session initiator -> 200
    app_web = _app_for(OWNER_ID)
    app_web.dependency_overrides[get_principal] = lambda: Principal(
        user_id=OWNER_ID, auth="session"
    )
    workflow_web = create_autospec(
        astream_ptc_workflow, return_value=_empty_async_gen()
    )
    from src.server.database.runs import lifecycle as _lifecycle

    with (
        _stub_downstream(owner_id=OWNER_ID, ws_owner=OWNER_ID),
        patch("src.server.handlers.chat.astream_ptc_workflow", workflow_web),
        # DALTON-HOOK(team-model-catalog): the resume route check reads the checkpoint's owning run.
        patch.object(
            checkpoint_handler,
            "resolve_checkpoint_resume_run_id",
            create_autospec(checkpoint_handler.resolve_checkpoint_resume_run_id, return_value=None),
        ),
        patch.object(
            _lifecycle,
            "get_latest_attempt",
            create_autospec(_lifecycle.get_latest_attempt, return_value=None),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_web), base_url="http://test"
        ) as client:
            resp_web = await client.post(
                f"/api/v1/threads/{THREAD_ID}/messages",
                json={
                    "workspace_id": "ws-placeholder",
                    "messages": [],
                    "hitl_response": {
                        INTERRUPT_ID: {"decisions": [{"type": "approve"}]}
                    },
                },
            )
    assert resp_web.status_code == 200
    workflow_web.assert_called_once()


@pytest.mark.asyncio
async def test_first_cos_message_without_primary_binds_home_and_reaches_session_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for Round 3 Decision 1: a user with no primary computer yet
    sending their first Chief of Staff message runs through real messaging.py ->
    route.bind -> ensure_home_bound -> ensure_primary_computer -> real
    astream_ptc_workflow -> acquire_session -> resolve_binding ->
    get_session_for_workspace without failing with WorkspaceNotOnComputer."""
    from contextlib import ExitStack

    from ptc_agent.config.core import SandboxConfig
    from src.server.database import workspace as ws_db
    from src.server.handlers.chat import ptc_run
    from src.server.handlers.chat.request_prep import PriorThread
    from src.server.services import home_placement
    from src.server.services.background_registry_store import BackgroundRegistryStore
    from src.server.services.computer_manager import _machines
    from src.server.services.report_back.handoff.carry import PriorAttempt
    from src.server.services.runs.coordinator import RunHandle
    from src.server.services.shared_workspaces import turn as sw_turn
    from src.server.services.workspace_manager import WorkspaceManager
    from src.tools.decorators import ToolUsageTracker
    from src.utils.tracking import TokenTrackingManager

    home_id = get_home_workspace_id(OWNER_ID)
    app = _app_for(OWNER_ID)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=OWNER_ID, auth="session"
    )

    state: dict[str, Any] = {
        "primary": None,
        "home_computer": None,
        "resolved_binding": None,
        "session_acquired_for": None,
        "restored_siblings_for": None,
    }
    promoted_computer = {
        "computer_id": COMPUTER_ID,
        "user_id": OWNER_ID,
        "kind": "docker",
        "name": "Primary Cloud",
        "is_primary": True,
        "status": "stopped",
        "root_dir": "/home/workspace",
        "dir_name": "Home",
        "provider_ref": "sbx-1",
        "config": {},
    }

    sandbox_cfg = create_autospec(SandboxConfig, instance=True)
    sandbox_cfg.provider = "docker"
    sandbox_cfg.creatable_providers.return_value = ("docker",)

    real_mgr = object.__new__(ComputerManager)
    real_mgr.config = SimpleNamespace(
        sandbox=sandbox_cfg,
        filesystem=SimpleNamespace(working_directory="/home/workspace"),
    )
    real_mgr.has_ready_session = lambda _ws: True  # type: ignore[assignment]

    async def _place_on_computer(ws_id, computer, **_kwargs):
        state["home_computer"] = {**computer, "dir_name": "Home"}
        return {"workspace_id": ws_id, "status": computer["status"]}

    real_mgr._place_on_computer = _place_on_computer  # type: ignore[method-assign]

    async def _get_session_for_workspace(ws_id, **_kwargs):
        # Exercise real resolve_binding on the freshly bound Home workspace.
        binding = await real_mgr.resolve_binding(ws_id)
        state["resolved_binding"] = binding
        state["session_acquired_for"] = ws_id
        return SimpleNamespace(sandbox=SimpleNamespace(), binding=binding)

    real_mgr.get_session_for_workspace = _get_session_for_workspace  # type: ignore[method-assign]

    async def _restore_sibling_folders(_session, ws_id):
        state["restored_siblings_for"] = ws_id

    real_mgr.restore_sibling_folders = _restore_sibling_folders  # type: ignore[method-assign]

    async def _get_computer_for_ws(ws_id, **_kwargs):
        return state["home_computer"] if ws_id == home_id else None

    async def _get_primary(_uid, **_kwargs):
        return state["primary"]

    async def _promote_primary(_uid):
        state["primary"] = promoted_computer
        return promoted_computer

    class _StopAfterSession(BaseException):
        pass

    sentinel_registry_store = create_autospec(BackgroundRegistryStore, instance=True)
    sentinel_registry_store.get_or_create_registry.side_effect = _StopAfterSession(
        "reached past acquire_session"
    )
    token_callback = create_autospec(TokenTrackingManager, instance=True)
    tool_tracker = create_autospec(ToolUsageTracker, instance=True)
    run_handle = RunHandle(
        run_id="run-first-1",
        thread_id=THREAD_ID,
        turn_index=1,
        attempt_no=1,
        finalized=False,
        origin=None,
    )

    def _close_detached(coro: Any, *_args: Any, **_kwargs: Any) -> None:
        if inspect.iscoroutine(coro):
            coro.close()

    ptc = "src.server.handlers.chat.ptc_run"
    patches = (
        _stub_downstream(owner_id=None, ws_owner=OWNER_ID),
        patch(
            "src.server.database.workspace.get_workspace",
            create_autospec(ws_db.get_workspace, return_value=None),
        ),
        patch(
            "src.server.database.workspace.get_or_create_home_workspace",
            create_autospec(
                ws_db.get_or_create_home_workspace,
                return_value={
                    "workspace_id": home_id,
                    "user_id": OWNER_ID,
                    "computer_id": None,
                    "dir_name": None,
                },
            ),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=real_mgr),
        ),
        patch(
            "src.server.services.computer_manager._machines.get_computer_for_workspace",
            create_autospec(
                _machines.get_computer_for_workspace,
                side_effect=_get_computer_for_ws,
            ),
        ),
        patch(
            "src.server.services.computer_manager._sessions.get_computer_for_workspace",
            create_autospec(
                _machines.get_computer_for_workspace,
                side_effect=_get_computer_for_ws,
            ),
        ),
        patch(
            "src.server.services.computer_manager._machines.db_get_workspace",
            create_autospec(
                ws_db.get_workspace,
                return_value={
                    "workspace_id": home_id,
                    "user_id": OWNER_ID,
                    "computer_id": None,
                    "dir_name": None,
                },
            ),
        ),
        patch(
            "src.server.services.computer_manager._machines.get_primary_computer",
            create_autospec(_machines.get_primary_computer, side_effect=_get_primary),
        ),
        patch(
            "src.server.services.computer_manager._machines.promote_primary_computer",
            create_autospec(
                _machines.promote_primary_computer, side_effect=_promote_primary
            ),
        ),
        patch(
            "src.server.services.computer_manager._machines.publish_workspace_binding_change",
            create_autospec(_machines.publish_workspace_binding_change, return_value=None),
        ),
        patch(
            "src.server.services.home_placement.get_home_computer",
            create_autospec(
                home_placement.get_home_computer,
                side_effect=lambda _uid, *, conn=None: state["home_computer"],
            ),
        ),
        patch(
            "src.server.services.home_placement.computer_unreachable",
            create_autospec(home_placement.computer_unreachable, return_value=False),
        ),
        patch(
            "src.server.services.home_placement.home_back_notice",
            create_autospec(home_placement.home_back_notice, return_value=None),
        ),
        # Keep real astream_ptc_workflow (undo _stub_downstream's replacement).
        patch("src.server.handlers.chat.astream_ptc_workflow", astream_ptc_workflow),
        patch(
            f"{ptc}.carry.prior_attempt",
            new=create_autospec(
                ptc_run.carry.prior_attempt,
                return_value=PriorAttempt(latest=None, retried=None),
            ),
        ),
        patch(
            f"{ptc}.ensure_thread",
            new=create_autospec(ptc_run.ensure_thread, return_value=PriorThread()),
        ),
        patch(
            f"{ptc}._resolve_origin_meta",
            new=create_autospec(ptc_run._resolve_origin_meta, return_value={}),
        ),
        patch(
            f"{ptc}._resolve_fork",
            new=create_autospec(
                ptc_run._resolve_fork,
                return_value=("create workspace NVDA", None),
            ),
        ),
        patch(
            f"{ptc}.begin_run",
            new=create_autospec(ptc_run.begin_run, return_value=run_handle),
        ),
        patch(
            f"{ptc}.wait_or_steer",
            new=create_autospec(ptc_run.wait_or_steer, return_value=(True, None)),
        ),
        patch(
            "src.server.services.shared_workspaces.turn.get_user_profile_for_prompt",
            new=create_autospec(sw_turn.get_user_profile_for_prompt, return_value=None),
        ),
        patch(
            f"{ptc}.init_tracking",
            new=create_autospec(
                ptc_run.init_tracking, return_value=(token_callback, tool_tracker)
            ),
        ),
        patch(
            f"{ptc}.apply_fetch_override",
            new=create_autospec(ptc_run.apply_fetch_override, return_value=None),
        ),
        patch(
            f"{ptc}._fire_and_forget",
            new=create_autospec(ptc_run._fire_and_forget, side_effect=_close_detached),
        ),
        patch(
            f"{ptc}.update_workspace_activity",
            new=create_autospec(ptc_run.update_workspace_activity, return_value=None),
        ),
        patch(
            f"{ptc}.keep_named_model",
            new=create_autospec(ptc_run.keep_named_model, return_value=None),
        ),
        patch(
            f"{ptc}.BackgroundRegistryStore.get_instance",
            new=create_autospec(
                BackgroundRegistryStore.get_instance,
                return_value=sentinel_registry_store,
            ),
        ),
    )
    with ExitStack() as stack:
        for ctx in patches:
            stack.enter_context(ctx)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            with pytest.raises(_StopAfterSession):
                await client.post(
                    f"/api/v1/threads/{THREAD_ID}/messages",
                    json={
                        "workspace_id": home_id,
                        "messages": [{"role": "user", "content": "create workspace NVDA"}],
                    },
                )

    assert state["home_computer"] is not None
    assert state["resolved_binding"] is not None
    assert state["resolved_binding"].computer_id == COMPUTER_ID
    assert state["session_acquired_for"] == home_id
    assert state["restored_siblings_for"] == home_id


def test_sse_producer_interrupt_debug_log_emits_only_whitelisted_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SSE DEBUG logging must never print raw interrupt_obj.value or full event_data."""
    producer = object.__new__(RunSSEProducer)
    producer.thread_id = THREAD_ID
    producer.workspace_id = "ws-1"
    producer.seq = 0
    producer._build_sse_event = lambda event, data: SimpleNamespace(event=event, data=data)  # type: ignore[method-assign]

    secret_marker = "SENSITIVE_INTERRUPT_SECRET_PAYLOAD_999"
    intr = SimpleNamespace(
        id=INTERRUPT_ID,
        value={
            "kind": "create_workspace",
            "action_requests": [
                {
                    "type": "create_workspace",
                    "workspace_name": secret_marker,
                }
            ],
        },
    )
    with caplog.at_level(logging.DEBUG, logger="src.server.services.runs.sse_producer"):
        producer._handle_interrupt({"__interrupt__": [intr]})

    combined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_marker not in combined
    assert "action_types=['create_workspace']" in combined
    assert INTERRUPT_ID in combined


@pytest.mark.asyncio
async def test_rejecting_expired_card_without_snapshot_preserves_reject_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Decision 6: when the authenticated initiator clicks Reject on an
    expired/pre-upgrade card missing approval_snapshots (or with a drifted
    snapshot), settle_resume_request keeps the reject decision instead of
    rewriting it to approve + scope_changed so live display and replay agree."""
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(include_snapshot=False))
    raw_req = _make_raw_request(headers={"sec-fetch-site": "same-origin"})
    principal = Principal(user_id=OWNER_ID, auth="session")
    thread_meta = {"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID}

    reject_req = _resume_body(decision_type="reject")
    approve_req = _resume_body(decision_type="approve")

    with (
        patch("src.dalton.settings.get_dalton_auth_mode", return_value="local"),
        patch("src.dalton.auth.is_dalton_supabase_auth_enabled", return_value=False),
    ):
        settled_reject = await settle_resume_request(
            THREAD_ID,
            reject_req,
            principal=principal,
            raw_request=raw_req,
            thread_meta=thread_meta,
        )
        settled_approve = await settle_resume_request(
            THREAD_ID,
            approve_req,
            principal=principal,
            raw_request=raw_req,
            thread_meta=thread_meta,
        )

    assert settled_reject.hitl_response is not None
    rej_answer = settled_reject.hitl_response[INTERRUPT_ID]
    assert rej_answer.decisions[0].type == "reject"
    assert not rej_answer.scope_changed

    assert settled_approve.hitl_response is not None
    app_answer = settled_approve.hitl_response[INTERRUPT_ID]
    assert app_answer.decisions[0].type == "approve"
    assert app_answer.scope_changed is True


@pytest.mark.asyncio
async def test_checkpoint_resume_guard_report_delivery_blocks_pat_oauth_computer_and_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AD5 r2: dalton_report_delivery_set is enforced by _verify_web_confirm_resume:
    PAT/OAuth/computer/service tokens, channel callers, and non-initiators are rejected 403;
    session initiator must supply the matching confirm_token (explicit reject still allowed)."""
    from src.dalton.services.report_delivery import compute_report_delivery_confirm_token

    auto_id = "33333333-3333-3333-3333-333333333333"
    report_targets = ["feishu:chat:oc_group_1"]
    valid_token = compute_report_delivery_confirm_token(
        action="set",
        caller_user_id=OWNER_ID,
        automation_id=auto_id,
        config_version=3,
        profile="daily-filing-alert",
        report_targets=report_targets,
        text_targets=None,
        failure_targets=[],
    )
    action_req = {
        "type": "dalton_report_delivery_set",
        "action": "set",
        "automation_id": auto_id,
        "automation_name": "Daily Filing",
        "config_version": 3,
        "profile": "daily-filing-alert",
        "report_targets": report_targets,
        "text_targets": None,
        "failure_targets": [],
        "confirm_token": valid_token,
    }
    intr = SimpleNamespace(id="intr-rd-1", value={"action_requests": [action_req]})
    cp_tuple = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "cp-rd-1"}},
        checkpoint={"channel_values": {}},
        pending_writes=[("task-1", "__interrupt__", (intr,))],
    )
    _setup_checkpointer(monkeypatch, cp_tuple)

    raw_req = _make_raw_request(headers={"sec-fetch-site": "same-origin"})
    thread_meta = {"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID}

    def _rd_resume(decision_type: str = "approve", token: str | None = valid_token, **extra: Any) -> ChatRequest:
        dec = HITLDecision(type=decision_type, confirm_token=token)  # type: ignore[arg-type]
        return ChatRequest(
            messages=[],
            hitl_response={"intr-rd-1": HITLResponse(decisions=[dec])},
            **extra,
        )

    with (
        patch("src.dalton.settings.get_dalton_auth_mode", return_value="local"),
        patch("src.dalton.auth.is_dalton_supabase_auth_enabled", return_value=False),
    ):
        # 1. PAT / OAuth / computer / service tokens with valid confirm_token are ALL refused 403.
        for bad_auth in ("pat", "oauth", "computer", "service"):
            with pytest.raises(HTTPException) as exc:
                await settle_resume_request(
                    THREAD_ID,
                    _rd_resume("approve", valid_token),
                    principal=Principal(user_id=OWNER_ID, auth=bad_auth),  # type: ignore[arg-type]
                    raw_request=raw_req,
                    thread_meta=thread_meta,
                )
            assert exc.value.status_code == 403
            assert exc.value.detail["code"] == "web_session_required"

        # 2. Channel surface (platform) refused 403.
        with pytest.raises(HTTPException) as exc_ch:
            await settle_resume_request(
                THREAD_ID,
                _rd_resume("approve", valid_token, platform="feishu"),
                principal=Principal(user_id=OWNER_ID, auth="session"),
                raw_request=raw_req,
                thread_meta=thread_meta,
            )
        assert exc_ch.value.status_code == 403
        assert exc_ch.value.detail["code"] == "web_session_required"

        # 3. Non-initiator web session refused 403.
        with pytest.raises(HTTPException) as exc_other:
            await settle_resume_request(
                THREAD_ID,
                _rd_resume("approve", valid_token),
                principal=Principal(user_id=OTHER_ID, auth="session"),
                raw_request=raw_req,
                thread_meta=thread_meta,
            )
        assert exc_other.value.status_code == 403
        assert exc_other.value.detail["code"] == "confirm_initiator_mismatch"

        # 4. Initiator web session with wrong/missing confirm_token refused 403.
        for bad_tok in (None, "wrong-token"):
            with pytest.raises(HTTPException) as exc_tok:
                await settle_resume_request(
                    THREAD_ID,
                    _rd_resume("approve", bad_tok),
                    principal=Principal(user_id=OWNER_ID, auth="session"),
                    raw_request=raw_req,
                    thread_meta=thread_meta,
                )
            assert exc_tok.value.status_code == 403
            assert exc_tok.value.detail["code"] == "confirm_token_mismatch"

        # 5. Initiator web session with matching confirm_token succeeds.
        settled_ok = await settle_resume_request(
            THREAD_ID,
            _rd_resume("approve", valid_token),
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=raw_req,
            thread_meta=thread_meta,
        )
        assert settled_ok.hitl_response is not None
        assert settled_ok.hitl_response["intr-rd-1"].decisions[0].confirm_token == valid_token

        # 6. Initiator web session explicit reject succeeds without token.
        settled_rej = await settle_resume_request(
            THREAD_ID,
            _rd_resume("reject", None),
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=raw_req,
            thread_meta=thread_meta,
        )
        assert settled_rej.hitl_response is not None
        assert settled_rej.hitl_response["intr-rd-1"].decisions[0].type == "reject"


@pytest.mark.asyncio
async def test_ad5_r3_passwordless_oss_rejects_report_delivery_approval_and_delegates_to_dalton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AD5 r3: passwordless OSS fallback (no LANGALPHA_OWNER_SECRET) refuses AD5 HITL approval
    with a message prompting the admin to enable login; owner-secret HMAC passes; AD5 token logic
    lives in src.dalton rather than inline in checkpoint_handler."""
    from src.dalton.services.report_delivery import compute_report_delivery_confirm_token

    assert not hasattr(checkpoint_handler, "_verify_report_delivery_interrupt_answer")
    assert not hasattr(checkpoint_handler, "_extract_answer_confirm_token")

    auto_id = "33333333-3333-3333-3333-333333333333"
    report_targets = ["feishu:chat:oc_group_1"]
    valid_token = compute_report_delivery_confirm_token(
        action="set",
        caller_user_id=OWNER_ID,
        automation_id=auto_id,
        config_version=3,
        profile="daily-filing-alert",
        report_targets=report_targets,
        text_targets=None,
        failure_targets=[],
    )
    action_req = {
        "type": "dalton_report_delivery_set",
        "action": "set",
        "automation_id": auto_id,
        "automation_name": "Daily Filing",
        "config_version": 3,
        "profile": "daily-filing-alert",
        "report_targets": report_targets,
        "text_targets": None,
        "failure_targets": [],
        "confirm_token": valid_token,
    }
    intr = SimpleNamespace(id="intr-rd-oss", value={"action_requests": [action_req]})
    cp_tuple = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "cp-rd-oss"}},
        checkpoint={"channel_values": {}},
        pending_writes=[("task-1", "__interrupt__", (intr,))],
    )
    _setup_checkpointer(monkeypatch, cp_tuple)
    _set_auth_mode(monkeypatch, "oss")
    monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)

    thread_meta = {"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID}
    approve_req = ChatRequest(
        messages=[],
        hitl_response={
            "intr-rd-oss": HITLResponse(
                decisions=[HITLDecision(type="approve", confirm_token=valid_token)]  # type: ignore[arg-type]
            )
        },
    )
    reject_req = ChatRequest(
        messages=[],
        hitl_response={
            "intr-rd-oss": HITLResponse(decisions=[HITLDecision(type="reject")])
        },
    )

    # Passwordless OSS fallback (no LANGALPHA_OWNER_SECRET) -> ALWAYS refused 403 on approve.
    with pytest.raises(HTTPException) as exc_oss:
        await settle_resume_request(
            THREAD_ID,
            approve_req,
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=_make_raw_request(headers={"sec-fetch-site": "same-origin"}),
            thread_meta=thread_meta,
        )
    assert exc_oss.value.status_code == 403
    assert exc_oss.value.detail["code"] == "web_session_required"
    assert "启用登录" in exc_oss.value.detail["message"]

    # Explicit reject still succeeds so a pending card can be dismissed cleanly.
    settled_rej = await settle_resume_request(
        THREAD_ID,
        reject_req,
        principal=Principal(user_id=OWNER_ID, auth="session"),
        raw_request=_make_raw_request(headers={"sec-fetch-site": "same-origin"}),
        thread_meta=thread_meta,
    )
    assert settled_rej.hitl_response is not None
    assert settled_rej.hitl_response["intr-rd-oss"].decisions[0].type == "reject"

    # Desktop app with LANGALPHA_OWNER_SECRET + valid x-langalpha-owner HMAC header succeeds.
    monkeypatch.setenv("LANGALPHA_OWNER_SECRET", "desktop-owner-secret-123")
    settled_desktop = await settle_resume_request(
        THREAD_ID,
        approve_req,
        principal=Principal(user_id=OWNER_ID, auth="session"),
        raw_request=_make_raw_request(
            headers={"x-langalpha-owner": "desktop-owner-secret-123"}
        ),
        thread_meta=thread_meta,
    )
    assert settled_desktop.hitl_response is not None
    assert settled_desktop.hitl_response["intr-rd-oss"].decisions[0].confirm_token == valid_token
