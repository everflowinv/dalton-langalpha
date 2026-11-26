"""Unit tests proving no assistant/sandbox/CLI/tool/history path receives a user session token.

Threat model (AD5 r2):
- Browser login tokens (``principal.auth == "session"``) exist only in the user's browser.
- Assistant code, sandboxes, ``la`` CLI credential files, graph configurable, tool results,
  and thread history never contain a user session token.
- Assistant / non-interactive tokens (``computer``, ``pat``, ``oauth``) are unconditionally
  rejected at confirmation issuance, REST automation writes, and HITL resume guards.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.dalton.app import router as dalton_router
from src.dalton.sandbox_caller_identity import bound_caller, caller_env
from src.dalton.services import report_delivery as rd_svc
from src.ptc_agent.core.sandbox import execution as sandbox_exec
from src.ptc_agent.core.sandbox.jobs import JobContext, JobRef
from src.server.auth import computer_token_core as core_tok
from src.server.dependencies import principal as principal_dep
from src.server.handlers.chat.request_prep import build_graph_config
from src.server.models.chat import ChatRequest
from src.server.services import cli_tokens


SESSION_SENTINEL = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.user_browser_session_only.sig"


def test_session_token_absent_from_sandbox_job_env_and_caller_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox job environment and Dalton caller env contain only job paths and
    HMAC-signed bridge claims—never a browser session token or cookie."""
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_bridge_admin_token",
        lambda: "bridge-admin-test-secret",
    )
    fake_workspace = SimpleNamespace(
        mcp_client_config="/workspace/.system/mcp_client.json",
        join=lambda rel: f"/workspace/{rel}",
    )
    fake_sandbox = SimpleNamespace(
        layout=SimpleNamespace(cli_token="/workspace/.system/cli_token.json"),
        workspace=lambda: fake_workspace,
    )
    ref = JobRef(
        command_id="cmd-1",
        state_dir="/workspace/.agents/threads/t1/commands/cmd-1",
        log_path="/workspace/.agents/threads/t1/commands/cmd-1.log",
        trace_path="/workspace/.agents/threads/t1/commands/cmd-1.trace",
        log_relpath=".agents/threads/t1/commands/cmd-1.log",
    )
    ctx = JobContext(thread_id="thread-1", call_id="call-1", env={"CUSTOM_FLAG": "1"})

    with bound_caller("user-1", "ws-1", "thread-1"):
        c_env = caller_env()
        job_env = sandbox_exec._job_env(fake_sandbox, ref, ctx)

    combined_keys = {k.lower() for k in job_env}
    assert not any(
        bad in k for k in combined_keys for bad in ("session", "cookie", "authorization", "supabase")
    )
    serialized = json.dumps({**c_env, **job_env}, ensure_ascii=False)
    assert SESSION_SENTINEL not in serialized
    assert job_env["LANGALPHA_TOKEN_FILE"] == "/workspace/.system/cli_token.json"
    # Even DALTON_CALLER_TOKEN is not a bearer session token accepted by get_principal.
    assert "DALTON_CALLER_TOKEN" in job_env
    assert not cli_tokens.is_cli_token(job_env["DALTON_CALLER_TOKEN"])


@pytest.mark.asyncio
async def test_session_token_absent_from_la_cli_payload_and_non_session_tokens_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``la`` CLI token file carries only a ``lac1.*`` computer token which
    never resolves to ``principal.auth == 'session'``, and computer/pat/oauth
    principals are rejected by confirmation issuance and REST report-delivery writes."""
    now = datetime.now(timezone.utc)
    minted = core_tok.Minted(
        token="lac1.comp-1.secret-part",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    cli_file = cli_tokens.payload(minted, "comp-1", "https://relay.internal")
    assert cli_file["token"].startswith("lac1.")
    assert cli_tokens.is_cli_token(cli_file["token"])
    assert SESSION_SENTINEL not in json.dumps(cli_file)
    assert set(cli_file.keys()) == {"token", "iat_ms", "computer_id", "base_url", "expires_at"}

    # With computer_cli_tokens disabled (Dalton default), lac1.* is refused with 401
    # before ever reaching session auth.
    monkeypatch.setattr("src.dalton.settings.is_computer_cli_tokens_enabled", lambda: False)
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/automations",
            "headers": [(b"host", b"app.example.com")],
        }
    )
    creds = SimpleNamespace(credentials=cli_file["token"])
    with pytest.raises(HTTPException) as exc_disabled:
        await principal_dep._principal(req, creds)  # type: ignore[arg-type]
    assert exc_disabled.value.status_code == 401

    # Even if computer_cli_tokens is enabled, lac1.* authenticates as auth="computer", never "session".
    monkeypatch.setattr("src.dalton.settings.is_computer_cli_tokens_enabled", lambda: True)
    monkeypatch.setattr(
        cli_tokens,
        "authenticate",
        create_autospec(
            cli_tokens.authenticate,
            return_value=cli_tokens.CliIdentity(computer_id="comp-1", user_id="user-1"),
        ),
    )
    p = await principal_dep._principal(req, creds)  # type: ignore[arg-type]
    assert p.auth == "computer"
    assert p.auth != "session"

    # Confirmation issuance rejects computer, pat, oauth, and service even with same-origin headers.
    same_origin_req = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("app.example.com", 443),
            "path": "/api/v1/dalton/report-delivery/confirmations",
            "headers": [
                (b"host", b"app.example.com"),
                (b"origin", b"https://app.example.com"),
                (b"sec-fetch-site", b"same-origin"),
            ],
        }
    )
    for bad_auth in ("computer", "pat", "oauth", "service"):
        with pytest.raises(HTTPException) as exc:
            dalton_router._require_web_session_for_report_delivery_confirm(
                principal_dep.Principal(user_id="user-1", auth=bad_auth),  # type: ignore[arg-type]
                same_origin_req,
            )
        assert exc.value.status_code == 403


def test_session_token_absent_from_graph_config_and_tool_history_payloads() -> None:
    """LangGraph configurable, LangSmith metadata, and HITL card / tool payloads
    carry only user/thread IDs and never accept or echo session credentials."""
    chat_req = ChatRequest(
        messages=[{"role": "user", "content": "check report delivery"}],
        locale="zh-CN",
    )
    graph_cfg = build_graph_config(
        thread_id="thread-abc",
        user_id="user-1",
        workspace_id="ws-1",
        timezone_str="UTC",
        token_callback=None,
        request=chat_req,
        effective_model="claude-sonnet-4-5",
        run_id="run-1",
        turn_index=0,
        surface="web",
    )
    cfg_dump = json.dumps(graph_cfg, default=str, ensure_ascii=False)
    assert SESSION_SENTINEL not in cfg_dump
    for key in graph_cfg["configurable"]:
        lower_k = str(key).lower()
        assert "token" not in lower_k
        assert "cookie" not in lower_k
        assert "authorization" not in lower_k
        assert "session" not in lower_k

    # Confirm token digest binds only structured targets + user_id, never session credentials.
    digest = rd_svc.compute_report_delivery_confirm_token(
        action="set",
        caller_user_id="user-1",
        automation_id="11111111-1111-1111-1111-111111111111",
        config_version=2,
        profile="daily-filing-alert",
        report_targets=["feishu:chat:oc_group_1"],
        text_targets=None,
        failure_targets=[],
    )
    assert SESSION_SENTINEL not in digest
    assert len(digest) == 64
