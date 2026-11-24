"""Batch 2 unit tests: backend confirmation authenticity verification across
DALTON_AUTH_MODE in ('local', 'supabase', 'oss'), covering all Principal.auth
literals ('session', 'service', 'oauth', 'pat', 'computer'), FastAPI route-level
mixed fork + hitl_response rejection, route-level first-party enforcement,
first Chief of Staff entry without own-machine promotion, SSE debug log
whitelisting, and unchanged behavior for other interrupts.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.base import BaseCheckpointSaver

from src.server.database.conversation import senders, threads_read
from src.server.database.home_workspace import get_home_workspace_id
from src.server.dependencies.principal import Principal, get_principal
from src.server.handlers import checkpoint_handler
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
            "computer_id": COMPUTER_ID,
            "computer_name": "My Mac",
            "computer_is_own_machine": True,
            "bootstrap_cloud_primary": False,
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
async def test_interrupt_id_or_snapshot_mismatch_returns_409_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_auth_mode(monkeypatch, "local")
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))

    # Wrong interrupt_id
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

    # Tampered snapshot (computer_id or computer_name differs from interrupt payload)
    _setup_checkpointer(
        monkeypatch,
        _checkpoint_tuple(
            action_type="create_workspace",
            snapshot={
                "action": "create_workspace",
                "tool_call_id": TOOL_CALL_ID,
                "workspace_name": "NVDA",
                "computer_id": "99999999-9999-9999-9999-999999999999",
                "computer_name": "My Mac",
                "computer_is_own_machine": True,
                "bootstrap_cloud_primary": False,
            },
        ),
    )
    with pytest.raises(HTTPException) as exc_info2:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=Principal(user_id=OWNER_ID, auth="session"),
            raw_request=_make_raw_request(),
            thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
        )
    assert exc_info2.value.status_code == 409
    assert exc_info2.value.detail["code"] == "scope_changed"


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
        lambda **kwargs: _empty_async_gen(), return_value=_empty_async_gen()
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
        lambda **kwargs: _empty_async_gen(), return_value=_empty_async_gen()
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
        lambda **kwargs: _empty_async_gen(), return_value=_empty_async_gen()
    )
    with (
        _stub_downstream(owner_id=OWNER_ID, ws_owner=OWNER_ID),
        patch("src.server.handlers.chat.astream_ptc_workflow", workflow_web),
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
async def test_route_first_cos_entry_does_not_promote_existing_own_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route-level regression for Codex High 3: entering Chief of Staff (Home)
    via messaging.py when the user has an unpromoted own machine and no primary
    computer defers Home binding through ensure_home_bound ->
    ensure_primary_computer(create_or_promote=False) and never calls
    promote_primary_computer or create_computer_for_user."""
    home_id = get_home_workspace_id(OWNER_ID)
    app = _app_for(OWNER_ID)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=OWNER_ID, auth="session"
    )

    real_mgr = object.__new__(ComputerManager)
    real_mgr.has_ready_session = lambda _ws: True  # type: ignore[assignment]
    create_computer_spy = create_autospec(
        real_mgr.create_computer_for_user,
        side_effect=AssertionError("create_computer_for_user must not be called"),
    )
    real_mgr.create_computer_for_user = create_computer_spy  # type: ignore[method-assign]
    promote_spy = create_autospec(
        lambda _uid: None,
        side_effect=AssertionError("promote_primary_computer must not be called"),
    )
    workflow = create_autospec(
        lambda **kwargs: _empty_async_gen(), return_value=_empty_async_gen()
    )

    with (
        _stub_downstream(owner_id=None, ws_owner=OWNER_ID),
        patch("src.server.database.workspace.get_workspace", AsyncMock(return_value=None)),
        patch(
            "src.server.database.workspace.get_or_create_home_workspace",
            AsyncMock(
                return_value={
                    "workspace_id": home_id,
                    "user_id": OWNER_ID,
                    "computer_id": None,
                    "dir_name": None,
                }
            ),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            return_value=real_mgr,
        ),
        patch(
            "src.server.services.computer_manager._machines.get_computer_for_workspace",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.computer_manager._machines.db_get_workspace",
            AsyncMock(
                return_value={
                    "workspace_id": home_id,
                    "user_id": OWNER_ID,
                    "computer_id": None,
                    "dir_name": None,
                }
            ),
        ),
        patch(
            "src.server.services.computer_manager._machines.get_primary_computer",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.computer_manager._machines.promote_primary_computer",
            promote_spy,
        ),
        patch(
            "src.server.services.home_placement.get_home_computer",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.home_placement.computer_unreachable",
            AsyncMock(return_value=False),
        ),
        patch("src.server.handlers.chat.astream_ptc_workflow", workflow),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{THREAD_ID}/messages",
                json={
                    "workspace_id": home_id,
                    "messages": [{"role": "user", "content": "create workspace NVDA"}],
                },
            )

    assert resp.status_code == 200
    create_computer_spy.assert_not_called()
    promote_spy.assert_not_called()
    workflow.assert_called_once()


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
