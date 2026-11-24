"""Batch 2 unit tests: backend confirmation authenticity verification across
DALTON_AUTH_MODE in ('local', 'supabase', 'oss'), covering all Principal.auth
literals ('session', 'service', 'oauth', 'pat', 'computer'), unauthenticated
API clients in OSS loopback mode, initiator checks, interrupt/snapshot matching,
and unchanged behavior for non-workspace interrupts (ask_user_question / credit_pause).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.server.database.conversation import senders, threads_read
from src.server.dependencies.principal import Principal
from src.server.handlers import checkpoint_handler
from src.server.handlers.checkpoint_handler import settle_resume_request
from src.server.models.chat import ChatRequest, HITLDecision, HITLResponse

_REAL_GET_THREAD_BRANCH_READ = threads_read.get_thread_branch_read
_REAL_TURN_BASE_CHECKPOINT_ID = checkpoint_handler.turn_base_checkpoint_id

OWNER_ID = "user-owner"
OTHER_ID = "user-other"
THREAD_ID = "11111111-1111-1111-1111-111111111111"
INTERRUPT_ID = "intr-ws-1"
TOOL_CALL_ID = "call-ws-1"
COMPUTER_ID = "22222222-2222-2222-2222-222222222222"
CONFIRM_TOKEN = "tok-server-secret-123456"


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
            "confirm_token": CONFIRM_TOKEN,
        }
        default_snap: dict[str, Any] = {
            "action": "create_workspace",
            "tool_call_id": tool_call_id,
            "workspace_name": "NVDA",
            "computer_id": COMPUTER_ID,
            "computer_is_own_machine": True,
            "bootstrap_cloud_primary": False,
            "confirm_token": CONFIRM_TOKEN,
        }
    elif action_type == "archive_workspace":
        action_req = {
            "type": "archive_workspace",
            "tool_call_id": tool_call_id,
            "workspace_id": "ws-1",
            "computer_id": COMPUTER_ID,
            "confirm_token": CONFIRM_TOKEN,
        }
        default_snap = {
            "action": "archive_workspace",
            "tool_call_id": tool_call_id,
            "workspace_id": "ws-1",
            "computer_id": COMPUTER_ID,
            "confirm_token": CONFIRM_TOKEN,
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


def _setup_checkpointer(monkeypatch: pytest.MonkeyPatch, cp_tuple: Any) -> None:
    branch_read = SimpleNamespace(checkpoint_id="cp-newest", run_horizon=7)
    monkeypatch.setattr(
        threads_read,
        "get_thread_branch_read",
        create_autospec(_REAL_GET_THREAD_BRANCH_READ, return_value=branch_read),
    )

    class _FakeCP:
        async def aget_tuple(self, config: Any) -> Any:
            return cp_tuple

    cp = create_autospec(_FakeCP, instance=True)
    cp.aget_tuple.return_value = cp_tuple
    monkeypatch.setattr(
        "src.server.handlers.checkpoint_handler.get_checkpointer", lambda: cp
    )
    monkeypatch.setattr(
        "src.server.handlers.checkpoint_handler.turn_base_checkpoint_id",
        create_autospec(_REAL_TURN_BASE_CHECKPOINT_ID, return_value=None),
    )


def _resume_body(
    *,
    interrupt_id: str = INTERRUPT_ID,
    decision_type: str = "approve",
    confirm_token: str | None = CONFIRM_TOKEN,
    platform: str | None = None,
    external_thread_id: str | None = None,
) -> ChatRequest:
    return ChatRequest(
        messages=[],
        hitl_response={
            interrupt_id: HITLResponse(
                decisions=[
                    HITLDecision(type=decision_type, confirm_token=confirm_token)
                ]
            )
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

    # Tampered snapshot (computer_id differs from interrupt payload)
    _setup_checkpointer(
        monkeypatch,
        _checkpoint_tuple(
            action_type="create_workspace",
            snapshot={
                "action": "create_workspace",
                "tool_call_id": TOOL_CALL_ID,
                "workspace_name": "NVDA",
                "computer_id": "99999999-9999-9999-9999-999999999999",
                "computer_is_own_machine": True,
                "bootstrap_cloud_primary": False,
                "confirm_token": CONFIRM_TOKEN,
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
async def test_oss_mode_allows_loopback_web_browser_and_desktop_owner_secret_but_rejects_unauthenticated_api_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r3 #2 + C16:
    - Unauthenticated API client (no Origin, no Sec-Fetch-Site, no owner secret) -> 403
    - Loopback browser (Origin=http://127.0.0.1:8088, Sec-Fetch-Site=same-origin, valid confirm_token) -> allowed
    - Desktop app with LANGALPHA_OWNER_SECRET -> requires matching x-langalpha-owner header
    - Authorization header, X-Service-Token, platform/external_thread_id, cross-site Origin, or missing confirm_token -> 403
    """
    _set_auth_mode(monkeypatch, "oss")
    monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)
    _setup_checkpointer(monkeypatch, _checkpoint_tuple(action_type="create_workspace"))
    session_principal = Principal(user_id=OWNER_ID, auth="session")
    meta = {"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID}

    # 1. Loopback web browser with same-origin headers + confirm_token -> ALLOWED
    out = await settle_resume_request(
        THREAD_ID,
        _resume_body(),
        principal=session_principal,
        raw_request=_make_raw_request(
            headers={
                "Origin": "http://127.0.0.1:8088",
                "Sec-Fetch-Site": "same-origin",
            }
        ),
        thread_meta=meta,
    )
    assert out.resume_run_horizon == 7

    # 2. Unauthenticated API client (no Origin, no Sec-Fetch-Site, no credentials) -> 403
    with pytest.raises(HTTPException) as exc_api:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=session_principal,
            raw_request=_make_raw_request(headers={}),
            thread_meta=meta,
        )
    assert exc_api.value.status_code == 403
    assert exc_api.value.detail["code"] == "web_session_required"

    # 3. Missing/wrong confirm_token even with loopback Origin -> 403
    with pytest.raises(HTTPException) as exc_tok:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(confirm_token=None),
            principal=session_principal,
            raw_request=_make_raw_request(
                headers={
                    "Origin": "http://127.0.0.1:8088",
                    "Sec-Fetch-Site": "same-origin",
                }
            ),
            thread_meta=meta,
        )
    assert exc_tok.value.status_code == 403

    # 4. Cross-site Origin / Sec-Fetch-Site -> 403
    with pytest.raises(HTTPException) as exc_csrf:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=session_principal,
            raw_request=_make_raw_request(
                headers={
                    "Origin": "https://evil.example",
                    "Sec-Fetch-Site": "cross-site",
                }
            ),
            thread_meta=meta,
        )
    assert exc_csrf.value.status_code == 403

    # 5. Non-empty Authorization, X-Service-Token, or channel platform -> 403
    for bad_headers, bad_body in (
        (
            {
                "Origin": "http://127.0.0.1:8088",
                "Sec-Fetch-Site": "same-origin",
                "Authorization": "Bearer some-api-token",
            },
            _resume_body(),
        ),
        (
            {
                "Origin": "http://127.0.0.1:8088",
                "Sec-Fetch-Site": "same-origin",
                "X-Service-Token": "svc-token",
            },
            _resume_body(),
        ),
        (
            {"Origin": "http://127.0.0.1:8088", "Sec-Fetch-Site": "same-origin"},
            _resume_body(platform="feishu"),
        ),
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

    # 6. Desktop app with LANGALPHA_OWNER_SECRET configured
    monkeypatch.setenv("LANGALPHA_OWNER_SECRET", "desktop-owner-secret-xyz")
    # Missing x-langalpha-owner -> 403 even with loopback Origin
    with pytest.raises(HTTPException) as exc_no_owner:
        await settle_resume_request(
            THREAD_ID,
            _resume_body(),
            principal=session_principal,
            raw_request=_make_raw_request(
                headers={
                    "Origin": "http://127.0.0.1:8088",
                    "Sec-Fetch-Site": "same-origin",
                }
            ),
            thread_meta=meta,
        )
    assert exc_no_owner.value.status_code == 403

    # Valid x-langalpha-owner + confirm_token -> ALLOWED
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
        _resume_body(confirm_token=None, platform="feishu", external_thread_id="ext-1"),
        principal=Principal(user_id=OWNER_ID, auth="service"),
        raw_request=_make_raw_request(headers={"X-Service-Token": "svc-token"}),
        thread_meta={"user_id": OWNER_ID, "workspace_owner_id": OWNER_ID},
    )
    assert out.resume_run_horizon == 7
